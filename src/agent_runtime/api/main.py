from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select

from agent_runtime.api.schemas import (
    CreateSessionRequest,
    CreateTaskRequest,
    SessionResponse,
    TaskResponse,
)
from agent_runtime.config import get_settings
from agent_runtime.db import SessionRow, TaskRow, create_app_schema, create_engine, create_session_factory
from agent_runtime.tasks.celery_app import celery_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = create_engine(settings)
    await create_app_schema(engine)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    yield
    await engine.dispose()


app = FastAPI(title="Agent Runtime Platform", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/sessions", response_model=SessionResponse, status_code=201)
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


@app.post("/v1/tasks", response_model=TaskResponse, status_code=202)
async def create_task(body: CreateTaskRequest, request: Request) -> TaskResponse:
    async with request.app.state.session_factory() as db:
        session = await db.scalar(select(SessionRow).where(SessionRow.thread_id == body.thread_id))
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


@app.get("/v1/tasks/{task_id}", response_model=TaskResponse)
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
