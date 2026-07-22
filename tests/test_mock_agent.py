import pytest

from agent_runtime.agent_sdk.mock import MockAgentExecutor
from agent_runtime.domain import RunRequest


@pytest.mark.asyncio
async def test_mock_agent_returns_and_reports_session_id():
    agent = MockAgentExecutor()
    observed: list[str] = []

    async def on_session_id(session_id: str) -> None:
        observed.append(session_id)

    result = await agent.run(
        RunRequest(user_id="u", thread_id="t", prompt="hello"),
        on_session_id=on_session_id,
    )

    assert result.text == "mock: hello"
    assert result.sdk_session_id == "mock-t"
    assert observed == ["mock-t"]
