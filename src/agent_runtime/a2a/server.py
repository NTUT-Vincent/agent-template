from __future__ import annotations

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette


def create_a2a_app(*, executor, public_url: str) -> Starlette:
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
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    return Starlette(routes=routes)
