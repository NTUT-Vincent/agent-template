from agent_runtime.domain import AgentResult, RunRequest


class MockAgentExecutor:
    async def run(self, request: RunRequest, *, memory_context: str = "") -> AgentResult:
        return AgentResult(
            text=f"mock: {request.prompt}",
            sdk_session_id=request.sdk_session_id or f"mock-{request.thread_id}",
            metadata={"memory_context": memory_context},
        )
