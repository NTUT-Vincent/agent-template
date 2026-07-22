"""Claude Agent SDK 的 PostgreSQL SessionStore 實作。

為什麼這個檔案存在？

Claude Agent SDK 自己有 native session / resume 概念。若 transcript 只存在 Pod local disk：

    Pod A 執行 Agent
      -> session transcript 寫在 Pod A
      -> Pod A 被 Kubernetes 重建
      -> Pod B 收到下一輪 request
      -> 找不到原本 session transcript

所以這個範本把 SDK native session transcript 鏡像到 PostgreSQL。

注意：
這不是 LangGraph checkpoint，也不是 Long-term memory。

三者責任：

    LangGraph PostgresSaver
        -> workflow/thread state

    LangGraph PostgresStore
        -> cross-thread long-term memory

    PostgresClaudeSessionStore
        -> Claude Agent SDK native transcript / resume
"""
from __future__ import annotations

import json
import re

import asyncpg
from claude_agent_sdk import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
)

# Table name 會被插入 SQL identifier，不能讓任意字串進來。
# 值本身仍使用 asyncpg parameter binding，避免把資料字串直接拼 SQL。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresClaudeSessionStore(SessionStore):
    """Claude Agent SDK SessionStore protocol 的 PostgreSQL implementation。

    資料模型使用：

        project_key
          + session_id
          + subpath
          + seq

    其中 seq 保留 transcript entry 的順序。
    """

    def __init__(self, pool: asyncpg.Pool, table: str = "claude_session_store") -> None:
        if not _IDENT.fullmatch(table):
            raise ValueError("invalid session-store table name")
        self._pool = pool
        self._table = table

    async def setup(self) -> None:
        """建立 SDK session store table/index。

        Reference template 為了方便教學直接在 startup setup。
        正式 production 建議把 schema 交給 migration 管理。
        """
        await self._pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                project_key text NOT NULL,
                session_id text NOT NULL,
                subpath text NOT NULL DEFAULT '',
                seq bigserial,
                entry jsonb NOT NULL,
                mtime bigint NOT NULL,
                PRIMARY KEY (project_key, session_id, subpath, seq)
            );
            CREATE INDEX IF NOT EXISTS {self._table}_list_idx
              ON {self._table} (project_key, session_id)
              WHERE subpath = '';
            """
        )

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """把 SDK 新產生的 transcript entries 依序 append 到 PostgreSQL。

        ClaudeAgentOptions(session_store_flush="eager") 會更積極呼叫這類持久化操作，
        因此 Pod crash 或跨 process resume 時比較不依賴 local state。
        """
        if not entries:
            return

        await self._pool.execute(
            f"""
            INSERT INTO {self._table} (project_key, session_id, subpath, entry, mtime)
            SELECT $1, $2, $3, item,
                   (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
            FROM unnest($4::jsonb[]) WITH ORDINALITY AS t(item, ord)
            ORDER BY ord
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
            [json.dumps(entry) for entry in entries],
        )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """依 session key 讀回完整 transcript entries。

        resume=<sdk_session_id> 時，SDK 可以透過 SessionStore 取得之前的 session 資料。
        """
        rows = await self._pool.fetch(
            f"""
            SELECT entry FROM {self._table}
            WHERE project_key = $1 AND session_id = $2 AND subpath = $3
            ORDER BY seq
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
        )

        if not rows:
            return None

        values: list[SessionStoreEntry] = []
        for row in rows:
            entry = row["entry"]
            values.append(json.loads(entry) if isinstance(entry, (str, bytes)) else entry)
        return values

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        """列出某個 project_key 底下可 resume 的 SDK sessions。"""
        rows = await self._pool.fetch(
            f"""
            SELECT session_id, MAX(mtime) AS mtime
            FROM {self._table}
            WHERE project_key = $1 AND subpath = ''
            GROUP BY session_id
            """,
            project_key,
        )
        return [{"session_id": row["session_id"], "mtime": int(row["mtime"])} for row in rows]

    async def delete(self, key: SessionKey) -> None:
        """刪除 session 或特定 subpath。

        正式環境要把資料保留政策、稽核、法遵與刪除權限一起設計，
        不要只把這個 method 直接暴露給任意 client。
        """
        subpath = key.get("subpath")
        if subpath:
            await self._pool.execute(
                f"DELETE FROM {self._table} WHERE project_key=$1 AND session_id=$2 AND subpath=$3",
                key["project_key"],
                key["session_id"],
                subpath,
            )
            return

        await self._pool.execute(
            f"DELETE FROM {self._table} WHERE project_key=$1 AND session_id=$2",
            key["project_key"],
            key["session_id"],
        )

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """列出 SDK 在同一 session 下使用的 subpath keys。"""
        rows = await self._pool.fetch(
            f"""
            SELECT DISTINCT subpath FROM {self._table}
            WHERE project_key=$1 AND session_id=$2 AND subpath <> ''
            """,
            key["project_key"],
            key["session_id"],
        )
        return [row["subpath"] for row in rows]
