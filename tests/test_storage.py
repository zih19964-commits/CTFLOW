"""持久化与示例的正式回归，所有写入都使用测试目录中的 ctflow.db。"""

import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ctflow.examples import seed_examples, single_worker
from ctflow.models import Node, NodeRun, Run, Usage, Worker, Workflow, to_dict
from ctflow.storage import Store


@pytest.fixture
def store(tmp_path: Path):
    database = Store(tmp_path / "data")
    yield database
    database.close()


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
                logs=[{"tool": "search_web", "status": "completed"}],
                evidence=["https://www.sqlite.org/"],
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
    store.close()
    reopened = Store(store.data_dir)
    try:
        assert reopened.get_worker(worker.id) == worker
    finally:
        reopened.close()


def test_workflow_round_trip_and_exports(store: Store, workflow: Workflow) -> None:
    store.save_workflow(workflow)
    assert store.get_workflow(workflow.id) == workflow
    assert store.get_workflow(workflow.name) == workflow
    assert store.list_workflows() == [workflow]
    folder = store.data_dir / "workflows"
    assert json.loads((folder / "workflow-review.json").read_text(encoding="utf-8")) == to_dict(
        workflow
    )
    assert "A" in (folder / "workflow-review.mmd").read_text(encoding="utf-8")
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


def test_sqlite_snapshot_is_authoritative(
    store: Store, worker: Worker, workflow: Workflow, run: Run
) -> None:
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.save_run(run)
    for relative in (
        "workers/worker-research.json",
        "workflows/workflow-review.json",
        "runs/run-run-1/run.json",
    ):
        (store.data_dir / relative).write_bytes(b"\xff\x00")
    assert store.get_worker(worker.id) == worker
    assert store.get_workflow(workflow.id) == workflow
    assert store.get_run(run.id) == run
    with sqlite3.connect(store.data_dir / "ctflow.db") as database:
        assert database.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.save_run(run)
    for relative in (
        "workers/worker-research.json",
        "workflows/workflow-review.json",
        "runs/run-run-1/run.json",
    ):
        assert isinstance(json.loads((store.data_dir / relative).read_text(encoding="utf-8")), dict)


def test_run_round_trip_and_frequent_updates(store: Store, run: Run) -> None:
    store.save_run(run)
    assert store.get_run(run.id) == run
    folder = store.data_dir / "runs/run-run-1"
    assert (folder / "node-A.md").read_text(encoding="utf-8") == run.nodes["A"].output + "\n"
    for index in range(5):
        run.nodes["A"].logs.append({"message": f"进度 {index}"})
        store.save_run(run)
    run.status = "failed"
    run.nodes["A"].error = "模型请求失败"
    store.save_run(run)
    assert store.get_run(run.id) == run
    assert store.get_run(run.id).usage.total_tokens == 20
    assert store.list_runs() == [run]
    assert "模型请求失败" in (folder / "node-A.md").read_text(encoding="utf-8")
    assert json.loads((folder / "run.json").read_text(encoding="utf-8")) == to_dict(run)
    store.close()
    reopened = Store(store.data_dir)
    try:
        assert reopened.get_run(run.id) == run
    finally:
        reopened.close()


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


def test_delete_definitions_keeps_historical_run(
    store: Store, worker: Worker, workflow: Workflow, run: Run
) -> None:
    store.save_worker(worker)
    store.save_workflow(workflow)
    store.save_run(run)
    store.delete_workflow(workflow.id)
    store.delete_worker(worker.id)
    assert store.list_workers() == []
    assert store.list_workflows() == []
    assert not (store.data_dir / "workers/worker-research.json").exists()
    assert not (store.data_dir / "workflows/workflow-review.json").exists()
    assert not (store.data_dir / "workflows/workflow-review.mmd").exists()
    assert store.get_run(run.id) == run


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


