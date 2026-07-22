"""跨 layer 共用的 domain contracts。

這個檔案故意不 import FastAPI、A2A、LangGraph、Claude SDK。
目的是讓不同 layer 透過乾淨的 application model 溝通，而不是互相傳 framework-specific type。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    """Internal app_tasks 的 lifecycle。

    注意：這不是 A2A TaskState。
    A2A protocol lifecycle 由官方 A2A SDK 自己的 TaskState 管理。
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunRequest:
    """LangGraph -> AgentExecutor 的 SDK-neutral input。

    thread_id：
        LangGraph/application workflow identity。

    sdk_session_id：
        Agent SDK native resume identity。

    兩者刻意分開，避免把 orchestration state 與 SDK transcript 綁死。
    """

    user_id: str
    thread_id: str
    prompt: str
    sdk_session_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    """AgentExecutor -> LangGraph 的 SDK-neutral output。

    ClaudeAgentExecutor 會把 ResultMessage 轉成這個 type；
    因此 graph/service/A2A 不需要知道 Claude SDK 的 message class。
    """

    text: str
    sdk_session_id: str | None
    metadata: dict[str, Any]
