from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select

from agent_runtime.a2a.executor import RuntimeA2AExecutor
from agent_runtime.a2a.server import add_a2a_routes
from agent_runtime.api.schemas import (
    CreateSessionRequest,
    CreateTaskRequest,
    SessionResponse,
    TaskResponse,
)
from agent_runtime.config import get_settings
from agent_runtime.db import SessionRow, TaskRow
from agent_runtime.runtime import RuntimeContainer
from agent_runtime.tasks.celery_app import celery_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime = RuntimeContainer(settings)
    await runtime.start()

    if runtime.engine is None or runtime.session_factory is None or runtime.service is None:
        raise RuntimeError("runtime failed to initialize")

    app.state.runtime = runtime
    app.state.session_factory = runtime.session_factory

    # A2A is a public interoperability surface. Register it on the same FastAPI
    # application so Agent Card + JSON-RPC are actually exposed by the container.
    if not getattr(app.state, "a2a_registered", False):
        executor = RuntimeA2AExecutor(runtime.service.run_prompt)
        await add_a2a_routes(
            app,
            executor=executor,
            public_url=settings.a2a_public_url,
            engine=runtime.engine,
        )
        app.state.a2a_registered = True

    yield
    await runtime.stop()


app = FastAPI(
    title="Agent Runtime Platform",
    version="0.1.0",
    description=(
        "Reference template: official A2A endpoints for agent-to-agent interoperability "
        "and /internal/v1 REST endpoints for application-specific operations."
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Internal application API
# These endpoints are intentionally NOT A2A. They are for UI/admin/application
# integration and use Celery as the delivery queue.
# ---------------------------------------------------------------------------


@app.post(
    "/internal/v1/sessions",
    response_model=SessionResponse,
    status_code=201,
    tags=["internal"],
)
async def create_session(body: CreateSessionRequest, request: Request) -> SessionResponse:
    row = SessionRow(user_id=body.user_id, thread_id=str(uuid4()))
    async with request.app.state.session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return SessionResponse(
        id=row.id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        sdk_session_id=row.sdk_session_id,
    )


@app.post(
    "/internal/v1/tasks",
    response_model=TaskResponse,
    status_code=202,
    tags=["internal"],
)
async def create_task(body: CreateTaskRequest, request: Request) -> TaskResponse:
    async with request.app.state.session_factory() as db:
        session = await db.scalar(
            select(SessionRow).where(SessionRow.thread_id == body.thread_id)
        )
        if session is None or session.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="session not found")

        row = TaskRow(
            user_id=body.user_id,
            thread_id=body.thread_id,
            prompt=body.prompt,
            remember=body.remember,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        job = celery_app.send_task("agent_runtime.run_task", args=[str(row.id)])
        row.celery_job_id = job.id
        await db.commit()

    return TaskResponse(
        id=row.id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        status=row.status,
        result=row.result,
        error=row.error,
    )


@app.get(
    "/internal/v1/tasks/{task_id}",
    response_model=TaskResponse,
    tags=["internal"],
)
async def get_task(task_id: str, request: Request) -> TaskResponse:
    try:
        task_uuid = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid task id") from exc

    async with request.app.state.session_factory() as db:
        row = await db.get(TaskRow, task_uuid)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskResponse(
            id=row.id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            status=row.status,
            result=row.result,
            error=row.error,
        )
