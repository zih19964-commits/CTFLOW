"""按标准 MCP 协议建立带超时保护与诊断输出的 stdio / http 工具会话。"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from contextlib import AsyncExitStack
from typing import IO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ctflow.config import Settings
from ctflow.models import Worker

_TOOL_ID = re.compile(r"[^A-Za-z0-9_-]")
_PASSTHROUGH_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "NPM_CONFIG_REGISTRY",
    "NPM_CONFIG_CACHE",
    "UV_CACHE_DIR",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "PYTHONUTF8",
)


def _merged_stdio_env(configured: dict[str, str] | None) -> dict[str, str] | None:
    """透传代理与包管理器缓存/镜像环境变量，避免 npx / uvx 子进程联网拉包卡死。"""
    inherited = {k: v for k in _PASSTHROUGH_ENV_VARS if (v := os.environ.get(k))}
    if not inherited and configured is None:
        return None
    return {**inherited, **(configured or {})}


def _read_stderr_tail(errlog: IO[str] | None, limit: int = 600) -> str:
    if errlog is None:
        return ""
    try:
        errlog.flush()
        errlog.seek(0)
        text = errlog.read().strip()
    except OSError:
        return ""
    return text[-limit:] if text else ""


class MCPTools:
    def __init__(
        self,
        settings: Settings,
        worker: Worker,
        *,
        extra_servers: dict[str, dict] | None = None,
        tool_filter: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.settings = settings
        self.worker = worker
        self.extra_servers = extra_servers or {}
        self.tool_filter = set(tool_filter) if tool_filter is not None else None
        self.stack = AsyncExitStack()
        self.schemas: list[dict] = []
        self._calls: dict[str, tuple[str, ClientSession, str]] = {}
        self._errlogs: dict[str, IO[str]] = {}

    async def __aenter__(self) -> MCPTools:
        catalog = {**self.settings.mcp_servers, **self.extra_servers}
        current_server = ""
        try:
            for server_name in self.worker.mcp_servers:
                current_server = server_name
                if server_name not in catalog:
                    raise ValueError(f"未登记的 MCP 服务器：{server_name}")
                config = catalog[server_name]
                async with asyncio.timeout(self.settings.timeout):
                    session = await self._connect(server_name, config)
                    listing = await session.list_tools()
                for tool in listing.tools:
                    if self.tool_filter is not None and tool.name not in self.tool_filter:
                        continue
                    name = _TOOL_ID.sub("_", f"mcp_{server_name}_{tool.name}")
                    if len(name) > 64 or name in self._calls:
                        raise ValueError(f"MCP 工具名称冲突或超过 64 字符：{name}")
                    self._calls[name] = (server_name, session, tool.name)
                    self.schemas.append(
                        {
                            "name": name,
                            "description": tool.description or tool.name,
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        }
                    )
            return self
        except TimeoutError as exc:
            detail = _read_stderr_tail(self._errlogs.get(current_server))
            suffix = f"（stderr：{detail}）" if detail else ""
            await self.stack.aclose()
            raise ValueError(
                f"连接 MCP 服务器 {current_server} 超过 {self.settings.timeout:g} 秒{suffix}"
            ) from exc
        except BaseException as exc:
            detail = _read_stderr_tail(self._errlogs.get(current_server))
            await self.stack.aclose()
            if detail and isinstance(exc, Exception) and not isinstance(exc, ValueError):
                raise ValueError(
                    f"启动 MCP 服务器 {current_server} 失败：{exc}（stderr：{detail}）"
                ) from exc
            raise

    async def _connect(self, server_name: str, config: dict) -> ClientSession:
        if config["transport"] == "stdio":
            # MCP 服务器的 stderr 写入临时文件隔离 Textual 画布，连接异常时提取尾部日志诊断。
            errlog = self.stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
            self._errlogs[server_name] = errlog
            transport = await self.stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config["command"],
                        args=config.get("args", []),
                        env=_merged_stdio_env(config.get("env")),
                        cwd=str(self.settings.workspace),
                    ),
                    errlog=errlog,
                )
            )
            read, write = transport
        else:
            read, write, _ = await self.stack.enter_async_context(
                streamablehttp_client(config["url"], headers=config.get("headers"))
            )
        session = await self.stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def call(self, name: str, arguments: dict) -> str:
        if name not in self._calls:
            raise ValueError(f"Worker 未装配 MCP 工具：{name}")
        server_name, session, original_name = self._calls[name]
        try:
            async with asyncio.timeout(self.settings.timeout):
                result = await session.call_tool(original_name, arguments)
        except TimeoutError as exc:
            detail = _read_stderr_tail(self._errlogs.get(server_name))
            suffix = f"（stderr：{detail}）" if detail else ""
            raise ValueError(
                f"MCP 工具 {name} 执行超过 {self.settings.timeout:g} 秒{suffix}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            detail = _read_stderr_tail(self._errlogs.get(server_name))
            suffix = f"（stderr：{detail}）" if detail else ""
            raise ValueError(f"MCP 工具 {name} 通信异常：{exc}{suffix}") from exc
        text = "\n".join(
            item.text for item in result.content if getattr(item, "type", "") == "text"
        )
        if result.isError:
            raise ValueError(f"MCP 工具执行失败：{text[:1000]}")
        return (text or str(result.content))[:20000]

    async def __aexit__(self, *exc: object) -> None:
        await self.stack.aclose()
