"""标准 argparse 命令行；结果走 stdout，进展与统计走 stderr。"""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from rich.console import Console

from ctflow.config import Settings, load_settings
from ctflow.engine import Engine
from ctflow.examples import seed_examples
from ctflow.mcp_tools import MCPTools
from ctflow.models import Node, Run, Workflow, new_id
from ctflow.provider import ModelClient
from ctflow.storage import Store

err = Console(stderr=True, markup=False, highlight=False)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ctflow", description="本地多 Worker 工作流终端")
    result.add_argument("--config", type=Path, help="配置文件路径（默认 config/config.json）")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("init", help="初始化本地配置、数据库和示例 Worker/工作流")
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
    doctor = commands.add_parser("doctor", help="检查配置；--live 额外进行一次实际模型请求")
    doctor.add_argument("--worker", help="检查指定 Worker 的独立连接")
    doctor.add_argument("--live", action="store_true")
    return result


def initialize_config(path: Path) -> None:
    if path.exists():
        return
    example = Settings(
        config_path=path,
        data_dir=Path("../data"),
        workspace=Path(".."),
    )
    data = asdict(example)
    data.pop("config_path")
    data["data_dir"] = "../data"
    data["workspace"] = ".."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


async def execute_cli(args: argparse.Namespace, store: Store, settings: Settings) -> int:
    if args.command == "models":
        worker = store.get_worker(args.worker)
        async with ModelClient(settings, worker) as client:
            print("\n".join(await client.list_models()))
        return 0
    if args.command == "doctor":
        print(settings.safe_summary())
        worker = store.get_worker(args.worker) if args.worker else None
        targets = [worker] if worker else store.list_workers()
        for target in targets:
            print(
                f"Worker {target.id} · {target.model} ({target.protocol}) · "
                f"接口：{'已填写' if target.base_url else '待配置'} · "
                f"API Key：{'已填写' if target.api_key else '待配置'} · "
                f"内置工具：{', '.join(target.builtin_tools) or '无'} · "
                f"MCP：{', '.join(target.mcp_servers) or '无'}"
            )
        if args.live:
            if worker is None:
                raise ValueError("真实连接检查请指定 --worker <id>")
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
        worker = store.get_worker(args.worker)
        if not worker.mcp_servers:
            print(f"Worker {worker.id} 未装配 MCP 服务器")
            return 0
        async with MCPTools(settings, worker) as tools:
            for schema in tools.schemas:
                print(f"{schema['name']}\t{schema.get('description', '')}")
        return 0
    engine = Engine(store, settings, on_event=report_event)
    if args.command == "worker":
        worker = store.get_worker(args.worker)
        workflow = Workflow(
            new_id(),
            f"独立运行 · {worker.name}",
            args.task,
            [Node("A", worker.name, worker.id)],
        )
    else:
        workflow = store.get_workflow(args.workflow)
    run = engine.create_run(workflow, args.task)
    await engine.execute(run)
    return report_result(run, settings)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = None
    settings = None
    try:
        if args.command == "init":
            initialize_config((args.config or Path.cwd() / "config" / "config.json").resolve())
        settings = load_settings(args.config)
        store = Store(settings.data_dir)
        seed_examples(store, settings.model)
        if args.command == "init":
            print(f"初始化完成。配置：{settings.config_path}\n数据：{settings.data_dir}")
            print("运行 ctflow 打开工作台；运行 ctflow run review 开始示例。")
            return 0
        if args.command is None or (args.command == "run" and args.tui):
            from ctflow.ui import CTFlowApp

            workflows = store.list_workflows()
            workflow = (
                store.get_workflow(args.workflow)
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
        if store:
            for worker in store.list_workers():
                message = worker.redact(message)
        err.print(f"错误：{message}")
        return 2
    finally:
        if store:
            store.close()
