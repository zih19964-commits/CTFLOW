"""简化 Mermaid 语法与 DAG 的正式回归测试。"""

from copy import deepcopy

import pytest

from ctflow.graph import parse_workflow, to_mermaid, topological_layers, validate_workflow
from ctflow.models import Node, Worker, Workflow


@pytest.fixture
def workers() -> dict[str, Worker]:
    return {
        "research": Worker(id="research", name="研究员", model="test"),
        "write": Worker(id="write", name="写作者", model="test"),
    }


def make_workflow(*nodes: Node) -> Workflow:
    return Workflow(id="test", name="测试", task="任务", nodes=list(nodes))


def test_repeated_identical_node_labels_in_mermaid_edges(workers):
    source = """flowchart TD
    A["调查 @research"] --> B["评审 @write"]
    A["调查 @research"] --> C["核对 @write"]
    B["评审 @write"] --> D["汇总 @write"]
    C["核对 @write"] --> D["汇总 @write"]
    """
    workflow = parse_workflow(source, workers)
    assert topological_layers(workflow) == [["A"], ["B", "C"], ["D"]]


@pytest.mark.parametrize("direction", ["TD", "LR"])
def test_parse_branch_merge(workers: dict[str, Worker], direction: str) -> None:
    source = f"""flowchart {direction}
A["调研 @research"] --> B["初稿 @write"]
A --> C["复核 @research"]
B --> D["汇总 @write"]
C --> D
E["独立 @research"]
"""
    workflow = parse_workflow(source, workers, workflow_id="flow", name="研究", task="文章")
    assert (workflow.id, workflow.name, workflow.task, workflow.source) == (
        "flow",
        "研究",
        "文章",
        source,
    )
    assert topological_layers(workflow) == [["A", "E"], ["B", "C"], ["D"]]
    assert workflow.nodes[3].dependencies == ["B", "C"]
    validate_workflow(workflow, workers)


def test_parse_chain_forward_references_and_fence(workers: dict[str, Worker]) -> None:
    workflow = parse_workflow(
        """```mermaid
flowchart TD;
A --> B --> C;
C["汇总 @write"]
B["初稿 @write"]
A["调研 @research"]
```""",
        workers,
    )
    assert workflow.id
    assert workflow.name == "新工作流"
    assert topological_layers(workflow) == [["A"], ["B"], ["C"]]


