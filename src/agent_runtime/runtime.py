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
    settings: Settings
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker | None = None
    asyncpg_pool: asyncpg.Pool | None = None
    langgraph: LangGraphPersistence | None = None
    service: AgentRuntimeService | None = None

    async def start(self) -> None:
        self.engine = create_engine(self.settings)
        await create_app_schema(self.engine)
        self.session_factory = create_session_factory(self.engine)

        self.asyncpg_pool = await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=10)
        claude_store = PostgresClaudeSessionStore(self.asyncpg_pool)
        await claude_store.setup()

        self.langgraph = LangGraphPersistence(self.settings.database_url)
        await self.langgraph.start(run_setup=True)
        checkpointer, store = self.langgraph.require()

        agent = ClaudeAgentExecutor(
            model=self.settings.claude_model,
            max_turns=self.settings.claude_max_turns,
            project_key=self.settings.claude_project_key,
            session_store=claude_store,
        )
        graph = build_graph(agent=agent, checkpointer=checkpointer, store=store)
        self.service = AgentRuntimeService(graph, self.session_factory)

    async def stop(self) -> None:
        if self.langgraph is not None:
            await self.langgraph.stop()
        if self.asyncpg_pool is not None:
            await self.asyncpg_pool.close()
        if self.engine is not None:
            await self.engine.dispose()
