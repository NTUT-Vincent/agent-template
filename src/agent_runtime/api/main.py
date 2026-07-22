"""FastAPI 應用程式入口。

這個檔案刻意同時承載兩種 HTTP 介面：

1. A2A 標準介面
   - 給「其他 Agent」呼叫。
   - 協定、Task、Message、Artifact、streaming 等語意由官方 A2A SDK 管理。

2. Internal REST API
   - 給公司自己的 UI、Admin、後台服務使用。
   - 這些 API 不屬於 A2A 規範，所以明確放在 /internal/v1 底下。

非常重要：
A2A 和 Internal REST 雖然入口不同，但最後都會進到同一個 AgentRuntimeService，
再進 LangGraph，最後才執行真正的 Agent SDK。這樣才不會維護兩套 Agent 邏輯。
"""
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
    """FastAPI 啟動與關閉時的生命週期管理。

    建議把這段看成整個 API Pod 的「組裝點」。

    啟動順序：

        Uvicorn
          -> FastAPI lifespan
          -> RuntimeContainer.start()
          -> PostgreSQL / LangGraph persistence / Agent SDK 初始化
          -> 建立 A2A AgentExecutor
          -> 把官方 A2A routes 掛到同一個 FastAPI app
          -> 開始接受流量

    為什麼不是另外跑一個 A2A server？

    因為 A2A 在這個範本裡只是另一個「協定入口」。同一個 Pod、同一個 Uvicorn
    process 就可以同時提供 Internal REST 與 A2A，部署與觀測都比較簡單。
    """
    settings = get_settings()

    # RuntimeContainer 是 composition root：把 DB、LangGraph、Agent SDK 組裝起來。
    # API layer 不應該自己 new ClaudeAgentExecutor 或自己處理 checkpoint。
    runtime = RuntimeContainer(settings)
    await runtime.start()

    if runtime.engine is None or runtime.session_factory is None or runtime.service is None:
        raise RuntimeError("runtime failed to initialize")

    # 放進 app.state，讓 REST handler 能共用同一個 runtime/session factory。
    app.state.runtime = runtime
    app.state.session_factory = runtime.session_factory

    # ------------------------------------------------------------------
    # A2A 接線點
    # ------------------------------------------------------------------
    # RuntimeA2AExecutor 是「A2A 協定 -> application runtime」的 adapter。
    #
    # 注意傳進去的是 runtime.service.run_prompt，而不是 Claude SDK 本身。
    # 原因是：
    #   A2A -> Service -> LangGraph -> Agent SDK
    #
    # LangGraph 才能統一處理：
    #   - thread/checkpoint
    #   - long-term memory
    #   - human approval（未來可加）
    #   - retry / workflow routing
    #
    # 如果 A2A 直接 call Claude SDK，就會繞過這些 durable workflow 能力。
    if not getattr(app.state, "a2a_registered", False):
        executor = RuntimeA2AExecutor(runtime.service.run_prompt)
        await add_a2a_routes(
            app,
            executor=executor,
            public_url=settings.a2a_public_url,
            engine=runtime.engine,
        )
        app.state.a2a_registered = True

    # yield 之後 FastAPI 開始正常服務 request。
    yield

    # Pod graceful shutdown 時關閉 connection pool / LangGraph persistence。
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
    """Kubernetes liveness/readiness probe 使用。

    /health 不是 A2A API，也不需要硬塞進 A2A 規範。
    """
    return {"status": "ok"}


# ============================================================================
# Internal Application API
# ============================================================================
# 下面 API 是「公司自己的產品 API」，不是 Agent-to-Agent protocol。
#
# 適用情境：
#   - Generative UI 建立一個 conversation
#   - Admin dashboard 查 background job 狀態
#   - 公司服務丟一個非同步任務進 Celery
#
# 不適用情境：
#   - Agent A 要呼叫 Agent B
#
# Agent-to-Agent 請走 /.well-known/agent-card.json + /a2a。
# ============================================================================


@app.post(
    "/internal/v1/sessions",
    response_model=SessionResponse,
    status_code=201,
    tags=["internal"],
)
async def create_session(body: CreateSessionRequest, request: Request) -> SessionResponse:
    """建立產品層的 conversation / session mapping。

    這裡最重要的欄位是 thread_id。

    thread_id 在本範本中的角色：

        UI conversation
             -> thread_id
             -> LangGraph checkpoint namespace
             -> 同一條 durable workflow / short-term state

    sdk_session_id 則是 Claude Agent SDK 自己的 native session id，兩者不要混為一談。
    """
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
    """建立「內部 background job」，並送進 Celery。

    注意：這個 TaskRow 不是 A2A Task。

    app_tasks：
        公司內部 job lifecycle，例如 queued/running/succeeded/failed。

    a2a_tasks：
        A2A protocol 對外可見的 Task lifecycle，由官方 A2A SDK 管理。

    兩者可能最後都執行同一個 LangGraph，但它們位於不同 abstraction layer。
    """
    async with request.app.state.session_factory() as db:
        session = await db.scalar(
            select(SessionRow).where(SessionRow.thread_id == body.thread_id)
        )
        if session is None or session.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="session not found")

        # 先把 job 寫進 PostgreSQL，再送 queue。
        # PostgreSQL 才是 application task 的 source of truth。
        row = TaskRow(
            user_id=body.user_id,
            thread_id=body.thread_id,
            prompt=body.prompt,
            remember=body.remember,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        # Redis/Celery 只負責 delivery / backpressure。
        # 不要把 Redis 裡有沒有 job 當成 workflow 是否存在的唯一依據。
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
    """查詢 internal background job 狀態。

    A2A client 不應該呼叫這個 endpoint 查 A2A Task；A2A Task 查詢應透過
    A2A protocol 對應的方法，由官方 SDK 的 request handler 處理。
    """
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
