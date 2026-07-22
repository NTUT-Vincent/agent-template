"""Protocol-neutral application service。

這一層是整個範本最重要的「共用執行邊界」。

所有入口最終都應該收斂到這裡：

    A2A Executor -----------+
                           |
    Internal Celery Job ---+--> AgentRuntimeService
                                   |
                                   v
                               LangGraph
                                   |
                                   v
                               Agent SDK

為什麼需要 Service layer？

因為 A2A、REST、CLI、排程器都只是「入口」。
真正的 session mapping、LangGraph thread、SDK session resume，不應該散落在每個入口裡。
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
        # graph 是已經 compile 完成的 LangGraph。
        # 它內部已經綁定：
        #   - checkpointer
        #   - long-term store
        #   - Agent SDK node
        self._graph = graph

        # session_factory 用來讀寫 app_sessions / app_tasks。
        # 這些是我們 application 自己的 table，不是 LangGraph table。
        self._sessions = session_factory

    async def run_prompt(
        self,
        *,
        user_id: str,
        thread_id: str,
        prompt: str,
        remember: bool = False,
    ) -> dict:
        """在一個 durable LangGraph thread 上執行一輪 Agent。

        這個 function 是整個 runtime 最值得先讀懂的 contract。

        Args:
            user_id:
                Application-level identity / tenant scope。
                long-term memory namespace 會依賴這個值。

            thread_id:
                LangGraph durable workflow identity。
                同一 thread_id 會使用同一條 checkpoint history。

            prompt:
                這一輪要交給 Agent 的輸入。

            remember:
                是否把這輪 interaction 寫進 long-term store。

        Returns:
            protocol-neutral dict。
            A2A layer 會再包成 Artifact；Internal REST 則可直接存成 task result。

        注意三種 ID 不要混：

            conversation / thread_id
                -> LangGraph checkpoint

            sdk_session_id
                -> Claude Agent SDK native transcript / resume

            A2A task_id
                -> A2A protocol lifecycle
        """

        # ------------------------------------------------------------------
        # Step 1：Application session mapping
        # ------------------------------------------------------------------
        # app_sessions 的功能不是保存整段 LangGraph state。
        # 它只負責記錄 application 層的關聯：
        #
        #   user_id + thread_id <-> sdk_session_id
        #
        # LangGraph state 本身由 PostgresSaver 保存。
        async with self._sessions() as db:
            session = await db.scalar(
                select(SessionRow).where(SessionRow.thread_id == thread_id)
            )

            # A2A request 可能帶著一個新的 contextId 直接進來，
            # 所以這裡允許在第一次看到 thread_id 時建立 session mapping。
            if session is None:
                session = SessionRow(user_id=user_id, thread_id=thread_id)
                db.add(session)
                await db.commit()
                await db.refresh(session)
            elif session.user_id != user_id:
                # 同一 thread 不應該被另一個 tenant/user 接管。
                # 正式環境還需要更完整的 authn/authz，而不是只靠這一層檢查。
                raise PermissionError("thread belongs to a different user/tenant")

            # Claude Agent SDK 的 native session id。
            # 第一次執行可能是 None；Agent SDK 回傳 session_id 後再寫回 DB。
            sdk_session_id = session.sdk_session_id

        # ------------------------------------------------------------------
        # Step 2：進入 LangGraph
        # ------------------------------------------------------------------
        # 這裡才是 application runtime 真正開始執行 workflow 的地方。
        #
        # config.configurable.thread_id 很重要：
        # LangGraph checkpointer 用它辨識「這是哪一條 durable thread」。
        # Pod 換掉之後，只要 thread_id 一樣，就可以從 PostgreSQL checkpoint
        # 讀回相同 workflow context。
        output = await self._graph.ainvoke(
            {
                "user_id": user_id,
                "thread_id": thread_id,
                "prompt": prompt,
                "remember": remember,
                "sdk_session_id": sdk_session_id,
            },
            config={"configurable": {"thread_id": thread_id}},
            context=GraphContext(user_id=user_id),
        )

        # 對外不直接暴露整份 LangGraph state。
        # Service 把它整理成穩定的 application result contract。
        result = {
            "text": output.get("result_text", ""),
            "sdk_session_id": output.get("sdk_session_id"),
            "metadata": output.get("result_metadata", {}),
        }

        # ------------------------------------------------------------------
        # Step 3：更新 SDK session mapping
        # ------------------------------------------------------------------
        # Claude Agent SDK 每次執行後可能產生/更新 native session id。
        # 我們只把 mapping 寫回 app_sessions；真正 transcript 則在
        # PostgresClaudeSessionStore 裡。
        async with self._sessions() as db:
            session = await db.scalar(
                select(SessionRow).where(SessionRow.thread_id == thread_id)
            )
            if session is not None:
                session.sdk_session_id = result["sdk_session_id"]
                await db.commit()

        return result

    async def run_task(self, task_id: UUID) -> dict:
        """執行 internal Celery job。

        這個 function 只是包裝 application task lifecycle：

            queued -> running -> succeeded / failed

        真正 Agent 執行仍然委派給 run_prompt()。

        這個設計的目的：
        不讓「A2A 路徑」和「Celery 路徑」各自維護一套 Agent 邏輯。
        """
        async with self._sessions() as db:
            task = await db.get(TaskRow, task_id)
            if task is None:
                raise KeyError(f"task {task_id} not found")

            task.status = TaskStatus.RUNNING.value
            await db.commit()

            # 先把必要欄位複製出來，再離開 DB session。
            user_id = task.user_id
            thread_id = task.thread_id
            prompt = task.prompt
            remember = task.remember

        try:
            # 重要：internal task 與 A2A 最後共用相同 run_prompt()。
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
            # application task lifecycle 記 failed，方便 UI/Admin 查詢。
            # LangGraph 自己的 checkpoint 並不等於 app_tasks.status。
            async with self._sessions() as db:
                task = await db.get(TaskRow, task_id)
                if task is not None:
                    task.status = TaskStatus.FAILED.value
                    task.error = str(exc)
                    await db.commit()
            raise
