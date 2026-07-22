# Agent Runtime Platform — 完整中文導讀

這份文件是給第一次接觸此範本的同事看的完整架構教學。

先讀：

1. `README.md`
2. 本文件
3. `docs/CLAUDE_SESSION_STORE.zh-TW.md`

其中第三份專門說明 Claude Agent SDK SessionStore 與 Kubernetes 跨 Pod recovery。

---

# 1. 先建立正確心智模型

本專案不是「一個很大的 Agent」，而是一個 Agent Runtime Platform 範本：

```text
A2A / Internal REST
        |
        v
AgentRuntimeService
        |
        v
LangGraph
        |
        v
Claude Agent SDK
        |
        v
MCP / Tools / Subagents
```

周圍還有不同 persistence owner：

```text
PostgreSQL
├── app_sessions
├── app_tasks
├── a2a_tasks
├── LangGraph checkpoint tables
├── LangGraph Store tables
├── Claude SessionStore transcript
└── Claude SessionStore summaries

Redis
└── Celery delivery queue
```

最重要的原則：

- A2A 不是 Agent runtime。
- LangGraph 不是 queue。
- Claude SessionStore 不是 LangGraph checkpoint。
- Long-term memory 不是 chat transcript。
- Celery task 不是 A2A Task。
- Pod 不是 state owner。

---

# 2. 各層到底負責什麼？

## 2.1 A2A

A2A 是 Agent-to-Agent interoperability protocol。

負責：

- Agent discovery
- Agent Card
- Message
- Task
- Artifact
- task lifecycle
- streaming
- cancellation

外部標準 endpoint：

```text
GET  /.well-known/agent-card.json
POST /a2a
```

不要把普通的：

```text
POST /agent-b
```

直接稱作 A2A。

---

## 2.2 Internal REST

給：

- UI
- Generative UI
- Admin
- 公司後端
- Operation / Debug tooling

目前：

```text
GET  /health
POST /internal/v1/sessions
POST /internal/v1/tasks
GET  /internal/v1/tasks/{id}
```

判斷方式：

```text
Agent -> Agent
    => A2A

UI / Admin / product backend -> application operation
    => Internal REST
```

---

## 2.3 AgentRuntimeService

所有入口共用的 protocol-neutral runtime boundary。

錯誤設計：

```text
A2A -> 一套 Claude runtime
REST -> 第二套 Claude runtime
Celery -> 第三套 Claude runtime
```

這會造成：

- session 規則不一致
- memory 規則不一致
- retry 不一致
- tracing 不一致
- A2A 與 UI 得到不同結果

所以統一成：

```text
A2A -------------------+
                       |
Internal REST/Celery --+--> AgentRuntimeService.run_prompt()
                               |
                               v
                           LangGraph
                               |
                               v
                         Agent SDK
```

`run_prompt()` 也負責：

- `thread_id` 與 application session mapping
- 讀取 `sdk_session_id`
- 建立 early session-id persistence callback
- 進入 LangGraph
- Result 後再做一致性驗證

---

## 2.4 LangGraph

LangGraph 管：

- workflow state
- durable `thread_id`
- checkpoint / resume
- routing
- human approval boundary
- deterministic orchestration
- long-term memory retrieval/write node

目前 graph 很小：

```text
START
  -> load_memory
  -> agent_sdk
  -> save_memory
  -> END
```

正式專案可以擴成：

```text
START
  -> authz
  -> classify
  -> load_context
  -> plan
  -> human_approval
  -> execute
  -> validate
  -> save_memory
  -> END
```

---

## 2.5 Claude Agent SDK

Claude Agent SDK 管：

- reasoning loop
- tool use
- MCP
- subagents
- native transcript
- native session resume

LangGraph 不直接 import Claude-specific type，而是透過：

```text
AgentExecutor
```

抽象呼叫：

```python
await agent.run(...)
```

---

## 2.6 Celery / Redis

Celery / Redis 只管 internal background job delivery：

- queue
- worker dispatch
- concurrency
- backpressure
- redelivery

它不應該保存真正 workflow state。

---

# 3. A2A Request 完整流程

假設另一個 Agent 發送：

```text
「分析 EQP-001 最近異常原因」
```

## Step 1 — 讀 Agent Card

