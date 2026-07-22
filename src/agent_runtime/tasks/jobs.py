"""Celery worker task entrypoint。

Internal REST 建立 app_tasks 後，只把 task_id 丟進 queue：

    POST /internal/v1/tasks
      -> PostgreSQL app_tasks
      -> Celery send_task(task_id)
      -> Redis
      -> worker 收到 task_id
      -> 這個檔案的 run_task()
      -> RuntimeContainer
      -> AgentRuntimeService.run_task()
      -> LangGraph
      -> Agent SDK

Queue message 刻意只放 task_id，而不是把完整 prompt/state 全塞進 Redis。
原因是 PostgreSQL 才是 application job 的 durable source of truth。
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from agent_runtime.config import get_settings
from agent_runtime.runtime import RuntimeContainer
from agent_runtime.tasks.celery_app import celery_app


async def _run(task_id: str) -> dict:
    """在 worker process 裡建立完整 Agent runtime，執行指定 internal task。"""

    # Worker 和 API 使用相同 RuntimeContainer，所以組裝出的：
    #   - LangGraph persistence
    #   - Agent SDK
    #   - SessionStore
    #   - Service layer
    # 都遵循同一套設定與生命週期。
    runtime = RuntimeContainer(get_settings())
    await runtime.start()

    try:
        if runtime.service is None:
            raise RuntimeError("runtime service not initialized")

        # run_task() 會：
        #   1. 從 PostgreSQL 讀 TaskRow
        #   2. 標記 running
        #   3. 呼叫共用 run_prompt()
        #   4. 更新 succeeded / failed
        return await runtime.service.run_task(UUID(task_id))
    finally:
        # 不論成功或失敗，都要關閉 DB / persistence connection。
        await runtime.stop()


@celery_app.task(name="agent_runtime.run_task")
def run_task(task_id: str) -> dict:
    """Celery 的同步 task wrapper。

    Celery worker function 是 sync entrypoint；真正 runtime 是 async，
    所以範本用 asyncio.run() 建立 event loop 執行 async workflow。

    大量高併發正式環境可再依 worker model 評估是否改用原生 async queue/worker。
    """
    return asyncio.run(_run(task_id))
