"""统一使用标准 OpenAI Chat Completions 协议按 Worker 独立连接请求上游模型。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import AsyncOpenAI, OpenAIError

from ctflow.config import Settings
from ctflow.models import Usage, Worker


def calculate_usage(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> Usage:
    price = settings.prices.get(model)
    cost = None
    if price is not None:
        cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
    return Usage(input_tokens, output_tokens, cost)


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    status: str = ""
    tool_steps: list[dict] = field(default_factory=list)


class ModelClient:
    def __init__(self, settings: Settings, worker: Worker) -> None:
        worker.require_api()
        self.settings = settings
        self.worker = worker
        self.openai = AsyncOpenAI(
            api_key=worker.api_key,
            base_url=worker.base_url,
            timeout=settings.timeout,
            max_retries=0,
        )

    async def __aenter__(self) -> ModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.openai.close()

    def usage(self, model: str, input_tokens: int, output_tokens: int) -> Usage:
        return calculate_usage(self.settings, model, input_tokens, output_tokens)

    async def list_models(self) -> list[str]:
        try:
            result = await self.openai.models.list()
        except OpenAIError as exc:
            raise ValueError(self.worker.redact(str(exc))) from None
        return sorted(model.id for model in result.data)

    def append_tool_round(
        self, messages: list[dict], reply: Reply, results: list[tuple[dict, str]]
    ) -> None:
        messages.append(
            {
                "role": "assistant",
                "content": reply.content or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in reply.tool_calls
                ],
            }
        )
        for call, result in results:
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    @staticmethod
    def _schemas(tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]

    async def stream(
        self,
        model: str,
        messages: list[dict],
        on_text: Callable[[str], None],
        tools: list[dict] | None = None,
    ) -> Reply:
        reply = Reply()
        calls: dict[int, dict] = {}
        parameters: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.settings.max_tokens,
        }
        if self.worker.extra_body:
            parameters["extra_body"] = self.worker.extra_body
        schemas = self._schemas(tools)
        if schemas:
            parameters["tools"] = schemas
        try:
            async with asyncio.timeout(self.settings.timeout):
                stream = await self.openai.chat.completions.create(**parameters)
                async with stream:
                    async for chunk in stream:
                        if chunk.usage is not None:
                            reply.usage = self.usage(
                                model, chunk.usage.prompt_tokens, chunk.usage.completion_tokens
                            )
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        if choice.finish_reason:
                            reply.status = (
                                "completed"
                                if choice.finish_reason == "stop"
                                else choice.finish_reason
                            )
                        delta = choice.delta
                        if delta.content:
                            reply.content += delta.content
                            on_text(delta.content)
                        for part in delta.tool_calls or []:
                            index = part.index if part.index is not None else 0
                            item = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if part.id:
                                item["id"] = part.id
                            if part.function:
                                item["name"] += part.function.name or ""
                                item["arguments"] += part.function.arguments or ""
            reply.tool_calls = [calls[index] for index in sorted(calls)]
        except TimeoutError as exc:
            raise ValueError(f"模型请求超过 {self.settings.timeout:g} 秒") from exc
        except OpenAIError as exc:
            raise ValueError(self.worker.redact(str(exc))) from None
        return reply
