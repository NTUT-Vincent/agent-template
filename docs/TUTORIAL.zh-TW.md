# Agent Runtime Platform — 完整中文導讀

這份文件不是 API reference，而是給第一次接觸此範本的同事看的「架構教學」。

建議不要一開始就逐檔案讀程式碼。先建立下面這個心智模型：

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
Agent SDK
        |
        v
MCP / Tools / Subagents
```

周圍再接上：

```text
PostgreSQL
├── app_sessions
├── app_tasks
├── a2a_tasks
├── LangGraph checkpoints
├── LangGraph Store
└── Claude SessionStore

Redis
└── Celery queue
```

---

# 1. 這個範本到底在解什麼問題？

單純做一個 Agent 很容易：

```python
response = call_llm(prompt)
```

但企業環境真正困難的是：

- Pod 重啟後 session 還在嗎？
- 長任務做到一半 worker 掛掉怎麼辦？
- 多輪對話要怎麼接續？
- Agent 的 native transcript 存哪裡？
- Long-term memory 和 chat history 是同一件事嗎？
- Agent A 要怎麼標準化呼叫 Agent B？
- Background job 和 A2A Task 是不是同一種 Task？
- Redis 掛掉後 workflow state 會不會一起消失？
- 多個 API Pod 如何共享狀態？
- 哪些流程應該交給 Agent 自主決定，哪些應該由 deterministic workflow 控制？

因此這個範本不是「聊天機器人範本」，而是：

> 一個可以部署到 Kubernetes、具備 durable state、A2A、queue、memory、session 的 Agent Runtime 起始架構。

---

# 2. 最重要的分層

## 2.1 A2A 是 Agent-to-Agent 協定層

A2A 負責：

- Agent discovery
- Agent Card
- Message
- Task
- Artifact
- task lifecycle
- streaming
- cancellation

本範本使用官方 A2A Python SDK。

外部標準介面：

```text
GET  /.well-known/agent-card.json
POST /a2a
```

請不要自己做：

```text
POST /agent-b
```

然後就稱為 A2A。

A2A 的價值就是讓不同 Agent 遵循相同的 discovery、message、task、artifact contract。

---

## 2.2 Internal REST 是產品自己的 API

Internal REST 給：

- Generative UI
- Admin Dashboard
- 公司後端服務
- Debug / Operation

目前範例：

```text
GET  /health
POST /internal/v1/sessions
POST /internal/v1/tasks
GET  /internal/v1/tasks/{id}
```

這些 API 不需要硬套 A2A。

判斷方式：

```text
Agent 呼叫 Agent？
    -> A2A

UI / Admin / 公司程式呼叫產品功能？
    -> Internal REST
```

---

## 2.3 AgentRuntimeService 是所有入口的共用執行邊界

這一層非常重要。

錯誤做法：

```text
A2A -> Claude Agent SDK
REST -> 另一套 Claude Agent SDK
Celery -> 第三套 Agent 邏輯
```

最後會出現：

- 三套 session 管理
- 三套 memory
- 三套 retry
- 行為不一致

本範本改成：

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

所以所有入口共用同一套 durable runtime。

---

# 3. A2A Request 完整流程

假設另一個 Agent 發送：

```text
「分析 EQP-001 最近的異常原因」
```

完整流程如下。

## Step 1 — A2A client 先讀 Agent Card

```text
GET /.well-known/agent-card.json
```

Agent Card 會告訴對方：

- Agent 名稱
- capabilities
- skills
- protocol binding
- A2A endpoint

這些資料在：

```text
src/agent_runtime/a2a/server.py
```

建立。

---

## Step 2 — Client 呼叫 `/a2a`

```text
POST /a2a
```

這個 endpoint 是官方 A2A SDK route，不是我們自己手刻的 request schema。

FastAPI 啟動時：

```text
api/main.py
  -> lifespan()
  -> add_a2a_routes(...)
```

然後：

```text
a2a/server.py
  -> add_a2a_routes_to_fastapi(...)
```

正式把 A2A route 加到 FastAPI。

---

## Step 3 — 官方 A2A SDK 進入 `RuntimeA2AExecutor`

```text
src/agent_runtime/a2a/executor.py
```

核心 method：

```python
async def execute(...)
```

它負責：

```text
A2A Message
  -> 建立 / 取得 A2A Task
  -> Task = working
  -> 取得 prompt
  -> 呼叫 shared runtime
  -> 建立 Artifact
  -> Task = completed