@pytest.mark.parametrize("node_id", ["../escape", "..\\escape", "/absolute", "C:\\escape"])
def test_node_result_paths_are_validated_before_writing(
    store: Store, run: Run, node_id: str
) -> None:
    run.nodes = {node_id: NodeRun(node_id, output="禁止越界")}
    with pytest.raises(ValueError, match="无效 ID"):
        store.save_run(run)
    assert store.list_runs() == []
    assert not (store.data_dir / "runs").exists()


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
    for relative, value in (
        (f"workers/worker-{identifier}.json", worker),
        (f"workflows/workflow-{identifier}.json", workflow),
        (f"runs/run-{identifier}/run.json", run),
    ):
        assert json.loads((store.data_dir / relative).read_text(encoding="utf-8")) == to_dict(value)
    assert identifier in (store.data_dir / "workflows" / f"workflow-{identifier}.mmd").read_text(
        encoding="utf-8"
    )
    assert (store.data_dir / "runs" / f"run-{identifier}" / f"node-{identifier}.md").read_text(
        encoding="utf-8"
    ) == run.nodes[identifier].output + "\n"
    store.delete_workflow(identifier)
    store.delete_worker(identifier)
    assert list((store.data_dir / "workers").iterdir()) == []
    assert list((store.data_dir / "workflows").iterdir()) == []
    assert store.get_run(identifier) == run


def test_failed_atomic_replace_preserves_snapshot_and_file(
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


def test_failed_delete_preserves_database_snapshot(
    store: Store, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_worker(worker)

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("文件正被占用")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(RuntimeError, match="无法删除 workers/research"):
        store.delete_worker(worker.id)
    assert store.get_worker(worker.id) == worker


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("workers", "worker"),
        ("workflows", "workflow"),
        ("runs", "run"),
        ("runs/run-run-1", "run"),
        ("runs/run-run-1/node-A.md", "run"),
    ],
)
def test_export_symlink_cannot_escape_data_dir(
    store: Store, worker: Worker, workflow: Workflow, run: Run, relative: str, kind: str
) -> None:
    outside = store.data_dir.parent / "outside"
    outside.mkdir()
    link = store.data_dir / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前环境不允许创建符号链接：{error}")
    with pytest.raises(ValueError, match="越出数据目录"):
        getattr(store, f"save_{kind}")({"worker": worker, "workflow": workflow, "run": run}[kind])
    assert list(outside.iterdir()) == []
    assert store.list_workers() == []
    assert store.list_workflows() == []
    assert store.list_runs() == []


