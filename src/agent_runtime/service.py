"""Protocol-neutral application service。

所有入口最終都收斂到這裡：

    A2A Executor -----------+
                           |
    Internal Celery Job ---+--> AgentRuntimeService
                                   |
                                   v
                               LangGraph
                                   |
                                   v
                               Agent SDK

Session recovery 的 application responsibility 是保存：

    LangGraph thread_id <-> Claude sdk_session_id

真正 transcript 則由 Claude SessionStore mirror 到 PostgreSQL。
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.db import SessionRow, TaskRow
from agent_runtime.domain import TaskStatus
from agent_runtime.graph.builder import GraphContext


class AgentRuntimeService:
    """A2A 與 internal job 共用的 Agent application service。"""

    def __init__(self, graph, session_factory: async_sessionmaker) -> None:
        self._graph = graph
        self._sessions = session_factory

    async def _persist_sdk_session_id(
        self,
        *,
        user_id: str,
        thread_id: str,
        sdk_session_id: str,
    ) -> None:
        """立即保存 `thread_id -> sdk_session_id` mapping。

        這個 method 會被 Claude init lifecycle callback 呼叫，而不是只在 ResultMessage
        後才執行。這是跨 Pod recovery 的關鍵：

            Claude init
              -> 取得 sdk_session_id
              -> COMMIT app_sessions
              -> Agent 繼續跑

        如果 Pod 之後 crash，下一顆 Pod 仍可以：

            thread_id
              -> app_sessions.sdk_session_id
              -> ClaudeAgentOptions(resume=...)
              -> PostgreSQL SessionStore.load()

        同一 thread 在本範本中不應該無聲切換到另一個 SDK session；若發現不同 ID，
        直接失敗，避免 conversation history 被錯誤接到另一份 transcript。
        """
        async with self._sessions() as db:
            session = await db.scalar(
                select(SessionRow).where(SessionRow.thread_id == thread_id)
            )
            if session is None:
                # 理論上 run_prompt 已先建 row，但 callback 保持 defensive，讓 lifecycle
                # persistence 不依賴呼叫順序的隱性假設。
                session = SessionRow(
                    user_id=user_id,
                    thread_id=thread_id,
                    sdk_session_id=sdk_session_id,
                )
                db.add(session)
            else:
                if session.user_id != user_id:
                    raise PermissionError("thread belongs to a different user/tenant")
                if session.sdk_session_id not in (None, sdk_session_id):
                    raise RuntimeError(
                        "Claude SDK session id changed unexpectedly for the same thread"
                    )
                session.sdk_session_id = sdk_session_id

            await db.commit()

    async def run_prompt(
        self,
        *,
        user_id: str,
        thread_id: str,
        prompt: str,
        remember: bool = False,
    ) -> dict:
        """在一個 durable LangGraph thread 上執行一輪 Agent。

        三種 persistence identity：

        `thread_id`
            LangGraph durable workflow identity / checkpoint key。

        `sdk_session_id`
            Claude Agent SDK native session identity；application 只保存 mapping。

        `SessionStore transcript`
            Claude SDK 原始 conversation/tool transcript，mirror 到 PostgreSQL。
        """
        # ------------------------------------------------------------------
        # Step 1：讀取或建立 application session mapping
        # ------------------------------------------------------------------
        async with self._sessions() as db:
            session = await db.scalar(
                select(SessionRow).where(SessionRow.thread_id == thread_id)
            )
            if session is None:
                session = SessionRow(user_id=user_id, thread_id=thread_id)
                db.add(session)
                await db.commit()
                await db.refresh(session)
            elif session.user_id != user_id:
                raise PermissionError("thread belongs to a different user/tenant")

            sdk_session_id = session.sdk_session_id

        # ------------------------------------------------------------------
        # Step 2：建立 early-session-id callback
        # ------------------------------------------------------------------
        async def persist_session_id_early(session_id: str) -> None:
            await self._persist_sdk_session_id(
                user_id=user_id,
                thread_id=thread_id,
                sdk_session_id=session_id,
            )

        # ------------------------------------------------------------------
        # Step 3：進入 LangGraph
        # ------------------------------------------------------------------
        output = await self._graph.ainvoke(
            {
                "user_id": user_id,
                "thread_id": thread_id,
                "prompt": prompt,
                "remember": remember,
                "sdk_session_id": sdk_session_id,
            },
            config={"configurable": {"thread_id": thread_id}},
            context=GraphContext(
                user_id=user_id,
                on_sdk_session_id=persist_session_id_early,
            ),
        )

        result = {
            "text": output.get("result_text", ""),
            "sdk_session_id": output.get("sdk_session_id"),
            "metadata": output.get("result_metadata", {}),
        }

        # ------------------------------------------------------------------
        # Step 4：ResultMessage 後再做一次 idempotent safeguard
        # ------------------------------------------------------------------
        # 正常情況 init callback 已經寫入 DB；這裡只是 fallback / consistency check。
        if result["sdk_session_id"]:
            await self._persist_sdk_session_id(
                user_id=user_id,
                thread_id=thread_id,
                sdk_session_id=result["sdk_session_id"],
            )

        return result

    async def run_task(self, task_id: UUID) -> dict:
        """執行 internal Celery job；真正 Agent 執行仍委派給 run_prompt()。"""
        async with self._sessions() as db:
            task = await db.get(TaskRow, task_id)
            if task is None:
                raise KeyError(f"task {task_id} not found")

            task.status = TaskStatus.RUNNING.value
            await db.commit()

            user_id = task.user_id
            thread_id = task.thread_id
            prompt = task.prompt
            remember = task.remember

        try:
            result = await self.run_prompt(
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                remember=remember,
            )

            async with self._sessions() as db:
                task = await db.get(TaskRow, task_id)
                assert task is not None
                task.status = TaskStatus.SUCCEEDED.value
                task.result = result
                await db.commit()

            return result
        except Exception as exc:
            async with self._sessions() as db:
                task = await db.get(TaskRow, task_id)
                if task is not None:
                    task.status = TaskStatus.FAILED.value
                    task.error = str(exc)
                    await db.commit()
            raise
