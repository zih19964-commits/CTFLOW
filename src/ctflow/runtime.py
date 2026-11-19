"""节点与对话请求 Runtime：支持单次生成、同轮并行工具调用与三级配额渐进收敛。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol

from ctflow.models import Usage
from ctflow.provider import ModelClient, Reply
from ctflow.tags import format_budget_exhausted, format_budget_warning


class ToolProvider(Protocol):
    schemas: list[dict]

    async def call(self, name: str, arguments: dict) -> str: ...


class RuntimeResponseError(ValueError):
    """保留被拒绝响应的用量和工具步骤，供引擎记入运行快照。"""

    def __init__(self, message: str, reply: Reply) -> None:
        super().__init__(message)
        self.reply = reply


class SingleRequestRuntime:
    def __init__(self, client: ModelClient) -> None:
        self.client = client

    async def execute(
        self, model: str, messages: list[dict], on_text: Callable[[str], None]
    ) -> Reply:
        reply = await self.client.stream(model, messages, on_text)
        if reply.tool_calls:
            raise RuntimeResponseError("单次请求运行不支持工具响应", reply)
        if reply.status != "completed" or not reply.content.strip():
            raise RuntimeResponseError(f"模型未返回完整结果（status={reply.status!r}）", reply)
        return reply


class ToolRuntime:
    """多步 ToolCall 循环：同轮并行调用 MCP 工具，并在配额临近/耗尽时执行三级渐进收敛。"""

    def __init__(
        self,
        client: ModelClient,
        max_rounds: int,
        on_tool: Callable[[str, str, str], None] | None = None,
        on_tool_step: Callable[[dict], None] | None = None,
        *,
        recover_tool_errors: bool = True,
    ) -> None:
        self.client = client
        self.max_rounds = max_rounds
        self.on_tool = on_tool
        self.on_tool_step = on_tool_step
        self.recover_tool_errors = recover_tool_errors

    def _tool_event(self, event: str, name: str, detail: str = "") -> None:
        if self.on_tool:
            self.on_tool(event, name, detail)

    def _step_event(self, step: dict) -> None:
        if self.on_tool_step:
            self.on_tool_step(dict(step))

    async def _invoke_one(
        self,
        tools: ToolProvider,
        call: dict,
        step_no: int,
        round_index: int,
    ) -> tuple[dict, str, dict]:
        name = call.get("name", "")
        raw_arguments = call.get("arguments") or "{}"
        args_preview = (
            json.dumps(raw_arguments, ensure_ascii=False)
            if isinstance(raw_arguments, dict)
            else str(raw_arguments)
        )
        step_record: dict = {
            "step": step_no,
            "round": round_index,
            "name": name,
            "arguments": args_preview[:400],
            "status": "running",
            "duration_ms": 0,
            "result": "",
        }
        self._step_event(step_record)
        self._tool_event("start", name, args_preview[:200])

        started = time.perf_counter()
        try:
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as error:
                    raise ValueError(f"工具 {name} 的参数不是合法 JSON") from error
            if not isinstance(arguments, dict):
                raise ValueError(f"工具 {name} 的参数必须是 JSON 对象")
            result = await tools.call(name, arguments)
        except Exception as error:
            duration_ms = int((time.perf_counter() - started) * 1000)
            err_text = str(error)
            step_record.update(
                {
                    "status": "error",
                    "duration_ms": duration_ms,
                    "result": err_text[:500],
                }
            )
            self._step_event(step_record)
            self._tool_event("error", name, err_text)
            if not self.recover_tool_errors:
                raise
            return (
                call,
                f"工具调用错误（请检查参数或改用其他方式）：{err_text}",
                dict(step_record),
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        step_record.update(
            {
                "status": "ok",
                "duration_ms": duration_ms,
                "result": result[:500],
            }
        )
        self._step_event(step_record)
        self._tool_event("end", name, result)
        return (call, result, dict(step_record))

    async def execute(
        self,
        model: str,
        messages: list[dict],
        on_text: Callable[[str], None],
        tools: ToolProvider,
    ) -> Reply:
        working = list(messages)
        total = Usage(cost=0.0)
        tool_steps: list[dict] = []
        step_counter = 0
        has_streamed_text = False

        def make_round_writer() -> Callable[[str], None]:
            round_started = False

            def on_round_text(chunk: str) -> None:
                nonlocal has_streamed_text, round_started
                if not chunk:
                    return
                if has_streamed_text and not round_started:
                    on_text("\n\n")
                has_streamed_text = True
                round_started = True
                on_text(chunk)

            return on_round_text

        for round_index in range(1, self.max_rounds + 1):
            reply = await self.client.stream(model, working, make_round_writer(), tools.schemas)
            total.add(reply.usage)

            if not reply.tool_calls:
                reply.usage = total
                reply.tool_steps = list(tool_steps)
                if reply.status != "completed" or not reply.content.strip():
                    raise RuntimeResponseError(
                        f"模型未返回完整结果（status={reply.status!r}）", reply
                    )
                return reply

            batch = []
            for call in reply.tool_calls:
                step_counter += 1
                batch.append(self._invoke_one(tools, call, step_counter, round_index))
            outcomes = await asyncio.gather(*batch)

            results: list[tuple[dict, str]] = []
            for call, result_text, record in outcomes:
                tool_steps.append(record)
                results.append((call, result_text))

            # 仅剩最后 1 轮工具配额时，在末尾回填消息附带预警标签提示模型准备收敛。
            remaining = self.max_rounds - round_index
            if remaining == 1 and results:
                last_call, last_text = results[-1]
                results[-1] = (last_call, f"{last_text}\n\n{format_budget_warning(1)}")

            self.client.append_tool_round(working, reply, results)

        # 已耗尽全部 max_rounds 工具配额：关闭工具权限执行最后一次收敛总结请求，避免半途报错断流。
        working.append({"role": "user", "content": format_budget_exhausted(self.max_rounds)})
        final_reply = await self.client.stream(model, working, make_round_writer(), None)
        total.add(final_reply.usage)
        final_reply.usage = total
        final_reply.tool_steps = list(tool_steps)
        if (
            final_reply.tool_calls
            or final_reply.status != "completed"
            or not final_reply.content.strip()
        ):
            raise RuntimeResponseError(
                f"已耗尽 {self.max_rounds} 轮工具配额且总结未完成（status={final_reply.status!r}）",
                final_reply,
            )
        return final_reply
