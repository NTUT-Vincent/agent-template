"""RuntimeContainer：整個 Agent Runtime 的 composition root。

這個檔案回答一個很重要的問題：

    「PostgreSQL、LangGraph、Agent SDK、SessionStore 到底在哪裡被組裝在一起？」

答案就是 RuntimeContainer.start()。

建議把它想成 dependency injection container 的簡化版：

    Settings
      -> DB Engine / SessionFactory
      -> Claude SessionStore
      -> LangGraph Saver + Store
      -> ClaudeAgentExecutor
      -> Compiled LangGraph
      -> AgentRuntimeService

API layer 與 Celery worker 都建立 RuntimeContainer，因此兩種入口會使用同一套組裝方式。
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent_runtime.agent_sdk.claude import ClaudeAgentExecutor
from agent_runtime.config import Settings
from agent_runtime.db import create_app_schema, create_engine, create_session_factory
from agent_runtime.graph.builder import build_graph
from agent_runtime.persistence.claude_session_store import PostgresClaudeSessionStore
from agent_runtime.persistence.langgraph import LangGraphPersistence
from agent_runtime.service import AgentRuntimeService


@dataclass(slots=True)
class RuntimeContainer:
    """集中管理 runtime dependency 的生命週期。

    為什麼需要這個 class？

    如果每個 FastAPI route / Celery task 都自己：
      - 建 DB pool
      - 建 LangGraph saver
      - new ClaudeAgentExecutor
      - compile graph

    很快就會出現重複邏輯、connection leak、設定不一致。

    所以用一個 container 統一 start/stop。
    """

    settings: Settings

    # Application DB：app_sessions / app_tasks / A2A DatabaseTaskStore 共用。
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker | None = None

    # Claude SessionStore 目前用 asyncpg 實作，所以另外維持一個 asyncpg pool。
    asyncpg_pool: asyncpg.Pool | None = None

    # LangGraph persistence wrapper：內含 PostgresSaver + PostgresStore。
    langgraph: LangGraphPersistence | None = None

    # Protocol-neutral service：A2A 與 Internal REST/Celery 最後都會呼叫它。
    service: AgentRuntimeService | None = None

    async def start(self) -> None:
        """初始化完整 Agent runtime。

        啟動順序刻意有依賴關係：

        1. DB engine / application schema
        2. Claude Agent SDK SessionStore
        3. LangGraph checkpoint + long-term store
        4. Agent SDK implementation
        5. compile graph
        6. service layer
        """

        # --------------------------------------------------------------
        # 1. Application database
        # --------------------------------------------------------------
        self.engine = create_engine(self.settings)
        await create_app_schema(self.engine)
        self.session_factory = create_session_factory(self.engine)

        # --------------------------------------------------------------
        # 2. Agent SDK native session persistence
        # --------------------------------------------------------------
        # Claude Agent SDK 自己有 session transcript/resume 概念。
        # 不能只放 Pod local filesystem，否則 Kubernetes reschedule 後會失去 native session。
        self.asyncpg_pool = await asyncpg.create_pool(
            self.settings.database_url,
            min_size=1,
            max_size=10,
        )
        claude_store = PostgresClaudeSessionStore(self.asyncpg_pool)
        await claude_store.setup()

        # --------------------------------------------------------------
        # 3. LangGraph persistence
        # --------------------------------------------------------------
        # 這裡同時取得：
        #   checkpointer = workflow/thread state
        #   store        = cross-thread long-term memory
        self.langgraph = LangGraphPersistence(self.settings.database_url)
        await self.langgraph.start(run_setup=True)
        checkpointer, store = self.langgraph.require()

        # --------------------------------------------------------------
        # 4. 真正的 Agent implementation
        # --------------------------------------------------------------
        # 目前 template 使用 Claude Agent SDK。
        # 未來要替換 SDK，最主要就是在這裡換 implementation，並維持 AgentExecutor contract。
        agent = ClaudeAgentExecutor(
            model=self.settings.claude_model,
            max_turns=self.settings.claude_max_turns,
            project_key=self.settings.claude_project_key,
            session_store=claude_store,
        )

        # --------------------------------------------------------------
        # 5. 把 Agent 放進 LangGraph
        # --------------------------------------------------------------
        # build_graph() 內的 agent_sdk node 最後會呼叫 agent.run()。
        graph = build_graph(
            agent=agent,
            checkpointer=checkpointer,
            store=store,
        )

        # --------------------------------------------------------------
        # 6. 建立 protocol-neutral service
        # --------------------------------------------------------------
        # FastAPI Internal REST、A2A Executor、Celery worker 都共用這個 contract。
        self.service = AgentRuntimeService(graph, self.session_factory)

    async def stop(self) -> None:
        """依序釋放 runtime resources。

        Kubernetes 收到 SIGTERM 時應讓 Uvicorn/Celery 有 graceful shutdown 時間，
        才能走到這裡正常關閉 connection pool。
        """
        if self.langgraph is not None:
            await self.langgraph.stop()

        if self.asyncpg_pool is not None:
            await self.asyncpg_pool.close()

        if self.engine is not None:
            await self.engine.dispose()