```text
GET /.well-known/agent-card.json
```

Agent Card 在：

```text
src/agent_runtime/a2a/server.py
```

描述：

- Agent 名稱
- capabilities
- skills
- protocol binding
- A2A endpoint

---

## Step 2 — 呼叫 `/a2a`

```text
POST /a2a
```

這個 route 是官方 A2A SDK helper 掛進 FastAPI，不是自己手刻 protocol schema。

啟動鏈：

```text
uvicorn agent_runtime.api.main:app
  -> FastAPI lifespan()
  -> RuntimeContainer.start()
  -> add_a2a_routes(...)
  -> add_a2a_routes_to_fastapi(...)
```

---

## Step 3 — 進 `RuntimeA2AExecutor`

檔案：

```text
src/agent_runtime/a2a/executor.py
```

它負責：

```text
A2A Message
  -> 建立/取得 A2A Task
  -> working
  -> 取得 prompt
  -> 呼叫 shared runtime
  -> result 轉 Artifact
  -> completed
```

A2A protocol Task 的 lifecycle 由 A2A SDK / DatabaseTaskStore 管。

---

## Step 4 — A2A contextId 對應 LangGraph thread_id

```text
A2A contextId -> LangGraph thread_id
```

不要使用：

```text
A2A taskId = thread_id
```

因為同一個 conversation context 可以包含多個 protocol task。

---

## Step 5 — A2A 呼叫 Service

初始化時：

```python
RuntimeA2AExecutor(runtime.service.run_prompt)
```

所以 request 進入：

```text
AgentRuntimeService.run_prompt()
```

---

## Step 6 — Service 準備 session mapping

`app_sessions` 保存：

```text
user_id
thread_id
sdk_session_id
```

第一次：

```text
thread_id = thread-001
sdk_session_id = NULL
```

後續：

```text
thread_id = thread-001
sdk_session_id = sdk-abc
```

這張 table 不保存 transcript，也不保存 graph state；只保存 application mapping。

---

## Step 7 — Service 建立 early session callback

第一輪 Claude native session 是 SDK 建立的，因此 application 一開始不知道 `sdk_session_id`。

不能等整輪完成才保存。

因此 Service 建立：

```text
persist_session_id_early(session_id)
```

並放進 `GraphContext`：

```text
GraphContext(
    user_id=...,
    on_sdk_session_id=callback,
)
```

Callback 不放進 `AgentState`，因為 callback 不應被 LangGraph checkpoint 序列化。

---

## Step 8 — Service 進 LangGraph

```python
await self._graph.ainvoke(
    ...,
    config={"configurable": {"thread_id": thread_id}},
    context=GraphContext(...),
)
```

`configurable.thread_id` 是 LangGraph checkpointer 辨識 durable thread 的 key。

---

## Step 9 — load_memory

LangGraph `PostgresStore` 查 long-term memory：

```text
("memories", user_id)
```

內容應該是：

- user preference
- project fact
- accepted decision
- domain knowledge

不是完整 chat transcript。

---

## Step 10 — agent_sdk node

真正從 LangGraph 接到 Agent 的地方：

```python
result = await agent.run(
    RunRequest(...),
    memory_context=...,
    on_session_id=runtime.context.on_sdk_session_id,
)
```

Graph 不知道實體是 Claude、OpenAI 或其他 runtime。

---

## Step 11 — ClaudeAgentExecutor

檔案：

```text
src/agent_runtime/agent_sdk/claude.py
```

核心 options：

```python
ClaudeAgentOptions(
    resume=request.sdk_session_id,
    session_store=self._session_store,
    session_store_flush="eager",
)
```

### 第一輪

```text
resume=None
```

SDK 建立新 native session。

### 後續輪次

```text
resume=sdk_session_id
```

SDK 會透過 SessionStore load 外部 transcript，讓新的 Pod 繼續先前 session。

---

## Step 12 — init SystemMessage 立即保存 session ID

真正關鍵的 crash-recovery 流程：

```text
query()
  -> SystemMessage(subtype="init")
  -> data["session_id"] = sdk-abc
  -> on_session_id("sdk-abc")
  -> AgentRuntimeService
  -> COMMIT app_sessions
  -> Agent 繼續
```

