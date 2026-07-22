"""LangGraph workflow 定義。

這個範本故意把 graph 做得很小，讓同事先理解三個最核心責任：

    START
      -> load_memory
      -> agent_sdk
      -> save_memory
      -> END

真正專案可以再加入 classify / plan / approval / validate 等節點。

核心分工：
- LangGraph 管 workflow / state / checkpoint / resume。
- Agent SDK 管需要自主推理的 agent loop。
- Claude SessionStore 管 SDK native transcript 的跨 Pod mirror / resume。
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore

from agent_runtime.agent_sdk.base import AgentExecutor, SessionIdCallback
from agent_runtime.domain import RunRequest
from agent_runtime.graph.state import AgentState


@dataclass(frozen=True, slots=True)
class GraphContext:
    """本次 invoke 的 runtime context，不會被 LangGraph checkpoint 保存。

    `user_id`
        用來決定 long-term memory namespace。

    `on_sdk_session_id`
        Agent SDK init 後一拿到 native session id，就立即通知 application layer。
        這個 callback 刻意放在 Context 而不是 AgentState，因為 callback 本身不可序列化，
        也不是需要 checkpoint 的 workflow state。

    這是處理 Kubernetes crash window 的關鍵：

        Claude SDK init
          -> 得到 sdk_session_id
          -> callback 立刻寫 app_sessions
          -> Agent 繼續執行

    因此即使 Pod 在 ResultMessage 前 crash，下一顆 Pod 仍有機會從 PostgreSQL 找到
    sdk_session_id，再透過 SessionStore.load() resume transcript。
    """

    user_id: str
    on_sdk_session_id: SessionIdCallback | None = None


def build_graph(*, agent: AgentExecutor, checkpointer, store: BaseStore):
    """建立並 compile LangGraph。

    `checkpointer` 保存 thread-scoped workflow state；`store` 保存跨 thread long-term memory。
    Claude SDK transcript 不放在這兩者裡，而是由 Agent SDK 的 SessionStore 管理。
    """

    async def load_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        """讀取 cross-thread long-term memory。"""
        namespace = ("memories", runtime.context.user_id)

        # Template 先保持 provider-neutral；production 可再加入 embedding/index、ranking、
        # token budget、去重與 memory type。
        hits = await runtime.store.asearch(namespace, limit=20)
        recent = hits[-5:]
        memory_context = "\n".join(str(item.value.get("data", "")) for item in recent)
        return {"memory_context": memory_context}

    async def run_agent(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        """LangGraph -> Agent SDK 的正式接點。

        Session 有兩條不同的 persistence path：

        1. `sdk_session_id`
           由 application DB (`app_sessions`) 保存 mapping。

        2. SDK transcript
           由 Claude `SessionStore` mirror 到 PostgreSQL。

        只有兩者都存在，跨 Pod resume 才完整：

            thread_id -> sdk_session_id -> SessionStore transcript
        """
        result = await agent.run(
            RunRequest(
                user_id=state["user_id"],
                thread_id=state["thread_id"],
                prompt=state["prompt"],
                sdk_session_id=state.get("sdk_session_id"),
            ),
            memory_context=state.get("memory_context", ""),
            on_session_id=runtime.context.on_sdk_session_id,
        )

        return {
            "sdk_session_id": result.sdk_session_id,
            "result_text": result.text,
            "result_metadata": result.metadata,
        }

    async def save_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        """選擇性把本輪結果寫入 long-term memory。

        這跟 Claude SessionStore 不同：

        - SessionStore = 原始 SDK conversation/tool transcript，供 resume。
        - PostgresStore = 應長期保存、可跨 thread 使用的 application memory。
        """
        if not state.get("remember", False):
            return {}

        namespace = ("memories", runtime.context.user_id)
        await runtime.store.aput(
            namespace,
            str(uuid4()),
            {
                "data": f"User: {state['prompt']}\nAgent: {state.get('result_text', '')}",
                "thread_id": state["thread_id"],
            },
        )
        return {}

    builder = StateGraph(AgentState, context_schema=GraphContext)
    builder.add_node("load_memory", load_memory)
    builder.add_node("agent_sdk", run_agent)
    builder.add_node("save_memory", save_memory)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "agent_sdk")
    builder.add_edge("agent_sdk", "save_memory")
    builder.add_edge("save_memory", END)

    return builder.compile(checkpointer=checkpointer, store=store)
