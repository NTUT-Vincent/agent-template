"""LangGraph PostgreSQL persistence wrapper。

這個檔案同時初始化兩種不同用途的 persistence：

1. AsyncPostgresSaver / checkpointer
   - thread-scoped workflow state
   - checkpoint / resume
   - human-in-the-loop / durable execution 的基礎

2. AsyncPostgresStore / store
   - cross-thread long-term memory
   - user/project knowledge

最常見的錯誤就是把這兩者都叫做「memory」，最後不知道資料該放哪裡。
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore


@dataclass(slots=True)
class LangGraphPersistence:
    """管理 LangGraph PostgresSaver / PostgresStore 的生命週期。"""

    database_url: str

    # checkpointer：回答「這條 thread 執行到哪裡？」
    checkpointer: AsyncPostgresSaver | None = None

    # store：回答「跨 thread 要長期保留什麼？」
    store: AsyncPostgresStore | None = None

    # from_conn_string() 回傳 async context manager；用 AsyncExitStack 集中關閉。
    _stack: AsyncExitStack | None = None

    async def start(self, *, run_setup: bool = True) -> None:
        """建立 PostgresSaver 與 PostgresStore。

        教學心智模型：

            LangGraph thread_id = conversation/workflow identity
                    |
                    v
            AsyncPostgresSaver
                    |
               checkpoints

        與：

            (namespace, key)
                    |
                    v
            AsyncPostgresStore
                    |
              long-term memory

        是兩條不同資料路徑。
        """
        stack = AsyncExitStack()

        self.checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(self.database_url)
        )
        self.store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(self.database_url)
        )
        self._stack = stack

        if run_setup:
            # setup() 會建立套件需要的 persistence schema/table。
            # Reference template 為了方便本機啟動直接做 setup；正式 production
            # 建議改成 migration job，而不是每顆 Pod 啟動都嘗試做 schema setup。
            await self.checkpointer.setup()
            await self.store.setup()

    async def stop(self) -> None:
        """關閉 LangGraph persistence connections。"""
        if self._stack is not None:
            await self._stack.aclose()

        self._stack = None
        self.checkpointer = None
        self.store = None

    def require(self) -> tuple[AsyncPostgresSaver, AsyncPostgresStore]:
        """取得已初始化的 persistence dependencies。

        RuntimeContainer.start() 會先 start() 再 require()；
        這個 guard 可以避免 graph 不小心拿到 None persistence。
        """
        if self.checkpointer is None or self.store is None:
            raise RuntimeError("LangGraph persistence has not been started")
        return self.checkpointer, self.store
