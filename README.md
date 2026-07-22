# Agent Runtime Platform Template

A reference template for building a **Kubernetes-ready enterprise agent service** with:

- LangGraph for durable workflow state
- PostgreSQL for checkpoints, long-term memory and sessions
- Claude Agent SDK for agent reasoning/tools
- the official A2A Python SDK (`a2a-sdk`) for agent-to-agent interoperability
- Celery + Redis for internal background jobs
- FastAPI as the application/API host

The main design rule is simple:

> **A2A is the public agent interoperability contract. Internal REST is application-specific. Both reuse the same LangGraph + Agent SDK runtime.**

## Architecture

```text
                         Agent / A2A Client
                                |
                  GET /.well-known/agent-card.json
                         POST /a2a (JSON-RPC)
                                |
                                v
                    Official A2A Python SDK
                   AgentExecutor / TaskStore
                                |
                                v
+-------------------- FastAPI application --------------------+
|                                                             |
|  Public A2A                              Internal REST       |
|  /a2a                                   /internal/v1/*      |
|     |                                         |             |
|     |                                         v             |
|     |                                  PostgreSQL app_tasks |
|     |                                         |             |
|     |                                  Redis / Celery       |
|     |                                         |             |
|     +--------------------+--------------------+             |
|                          v                                  |
|                AgentRuntimeService                          |
|                          |                                  |
|                       LangGraph                             |
|                  /                     \                    |
|       AsyncPostgresSaver         AsyncPostgresStore         |
|          checkpoints              long-term memory          |
|                          |                                  |
|                   Claude Agent SDK                          |
|                          |                                  |
|             PostgreSQL SessionStore                        |
+-------------------------------------------------------------+
```

## API boundary

This distinction is intentional. Do not make every endpoint "look like A2A".

| Endpoint | Contract | Purpose |
|---|---|---|
| `GET /.well-known/agent-card.json` | **A2A** | Agent discovery / capabilities |
| `POST /a2a` | **A2A 1.0 JSON-RPC** | Agent-to-agent messages, tasks, streaming, cancellation |
| `GET /health` | Operations | Kubernetes health probe |
| `GET /docs` | FastAPI | Development/OpenAPI documentation |
| `POST /internal/v1/sessions` | Internal REST | Create an application conversation/thread |
| `POST /internal/v1/tasks` | Internal REST | Submit a Celery-backed application job |
| `GET /internal/v1/tasks/{id}` | Internal REST | Poll internal job status |

The A2A routes are registered using the official SDK route helpers and are served by the same FastAPI process as the internal API.

## Responsibility model

### A2A SDK

Owns protocol-facing concepts:

- Agent Card
- Message / Task / Artifact
- A2A task lifecycle
- JSON-RPC transport
- streaming / resubscription behavior
- cancellation protocol

A2A task state is persisted with the official `DatabaseTaskStore` in PostgreSQL (`a2a_tasks`).

### LangGraph

Owns execution/workflow state:

- durable `thread_id`
- checkpoint / resume
- workflow routing
- human-in-the-loop boundaries
- long-running workflow state

`A2A contextId` is mapped to the LangGraph `thread_id`, so multiple messages/tasks in one A2A conversation can continue the same durable graph context.

### Agent SDK

Owns adaptive agent execution:

- reasoning loop
- MCP/tool usage
- subagents
- file/code operations

Native SDK transcripts are mirrored to PostgreSQL through `PostgresClaudeSessionStore`, so resuming a session does not depend on a sticky Kubernetes Pod.

### Celery / Redis

Only owns **internal job delivery and backpressure**.

It is not the source of truth for agent/workflow state. Internal job status is persisted in PostgreSQL; LangGraph checkpoints remain the workflow source of truth.

A2A requests do **not** create a duplicate Celery/A2A task chain. They invoke the same protocol-neutral `AgentRuntimeService.run_prompt()` directly, while the A2A SDK owns the visible A2A Task lifecycle.

## Persistence model

