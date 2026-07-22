from __future__ import annotations

from typing import Protocol

from agent_runtime.domain import AgentResult, RunRequest


class AgentExecutor(Protocol):
    async def run(self, request: RunRequest, *, memory_context: str = "") -> AgentResult: ...
