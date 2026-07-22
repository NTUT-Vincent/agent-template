from __future__ import annotations

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette


async def create_a2a_app(*, executor, public_url: str, engine: AsyncEngine) -> Starlette:
    """Build an A2A Protocol 1.0 server using the official A2A Python SDK.

    A2A originated at Google and is now maintained by the A2A Project. The
    official Python package remains ``a2a-sdk``. Task state is persisted in
    PostgreSQL so A2A task lifecycle is not tied to a single Kubernetes Pod.
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
            AgentInterface(protocol_binding="JSONRPC", url=public_url, protocol_version="1.0")
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
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    return Starlette(routes=routes)
