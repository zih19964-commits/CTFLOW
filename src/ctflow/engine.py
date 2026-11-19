"""以直接依赖任务和 Semaphore 并发调度 DAG，使用闭合 XML 标签协议传导上下游上下文。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable

from ctflow.config import Settings
from ctflow.graph import topological_layers, validate_workflow
from ctflow.mcp_tools import MCPTools
from ctflow.models import Node, NodeRun, Run, Workflow, new_id, now
from ctflow.provider import ModelClient, Reply
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime, ToolRuntime
from ctflow.storage import Store
from ctflow.tags import format_system_context, format_workflow_input
from ctflow.tools import read_skills


class Engine:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        client: ModelClient | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.on_event = on_event
        self.run: Run | None = None
        self.active = False
        self._stopped = False
        self._tasks: dict[str, asyncio.Task] = {}

    def create_run(self, workflow: Workflow, task: str | None = None) -> Run:
        all_workers = {w.id: w for w in self.store.list_workers()}
        validate_workflow(workflow, all_workers)

        workers = {
            node.worker_id: copy.deepcopy(all_workers[node.worker_id]) for node in workflow.nodes
        }
        for worker in workers.values():
            unknown = set(worker.mcp_servers) - self.settings.mcp_servers.keys()
            if unknown:
                raise ValueError(
                    f"Worker {worker.id} 引用了未登记的 MCP：{', '.join(sorted(unknown))}"
                )

        # 创建运行快照时固化所选技能正文，后续外部编辑技能或 Worker 不影响本次运行。
        skill_contexts = {
            wid: read_skills(self.settings.skills_dir, worker.skills)
            for wid, worker in workers.items()
        }
        node_runs = {node.id: NodeRun(node.id) for node in workflow.nodes}
        run_task = workflow.task if task is None else task
        run = Run(
            new_id(),
            copy.deepcopy(workflow),
            workers,
            run_task,
            nodes=node_runs,
            skill_contexts=skill_contexts,
        )
        self.store.save_run(run)
        return run

    def _emit(self, event_type: str, node_id: str | None = None, **values: object) -> None:
        if self.on_event and self.run:
            self.on_event({**values, "type": event_type, "run_id": self.run.id, "node_id": node_id})

    def _log(self, node: NodeRun, message: str, **values: object) -> None:
        entry = {"at": now(), "message": self._redact(message), **values}
        node.logs.append(entry)
        self._emit("log", node.node_id, **entry)

    def _redact(self, text: str) -> str:
        if self.run:
            for worker in sorted(
                self.run.workers.values(), key=lambda item: len(item.api_key), reverse=True
            ):
                text = worker.redact(text)
        return text

    def stop(self) -> None:
        if self.active:
            self._stopped = True
            for task in self._tasks.values():
                task.cancel()

    async def execute(self, run: Run) -> Run:
        if self.active:
            raise ValueError("已有工作流正在执行")
        if run.status != "pending":
            raise ValueError("运行记录已经执行，请创建新的 Run")
        validate_workflow(run.workflow, run.workers)
        if set(run.nodes) != {node.id for node in run.workflow.nodes}:
            raise ValueError("运行快照的节点状态与工作流不一致")
        self.run = run
        self.active = True
        self._stopped = False
        try:
            return await self._execute(run)
        finally:
            self.active = False

    async def _execute(self, run: Run) -> Run:
        run.status = "running"
        semaphore = asyncio.Semaphore(self.settings.concurrency)
        nodes_by_id = {node.id: node for node in run.workflow.nodes}
        layers = topological_layers(run.workflow)
        layer_map = {
            nid: layer_idx for layer_idx, layer in enumerate(layers, start=1) for nid in layer
        }
        try:
            self.store.save_run(run)
            self._emit("run", status="running")
            # 按拓扑序建立协程，每个节点仅等待自身的直接上游而非整层屏障。
            for layer in layers:
                for node_id in layer:
                    self._tasks[node_id] = asyncio.create_task(
                        self._execute_node(
                            run, nodes_by_id[node_id], layer_map[node_id], nodes_by_id, semaphore
                        )
                    )
            if self._stopped:
                self.stop()
            await asyncio.gather(*self._tasks.values())
            run.status = (
                "success"
                if all(item.status == "success" for item in run.nodes.values())
                else "failed"
            )
        except asyncio.CancelledError:
            stopped_by_user = self._stopped
            self.stop()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            run.status = "stopped"
            if not stopped_by_user:
                raise
        except Exception:
            self.stop()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            run.status = "failed"
            raise
        finally:
            for state in run.nodes.values():
                if state.status in {"waiting", "ready", "running"}:
                    state.status = "stopped"
                    state.finished_at = now()
                    self._emit("node", state.node_id, status=state.status)
            run.finished_at = now()
            self._tasks.clear()
            self.active = False
            self.store.save_run(run)
            self._emit("run", status=run.status)
        return run

    def _messages(
        self, run: Run, node: Node, layer: int, nodes_by_id: dict[str, Node]
    ) -> list[dict]:
        """使用闭合 XML 传导标签为当前节点组装角色指令、技能、预算与直接上游结论。"""
        worker = run.workers[node.worker_id]
        skills = run.skill_contexts.get(worker.id, {})
        budget = self.settings.max_tool_rounds if worker.mcp_servers else None
        system_prompt = format_system_context(worker, skills, max_tool_rounds=budget)

        upstreams = [(nodes_by_id[uid], run.nodes[uid].output) for uid in node.dependencies]
        user_payload = format_workflow_input(
            workflow_id=run.workflow.id,
            workflow_name=run.workflow.name,
            global_task=run.task,
            node=node,
            layer=layer,
            upstreams=upstreams,
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ]

    async def _execute_node(
        self,
        run: Run,
        node: Node,
        layer: int,
        nodes_by_id: dict[str, Node],
        semaphore: asyncio.Semaphore,
    ) -> None:
        state = run.nodes[node.id]
        try:
            if node.dependencies:
                await asyncio.gather(*(self._tasks[uid] for uid in node.dependencies))
            for uid in node.dependencies:
                if run.nodes[uid].status != "success":
                    state.status = "blocked"
                    state.error = "直接上游节点未成功"
                    return
            state.status = "ready"
            self._emit("node", node.id, status="ready")
            async with semaphore:
                if self._stopped:
                    raise asyncio.CancelledError
                state.status = "running"
                state.started_at = now()
                state.attempts = 1
                self.store.save_run(run)
                self._emit("node", node.id, status="running")
                self._log(state, "正在请求模型", type="request")

                def on_text(text: str) -> None:
                    state.output += text
                    self._emit("output", node.id, text=self._redact(text))

                worker = run.workers[node.worker_id]
                try:
                    if self.client is None:
                        async with ModelClient(self.settings, worker) as client:
                            reply = await self._request(
                                client, run, node, layer, nodes_by_id, state, on_text
                            )
                    else:
                        reply = await self._request(
                            self.client, run, node, layer, nodes_by_id, state, on_text
                        )
                except RuntimeResponseError as exc:
                    state.usage = exc.reply.usage
                    state.output = self._redact(exc.reply.content)
                    self._emit("usage", node.id)
                    raise
                state.usage = reply.usage
                state.output = self._redact(reply.content)
                self._emit("usage", node.id)
                state.status = "success"
                self._log(state, "节点完成", type="complete")
        except asyncio.CancelledError:
            state.status = "stopped"
            self._log(state, "节点已停止", type="stopped")
            raise
        except Exception as exc:
            state.status = "failed"
            state.error = self._redact(str(exc))
            self._log(state, state.error, type="error")
        finally:
            state.output = self._redact(state.output)
            state.finished_at = now()
            self.store.save_run(run)
            self._emit("node", node.id, status=state.status)

    async def _request(
        self,
        client: ModelClient,
        run: Run,
        node: Node,
        layer: int,
        nodes_by_id: dict[str, Node],
        state: NodeRun,
        on_text: Callable[[str], None],
    ) -> Reply:
        worker = run.workers[node.worker_id]
        messages = self._messages(run, node, layer, nodes_by_id)
        if not worker.mcp_servers:
            return await SingleRequestRuntime(client).execute(worker.model, messages, on_text)

        def on_tool(event: str, name: str, detail: str = "") -> None:
            if event == "start":
                self._log(state, f"调用 MCP 工具 {name}", type="tool")
            elif event == "end":
                self._log(state, f"MCP 工具完成 {name}", type="tool")
            else:
                self._log(state, f"MCP 工具失败 {name}：{detail}", type="tool")

        def on_tool_step(step: dict) -> None:
            redacted_step = {
                **step,
                "arguments": self._redact(str(step.get("arguments", ""))),
                "result": self._redact(str(step.get("result", ""))),
            }
            for existing in reversed(state.logs):
                if (
                    existing.get("type") == "tool_step"
                    and existing.get("step") == redacted_step["step"]
                ):
                    existing.update(redacted_step)
                    self._emit("log", state.node_id, **existing)
                    return
            entry = {
                "at": now(),
                "type": "tool_step",
                "message": redacted_step["name"],
                **redacted_step,
            }
            state.logs.append(entry)
            self._emit("log", state.node_id, **entry)

        async with MCPTools(self.settings, worker) as tools:
            return await ToolRuntime(
                client,
                self.settings.max_tool_rounds,
                on_tool=on_tool,
                on_tool_step=on_tool_step,
            ).execute(worker.model, messages, on_text, tools)
