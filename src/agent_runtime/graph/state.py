"""LangGraph thread state 定義。

這裡的欄位會沿著 graph node 傳遞；搭配 checkpointer 時，相關 state 會形成
thread-scoped durable workflow history。

設計原則：只放 workflow 真正需要恢復/傳遞的 state。
不要把整個 business database 或大型 binary artifact 塞進 checkpoint。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """目前最小 Agent workflow state。

    欄位用途：

    messages
        預留給 LangGraph message reducer。現在主要 prompt 流程尚未使用完整 messages history。

    user_id
        application identity；long-term memory namespace 會參考它。

    thread_id
        LangGraph durable thread identity。

    prompt
        本輪 Agent input。

    remember
        是否在執行後寫入 long-term memory。

    sdk_session_id
        Claude Agent SDK native session id；只是被 graph state 帶著走，不等於 thread_id。

    memory_context
        load_memory node 產生、準備注入 Agent 的相關 long-term memory。

    result_text / result_metadata
        agent_sdk node 回傳給後續 workflow node 的 SDK-neutral result。
    """

    messages: Annotated[list, add_messages]
    user_id: str
    thread_id: str
    prompt: str
    remember: bool
    sdk_session_id: str | None
    memory_context: str
    result_text: str
    result_metadata: dict[str, Any]
