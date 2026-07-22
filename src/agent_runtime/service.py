from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.db import SessionRow, TaskRow
from agent_runtime.domain import TaskStatus
from agent_runtime.graph.builder import GraphContext


class AgentRuntimeService:
    """Application runtime shared by internal REST jobs and A2A requests."""

    def __init__(self, graph, session_factory: async_sessionmaker) -> None:
        self._graph = graph
        self._sessions = session_factory

    async def run_prompt(
        self,
        *,
        user_id: str,
        thread_id: str,
        prompt: str,
        remember: bool = False,
    ) -> dict:
        """Run one prompt on a durable LangGraph thread.

        This is the protocol-neutral execution boundary. Internal REST/Celery and
        the A2A AgentExecutor both delegate here instead of implementing separate
        agent loops.
        """
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
        result = {
            "text": output.get("result_text", ""),
            "sdk_session_id": output.get("sdk_session_id"),
            "metadata": output.get("result_metadata", {}),
        }

        async with self._sessions() as db:
            session = await db.scalar(
                select(SessionRow).where(SessionRow.thread_id == thread_id)
            )
            if session is not None:
                session.sdk_session_id = result["sdk_session_id"]
                await db.commit()

        return result

    async def run_task(self, task_id: UUID) -> dict:
        """Execute an internal queued task and persist its application status."""
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
