"""A2A AgentExecutor：把標準 A2A request 接到共用 Agent runtime。

讀這個檔案時，可以把它理解成一個「protocol adapter」：

    A2A Message
        -> RuntimeA2AExecutor.execute()
        -> AgentRuntimeService.run_prompt()
        -> LangGraph
        -> Agent SDK
        -> 回傳文字結果
        -> A2A Artifact + completed Task

A2A SDK 負責 protocol lifecycle；我們的 application runtime 負責真正的 Agent 執行。
兩者不要混在一起。
"""
from __future__ import annotations

from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils import new_task_from_user_message, new_text_message, new_text_part


class RuntimeA2AExecutor(AgentExecutor):
    """官方 A2A AgentExecutor 的 application adapter。

    run_prompt 是一個 protocol-neutral callable。

    現在傳入的是：

        AgentRuntimeService.run_prompt

    因此 executor 不需要知道 Claude、LangGraph、PostgreSQL 的實作細節。
    未來就算把 Claude Agent SDK 換成別的 Agent SDK，只要 service contract 不變，
    A2A layer 理論上不需要重寫。
    """

    def __init__(self, run_prompt) -> None:
        self._run_prompt = run_prompt

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """執行一個 A2A request。

        這裡示範最常見的 A2A server 流程：

        1. 取得 / 建立 A2A Task
        2. 發出 working 狀態
        3. 把使用者訊息轉成 application prompt
        4. 呼叫共用 Agent runtime
        5. 把結果包成 Artifact
        6. 把 Task 標記 completed

        EventQueue 是 A2A SDK 用來發布 Task/Message/Artifact event 的通道。
        streaming client 也是透過這些 event 收到進度。
        """
        if context.message is None:
            raise ValueError("A2A message is required")

        # --------------------------------------------------------------
        # A2A Task
        # --------------------------------------------------------------
        # 如果這次 request 還沒有既存 Task，就由 incoming Message 建立一個標準 A2A Task。
        # Task 是 A2A protocol concept，不等於我們資料庫裡的 app_tasks。
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)

        # TaskUpdater 是官方 SDK 提供的 lifecycle helper。
        # 不要自己手刻 JSON 去更新 Task state。
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Agent task accepted"),
        )

        # --------------------------------------------------------------
        # ID 對應：A2A contextId -> LangGraph thread_id
        # --------------------------------------------------------------
        # A2A：
        #   contextId = 一段多輪對話 / 工作上下文
        #   taskId    = 其中某一次具體 Task
        #
        # LangGraph：
        #   thread_id = checkpoint / short-term workflow state 的 durable identity
        #
        # 因此 contextId 比 taskId 更適合映射到 thread_id。
        # 同一 context 的下一個 Task 就可以延續相同 LangGraph checkpoint history。
        thread_id = task.context_id or context.context_id or str(uuid4())

        # 目前範本只是示範 tenant mapping。
        # 正式環境應從認證後的 ServerCallContext / identity provider 取得可信 tenant/user，
        # 不應把 client 任意傳入的字串直接當 authorization identity。
        user_id = f"a2a:{context.tenant}"

        # 官方 SDK 幫我們把 A2A Message parts 整理成 user input。
        prompt = context.get_user_input()

        # --------------------------------------------------------------
        # 真正「接到 Agent」的第一個 application call。
        # --------------------------------------------------------------
        # 這裡沒有直接呼叫 Claude Agent SDK。
        #
        # 原因：我們希望所有入口都經過同一套 durable runtime：
        #   A2A -> Service -> LangGraph -> Agent SDK
        #
        # 這樣 memory、checkpoint、session、workflow policy 才只有一套。
        result = await self._run_prompt(
            user_id=user_id,
            thread_id=thread_id,
            prompt=prompt,
            remember=False,
        )

        # --------------------------------------------------------------
        # Agent result -> A2A Artifact
        # --------------------------------------------------------------
        # A2A 世界裡，「執行結果」應該用 Artifact 表達，而不是回傳自訂 response JSON。
        # 目前範本只回 text/plain；實際專案可以再擴充結構化資料或檔案 artifact。
        await updater.add_artifact(
            parts=[new_text_part(text=result["text"], media_type="text/plain")]
        )

        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Agent task completed"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """處理 A2A cancellation。

        A2A 層收到 cancel 不代表外部副作用自動 rollback。

        例如 Agent 已經：
          - 寫入 Oracle
          - 呼叫設備 API
          - push Git commit

        那些操作仍需要 tool 自己具備 idempotency / cancellation / compensation 設計。
        """
        task = context.current_task
        if task is None:
            return

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("Cancellation requested"),
        )