```

---

## Step 4 — A2A contextId 對應 LangGraph thread_id

這是非常重要的 ID mapping。

A2A 有：

```text
contextId
    一段對話 / 工作上下文

taskId
    某一次具體工作
```

LangGraph 有：

```text
thread_id
    durable workflow / checkpoint identity
```

因此本範本採：

```text
A2A contextId
      |
      v
LangGraph thread_id
```

而不是：

```text
A2A taskId = LangGraph thread_id
```

原因是同一段 A2A conversation 可能產生多個 Task，但仍希望延續同一份 workflow context。

---

## Step 5 — A2A Executor 呼叫 Service

```python
result = await self._run_prompt(...)
```

初始化時實際注入的是：

```python
RuntimeA2AExecutor(runtime.service.run_prompt)
```

所以這裡代表：

```text
A2A
  -> AgentRuntimeService.run_prompt()
```

---

## Step 6 — Service 呼叫 LangGraph

```text
src/agent_runtime/service.py
```

核心：

```python
await self._graph.ainvoke(...)
```

並指定：

```python
config={
    "configurable": {
        "thread_id": thread_id
    }
}
```

這個 `thread_id` 就是 LangGraph checkpointer 用來辨識 durable thread 的 key。

---

## Step 7 — LangGraph load memory

```text
START
  -> load_memory
```

檔案：

```text
src/agent_runtime/graph/builder.py
```

它會從 PostgresStore 找 user-scoped long-term memory。

這不是 chat history。

它更像：

```text
使用者偏好
已確認的 domain facts
過去決策
重要 project context
```

---

## Step 8 — LangGraph 進入 Agent SDK node

Graph：

```text
load_memory
   -> agent_sdk
```

真正接 Agent 的地方：

```python
result = await agent.run(...)
```

因此：

```text
LangGraph
  -> AgentExecutor abstraction
```

---

## Step 9 — Agent implementation 是 ClaudeAgentExecutor

在：

```text
src/agent_runtime/runtime.py
```

組裝：

```python
agent = ClaudeAgentExecutor(...)

graph = build_graph(
    agent=agent,
    ...
)
```

所以 graph 裡 `agent.run()` 實際會呼叫：

```text
ClaudeAgentExecutor.run()
```

---

## Step 10 — 真正進 Claude Agent SDK

檔案：

```text
src/agent_runtime/agent_sdk/claude.py
```

核心：

```python
async for message in query(
    prompt=request.prompt,
    options=options,
):
```

到這裡才真正進 Agent SDK agent loop。

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

---

## Step 11 — Agent SDK 回傳 result

Claude SDK 回傳 `ResultMessage` 後，adapter 轉成：

```text
AgentResult
├── text
├── sdk_session_id
└── metadata
```

SDK-specific type 到這一層就結束。

LangGraph 不需要知道 `ResultMessage` 的細節。

---

## Step 12 — Result 回到 A2A Artifact

回程：

```text
Claude Agent SDK
  -> AgentResult
  -> LangGraph state
  -> Service result
  -> RuntimeA2AExecutor
  -> Artifact
  -> Task completed
  -> A2A client
```

這就是完整 A2A request lifecycle。

---

# 4. Internal Background Job 完整流程

Internal flow 與 A2A 不同。

```text
POST /internal/v1/tasks
```

## Step 1 — 寫入 PostgreSQL

建立：

```text
app_tasks
status = queued
```

先寫 DB，再 enqueue。

因為 PostgreSQL 才是 application job 的 source of truth。

---

## Step 2 — 丟 Celery

```text
celery_app.send_task(task_id)
```

Queue message 只帶 task id。

不把完整 workflow state 塞進 Redis。

---

## Step 3 — Worker 取得 job

```text
src/agent_runtime/tasks/jobs.py
```

```text
Redis
  -> Celery worker
  -> run_task(task_id)
```

---

## Step 4 — Worker 建立 RuntimeContainer

Worker 與 API 使用相同：

```text
RuntimeContainer
```

所以兩邊使用相同的：

- database
- LangGraph persistence
- Agent SDK
- SessionStore
- Service

---

## Step 5 — 最後仍然進 `run_prompt()`

```text
run_task(task_id)
  -> AgentRuntimeService.run_task()
  -> AgentRuntimeService.run_prompt()
  -> LangGraph
  -> Agent SDK
