from __future__ import annotations

import asyncio
from uuid import UUID

from agent_runtime.config import get_settings
from agent_runtime.runtime import RuntimeContainer
from agent_runtime.tasks.celery_app import celery_app


async def _run(task_id: str) -> dict:
    runtime = RuntimeContainer(get_settings())
    await runtime.start()
    try:
        if runtime.service is None:
            raise RuntimeError("runtime service not initialized")
        return await runtime.service.run_task(UUID(task_id))
    finally:
        await runtime.stop()


@celery_app.task(name="agent_runtime.run_task")
def run_task(task_id: str) -> dict:
    return asyncio.run(_run(task_id))
