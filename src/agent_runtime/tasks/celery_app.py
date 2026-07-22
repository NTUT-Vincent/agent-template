from celery import Celery

from agent_runtime.config import get_settings

settings = get_settings()
celery_app = Celery(
    "agent_runtime",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["agent_runtime.tasks.jobs"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
)