這樣即使 Agent 中途執行很多 tool 後 Pod crash，application 已經知道下一顆 Pod 要 resume 哪一份 transcript。

不要只依賴最後的 `ResultMessage.session_id`。

---

## Step 13 — SessionStore eager mirror

Claude SDK native transcript 原本會寫 local JSONL。

另外透過：

```text
SessionStore.append()
```

mirror 到：

```text
PostgresClaudeSessionStore -> PostgreSQL
```

SessionStore 是 mirror，不是 transaction log。

`eager` 的目的是更積極 flush，縮小 external store 落後 local transcript 的 crash window。

---

## Step 14 — ResultMessage

SDK 完成後回：

```text
ResultMessage
```

adapter 轉成 framework neutral：

```text
AgentResult
├── text
├── sdk_session_id
└── metadata
```

Service 會再用 `sdk_session_id` 做一次 idempotent consistency check。

---

## Step 15 — 回 A2A Artifact

```text
Claude Agent SDK
  -> AgentResult
  -> LangGraph state
  -> AgentRuntimeService
  -> RuntimeA2AExecutor
  -> Artifact
  -> A2A Task completed
```

---

# 4. Internal Background Job 流程

```text
POST /internal/v1/tasks
```

## Step 1 — 先寫 PostgreSQL

```text
app_tasks
status = queued
```

PostgreSQL 是 application job lifecycle source of truth。

## Step 2 — enqueue Celery

```text
celery_app.send_task(task_id)
```

Queue message 只需要 task id，不塞完整 workflow state。

## Step 3 — Worker 取 job

```text
Redis
  -> Celery Worker
  -> run_task(task_id)
```

## Step 4 — Worker 建 RuntimeContainer

Worker 與 API 使用同一套：

- PostgreSQL
- LangGraph persistence
- Claude Agent SDK
- SessionStore
- AgentRuntimeService

## Step 5 — 最後仍然進 `run_prompt()`

```text
run_task(task_id)
  -> AgentRuntimeService.run_task()
  -> AgentRuntimeService.run_prompt()
  -> LangGraph
  -> Agent SDK
```

所以 A2A 與 Celery 的 lifecycle 不同，但 runtime 相同。

---

# 5. Persistence 不要混

## 5.1 `app_sessions`

用途：application identity mapping。

```text
user_id
thread_id
sdk_session_id
```

回答：

> 這條 LangGraph thread 應該 resume 哪一個 Claude SDK session？

---

## 5.2 LangGraph `AsyncPostgresSaver`

用途：workflow checkpoint。

回答：

> 這條 thread 的 graph state 是什麼？現在執行到哪？

---

## 5.3 LangGraph `AsyncPostgresStore`

用途：cross-thread long-term memory。

回答：

> 哪些資訊值得跨 conversation 保留？

例如：

- user preference
- project facts
- machine alias
- accepted decision

---

## 5.4 `PostgresClaudeSessionStore`

用途：Claude Agent SDK native transcript。

回答：

> Claude native session 要 resume 時，conversation/tool/subagent transcript 在哪？

主要欄位：

```text
project_key
session_id
subpath
seq
entry_uuid
entry JSONB
mtime
```

---

## 5.5 A2A `DatabaseTaskStore`

用途：protocol-visible A2A Task lifecycle。

```text
a2a_tasks
```

它不是 internal `app_tasks`。

---

## 5.6 `app_tasks`

用途：internal product background job lifecycle。

```text
queued
running
succeeded
failed
```

---

# 6. SessionStore adapter 為什麼需要這些細節？

專題：[`CLAUDE_SESSION_STORE.zh-TW.md`](CLAUDE_SESSION_STORE.zh-TW.md)

## append 必須 idempotent

SDK mirror retry 可能重送相同 entries。

因此用：

```text
entry.uuid
```

做 unique dedupe：

```text
(project_key, session_id, subpath, entry_uuid)
```

## subpath

Subagent transcript 不和 main transcript 混在一起：

```text
main
  subpath=""

subagent
  subpath="subagents/agent-..."
```

`list_subkeys()` 讓 SDK resume subagent transcripts。

## session summary

實作：

```text
fold_session_summary()
list_session_summaries()
```

這是 SDK-owned session metadata sidecar，不是 application memory。

## mirror_error

