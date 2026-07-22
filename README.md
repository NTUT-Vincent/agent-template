# Agent Runtime Platform

A minimal Kubernetes-oriented agent framework built around **LangGraph + PostgreSQL + Claude Agent SDK + the official A2A Python SDK + Celery/Redis**.

The goal is to keep each durability concern separate:

```text
Client / Generative UI
        |
        v
     FastAPI ---------------- A2A JSON-RPC
        |                         |
        |                         +-- Official A2A Python SDK
        |                         +-- PostgreSQL a2a_tasks
        v
   PostgreSQL app_tasks
        |
        v
   Redis / Celery queue
        |
        v
   Worker / LangGraph
      |         |
      |         +-- PostgresSaver: thread checkpoints / resume
      |         +-- PostgresStore: long-term cross-thread memory
      |
      +-- Claude Agent SDK
             +-- Postgres SessionStore: native SDK transcript / resume
             +-- MCP / tools / subagents (add as needed)
```

## Why this split

- **LangGraph `thread_id`** is the durable workflow identity.
- **PostgresSaver** stores graph checkpoints so another worker can resume a thread.
- **PostgresStore** stores long-term memories across threads. This scaffold uses explicit `remember=true` writes and provider-neutral retrieval; add an embedding index for semantic search.
- **Claude Agent SDK SessionStore** mirrors native SDK transcripts to PostgreSQL, avoiding a dependency on one Pod's local filesystem.
- **Celery/Redis** is delivery/backpressure, not the source of truth. Task status lives in PostgreSQL.
- **A2A** is a transport/interoperability boundary. It should call the same runtime instead of creating a second agent loop.

## Current MVP flow

1. `POST /v1/sessions` creates an application conversation and LangGraph `thread_id`.
2. `POST /v1/tasks` writes a durable task row and enqueues its ID.
3. A Celery worker loads the row and invokes LangGraph with the same `thread_id`.
4. LangGraph loads user-scoped long-term memory, then calls the Claude Agent SDK node.
5. If the task sets `remember=true`, the graph writes the interaction into `PostgresStore`.
6. The SDK transcript is mirrored to Postgres with eager flush.
7. The worker stores the result and latest `sdk_session_id` back into PostgreSQL.
8. A2A requests are handled by the official A2A Python SDK; A2A task lifecycle is persisted in PostgreSQL through `DatabaseTaskStore`.

## Run locally

Requirements: Docker and an Anthropic API key.

```bash
cp .env.example .env
# edit ANTHROPIC_API_KEY in .env
docker compose up --build
```

Create a session:

```bash
curl -s http://localhost:8000/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"user_id":"vincent"}'
```

Then enqueue a task with the returned `thread_id`:

```bash
curl -s http://localhost:8000/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"user_id":"vincent","thread_id":"THREAD_ID","prompt":"Analyze this incident.","remember":true}'
```

Poll:

```bash
curl -s http://localhost:8000/v1/tasks/TASK_ID
```

## Persistence model

| Concern | Storage / component |
|---|---|
| UI conversation mapping | `app_sessions` |
| Task lifecycle | `app_tasks` |
| Workflow checkpoint | `AsyncPostgresSaver` |
| Cross-session memory | `AsyncPostgresStore` |
| Claude native transcript | `PostgresClaudeSessionStore` |
| A2A protocol task lifecycle | Official `DatabaseTaskStore` → PostgreSQL `a2a_tasks` |
| Queue / backpressure | Redis + Celery |
| Business data | Your domain DB / MCP services |

Do not equate queue delivery with workflow state. A lost Redis job should be recoverable by reconciling PostgreSQL rows that remain `queued` or stale `running`.

## Production hardening backlog

- Replace `create_all()` / `setup()` at app startup with migrations or a Kubernetes migration Job.
- Add auth and tenant scoping to every session, task, memory namespace and A2A request.
- Add a memory extraction policy (or LangMem) instead of persisting every opt-in interaction verbatim; configure a PostgresStore embedding index for semantic retrieval.
- Add per-thread distributed locks to prevent two workers from mutating one thread concurrently.
- Add an outbox pattern so `app_tasks` creation and queue publication cannot diverge.
- Add idempotency keys around side-effecting MCP/tool calls.
- Add task cancellation, timeout/deadline propagation and graceful worker shutdown.
- Add SSE/WebSocket event fan-out for live UI updates; do not use the stream as the source of truth.
- Add OTel collector, trace IDs, structured logs and audit tables.
- Encrypt or strictly control checkpoint/session data; `LANGGRAPH_STRICT_MSGPACK=true` is set by default.

## Kubernetes

`k8s/` contains API and worker Deployments plus example config. In production, PostgreSQL and Redis should normally be managed/external services.

The API and worker Pods are intentionally stateless. Native Claude Agent SDK transcripts and A2A task state are persisted in PostgreSQL so worker/API restarts do not require sticky Pods.

## A2A

This project uses the **official A2A Python SDK**, installed as `a2a-sdk`. A2A was originally introduced by Google; the protocol and SDKs are now maintained under the independent A2A Project (`a2aproject/a2a-python`). This is not the third-party `python-a2a` package.

`agent_runtime.a2a` uses official SDK components including:

- `AgentExecutor` / `RequestContext`
- `EventQueue` / `TaskUpdater`
- `DefaultRequestHandler`
- Agent Card and JSON-RPC route helpers
- `DatabaseTaskStore` backed by PostgreSQL

The bridge delegates business execution to the existing LangGraph + Agent SDK runtime instead of creating another agent loop.

## Package versions used when scaffolded

Scaffolded July 2026 against the then-current major lines: LangGraph 1.2.x, `langgraph-checkpoint-postgres` 3.1.x, Claude Agent SDK 0.2.x and official A2A Python SDK 1.1.x.
