from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    user_id: str
    thread_id: str
    prompt: str
    remember: bool
    sdk_session_id: str | None
    memory_context: str
    result_text: str
    result_metadata: dict[str, Any]
