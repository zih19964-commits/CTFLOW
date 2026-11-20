"""标准命令行入口：支持跨目录直调、@workflow 一行简写、stdout/stderr 分离与一键回滚。"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from rich.console import Console

from ctflow.architect import ArchitectTools, get_architect_worker
from ctflow.config import PROJECT_ROOT, Settings, load_settings, resolve_config_path
from ctflow.engine import Engine
from ctflow.examples import seed_examples
from ctflow.mcp_tools import MCPTools
from ctflow.models import ARCHITECT_ID, Node, Run, Workflow, new_id
from ctflow.provider import ModelClient
from ctflow.storage import Store

err = Console(stderr=True, markup=False, highlight=False)


_SUBCOMMANDS = frozenset(
    {"init", "run", "worker", "list", "show", "models", "mcp", "doctor", "rollback"}
)


def _expand_shorthand(argv: list[str]) -> list[str]:
    """将 `@<workflow> [task]`、`@@<worker> <task>` 或直接 `<workflow> <task>` 展开为子命令。"""
    if not argv:
        return argv
    idx = 0
    prefix: list[str] = []
    if argv[0] == "--config" and len(argv) >= 2:
        prefix = argv[:2]
        idx = 2
    elif argv[0].startswith("--config="):
        prefix = [argv[0]]
        idx = 1
    if idx >= len(argv):
        return argv

    token = argv[idx]
    rest = argv[idx + 1 :]
    if token.startswith("@@") and len(token) > 2:
        worker_id = token[2:]
        if not rest:
            return [*prefix, "worker", worker_id]
        if rest[0].startswith("-"):
            return [*prefix, "worker", worker_id, *rest]
        return [*prefix, "worker", worker_id, "--task", " ".join(rest)]
    if token.startswith("@") and len(token) > 1:
        flow_id = token[1:]
        if not rest:
            return [*prefix, "run", flow_id]
        if rest[0].startswith("-"):
            return [*prefix, "run", flow_id, *rest]
        return [*prefix, "run", flow_id, "--task", " ".join(rest)]
    if not token.startswith("-") and token not in _SUBCOMMANDS:
        if not rest:
            return [*prefix, "run", token]
        if rest[0].startswith("-"):
            return [*prefix, "run", token, *rest]
        return [*prefix, "run", token, "--task", " ".join(rest)]
    return argv


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ctflow",
        description='本地多 Worker 依赖图终端（支持 `ctflow @<workflow> "任务"` 一行直调）',
    )
    result.add_argument(
        "--config", type=Path, help="配置文件路径（默认自动寻址 config/config.json）"
    )
    commands = result.add_subparsers(dest="command")
    commands.add_parser("init", help="初始化本地配置、外置扩展目录与示例角色/工作流")
    run = commands.add_parser("run", help="运行已保存的 Workflow")
    run.add_argument("workflow")
    run.add_argument("--task", help="覆盖本次运行的总任务")
    run.add_argument("--tui", action="store_true", help="打开工作台并自动运行")
    worker = commands.add_parser("worker", help="独立运行一个 Worker")
    worker.add_argument("worker")
    worker.add_argument("--task", required=True)
    listing = commands.add_parser("list", help="列出已保存的数据")
    listing.add_argument("kind", choices=["workers", "workflows", "runs"])
    show = commands.add_parser("show", help="查看一次运行的状态和最终结果")
    show.add_argument("run_id")
    models = commands.add_parser("models", help="从指定 Worker 的接口读取可用模型")
    models.add_argument("worker", help="Worker ID 或名称")
    mcp = commands.add_parser("mcp", help="列出 Worker 已装配的 MCP 工具")
    mcp.add_argument("worker", help="Worker ID 或名称")
    doctor = commands.add_parser("doctor", help="检查配置；--live 额外执行一次真实连接请求")
    doctor.add_argument("--worker", help="检查指定 Worker 的独立连接")
    doctor.add_argument("--live", action="store_true")
    commands.add_parser("rollback", help="从最近一份隔离快照恢复 workers 与 workflows 配置")
    return result


def initialize_config(path: Path) -> None:
    folder = path.parent
    is_config_subdir = folder.name == "config"
    if not path.exists():
        data = {
            "model": "qwen3.8-max",
            "models": ["qwen3.8-max", "qwen3.8-flash"],
            "workspace": ".." if is_config_subdir else ".",
            "data_dir": "../data" if is_config_subdir else "data",
            "extensions_dir": "../extensions" if is_config_subdir else "extensions",
            "concurrency": 3,
            "timeout": 60,
            "max_tokens": 4096,
            "max_tool_rounds": 15,
            "currency": "CNY",
            "prices": {},
            "architect": {
                "name": "编排助手",
                "model": "qwen3.8-max",
                "base_url": "",
                "extra_body": {},
            },
        }
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ext_dir = (folder / ("../extensions" if is_config_subdir else "extensions")).resolve()
    (ext_dir / "skills").mkdir(parents=True, exist_ok=True)
    (ext_dir / "mcp").mkdir(parents=True, exist_ok=True)
    mcp_file = ext_dir / "mcp.json"
    example_mcp = PROJECT_ROOT / "extensions" / "mcp.example.json"
    if not mcp_file.exists():
        default_mcp = (
            example_mcp.read_text(encoding="utf-8")
            if example_mcp.is_file()
            else json.dumps({"mcpServers": {}}, ensure_ascii=False, indent=2) + "\n"
        )
        mcp_file.write_text(default_mcp, encoding="utf-8")


def report_event(event: dict) -> None:
    if event["type"] in {"run", "node"}:
        target = event["node_id"] or event["run_id"]
        err.print(f"{target} → {event['status']}")
    elif event["type"] == "log":
        err.print(f"{event['node_id']} · {event['message']}")


def report_result(run: Run, settings: Settings) -> int:
    usage = run.usage
    cost = "待配置价格" if usage.cost is None else f"{usage.cost:.6f} {settings.currency}"
    err.print(
        f"Run {run.id} | {run.status} | 输入 {usage.input_tokens} / "
        f"输出 {usage.output_tokens} / 总计 {usage.total_tokens} Token | 费用估算 {cost}"
    )
    predecessors = {key for node in run.workflow.nodes for key in node.dependencies}
    terminals = [node for node in run.workflow.nodes if node.id not in predecessors]
    for node in terminals:
        state = run.nodes[node.id]
        if state.status == "success":
            if len(terminals) > 1:
                print(f"--- {node.label} ---")
            print(state.output)
    for node_id, state in run.nodes.items():
        if state.error:
            err.print(f"{node_id}: {state.error}")
    return {"success": 0, "stopped": 130}.get(run.status, 1)


def _resolve_worker(store: Store, settings: Settings, identifier: str):
    clean = identifier.lstrip("@")
    if clean.casefold() == ARCHITECT_ID:
        return get_architect_worker(settings, store)
    return store.get_worker(clean)


async def execute_cli(args: argparse.Namespace, store: Store, settings: Settings) -> int:
    if args.command == "models":
        worker = _resolve_worker(store, settings, args.worker)
        async with ModelClient(settings, worker) as client:
            print("\n".join(await client.list_models()))
        return 0
    if args.command == "doctor":
        print(settings.safe_summary())
        npx_state = "已就绪" if shutil.which("npx") else "未安装"
        uvx_state = "已就绪" if shutil.which("uvx") else "未安装"
        print(f"外置工具启动器：npx {npx_state} · uvx {uvx_state}")
        print(f"已登记 MCP 服务器：{', '.join(sorted(settings.mcp_servers)) or '无'}")
        if args.worker:
            targets = [_resolve_worker(store, settings, args.worker)]
        else:
            targets = [
                get_architect_worker(settings, store, with_inventory=False),
                *store.list_workers(),
            ]
        for target in targets:
            print(
                f"Worker {target.id} · {target.name} · {target.model} · "
                f"接口：{'已填写' if target.base_url else '待配置'} · "
                f"API Key：{'已填写' if target.api_key else '待配置'} · "
                f"MCP：{', '.join(target.mcp_servers) or '无'}"
            )
        if store.corrupted_files:
            err.print(f"警告：跳过损坏文件 {', '.join(store.corrupted_files)}")
        if args.live:
            if not args.worker:
                raise ValueError("真实连接检查请指定 --worker <id>")
            worker = targets[0]
            async with ModelClient(settings, worker) as client:
                reply = await client.stream(
                    worker.model,
                    [{"role": "user", "content": "请只回复：CTFlow连接成功"}],
                    lambda text: None,
                )
            if reply.status != "completed" or reply.tool_calls or not reply.content.strip():
                raise ValueError("模型未完整返回结果")
            print(f"真实连接：{reply.content}（{reply.usage.total_tokens} Token）")
        return 0
    if args.command == "mcp":
        worker = _resolve_worker(store, settings, args.worker)
        if not worker.mcp_servers:
            print(f"Worker {worker.id} 未装配 MCP 服务器")
            return 0
        ctx = (
            ArchitectTools(settings, store, worker)
            if worker.id == ARCHITECT_ID
            else MCPTools(settings, worker)
        )
        async with ctx as tools:
            for schema in tools.schemas:
                print(f"{schema['name']}\t{schema.get('description', '')}")
        return 0
    engine = Engine(store, settings, on_event=report_event)
    if args.command == "worker":
        worker = store.get_worker(args.worker.lstrip("@"))
        workflow = Workflow(
            new_id(),
            f"独立运行 · {worker.name}",
            args.task,
            [Node("A", worker.name, worker.id)],
        )
    else:
        workflow = store.get_workflow(args.workflow.lstrip("@"))
    run = engine.create_run(workflow, args.task)
    await engine.execute(run)
    return report_result(run, settings)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    expanded = _expand_shorthand(raw_argv)
    args = parser().parse_args(expanded)
    store: Store | None = None
    settings: Settings | None = None
    try:
        if args.command == "init":
            target_cfg = (args.config or Path.cwd() / "config" / "config.json").resolve()
            initialize_config(target_cfg)
            settings = load_settings(target_cfg)
        else:
            settings = load_settings(resolve_config_path(args.config) if args.config else None)
        store = Store(settings.data_dir, backup_dir=settings.backup_dir)
        seed_examples(store, settings.model)
        if args.command == "init":
            print(
                f"初始化完成。\n配置：{settings.config_path}\n"
                f"扩展：{settings.extensions_dir}\n数据：{settings.data_dir}"
            )
            print('运行 ctflow 打开工作台；运行 ctflow @review "任务" 直接调用工作流。')
            return 0
        if args.command == "rollback":
            restored = store.rollback_latest()
            print(f"已回滚配置快照：{restored}")
            return 0
        if args.command is None or (args.command == "run" and args.tui):
            from ctflow.ui import CTFlowApp

            workflows = store.list_workflows()
            target_id = args.workflow.lstrip("@") if args.command == "run" else "review"
            workflow = (
                store.get_workflow(target_id)
                if args.command == "run"
                else next((item for item in workflows if item.id == "review"), None)
            )
            if workflow and args.command == "run" and args.task is not None:
                workflow.task = args.task
            CTFlowApp(
                store,
                settings,
                workflow=workflow,
                auto_run=args.command == "run",
                start_in_hub=args.command is None,
            ).run()
            return 0
        if args.command == "list":
            entries = getattr(store, f"list_{args.kind}")()
            for item in entries:
                if args.kind == "runs":
                    print(f"{item.id}\t{item.workflow.name}\t{item.status}\t{item.created_at}")
                else:
                    print(f"{item.id}\t{item.name}")
            return 0
        if args.command == "show":
            run = store.get_run(args.run_id)
            return report_result(run, settings)
        return asyncio.run(execute_cli(args, store, settings))
    except KeyboardInterrupt:
        err.print("已停止。运行记录保存在本地历史中。")
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        message = str(exc)
        if store and settings:
            for worker in [
                get_architect_worker(settings, store, with_inventory=False),
                *store.list_workers(),
            ]:
                message = worker.redact(message)
        err.print(f"错误：{message}")
        return 2
    finally:
        if store:
            store.close()
