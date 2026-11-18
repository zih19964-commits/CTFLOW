"""严格的简化 Mermaid 语法，以及工作流依赖图操作。"""

import json
import re

from ctflow.models import Node, Worker, Workflow, new_id

_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NODE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(?:\[\s*("(?:[^"\\]|\\.)*")\s*\])?')
_LABEL = re.compile(r"(.+?)\s+@([^\s@]+)")


def _check_identity(node: Node) -> None:
    if not _ID.fullmatch(node.id):
        raise ValueError(f"节点 ID 无效：{node.id!r}，请使用字母、数字和下划线")
    if not node.label.strip() or "\n" in node.label or "\r" in node.label:
        raise ValueError(f"节点 {node.id} 的名称不能为空或包含换行")
    if not node.worker_id or re.search(r"[\s@]", node.worker_id):
        raise ValueError(f"节点 {node.id} 的 Worker ID 无效：{node.worker_id!r}")


def _dependency_graph(workflow: Workflow) -> tuple[dict[str, int], dict[str, list[str]]]:
    """集中检查节点身份与边，供校验和 Kahn 分层共用。"""
    if not workflow.nodes:
        raise ValueError("工作流至少需要一个节点")
    indegree: dict[str, int] = {}
    for node in workflow.nodes:
        if not _ID.fullmatch(node.id):
            raise ValueError(f"节点 ID 无效：{node.id!r}")
        if node.id in indegree:
            raise ValueError(f"重复节点 ID：{node.id}")
        indegree[node.id] = len(node.dependencies)

    successors: dict[str, list[str]] = {}
    for node_id in indegree:
        successors[node_id] = []
    for node in workflow.nodes:
        seen: set[str] = set()
        for parent in node.dependencies:
            if parent == node.id:
                raise ValueError(f"节点 {node.id} 不能依赖自身（自环）")
            if parent not in indegree:
                raise ValueError(f"节点 {node.id} 引用了悬空节点：{parent}")
            if parent in seen:
                raise ValueError(f"节点 {node.id} 存在重复依赖：{parent}")
            seen.add(parent)
            successors[parent].append(node.id)
    return indegree, successors


def topological_layers(workflow: Workflow) -> list[list[str]]:
    """用 Kahn 算法返回可并行执行的各层，时间和空间均为 O(V + E)。"""
    indegree, successors = _dependency_graph(workflow)
    # 入度表示尚未处理的上游数量；入度为零的节点可以排入当前层。
    ready = []
    for node_id, count in indegree.items():
        if count == 0:
            ready.append(node_id)
    layers: list[list[str]] = []
    visited = 0
    while ready:
        layers.append(ready)
        visited += len(ready)
        next_ready: list[str] = []
        for node_id in ready:
            for child in successors[node_id]:
                # 当前节点已排入顺序，它的下游便少等待一个依赖。
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        ready = next_ready
    if visited != len(indegree):
        blocked_nodes = []
        for node_id, count in indegree.items():
            if count > 0:
                blocked_nodes.append(node_id)
        blocked = "、".join(blocked_nodes)
        raise ValueError(f"工作流存在环，无法完成拓扑分层；未就绪节点：{blocked}")
    return layers


def validate_workflow(workflow: Workflow, workers: dict[str, Worker]) -> None:
    """验证节点名称、Worker 引用及完整 DAG；失败时抛出中文 ValueError。"""
    for node in workflow.nodes:
        _check_identity(node)
        if node.worker_id not in workers:
            raise ValueError(f"节点 {node.id} 引用了未知 Worker：{node.worker_id}")
    topological_layers(workflow)


def _source_body(source: str) -> str:
    text = source.strip()
    if "```" in text:
        fenced = re.fullmatch(r"```mermaid[^\S\n]*\n(.*?)\n```", text, re.DOTALL)
        if not fenced or "```" in fenced.group(1):
            raise ValueError("源文本只能是裸图文本或单个 mermaid 代码块，不能包含其他内容")
        text = fenced.group(1).strip()
    return text


def parse_workflow(
    source: str,
    workers: dict[str, Worker],
    *,
    workflow_id: str = "",
    name: str = "新工作流",
    task: str = "",
) -> Workflow:
    """解析逐行声明和链式连线；相同节点可在多条连线中重复引用。"""
    lines = _source_body(source).splitlines()
    if not lines or not re.fullmatch(r"flowchart\s+(TD|LR)\s*;?", lines[0].strip()):
        raise ValueError("首行必须是 flowchart TD 或 flowchart LR")

    nodes: dict[str, Node] = {}
    references: dict[str, int] = {}
    edges: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(";"):
            line = line[:-1].rstrip()
        if not line:
            raise ValueError(f"第 {line_number} 行语法无效：{raw_line.strip()}")
        position = 0
        chain: list[str] = []
        while position < len(line):
            match = _NODE.match(line, position)
            if not match:
                raise ValueError(f"第 {line_number} 行语法无效：{raw_line.strip()}")
            node_id, quoted_label = match.groups()
            chain.append(node_id)
            references.setdefault(node_id, line_number)
            if quoted_label is not None:
                try:
                    label = json.loads(quoted_label)
                except ValueError as error:
                    raise ValueError(f"第 {line_number} 行节点 {node_id} 的名称转义无效") from error
                if "\n" in label or "\r" in label:
                    raise ValueError(f"第 {line_number} 行节点 {node_id} 的名称不能包含换行")
                label_match = _LABEL.fullmatch(label.strip())
                if not label_match:
                    raise ValueError(f"第 {line_number} 行节点 {node_id} 必须包含名称和 @worker")
                title, worker_id = label_match.groups()
                if node_id in nodes:
                    existing = nodes[node_id]
                    if existing.label != title.strip() or existing.worker_id != worker_id:
                        raise ValueError(
                            f"第 {line_number} 行重复定义节点 ID 且内容冲突：{node_id}"
                        )
                else:
                    nodes[node_id] = Node(id=node_id, label=title.strip(), worker_id=worker_id)

            position = match.end()
            while position < len(line) and line[position].isspace():
                position += 1
            if position == len(line):
                break
            if not line.startswith("-->", position):
                raise ValueError(f"第 {line_number} 行语法无效：{raw_line.strip()}")
            position += 3
            while position < len(line) and line[position].isspace():
                position += 1
            if position == len(line):
                raise ValueError(f"第 {line_number} 行连线缺少目标节点")
        edges.extend(zip(chain, chain[1:], strict=False))

    for node_id, line_number in references.items():
        if node_id not in nodes:
            raise ValueError(f"第 {line_number} 行引用了未定义的悬空节点：{node_id}")
    for parent, child in edges:
        nodes[child].dependencies.append(parent)

    workflow = Workflow(
        id=workflow_id or new_id(), name=name, task=task, nodes=list(nodes.values()), source=source
    )
    validate_workflow(workflow, workers)
    return workflow


def to_mermaid(workflow: Workflow) -> str:
    """输出规范的 TD 图文本；节点任务保存在工作流快照中。"""
    topological_layers(workflow)
    lines = ["flowchart TD"]
    for node in workflow.nodes:
        _check_identity(node)
        label = json.dumps(f"{node.label} @{node.worker_id}", ensure_ascii=False)
        lines.append(f"    {node.id}[{label}]")
    for node in workflow.nodes:
        lines.extend(f"    {parent} --> {node.id}" for parent in node.dependencies)
    return "\n".join(lines)
