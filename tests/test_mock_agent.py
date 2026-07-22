import pytest

from agent_runtime.agent_sdk.mock import MockAgentExecutor
from agent_runtime.domain import RunRequest


@pytest.mark.asyncio
async def test_mock_agent_returns_session_id():
    agent = MockAgentExecutor()
    result = await agent.run(RunRequest(user_id="u", thread_id="t", prompt="hello"))
    assert result.text == "mock: hello"
    assert result.sdk_session_id == "mock-t"
