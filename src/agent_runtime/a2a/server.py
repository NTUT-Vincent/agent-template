"""A2A Server 組裝層。

這個檔案只做三件事：

1. 宣告 Agent Card：告訴其他 Agent「我是誰、會什麼、要去哪裡呼叫」。
2. 建立官方 A2A RequestHandler / TaskStore。
3. 把官方 A2A routes 註冊到既有 FastAPI app。

請不要把真正的商業邏輯寫在這裡。
真正的 Agent 執行會從 RuntimeA2AExecutor 再進 AgentRuntimeService / LangGraph。
"""
from __future__ import annotations

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine


async def add_a2a_routes(
    app: FastAPI,
    *,
    executor,
    public_url: str,
    engine: AsyncEngine,
) -> None:
    """把官方 A2A 1.0 JSON-RPC server 掛到 FastAPI。

    最終對外會看到：

        GET  /.well-known/agent-card.json
        POST /a2a

    /a2a 不是我們自己定義 request body 的普通 REST endpoint。
    Request/response、Task、Message、Artifact、streaming 等格式都交給官方 SDK。

    Args:
        app:
            主 FastAPI application。A2A 不需要另外開一個 port/process。

        executor:
            A2A AgentExecutor。收到標準 A2A Message 後，真正要怎麼執行 Agent，
            就由這個 executor 決定。

        public_url:
            寫進 Agent Card 的「外部可連線 URL」。部署到 K8s 時不能留 localhost。
            例如：https://my-agent.company.com/a2a

        engine:
            SQLAlchemy async engine，提供給官方 DatabaseTaskStore 使用。
    """

    # ------------------------------------------------------------------
    # AgentSkill = 這個 Agent 對外宣告的能力。
    # ------------------------------------------------------------------
    # 同事要新增「其他 Agent 可以發現並呼叫的能力」時，可以先從這裡擴充 skill。
    # 但 skill 只是 capability metadata，不代表要在這裡實作商業邏輯。
    skill = AgentSkill(
        id="general_agent",
        name="General enterprise agent",
        description="Runs durable LangGraph + Agent SDK tasks.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["langgraph", "agent-sdk", "a2a"],
        examples=["Analyze this incident and summarize likely root causes."],
    )

    # ------------------------------------------------------------------
    # AgentCard = A2A 的 discovery metadata。
    # ------------------------------------------------------------------
    # 其他 Agent 通常會先讀 /.well-known/agent-card.json，確認：
    #   - 這個 Agent 支援什麼能力
    #   - 使用哪一種 protocol binding
    #   - endpoint 在哪裡
    #   - 是否支援 streaming
    card = AgentCard(
        name="Agent Runtime Platform",
        description="Durable Kubernetes-ready agent runtime",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                # 這個範本選 JSON-RPC binding。
                protocol_binding="JSONRPC",
                url=public_url,
                protocol_version="1.0",
            )
        ],
        skills=[skill],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )

    # ------------------------------------------------------------------
    # A2A Task persistence
    # ------------------------------------------------------------------
    # 不使用 InMemoryTaskStore，因為在 Kubernetes：
    #   Pod A 建立 Task -> Pod A 被重啟 -> 記憶體 Task 消失
    #
    # 改用官方 DatabaseTaskStore 後：
    #   A2A Task lifecycle -> PostgreSQL a2a_tasks
    #
    # 所以任一 API Pod 都能讀到同一份 A2A Task state。
    task_store = DatabaseTaskStore(
        engine=engine,
        create_table=True,
        table_name="a2a_tasks",
    )
    await task_store.initialize()

    # DefaultRequestHandler 是官方 SDK 的協定處理層。
    # 它會把 A2A protocol request 轉成交給 executor 的工作，並使用 task_store
    # 維護標準 Task lifecycle。
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
    )

    # ------------------------------------------------------------------
    # 真正把 A2A「跑起來」的地方。
    # ------------------------------------------------------------------
    # FastAPI 原本只有 /health、/internal/v1/...。
    # 執行這段後，官方 SDK 產生的 Agent Card 與 JSON-RPC routes 會被加入 app。
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
