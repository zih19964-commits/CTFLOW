"""安全的工作区内置工具与组合工具提供器（内置工具 + MCP 服务器）。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from ctflow.config import Settings
from ctflow.mcp_tools import MCPTools
from ctflow.models import Worker

_IGNORED_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "data"}
)
_MAX_OUTPUT_CHARS = 20_000
_MAX_SEARCH_FILE_BYTES = 512 * 1024

BUILTIN_TOOL_CATALOG: dict[str, dict] = {
    "list_dir": {
        "label": "列目录 list_dir",
        "description": "列出工作区指定目录下的文件和子目录（自动跳过敏感和缓存目录）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作区的目录路径，默认为 '.'",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "最多返回条目数（1-200，默认 80）",
                },
            },
        },
    },
    "read_file": {
        "label": "读文件 read_file",
        "description": "按行范围读取工作区内的 UTF-8 文本文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作区的文件路径",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始，默认 1）",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（含，默认 start_line + 199，单次最多 400 行）",
                },
            },
            "required": ["path"],
        },
    },
    "search_text": {
        "label": "搜文本 search_text",
        "description": "在工作区文本文件中搜索关键词，返回匹配的文件路径与行号",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的文本关键词（不区分大小写）",
                },
                "path": {
                    "type": "string",
                    "description": "相对工作区的搜索目录或文件路径，默认为 '.'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回匹配行数（1-100，默认 30）",
                },
            },
            "required": ["query"],
        },
    },
    "write_file": {
        "label": "写文件 write_file",
        "description": "在工作区内创建或覆盖 UTF-8 文本文件（需显式开启）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作区的目标文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def validate_builtin_tools(names: list[str]) -> None:
    unknown = set(names) - BUILTIN_TOOL_CATALOG.keys()
    if unknown:
        raise ValueError(f"未知的内置工具：{', '.join(sorted(unknown))}")


def has_configured_tools(worker: Worker) -> bool:
    return bool(worker.builtin_tools or worker.mcp_servers)


def _safe_path(workspace: Path, raw_path: str) -> Path:
    root = workspace.resolve()
    rel = (raw_path or ".").strip()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"路径越出工作区边界：{raw_path}")
    relative = candidate.relative_to(root)
    if any(part in _IGNORED_DIRS for part in relative.parts):
        raise ValueError(f"禁止访问受保护目录：{raw_path}")
    return candidate


def _run_list_dir(workspace: Path, arguments: dict) -> str:
    target = _safe_path(workspace, str(arguments.get("path", ".")))
    if not target.exists():
        raise ValueError(f"目录不存在：{arguments.get('path', '.')}")
    if not target.is_dir():
        raise ValueError(f"目标不是目录：{arguments.get('path', '.')}")
    limit = max(1, min(200, int(arguments.get("max_entries", 80))))
    entries: list[str] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        if child.name in _IGNORED_DIRS:
            continue
        if child.is_dir():
            entries.append(f"[dir]  {child.name}/")
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append(f"[file] {child.name} ({size} B)")
        if len(entries) >= limit:
            entries.append(f"...（已截断，最多显示 {limit} 项）")
            break
    return ("\n".join(entries) or "（空目录）")[:_MAX_OUTPUT_CHARS]


def _run_read_file(workspace: Path, arguments: dict) -> str:
    raw_path = str(arguments.get("path", "")).strip()
    if not raw_path:
        raise ValueError("read_file 需要非空 path 参数")
    target = _safe_path(workspace, raw_path)
    if not target.is_file():
        raise ValueError(f"文件不存在：{raw_path}")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"文件过大（超过 2MB），请改用 search_text 定位：{raw_path}")
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 文本：{raw_path}") from exc
    total = len(lines)
    start = max(1, int(arguments.get("start_line", 1)))
    default_end = min(total, start + 199)
    end = int(arguments.get("end_line", default_end))
    end = min(total, max(start, min(end, start + 399)))
    if start > total:
        return f"{raw_path} 共 {total} 行，请求的起始行 {start} 超出范围。"
    body = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
    header = f"# {raw_path} (第 {start}-{end} 行 / 共 {total} 行)\n"
    return (header + body)[:_MAX_OUTPUT_CHARS]


def _run_search_text(workspace: Path, arguments: dict) -> str:
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("search_text 需要非空 query 参数")
    target = _safe_path(workspace, str(arguments.get("path", ".")))
    if not target.exists():
        raise ValueError(f"路径不存在：{arguments.get('path', '.')}")
    limit = max(1, min(100, int(arguments.get("max_results", 30))))
    root = workspace.resolve()
    needle = query.casefold()
    hits: list[str] = []

    def scan_file(file_path: Path) -> None:
        if len(hits) >= limit:
            return
        try:
            if file_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                return
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        rel = file_path.relative_to(root).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle in line.casefold():
                hits.append(f"{rel}:{line_no}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    return

    if target.is_file():
        scan_file(target)
    else:
        for file_path in sorted(target.rglob("*")):
            if len(hits) >= limit:
                break
            rel_parts = file_path.relative_to(root).parts
            if any(part in _IGNORED_DIRS for part in rel_parts):
                continue
            if file_path.is_file():
                scan_file(file_path)

    if not hits:
        return f"未找到包含 {query!r} 的文本。"
    suffix = f"\n...（已达上限 {limit} 条）" if len(hits) >= limit else ""
    return ("\n".join(hits) + suffix)[:_MAX_OUTPUT_CHARS]


def _run_write_file(workspace: Path, arguments: dict) -> str:
    raw_path = str(arguments.get("path", "")).strip()
    if not raw_path:
        raise ValueError("write_file 需要非空 path 参数")
    content = str(arguments.get("content", ""))
    target = _safe_path(workspace, raw_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    line_count = content.count("\n") + (1 if content else 0)
    return f"已写入 {raw_path}（{len(content.encode('utf-8'))} 字节，{line_count} 行）"


_BUILTIN_HANDLERS = {
    "list_dir": _run_list_dir,
    "read_file": _run_read_file,
    "search_text": _run_search_text,
    "write_file": _run_write_file,
}


class BuiltinTools:
    """按 Worker 选择暴露的工作区内置工具集。"""

    def __init__(self, workspace: Path, enabled: list[str]) -> None:
        validate_builtin_tools(enabled)
        self.workspace = workspace.resolve()
        self.enabled = list(dict.fromkeys(enabled))
        self.schemas: list[dict] = [
            {
                "name": name,
                "description": BUILTIN_TOOL_CATALOG[name]["description"],
                "parameters": BUILTIN_TOOL_CATALOG[name]["parameters"],
            }
            for name in self.enabled
        ]

    async def call(self, name: str, arguments: dict) -> str:
        handler = _BUILTIN_HANDLERS.get(name)
        if handler is None or name not in self.enabled:
            raise ValueError(f"Worker 未装配内置工具：{name}")
        return await asyncio.to_thread(handler, self.workspace, arguments)


class CompositeTools:
    """统一封装内置工具与外部 MCP 工具，实现 ToolProvider 协议。"""

    def __init__(self, settings: Settings, worker: Worker) -> None:
        self.settings = settings
        self.worker = worker
        self.stack = AsyncExitStack()
        self.schemas: list[dict] = []
        self._builtin: BuiltinTools | None = None
        self._mcp: MCPTools | None = None
        self._builtin_names: set[str] = set()

    async def __aenter__(self) -> CompositeTools:
        try:
            if self.worker.builtin_tools:
                self._builtin = BuiltinTools(self.settings.workspace, self.worker.builtin_tools)
                self._builtin_names = set(self._builtin.enabled)
                self.schemas.extend(self._builtin.schemas)
            if self.worker.mcp_servers:
                self._mcp = await self.stack.enter_async_context(
                    MCPTools(self.settings, self.worker)
                )
                for schema in self._mcp.schemas:
                    if schema["name"] in self._builtin_names:
                        raise ValueError(f"工具名称冲突：{schema['name']}")
                self.schemas.extend(self._mcp.schemas)
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def call(self, name: str, arguments: dict) -> str:
        if self._builtin is not None and name in self._builtin_names:
            return await self._builtin.call(name, arguments)
        if self._mcp is not None:
            return await self._mcp.call(name, arguments)
        raise ValueError(f"Worker 未装配工具：{name}")

    async def __aexit__(self, *exc: object) -> None:
        await self.stack.aclose()
