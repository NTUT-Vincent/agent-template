from __future__ import annotations

import itertools
import os

import asyncpg
import pytest
from claude_agent_sdk.testing import run_session_store_conformance

from agent_runtime.persistence.claude_session_store import PostgresClaudeSessionStore


@pytest.mark.asyncio
async def test_postgres_claude_session_store_conformance() -> None:
    """跑官方 Claude Agent SDK SessionStore conformance suite。

    本測試需要 PostgreSQL。CI 會提供 TEST_DATABASE_URL；本機沒有 PostgreSQL 時自動 skip。

    為什麼不用自己猜 SessionStore 行為？
    因為官方 conformance suite 會直接檢查 append/load/order/subpath/list/delete/summary 等
    contract，能避免自製 adapter 看起來能跑、實際 resume 時才發現不相容。
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    counter = itertools.count()

    async def make_store() -> PostgresClaudeSessionStore:
        # Conformance suite 每個 contract 都要拿到乾淨 store；用不同 table 達到 isolation。
        table = f"claude_session_store_test_{next(counter)}"
        store = PostgresClaudeSessionStore(pool, table=table)
        await store.setup()
        return store

    try:
        await run_session_store_conformance(make_store)
    finally:
        await pool.close()
