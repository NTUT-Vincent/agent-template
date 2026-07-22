# Agent Runtime Platform Template

給公司內部 Agent 專案使用的 **Kubernetes-ready Agent Runtime 範本**。

這個 repository 的重點不是示範「怎麼 call LLM」，而是提供一套可延伸的工程骨架，處理：

- A2A Agent-to-Agent 標準介面
- LangGraph durable workflow / checkpoint
- PostgreSQL persistence
- Long-term memory
- Agent SDK native session
- Celery / Redis background job
- FastAPI application API
- Kubernetes stateless deployment

> 核心原則：**A2A 是 Agent 對 Agent 的標準協定；Internal REST 是產品自己的 API；兩者共用同一個 LangGraph + Agent SDK runtime。**

---

## 👋 第一次看這個 Repo？從這裡開始

完整繁中教學：

**[`docs/TUTORIAL.zh-TW.md`](docs/TUTORIAL.zh-TW.md)**

建議閱讀順序：

```text
1. README.md
2. docs/TUTORIAL.zh-TW.md
3. src/agent_runtime/api/main.py
4. src/agent_runtime/a2a/server.py
5. src/agent_runtime/a2a/executor.py
6. src/agent_runtime/service.py
7. src/agent_runtime/graph/builder.py
8. src/agent_runtime/runtime.py
9. src/agent_runtime/agent_sdk/claude.py
10. src/agent_runtime/persistence/langgraph.py
11. src/agent_runtime/persistence/claude_session_store.py
12. src/agent_runtime/tasks/celery_app.py
13. src/agent_runtime/tasks/jobs.py
14. src/agent_runtime/db.py
```

上述核心檔案都加入了大量繁中註解，可以直接沿 call chain 往下讀。

---

# 1. 一張圖先看懂整體架構

```text
                           Other Agent
                               |
                         A2A Protocol
                               |
              GET /.well-known/agent-card.json
                         POST /a2a
                               |
                               v
                    Official A2A Python SDK
                    AgentExecutor / TaskStore
                               |
                               v
+----------------------- FastAPI -----------------------+
|                                                       |
|  Public A2A                        Internal REST       |
|  /a2a                             /internal/v1/*      |
|     |                                   |             |
|     |                                   v             |
|     |                             app_tasks           |
|     |                                   |             |
|     |                             Redis / Celery      |
|     |                                   |             |
|     +----------------+------------------+             |
|                      v                                |
|             AgentRuntimeService                       |
|                      |                                |
|                      v                                |
|                  LangGraph                            |
|              /               \                        |
|   PostgresSaver             PostgresStore             |
|    checkpoint              long memory                |
|                      |                                |
|                      v                                |
|               agent_sdk node                         |
|                      |                                |
|                      v                                |
|             Claude Agent SDK                         |
|                      |                                |
|          PostgresClaudeSessionStore                  |
+-------------------------------------------------------+
```

---

# 2. A2A 到底在哪裡「跑起來」？

整個 container 只啟動一個 Uvicorn/FastAPI server：

```text
uvicorn agent_runtime.api.main:app
```

FastAPI startup 時：

```text
src/agent_runtime/api/main.py
  -> lifespan()
  -> add_a2a_routes(...)
```

接著：

```text
src/agent_runtime/a2a/server.py
  -> add_a2a_routes_to_fastapi(...)
```

因此同一個 process 同時提供：

```text
GET  /.well-known/agent-card.json
POST /a2a

GET  /health
POST /internal/v1/sessions
POST /internal/v1/tasks
GET  /internal/v1/tasks/{id}
```

A2A 不需要另外開第二個 port 或第二個 server process。

---

# 3. A2A 到底在哪裡「接到 Agent」？

最重要的 call chain：

```text
POST /a2a
   |
   v
Official A2A SDK
   |
   v
RuntimeA2AExecutor.execute()
   |
   v
AgentRuntimeService.run_prompt()
   |
   v
LangGraph.ainvoke()
   |
   v
node: agent_sdk
   |
   v
ClaudeAgentExecutor.run()
   |
   v
claude_agent_sdk.query()
```

