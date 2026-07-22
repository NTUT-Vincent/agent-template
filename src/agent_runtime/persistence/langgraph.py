from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore


@dataclass(slots=True)
class LangGraphPersistence:
    database_url: str
    checkpointer: AsyncPostgresSaver | None = None
    store: AsyncPostgresStore | None = None
    _stack: AsyncExitStack | None = None

    async def start(self, *, run_setup: bool = True) -> None:
        stack = AsyncExitStack()
        self.checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(self.database_url)
        )
        self.store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(self.database_url)
        )
        self._stack = stack
        if run_setup:
            await self.checkpointer.setup()
            await self.store.setup()

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self.checkpointer = None
        self.store = None

    def require(self) -> tuple[AsyncPostgresSaver, AsyncPostgresStore]:
        if self.checkpointer is None or self.store is None:
            raise RuntimeError("LangGraph persistence has not been started")
        return self.checkpointer, self.store