| Concern | Storage / component |
|---|---|
| Application conversation mapping | `app_sessions` |
| Internal job lifecycle | `app_tasks` |
| A2A protocol task lifecycle | official `DatabaseTaskStore` → `a2a_tasks` |
| Workflow checkpoints | `AsyncPostgresSaver` |
| Cross-thread memory | `AsyncPostgresStore` |
| Claude native transcript | `PostgresClaudeSessionStore` |
| Queue / backpressure | Redis + Celery |
| Business/domain data | domain DB / MCP services |

## Project layout

```text
src/agent_runtime/
├── a2a/
│   ├── executor.py       # official AgentExecutor -> shared runtime
│   └── server.py         # Agent Card + /a2a official routes
├── agent_sdk/
│   ├── base.py
│   ├── claude.py
│   └── mock.py
├── api/
│   ├── main.py           # FastAPI host + internal REST
│   └── schemas.py
├── graph/
│   ├── builder.py
│   └── state.py
├── persistence/
│   ├── claude_session_store.py
│   └── langgraph.py
├── tasks/
│   ├── celery_app.py
│   └── jobs.py
├── db.py
├── runtime.py
└── service.py            # protocol-neutral execution boundary
```

## Run locally

Requirements: Docker and an Anthropic API key.

```bash
cp .env.example .env
# set ANTHROPIC_API_KEY
docker compose up --build
```

Useful endpoints:

```text
http://localhost:8000/docs
http://localhost:8000/health
http://localhost:8000/.well-known/agent-card.json
http://localhost:8000/a2a
```

`A2A_PUBLIC_URL` must point to the externally reachable JSON-RPC endpoint advertised in the Agent Card:

```env
A2A_PUBLIC_URL=http://localhost:8000/a2a
```

For an A2A integration test, prefer an official A2A client SDK that first reads the Agent Card and then sends messages through the advertised interface rather than building custom `/agent/...` HTTP calls.

### Internal REST example

Create a UI/application session:

```bash
curl -s http://localhost:8000/internal/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"user_id":"demo-user"}'
```

Submit a background job with the returned `thread_id`:

```bash
curl -s http://localhost:8000/internal/v1/tasks \
  -H 'content-type: application/json' \
  -d '{
    "user_id":"demo-user",
    "thread_id":"THREAD_ID",
    "prompt":"Analyze this incident.",
    "remember":true
  }'
```

## How to extend this template

When adding a new capability, keep these boundaries:

1. **Another agent needs to call it** → expose/update an A2A `AgentSkill` and keep implementation behind `AgentExecutor`.
2. **The agent needs a tool** → expose the capability through MCP/tool integration, not A2A.
3. **It is an internal deterministic workflow step** → add a LangGraph node/service, not a separate A2A agent.
4. **UI/admin needs application-specific operations** → add under `/internal/v1/...`, not to the A2A protocol surface.
5. **The operation has side effects** → make the tool idempotent and auditable; checkpointing alone does not guarantee exactly-once execution.

Avoid architectures like:

```text
Agent A -> custom POST /agent-b -> Agent B
```

and calling that "A2A". A standard A2A boundary should expose Agent Card discovery and the protocol Task/Message/Artifact lifecycle through the official SDK.

## Production hardening backlog

This repository is a reference template, not a finished enterprise platform. Before production use:

- replace startup `create_all()` / SDK `setup()` with managed migrations
- add authentication and map authenticated identity/tenant into A2A `ServerCallContext`
- add authorization/tenant scoping to sessions, memory namespaces and task stores
- add per-thread distributed locking when concurrent mutation is possible
- use an outbox pattern for internal DB-job enqueue consistency
- add idempotency keys around side-effecting MCP/tool calls
- add deadlines/timeouts and cooperative cancellation to downstream tools
- add OpenTelemetry traces, structured logs and audit records
- define a deliberate long-term-memory extraction policy instead of remembering every message
- encrypt or strictly restrict checkpoint/session data

## Kubernetes

`k8s/` contains API and worker Deployment examples. PostgreSQL and Redis should normally be managed/external services in production.

API Pods are horizontally replaceable: A2A task state, LangGraph checkpoints, long-term memory and Agent SDK session data are persisted outside the Pod.

## Versions

Scaffolded in July 2026 against the then-current major lines: LangGraph 1.2.x, `langgraph-checkpoint-postgres` 3.1.x, Claude Agent SDK 0.2.x and official A2A Python SDK 1.1.x.