External store append 最終失敗時，Agent 可以繼續執行並產生 `mirror_error`。

Template 會：

```text
log error
increment mirror_error_count
```

Production 應接 metrics / alert。

---

# 7. Pod crash recovery

## 情境 A：API Pod 在 request 間重啟

外部 state 都還在：

```text
app_sessions          -> PostgreSQL
LangGraph checkpoint  -> PostgreSQL
Claude transcript     -> PostgreSQL SessionStore
A2A task              -> PostgreSQL
```

下一顆 Pod 直接讀回。

## 情境 B：Claude 執行中 Pod crash

理想時間線：

```text
Claude init
  -> sdk_session_id
  -> COMMIT app_sessions
  -> eager mirror transcript
  -> tool/reasoning...
  -> Pod crash
```

下一顆 Pod：

```text
thread_id
  -> app_sessions.sdk_session_id
  -> resume=sdk_session_id
  -> SessionStore.load()
  -> restore transcript
```

仍存在一個無法完全消除的小窗口：若 Pod 在 SDK 尚未 emit init session ID 前就死亡，application 尚無法知道該新 session identity。這是 SDK 產生 session ID 的 lifecycle 邊界。

## 情境 C：Worker Pod crash

Celery：

```text
task_acks_late=True
task_reject_on_worker_lost=True
```

可以讓 delivery 有機會 redeliver。

但：

```text
redelivery != exactly once
```

---

# 8. Exactly-once 為什麼 SessionStore 解不了？

例如：

```text
Claude
  -> MCP tool 寫 DB 成功
  -> Pod crash
  -> workflow/job retry
  -> tool 再跑一次
```

SessionStore 只解決 conversation resume。

Side-effect tool 仍需要：

- idempotency key
- unique constraint
- upsert
- execution record
- transaction
- outbox
- compensation

---

# 9. Memory 跟 Session 怎麼合作？

不要做全量雙向 transcript 同步。

正確模型：

```text
LangGraph PostgresStore
   -> retrieve relevant long-term memory
   -> memory_context
   -> Claude Agent SDK

Claude SDK SessionStore
   -> native conversation continuity

Agent result
   -> memory extraction/policy
   -> LangGraph PostgresStore
```

也就是：

```text
Memory = application knowledge
Session = native conversation transcript
Checkpoint = workflow state
```

三者不同。

---

# 10. ID 對照

| ID | Owner | 用途 |
|---|---|---|
| `user_id` | Application | identity / memory namespace |
| `thread_id` | LangGraph | durable workflow identity |
| `sdk_session_id` | Claude SDK | native session identity |
| `A2A contextId` | A2A | conversation context |
| `A2A taskId` | A2A | protocol Task |
| `app_tasks.id` | Application | internal background job |
| `celery_job_id` | Celery | delivery tracking |

Mapping：

```text
A2A contextId -> LangGraph thread_id
LangGraph thread_id <-> Claude sdk_session_id
```

不要把全部 ID 合併。

---

# 11. 為什麼 LangGraph 還需要存在？

Claude Agent SDK 很適合：

- reasoning
- MCP
- subagents
- tool exploration

LangGraph 很適合：

- deterministic workflow
- checkpoint
- human approval
- routing
- bounded retry
- workflow state

所以推薦：

```text
LangGraph
├── authz
├── validate
├── approval
├── deterministic nodes
└── agent_sdk node
      -> Claude Agent SDK
```

而不是把整個企業流程都塞進一個巨大 agent loop。

---

# 12. LangChain 要不要用？

不是核心依賴。

目前 core：

```text
FastAPI
A2A SDK
LangGraph
Claude Agent SDK
PostgreSQL
Celery / Redis
MCP
```

LangChain ecosystem 可以在需要特定：

- loader
- splitter
- retriever
- vector-store integration
- utility integration

時局部使用。

不要額外再堆一層 LangChain Agent abstraction。

---

# 13. 新增功能要放哪？

## Agent 使用 Tool

```text
Claude Agent SDK / MCP
```

## Deterministic business workflow

```text
LangGraph node / service
```

## 外部 Agent 呼叫

```text
A2A AgentSkill + AgentExecutor boundary
```

## UI/Admin operation

```text
/internal/v1/*
```

## Human approval

