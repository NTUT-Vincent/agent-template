# Agent Runtime Platform Template

給公司內部 Agent 專案使用的 **Kubernetes-ready Agent Runtime 範本**。

這個 repository 的目標不是只示範「怎麼 call LLM」，而是提供一套可以延伸成正式 Agent 平台的工程骨架：

- A2A Agent-to-Agent 標準介面
- LangGraph durable workflow / checkpoint
- PostgreSQL long-term memory
- Claude Agent SDK reasoning / MCP / subagents
- Claude Agent SDK 官方 SessionStore 跨 Pod session recovery
- Celery / Redis background job
- FastAPI application API
- Kubernetes stateless deployment

> 核心原則：**A2A 是 Agent 對 Agent 的標準協定；Internal REST 是產品自己的 API；LangGraph 管 workflow；Claude Agent SDK 管 agent loop；SessionStore 管 Claude native transcript。**

---

## 第一次看這個 Repo？

建議依序閱讀：

1. 本 README：先建立整體心智模型
2. [`docs/TUTORIAL.zh-TW.md`](docs/TUTORIAL.zh-TW.md)：完整 runtime / A2A / LangGraph 教學
3. [`docs/CLAUDE_SESSION_STORE.zh-TW.md`](docs/CLAUDE_SESSION_STORE.zh-TW.md)：Claude SessionStore 與跨 Pod recovery 專題
4. `src/agent_runtime/api/main.py`
5. `src/agent_runtime/a2a/server.py`
6. `src/agent_runtime/a2a/executor.py`
7. `src/agent_runtime/service.py`
8. `src/agent_runtime/graph/builder.py`
9. `src/agent_runtime/runtime.py`
10. `src/agent_runtime/agent_sdk/claude.py`
11. `src/agent_runtime/persistence/claude_session_store.py`
12. `src/agent_runtime/persistence/langgraph.py`
13. `src/agent_runtime/tasks/*`
14. `src/agent_runtime/db.py`

核心 Python 檔案都有繁中教學註解，可以直接沿 call chain 往下讀。

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
                                |
                        RuntimeA2AExecutor
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
|             ClaudeAgentExecutor                       |
|                      |                                |
|         +------------+-------------+                  |
|         |                          |                  |
|         v                          v                  |
|  Claude Agent SDK        PostgresClaudeSessionStore   |
|  reasoning/MCP              native transcript         |
+-------------------------------------------------------+
```

另外還有一個很重要的 application mapping：

```text
app_sessions
thread_id <-> sdk_session_id
```

它讓下一顆 Pod 知道同一條 LangGraph thread 要 resume 哪一份 Claude native session。

---

# 2. A2A 在哪裡跑起來？

整個 API container 只啟動一個 Uvicorn/FastAPI process：

```text
uvicorn agent_runtime.api.main:app
```

FastAPI startup：

```text
src/agent_runtime/api/main.py
  -> lifespan()
  -> RuntimeContainer.start()
  -> RuntimeA2AExecutor(runtime.service.run_prompt)
  -> add_a2a_routes(...)
```

`a2a/server.py` 再使用官方 SDK：

```text
add_a2a_routes_to_fastapi(...)
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

A2A 不需要另一個 port 或另一個 server process。

---

# 3. A2A 在哪裡接到真正 Agent？

完整 call chain：

```text
POST /a2a
   -> Official A2A SDK
   -> RuntimeA2AExecutor.execute()
   -> AgentRuntimeService.run_prompt()
   -> LangGraph.ainvoke()
   -> graph node: agent_sdk
   -> ClaudeAgentExecutor.run()
   -> claude_agent_sdk.query()
```

三個主要 bridge：

```python
# A2A -> Application runtime
RuntimeA2AExecutor(runtime.service.run_prompt)

# Application runtime -> LangGraph
await self._graph.ainvoke(...)

# LangGraph -> Agent SDK
await agent.run(...)
```

A2A 只是 interoperability protocol；真正 autonomous reasoning / MCP / subagent execution 發生在 Claude Agent SDK。

---

# 4. API 邊界

| Endpoint | Contract | 用途 |
|---|---|---|
| `GET /.well-known/agent-card.json` | A2A | Agent discovery / capabilities |
| `POST /a2a` | A2A JSON-RPC | Agent-to-Agent message/task/artifact |
| `GET /health` | Operations | Kubernetes probe |
| `GET /docs` | FastAPI | OpenAPI / 開發文件 |
| `POST /internal/v1/sessions` | Internal REST | 建立產品 conversation/thread |
| `POST /internal/v1/tasks` | Internal REST | 建立 Celery background job |
| `GET /internal/v1/tasks/{id}` | Internal REST | 查 internal job 狀態 |

原則：

```text
Agent -> Agent
    => A2A

UI / Admin / 公司服務 -> application operation
    => /internal/v1/*

Agent -> Tool / DB / Domain service
    => MCP / Tool API
```

