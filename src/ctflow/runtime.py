"""节点请求 runtime：无工具单次请求，有 MCP 时执行最小工具循环。"""

import json
from collections.abc import Callable
from typing import Protocol

from ctflow.models import Usage
from ctflow.provider import ModelClient, Reply


class ToolProvider(Protocol):
    schemas: list[dict]

    async def call(self, name: str, arguments: dict) -> str: ...


class RuntimeResponseError(ValueError):
    """保留被拒绝响应的用量，供引擎记入运行快照。"""

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
    """在单次请求基础上执行 MCP 工具调用，最多进行配置的轮数。"""

    def __init__(
        self,
        client: ModelClient,
        max_rounds: int,
        on_tool: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.client = client
        self.max_rounds = max_rounds
        self.on_tool = on_tool

    def _tool_event(self, event: str, name: str, detail: str = "") -> None:
        if self.on_tool:
            self.on_tool(event, name, detail)

    async def execute(
        self,
        model: str,
        messages: list[dict],
        on_text: Callable[[str], None],
        tools: ToolProvider,
    ) -> Reply:
        working = list(messages)
        total = Usage()
        last = Reply()
        for _ in range(self.max_rounds):
            reply = await self.client.stream(model, working, on_text, tools.schemas)
            last = reply
            total.add(reply.usage)
            if not reply.tool_calls:
                reply.usage = total
                if reply.status != "completed" or not reply.content.strip():
                    raise RuntimeResponseError(
                        f"模型未返回完整结果（status={reply.status!r}）", reply
                    )
                return reply
            results = []
            for call in reply.tool_calls:
                name = call.get("name", "")
                raw_arguments = call.get("arguments") or "{}"
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError as error:
                        raise ValueError(f"MCP 工具 {name} 的参数不是合法 JSON") from error
                if not isinstance(arguments, dict):
                    raise ValueError(f"MCP 工具 {name} 的参数必须是 JSON 对象")
                self._tool_event("start", name)
                try:
                    result = await tools.call(name, arguments)
                except Exception as error:
                    self._tool_event("error", name, str(error))
                    raise
                self._tool_event("end", name, result)
                results.append((call, result))
            self.client.append_tool_round(working, reply, results)
        last.usage = total
        raise RuntimeResponseError(
            f"已达到 MCP 工具轮数上限 {self.max_rounds}，请简化任务或提高 max_tool_rounds",
            last,
        )