三個關鍵 bridge：

```python
# A2A -> Application Runtime
RuntimeA2AExecutor(runtime.service.run_prompt)

# Application Runtime -> LangGraph
await self._graph.ainvoke(...)

# LangGraph -> Agent SDK
await agent.run(...)
```

真正進 Claude Agent SDK：

```python
async for message in query(
    prompt=request.prompt,
    options=options,
):
    ...
```

---

# 4. API 邊界

不要讓所有 endpoint 都假裝是 A2A。

| Endpoint | Contract | 用途 |
|---|---|---|
| `GET /.well-known/agent-card.json` | **A2A** | Agent discovery / capabilities |
| `POST /a2a` | **A2A 1.0 JSON-RPC** | Agent-to-Agent message/task/artifact |
| `GET /health` | Operations | Kubernetes probe |
| `GET /docs` | FastAPI | OpenAPI / 開發文件 |
| `POST /internal/v1/sessions` | Internal REST | 建立產品 conversation/thread |
| `POST /internal/v1/tasks` | Internal REST | 建立 Celery background job |
| `GET /internal/v1/tasks/{id}` | Internal REST | 查 internal job 狀態 |

判斷方式：

```text
另一個 Agent 要呼叫這個 Agent？
    -> A2A

UI / Admin / 公司後端要操作產品功能？
    -> /internal/v1/*
```

---

# 5. 各元件到底負責什麼？

## A2A SDK

負責 protocol-facing concepts：

- Agent Card
- Message
- Task
- Artifact
- JSON-RPC transport
- streaming
- cancellation
- A2A task lifecycle

A2A Task persistence：

```text
DatabaseTaskStore -> PostgreSQL a2a_tasks
```

---

## LangGraph

負責：

- durable `thread_id`
- workflow state
- checkpoint / resume
- routing
- human-in-the-loop boundary
- deterministic orchestration

目前 graph：

```text
START
  -> load_memory
  -> agent_sdk
  -> save_memory
  -> END
```

---

## Agent SDK

負責：

- reasoning loop
- MCP / tool use
- subagents
- adaptive execution
- native session transcript

目前 implementation：

```text
ClaudeAgentExecutor
```

---

## Celery / Redis

只負責：

- internal job delivery
- backpressure
- worker concurrency

不是 durable workflow source of truth。

---

# 6. Persistence 一覽

| 問題 | 元件 / 儲存 |
|---|---|
| UI conversation 對應哪個 thread？ | `app_sessions` |
| Internal background job 狀態？ | `app_tasks` |
| A2A protocol Task 狀態？ | `a2a_tasks` |
| LangGraph workflow 執行到哪？ | `AsyncPostgresSaver` |
| 跨 thread 的 long-term memory？ | `AsyncPostgresStore` |
| Claude SDK native transcript？ | `PostgresClaudeSessionStore` |
| 哪個 worker 要收到 job？ | Redis + Celery |
| 真正 business/domain data？ | Domain DB / MCP services |

最重要的區分：

```text
Checkpoint != Long-term Memory != SDK Session != Queue
```

---

# 7. ID Mapping

| ID | 層級 | 意義 |
|---|---|---|
| `user_id` | Application | identity / memory namespace |
| `thread_id` | LangGraph | durable checkpoint identity |
| `sdk_session_id` | Agent SDK | native session resume |
| `A2A contextId` | A2A | Agent conversation context |
| `A2A taskId` | A2A | protocol task |
| `app_tasks.id` | Internal | background job |
| `celery_job_id` | Queue | delivery tracking |

推薦：

```text
A2A contextId -> LangGraph thread_id
```

不要把 `taskId`、`thread_id`、`sdk_session_id` 全部強迫設成同一個 ID。

---

# 8. Project Layout