```

因此只有 job lifecycle 不同，真正 Agent runtime 是同一套。

---

# 5. 四種 Persistence 千萬不要混

這是整份架構最重要的地方之一。

## 5.1 `app_sessions`

用途：application mapping。

```text
user_id
thread_id
sdk_session_id
```

它不保存 LangGraph checkpoint。

它只是回答：

> 這個 application conversation 對應哪個 LangGraph thread 和 SDK session？

---

## 5.2 LangGraph `AsyncPostgresSaver`

用途：workflow checkpoint。

回答：

> 這條 thread 的 graph 執行到哪裡？目前 state 是什麼？

例如未來 graph：

```text
parse
  -> analyze
  -> waiting_for_human_approval
  -> execute
```

Pod 在 waiting approval 時重啟，checkpointer 才是恢復 workflow 的關鍵。

---

## 5.3 LangGraph `AsyncPostgresStore`

用途：cross-thread long-term memory。

回答：

> 跨不同 conversation，有哪些資訊值得長期保留？

例如：

```text
user preference
project facts
machine alias
accepted conclusion
```

它不是完整 chat history。

---

## 5.4 `PostgresClaudeSessionStore`

用途：Claude Agent SDK native transcript。

回答：

> Claude SDK 自己要 resume session 時，native transcript 在哪裡？

這個資料不應只存在 Pod local filesystem。

---

## 5.5 A2A `DatabaseTaskStore`

用途：A2A protocol task lifecycle。

```text
a2a_tasks
```

回答：

> 對外這個 A2A Task 現在是 working、completed 還是 canceled？

它不是 internal `app_tasks`。

---

# 6. ID 對照表

建議所有同事先把下面這張表記住。

| ID | 所屬層 | 用途 |
|---|---|---|
| `user_id` | Application | identity / memory namespace |
| `thread_id` | LangGraph | durable workflow/checkpoint identity |
| `sdk_session_id` | Agent SDK | native transcript resume |
| `A2A contextId` | A2A | 多輪 Agent context |
| `A2A taskId` | A2A | 某一次 protocol Task |
| `app_tasks.id` | Internal App | 某一次 Celery background job |
| `celery_job_id` | Queue | Celery delivery tracking |

推薦 mapping：

```text
A2A contextId -> LangGraph thread_id
```

不要把所有 ID 都設成同一個值，只是為了看起來簡單。

---

# 7. Pod 掛掉時會發生什麼？

假設：

```text
API Pod A
Worker Pod B
```

都可能隨時被 Kubernetes 重建。

## API Pod 掛掉

A2A Task：

```text
DatabaseTaskStore -> PostgreSQL
```

仍在。

Application session：

```text
app_sessions -> PostgreSQL
```

仍在。

下一顆 API Pod 可以讀同一份資料。

---

## Worker Pod 執行中掛掉

Celery 設定：

```text
task_acks_late=True
task_reject_on_worker_lost=True
```

可以讓未完成 delivery 有機會重新交付。

但請注意：

> Queue redelivery 不等於 exactly once。

例如：

```text
Agent 呼叫外部 API 成功
  -> Pod 掛掉
  -> checkpoint / job status 還沒更新
  -> job 被重新執行
  -> API 可能被呼叫第二次
```

因此 side-effect tool 一定要設計：

- idempotency key
- unique constraint
- upsert
- execution record
- read-before-write
- transaction / compensation

Persistence 不會自動解決 exactly-once。

---

# 8. 為什麼 LangGraph 要包 Agent SDK？

不是因為 Agent SDK 不夠強。

而是兩者解決不同問題。

Agent SDK 擅長：

- reasoning loop
- tool use
- MCP
- subagents
- autonomous exploration

LangGraph 擅長：

- deterministic routing
- checkpoint
- resume
- human approval
- workflow state
- bounded retry

所以推薦：

```text
LangGraph
├── deterministic node
├── validation node
├── approval node
└── agent_sdk node
       -> Agent SDK
```

而不是：

```text
LangGraph
  -> 一個巨大 Agent node
       -> 所有流程都在裡面
