"""单 Worker 独立多轮对话会话，复用统一模型客户端与多步工具循环。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from ctflow.builtin_tools import CompositeTools, has_configured_tools, validate_builtin_tools
from ctflow.config import Settings
from ctflow.models import Usage, Worker, new_id, now
from ctflow.provider import ModelClient
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime, ToolRuntime
from ctflow.tools import read_skills


@dataclass
class ChatTurn:
    """单轮对话记录，包含用户输入、模型回复、工具步骤与用量。"""

    user: str
    assistant: str = ""
    status: str = "running"
    error: str = ""
    tool_steps: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    created_at: str = field(default_factory=now)
    finished_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ChatTurn:
        values = dict(data)
        values["usage"] = Usage(**data.get("usage", {}))
        return cls(**values)


@dataclass
class ChatSession:
    """单个 Worker 的多轮对话上下文与持久化记录。"""

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

    def to_public_dict(self, secret_worker: Worker | None = None) -> dict:
        raw = asdict(self)
        if secret_worker is None or not secret_worker.api_key:
            return raw

        def redact(item: object) -> object:
            if isinstance(item, str):
                return secret_worker.redact(item)
            if isinstance(item, dict):
                return {redact(k): redact(v) for k, v in item.items()}
            if isinstance(item, list):
                return [redact(v) for v in item]
            return item

        return redact(raw)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: dict) -> ChatSession:
        values = dict(data)
        values["turns"] = [ChatTurn.from_dict(item) for item in data.get("turns", [])]
        return cls(**values)

    def build_messages(self, worker: Worker, settings: Settings, user_text: str) -> list[dict]:
        """组装 system 提示词、技能正文及历史多轮对话消息。"""
        skill_text = read_skills(settings.workspace, worker.skills) if worker.skills else ""
        parts = [part for part in (worker.prompt.strip(), skill_text.strip()) if part]
        messages: list[dict] = []
        if parts:
            messages.append({"role": "system", "content": "\n\n".join(parts)})
        for turn in self.turns:
            if turn.status == "success" and turn.assistant.strip():
                messages.append({"role": "user", "content": turn.user})
                messages.append({"role": "assistant", "content": turn.assistant})
        messages.append({"role": "user", "content": user_text})
        return messages


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_chat_session(data_dir: Path, session: ChatSession, worker: Worker | None = None) -> Path:
    path = data_dir.resolve() / "chats" / f"chat-{session.id}.json"
    _write_json_atomic(path, session.to_public_dict(worker))
    return path


def load_latest_chat(data_dir: Path, worker_id: str) -> ChatSession | None:
    folder = data_dir.resolve() / "chats"
    if not folder.is_dir():
        return None
    matches: list[ChatSession] = []
    for file_path in folder.glob("chat-*.json"):
        try:
            session = ChatSession.from_dict(json.loads(file_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if session.worker_id == worker_id:
            matches.append(session)
    if not matches:
        return None
    matches.sort(key=lambda item: item.updated_at, reverse=True)
    return matches[0]


def export_chat_markdown(
    data_dir: Path, session: ChatSession, currency: str, redact: Callable[[str], str]
) -> Path:
    lines = [
        f"# 单 Worker 对话 · {session.worker_name} (@{session.worker_id})",
        f"模型：{session.model} · 会话 ID：{session.id} · 轮数：{len(session.turns)}",
    ]
    for index, turn in enumerate(session.turns, start=1):
        lines.append(f"## 第 {index} 轮 · 用户\n\n{turn.user}")
        if turn.tool_steps:
            step_lines = [
                f"- Step {s['step']} `{s['name']}` ({s['status']}, {s.get('duration_ms', 0)}ms): "
                f"`{s.get('arguments', '')}`"
                for s in turn.tool_steps
            ]
            lines.append("### 工具调用\n\n" + "\n".join(step_lines))
        body = turn.assistant or "（无输出）"
        if turn.error:
            body += f"\n\n错误：{turn.error}"
        lines.append(f"## 第 {index} 轮 · {session.worker_name} ({turn.status})\n\n{body}")
    usage = session.usage
    cost = "费用待配置" if usage.cost is None else f"{usage.cost:.4f} {currency}"
    lines.append(f"总 Token：{usage.total_tokens} · {cost}")
    folder = data_dir.resolve() / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"chat-{session.worker_id}-{new_id()}.md"
    target.write_text(redact("\n\n".join(lines)) + "\n", encoding="utf-8")
    return target


async def run_chat_turn(
    session: ChatSession,
    worker: Worker,
    settings: Settings,
    user_text: str,
    *,
    client: ModelClient | None = None,
    on_update: Callable[[], None] | None = None,
) -> ChatTurn:
    """在指定 Worker 上执行一轮对话，实时流式更新文本与工具步骤。"""
    validate_builtin_tools(worker.builtin_tools)
    unknown_mcp = set(worker.mcp_servers) - settings.mcp_servers.keys()
    if unknown_mcp:
        raise ValueError(f"Worker {worker.id} 引用了未登记的 MCP：{', '.join(sorted(unknown_mcp))}")

    messages = session.build_messages(worker, settings, user_text)
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
        if not has_configured_tools(worker):
            reply = await SingleRequestRuntime(active_client).execute(
                worker.model, messages, on_text
            )
        else:
            async with CompositeTools(settings, worker) as tools:
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
        save_chat_session(settings.data_dir, session, worker)
        if on_update:
            on_update()
    return turn