def test_parse_inline_chain(workers: dict[str, Worker]) -> None:
    workflow = parse_workflow(
        'flowchart TD\nA["一 @research"] --> B["二 @write"] --> C["三 @write"]', workers
    )
    assert [node.dependencies for node in workflow.nodes] == [[], ["A"], ["B"]]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "首行"),
        ('A["一 @research"]', "首行"),
        ('flowchart BT\nA["一 @research"]', "首行"),
        ("flowchart TD", "至少需要"),
        ("flowchart TD\n;", "语法无效"),
        ('flowchart TD\nA["一 @research"]\n未知语法', "语法无效"),
        ('flowchart TD\nA["一 @research"] garbage', "语法无效"),
        ('flowchart TD\nA["一 @research"] -.-> B', "语法无效"),
        ('flowchart TD\nA["一 @research"] -->', "缺少目标"),
        ('flowchart TD\nA["一 @research"] --> --> B', "语法无效"),
        ('flowchart TD\nA["一 @research"]; B["二 @write"]', "语法无效"),
        ('flowchart TD\nA["一 @research"]\n%% 注释', "语法无效"),
        ('flowchart TD\nA["无角色"]', "@worker"),
        ('flowchart TD\nA[" @research"]', "@worker"),
        ('flowchart TD\nA["一 @missing"]', "未知 Worker"),
        ('flowchart TD\nA["一 @research"] --> B', "悬空节点：B"),
        ('flowchart TD\nA --> B["二 @write"]', "悬空节点：A"),
        ('flowchart TD\nA["一 @research"]\nA["二 @write"]', "重复定义"),
        ('flowchart TD\nA["一 @research"] --> A', "自环"),
        ('flowchart TD\nA["一 @research"] --> B["二 @write"] --> A', "A → B → A"),
        ('flowchart TD\nA["一 @research"]\na["二 @write"]', "大小写冲突"),
        ('flowchart TD\nA["一 @research"] --> B["二 @write"]\nA --> B', "重复依赖"),
        ('flowchart TD\nA["坏\\q @research"]', "转义无效"),
        ('flowchart TD\nA["坏\\n名称 @research"]', "换行"),
        ('flowchart TD\n1A["一 @research"]', "语法无效"),
    ],
)
def test_parse_rejects_invalid_source(
    workers: dict[str, Worker], source: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_workflow(source, workers)


@pytest.mark.parametrize(
    "source",
    [
        '说明\n```mermaid\nflowchart TD\nA["一 @research"]\n```',
        '```mermaid\nflowchart TD\nA["一 @research"]\n```\n说明',
        '```python\nflowchart TD\nA["一 @research"]\n```',
        '```mermaid\nflowchart TD\nA["一 @research"]\n```\n```mermaid\nflowchart TD\n```',
        '```mermaid\nflowchart TD\nA["一 @research"]',
    ],
)
def test_parse_rejects_mixed_or_broken_fences(workers: dict[str, Worker], source: str) -> None:
    with pytest.raises(ValueError, match="单个 mermaid"):
        parse_workflow(source, workers)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([], "至少需要"),
        ([Node("A", "一", "research"), Node("A", "二", "write")], "重复节点 ID"),
        ([Node("A", "一", "missing")], "未知 Worker"),
        ([Node("A", "一", "research", dependencies=["B"])], "悬空节点"),
        ([Node("A", "一", "research", dependencies=["A"])], "自环"),
        ([Node("bad-id", "一", "research")], "节点 ID 无效"),
        ([Node("A", " ", "research")], "名称不能为空"),
        ([Node("A", "一", "bad worker")], "Worker ID 无效"),
        ([Node("A", "一", "")], "Worker ID 无效"),
        (
            [Node("A", "一", "research"), Node("B", "二", "write", dependencies=["A", "A"])],
            "重复依赖",
        ),
        (
            [
                Node("A", "一", "research", dependencies=["B"]),
                Node("B", "二", "write", dependencies=["A"]),
            ],
            "存在环",
        ),
    ],
)
def test_validate_dataclass_workflows(
    workers: dict[str, Worker], nodes: list[Node], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_workflow(make_workflow(*nodes), workers)


def test_topological_layers_rejects_disconnected_cycle() -> None:
    workflow = make_workflow(
        Node("A", "起点", "research"),
        Node("B", "环一", "research", dependencies=["C"]),
        Node("C", "环二", "research", dependencies=["B"]),
    )
    with pytest.raises(ValueError, match="B → C → B"):
        topological_layers(workflow)


def test_cycle_error_isolates_minimal_loop_without_downstream_tail(
    workers: dict[str, Worker],
) -> None:
    workflow = make_workflow(
        Node("D", "受牵连下游", "write", dependencies=["B"]),
        Node("A", "环首", "research", dependencies=["C"]),
        Node("B", "环中", "write", dependencies=["A"]),
        Node("C", "环尾", "research", dependencies=["B"]),
    )
    with pytest.raises(ValueError, match="A → B → C → A"):
        validate_workflow(workflow, workers)
    with pytest.raises(ValueError, match="大小写冲突"):
        validate_workflow(
            make_workflow(Node("StepA", "一", "research"), Node("stepa", "二", "write")),
            workers,
        )


def test_serialization_round_trip_and_metadata_preservation(workers: dict[str, Worker]) -> None:
    workflow = make_workflow(
        Node("A", '阅读 "资料" \\ 目录', "research", task="读取资料"),
        Node("B", "写作 @附注", "write", dependencies=["A"], task="完成文章"),
        Node("C", "独立节点", "research"),
    )
    before = deepcopy(workflow)
    validate_workflow(workflow, workers)
    source = to_mermaid(workflow)
    assert workflow == before
    assert source.startswith("flowchart TD\n")
    restored = parse_workflow(source, workers)
    assert [
        (node.id, node.label, node.worker_id, node.dependencies) for node in restored.nodes
    ] == [(node.id, node.label, node.worker_id, node.dependencies) for node in workflow.nodes]
    assert to_mermaid(restored) == source


def test_topological_layers_and_validation_leave_metadata_unchanged(
    workers: dict[str, Worker],
) -> None:
    workflow = make_workflow(
        Node("A", "一", "research", task="任务"),
        Node("B", "二", "write", dependencies=["A"]),
    )
    before = deepcopy(workflow)
    assert topological_layers(workflow) == [["A"], ["B"]]
    validate_workflow(workflow, workers)
    assert workflow == before


def test_five_worker_three_stage_scenario() -> None:
    workers = {f"w{i}": Worker(id=f"w{i}", name=f"角色{i}", model="test") for i in range(5)}
    workflow = parse_workflow(
        """flowchart TD
A["资料 @w0"] --> C["初稿 @w2"]
B["数据 @w1"] --> C
C --> D["事实复核 @w3"]
C --> E["表达复核 @w4"]
""",
        workers,
    )
    assert topological_layers(workflow) == [["A", "B"], ["C"], ["D", "E"]]
