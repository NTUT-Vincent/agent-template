import pytest

from agent_runtime.persistence.claude_session_store import PostgresClaudeSessionStore


def test_session_store_rejects_unsafe_table_name():
    with pytest.raises(ValueError):
        PostgresClaudeSessionStore(pool=object(), table="x;drop table y")  # type: ignore[arg-type]
