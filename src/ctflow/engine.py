"""以直接依赖任务和 Semaphore 调度 DAG，每个节点使用所属 Worker 的连接。"""

import asyncio
import copy
import json
from collections.abc import Callable

from ctflow.builtin_tools import CompositeTools, has_configured_tools, validate_builtin_tools
from ctflow.config import Settings
from ctflow.graph import topological_layers, validate_workflow
from ctflow.models import Node, NodeRun, Run, Workflow, new_id, now
from ctflow.provider import ModelClient, Reply
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime, ToolRuntime
from ctflow.storage import Store
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
        all_workers = {}
        for worker in self.store.list_workers():
            all_workers[worker.id] = worker
        validate_workflow(workflow, all_workers)

        workers = {}
        for node in workflow.nodes:
            workers[node.worker_id] = copy.deepcopy(all_workers[node.worker_id])
        for worker in workers.values():
            validate_builtin_tools(worker.builtin_tools)
            unknown = set(worker.mcp_servers) - self.settings.mcp_servers.keys()
            if unknown:
                raise ValueError(
                    f"Worker {worker.id} 引用了未登记的 MCP：{', '.join(sorted(unknown))}"
                )

        # 技能在创建时读取并保存正文；后续编辑技能或 Worker 不改变本次运行。
        skill_contexts = {}
        for worker_id, worker in workers.items():
            skill_contexts[worker_id] = read_skills(self.settings.workspace, worker.skills)

        node_runs = {}
        for node in workflow.nodes:
            node_runs[node.id] = NodeRun(node.id)

        run_task = task
        if run_task is None:
            run_task = workflow.task
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
            # 供应商异常也可能回显其他 Worker 的凭据，日志统一覆盖本次运行全部密钥。
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
        nodes_by_id = {}
        for node in run.workflow.nodes:
            nodes_by_id[node.id] = node
        try:
            self.store.save_run(run)
            self._emit("run", status="running")
            # 先按拓扑顺序建立全部任务，再让每个任务只等待直接上游，避免整层等待。
            for layer in topological_layers(run.workflow):
                for node_id in layer:
                    self._tasks[node_id] = asyncio.create_task(
                        self._execute_node(run, nodes_by_id[node_id], semaphore)
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

    def _messages(self, run: Run, node: Node) -> list[dict]:
        """为当前节点组装角色指令、任务和直接上游结果。"""
        worker = run.workers[node.worker_id]
        skill_text = run.skill_contexts[worker.id]
        prompt_parts = []
        if worker.prompt:
            prompt_parts.append(worker.prompt)
        if skill_text:
            prompt_parts.append(skill_text)
        prompt = "\n\n".join(prompt_parts)

        upstream = []
        for upstream_id in node.dependencies:
            upstream_state = run.nodes[upstream_id]
            upstream.append(
                {
                    "node": upstream_id,
                    "result": upstream_state.output,
                    "evidence": upstream_state.evidence,
                }
            )
        return [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"总任务：{run.task}\n当前节点：{node.label}\n节点补充任务：{node.task}\n"
                    f"直接上游结果：{json.dumps(upstream, ensure_ascii=False)}"
                ),
            },
        ]

    async def _execute_node(
        self,
        run: Run,
        node: Node,
        semaphore: asyncio.Semaphore,
    ) -> None:
        state = run.nodes[node.id]
        try:
            if node.dependencies:
                # 只等待当前节点的直接上游；其他无关节点可以继续运行。
                upstream_tasks = []
                for upstream_id in node.dependencies:
                    upstream_tasks.append(self._tasks[upstream_id])
                await asyncio.gather(*upstream_tasks)
            for upstream_id in node.dependencies:
                if run.nodes[upstream_id].status != "success":
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
                # 汇总上游证据，并按首次出现的顺序去重。
                seen_evidence = set()
                state.evidence = []
                for upstream_id in node.dependencies:
                    for evidence in run.nodes[upstream_id].evidence:
                        if evidence not in seen_evidence:
                            seen_evidence.add(evidence)
                            state.evidence.append(evidence)
                self.store.save_run(run)
                self._emit("node", node.id, status="running")
                self._log(state, "正在请求模型", type="request")

                def on_text(text: str) -> None:
                    state.output += text
                    self._emit("output", node.id, text=self._redact(text))

                worker = run.workers[node.worker_id]
                try:
                    if self.client is None:
                        # 在并发槽位内创建并关闭节点专属连接，取消同样经过上下文清理。
                        async with ModelClient(self.settings, worker) as client:
                            reply = await self._request(client, run, node, state, on_text)
                    else:
                        reply = await self._request(self.client, run, node, state, on_text)
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
        state: NodeRun,
        on_text: Callable[[str], None],
    ) -> Reply:
        worker = run.workers[node.worker_id]
        messages = self._messages(run, node)
        if not has_configured_tools(worker):
            return await SingleRequestRuntime(client).execute(worker.model, messages, on_text)

        def on_tool(event: str, name: str, detail: str = "") -> None:
            prefix = "MCP 工具" if name.startswith("mcp_") else "工具"
            if event == "start":
                self._log(state, f"调用{prefix} {name}", type="tool")
            elif event == "end":
                self._log(state, f"{prefix}完成 {name}", type="tool")
            else:
                self._log(state, f"{prefix}失败 {name}：{detail}", type="tool")

        def on_tool_step(step: dict) -> None:
            redacted_step = {
                **step,
                "arguments": self._redact(str(step.get("arguments", ""))),
                "result": self._redact(str(step.get("result", ""))),
            }
            # 更新同 step 编号的记录（running -> ok/error），保证实时状态与终态一致。
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

        async with CompositeTools(self.settings, worker) as tools:
            return await ToolRuntime(
                client,
                self.settings.max_tool_rounds,
                on_tool=on_tool,
                on_tool_step=on_tool_step,
            ).execute(worker.model, messages, on_text, tools)
