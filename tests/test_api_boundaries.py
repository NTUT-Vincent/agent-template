from __future__ import annotations

import inspect

from agent_runtime.a2a import server
from agent_runtime.api import main


def test_a2a_is_mounted_on_main_fastapi_app() -> None:
    server_source = inspect.getsource(server)
    main_source = inspect.getsource(main)

    assert "add_a2a_routes_to_fastapi" in server_source
    assert 'rpc_url="/a2a"' in server_source
    assert "create_agent_card_routes" in server_source
    assert "await add_a2a_routes(" in main_source


def test_application_rest_is_explicitly_internal() -> None:
    source = inspect.getsource(main)

    assert '"/internal/v1/sessions"' in source
    assert '"/internal/v1/tasks"' in source
    assert '"/internal/v1/tasks/{task_id}"' in source
    assert '@app.post("/v1/tasks"' not in source
