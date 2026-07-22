"""Agent SDK abstraction。

LangGraph 不應直接依賴 `claude_agent_sdk`。

因此先定義一個小而穩定的 contract：

    AgentExecutor.run(RunRequest) -> AgentResult

但 production runtime 還有一個很重要的需求：

    Agent SDK 在「真正開始執行」之後，可能比 ResultMessage 更早知道 native session id。

以 Claude Agent SDK 為例，Python 的 init `SystemMessage.data` 會帶 session id。
在 Kubernetes / 多 Pod 環境中，我們希望一拿到這個 ID 就立即寫進 application DB，
而不是等整輪 Agent 跑完才寫；否則 Pod 若在中途 crash，PostgreSQL SessionStore
可能已經有 transcript，但 application 不知道該用哪個 session id resume。

因此這個抽象提供 `on_session_id` callback。它屬於「生命週期事件」，不是 Agent
business result，也不應該塞進 prompt 或 LangGraph checkpoint。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_runtime.domain import AgentResult, RunRequest

# Agent SDK 一旦知道 native session id，就透過這個 callback 通知上層。
# 上層通常會立刻把 thread_id <-> sdk_session_id mapping 寫進 PostgreSQL。
SessionIdCallback = Callable[[str], Awaitable[None]]


class AgentExecutor(Protocol):
    """所有 Agent SDK adapter 應遵守的最小介面。"""

    async def run(
        self,
        request: RunRequest,
        *,
        memory_context: str = "",
        on_session_id: SessionIdCallback | None = None,
    ) -> AgentResult:
        """執行一輪 Agent，回傳 SDK-neutral AgentResult。

        `on_session_id` 是 optional lifecycle callback：

        - 第一次 session：SDK init 後盡早回報新 session id。
        - resume session：也可以再次回報同一個 id；上層應做 idempotent update。
        - callback 失敗時應讓 request 失敗，因為無法保存 resume identity 代表
          Kubernetes crash recovery 會失去保證。
        """
        ...