```text
LangGraph interrupt/resume boundary
```

## Generative UI streaming

```text
Agent/LangGraph event
  -> event publisher
  -> SSE/WebSocket
```

但：

```text
SSE/WebSocket = delivery
PostgreSQL/LangGraph = state
```

---

# 14. CI / Validation

CI：

```text
ruff check
pytest
```

SessionStore 另外使用官方 conformance suite：

```python
from claude_agent_sdk.testing import run_session_store_conformance
```

測試真正的 PostgreSQL adapter：

```text
tests/test_claude_session_store_conformance.py
```

驗證：

- append/load order
- unknown session behavior
- multiple append
- subpath isolation
- project isolation
- list sessions
- delete
- list subkeys
- session summaries

---

# 15. Production 前要補什麼？

## Authentication / Authorization

- A2A caller identity
- tenant mapping
- RBAC
- tool permission
- human approval policy

不要相信 client 自己傳的 `user_id`。

## Migration

目前 template 為方便啟動會 setup schema。

Production 改：

```text
Alembic / migration job
```

特別是：

- SessionStore transcript table
- SessionStore summary table
- LangGraph tables
- app tables
- A2A task table

## Concurrency

同一 `thread_id` 不應無限制 concurrent mutation。

考慮：

- PostgreSQL advisory lock
- distributed lock
- workflow concurrency policy

SessionStore 自己也已針對同一 session/subpath append 使用 advisory transaction lock。

## Outbox

目前：

```text
DB commit
  -> Celery send_task
```

中間仍有 crash window。

正式環境可加 outbox。

## Observability

至少追：

```text
trace_id
thread_id
sdk_session_id
A2A task_id
app_task_id
mirror_error_count
```

並接：

- OpenTelemetry
- structured logging
- metrics
- alert
- audit records

## Security / Retention

Transcript / checkpoint 可能包含敏感資料。

需定義：

- encryption
- row/tenant isolation
- retention
- deletion policy
- audit

---

# 16. 建議程式碼閱讀順序

```text
1. README.md
2. docs/TUTORIAL.zh-TW.md
3. docs/CLAUDE_SESSION_STORE.zh-TW.md
4. src/agent_runtime/api/main.py
5. src/agent_runtime/a2a/server.py
6. src/agent_runtime/a2a/executor.py
7. src/agent_runtime/service.py
8. src/agent_runtime/graph/builder.py
9. src/agent_runtime/runtime.py
10. src/agent_runtime/agent_sdk/claude.py
11. src/agent_runtime/persistence/claude_session_store.py
12. src/agent_runtime/persistence/langgraph.py
13. src/agent_runtime/tasks/celery_app.py
14. src/agent_runtime/tasks/jobs.py
15. src/agent_runtime/db.py
```

看完後應該能回答：

1. A2A 在哪裡掛進 FastAPI？
2. A2A 在哪裡接到真正 Agent？
3. LangGraph thread_id 是什麼？
4. Claude sdk_session_id 是什麼？
5. 為什麼 init event 就要保存 sdk_session_id？
6. SessionStore 如何讓新 Pod resume？
7. SessionStore 為什麼需要 entry UUID dedupe？
8. mirror_error 代表什麼？
9. Memory / Checkpoint / Session 有什麼差別？
10. A2A Task / app_tasks / Celery job 有什麼差別？

---

# 17. 最終架構圖

```text
                            Other Agent
                                |
                                | A2A
                                v
                     Official A2A SDK
                                |
                     RuntimeA2AExecutor
                                |
                                v
UI/Admin ----------> AgentRuntimeService <---------- Celery Worker
                           |
                           v
                       LangGraph
              +------------+-------------+
              |                          |
              v                          v
       PostgresSaver               PostgresStore
       workflow state              long memory
              |
              v
        agent_sdk node
              |
              v
     ClaudeAgentExecutor
              |
       +------+-------------------+
       |                          |
       v                          v
Claude Agent SDK         PostgresClaudeSessionStore
reasoning/MCP            transcript/subagents/summary
       |                          |
       +-------------+------------+
                     |
                     v
                 PostgreSQL

app_sessions:
thread_id <-> sdk_session_id
```

**Pod 只是可替換的 executor；durable identity 與 state 都必須在 Pod 外。**
