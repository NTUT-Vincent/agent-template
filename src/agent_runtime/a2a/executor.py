"""A2A bridge: A2A protocol task -> existing runtime task API.

The official A2A SDK owns protocol lifecycle; this executor keeps business logic in
AgentRuntimeService instead of creating a second agent loop.
"""
from __future__ import annotations

from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils import get_message_text, new_task_from_user_message, new_text_message, new_text_part


class RuntimeA2AExecutor(AgentExecutor):
    def __init__(self, submit_and_wait) -> None:
        self._submit_and_wait = submit_and_wait

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Agent task accepted"),
        )

        prompt = get_message_text(context.message) or ""
        user_id = "a2a-user"
        thread_id = task.context_id or str(uuid4())
        result = await self._submit_and_wait(user_id=user_id, thread_id=thread_id, prompt=prompt)

        await updater.add_artifact(parts=[new_text_part(text=result["text"], media_type="text/plain")])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Agent task completed"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Cancellation should be wired to Celery revoke + cooperative cancellation in production.
        task = context.current_task
        if task is None:
            return
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("Cancellation requested"),
        )
