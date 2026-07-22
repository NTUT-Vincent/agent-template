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
    """Register the official A2A 1.0 JSON-RPC surface on the main FastAPI app.

    Public protocol endpoints:
    - GET /.well-known/agent-card.json
    - POST /a2a (A2A JSON-RPC methods, including streaming over SSE)

    A2A protocol task state is owned by the official SDK and persisted in PostgreSQL.
    """
    skill = AgentSkill(
        id="general_agent",
        name="General enterprise agent",
        description="Runs durable LangGraph + Agent SDK tasks.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["langgraph", "agent-sdk", "a2a"],
        examples=["Analyze this incident and summarize likely root causes."],
    )
    card = AgentCard(
        name="Agent Runtime Platform",
        description="Durable Kubernetes-ready agent runtime",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=public_url,
                protocol_version="1.0",
            )
        ],
        skills=[skill],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )

    task_store = DatabaseTaskStore(
        engine=engine,
        create_table=True,
        table_name="a2a_tasks",
    )
    await task_store.initialize()

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
    )

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
