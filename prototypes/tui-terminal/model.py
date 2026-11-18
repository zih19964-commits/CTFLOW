"""五种终端布局共享的内存模型；耗时、用量、证据与报告均为本地模拟。"""

from collections import deque
from dataclasses import dataclass, replace
from math import isfinite


@dataclass(frozen=True)
class Worker:
    key: str
    name: str
    model: str
    summary: str
    prompt: str = ""


@dataclass
class Node:
    id: str
    name: str
    worker: str
    deps: tuple[str, ...]
    task: str = ""
    status: str = "waiting"
    output: str = ""
    tokens: int = 0
    started: float | None = None
    ended: float | None = None
    error: str = ""
    duration: float = 5.0


class Demo:
    """以直接上游结果交接的演示图，所有界面状态从此模型读取。"""

    def __init__(self) -> None:
        presets = (
            ("research", "资料研究", "提炼需求、场景与可复核的依据"),
            ("code", "实现分析", "设计核心流程、模块边界与验证方式"),
            ("pro", "支持论证", "说明方案价值及成立条件"),
            ("con", "反对论证", "检查风险、反例与验证缺口"),
            ("judge", "综合裁决", "综合双方证据形成可执行结论"),
            ("writer", "报告撰写", "整理目标、方案、证据与下一步"),
        )
        self.workers = {
            key: Worker(key, name, "gpt-6.1-sol", summary, f"请负责{summary}。")
            for key, name, summary in presets
        }
        self.task = "评审一个兼顾实现成本、用户体验与风险控制的方案"
        self.workflow_name = "五角色方案评审"
        self.nodes = self._make_nodes()
        self.selected_id = "A"
        self.messages: list[tuple[str, str]] = [
            ("assistant", "输入任务或使用命令编排工作流，在右侧节点卡片选择并装配 Worker。")
        ]
        self.logs: list[tuple[float, str, str]] = []
        self.artifact = ""
        self.elapsed = 0.0
        self.revision = 0
        self._scenario = "normal"

    def _make_nodes(self, serial: bool = False) -> list[Node]:
        keys = ("research", "code", "pro", "con", "judge")
        dependencies = ((), (), ("A", "B"), ("A", "B"), ("C", "D"))
        return [
            Node(
                chr(65 + index),
                self.workers[key].name,
                key,
                (chr(64 + index),) if serial and index else dependencies[index],
                duration=float(4 + index % 3),
            )
            for index, key in enumerate(keys)
        ]

    @property
    def selected(self) -> Node:
        return self._node(self.selected_id)

    @property
    def status(self) -> str:
        if any(node.status == "running" for node in self.nodes):
            return "running"
        if self.nodes and all(node.status == "completed" for node in self.nodes):
            return "completed"
        if any(node.status == "defeat" for node in self.nodes):
            return "defeat"
        return "waiting"

    @property
    def busy(self) -> bool:
        return self.status == "running"

    @property
    def tokens(self) -> int:
        return sum(node.tokens for node in self.nodes)

    @property
    def complete_count(self) -> int:
        return sum(node.status == "completed" for node in self.nodes)

    def _node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise ValueError(f"未知节点：{node_id}")

    def _worker(self, key: str) -> Worker:
        if key not in self.workers:
            raise ValueError(f"未知 Worker：{key}")
        return self.workers[key]

    def _require_editable(self) -> None:
        if self.busy:
            raise ValueError("工作流正在运行，请先停止再修改")

    def _validate_flow(self, nodes: list[Node]) -> None:
        """Kahn 拓扑验证，时间和空间复杂度均为 O(节点数 + 依赖数)。"""
        if not nodes:
            raise ValueError("工作流至少需要一个节点")
        by_id = {}
        for node in nodes:
            if not node.id.strip():
                raise ValueError("节点 ID 不能为空")
            if node.id in by_id:
                raise ValueError(f"节点 ID 重复：{node.id}")
            self._worker(node.worker)
            if not isfinite(node.duration) or node.duration <= 0:
                raise ValueError(f"节点 {node.id} 的模拟耗时必须是有限的正数")
            by_id[node.id] = node
        children: dict[str, list[str]] = {key: [] for key in by_id}
        indegree = {}
        for node in nodes:
            if len(set(node.deps)) != len(node.deps):
                raise ValueError(f"节点 {node.id} 的依赖重复")
            for parent_id in node.deps:
                if parent_id == node.id:
                    raise ValueError(f"节点 {node.id} 不能依赖自身")
                if parent_id not in by_id:
                    raise ValueError(f"节点 {node.id} 的依赖不存在：{parent_id}")
                children[parent_id].append(node.id)
            indegree[node.id] = len(node.deps)
        ready = deque(key for key, count in indegree.items() if count == 0)
        visited = 0
        while ready:
            visited += 1
            for child_id in children[ready.popleft()]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        if visited != len(nodes):
            raise ValueError("依赖关系存在环，请移除循环依赖")

    def _reset_run(self, *, explain: bool = False) -> None:
        had_run = any(node.status != "waiting" for node in self.nodes)
        for node in self.nodes:
            node.status = "waiting"
            node.output = ""
            node.tokens = 0
            node.started = None
            node.ended = None
            node.error = ""
        self.logs.clear()
        self.artifact = ""
        self.elapsed = 0.0
        if explain and had_run:
            self.messages.append(("assistant", "工作流已修改，已清空上一轮运行数据，请重新执行。"))

    def _log(self, node_id: str, text: str) -> None:
        self.logs.append((self.elapsed, node_id, text))

    def select(self, id: str) -> None:
        self._node(id)
        if self.selected_id != id:
            self.selected_id = id
            self.revision += 1

    def plan(self, task: str, serial: bool = False) -> None:
        self._require_editable()
        if not task.strip():
            raise ValueError("任务不能为空")
        self._reset_run(explain=True)
        self.task = task.strip()
        self.nodes = self._make_nodes(serial)
        self.selected_id = "A"
        self.workflow_name = f"五角色{'串行' if serial else '并行'}评审"
        self.messages.extend(
            [
                ("user", self.task),
                ("assistant", f"已生成{self.workflow_name}，可装配 Worker 后执行。"),
            ]
        )
        self.revision += 1

    def load_flow(self, kind: str) -> None:
        self._require_editable()
        if kind not in {"review", "serial", "new"}:
            raise ValueError(f"未知工作流：{kind}，请选择 review、serial 或 new")
        self._reset_run(explain=True)
        if kind == "new":
            self.nodes = [Node("A", "新任务", "research", ())]
            self.task = ""
            self.workflow_name = "新建工作流"
        else:
            self.nodes = self._make_nodes(serial=kind == "serial")
            self.workflow_name = "五角色串行评审" if kind == "serial" else "五角色方案评审"
        self.selected_id = "A"
        self.messages.append(("assistant", f"已载入{self.workflow_name}。"))
        self.revision += 1

    def replace_flow(self, nodes: list[Node]) -> None:
        self._require_editable()
        self._validate_flow(nodes)
        # 验证候选图后才修改当前图，拒绝时保留全部已有结果。
        fresh = [
            Node(
                node.id, node.name, node.worker, tuple(node.deps), node.task, duration=node.duration
            )
            for node in nodes
        ]
        self._reset_run(explain=True)
        self.nodes = fresh
        self.selected_id = fresh[0].id
        self.messages.append(("assistant", f"拓扑已更新，共 {len(fresh)} 个节点。"))
        self.revision += 1

    def assign(self, nodeid: str, workerkey: str) -> None:
        self._require_editable()
        node = self._node(nodeid)
        worker = self._worker(workerkey)
        self._reset_run(explain=True)
        node.worker = worker.key
        self.messages.append(("assistant", f"节点 {nodeid} 已装配预设 Worker：{worker.name}。"))
        self.revision += 1

    def add_node(self, name: str, workerkey: str, deps: tuple[str, ...] = ()) -> Node:
        self._require_editable()
        self._worker(workerkey)
        if not name.strip():
            raise ValueError("节点名称不能为空")
        used = {node.id for node in self.nodes}
        index = 0
        while True:
            number = index + 1
            node_id = ""
            while number:
                number, digit = divmod(number - 1, 26)
                node_id = chr(65 + digit) + node_id
            if node_id not in used:
                break
            index += 1
        node = Node(node_id, name.strip(), workerkey, tuple(deps))
        self._validate_flow([*self.nodes, node])
        self._reset_run(explain=True)
        self.nodes.append(node)
        self.selected_id = node.id
        self.messages.append(("assistant", f"已添加节点 {node.id}：{node.name}。"))
        self.revision += 1
        return node

    def set_task(self, nodeid: str, text: str) -> None:
        self._require_editable()
        node = self._node(nodeid)
        self._reset_run(explain=True)
        node.task = text.strip()
        self.messages.append(("assistant", f"节点 {nodeid} 的任务已更新。"))
        self.revision += 1

    def set_dependencies(self, nodeid: str, deps: tuple[str, ...]) -> None:
        self._require_editable()
        node = self._node(nodeid)
        candidate = replace(node, deps=tuple(deps))
        self._validate_flow([candidate if item is node else item for item in self.nodes])
        self._reset_run(explain=True)
        node.deps = candidate.deps
        self.messages.append(("assistant", f"节点 {nodeid} 的依赖已更新。"))
        self.revision += 1

    def start(self, scenario: str = "normal") -> None:
        self._require_editable()
        if scenario not in {"normal", "defeat"}:
            raise ValueError(f"未知运行场景：{scenario}，请选择 normal 或 defeat")
        self._validate_flow(self.nodes)
        self._reset_run()
        self._scenario = scenario
        self._log("·", f"开始模拟执行，场景：{scenario}")
        self.messages.append(("assistant", "开始执行，各节点接收直接上游的结果与证据引用。"))
        self._schedule()
        self.revision += 1

    def stop(self) -> None:
        if not self.busy:
            raise ValueError("当前没有正在运行的工作流")
        for node in self.nodes:
            if node.status in {"waiting", "running"}:
                self._defeat(node, "用户停止执行")
        self._finish_run()
        self.revision += 1

    def _defeat(self, node: Node, reason: str) -> None:
        node.status = "defeat"
        node.error = reason
        node.ended = self.elapsed
        node.output = (
            f"## {node.name}：执行失败\n\n- 原因：{reason}\n\n```text\n{node.id}: defeat\n```"
        )
        self._log(node.id, reason)

    def _schedule(self) -> None:
        by_id = {node.id: node for node in self.nodes}
        children: dict[str, list[Node]] = {key: [] for key in by_id}
        for node in self.nodes:
            for parent_id in node.deps:
                children[parent_id].append(node)
        # 先沿有向边传播失败，输入列表无需按拓扑顺序排列。
        failed = deque(node for node in self.nodes if node.status == "defeat")
        while failed:
            parent = failed.popleft()
            for node in children[parent.id]:
                if node.status == "waiting":
                    self._defeat(node, f"上游失败：{parent.id}，任务受阻")
                    failed.append(node)
        for node in self.nodes:
            if node.status == "waiting" and all(
                by_id[key].status == "completed" for key in node.deps
            ):
                node.status = "running"
                node.started = self.elapsed
                self._log(node.id, f"{node.name} 开始模拟执行")

    def _finish_node(self, node: Node) -> None:
        failure_id = "C" if any(item.id == "C" for item in self.nodes) else self.nodes[0].id
        if self._scenario == "defeat" and node.id == failure_id:
            self._defeat(node, "模拟故障：本节点执行失败")
            return
        node.status = "completed"
        node.ended = self.elapsed
        worker = self.workers[node.worker]
        evidence = "、".join(f"[演示证据:{key}]" for key in node.deps) or "[演示证据:任务输入]"
        node.output = (
            f"## {node.name}\n\n"
            f"任务：{node.task or self.task or '整理当前任务的目标与验证方法'}\n\n"
            f"- 执行角色：{worker.name}；{worker.summary}。\n"
            "- 建议：先验证一条完整核心路径，记录输入、交接与验收结果。\n"
            f"- 依据：{evidence}\n\n"
            f"```text\n节点 {node.id} -> 核心路径验证 -> 可复核结论\n```"
        )
        self._log(node.id, f"模拟完成，{node.tokens} Token")

    def _finish_run(self) -> None:
        result = "全部节点完成" if self.status == "completed" else "执行结束，存在失败节点"
        sections = "\n\n".join(node.output for node in self.nodes)
        self.artifact = (
            f"# {self.workflow_name} · 方案报告\n\n"
            f"## 目标\n\n{self.task or '通过节点任务定义目标'}\n\n"
            f"## 执行结论\n\n{result}。完成 {self.complete_count}/{len(self.nodes)} 个节点，"
            f"模拟耗时 {self.elapsed:.2f} 秒，用量 {self.tokens} Token。\n\n"
            "## 实施方案\n\n- 明确目标用户、输入与交付标准。\n"
            "- 完成核心路径，再验证异常处理与节点交接。\n"
            "- 根据各节点证据决定实现顺序与验收范围。\n\n"
            f"## 节点结果与证据\n\n{sections}\n\n"
            "## 下一步\n\n检查各节点结论与失败原因，调整任务和装配后重新执行。\n\n"
            "本报告使用本地模拟数据。\n"
        )
        self._log("·", result)
        self.messages.append(("assistant", self.artifact))

    def tick(self, dt: float) -> bool:
        if not isfinite(dt) or dt < 0:
            raise ValueError("推进秒数必须是有限的非负数")
        if not self.busy or dt == 0:
            return False
        target = self.elapsed + dt
        if not isfinite(target):
            raise ValueError("累计模拟时间必须是有限数")
        # 在完成事件处切分时间，先结算同一时刻的父节点，再启动子节点。
        while self.busy and self.elapsed < target:
            running = [node for node in self.nodes if node.status == "running"]
            finishes = {node.id: node.started + node.duration for node in running}
            event = min(finishes.values())
            self.elapsed = min(target, event)
            for node in running:
                spent = min(node.duration, self.elapsed - node.started)
                node.tokens = round(spent * 60)
                if finishes[node.id] <= self.elapsed:
                    self._finish_node(node)
            self._schedule()
            if not self.busy:
                self._finish_run()
        self.revision += 1
        return True
