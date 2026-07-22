from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)


class SessionResponse(BaseModel):
    id: UUID
    user_id: str
    thread_id: str
    sdk_session_id: str | None


class CreateTaskRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    remember: bool = False


class TaskResponse(BaseModel):
    id: UUID
    user_id: str
    thread_id: str
    status: str
    result: dict | None
    error: str | None
