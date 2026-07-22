# Agent Runtime Platform：繁體中文導讀

這份文件是給第一次接觸本專案的同事看的閱讀地圖。目標不是只教你「怎麼把服務跑起來」，而是讓你理解：

1. A2A、LangGraph、Agent SDK、Memory、Session、Task Queue 各自負責什麼。
2. 一個 A2A 請求如何一路走到真正的 Agent。
3. Pod 重啟後，哪些狀態會保存，哪些不應該依賴記憶體。
4. 未來要換模型、加工具、加工作流時，應該修改哪一層。

---

## 1. 先建立正確心智模型

本專案不是「一支很大的 Agent」。它是一個 Agent Runtime Platform 範本。

```text
A2A / Internal REST
        ↓
Protocol Adapter
        ↓
AgentRuntimeService
        ↓
LangGraph
        ↓
Agent SDK
        ↓
LLM / MCP / Tool / Subagent
```

每一層只負責一件事情：

| 層級 | 責任 |
|---|---|
| A2A | Agent 與 Agent 之間的標準互通協定 |
| Internal REST | UI、管理後台、產品內部 API |
| AgentRuntimeService | 不依賴協定的共用執行入口 |
| LangGraph | 流程、節點、checkpoint、resume、memory |
| Agent SDK | Agent reasoning loop、工具、MCP、subagent |
| Celery / Redis | 內部背景工作的派送與 backpressure |
| PostgreSQL | 真正的持久化來源 |

請特別注意：

- A2A 不是模型，也不是 Agent SDK。
- LangGraph 不是 queue。
- Redis 不是 workflow state source of truth。
- Session 不等於 Task，也不等於 Run。
- Checkpoint 不保證外部副作用 exactly once。

---

## 2. 一個 A2A 請求如何接到 Agent

### 2.1 啟動階段

Docker / Kubernetes 最後執行：

```bash
uvicorn agent_runtime.api.main:app --host 0.0.0.0 --port 8000
```

FastAPI 啟動時會執行 `lifespan()`：

```text
RuntimeContainer.start()
    ├── 建立 PostgreSQL engine
    ├── 建立 Claude SessionStore
    ├── 啟動 LangGraph PostgresSaver / PostgresStore
    ├── 建立 ClaudeAgentExecutor
    ├── build_graph(agent=ClaudeAgentExecutor)
    └── 建立 AgentRuntimeService
```

接著建立：

```text
RuntimeA2AExecutor(runtime.service.run_prompt)
```

這一行是 A2A 與共用 runtime 的接點。

### 2.2 請求階段

```text
POST /a2a
   ↓
Official A2A SDK DefaultRequestHandler
   ↓
RuntimeA2AExecutor.execute()
   ↓
AgentRuntimeService.run_prompt()
   ↓
LangGraph.ainvoke()
   ↓
agent_sdk node
   ↓
ClaudeAgentExecutor.run()
   ↓
claude_agent_sdk.query()
```

真正呼叫 Agent SDK 的位置在：

```text
src/agent_runtime/agent_sdk/claude.py
```

真正把 Agent 放進 LangGraph 的位置在：

```text
src/agent_runtime/runtime.py
```

真正從 LangGraph node 呼叫 Agent 的位置在：

```text
src/agent_runtime/graph/builder.py
```

---

## 3. 為什麼要有 AgentRuntimeService

A2A 與 Internal REST 都可能要執行 Agent，但不應該各自寫一套 agent loop。

錯誤設計：

```text
A2A Executor -> 自己呼叫 Agent SDK
Internal Worker -> 另一套 LangGraph
```

這會造成：

- 兩套 memory 行為不同
- 兩套 session 規則不同
- 兩套 retry / error handling
- A2A 和 UI 得到不一致的結果

本範本統一成：

```text
A2A ----------┐
              ├── AgentRuntimeService.run_prompt()
Internal REST ┘
```

`run_prompt()` 是 protocol-neutral boundary。它不知道呼叫者是 A2A、HTTP、Slack 還是排程。

---

## 4. ID 對應

不要把所有 ID 都混成同一個。

| ID | 意義 |
|---|---|
| `A2A task_id` | 一次 A2A protocol task |
| `A2A context_id` | 多個 A2A message/task 共用的對話脈絡 |
| `thread_id` | LangGraph durable workflow identity |
| `sdk_session_id` | Claude Agent SDK 原生 session |
| `app_task_id` | Internal REST/Celery 背景工作 |
| `celery_job_id` | Redis/Celery delivery job |

本範本採用：

```text
A2A context_id -> LangGraph thread_id
```

原因是 context_id 代表同一段持續對話，而 LangGraph thread_id 正好代表持續 workflow state。

但：

```text
A2A task_id != thread_id
```

因為同一個 context 可以有多個 task。

---

## 5. Persistence 分層

PostgreSQL 裡的不同資料不是重複，而是保存不同層級的狀態。

```text
PostgreSQL
├── app_sessions
│   └── UI / app conversation 到 thread_id / sdk_session_id 的映射
├── app_tasks
│   └── Internal REST/Celery job 狀態
├── a2a_tasks
│   └── A2A protocol Task lifecycle
├── LangGraph checkpoint tables
│   └── graph state / node-level resume
├── LangGraph store tables
│   └── cross-thread long-term memory
└── Claude session tables
    └── Agent SDK 原生 transcript / resume
```