不要把所有 HTTP endpoint 都包裝成 A2A。

---

# 5. 各層責任

## A2A SDK

負責：

- Agent Card
- Message / Task / Artifact
- protocol task lifecycle
- JSON-RPC transport
- streaming / cancellation

A2A task state 使用官方 `DatabaseTaskStore` 寫 PostgreSQL `a2a_tasks`。

## AgentRuntimeService

所有入口的 protocol-neutral execution boundary：

```text
A2A -------------------+
                       |
Internal REST/Celery --+--> AgentRuntimeService.run_prompt()
```

它負責 application session mapping、把 request 送進 LangGraph，以及 early Claude session identity persistence。

## LangGraph

負責：

- durable `thread_id`
- workflow state
- checkpoint / resume
- deterministic routing
- human-in-the-loop boundary
- long-term memory retrieval / write policy

目前 graph：

```text
START
  -> load_memory
  -> agent_sdk
  -> save_memory
  -> END
```

## Claude Agent SDK

負責：

- reasoning loop
- MCP / tool use
- subagents
- adaptive execution
- native session transcript

## Celery / Redis

只負責 internal background job delivery、backpressure、worker concurrency。

Redis **不是** workflow source of truth。

---

# 6. Persistence 一覽

| 問題 | Owner / Storage |
|---|---|
| Application conversation 對應哪個 SDK session？ | `app_sessions` |
| Internal job lifecycle？ | `app_tasks` |
| A2A protocol Task lifecycle？ | `a2a_tasks` |
| LangGraph workflow 執行到哪？ | `AsyncPostgresSaver` |
| 跨 thread long-term memory？ | `AsyncPostgresStore` |
| Claude SDK native transcript / subagents？ | `PostgresClaudeSessionStore` |
| Queue delivery / backpressure？ | Redis + Celery |
| Business/domain data？ | Domain DB / MCP services |

請記住：

```text
Checkpoint
!= Long-term Memory
!= Claude Session transcript
!= A2A Task
!= Celery Job
```

---

# 7. Claude SessionStore：跨 Pod recovery

Claude Agent SDK 的 native transcript 不能只依賴 Pod local filesystem。

本範本使用官方 SessionStore contract：

```text
Claude SDK local JSONL
        |
        | SessionStore.append()
        v
PostgresClaudeSessionStore
        |
        v
PostgreSQL
```

SessionStore 是 **mirror**，不是 transaction log。`session_store_flush="eager"` 用來縮短 external mirror 落後 local transcript 的窗口，但不能提供 exactly-once。

## 第一輪

```text
thread_id = thread-001
sdk_session_id = NULL

Claude query()
   -> SystemMessage(subtype="init")
   -> data["session_id"] = sdk-abc
   -> 立即 COMMIT app_sessions
   -> SessionStore eager mirror
   -> Agent 繼續執行
```

我們**不再只等 ResultMessage** 才保存 `sdk_session_id`。

這是為了縮小以下 crash window：

```text
transcript 已 mirror
Pod crash
但 app_sessions 還不知道 session id
```

## 下一顆 Pod

```text
thread_id
   -> app_sessions.sdk_session_id
   -> ClaudeAgentOptions(resume=sdk_session_id)
   -> SessionStore.load()
   -> PostgreSQL transcript
   -> restore Claude native session
```

專題文件：[`docs/CLAUDE_SESSION_STORE.zh-TW.md`](docs/CLAUDE_SESSION_STORE.zh-TW.md)。

---

# 8. SessionStore adapter 的可靠性規則

`PostgresClaudeSessionStore` 目前實作：

- `append()`
- `load()`
- `list_sessions()`
- `list_subkeys()`
- `delete()`
- `list_session_summaries()`
- `fold_session_summary()` sidecar

Transcript row 包含：

```text
project_key
session_id
subpath
seq
entry_uuid
entry JSONB
mtime
```

因為 SessionStore mirror retry 可能重送相同 batch，所以用 `entry.uuid` 做 dedupe：

```text
UNIQUE(project_key, session_id, subpath, entry_uuid)
```

同一 session/subpath append 使用 PostgreSQL advisory transaction lock，保護 append order 與 summary fold。

Subagent transcript 使用 `subpath` 保存，SDK 可透過 `list_subkeys()` 還原。

如果 external mirror 最終失敗，Agent SDK 可能繼續執行並 emit `mirror_error`；`ClaudeAgentExecutor` 會記錄 error 與 `mirror_error_count`。Production 應把它接到 OTel / metrics / alert。

---

# 9. ID Mapping

| ID | 層級 | 意義 |
|---|---|---|
| `user_id` | Application | identity / memory namespace |
| `thread_id` | LangGraph | durable workflow/checkpoint identity |
| `sdk_session_id` | Claude Agent SDK | native session resume identity |
| `A2A contextId` | A2A | Agent conversation context |
| `A2A taskId` | A2A | protocol task |
| `app_tasks.id` | Internal | background job |
| `celery_job_id` | Queue | delivery tracking |

