"""LangGraph workflow 定義。

這個範本故意把 graph 做得很小，讓同事先理解三個最核心責任：

    START
      -> load_memory
      -> agent_sdk
      -> save_memory
      -> END

真正專案可以再加入：

    classify
    -> plan
    -> approval
    -> tool execution
    -> validate
    -> commit
    -> memory

但原則不變：

- LangGraph 管 workflow / state / checkpoint / resume。
- Agent SDK 管需要自主推理的 agent loop。
- 不要把整個企業流程都塞進一個巨大 Agent SDK node。
"""
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
    """不需要 checkpoint、但執行期間需要的 runtime context。

    這裡只放 user_id，主要用來決定 long-term memory namespace。

    與 AgentState 的差別：

    AgentState
        會沿著 graph node 流動，並被 checkpointer 保存。

    GraphContext
        是本次 invoke 的 runtime context，適合 tenant/user identity 這類資訊。
    """

    user_id: str


def build_graph(*, agent: AgentExecutor, checkpointer, store: BaseStore):
    """建立並 compile LangGraph。

    Args:
        agent:
            Agent abstraction。目前 RuntimeContainer 傳入 ClaudeAgentExecutor。
            Graph 不直接 import Claude SDK，因此未來可以替換 Agent implementation。

        checkpointer:
            short-term / workflow persistence。
            目前使用 AsyncPostgresSaver。

        store:
            cross-thread long-term memory。
            目前使用 AsyncPostgresStore。

    最重要的觀念：

        checkpointer != store

    checkpointer 回答：
        「這條 thread 的 workflow 執行到哪、state 是什麼？」

    store 回答：
        「這個 user/project 跨 thread 想長期保留哪些資訊？」
    """

    async def load_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        """讀取 cross-thread long-term memory。

        namespace 使用：

            ("memories", user_id)

        代表同一 user 的不同 thread 可以共享 long-term memory。

        這和 LangGraph thread checkpoint 是不同資料模型：

            thread-A ----+
                         +--> ("memories", user-1)
            thread-B ----+

        正式專案通常會再把 namespace 擴充 tenant/project，例如：

            ("tenant", tenant_id, "user", user_id, "memories")
        """
        namespace = ("memories", runtime.context.user_id)

        # 目前 template 不強迫任何 embedding provider，先用 provider-neutral 搜尋。
        # 若要 semantic memory，可在 PostgresStore 初始化時配置 embedding/index。
        hits = await runtime.store.asearch(namespace, limit=20)
        recent = hits[-5:]

        # 這裡只是簡化示範：把最近 memory 串成文字 context。
        # Production 建議做 memory type、ranking、token budget、去重與安全過濾。
        memory_context = "\n".join(str(item.value.get("data", "")) for item in recent)
        return {"memory_context": memory_context}

    async def run_agent(state: AgentState) -> dict:
        """LangGraph -> Agent SDK 的正式接點。

        使用者前面問「哪裡真的接到 Agent？」答案就是這裡：

            result = await agent.run(...)

        注意 graph 不知道 agent 是 Claude、OpenAI 或其他 SDK。
        它只依賴 AgentExecutor contract。
        """
        result = await agent.run(
            RunRequest(
                user_id=state["user_id"],
                thread_id=state["thread_id"],
                prompt=state["prompt"],

                # sdk_session_id 是 Agent SDK native session identity。
                # 它不是 LangGraph thread_id，只是被 LangGraph state 帶著走。
                sdk_session_id=state.get("sdk_session_id"),
            ),
            memory_context=state.get("memory_context", ""),
        )

        # 把 SDK-specific result 轉回 graph state。
        # 下游 node 不需要理解 Claude ResultMessage。
        return {
            "sdk_session_id": result.sdk_session_id,
            "result_text": result.text,
            "result_metadata": result.metadata,
        }

    async def save_memory(state: AgentState, runtime: Runtime[GraphContext]) -> dict:
        """選擇性把本輪結果寫入 long-term memory。

        remember=False 時完全不寫。

        目前範本直接保存 interaction 是為了教學清楚；正式環境更建議：

        interaction
          -> memory extraction/classification
          -> 去除敏感/暫時資訊
          -> 只保存值得長期存在的 facts/preferences/decisions
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

    # ==================================================================
    # Graph topology
    # ==================================================================
    builder = StateGraph(AgentState, context_schema=GraphContext)

    # Node 名稱最好具有明確責任；這也會直接影響 tracing/debugging 的可讀性。
    builder.add_node("load_memory", load_memory)
    builder.add_node("agent_sdk", run_agent)
    builder.add_node("save_memory", save_memory)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "agent_sdk")
    builder.add_edge("agent_sdk", "save_memory")
    builder.add_edge("save_memory", END)

    # compile 時綁定兩種 persistence：
    #
    #   checkpointer -> thread-scoped durable workflow state
    #   store        -> cross-thread long-term memory
    #
    # 之後 service 用 graph.ainvoke(configurable.thread_id=...) 執行。
    return builder.compile(checkpointer=checkpointer, store=store)
