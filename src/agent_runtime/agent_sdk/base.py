"""Agent SDK abstraction。

LangGraph 不應直接依賴 `claude_agent_sdk`。

因此先定義一個最小 contract：

    AgentExecutor.run(RunRequest) -> AgentResult

目前 implementation 是 ClaudeAgentExecutor；未來可以新增：

    OpenAIAgentExecutor
    InternalAgentExecutor
    MockAgentExecutor

只要遵守相同 contract，LangGraph topology 與 A2A layer 不需要跟著重寫。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.domain import AgentResult, RunRequest


class AgentExecutor(Protocol):
    """所有 Agent SDK adapter 應遵守的最小介面。"""

    async def run(self, request: RunRequest, *, memory_context: str = "") -> AgentResult:
        """執行一輪 Agent，回傳 SDK-neutral AgentResult。"""
        ...
