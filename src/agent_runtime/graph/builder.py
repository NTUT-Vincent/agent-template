from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore

from agent_runtime.agent_sdk.base import AgentExecutor
from agent_runtime.domain import RunRequest
from agent_runtime.graph.state import AgentState


@dataclass(frozen=True, slots=True)
class GraphContext:
    user_id: str


def build_graph(*, agent: AgentExecutor, checkpointer, store: BaseStore):
    async def load_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        namespace = ("memories", runtime.context.user_id)
        # Semantic search requires an embedding index. The base scaffold intentionally
        # stays provider-neutral, so it loads recent namespace entries without vectors.
        hits = await runtime.store.asearch(namespace, limit=20)
        recent = hits[-5:]
        memory_context = "\n".join(str(item.value.get("data", "")) for item in recent)
        return {"memory_context": memory_context}

    async def run_agent(state: AgentState) -> dict:
        result = await agent.run(
            RunRequest(
                user_id=state["user_id"],
                thread_id=state["thread_id"],
                prompt=state["prompt"],
                sdk_session_id=state.get("sdk_session_id"),
            ),
            memory_context=state.get("memory_context", ""),
        )
        return {
            "sdk_session_id": result.sdk_session_id,
            "result_text": result.text,
            "result_metadata": result.metadata,
        }

    async def save_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
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
