from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.db import SessionRow, TaskRow
from agent_runtime.domain import TaskStatus
from agent_runtime.graph.builder import GraphContext


class AgentRuntimeService:
    def __init__(self, graph, session_factory: async_sessionmaker) -> None:
        self._graph = graph
        self._sessions = session_factory

    async def run_task(self, task_id: UUID) -> dict:
        async with self._sessions() as db:
            task = await db.get(TaskRow, task_id)
            if task is None:
                raise KeyError(f"task {task_id} not found")
            task.status = TaskStatus.RUNNING.value
            await db.commit()

            session = await db.scalar(select(SessionRow).where(SessionRow.thread_id == task.thread_id))
            sdk_session_id = session.sdk_session_id if session else None

        try:
            output = await self._graph.ainvoke(
                {
                    "user_id": task.user_id,
                    "thread_id": task.thread_id,
                    "prompt": task.prompt,
                    "remember": task.remember,
                    "sdk_session_id": sdk_session_id,
                },
                config={"configurable": {"thread_id": task.thread_id}},
                context=GraphContext(user_id=task.user_id),
            )
            result = {
                "text": output.get("result_text", ""),
                "sdk_session_id": output.get("sdk_session_id"),
                "metadata": output.get("result_metadata", {}),
            }
            async with self._sessions() as db:
                task = await db.get(TaskRow, task_id)
                assert task is not None
                task.status = TaskStatus.SUCCEEDED.value
                task.result = result
                session = await db.scalar(
                    select(SessionRow).where(SessionRow.thread_id == task.thread_id)
                )
                if session is not None:
                    session.sdk_session_id = result["sdk_session_id"]
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