### 5.1 PostgresSaver

保存 thread-scoped workflow state：

- 現在在哪個 node
- graph state
- messages / tool result（視 state 定義）
- interrupt / resume 所需資料

### 5.2 PostgresStore

保存跨 thread 的長期記憶，例如：

- 使用者偏好
- 專案背景
- 已確認的 domain facts
- 過去審核結果

### 5.3 Claude SessionStore

保存 Claude Agent SDK 自己需要的 session transcript。

它不是 LangGraph checkpoint 的替代品。LangGraph 保存 workflow；SDK SessionStore 保存 SDK native context。

---

## 6. Queue 與 Workflow 的差異

Celery / Redis 負責：

- 排隊
- worker 派送
- concurrency
- backpressure
- delivery retry

LangGraph 負責：

- 執行到哪個 node
- 狀態如何恢復
- workflow retry
- human-in-the-loop
- checkpoint

所以 Redis 掛掉時，不能失去「任務真實狀態」。真正狀態應該仍可從 PostgreSQL 的 `app_tasks` 與 LangGraph checkpoints 重建。

---

## 7. 如何換 Agent SDK

Graph 只依賴抽象介面：

```text
agent_runtime.agent_sdk.base.AgentExecutor
```

目前實作是：

```text
ClaudeAgentExecutor
```

未來可以新增：

```text
OpenAIAgentExecutor
GeminiAgentExecutor
InternalAgentExecutor
```

只要維持相同輸入輸出：

```python
await agent.run(RunRequest(...), memory_context=...)
```

A2A、FastAPI、LangGraph、queue 與 persistence 不需要全部重寫。

---

## 8. 如何新增 MCP / Tool

Tool 應該接在 Agent SDK 層，而不是直接塞進 A2A protocol。

```text
Other Agent --A2A--> This Agent --MCP--> Tool / Data / Service
```

判斷原則：

- 對方是具有自主能力的 Agent：A2A
- 對方是工具、資料庫、服務：MCP / Tool API
- 是固定內部流程：LangGraph node

---

## 9. 如何新增工作流節點

目前 graph：

```text
START
  ↓
load_memory
  ↓
agent_sdk
  ↓
save_memory
  ↓
END
```

企業流程可擴成：

```text
START
  ↓
authz_check
  ↓
load_context
  ↓
plan
  ↓
human_approval
  ↓
execute_tools
  ↓
validate_result
  ↓
save_memory
  ↓
END
```

不要把所有事情都塞進一個巨大 `agent_sdk` node，否則 Pod 在 node 中途死亡時，整個 node 可能重跑。

對有副作用的動作：

- 使用 idempotency key
- 使用 unique constraint
- 使用 upsert
- 寫 execution log
- 必要時用 transaction / outbox pattern

---

## 10. 建議閱讀順序

1. `src/agent_runtime/api/main.py`
   - 看服務如何啟動
   - 看 A2A routes 如何掛進 FastAPI

2. `src/agent_runtime/runtime.py`
   - 看所有元件如何組裝

3. `src/agent_runtime/a2a/server.py`
   - 看 Agent Card、JSON-RPC、DatabaseTaskStore

4. `src/agent_runtime/a2a/executor.py`
   - 看 A2A Message 如何轉成 runtime 呼叫

5. `src/agent_runtime/service.py`
   - 看共用執行邊界

6. `src/agent_runtime/graph/builder.py`
   - 看 memory / agent node / checkpoint

7. `src/agent_runtime/agent_sdk/claude.py`
   - 看真正的 Agent SDK 呼叫

8. `src/agent_runtime/persistence/*`
   - 看 LangGraph 與 SDK session 如何落 PostgreSQL

9. `src/agent_runtime/tasks/*`
   - 看 Internal REST 如何透過 Celery 執行

---

## 11. 常見誤區

### 誤區一：有 POST API 就叫 A2A

錯。A2A 應包含 Agent Card、Message、Task、Artifact、狀態與標準 binding。

### 誤區二：A2A task 等於 Celery task

錯。A2A Task 是 protocol-visible state；Celery task 是 delivery job。

### 誤區三：LangGraph checkpoint 等於 exactly once

錯。外部 DB write / HTTP side effect 仍可能在 crash 邊界重複執行。

### 誤區四：所有對話都存兩份完整 history

避免讓 LangGraph state 與 Agent SDK session 各自無限制保存相同內容，否則會造成 context 重複與 token 膨脹。

### 誤區五：Memory 就是 chat history

Memory 應該是經過選擇、抽取、可重用的長期資訊，而不是把所有訊息原封不動永久保存。

---

## 12. 範本的使用方式

複製此 repository 後，建議依序修改：

1. Agent Card 的 name / description / skills。
2. `ClaudeAgentExecutor` 的 system prompt、tools、MCP。
3. `AgentState` 與 graph nodes。
4. Memory namespace 與 extraction policy。
5. Authentication / tenant mapping。
6. Internal REST schema。
7. Kubernetes image、Ingress、A2A_PUBLIC_URL。
8. Migration、OTel、audit、idempotency。

請不要一開始就修改所有 persistence table 或移除 service layer。先保持分層，再依產品需求縮減。