from agent_runtime.agent_sdk.base import SessionIdCallback
from agent_runtime.domain import AgentResult, RunRequest


class MockAgentExecutor:
    async def run(
        self,
        request: RunRequest,
        *,
        memory_context: str = "",
        on_session_id: SessionIdCallback | None = None,
    ) -> AgentResult:
        session_id = request.sdk_session_id or f"mock-{request.thread_id}"
        if on_session_id is not None:
            await on_session_id(session_id)

        return AgentResult(
            text=f"mock: {request.prompt}",
            sdk_session_id=session_id,
            metadata={"memory_context": memory_context},
        )
