"""内置工作区工具、组合多步 ToolCall、Anthropic 流式工具参数修复与单 Worker 对话测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from ctflow.builtin_tools import BuiltinTools, CompositeTools
from ctflow.chat import ChatSession, export_chat_markdown, load_latest_chat, run_chat_turn
from ctflow.config import Settings
from ctflow.engine import Engine
from ctflow.models import Node, Usage, Worker, Workflow
from ctflow.provider import ModelClient, Reply
from ctflow.runtime import ToolRuntime
from ctflow.storage import Store

MCP_SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def make_settings(tmp_path: Path, *, with_mcp: bool = False) -> Settings:
    servers = {}
    if with_mcp:
        servers["echo"] = {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(MCP_SERVER)],
        }
    return Settings(
        tmp_path / "config.json",
        tmp_path / "data",
        tmp_path,
        max_tool_rounds=6,
        mcp_servers=servers,
    )


async def test_builtin_tools_workspace_operations_and_sandbox(tmp_path: Path):
    (tmp_path / "notes.md").write_text("第一行\n第二行 CTflow 关键词\n第三行\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.txt").write_text("top-secret", encoding="utf-8")

    tools = BuiltinTools(tmp_path, ["list_dir", "read_file", "search_text", "write_file"])
    listing = await tools.call("list_dir", {"path": "."})
    assert "notes.md" in listing
    assert "data" not in listing

    content = await tools.call("read_file", {"path": "notes.md", "start_line": 2, "end_line": 2})
    assert "2: 第二行 CTflow 关键词" in content

    hits = await tools.call("search_text", {"query": "ctflow"})
    assert "notes.md:2:" in hits
    assert "secret.txt" not in hits

    written = await tools.call("write_file", {"path": "sub/out.txt", "content": "hello\nworld"})
    assert "已写入" in written
    assert (tmp_path / "sub" / "out.txt").read_text(encoding="utf-8") == "hello\nworld"

    with pytest.raises(ValueError, match="越出工作区"):
        await tools.call("read_file", {"path": "../outside.txt"})

    with pytest.raises(ValueError, match="受保护目录"):
        await tools.call("read_file", {"path": "data/secret.txt"})


async def test_composite_tools_and_multistep_tool_error_recovery(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("真实内容 ABC", encoding="utf-8")
    settings = make_settings(tmp_path, with_mcp=True)
    worker = Worker(
        "agent",
        "多步助手",
        "test-model",
        builtin_tools=["read_file"],
        mcp_servers=["echo"],
    )

    class MultiStepClient:
        def __init__(self) -> None:
            self.round = 0

        def append_tool_round(self, messages, reply, results):
            messages.append({"role": "assistant", "content": reply.content or None})
            for call, result in results:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

        async def stream(self, model, messages, on_text, tools=None):
            self.round += 1
            if self.round == 1:
                # 第 1 步故意传不存在的文件，验证工具错误回填模型自修正。
                return Reply(
                    content="我先尝试读取文件。",
                    tool_calls=[
                        {"id": "c1", "name": "read_file", "arguments": '{"path":"missing.txt"}'}
                    ],
                    usage=Usage(5, 2, 0.001),
                    status="tool_calls",
                )
            if self.round == 2:
                assert "文件不存在" in messages[-1]["content"]
                # 第 2 步同时调内置工具 + MCP 工具。
                return Reply(
                    tool_calls=[
                        {"id": "c2", "name": "read_file", "arguments": '{"path":"readme.txt"}'},
                        {"id": "c3", "name": "mcp_echo_echo", "arguments": '{"text":"ok"}'},
                    ],
                    usage=Usage(6, 3, 0.001),
                    status="tool_calls",
                )
            assert "真实内容 ABC" in messages[-2]["content"]
            assert "MCP: ok" in messages[-1]["content"]
            on_text("汇总完成：真实内容 ABC + MCP: ok")
            return Reply(
                content="汇总完成：真实内容 ABC + MCP: ok",
                usage=Usage(8, 4, 0.001),
                status="completed",
            )

    steps_seen = []
    client = MultiStepClient()
    async with CompositeTools(settings, worker) as tools:
        assert {s["name"] for s in tools.schemas} == {"read_file", "mcp_echo_echo"}
        reply = await ToolRuntime(
            client,
            settings.max_tool_rounds,
            on_tool_step=steps_seen.append,
        ).execute("test-model", [{"role": "user", "content": "读取并回显"}], lambda _: None, tools)

    assert reply.content == "汇总完成：真实内容 ABC + MCP: ok"
    assert reply.usage.total_tokens == 28
    assert len(reply.tool_steps) == 3
    assert reply.tool_steps[0]["status"] == "error"
    assert reply.tool_steps[1]["status"] == "ok"
    assert reply.tool_steps[2]["name"] == "mcp_echo_echo"
    assert any(s["status"] == "running" for s in steps_seen)


async def test_engine_records_builtin_tool_steps_in_node_logs(tmp_path: Path):
    (tmp_path / "info.txt").write_text("来自文件的数据", encoding="utf-8")
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir)
    try:
        worker = Worker("reader", "阅读者", "m1", builtin_tools=["read_file"])
        store.save_worker(worker)
        wf = Workflow("flow1", "读文件流", "读取 info.txt", [Node("A", "读文件", "reader")])
        store.save_workflow(wf)

        class StepClient:
            def __init__(self) -> None:
                self.calls = 0

            def append_tool_round(self, messages, reply, results):
                for call, res in results:
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": res})

            async def stream(self, model, messages, on_text, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return Reply(
                        tool_calls=[
                            {"id": "t1", "name": "read_file", "arguments": '{"path":"info.txt"}'}
                        ],
                        usage=Usage(3, 2),
                        status="tool_calls",
                    )
                assert "来自文件的数据" in messages[-1]["content"]
                on_text("已读取：来自文件的数据")
                return Reply("已读取：来自文件的数据", usage=Usage(4, 3), status="completed")

        engine = Engine(store, settings, client=StepClient())
        run = engine.create_run(wf)
        await engine.execute(run)
        assert run.status == "success"
        step_logs = [item for item in run.nodes["A"].logs if item.get("type") == "tool_step"]
        assert len(step_logs) == 1
        assert step_logs[0]["name"] == "read_file"
        assert step_logs[0]["status"] == "ok"
        assert "来自文件的数据" in step_logs[0]["result"]
    finally:
        store.close()


async def test_anthropic_stream_tool_use_input_json_delta_is_valid_json(tmp_path: Path):
    """回归：Anthropic 空 input:{} + input_json_delta 不应拼出非法 '{}{...}'。"""
    settings = make_settings(tmp_path)
    worker = Worker(
        "claude",
        "Claude",
        "claude-sonnet",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        protocol="anthropic",
    )
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 6},
        },
    ]
    wire = "".join(f"data: {json.dumps(item)}\n\n" for item in events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = ModelClient(settings, worker)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        reply = await client.stream(
            "claude-sonnet",
            [{"role": "user", "content": "读取 README"}],
            lambda _: None,
            tools=[
                {
                    "name": "read_file",
                    "description": "读文件",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                }
            ],
        )
    assert reply.status == "tool_calls"
    assert len(reply.tool_calls) == 1
    assert json.loads(reply.tool_calls[0]["arguments"]) == {"path": "README.md"}


async def test_single_worker_chat_multi_turn_tools_persistence_and_export(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("你好世界", encoding="utf-8")
    settings = make_settings(tmp_path)
    worker = Worker(
        "helper",
        "本地助手",
        "m-chat",
        prompt="你是简洁的本地助手。",
        api_key="super-secret-chat-key",
        builtin_tools=["read_file"],
    )
    session = ChatSession("sess1", worker.id, worker.name, worker.model)

    class ChatFakeClient:
        def __init__(self) -> None:
            self.seen_messages: list[list[dict]] = []

        def append_tool_round(self, messages, reply, results):
            for call, res in results:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": res})

        async def stream(self, model, messages, on_text, tools=None):
            self.seen_messages.append(list(messages))
            last_user = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
            if "读文件" in last_user and not any(m.get("role") == "tool" for m in messages):
                return Reply(
                    tool_calls=[
                        {"id": "c1", "name": "read_file", "arguments": '{"path":"hello.txt"}'}
                    ],
                    usage=Usage(5, 2, 0.001),
                    status="tool_calls",
                )
            if any(m.get("role") == "tool" for m in messages):
                answer = "文件内容是：你好世界"
            else:
                answer = f"收到第二轮：{last_user}（含密钥测试 super-secret-chat-key）"
            on_text(answer)
            return Reply(answer, usage=Usage(10, 5, 0.002), status="completed")

    fake = ChatFakeClient()
    turn1 = await run_chat_turn(session, worker, settings, "帮我读文件 hello.txt", client=fake)
    assert turn1.status == "success"
    assert len(turn1.tool_steps) == 1
    assert turn1.tool_steps[0]["name"] == "read_file"
    assert "你好世界" in turn1.assistant

    turn2 = await run_chat_turn(session, worker, settings, "总结刚才的内容", client=fake)
    assert turn2.status == "success"
    assert "super-secret-chat-key" not in turn2.assistant
    assert "[密钥已隐藏]" in turn2.assistant
    # 第二轮请求应包含第一轮上下文
    roles = [m["role"] for m in fake.seen_messages[-1]]
    assert roles == ["system", "user", "assistant", "user"]

    loaded = load_latest_chat(settings.data_dir, "helper")
    assert loaded is not None
    assert len(loaded.turns) == 2
    assert loaded.usage.total_tokens == 37

    exported = export_chat_markdown(settings.data_dir, loaded, settings.currency, worker.redact)
    text = exported.read_text(encoding="utf-8")
    assert "read_file" in text
    assert "super-secret-chat-key" not in text


async def test_chat_turn_cancellation_marks_stopped(tmp_path: Path):
    settings = make_settings(tmp_path)
    worker = Worker("helper", "助手", "m1")
    session = ChatSession("sess2", worker.id, worker.name, worker.model)
    started = asyncio.Event()

    class SlowClient:
        async def stream(self, model, messages, on_text):
            on_text("部分")
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        run_chat_turn(session, worker, settings, "慢请求", client=SlowClient())
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.turns[0].status == "stopped"
    assert session.turns[0].assistant == "部分"