```

因為 checkpoint 是以 graph execution boundary 為基礎。

Agent node 越巨大，crash 後可能重做的工作越多。

---

# 9. 新增 MCP Tool 要改哪裡？

原則：

> Agent 使用工具 = MCP / Tool layer，不是 A2A。

例如：

```text
查 Oracle
查 Neo4j
讀 OKF
呼叫設備 API
```

這些通常是 Agent 的工具。

建議接在：

```text
ClaudeAgentExecutor / Agent SDK configuration
```

不要為每一個 tool 都建立一個 A2A Agent。

只有當它真的具備獨立 Agent 能力、狀態與對外 contract 時才考慮 A2A。

---

# 10. 新增另一個 A2A Agent Skill 要改哪裡？

先看：

```text
src/agent_runtime/a2a/server.py
```

Agent Card 裡新增 / 調整：

```text
AgentSkill
```

但 Skill 只是 discovery metadata。

真正 routing 可以放：

```text
LangGraph classify/router node
```

例如：

```text
A2A Message
  -> classify skill intent
      -> document workflow
      -> equipment workflow
      -> knowledge workflow
```

不要把所有 business logic 寫進 Agent Card 或 A2A server.py。

---

# 11. 新增 Human Approval 要改哪裡？

應該改 LangGraph，而不是 A2A layer。

例如：

```text
analyze
  -> prepare_action
  -> human_approval
  -> execute_side_effect
```

A2A 只是把外部 Task/Message 帶進來。

真正 workflow interrupt/resume 應該由 LangGraph persistence 管。

---

# 12. 新增 Generative UI streaming 要放哪裡？

可以在 Agent SDK / LangGraph event 上增加 event publisher，再由 API 用 SSE/WebSocket 推給 UI。

但必須記住：

```text
SSE/WebSocket = delivery
PostgreSQL/LangGraph = state
```

Client 斷線不能代表 workflow 消失。

重新連線時應該能從 durable state 重新取得真實進度。

---

# 13. Production 前一定要補的東西

這個 repository 是 template，不是完成品。

正式上線至少還需要：

## Authentication / Authorization

- A2A caller identity
- tenant scope
- RBAC
- tool permission
- human approval policy

不要相信 client 自己傳的 `user_id`。

---

## Migration

目前為了 clone 後可直接啟動，使用：

```text
create_all()
setup()
```

正式環境改成：

```text
Alembic / migration job
```

---

## Distributed lock

同一個 `thread_id` 不應該同時被多 worker 無限制寫入。

可考慮：

- PostgreSQL advisory lock
- Redis lock
- workflow-level concurrency policy

---

## Outbox

目前：

```text
DB commit
  -> Celery send_task
```

兩者之間仍可能 crash。

正式環境可以加入 outbox pattern，避免：

```text
DB 有 job
但 queue 沒收到
```

或相反。

---

## Observability

至少要有：

```text
trace_id
thread_id
A2A task_id
app_task_id
sdk_session_id
```

並串：

- OpenTelemetry
- structured log
- metrics
- audit records

---

# 14. 建議程式碼閱讀順序

第一次 clone 下來，推薦照這個順序：

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

看完之後應該能回答：

1. A2A 在哪裡掛進 FastAPI？
2. A2A 在哪裡接到真正 Agent？
3. LangGraph thread_id 存在哪裡？
4. Claude native session 存在哪裡？
5. Long-term memory 和 checkpoint 差在哪？
6. Celery 為什麼不是 source of truth？
7. A2A Task 和 app_tasks 差在哪？
8. Pod 重啟後哪些 state 會留下？

如果這八題能回答，代表已經理解這個 template 的核心架構。

---

# 15. 一張圖總結

```text
                           Other Agent
                               |
                               | A2A 1.0
                               v
                /.well-known/agent-card.json
                           POST /a2a
                               |
                               v
                    Official A2A SDK
                               |
                    RuntimeA2AExecutor
                               |
                               +----------------------+
                                                      |
UI / Admin                                             |
    |                                                  |
    | /internal/v1/*                                   |
    v                                                  |
FastAPI                                                |
    |                                                  |
    +--> app_tasks -> Redis/Celery -> Worker ----------+
                                                      |
                                                      v
                                         AgentRuntimeService
                                                      |
                                                      v
                                                 LangGraph
                                      +---------------+---------------+
                                      |                               |
                                      v                               v
                              PostgresSaver                    PostgresStore
                              checkpoint                       long memory
                                      |
                                      v
                                agent_sdk node
                                      |
                                      v
                            ClaudeAgentExecutor
                                      |
                         PostgresClaudeSessionStore
                                      |
                                      v
                          claude_agent_sdk.query()
                                      |
                                      v
                              MCP / Tools / LLM
```

這就是本範本的核心。
