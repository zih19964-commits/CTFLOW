"""纯文件持久化、keyring 凭据隔离、坏文件容错与快照回滚回归测试。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from ctflow.examples import seed_examples
from ctflow.models import Node, NodeRun, Run, Usage, Worker, Workflow, to_dict
from ctflow.storage import Store


@pytest.fixture
def store(tmp_path: Path):
    st = Store(tmp_path / "data", backup_dir=tmp_path / "backups")
    yield st
    st.close()


@pytest.fixture
def worker() -> Worker:
    return Worker(
        "research",
        "资料研究",
        "test-model",
        "提供依据",
        skills=["研究"],
    )


@pytest.fixture
def workflow(worker: Worker) -> Workflow:
    return Workflow(
        "review",
        "方案评审",
        "评审课程管理器",
        [Node("A", "研究", worker.id, "提供依据")],
        source="用户绘制",
    )


@pytest.fixture
def run(worker: Worker, workflow: Workflow) -> Run:
    return Run(
        "run-1",
        workflow,
        {worker.id: worker},
        "评审方案",
        status="running",
        nodes={
            "A": NodeRun(
                "A",
                status="completed",
                output="# 研究结果\n\n适合本地课程项目。",
                usage=Usage(12, 8, 0.01),
                attempts=1,
                started_at="2026-10-18T00:00:00+00:00",
                finished_at="2026-10-18T00:00:01+00:00",
                logs=[{"tool": "mcp_search", "status": "completed"}],
            )
        },
    )


def test_worker_round_trip_update_and_reopen(store: Store, worker: Worker) -> None:
    store.save_worker(worker)
    assert store.get_worker(worker.id) == worker
    assert store.get_worker(worker.name) == worker
    worker.prompt = "更新后的依据要求"
    store.save_worker(worker)
    assert store.list_workers() == [worker]
    path = store.data_dir / "workers/worker-research.json"
    assert json.loads(path.read_text(encoding="utf-8")) == to_dict(worker)
    assert "更新后的依据要求" in path.read_text(encoding="utf-8")
    assert not (store.data_dir / "ctflow.db").exists()
    reopened = Store(store.data_dir, backup_dir=store.backup_dir)
    assert reopened.get_worker(worker.id) == worker


def test_workflow_round_trip(store: Store, workflow: Workflow) -> None:
    store.save_workflow(workflow)
    assert store.get_workflow(workflow.id) == workflow
    assert store.get_workflow(workflow.name) == workflow
    assert store.list_workflows() == [workflow]
    folder = store.data_dir / "workflows"
    assert json.loads((folder / "workflow-review.json").read_text(encoding="utf-8")) == to_dict(
        workflow
    )
    workflow.task = "补充评审"
    store.save_workflow(workflow)
    assert store.list_workflows() == [workflow]


@pytest.mark.parametrize("kind", ["worker", "workflow"])
def test_names_can_be_ambiguous_but_ids_are_explicit(
    store: Store, worker: Worker, workflow: Workflow, kind: str
) -> None:
    value = worker if kind == "worker" else workflow
    save = getattr(store, f"save_{kind}")
    get = getattr(store, f"get_{kind}")
    save(value)
    other = replace(value, id="other")
    save(other)
    with pytest.raises(ValueError, match="歧义"):
        get(value.name)
    assert get(value.id) == value
    assert get(other.id) == other


@pytest.mark.parametrize(
    "method", ["get_worker", "get_workflow", "get_run", "delete_worker", "delete_workflow"]
)
def test_unknown_ids_raise_value_error(store: Store, method: str) -> None:
    with pytest.raises(ValueError, match="找不到"):
        getattr(store, method)("unknown")


def test_run_round_trip_and_frequent_updates(store: Store, run: Run) -> None:
    store.save_run(run)
    assert store.get_run(run.id) == run
    for index in range(5):
        run.nodes["A"].logs.append({"message": f"进度 {index}"})
        store.save_run(run)
    run.status = "failed"
    run.nodes["A"].error = "模型请求失败"
    store.save_run(run)
    assert store.get_run(run.id) == run
    assert store.get_run(run.id).usage.total_tokens == 20
    assert store.list_runs() == [run]
    assert json.loads((store.data_dir / "runs/run-run-1.json").read_text(encoding="utf-8")) == (
        to_dict(run)
    )


def test_runs_are_sorted_and_limited(store: Store, run: Run) -> None:
    older = replace(run, id="older", created_at="2026-10-17T00:00:00+00:00")
    newer = replace(run, id="newer", created_at="2026-10-18T00:00:00+00:00")
    store.save_run(newer)
    store.save_run(older)
    assert store.list_runs() == [newer, older]
    assert store.list_runs(1) == [newer]
    assert store.list_runs(0) == []
    with pytest.raises(ValueError, match="负数"):
        store.list_runs(-1)


def test_delete_worker_requires_no_workflow_reference(
    store: Store, worker: Worker, workflow: Workflow
) -> None:
    store.save_worker(worker)
    store.save_workflow(workflow)
    with pytest.raises(ValueError, match="引用"):
        store.delete_worker(worker.id)
    assert store.get_worker(worker.id) == worker
    assert (store.data_dir / "workers/worker-research.json").exists()


def test_delete_definitions_keeps_historical_run_and_cleans_keyring(
    store: Store, worker: Worker, workflow: Workflow, run: Run
) -> None:
    worker.api_key = "secret-to-delete"
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.save_run(run)
    assert store._secrets.get(f"worker:{worker.id}") == "secret-to-delete"
    store.delete_workflow(workflow.id)
    store.delete_worker(worker.id)
    assert store.list_workers() == []
    assert store.list_workflows() == []
    assert store._secrets.get(f"worker:{worker.id}") == ""
    saved_run = store.get_run(run.id)
    assert saved_run.id == run.id
    assert saved_run.workers[worker.id].api_key == ""


@pytest.mark.parametrize(
    "identifier", ["", "..", "../escape", "a/b", "a\\b", "C:escape", "x.", "x" * 129]
)
def test_unsafe_ids_cannot_write_files(
    store: Store, worker: Worker, workflow: Workflow, run: Run, identifier: str
) -> None:
    for value, save in (
        (replace(worker, id=identifier), store.save_worker),
        (replace(workflow, id=identifier), store.save_workflow),
        (replace(run, id=identifier), store.save_run),
    ):
        with pytest.raises(ValueError, match="无效 ID"):
            save(value)
    assert store.list_workers() == []
    assert store.list_workflows() == []
    assert store.list_runs() == []


def test_node_result_identity_and_case_collisions_are_rejected(store: Store, run: Run) -> None:
    run.nodes = {"A": NodeRun("B")}
    with pytest.raises(ValueError, match="不一致"):
        store.save_run(run)
    run.nodes = {"A": NodeRun("A"), "a": NodeRun("a")}
    with pytest.raises(ValueError, match="文件冲突"):
        store.save_run(run)


def test_worker_id_case_collision_is_rejected(store: Store, worker: Worker) -> None:
    store.save_worker(worker)
    with pytest.raises(ValueError, match="文件冲突"):
        store.save_worker(replace(worker, id=worker.id.upper()))
    assert store.list_workers() == [worker]


@pytest.mark.parametrize("identifier", ["con", "CON", "AUX", "NUL", "COM1", "LPT1"])
def test_windows_device_names_are_saved_as_regular_files(
    store: Store, worker: Worker, workflow: Workflow, run: Run, identifier: str
) -> None:
    worker = replace(worker, id=identifier)
    workflow = replace(
        workflow,
        id=identifier,
        nodes=[replace(workflow.nodes[0], id=identifier, worker_id=identifier)],
    )
    run = replace(
        run,
        id=identifier,
        workflow=workflow,
        workers={identifier: worker},
        nodes={identifier: replace(run.nodes["A"], node_id=identifier)},
    )
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.save_run(run)
    assert store.get_worker(identifier) == worker
    assert store.get_workflow(identifier) == workflow
    assert store.get_run(identifier) == run
    store.delete_workflow(identifier)
    store.delete_worker(identifier)
    assert list((store.data_dir / "workers").iterdir()) == []
    assert list((store.data_dir / "workflows").iterdir()) == []


def test_failed_atomic_replace_preserves_existing_file(
    store: Store, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_worker(worker)
    path = store.data_dir / "workers/worker-research.json"
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise PermissionError("文件正被占用")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="无法原子保存文件.*worker-research.json"):
        store.save_worker(replace(worker, prompt="不会成功的更新"))
    assert store.get_worker(worker.id) == worker
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_worker_credentials_persist_in_keyring_never_on_disk(
    store: Store, worker: Worker, run: Run
) -> None:
    worker.base_url = "https://provider.example/v1"
    worker.api_key = "test-private-worker-key"
    worker.extra_body = {"enable_thinking": False}
    store.save_worker(worker)
    store.save_run(run)
    assert store.get_worker(worker.id) == worker
    assert worker.api_key not in repr(worker)
    assert "api_key" not in to_dict(worker)
    for path in store.data_dir.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert worker.api_key not in text
        assert '"api_key"' not in text
    reopened = Store(store.data_dir, backup_dir=store.backup_dir)
    loaded = reopened.get_worker(worker.id)
    loaded.require_api()
    assert loaded == worker
    assert reopened.list_runs()[0].workers[worker.id].api_key == ""


def test_public_snapshots_redact_secrets_in_text(store: Store, worker: Worker, run: Run) -> None:
    worker.api_key = "private-test-key"
    worker.prompt = f"提示 {worker.api_key}"
    other = replace(worker, id="other", api_key="other-private-test-key")
    run.workers[other.id] = other
    state = run.nodes["A"]
    state.output = f"结果 {worker.api_key} {other.api_key}"
    state.error = f"错误 {worker.api_key} {other.api_key}"
    state.logs.append({"message": state.error})
    store.save_worker(worker)
    store.save_run(run)
    for path in store.data_dir.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert worker.api_key not in text and other.api_key not in text
    restored = store.get_run(run.id)
    assert restored.nodes["A"].logs[-1]["message"].count("[密钥已隐藏]") == 2
    assert worker.api_key == "private-test-key"


def test_corrupted_file_isolation_and_snapshot_rollback(
    store: Store, worker: Worker, workflow: Workflow
) -> None:
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.create_backup()

    # 模拟外部误改写坏一个新文件并误删原工作流
    store.delete_workflow(workflow.id)
    bad_file = store.data_dir / "workers" / "worker-broken.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    # 读取端自动隔离损坏文件，正常返回有效 Worker
    assert store.list_workers() == [worker]
    assert "workers/worker-broken.json" in store.corrupted_files
    assert store.list_workflows() == []

    # 一键回滚恢复到备份时状态
    msg = store.rollback_latest()
    assert "1 个 Worker、1 套工作流" in msg
    assert store.corrupted_files == []
    assert store.get_workflow(workflow.id) == workflow
    assert not bad_file.exists()


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([], "至少需要"),
        ([Node("A", "自环", "research", dependencies=["A"])], "自环"),
        (
            [
                Node("A", "环一", "research", dependencies=["B"]),
                Node("B", "环二", "research", dependencies=["A"]),
            ],
            "A → B → A",
        ),
        ([Node("A", "一", "research"), Node("A", "二", "research")], "重复节点 ID"),
        ([Node("A", "一", "research"), Node("a", "二", "research")], "大小写冲突"),
        ([Node("A", "悬空", "research", dependencies=["X"])], "悬空节点"),
    ],
)
def test_save_workflow_rejects_non_dag_and_conflicts_without_writing_files(
    store: Store, nodes: list[Node], message: str
) -> None:
    invalid = Workflow("bad-flow", "冲突流程", "任务", nodes)
    with pytest.raises(ValueError, match=message):
        store.save_workflow(invalid)
    assert store.list_workflows() == []
    assert not (store.data_dir / "workflows").exists()


def test_examples_are_idempotent_and_have_three_stages(store: Store) -> None:
    seed_examples(store, "test-model")
    workers = {w.id: w for w in store.list_workers()}
    assert len(workers) == 11
    workflow = store.get_workflow("review")
    assert {n.id: n.dependencies for n in workflow.nodes} == {
        "A": [],
        "B": [],
        "C": ["A", "B"],
        "D": ["A", "B"],
        "E": ["C", "D"],
    }
    changed = replace(workers["research"], prompt="用户的提示词")
    store.save_worker(changed)
    seed_examples(store, "another-model")
    assert store.get_worker(changed.id) == changed


def test_optimistic_concurrency_and_reentrant_write_lock(
    store: Store, worker: Worker, workflow: Workflow
) -> None:
    """验证乐观并发控制（expected_mtime）可拦截多开旧副本盲覆盖，且进程锁支持同实例可重入。"""
    with store.write_lock():
        store.save_worker(worker)
        mtime1 = store.save_workflow(workflow)
    assert mtime1 == store.workflow_mtime(workflow.id)

    # 模拟另一个实例抢先修改了同一工作流
    external = replace(workflow, task="外部新实例已修改的任务")
    time_ns_after = store.save_workflow(external)
    if time_ns_after == mtime1:
        # 极端高并发下同纳秒时人工推进 mtime 确保区分
        wf_path = store.data_dir / "workflows" / f"workflow-{workflow.id}.json"
        os.utime(wf_path, ns=(mtime1 + 1_000_000, mtime1 + 1_000_000))

    stale = replace(workflow, task="本实例过期的内存修改")
    with pytest.raises(ValueError, match="已被外部实例或编排助手更新"):
        store.save_workflow(stale, expected_mtime=mtime1)
    assert store.get_workflow(workflow.id).task == "外部新实例已修改的任务"
