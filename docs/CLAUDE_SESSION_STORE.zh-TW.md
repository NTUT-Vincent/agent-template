# Claude Agent SDK SessionStore：跨 Pod Session 恢復指南

這份文件專門說明本範本如何用 Claude Agent SDK 官方 `SessionStore` contract 解決 Kubernetes Pod 重啟後 session transcript 遺失的問題。

官方文件：<https://code.claude.com/docs/en/agent-sdk/session-storage>

---

## 1. 問題是什麼？

Claude Agent SDK 預設把 native session transcript 寫到 Pod 本地檔案系統：

```text
~/.claude/projects/.../<session-id>.jsonl
```

Kubernetes Pod 是 ephemeral：

```text
Pod A
  -> local transcript
  -> Pod 被重建
  -> Pod B
  -> local transcript 不存在
```

所以不能把 Pod local filesystem 當成 production session source。

---

## 2. 官方 SessionStore 的模型

官方 SessionStore 是 external transcript mirror：

```text
Claude Code subprocess
        |
        | local JSONL first
        v
SessionStore.append(...)
        |
        v
PostgreSQL
```

resume 時：

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
new Pod continues session
```

重要：SessionStore 是 mirror，不是 transaction log。SDK 仍會先寫 local transcript，再 mirror 到 external store。

---

## 3. 本範本保存兩種 session 資料

### Application mapping

`app_sessions`：

```text
thread_id <-> sdk_session_id
```

它回答：

> 這條 LangGraph thread 要 resume 哪一個 Claude native session？

### Claude native transcript

`PostgresClaudeSessionStore`：

```text
project_key
session_id
subpath
seq
entry_uuid
entry
mtime
```

它回答：

> 這個 Claude session 的 transcript entries 在哪裡？

兩者缺一不可：

```text
thread_id
   |
   v
sdk_session_id
   |
   v
SessionStore transcript
```

---

## 4. 為什麼不能只等 ResultMessage 才保存 session id？

錯誤時序：

```text
query()
  -> SDK 建 session
  -> transcript 已開始 mirror
  -> Agent 執行很多 tool
  -> Pod crash
  -> 還沒收到 ResultMessage
```

若 `app_sessions.sdk_session_id` 仍是 NULL，PostgreSQL 雖然可能已經有 transcript，application 卻不知道下一顆 Pod 要 resume 哪一份。

所以本範本使用 Python SDK init `SystemMessage.data["session_id"]`：

```text
query()
  -> SystemMessage(subtype="init")
  -> 立即取得 sdk_session_id
  -> COMMIT app_sessions
  -> Agent 繼續執行
```

程式位置：

```text
src/agent_runtime/agent_sdk/claude.py
src/agent_runtime/service.py
src/agent_runtime/graph/builder.py
```

---

## 5. Early session ID 如何穿過 LangGraph？

不能把 Python callback 放進 checkpoint state，因為 callback 不可序列化。

因此本範本放在 `GraphContext`：

```text
AgentRuntimeService
  -> 建立 persist_session_id_early callback
  -> GraphContext(on_sdk_session_id=callback)
  -> LangGraph agent_sdk node
  -> ClaudeAgentExecutor.run(on_session_id=callback)
  -> init SystemMessage
  -> callback
  -> PostgreSQL app_sessions
```

`GraphContext` 是本次 invoke 的 runtime context，不是 durable graph state。

---

## 6. SessionStore append 為什麼一定要去重？

官方 SessionStore mirror 是 best-effort，append 失敗會 retry。

可能發生：

```text
append(batch A)
  -> PostgreSQL 寫入成功
  -> network response lost
  -> SDK retry batch A
```

如果 adapter 只是盲目 INSERT，就會重複 transcript entries。

官方建議使用 `entry.uuid` deduplicate。

本範本因此新增：

```text
entry_uuid
```

以及 unique index：

```text
(project_key, session_id, subpath, entry_uuid)
```

INSERT 使用：

```sql
ON CONFLICT (...) DO NOTHING
```

---

## 7. 為什麼有 subpath？

Claude subagent transcript 不是全部混在 main transcript。

官方使用類似：

```text
subagents/agent-<id>
```

的 `subpath`。

因此 SessionStore 必須：

```text
main transcript
session_id = abc
subpath = ""

subagent transcript
session_id = abc
subpath = "subagents/agent-123"
```

resume 時 SDK 會呼叫 `list_subkeys()` 來找到 subagent transcripts。

---

## 8. Session Summary sidecar

本範本實作官方 optional：

```python
list_session_summaries()
```

並在 `append()` 內用：

```python
fold_session_summary(...)
```

維護 SDK-owned opaque summary。

目的不是 Agent memory，而是讓 session listing 不必對每一個 session 都完整 `load()` transcript。

請不要把 summary data 當成自己的 business schema；它是 SDK-owned state，應原樣保存。

---

## 9. mirror_error

官方規格中，external mirror 最終失敗不一定中止 Agent。

SDK 會發出：

```text
SystemMessage(subtype="mirror_error")
```

本範本在 `ClaudeAgentExecutor` 中記錄 error log 與 `mirror_error_count`。

代表：

```text
Agent 本輪可能成功
但 PostgreSQL transcript 可能缺少某一 batch
```

Production 應把這個 log 接到 OTel / metrics / alert。

---

## 10. Pod crash recovery 實際流程

### 正常第一輪

```text
thread_id = thread-001

Pod A
  -> Claude query
  -> init session_id = sdk-abc
  -> app_sessions COMMIT
  -> eager SessionStore mirror
  -> PostgreSQL transcript
```

### Pod crash

```text
Pod A dies
```

外部資料仍有：

```text
app_sessions
thread-001 -> sdk-abc

claude_session_store
sdk-abc -> transcript entries
```

### Pod B 接手

```text
Pod B
  -> load thread-001
  -> sdk_session_id = sdk-abc
  -> ClaudeAgentOptions(resume="sdk-abc")
  -> SessionStore.load()
  -> restore transcript
  -> continue
```

因此 Pod 本身不再擁有 conversation identity。

---

## 11. LangGraph Memory 仍然不要塞進 SessionStore

這兩者責任不同：

```text
Claude SessionStore
  -> native conversation transcript
  -> tool calls/results
  -> resume Claude session

LangGraph PostgresStore
  -> long-term application memory
  -> user preference
  -> domain fact
  -> past decision
```

執行時：

```text
PostgresStore
  -> retrieve relevant memory
  -> memory_context
  -> Agent SDK
```

執行完再經過 memory extraction 寫回 PostgresStore。

不要把完整 transcript 同時複製成 LangGraph long-term memory。

---

## 12. CI 驗證

本範本不是自己猜 SessionStore 規則，而是直接跑官方 Python SDK conformance suite：

```python
from claude_agent_sdk.testing import run_session_store_conformance
```

CI 會啟動 PostgreSQL service，再對 `PostgresClaudeSessionStore` 驗證 append/load/order/subpath/list/delete/summary 等 contract。

檔案：

```text
tests/test_claude_session_store_conformance.py
.github/workflows/ci.yml
```

---

## 13. SessionStore 不等於 exactly-once

最後仍要記住：

```text
SessionStore durability != business side-effect exactly once
```

例如：

```text
Claude
  -> MCP tool 寫資料成功
  -> Pod crash
  -> workflow retry
  -> tool 再執行一次
```

因此 side-effect tool 還是要有：

- idempotency key
- unique constraint
- execution record
- transaction / outbox
- compensation strategy

SessionStore 解決的是 **conversation resume**，不是 distributed transaction。
