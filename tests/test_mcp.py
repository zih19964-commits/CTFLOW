"""MCP stdio 工具的连接、调用与节点集成回归。"""

import sys
from pathlib import Path

import pytest

from ctflow.config import Settings
from ctflow.engine import Engine
from ctflow.mcp_tools import MCPTools
from ctflow.models import Node, Usage, Worker, Workflow
from ctflow.provider import Reply
from ctflow.runtime import ToolRuntime
from ctflow.storage import Store

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def mcp_settings(tmp_path):
    return Settings(
        tmp_path / "config.json",
        tmp_path / "data",
        tmp_path,
        mcp_servers={
            "echo": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
            }
        },
    )


def echo_worker():
    return Worker("worker", "测试", "test-model", mcp_servers=["echo"])


async def test_mcp_stdio_lists_and_calls_tool(tmp_path):
    async with MCPTools(mcp_settings(tmp_path), echo_worker()) as tools:
        assert [item["name"] for item in tools.schemas] == ["mcp_echo_echo"]
        assert tools.schemas[0]["parameters"]["properties"]["text"]["type"] == "string"
        assert await tools.call("mcp_echo_echo", {"text": "hello"}) == "MCP: hello"


class ToolClient:
    def __init__(self):
        self.calls = []

    def append_tool_round(self, messages, reply, results):
        messages.append(
            {
                "role": "assistant",
                "content": reply.content or None,
                "tool_calls": reply.tool_calls,
            }
        )
        for call, result in results:
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    async def stream(self, model, messages, on_text, tools=None):
        self.calls.append((list(messages), tools))
        if len(self.calls) == 1:
            return Reply(
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "mcp_echo_echo",
                        "arguments": '{"text":"from-runtime"}',
                    }
                ],
                usage=Usage(5, 1),
                status="tool_calls",
            )
        assert tools and tools[0]["name"] == "mcp_echo_echo"
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["content"] == "MCP: from-runtime"
        return Reply("工具结果已处理", usage=Usage(2, 2), status="completed")


async def test_tool_runtime_executes_mcp_and_sums_usage(tmp_path):
    events = []
    client = ToolClient()
    async with MCPTools(mcp_settings(tmp_path), echo_worker()) as tools:
        result = await ToolRuntime(client, 4, lambda *event: events.append(event)).execute(
            "test-model", [{"role": "user", "content": "调用工具"}], lambda text: None, tools
        )
    assert result.content == "工具结果已处理"
    assert result.usage.total_tokens == 10
    assert events[0][:2] == ("start", "mcp_echo_echo")
    assert events[1][:2] == ("end", "mcp_echo_echo")
    assert len(client.calls) == 2


async def test_engine_runs_node_with_mcp_tool(tmp_path):
    settings = mcp_settings(tmp_path)
    store = Store(settings.data_dir)
    try:
        store.save_worker(echo_worker())
        workflow = Workflow("mcp-flow", "MCP", "调用 echo 工具", [Node("A", "A", "worker")])
        store.save_workflow(workflow)
        engine = Engine(store, settings, ToolClient())
        run = engine.create_run(workflow)
        await engine.execute(run)
        assert run.status == "success"
        assert run.nodes["A"].output == "工具结果已处理"
        assert any("MCP 工具完成" in item["message"] for item in run.nodes["A"].logs)
    finally:
        store.close()


def test_engine_rejects_unknown_mcp_name(tmp_path):
    settings = mcp_settings(tmp_path)
    store = Store(settings.data_dir)
    try:
        store.save_worker(Worker("worker", "测试", "test-model", mcp_servers=["missing"]))
        workflow = Workflow("bad-mcp", "MCP", "任务", [Node("A", "A", "worker")])
        store.save_workflow(workflow)
        with pytest.raises(ValueError, match="未登记的 MCP"):
            Engine(store, settings, ToolClient()).create_run(workflow)
    finally:
        store.close()
