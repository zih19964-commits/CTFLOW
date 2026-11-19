"""按标准 MCP 协议建立 stdio / http 工具会话并暴露给节点与单聊。"""

from __future__ import annotations

import re
import tempfile
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ctflow.config import Settings
from ctflow.models import Worker

_TOOL_ID = re.compile(r"[^A-Za-z0-9_-]")


class MCPTools:
    def __init__(
        self,
        settings: Settings,
        worker: Worker,
        *,
        extra_servers: dict[str, dict] | None = None,
    ) -> None:
        self.settings = settings
        self.worker = worker
        self.extra_servers = extra_servers or {}
        self.stack = AsyncExitStack()
        self.schemas: list[dict] = []
        self._calls: dict[str, tuple[ClientSession, str]] = {}

    async def __aenter__(self) -> MCPTools:
        catalog = {**self.settings.mcp_servers, **self.extra_servers}
        try:
            for server_name in self.worker.mcp_servers:
                if server_name not in catalog:
                    raise ValueError(f"未登记的 MCP 服务器：{server_name}")
                config = catalog[server_name]
                session = await self._connect(config)
                listing = await session.list_tools()
                for tool in listing.tools:
                    name = _TOOL_ID.sub("_", f"mcp_{server_name}_{tool.name}")
                    if len(name) > 64 or name in self._calls:
                        raise ValueError(f"MCP 工具名称冲突或超过 64 字符：{name}")
                    self._calls[name] = (session, tool.name)
                    self.schemas.append(
                        {
                            "name": name,
                            "description": tool.description or tool.name,
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        }
                    )
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def _connect(self, config: dict) -> ClientSession:
        if config["transport"] == "stdio":
            # MCP 服务器的 stderr 写入临时文件，防止污染 Textual 全屏画布。
            errlog = self.stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
            transport = await self.stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config["command"],
                        args=config.get("args", []),
                        env=config.get("env"),
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
        session, original_name = self._calls[name]
        result = await session.call_tool(original_name, arguments)
        text = "\n".join(
            item.text for item in result.content if getattr(item, "type", "") == "text"
        )
        if result.isError:
            raise ValueError(f"MCP 工具执行失败：{text[:1000]}")
        return (text or str(result.content))[:20000]

    async def __aexit__(self, *exc: object) -> None:
        await self.stack.aclose()
