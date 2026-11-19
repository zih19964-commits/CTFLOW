"""单 Worker 及 @architect 独立内存态多轮对话，支持 MCP 工具调用与提取 Mermaid 建图。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ctflow.architect import ArchitectTools
from ctflow.config import Settings
from ctflow.mcp_tools import MCPTools
from ctflow.models import ARCHITECT_ID, Usage, Worker, now
from ctflow.provider import ModelClient
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime, ToolRuntime
from ctflow.storage import Store
from ctflow.tags import format_system_context
from ctflow.tools import read_skills

_MERMAID_FENCE = re.compile(r"```mermaid[^\S\n]*\n(.*?)\n```", re.DOTALL)


def extract_mermaid_source(text: str) -> str:
    """从模型回复中提取最后一个 mermaid 代码块或裸 flowchart 文本。"""
    blocks = _MERMAID_FENCE.findall(text)
    if blocks:
        return blocks[-1].strip()
    stripped = text.strip()
    if stripped.startswith(("flowchart TD", "flowchart LR")):
        return stripped
    raise ValueError("最后一轮回复中未找到有效的 ```mermaid 代码块")


@dataclass
class ChatTurn:
    """内存单轮对话记录（包含用户提问、流式回复、工具步骤与本轮用量）。"""

    user: str
    assistant: str = ""
    status: str = "running"
    error: str = ""
    tool_steps: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    created_at: str = field(default_factory=now)
    finished_at: str | None = None


@dataclass
class ChatSession:
    """驻留内存的单 Worker 多轮对话上下文（不落盘保存）。"""

    id: str
    worker_id: str
    worker_name: str
    model: str
    turns: list[ChatTurn] = field(default_factory=list)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    @property
    def usage(self) -> Usage:
        total = Usage(cost=0.0)
        for turn in self.turns:
            if turn.usage.total_tokens or turn.status == "success":
                total.add(turn.usage)
        return total

    def build_messages(
        self, worker: Worker, settings: Settings, user_text: str, *, has_tools: bool
    ) -> list[dict]:
        """组装带闭合标签的 System 上下文与内存多轮对话历史。"""
        skills = read_skills(settings.skills_dir, worker.skills) if worker.skills else {}
        budget = settings.max_tool_rounds if has_tools else None
        sys_text = format_system_context(worker, skills, max_tool_rounds=budget)
        messages: list[dict] = []
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for turn in self.turns:
            if turn.status == "success" and turn.assistant.strip():
                messages.append({"role": "user", "content": turn.user})
                messages.append({"role": "assistant", "content": turn.assistant})
        messages.append({"role": "user", "content": user_text})
        return messages


async def run_chat_turn(
    session: ChatSession,
    worker: Worker,
    settings: Settings,
    user_text: str,
    *,
    store: Store | None = None,
    client: ModelClient | None = None,
    on_update: Callable[[], None] | None = None,
) -> ChatTurn:
    """在指定 Worker（或独立隔离的 @architect）上执行一轮对话。"""
    is_architect = worker.id == ARCHITECT_ID
    if not is_architect:
        unknown_mcp = set(worker.mcp_servers) - settings.mcp_servers.keys()
        if unknown_mcp:
            raise ValueError(
                f"Worker {worker.id} 引用了未登记的 MCP：{', '.join(sorted(unknown_mcp))}"
            )

    has_tools = bool(worker.mcp_servers)
    messages = session.build_messages(worker, settings, user_text, has_tools=has_tools)
    session.worker_name = worker.name
    session.model = worker.model
    turn = ChatTurn(user=user_text, status="running")
    session.turns.append(turn)
    session.updated_at = now()
    if on_update:
        on_update()

    def on_text(chunk: str) -> None:
        turn.assistant += worker.redact(chunk)
        if on_update:
            on_update()

    def on_tool_step(step: dict) -> None:
        redacted = {
            **step,
            "arguments": worker.redact(str(step.get("arguments", ""))),
            "result": worker.redact(str(step.get("result", ""))),
        }
        for existing in turn.tool_steps:
            if existing.get("step") == redacted["step"]:
                existing.update(redacted)
                if on_update:
                    on_update()
                return
        turn.tool_steps.append(redacted)
        if on_update:
            on_update()

    async def execute_with(active_client: ModelClient) -> None:
        if not has_tools:
            reply = await SingleRequestRuntime(active_client).execute(
                worker.model, messages, on_text
            )
        elif is_architect and store is not None:
            async with ArchitectTools(settings, store, worker) as tools:
                if tools.schemas:
                    reply = await ToolRuntime(
                        active_client,
                        settings.max_tool_rounds,
                        on_tool_step=on_tool_step,
                    ).execute(worker.model, messages, on_text, tools)
                else:
                    reply = await SingleRequestRuntime(active_client).execute(
                        worker.model, messages, on_text
                    )
        else:
            async with MCPTools(settings, worker) as tools:
                reply = await ToolRuntime(
                    active_client,
                    settings.max_tool_rounds,
                    on_tool_step=on_tool_step,
                ).execute(worker.model, messages, on_text, tools)
        turn.usage = reply.usage
        turn.assistant = worker.redact(reply.content)
        turn.status = "success"

    try:
        if client is None:
            async with ModelClient(settings, worker) as owned:
                await execute_with(owned)
        else:
            await execute_with(client)
    except asyncio.CancelledError:
        turn.status = "stopped"
        raise
    except RuntimeResponseError as exc:
        turn.usage = exc.reply.usage
        if exc.reply.content:
            turn.assistant = worker.redact(exc.reply.content)
        turn.status = "failed"
        turn.error = worker.redact(str(exc))
    except Exception as exc:
        turn.status = "failed"
        turn.error = worker.redact(str(exc))
    finally:
        turn.finished_at = now()
        session.updated_at = turn.finished_at
        if on_update:
            on_update()
    return turn