推薦：

```text
A2A contextId -> LangGraph thread_id
```

並另外保存：

```text
LangGraph thread_id <-> Claude sdk_session_id
```

不要把 `taskId`、`thread_id`、`sdk_session_id` 強迫設成同一個 ID。

---

# 10. Memory 與 Session 不要同步成同一份資料

```text
LangGraph PostgresStore
    -> long-term facts / preferences / decisions

Claude SessionStore
    -> native conversation / tool transcript

LangGraph PostgresSaver
    -> workflow checkpoint
```

執行時是：

```text
PostgresStore
  -> retrieve relevant memory
  -> memory_context
  -> Claude Agent SDK
```

Agent 結果再經過 memory extraction / policy 寫回 PostgresStore。

不要把完整 Claude transcript 又複製一份到 long-term memory；也不要把 LangGraph checkpoint 當 chat history。

---

# 11. Pod crash 後哪些資料會留下？

```text
A2A task state        -> PostgreSQL
app_sessions mapping  -> PostgreSQL
LangGraph checkpoint  -> PostgreSQL
LangGraph memory      -> PostgreSQL
Claude transcript     -> PostgreSQL SessionStore mirror
Internal job status   -> PostgreSQL
Queue delivery        -> Redis / Celery
```

API/Worker Pod 本身應視為 replaceable executor。

但 durability 不等於 exactly-once：

```text
Agent -> side-effect tool success -> Pod crash -> retry -> tool 可能再執行
```

因此 side-effect tool 必須有：

- idempotency key
- unique constraint / upsert
- execution record
- transaction / outbox
- compensation strategy

---

# 12. CI / Validation

CI 會：

```text
ruff check
pytest
```

並啟動 PostgreSQL service，執行 Claude Agent SDK 官方 SessionStore conformance suite：

```python
from claude_agent_sdk.testing import run_session_store_conformance
```

測試檔：

```text
tests/test_claude_session_store_conformance.py
```

這比只寫幾個自訂 unit tests 更能確認 adapter 是否符合 SDK contract。

---

# 13. 本機啟動

需求：Docker + Anthropic API key。

```bash
cp .env.example .env
# 設定 ANTHROPIC_API_KEY
docker compose up --build
```

確認：

```text
http://localhost:8000/docs
http://localhost:8000/health
http://localhost:8000/.well-known/agent-card.json
http://localhost:8000/a2a
```

`A2A_PUBLIC_URL` 必須是其他 Agent 真正能連到的 endpoint：

```env
A2A_PUBLIC_URL=http://localhost:8000/a2a
```

Kubernetes 時改成實際 ingress/gateway URL。

---

# 14. Project Layout

```text
src/agent_runtime/
├── a2a/
│   ├── executor.py
│   └── server.py
├── agent_sdk/
│   ├── base.py
│   ├── claude.py
│   └── mock.py
├── api/
│   ├── main.py
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
├── config.py
├── db.py
├── runtime.py
└── service.py

docs/
├── TUTORIAL.zh-TW.md
└── CLAUDE_SESSION_STORE.zh-TW.md
```

---

# 15. 新增功能時放哪裡？

```text
另一個 Agent 要呼叫
    -> A2A AgentSkill + AgentExecutor boundary

Agent 要查資料 / 操作 service
    -> MCP / Tool

固定企業流程
    -> LangGraph node / application service

UI / Admin operation
    -> /internal/v1/*

side effect
    -> idempotency + audit + policy
```

LangChain 不需要成為 core runtime；有需要特定 loader/retriever/integration 時再局部使用 ecosystem package。

---

# 16. Production 前仍需補強

這是 reference template，不是完成品。正式上線至少還需要：

- managed DB migrations，取代 startup DDL/setup
- authentication / tenant mapping / RBAC
- per-thread concurrency control
- outbox pattern
- tool idempotency
- deadline / timeout / cooperative cancellation
- OTel / structured logs / metrics / audit
- SessionStore `mirror_error` alert
- checkpoint / transcript encryption 與資料保留政策
- memory extraction / ranking / token budget
- streaming reconnect/resubscription strategy

---

# 17. 最終心智模型

```text
A2A / Internal REST
        |
        v
AgentRuntimeService
        |
        v
LangGraph -------------------- PostgresSaver
   |                           workflow checkpoint
   |
   +-------------------------- PostgresStore
   |                           long-term memory
   |
   v
ClaudeAgentExecutor
        |
        +-------------------- PostgresClaudeSessionStore
        |                      native transcript / subagents
        |
        v
Claude Agent SDK
        |
        v
MCP / Tools / Subagents

app_sessions:
LangGraph thread_id <-> Claude sdk_session_id
```

**Pod 是執行者，不是 state owner。**
