# Agent Runtime Platform

A minimal Kubernetes-oriented agent framework built around **LangGraph + PostgreSQL + Claude Agent SDK + A2A + Celery/Redis**.

The goal is to keep each durability concern separate:

```text
Client / Generative UI
        |
        v
     FastAPI ---------------- A2A JSON-RPC
        |
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
- Use a persistent A2A `TaskStore` instead of `InMemoryTaskStore` before exposing A2A in production.
- Add SSE/WebSocket event fan-out for live UI updates; do not use the stream as the source of truth.
- Add OTel collector, trace IDs, structured logs and audit tables.
- Encrypt or strictly control checkpoint/session data; `LANGGRAPH_STRICT_MSGPACK=true` is set by default.

## Kubernetes

`k8s/` contains API and worker Deployments plus example config. In production, PostgreSQL and Redis should normally be managed/external services.

The API and worker Pods are intentionally stateless. Native Claude Agent SDK transcripts are mirrored to PostgreSQL so a worker restart or re-scheduling does not require a sticky Pod.

## A2A

`agent_runtime.a2a` contains a protocol bridge using the official A2A Python SDK. The bridge delegates requests to the same task/runtime layer. For production, back A2A task state with PostgreSQL and validate all remote Agent Card/message/artifact input as untrusted.

## Package versions used when scaffolded

Scaffolded July 2026 against the then-current major lines: LangGraph 1.2.x, `langgraph-checkpoint-postgres` 3.1.x, Claude Agent SDK 0.2.x and A2A SDK 1.1.x.
