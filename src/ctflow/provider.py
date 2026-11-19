"""按 Worker 选择 Chat Completions、Responses 或 Anthropic 上游。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI, OpenAIError

from ctflow.config import Settings
from ctflow.models import Usage, Worker

PROTOCOLS = frozenset({"chat", "responses", "anthropic"})


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
        self.protocol = worker.protocol
        self.openai = None
        self.http = None
        if self.protocol in {"chat", "responses"}:
            self.openai = AsyncOpenAI(
                api_key=worker.api_key,
                base_url=worker.base_url,
                timeout=settings.timeout,
                max_retries=0,
            )
        else:
            self.http = httpx.AsyncClient(timeout=settings.timeout)

    async def __aenter__(self) -> ModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.openai is not None:
            await self.openai.close()
        if self.http is not None:
            await self.http.aclose()

    def usage(self, model: str, input_tokens: int, output_tokens: int) -> Usage:
        return calculate_usage(self.settings, model, input_tokens, output_tokens)

    async def list_models(self) -> list[str]:
        if self.protocol == "anthropic":
            response = await self.http.get(
                self._anthropic_base() + "/v1/models", headers=self._anthropic_headers()
            )
            if response.status_code >= 400:
                raise ValueError(self.worker.redact(response.text))
            return sorted(item["id"] for item in response.json().get("data", []))
        try:
            result = await self.openai.models.list()
        except OpenAIError as exc:
            raise ValueError(self.worker.redact(str(exc))) from None
        return sorted(model.id for model in result.data)

    async def stream(
        self,
        model: str,
        messages: list[dict],
        on_text: Callable[[str], None],
        tools: list[dict] | None = None,
    ) -> Reply:
        if self.protocol == "chat":
            return await self._stream_chat(model, messages, on_text, tools)
        if self.protocol == "responses":
            return await self._stream_responses(model, messages, on_text, tools)
        return await self._stream_anthropic(model, messages, on_text, tools)

    def append_tool_round(
        self, messages: list[dict], reply: Reply, results: list[tuple[dict, str]]
    ) -> None:
        if self.protocol == "chat":
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
        elif self.protocol == "responses":
            if reply.content:
                messages.append({"role": "assistant", "content": reply.content})
            for call, _ in results:
                messages.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": call["arguments"],
                    }
                )
            for call, result in results:
                messages.append(
                    {"type": "function_call_output", "call_id": call["id"], "output": result}
                )
        else:
            content = []
            if reply.content:
                content.append({"type": "text", "text": reply.content})
            for call, _ in results:
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": json.loads(call["arguments"] or "{}"),
                    }
                )
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": result,
                        }
                        for call, result in results
                    ],
                }
            )

    def _schemas(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        if self.protocol == "chat":
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
        if self.protocol == "responses":
            return [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                }
                for tool in tools
            ]
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            }
            for tool in tools
        ]

    async def _stream_chat(
        self, model: str, messages: list[dict], on_text: Callable[[str], None], tools
    ) -> Reply:
        reply = Reply()
        calls: dict[int, dict] = {}
        parameters = {
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

    async def _stream_responses(
        self, model: str, messages: list[dict], on_text: Callable[[str], None], tools
    ) -> Reply:
        reply = Reply()
        parameters = {
            "model": model,
            "input": messages,
            "stream": True,
            "max_output_tokens": self.settings.max_tokens,
        }
        if self.worker.extra_body:
            parameters["extra_body"] = self.worker.extra_body
        schemas = self._schemas(tools)
        if schemas:
            parameters["tools"] = schemas
        try:
            async with asyncio.timeout(self.settings.timeout):
                stream = await self.openai.responses.create(**parameters)
                async with stream:
                    async for event in stream:
                        if event.type == "response.output_text.delta":
                            reply.content += event.delta
                            on_text(event.delta)
                        elif event.type in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                        }:
                            response = event.response
                            reply.status = response.status or ""
                            if response.usage is not None:
                                reply.usage = self.usage(
                                    model,
                                    response.usage.input_tokens,
                                    response.usage.output_tokens,
                                )
                            for item in response.output:
                                if item.type == "function_call":
                                    reply.tool_calls.append(
                                        {
                                            "id": item.call_id,
                                            "name": item.name,
                                            "arguments": item.arguments,
                                        }
                                    )
                        elif event.type == "error":
                            raise ValueError(self.worker.redact(event.message))
        except TimeoutError as exc:
            raise ValueError(f"模型请求超过 {self.settings.timeout:g} 秒") from exc
        except OpenAIError as exc:
            raise ValueError(self.worker.redact(str(exc))) from None
        return reply

    async def _stream_anthropic(
        self, model: str, messages: list[dict], on_text: Callable[[str], None], tools
    ) -> Reply:
        reply = Reply()
        system = "\n\n".join(
            item["content"]
            for item in messages
            if item.get("role") == "system" and item.get("content")
        )
        chat_messages = [item for item in messages if item.get("role") != "system"]
        payload = {
            **self.worker.extra_body,
            "model": model,
            "max_tokens": self.settings.max_tokens,
            "stream": True,
            "messages": chat_messages,
        }
        if system:
            payload["system"] = system
        schemas = self._schemas(tools)
        if schemas:
            payload["tools"] = schemas
        blocks: dict[int, dict] = {}
        try:
            async with asyncio.timeout(self.settings.timeout):
                async with self.http.stream(
                    "POST",
                    self._anthropic_base() + "/v1/messages",
                    headers=self._anthropic_headers(),
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise ValueError(self.worker.redact(body))
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        event = json.loads(raw)
                        kind = event.get("type", "")
                        if kind == "message_start":
                            usage = event.get("message", {}).get("usage", {})
                            reply.usage = self.usage(
                                model,
                                usage.get("input_tokens", 0),
                                reply.usage.output_tokens,
                            )
                        elif kind == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                raw_input = block.get("input")
                                initial = (
                                    json.dumps(raw_input, ensure_ascii=False)
                                    if isinstance(raw_input, dict) and raw_input
                                    else ""
                                )
                                blocks[event["index"]] = {
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "arguments": initial,
                                }
                        elif kind == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                reply.content += text
                                on_text(text)
                            elif delta.get("type") == "input_json_delta":
                                block = blocks.get(event["index"])
                                if block is not None:
                                    block["arguments"] += delta.get("partial_json", "")
                        elif kind == "message_delta":
                            stop_reason = event.get("delta", {}).get("stop_reason")
                            if stop_reason:
                                reply.status = {
                                    "end_turn": "completed",
                                    "tool_use": "tool_calls",
                                    "max_tokens": "length",
                                    "stop_sequence": "completed",
                                }.get(stop_reason, stop_reason)
                            usage = event.get("usage", {})
                            if "output_tokens" in usage:
                                reply.usage = self.usage(
                                    model,
                                    reply.usage.input_tokens,
                                    usage["output_tokens"],
                                )
                        elif kind == "error":
                            message = event.get("error", {}).get("message", "Anthropic 请求失败")
                            raise ValueError(self.worker.redact(message))
            reply.tool_calls = [
                {
                    **blocks[index],
                    "arguments": blocks[index]["arguments"] or "{}",
                }
                for index in sorted(blocks)
            ]
        except TimeoutError as exc:
            raise ValueError(f"模型请求超过 {self.settings.timeout:g} 秒") from exc
        except httpx.HTTPError as exc:
            raise ValueError(self.worker.redact(str(exc))) from None
        return reply

    def _anthropic_base(self) -> str:
        base = self.worker.base_url.rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    def _anthropic_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.worker.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
