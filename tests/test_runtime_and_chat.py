"""多步 MCP 工具并行调用、三级配额渐进收敛、内存态单聊与 @architect 隔离回滚测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ctflow.architect import ArchitectTools, get_architect_worker
from ctflow.chat import ChatSession, extract_mermaid_source, run_chat_turn
from ctflow.config import Settings
from ctflow.models import ARCHITECT_ID, Node, Usage, Worker, Workflow
from ctflow.provider import Reply
from ctflow.runtime import ToolRuntime
from ctflow.storage import Store

MCP_SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def make_settings(tmp_path: Path, *, max_rounds: int = 3, with_mcp: bool = False) -> Settings:
    servers = {}
    if with_mcp:
        servers["echo"] = {
            "command": sys.executable,
            "args": [str(MCP_SERVER)],
        }
    return Settings(
        tmp_path / "config" / "config.json",
        tmp_path / "data",
        tmp_path,
        max_tool_rounds=max_rounds,
        mcp_servers=servers,
    )


async def test_parallel_tool_calls_error_recovery_and_budget_warning_convergence(tmp_path: Path):
    """验证同轮并行调用、参数错误自愈、倒数第 1 轮预警与配额耗尽强制无工具总结。"""
    settings = make_settings(tmp_path, max_rounds=2, with_mcp=True)
    worker = Worker("agent", "多步助手", "test-model", mcp_servers=["echo"])

    concurrent_active = 0
    concurrent_peak = 0

    class ParallelDummyTools:
        schemas = [
            {
                "name": "mcp_echo_slow",
                "description": "并发测试工具",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]

        async def call(self, name: str, arguments: dict) -> str:
            nonlocal concurrent_active, concurrent_peak
            if arguments.get("q") == "bad":
                raise ValueError("模拟参数错误")
            concurrent_active += 1
            concurrent_peak = max(concurrent_peak, concurrent_active)
            await asyncio.sleep(0.03)
            concurrent_active -= 1
            return f"结果:{arguments['q']}"

    class BudgetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict], list[dict] | None]] = []

        def append_tool_round(self, messages, reply, results):
            messages.append({"role": "assistant", "content": reply.content or None})
            for call, res in results:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": res})

        async def stream(self, model, messages, on_text, tools=None):
            self.calls.append((list(messages), tools))
            turn_idx = len(self.calls)
            if turn_idx == 1:
                # 第 1 轮（共 2 轮配额）：同时发 2 个正常调用 + 1 个错误调用
                assert tools is not None
                return Reply(
                    tool_calls=[
                        {"id": "c1", "name": "mcp_echo_slow", "arguments": '{"q":"k1"}'},
                        {"id": "c2", "name": "mcp_echo_slow", "arguments": '{"q":"k2"}'},
                        {"id": "c3", "name": "mcp_echo_slow", "arguments": '{"q":"bad"}'},
                    ],
                    usage=Usage(10, 4),
                    status="tool_calls",
                )
            if turn_idx == 2:
                # 仅剩最后 1 轮工具机会：收到 <budget_warning remaining_rounds="1">
                assert '<budget_warning remaining_rounds="1">' in messages[-1]["content"]
                assert "模拟参数错误" in messages[-1]["content"]
                # 模型在第 2 轮仍选择调用最后一次工具（耗尽配额）
                return Reply(
                    tool_calls=[
                        {"id": "c4", "name": "mcp_echo_slow", "arguments": '{"q":"final"}'}
                    ],
                    usage=Usage(12, 3),
                    status="tool_calls",
                )
            # 第 3 次请求为配额耗尽后的无工具收敛轮：tools 为 None 且注入 <budget_exhausted>
            assert tools is None
            assert '<budget_exhausted used_rounds="2">' in messages[-1]["content"]
            summary = "已综合 k1、k2 与 final 输出最终结论。"
            on_text(summary)
            return Reply(summary, usage=Usage(15, 8), status="completed")

    client = BudgetClient()
    reply = await ToolRuntime(client, settings.max_tool_rounds).execute(
        worker.model,
        [{"role": "user", "content": "开始调研"}],
        lambda _: None,
        ParallelDummyTools(),
    )
    assert concurrent_peak == 2  # 验证同轮多个工具真正通过 asyncio.gather 并行执行
    assert reply.content == "已综合 k1、k2 与 final 输出最终结论。"
    assert reply.usage.total_tokens == 52
    assert len(reply.tool_steps) == 4
    assert reply.tool_steps[2]["status"] == "error"


async def test_in_memory_chat_multi_turn_mcp_and_no_disk_files(tmp_path: Path):
    settings = make_settings(tmp_path, with_mcp=True)
    worker = Worker(
        "helper",
        "本地助手",
        "m-chat",
        prompt="你是简洁助手。",
        api_key="super-secret-chat-key",
        mcp_servers=["echo"],
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
            if "调用回显" in last_user and not any(m.get("role") == "tool" for m in messages):
                return Reply(
                    tool_calls=[
                        {"id": "c1", "name": "mcp_echo_echo", "arguments": '{"text":"hello"}'}
                    ],
                    usage=Usage(5, 2, 0.001),
                    status="tool_calls",
                )
            if any(m.get("role") == "tool" for m in messages):
                answer = "工具返回：MCP: hello"
            else:
                answer = f"第二轮总结：{last_user}（密钥 super-secret-chat-key）"
            on_text(answer)
            return Reply(answer, usage=Usage(10, 5, 0.002), status="completed")

    fake = ChatFakeClient()
    turn1 = await run_chat_turn(session, worker, settings, "请调用回显", client=fake)
    assert turn1.status == "success"
    assert "<execution_budget" in fake.seen_messages[0][0]["content"]
    assert "<worker_role" in fake.seen_messages[0][0]["content"]
    assert turn1.tool_steps[0]["name"] == "mcp_echo_echo"
    assert "MCP: hello" in turn1.assistant

    turn2 = await run_chat_turn(session, worker, settings, "继续追问", client=fake)
    assert turn2.status == "success"
    assert "super-secret-chat-key" not in turn2.assistant
    assert "[密钥已隐藏]" in turn2.assistant
    assert [m["role"] for m in fake.seen_messages[-1]] == ["system", "user", "assistant", "user"]
    # 验证单聊仅驻留内存，绝不在磁盘生成 chats 目录
    assert not (settings.data_dir / "chats").exists()


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


async def test_architect_isolation_inventory_backup_and_mermaid_extract(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir, backup_dir=settings.backup_dir)
    store.save_worker(Worker("judge", "裁判", "qwen-max", base_url="https://api.example/v1"))
    store.save_workflow(Workflow("demo", "示例流", "任务", [Node("A", "起点", "judge")]))

    # 1. @architect 不保存在 workers/ 目录，且普通 Worker 禁止占用 architect ID
    with pytest.raises(ValueError, match="保留标识"):
        store.save_worker(Worker(ARCHITECT_ID, "伪造", "m1"))
    arch = get_architect_worker(settings, store)
    assert arch.id == ARCHITECT_ID
    assert '<worker id="judge"' in arch.prompt
    assert '<workflow id="demo"' in arch.prompt
    assert not (store.data_dir / "workers" / "worker-architect.json").exists()

    # 2. ArchitectTools 在执行写类工具前自动在隔离区创建备份快照
    tools = ArchitectTools(settings, store, arch)

    class FakeMCP:
        async def call(self, name: str, arguments: dict) -> str:
            (store.data_dir / "workflows" / "workflow-demo.json").write_text(
                "{corrupted json", encoding="utf-8"
            )
            return "written"

    tools._mcp = FakeMCP()  # type: ignore[assignment]
    await tools.call("mcp_architect_fs_write_file", {"path": "workflow-demo.json"})
    assert list(settings.backup_dir.glob("snap-*"))

    # 坏文件被自动跳过不崩主程序，且 rollback_latest 一键完整还原
    assert store.list_workflows() == []
    assert "workflows/workflow-demo.json" in store.corrupted_files
    restored_msg = store.rollback_latest()
    assert "1 套工作流" in restored_msg
    assert store.get_workflow("demo").name == "示例流"
    assert store.corrupted_files == []

    # 3. 提取回复中的 mermaid 代码块
    md_reply = '好的，为你设计如下：\n\n```mermaid\nflowchart TD\n    A["审核 @judge"]\n```\n'
    assert extract_mermaid_source(md_reply) == 'flowchart TD\n    A["审核 @judge"]'
