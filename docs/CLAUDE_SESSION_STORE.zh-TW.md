# Claude Agent SDK SessionStore：跨 Pod Session 恢復指南

這份文件專門說明本範本如何使用 Claude Agent SDK 官方 `SessionStore` contract，解決 Kubernetes Pod 重啟、換 node、multi-host 執行時 Claude native session transcript 無法只靠本機檔案保存的問題。

相關文件：

- [`../README.md`](../README.md)：整體架構摘要
- [`TUTORIAL.zh-TW.md`](TUTORIAL.zh-TW.md)：完整 runtime 教學
- 官方文件：<https://code.claude.com/docs/en/agent-sdk/session-storage>

---

# 1. 問題：Pod local transcript 不可靠

Claude Agent SDK native session 預設會有本機 transcript，例如：

```text
~/.claude/projects/.../<session-id>.jsonl
```

但 Kubernetes Pod 是 ephemeral：

```text
Pod A
  -> local transcript
  -> Pod 被重建
  -> Pod B
  -> Pod A filesystem 不存在
```

因此 production 不能把 Pod local filesystem 當成唯一 session persistence。

---

# 2. 官方 SessionStore 的正確心智模型

SessionStore 是 **external transcript mirror**：

```text
Claude SDK / local JSONL
        |
        | SessionStore.append()
        v
External Store
        |
        v
PostgreSQL
```

Resume：

```text
ClaudeAgentOptions(resume=sdk_session_id)
        |
        v
SessionStore.load(...)
        |
        v
restore transcript
        |
        v
new Pod continues native session
```

非常重要：

> **SessionStore 是 mirror，不是 transaction log，也不是 local transcript 的強一致 replacement。**

所以：

- mirror 需要 retry-safe
- append 需要 dedupe
- mirror failure 需要 observability
- tool side effect 還是需要 idempotency

---

# 3. 本範本其實保存三個不同 session/workflow state

## 3.1 `app_sessions`

Application mapping：

```text
thread_id <-> sdk_session_id
```

用途：

> 這條 LangGraph durable thread 應該 resume 哪一份 Claude native session？

它不保存 transcript。

---

## 3.2 `PostgresClaudeSessionStore`

保存 Claude Agent SDK native transcript：

```text
project_key
session_id
subpath
seq
entry_uuid
entry JSONB
mtime
```

它回答：

> `sdk_session_id` 對應的 Claude transcript / subagent transcript 在哪？

---

## 3.3 LangGraph PostgresSaver

保存 workflow checkpoint：

```text
thread_id
  -> graph state
  -> node progress
  -> interrupt/resume state
```

這不是 Claude transcript。

---

# 4. 為什麼不能只等 ResultMessage 才存 session ID？

錯誤時序：

```text
query()
  -> SDK 建立 native session
  -> transcript 開始 mirror
  -> Agent 執行 tools
  -> Pod crash
  -> 尚未收到 ResultMessage
```

此時可能變成：

```text
PostgreSQL SessionStore
  已有 sdk-abc transcript

app_sessions
  thread-001 -> NULL
```

也就是 transcript 還在，但 application 不知道該 resume 哪一份。

所以本範本使用 init `SystemMessage`：

```text
query()
  -> SystemMessage(subtype="init")
  -> data["session_id"] = sdk-abc
  -> 立即 COMMIT app_sessions
  -> Agent 繼續執行
```

程式位置：

```text
src/agent_runtime/agent_sdk/claude.py
src/agent_runtime/graph/builder.py
src/agent_runtime/service.py
```

---

# 5. Early session ID 如何穿過 LangGraph？

不能把 Python callback 放進 durable `AgentState`，因為 callback 不應被 checkpoint serialization。

所以使用 `GraphContext`：

```text
AgentRuntimeService
  -> 建立 persist_session_id_early()
  -> GraphContext(on_sdk_session_id=callback)
  -> LangGraph agent_sdk node
  -> ClaudeAgentExecutor.run(on_session_id=callback)
  -> init SystemMessage
  -> callback(session_id)
  -> COMMIT app_sessions
```

`GraphContext` 是本次 invoke runtime context，不是 durable graph state。

---

# 6. 完整第一輪時序

```text
thread_id = thread-001
sdk_session_id = NULL

AgentRuntimeService
  -> graph.ainvoke(thread_id=thread-001)
  -> ClaudeAgentExecutor
  -> ClaudeAgentOptions(
       resume=None,
       session_store=PostgresClaudeSessionStore,
       session_store_flush="eager"
     )
  -> query()
  -> init session_id=sdk-abc
  -> callback
  -> app_sessions COMMIT
  -> SDK continues
  -> SessionStore.append()
  -> PostgreSQL transcript
  -> ResultMessage
  -> final consistency check
```

ResultMessage 後仍會再保存一次 session ID，但它是 fallback / consistency check，不再是第一次 persistence point。

---

# 7. 下一顆 Pod 如何 resume？

Pod A crash 後，外部資料仍存在：

```text
app_sessions
thread-001 -> sdk-abc

claude_session_store
sdk-abc -> transcript entries

LangGraph checkpoint
thread-001 -> workflow state
```

Pod B：

```text
thread-001
  -> app_sessions
  -> sdk_session_id=sdk-abc
  -> ClaudeAgentOptions(resume="sdk-abc")
  -> SessionStore.load()
  -> restore transcript
  -> continue Claude native session
```

所以：

> **Pod 不擁有 conversation identity；Pod 只是可替換 executor。**

---

# 8. `session_store_flush="eager"` 的角色

Template 使用：

```python
session_store_flush="eager"
```

