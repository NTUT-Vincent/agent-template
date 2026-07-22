from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunRequest:
    user_id: str
    thread_id: str
    prompt: str
    sdk_session_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    text: str
    sdk_session_id: str | None
    metadata: dict[str, Any]
