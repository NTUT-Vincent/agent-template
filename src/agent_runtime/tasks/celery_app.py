"""Celery queue 設定。

Celery/Redis 在這個架構裡只負責：

- internal background job delivery
- backpressure
- worker concurrency
- worker crash 後的 redelivery

它不負責：

- LangGraph checkpoint
- A2A Task persistence
- Agent SDK session
- long-term memory

換句話說：Queue 是「誰來執行」，不是「Agent 現在執行到哪裡」。
"""
from celery import Celery

from agent_runtime.config import get_settings

settings = get_settings()

# broker：producer 把 job 丟進 Redis，worker 從 Redis 取走。
# backend：這個範本也使用 Redis 作為 Celery result backend。
#
# 但 application task 的正式狀態仍會寫入 PostgreSQL app_tasks，
# 不應只依賴 Redis result backend 當 source of truth。
celery_app = Celery(
    "agent_runtime",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["agent_runtime.tasks.jobs"],
)

celery_app.conf.update(
    # worker 完成任務後才 ack。
    # worker 執行中死亡時，broker 才有機會把 job 重新交付。
    task_acks_late=True,

    # worker process 異常消失時拒絕/重新交付尚未完成的 task。
    task_reject_on_worker_lost=True,

    # 每個 worker process 一次只預抓少量 job，避免某一個 process 把大量長任務搶走。
    worker_prefetch_multiplier=1,

    # 讓 Celery 能標示 started；application 仍會另外更新 PostgreSQL TaskRow.status。
    task_track_started=True,
)
