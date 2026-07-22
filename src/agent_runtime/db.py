"""Application-level PostgreSQL models。

這個檔案只放「我們自己的 application tables」：

    app_sessions
    app_tasks

請注意它們不是：

- LangGraph checkpoint tables
- LangGraph long-term store tables
- A2A SDK 的 a2a_tasks
- Claude Agent SDK 的 claude_session_store

這個區分很重要，因為這四種 persistence 分別解決不同問題。
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent_runtime.config import Settings


class Base(AsyncAttrs, DeclarativeBase):
    """SQLAlchemy declarative base。"""


class SessionRow(Base):
    """Application conversation 與 Agent SDK session 的 mapping。

    一筆資料可以理解成：

        user_id
          + thread_id          -> LangGraph durable thread
          + sdk_session_id     -> Claude Agent SDK native session

    這張表本身不存整段聊天內容，也不存 LangGraph checkpoint。
    """

    __tablename__ = "app_sessions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    user_id: Mapped[str] = mapped_column(String(200), index=True)

    # thread_id 是 LangGraph workflow identity。
    # unique=True 避免同一 thread 被建立兩份 application mapping。
    thread_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    # 第一次 Agent 執行前通常是 None；Claude SDK 回傳 session_id 後寫回。
    sdk_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TaskRow(Base):
    """Internal REST/Celery background job lifecycle。

    這不是 A2A Task。

    使用情境：

        POST /internal/v1/tasks
          -> app_tasks(status=queued)
          -> Celery
          -> worker
          -> status=running
          -> status=succeeded / failed

    A2A Task 則由官方 A2A DatabaseTaskStore 管理在 a2a_tasks。
    """

    __tablename__ = "app_tasks"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    user_id: Mapped[str] = mapped_column(String(200), index=True)
    thread_id: Mapped[str] = mapped_column(String(200), index=True)
    prompt: Mapped[str] = mapped_column(Text)

    # 範本用 remember 明確示範「這輪是否要寫 long-term memory」。
    remember: Mapped[bool] = mapped_column(Boolean, default=False)

    # queued/running/succeeded/failed 屬於 application job 狀態。
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")

    # protocol-neutral result；目前是 text/sdk_session_id/metadata。
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 方便追查這筆 DB job 對應哪個 Celery delivery id。
    celery_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def sqlalchemy_url(database_url: str) -> str:
    """把一般 PostgreSQL URL 轉成 SQLAlchemy asyncpg driver URL。

    其他元件可能使用：

        postgresql://...

    SQLAlchemy async engine 則需要：

        postgresql+asyncpg://...
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def create_engine(settings: Settings) -> AsyncEngine:
    """建立 application SQLAlchemy async engine。"""
    return create_async_engine(
        sqlalchemy_url(settings.database_url),
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """建立 SQLAlchemy async session factory。"""
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_app_schema(engine: AsyncEngine) -> None:
    """建立 reference-template application tables。

    教學範本為了 clone 後可直接啟動使用 create_all()。

    正式 production 請改用 Alembic/Kubernetes migration Job，避免多 Pod startup
    同時承擔 schema migration 責任。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