def test_examples_are_idempotent_and_have_three_stages(store: Store) -> None:
    seed_examples(store, "test-model")
    workers = {worker.id: worker for worker in store.list_workers()}
    assert set(workers) == {
        "research",
        "code",
        "pro",
        "con",
        "judge",
        "scout-facts",
        "scout-practice",
        "scout-risks",
        "synthesis",
        "support",
        "challenge",
    }
    assert all(worker.model == "test-model" for worker in workers.values())
    assert all(worker.skills == [] for worker in workers.values())
    assert all(not worker.base_url and not worker.api_key for worker in workers.values())
    workflow = store.get_workflow("review")
    assert {node.id: node.dependencies for node in workflow.nodes} == {
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
    assert len(store.list_workers()) == 11
    assert store.get_workflow("review") == workflow
    assert {item.id for item in store.list_workflows()} == {
        "review",
        "serial-111",
        "debate-312",
        "decision-3121",
    }


def test_examples_preserve_nonempty_store(store: Store, worker: Worker) -> None:
    store.save_worker(worker)
    seed_examples(store, "test-model")
    assert store.list_workers() == [worker]
    assert store.list_workflows() == []


def test_single_worker_uses_chosen_model() -> None:
    worker = single_worker("chosen-model")
    assert worker.id == "single"
    assert worker.model == "chosen-model"
    assert worker.skills == []
    assert worker.base_url == worker.api_key == ""


def test_worker_credentials_only_persist_in_worker_database(store, worker, run):
    worker.base_url = "https://provider.example/v1"
    worker.api_key = "test-private-worker-key"
    worker.extra_body = {"enable_thinking": False}
    store.save_worker(worker)
    store.save_run(run)
    assert store.get_worker(worker.id) == worker
    assert worker.api_key not in repr(worker)
    assert worker.api_key not in repr(run)
    assert "api_key" not in to_dict(worker)
    assert "api_key" not in to_dict(run)["workers"][worker.id]
    with sqlite3.connect(store.data_dir / "ctflow.db") as database:
        saved_worker = json.loads(database.execute("SELECT snapshot FROM workers").fetchone()[0])
        saved_run = database.execute("SELECT snapshot FROM runs").fetchone()[0]
    assert saved_worker["api_key"].startswith("dpapi:v1:")
    assert saved_worker["api_key"] != worker.api_key
    assert worker.api_key not in json.dumps(saved_worker)
    assert saved_worker["base_url"] == worker.base_url
    assert saved_worker["extra_body"] == worker.extra_body
    assert worker.api_key not in saved_run and '"api_key"' not in saved_run
    for path in store.data_dir.rglob("*.json"):
        assert worker.api_key not in path.read_text(encoding="utf-8")
    stored_run = store.get_run(run.id)
    assert stored_run.workers[worker.id].api_key == ""
    assert stored_run.workers[worker.id].base_url == worker.base_url
    reopened = Store(store.data_dir)
    try:
        loaded = reopened.get_worker(worker.id)
        loaded.require_api()
        assert loaded == worker
        assert reopened.list_workers() == [worker]
        assert reopened.list_runs()[0].workers[worker.id].api_key == ""
    finally:
        reopened.close()


def test_legacy_plaintext_worker_key_is_encrypted_when_opened(tmp_path):
    data_dir = tmp_path / "data"
    database = Store(data_dir)
    worker = Worker("legacy", "旧 Worker", "test-model", api_key="legacy-plain-key")
    snapshot = json.dumps(asdict(worker), ensure_ascii=False)
    with sqlite3.connect(data_dir / "ctflow.db") as connection:
        connection.execute(
            "INSERT INTO workers (id, name, snapshot) VALUES (?, ?, ?)",
            (worker.id, worker.name, snapshot),
        )
    database.close()

    reopened = Store(data_dir)
    try:
        assert reopened.get_worker(worker.id).api_key == "legacy-plain-key"
        with sqlite3.connect(data_dir / "ctflow.db") as connection:
            raw = connection.execute("SELECT snapshot FROM workers").fetchone()[0]
        assert "legacy-plain-key" not in raw
        assert json.loads(raw)["api_key"].startswith("dpapi:v1:")
    finally:
        reopened.close()


def test_fernet_backend_is_cross_platform_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFLOW_SECRET_BACKEND", "fernet")
    data_dir = tmp_path / "data"
    database = Store(data_dir)
    worker = Worker(
        "fernet",
        "跨平台",
        "test-model",
        api_key="fernet-private-key",
        base_url="https://example.com/v1",
    )
    database.save_worker(worker)
    assert database.get_worker(worker.id) == worker
    database.close()
    with sqlite3.connect(data_dir / "ctflow.db") as connection:
        raw = connection.execute("SELECT snapshot FROM workers").fetchone()[0]
    assert json.loads(raw)["api_key"].startswith("fernet:v1:")
    assert "fernet-private-key" not in raw
    assert (data_dir / ".ctflow.key").is_file()


def test_public_snapshots_and_markdown_redact_secrets_in_text(store, worker, run):
    worker.api_key = "private-test-key"
    worker.prompt = f"提示 {worker.api_key}"
    worker.extra_body = {"metadata": [worker.api_key]}
    other = replace(worker, id="other", api_key="other-private-test-key")
    run.workers[other.id] = other
    state = run.nodes["A"]
    state.output = f"结果 {worker.api_key} {other.api_key}"
    state.error = f"错误 {worker.api_key} {other.api_key}"
    state.logs.append({"message": state.error})
    store.save_worker(worker)
    store.save_run(run)
    exported = json.dumps(to_dict(run), ensure_ascii=False)
    assert worker.api_key not in exported and other.api_key not in exported
    for path in [*store.data_dir.rglob("*.json"), *store.data_dir.rglob("*.md")]:
        text = path.read_text(encoding="utf-8")
        assert worker.api_key not in text and other.api_key not in text
    restored = store.get_run(run.id)
    assert restored.nodes["A"].logs[-1]["message"].count("[密钥已隐藏]") == 2
    assert worker.api_key == "private-test-key"  # 公开导出不改变运行内存中的连接。


def test_old_run_without_worker_connection_fields_loads(run):
    snapshot = to_dict(run)
    for worker in snapshot["workers"].values():
        worker.pop("base_url")
        worker.pop("extra_body")
    loaded = Run.from_dict(snapshot)
    worker = loaded.workers["research"]
    assert worker.base_url == worker.api_key == ""
    assert worker.extra_body == {}
    assert loaded == run
