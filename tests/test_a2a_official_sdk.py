from __future__ import annotations

import inspect

from agent_runtime.a2a import server


def test_a2a_uses_official_database_task_store() -> None:
    source = inspect.getsource(server)
    assert "DatabaseTaskStore" in source
    assert "InMemoryTaskStore" not in source
    assert 'table_name="a2a_tasks"' in source