```text
src/agent_runtime/
├── a2a/
│   ├── executor.py       # A2A Message -> shared runtime
│   └── server.py         # Agent Card + official A2A routes
├── agent_sdk/
│   ├── base.py           # Agent abstraction
│   ├── claude.py         # Claude Agent SDK adapter
│   └── mock.py
├── api/
│   ├── main.py           # FastAPI host + A2A wiring + internal REST
│   └── schemas.py
├── graph/
│   ├── builder.py        # LangGraph topology
│   └── state.py
├── persistence/
│   ├── claude_session_store.py
│   └── langgraph.py
├── tasks/
│   ├── celery_app.py
│   └── jobs.py
├── config.py
├── db.py
├── runtime.py            # dependency composition root
└── service.py            # protocol-neutral execution boundary
```

---

# 9. 本機啟動

需求：

- Docker
- Anthropic API key

```bash
cp .env.example .env
```

設定：

```env
ANTHROPIC_API_KEY=...
```

啟動：

```bash
docker compose up --build
```

確認：

```text
http://localhost:8000/docs
http://localhost:8000/health
http://localhost:8000/.well-known/agent-card.json
http://localhost:8000/a2a
```

`A2A_PUBLIC_URL` 是 Agent Card 對其他 Agent 宣告的實際 endpoint：

```env
A2A_PUBLIC_URL=http://localhost:8000/a2a
```

Kubernetes deployment 時必須改成 ingress/gateway 真正可達的 URL。

---

# 10. Internal REST Demo

建立 session：

```bash
curl -s http://localhost:8000/internal/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"user_id":"demo-user"}'
```

取得 `thread_id` 後建立 background job：

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

---

# 11. 要新增功能時放哪裡？

```text
Agent <-> Agent
    -> A2A

Agent -> Tool / Data / Service
    -> MCP / Tool

Deterministic workflow step
    -> LangGraph node / service

UI / Admin operation
    -> /internal/v1/*

Background job
    -> Celery

Durable workflow state
    -> PostgresSaver

Cross-thread memory
    -> PostgresStore
```

## 不要這樣做

```text
Agent A
  -> custom POST /agent-b
  -> Agent B
```

然後就稱它是 A2A。

也不要：

```text
LangChain Agent
  -> LangGraph Agent
  -> Agent SDK Agent
```

同時堆很多層 autonomous loop。

推薦只有一個主要 reasoning runtime，LangGraph 負責外層 orchestration。

---

# 12. Production 前必補

此 repo 是 **reference template**，不是完成的 production platform。

至少需要補：

- authentication / authorization
- A2A caller identity / tenant mapping
- DB migrations
- per-thread concurrency control / distributed lock
- outbox pattern
- idempotent side-effect tools
- deadline / timeout / cooperative cancellation
- OpenTelemetry
- structured logs
- audit records
- memory extraction / retention policy
- secret management
- data encryption / access control

特別注意：

> checkpoint / queue retry 不等於 exactly-once。

任何有副作用的 tool 都應自己實作 idempotency。

---

# 13. 進一步教學

完整逐步 request flow、Pod crash 恢復、ID mapping、擴充 MCP/A2A/Human Approval 的說明：

👉 **[`docs/TUTORIAL.zh-TW.md`](docs/TUTORIAL.zh-TW.md)**

核心 Python 檔案內也已加入大量繁中註解，建議搭配教學文件逐層閱讀。

---

# 14. Kubernetes

`k8s/` 提供 API / Worker Deployment 範例。

Production 建議 PostgreSQL、Redis 使用 managed/external service。

API / Worker Pod 本身盡量 stateless；重要 state 都存到 Pod 外：

```text
A2A Task        -> PostgreSQL
LangGraph state -> PostgreSQL
Long memory     -> PostgreSQL
SDK session     -> PostgreSQL
Internal task   -> PostgreSQL
Queue delivery  -> Redis
```

因此 Pod restart/reschedule 不需要 sticky Pod 才能保留核心狀態。

---

# 15. Versions

Scaffolded in July 2026 against the then-current major lines:

- LangGraph 1.2.x
- `langgraph-checkpoint-postgres` 3.1.x
- Claude Agent SDK 0.2.x
- official A2A Python SDK 1.1.x