目的：更積極把 local transcript mirror 到 external store，縮短 Pod crash 時 external store 落後的時間窗口。

但它仍然不代表：

```text
每一個 Claude frame
與 PostgreSQL
具有同步 transaction consistency
```

所以不能把 SessionStore 當成 exactly-once event log。

---

# 9. append 為什麼一定要 dedupe？

Mirror retry 可能出現：

```text
append(batch A)
  -> PostgreSQL 已成功
  -> response/network lost
  -> SDK retry batch A
```

盲目 INSERT 會造成 transcript 重複。

因此本範本使用官方 entry 的：

```text
entry.uuid
```

保存成：

```text
entry_uuid
```

並建立：

```text
UNIQUE(project_key, session_id, subpath, entry_uuid)
```

寫入使用 conflict-safe insert。

同一 session/subpath append 還會取得 PostgreSQL advisory transaction lock，保護：

- append order
- UUID dedupe check
- session summary fold

---

# 10. `subpath` 為什麼重要？

Claude subagent transcript 不是全部混在 main transcript。

```text
main transcript
session_id = sdk-abc
subpath = ""

subagent transcript
session_id = sdk-abc
subpath = "subagents/agent-123"
```

SessionStore 實作：

```python
list_subkeys(...)
```

讓 SDK resume 時可以找到 subagent transcript。

---

# 11. Session Summary sidecar

Template 也支援 SessionStore optional summary contract：

```python
fold_session_summary(...)
list_session_summaries(...)
```

保存：

```text
project_key
session_id
mtime
data JSONB
```

這份 summary 是 **SDK-owned opaque state**。

用途是讓 session listing 不需要對每一份 session transcript 做完整 `load()`。

不要把它當：

- LangGraph memory
- business schema
- user profile store

---

# 12. `mirror_error`

SessionStore external append 最終失敗時，Agent SDK 可能不停止 Agent，而是 emit：

```text
SystemMessage(subtype="mirror_error")
```

Template 的 `ClaudeAgentExecutor` 會：

```text
logger.error(...)
mirror_error_count += 1
```

這代表：

```text
Agent 本輪可能成功
但是 shared PostgreSQL transcript 可能缺某一批資料
```

Production 應該將它接到：

- OpenTelemetry
- structured logging
- metrics
- alert
- SLO / incident monitoring

收到 mirror error 後，不應假裝 cross-Pod resume durability 完全健康。

---

# 13. SessionStore 與 LangGraph Memory 的關係

不要做：

```text
Claude transcript
  -> 全量 copy 到 LangGraph memory
```

也不要：

```text
LangGraph memory
  -> 全量寫進 Claude transcript
```

正確模型：

```text
LangGraph PostgresStore
   -> retrieve relevant facts/preferences/decisions
   -> memory_context
   -> Claude Agent SDK

Claude SessionStore
   -> native conversation/tool continuity

Agent result
   -> memory extraction / policy
   -> LangGraph PostgresStore
```

所以：

```text
Memory     = application knowledge
Session    = native conversation transcript
Checkpoint = workflow state
```

---

# 14. SessionStore 不等於 exactly-once

例子：

```text
Claude
  -> MCP tool 寫外部資料成功
  -> Pod crash
  -> workflow/job retry
  -> MCP tool 再執行
```

SessionStore 不會阻止 business side effect 重複。

Side-effect tool 必須另外設計：

- idempotency key
- unique constraint
- upsert
- execution record
- transaction
- outbox
- compensation strategy

---

# 15. 官方 conformance test

本範本直接使用 Claude Agent SDK 官方 conformance suite：

```python
from claude_agent_sdk.testing import run_session_store_conformance
```

測試：

```text
tests/test_claude_session_store_conformance.py
```

CI 會啟動 PostgreSQL service，驗證 adapter contract，包括：

- append / load round trip
- append order
- unknown session
- subpath isolation
- project isolation
- list sessions
- delete
- list subkeys
- session summaries

這比只自己猜幾個 unit test case 更可靠。

---

# 16. Recovery 保證與限制

Template 能提供的合理 recovery contract：

```text
LangGraph workflow state
    -> PostgreSQL

Claude native transcript
    -> PostgreSQL SessionStore mirror

thread_id <-> sdk_session_id
    -> app_sessions PostgreSQL

A2A Task state
    -> A2A DatabaseTaskStore PostgreSQL

Internal Task state
    -> app_tasks PostgreSQL
```

但仍有兩個重要限制：

## 16.1 init 前 crash

如果新的 Claude session 尚未 emit init session ID，Pod 就死亡，application 還沒有 SDK session identity 可以保存。

這是目前由 SDK 產生 session ID 時的 lifecycle boundary。

## 16.2 Mirror 是 best-effort

SessionStore 不是 distributed transaction log。

即使使用 eager flush，仍應對 mirror error、tool idempotency、workflow retry 做完整設計。

---

# 17. 最終圖

```text
                       PostgreSQL
          +----------------+------------------+
          |                |                  |
          v                v                  v
     app_sessions    Claude SessionStore   LangGraph
     thread <-> sdk   transcript/subagent   checkpoint
          |                |                  |
          +----------------+---------+--------+
                                     |
                                     v
                                Kubernetes Pod
                                     |
                                     v
                                  LangGraph
                                     |
                                     v
                             ClaudeAgentExecutor
                                     |
                                     v
                              Claude Agent SDK
                                     |
                                     v
                              MCP / Tools / LLM
```

一句話總結：

> **跨 Pod resume 需要同時保存「哪一個 session」與「那個 session 的 transcript」：`app_sessions` 保存 identity mapping，SessionStore 保存 native transcript；LangGraph 則另外保存 workflow state。**
