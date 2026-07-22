"""Official A2A AgentExecutor -> shared LangGraph runtime.

A2A owns protocol lifecycle (Task, status, artifacts, streaming). The application
runtime owns LangGraph state, memory and Agent SDK execution.
"""
from __future__ import annotations

from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils import new_task_from_user_message, new_text_message, new_text_part


class RuntimeA2AExecutor(AgentExecutor):
    def __init__(self, run_prompt) -> None:
        self._run_prompt = run_prompt

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ValueError("A2A message is required")

        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Agent task accepted"),
        )

        # A2A contextId is a good fit for the durable LangGraph thread identity:
        # multiple A2A tasks/messages in one conversation can share the same context.
        thread_id = task.context_id or context.context_id or str(uuid4())
        user_id = f"a2a:{context.tenant}"
        prompt = context.get_user_input()

        result = await self._run_prompt(
            user_id=user_id,
            thread_id=thread_id,
            prompt=prompt,
            remember=False,
        )

        await updater.add_artifact(
            parts=[new_text_part(text=result["text"], media_type="text/plain")]
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Agent task completed"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The official A2A runtime cancels the active execute() coroutine before
        # calling this hook. Downstream tools should still be designed for
        # cooperative cancellation/idempotency when they have side effects.
        task = context.current_task
        if task is None:
            return
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("Cancellation requested"),
        )
