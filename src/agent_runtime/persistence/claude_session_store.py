"""Claude Agent SDK 的 PostgreSQL SessionStore 實作。

這個 adapter 對齊官方 SessionStore contract，目標是讓任意 Kubernetes Pod 都能 resume
同一個 Claude native session。

資料責任：

    LangGraph PostgresSaver
        -> workflow/thread checkpoint

    LangGraph PostgresStore
        -> application long-term memory

    PostgresClaudeSessionStore
        -> Claude Agent SDK native transcript / subagent transcript / session summary

重要限制：SessionStore 是「mirror」，不是 local JSONL 的 transaction replacement。SDK 會先寫
local transcript，再呼叫 append()。因此：

- append 必須可重試、可去重。
- 官方明確建議用 `entry.uuid` deduplicate。
- mirror_error 必須由 Agent adapter 監控。
- 有副作用的 tool 仍需 idempotency；SessionStore 不提供 exactly-once。
"""
from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from claude_agent_sdk import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
    fold_session_summary,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresClaudeSessionStore(SessionStore):
    """Claude Agent SDK SessionStore protocol 的 PostgreSQL implementation。

    Transcript table：一個 SDK entry 一列，BIGSERIAL 保留 append order。

        project_key
        session_id
        subpath
        seq
        entry_uuid
        entry jsonb
        mtime

    `entry_uuid` 用來處理 SDK mirror retry：若第一次 INSERT 已成功但 caller 因網路問題
    重送同一 batch，`ON CONFLICT DO NOTHING` 不會把相同 transcript frame 寫兩次。

    Summary table：保存官方 `fold_session_summary()` 產生的 opaque sidecar，讓
    `list_sessions_from_store()` 不必逐 session load 完整 transcript。
    """

    def __init__(self, pool: asyncpg.Pool, table: str = "claude_session_store") -> None:
        if not _IDENT.fullmatch(table):
            raise ValueError("invalid session-store table name")
        self._pool = pool
        self._table = table
        self._summary_table = f"{table}_summaries"

    async def setup(self) -> None:
        """建立/補齊 SessionStore schema。

        Template 為了可直接跑，在 startup 做 idempotent setup；production 應改由 migration
        管理同樣 schema，避免多 Pod 同時做 DDL。
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    project_key text NOT NULL,
                    session_id text NOT NULL,
                    subpath text NOT NULL DEFAULT '',
                    seq bigserial,
                    entry_uuid text,
                    entry jsonb NOT NULL,
                    mtime bigint NOT NULL,
                    PRIMARY KEY (project_key, session_id, subpath, seq)
                );

                -- 舊版 template 沒有 entry_uuid；保留 ALTER 讓既有 DB 可平滑升級。
                ALTER TABLE {self._table}
                  ADD COLUMN IF NOT EXISTS entry_uuid text;

                CREATE UNIQUE INDEX IF NOT EXISTS {self._table}_entry_uuid_uidx
                  ON {self._table} (project_key, session_id, subpath, entry_uuid);

                CREATE INDEX IF NOT EXISTS {self._table}_list_idx
                  ON {self._table} (project_key, session_id)
                  WHERE subpath = '';

                CREATE TABLE IF NOT EXISTS {self._summary_table} (
                    project_key text NOT NULL,
                    session_id text NOT NULL,
                    mtime bigint NOT NULL,
                    data jsonb NOT NULL,
                    PRIMARY KEY (project_key, session_id)
                );
                """
            )

    @staticmethod
    def _entry_uuid(entry: SessionStoreEntry) -> str | None:
        """取出官方建議的 dedupe key；沒有 uuid 的 opaque entry 仍照原樣保存。"""
        value = entry.get("uuid")
        return value if isinstance(value, str) and value else None

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """把 SDK mirror batch 依序 append 到 PostgreSQL。

        官方行為允許 batch retry，所以這裡必須 idempotent：

            append(batch)
              -> DB success
              -> caller 沒收到成功
              -> SDK retry 同 batch
              -> entry.uuid unique index 去重

        同一 session/subpath 使用 PostgreSQL advisory transaction lock，避免 concurrent append
        在「檢查 UUID / 寫 transcript / fold summary」之間互相踩到。
        """
        if not entries:
            return

        project_key = key["project_key"]
        session_id = key["session_id"]
        subpath = key.get("subpath") or ""

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # serialize 同一 transcript 的 append；不同 session 仍可平行。
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))",
                    project_key,
                    f"{session_id}:{subpath}",
                )

                uuids = [u for entry in entries if (u := self._entry_uuid(entry)) is not None]
                existing_uuids: set[str] = set()
                if uuids:
                    rows = await conn.fetch(
                        f"""
                        SELECT entry_uuid FROM {self._table}
                        WHERE project_key=$1 AND session_id=$2 AND subpath=$3
                          AND entry_uuid = ANY($4::text[])
                        """,
                        project_key,
                        session_id,
                        subpath,
                        uuids,
                    )
                    existing_uuids = {row["entry_uuid"] for row in rows}

                # 只把尚未存在的 UUID entry 拿去 INSERT / fold summary。
                # 沒有 uuid 的 entry 無法可靠 dedupe，因此每次都保存。
                new_entries: list[SessionStoreEntry] = []
                seen_in_batch: set[str] = set()
                for entry in entries:
                    entry_uuid = self._entry_uuid(entry)
                    if entry_uuid is not None:
                        if entry_uuid in existing_uuids or entry_uuid in seen_in_batch:
                            continue
                        seen_in_batch.add(entry_uuid)
                    new_entries.append(entry)

                if new_entries:
                    await conn.executemany(
                        f"""
                        INSERT INTO {self._table}
                            (project_key, session_id, subpath, entry_uuid, entry, mtime)
                        VALUES (
                            $1, $2, $3, $4, $5::jsonb,
                            (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
                        )
                        ON CONFLICT (project_key, session_id, subpath, entry_uuid)
                        DO NOTHING
                        """,
                        [
                            (
                                project_key,
                                session_id,
                                subpath,
                                self._entry_uuid(entry),
                                json.dumps(entry),
                            )
                            for entry in new_entries
                        ],
                    )

                # Summary 只屬於 main transcript；subagent transcript 不可混進 main summary。
                if subpath == "" and new_entries:
                    summary_row = await conn.fetchrow(
                        f"""
                        SELECT mtime, data FROM {self._summary_table}
                        WHERE project_key=$1 AND session_id=$2
                        FOR UPDATE
                        """,
                        project_key,
                        session_id,
                    )
                    previous: SessionSummaryEntry | None = None
                    if summary_row is not None:
                        data = summary_row["data"]
                        previous = {
                            "session_id": session_id,
                            "mtime": int(summary_row["mtime"]),
                            "data": json.loads(data) if isinstance(data, (str, bytes)) else data,
                        }

                    summary = fold_session_summary(previous, key, new_entries)

                    # mtime 要跟 list_sessions 使用同一個 clock/source；直接取 transcript row
                    # 的 DB timestamp，而不是使用 entry 內 timestamp。
                    mtime = await conn.fetchval(
                        f"""
                        SELECT MAX(mtime) FROM {self._table}
                        WHERE project_key=$1 AND session_id=$2 AND subpath=''
                        """,
                        project_key,
                        session_id,
                    )
                    summary["mtime"] = int(mtime or 0)

                    await conn.execute(
                        f"""
                        INSERT INTO {self._summary_table}
                            (project_key, session_id, mtime, data)
                        VALUES ($1, $2, $3, $4::jsonb)
                        ON CONFLICT (project_key, session_id)
                        DO UPDATE SET mtime=EXCLUDED.mtime, data=EXCLUDED.data
                        """,
                        project_key,
                        session_id,
                        summary["mtime"],
                        json.dumps(summary["data"]),
                    )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """依 key 讀回完整 raw transcript，順序必須與 append 相同。"""
        rows = await self._pool.fetch(
            f"""
            SELECT entry FROM {self._table}
            WHERE project_key=$1 AND session_id=$2 AND subpath=$3
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
        """列出 main transcript sessions；mtime 與 summary 使用相同 DB clock。"""
        rows = await self._pool.fetch(
            f"""
            SELECT session_id, MAX(mtime) AS mtime
            FROM {self._table}
            WHERE project_key=$1 AND subpath=''
            GROUP BY session_id
            """,
            project_key,
        )
        return [{"session_id": row["session_id"], "mtime": int(row["mtime"])} for row in rows]

    async def list_session_summaries(self, project_key: str) -> list[SessionSummaryEntry]:
        """回傳 SDK-owned opaque summary sidecars，避免 listing 時 N 次 load transcript。"""
        rows = await self._pool.fetch(
            f"""
            SELECT session_id, mtime, data
            FROM {self._summary_table}
            WHERE project_key=$1
            ORDER BY mtime DESC
            """,
            project_key,
        )
        summaries: list[SessionSummaryEntry] = []
        for row in rows:
            data: Any = row["data"]
            summaries.append(
                {
                    "session_id": row["session_id"],
                    "mtime": int(row["mtime"]),
                    "data": json.loads(data) if isinstance(data, (str, bytes)) else data,
                }
            )
        return summaries

    async def delete(self, key: SessionKey) -> None:
        """刪除 session/subpath；刪 main key 時同時 cascade subkeys 與 summary。"""
        subpath = key.get("subpath")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if subpath:
                    await conn.execute(
                        f"""
                        DELETE FROM {self._table}
                        WHERE project_key=$1 AND session_id=$2 AND subpath=$3
                        """,
                        key["project_key"],
                        key["session_id"],
                        subpath,
                    )
                    return

                await conn.execute(
                    f"DELETE FROM {self._table} WHERE project_key=$1 AND session_id=$2",
                    key["project_key"],
                    key["session_id"],
                )
                await conn.execute(
                    f"DELETE FROM {self._summary_table} WHERE project_key=$1 AND session_id=$2",
                    key["project_key"],
                    key["session_id"],
                )

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """列出 subagent/sidecar subpaths；resume 時 SDK 用它還原 subagent transcript。"""
        rows = await self._pool.fetch(
            f"""
            SELECT DISTINCT subpath FROM {self._table}
            WHERE project_key=$1 AND session_id=$2 AND subpath <> ''
            ORDER BY subpath
            """,
            key["project_key"],
            key["session_id"],
        )
        return [row["subpath"] for row in rows]
