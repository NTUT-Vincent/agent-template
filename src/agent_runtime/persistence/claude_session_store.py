"""Postgres-backed Claude Agent SDK SessionStore.

Adapted from Anthropic's reference contract: one JSONB row per transcript entry,
with a composite logical key of project_key/session_id/subpath.
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

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresClaudeSessionStore(SessionStore):
    def __init__(self, pool: asyncpg.Pool, table: str = "claude_session_store") -> None:
        if not _IDENT.fullmatch(table):
            raise ValueError("invalid session-store table name")
        self._pool = pool
        self._table = table

    async def setup(self) -> None:
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
        rows = await self._pool.fetch(
            f"""
            SELECT DISTINCT subpath FROM {self._table}
            WHERE project_key=$1 AND session_id=$2 AND subpath <> ''
            """,
            key["project_key"],
            key["session_id"],
        )
        return [row["subpath"] for row in rows]
