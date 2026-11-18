"""串行、正反判断与综合裁决预设；数据库和凭据全部为测试数据。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from ctflow.examples import seed_examples, seed_flow_examples
from ctflow.graph import topological_layers, validate_workflow
from ctflow.models import Node, Worker, Workflow
from ctflow.storage import Store

WORKER_IDS = {
    "scout-facts",
    "scout-practice",
    "scout-risks",
    "synthesis",
    "support",
    "challenge",
}
FLOW_IDS = {"serial-111", "debate-312", "decision-3121"}
SCOUT_IDS = {"scout-facts", "scout-practice", "scout-risks"}


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "data")
    yield database
    database.close()


@pytest.fixture
def template(store):
    worker = Worker(
        "judge",
        "用户裁决角色",
        "qwen-test-model",
        "用户的裁决提示词",
        ["judge-only"],
        base_url="https://qwen.example/v1",
        api_key="fake-judge-key",
        extra_body={
            "enable_thinking": False,
            "metadata": {"labels": ["template"]},
            "search_options": {"forced_search": False, "custom_option": {"labels": ["keep"]}},
        },
    )
    store.save_worker(worker)
    return worker


@pytest.mark.parametrize(
    ("flow_id", "widths"),
    [("serial-111", [1, 1, 1]), ("debate-312", [3, 1, 2]), ("decision-3121", [3, 1, 2, 1])],
)
def test_presets_have_required_layers_and_valid_worker_references(store, template, flow_id, widths):
    seed_flow_examples(store, template)
    workers = {worker.id: worker for worker in store.list_workers()}
    assert set(workers) == WORKER_IDS | {"judge"}
    assert {workflow.id for workflow in store.list_workflows()} == FLOW_IDS
    workflow = store.get_workflow(flow_id)
    validate_workflow(workflow, workers)
    assert [len(layer) for layer in topological_layers(workflow)] == widths
    assert workflow.task.strip()
    assert all(
        node.task.strip() and workers[node.worker_id].prompt.strip() for node in workflow.nodes
    )
    assert all(node.worker_id in workers for node in workflow.nodes)


def test_serial_chain_passes_only_direct_upstream_and_reuses_judge(store, template):
    seed_flow_examples(store, template)
    workflow = store.get_workflow("serial-111")
    assert {node.id: (node.worker_id, node.dependencies) for node in workflow.nodes} == {
        "A": ("scout-facts", []),
        "B": ("synthesis", ["A"]),
        "C": ("judge", ["B"]),
    }
    assert "独立" in workflow.nodes[-1].task
    assert "来源" in workflow.nodes[1].task


@pytest.mark.parametrize("flow_id", ["debate-312", "decision-3121"])
def test_parallel_search_join_and_opposing_views_have_exact_direct_dependencies(
    store, template, flow_id
):
    seed_flow_examples(store, template)
    workflow = store.get_workflow(flow_id)
    nodes = {node.id: node for node in workflow.nodes}
    expected = {
        "A": [],
        "B": [],
        "C": [],
        "D": ["A", "B", "C"],
        "E": ["D"],
        "F": ["D"],
    }
    if flow_id == "decision-3121":
        expected["G"] = ["E", "F"]
        assert nodes["G"].worker_id == "judge"
        assert "独立" in nodes["G"].task
    assert {key: node.dependencies for key, node in nodes.items()} == expected
    assert {nodes[key].worker_id for key in ("A", "B", "C")} == SCOUT_IDS
    assert nodes["E"].worker_id == "support"
    assert nodes["F"].worker_id == "challenge"
    parents = {parent for node in nodes.values() for parent in node.dependencies}
    assert set(nodes) - parents == ({"E", "F"} if flow_id == "debate-312" else {"G"})
    for key in ("E", "F"):
        assert "不要求与另一分支得出相反结论" in nodes[key].task
        assert "证据" in nodes[key].task
        assert "不强行" in store.get_worker(nodes[key].worker_id).prompt


def test_connections_and_search_options_are_deep_copied_for_every_worker(
    store, template, monkeypatch
):
    original = deepcopy(template)
    saved = []
    save_worker = store.save_worker

    def capture(worker):
        saved.append(worker)
        save_worker(worker)

    monkeypatch.setattr(store, "save_worker", capture)
    seed_flow_examples(store, template)
    assert len(saved) == 6
    assert template == original and store.get_worker("judge") == original
    assert len({id(worker.extra_body) for worker in saved}) == 6
    for worker in saved:
        assert worker.model == template.model
        assert worker.base_url == template.base_url and worker.api_key == template.api_key
        assert worker.skills == []
        assert worker.extra_body["metadata"] == template.extra_body["metadata"]
        assert worker.extra_body["metadata"] is not template.extra_body["metadata"]
        assert worker.extra_body["search_options"] is not template.extra_body["search_options"]
        assert worker.extra_body == template.extra_body
        if worker.id in SCOUT_IDS:
            assert "如果服务支持联网检索" in worker.prompt
            assert "若服务未执行检索" in worker.prompt
            assert "不得" in worker.prompt
        assert "enable_source" not in worker.extra_body
    saved[0].extra_body["metadata"]["labels"].append("changed")
    saved[0].extra_body["search_options"]["custom_option"]["labels"].append("changed")
    assert all(worker.extra_body["metadata"]["labels"] == ["template"] for worker in saved[1:])
    assert all(
        worker.extra_body["search_options"]["custom_option"]["labels"] == ["keep"]
        for worker in saved[1:]
    )
    assert template == original


@pytest.mark.parametrize(
    "kind, identifier",
    [
        *[("worker", key) for key in sorted(WORKER_IDS)],
        *[("workflow", key) for key in sorted(FLOW_IDS)],
    ],
)
@pytest.mark.parametrize("uppercase", [False, True])
def test_any_existing_id_conflict_aborts_before_all_writes(
    store, template, monkeypatch, kind, identifier, uppercase
):
    identifier = identifier.upper() if uppercase else identifier
    if kind == "worker":
        store.save_worker(replace(template, id=identifier, name="用户自定义", prompt="用户原文"))
    else:
        store.save_workflow(
            Workflow(identifier, "用户自定义", "用户原文", [Node("A", "A", "judge")])
        )
    workers_before = store.list_workers()
    workflows_before = store.list_workflows()
    writes = []
    monkeypatch.setattr(store, "save_worker", writes.append)
    monkeypatch.setattr(store, "save_workflow", writes.append)
    with pytest.raises(ValueError, match=f"ID 冲突.*{identifier.lower()}"):
        seed_flow_examples(store, template)
    assert writes == []
    assert store.list_workers() == workers_before
    assert store.list_workflows() == workflows_before


def test_missing_existing_judge_is_reported_before_any_records_are_written(store, monkeypatch):
    writes = []
    monkeypatch.setattr(store, "save_worker", writes.append)
    monkeypatch.setattr(store, "save_workflow", writes.append)
    with pytest.raises(ValueError, match="未知 Worker：judge"):
        seed_flow_examples(store, Worker("template", "模板", "test-model"))
    assert writes == []
    assert store.list_workers() == [] and store.list_workflows() == []


def test_append_preserves_all_existing_objects_and_repeated_append_reports_conflicts(
    store, template
):
    existing = replace(template, id="user-worker", name="原有用户角色", prompt="用户定义")
    workflow = Workflow("user-flow", "原有用户流程", "原有任务", [Node("A", "A", existing.id)])
    store.save_worker(existing)
    store.save_workflow(workflow)
    seed_flow_examples(store, template)
    assert store.get_worker(existing.id) == existing
    assert store.get_worker("judge") == template
    assert store.get_workflow(workflow.id) == workflow
    workers_before = store.list_workers()
    flows_before = store.list_workflows()
    with pytest.raises(ValueError, match="ID 冲突") as error:
        seed_flow_examples(store, template)
    assert all(key in str(error.value) for key in WORKER_IDS | FLOW_IDS)
    assert store.list_workers() == workers_before
    assert store.list_workflows() == flows_before


def test_clean_install_includes_original_review_and_all_new_presets_without_credentials(store):
    seed_examples(store, "chosen-model")
    workers = {worker.id: worker for worker in store.list_workers()}
    assert set(workers) == WORKER_IDS | {"research", "code", "pro", "con", "judge"}
    assert {workflow.id for workflow in store.list_workflows()} == FLOW_IDS | {"review"}
    for worker in workers.values():
        assert worker.model == "chosen-model"
        assert worker.base_url == worker.api_key == ""
        assert worker.extra_body == {}
    assert [len(layer) for layer in topological_layers(store.get_workflow("review"))] == [2, 2, 1]
    before = store.list_workers(), store.list_workflows()
    seed_examples(store, "changed-default")
    assert (store.list_workers(), store.list_workflows()) == before
