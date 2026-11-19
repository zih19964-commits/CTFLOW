"""DAG 并发调度、闭合 XML 标签上下文传导、停止、阻断、快照与多供应商连接隔离测试。"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict

import httpx
import pytest
from openai import AsyncOpenAI

from ctflow.config import Settings
from ctflow.engine import Engine
from ctflow.examples import seed_examples
from ctflow.graph import topological_layers
from ctflow.models import Node, Usage, Worker, Workflow, to_dict
from ctflow.provider import Reply
from ctflow.storage import Store

_LABEL_RE = re.compile(r'<current_node id="[^"]+" label="([^"]+)"')


def _extract_label(user_xml: str) -> str:
    match = _LABEL_RE.search(user_xml)
    assert match is not None
    return match.group(1)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.counts = Counter()
        self.started = defaultdict(asyncio.Event)
        self.releases = {}
        self.failures = set()
        self.active = 0
        self.peak = 0

    async def stream(self, model, messages, on_text):
        label = _extract_label(messages[1]["content"])
        self.counts[label] += 1
        self.calls.append((model, label, messages))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.setdefault(label, asyncio.Event()).set()
        try:
            if label in self.releases:
                await self.releases[label].wait()
            if label in self.failures:
                raise ValueError("模型失败")
            content = f"{label}的结果"
            on_text(content)
            return Reply(content, usage=Usage(10, 5, 0.001), status="completed")
        finally:
            self.active -= 1


@pytest.fixture
def setup_engine(tmp_path):
    settings = Settings(tmp_path / "config.json", tmp_path / "data", tmp_path, concurrency=2)
    store = Store(settings.data_dir, backup_dir=settings.backup_dir)
    store.save_worker(Worker("worker", "测试角色", "vendor/custom-Model:2026", "这是固定上下文"))
    client = FakeClient()
    engine = Engine(store, settings, client)
    yield engine, client, store
    store.close()


def review_graph() -> Workflow:
    return Workflow(
        "review",
        "三阶段测试",
        "总任务",
        [
            Node("A", "A", "worker"),
            Node("B", "B", "worker"),
            Node("C", "C", "worker", dependencies=["A", "B"]),
            Node("D", "D", "worker", dependencies=["A", "B"]),
            Node("E", "E", "worker", dependencies=["C", "D"]),
        ],
    )


async def started(client, label):
    await asyncio.wait_for(client.started[label].wait(), 2)


async def test_parallel_join_xml_tag_propagation_usage_and_snapshot(setup_engine):
    engine, client, store = setup_engine
    events = []
    engine.on_event = events.append
    client.releases = {key: asyncio.Event() for key in "AB"}
    workflow = review_graph()
    run = engine.create_run(workflow)
    workflow.task = "之后改动的任务"
    workflow.nodes[2].dependencies.clear()
    task = asyncio.create_task(engine.execute(run))
    await started(client, "A")
    await started(client, "B")
    client.releases["A"].set()
    await asyncio.sleep(0.02)
    assert "C" not in client.started
    client.releases["B"].set()
    assert await asyncio.wait_for(task, 3) is run
    assert run.status == "success"
    assert run.task == "总任务"
    assert run.workflow.nodes[2].dependencies == ["A", "B"]
    assert run.usage.total_tokens == 75
    assert run.usage.cost == pytest.approx(0.005)
    assert client.peak == 2
    assert client.counts == dict.fromkeys("ABCDE", 1)

    messages = {label: msgs for _, label, msgs in client.calls}
    e_user = messages["E"][1]["content"]
    assert '<workflow_input workflow_id="review" workflow_name="三阶段测试">' in e_user
    assert '<current_node id="E" label="E" layer="3">' in e_user
    assert '<upstream_outputs count="2">' in e_user
    assert '<upstream id="C" label="C" worker="@worker">\nC的结果\n</upstream>' in e_user
    assert '<upstream id="D" label="D" worker="@worker">\nD的结果\n</upstream>' in e_user
    assert "A的结果" not in e_user  # 严格仅传递直接上游
    assert '<upstream_outputs count="0" />' in messages["A"][1]["content"]
    assert store.get_run(run.id).nodes["E"].output == "E的结果"
    assert events[-1] == {"type": "run", "run_id": run.id, "node_id": None, "status": "success"}


async def test_direct_dependency_starts_without_waiting_for_unrelated_root(setup_engine):
    engine, client, _ = setup_engine
    client.releases["B"] = asyncio.Event()
    workflow = Workflow(
        "direct",
        "直接依赖",
        "任务",
        [
            Node("A", "A", "worker"),
            Node("B", "B", "worker"),
            Node("C", "C", "worker", dependencies=["A"]),
        ],
    )
    run = engine.create_run(workflow)
    task = asyncio.create_task(engine.execute(run))
    await started(client, "C")
    assert run.nodes["B"].status == "running"
    client.releases["B"].set()
    await asyncio.wait_for(task, 3)
    assert run.status == "success"


async def test_semaphore_limits_independent_requests(setup_engine):
    engine, client, _ = setup_engine
    release = asyncio.Event()
    client.releases = dict.fromkeys("ABCD", release)
    run = engine.create_run(
        Workflow("parallel", "并发上限", "任务", [Node(key, key, "worker") for key in "ABCD"])
    )
    task = asyncio.create_task(engine.execute(run))
    await started(client, "A")
    await started(client, "B")
    assert set(client.started) == {"A", "B"}
    assert run.nodes["C"].status == "ready"
    release.set()
    await asyncio.wait_for(task, 3)
    assert run.status == "success" and client.peak == 2


async def test_stop_cancels_requests_and_waiters_and_allows_new_run(setup_engine):
    engine, client, store = setup_engine
    client.releases = {key: asyncio.Event() for key in "AB"}
    run = engine.create_run(review_graph())
    task = asyncio.create_task(engine.execute(run))
    await started(client, "A")
    await started(client, "B")
    with pytest.raises(ValueError, match="正在执行"):
        await engine.execute(engine.create_run(review_graph()))
    engine.stop()
    await asyncio.wait_for(task, 3)
    assert run.status == "stopped"
    assert all(state.status == "stopped" and state.finished_at for state in run.nodes.values())
    assert not engine.active and client.active == 0
    assert store.get_run(run.id).status == "stopped"
    client.releases.clear()
    next_run = engine.create_run(review_graph())
    await engine.execute(next_run)
    assert next_run.status == "success"


async def test_unsorted_failure_blocks_descendants_but_independent_branch_finishes(setup_engine):
    engine, client, store = setup_engine
    client.failures = {"A"}
    workflow = Workflow(
        "unordered",
        "反序输入",
        "任务",
        [
            Node("C", "C", "worker", dependencies=["B"]),
            Node("B", "B", "worker", dependencies=["A"]),
            Node("A", "A", "worker"),
            Node("D", "D", "worker"),
        ],
    )
    run = engine.create_run(workflow)
    await asyncio.wait_for(engine.execute(run), 3)
    assert run.status == "failed"
    assert run.nodes["A"].status == "failed"
    assert run.nodes["B"].status == run.nodes["C"].status == "blocked"
    assert run.nodes["D"].status == "success"
    assert client.counts == {"A": 1, "D": 1}
    assert store.get_run(run.id).nodes["C"].status == "blocked"


async def test_skill_and_worker_snapshots_use_xml_tags_and_survive_edit(setup_engine):
    engine, client, store = setup_engine
    folder = engine.settings.skills_dir / "reviewer"
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text("先给结论，再列证据。", encoding="utf-8")
    worker = store.get_worker("worker")
    worker.skills = ["reviewer"]
    store.save_worker(worker)
    workflow = Workflow(
        "skill", "技能测试", "任务", [Node("A", "A", "worker", task="独立节点任务")]
    )
    run = engine.create_run(workflow, task="覆盖的总任务")
    worker.skills.clear()
    worker.prompt = "后改的上下文"
    worker.model = "后改的模型"
    store.save_worker(worker)
    path.write_text("后改的技能正文", encoding="utf-8")
    run = store.get_run(run.id)
    await engine.execute(run)
    model, _, messages = client.calls[0]
    assert model == "vendor/custom-Model:2026"
    sys_msg = messages[0]["content"]
    assert '<worker_role id="worker" name="测试角色">\n这是固定上下文\n</worker_role>' in sys_msg
    assert '<skill id="reviewer">\n先给结论，再列证据。\n</skill>' in sys_msg
    assert "<global_task>\n覆盖的总任务\n</global_task>" in messages[1]["content"]
    assert "独立节点任务" in messages[1]["content"]


def test_missing_skill_fails_at_creation_without_saving_run(setup_engine):
    engine, client, store = setup_engine
    worker = store.get_worker("worker")
    worker.skills = ["missing"]
    store.save_worker(worker)
    with pytest.raises(ValueError, match="缺少文件"):
        engine.create_run(review_graph())
    assert store.list_runs() == [] and client.calls == []


def install_mock_openai(monkeypatch, handler):
    clients = []

    def create_client(**options):
        client = AsyncOpenAI(
            **options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        clients.append(client)
        return client

    monkeypatch.setattr("ctflow.provider.AsyncOpenAI", create_client)
    return clients


def stream_response(content):
    events = [
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test-model",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    ]
    wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)


def save_connected_workers(store):
    for key in "ab":
        store.save_worker(
            Worker(
                key,
                key,
                f"model-{key}",
                f"prompt-{key}",
                base_url=f"https://{key}.example/v1",
                api_key=f"secret-{key}",
                extra_body={"metadata": {"vendor": key}},
            )
        )


async def test_real_clients_route_parallel_workers_and_join_with_snapshot_settings(
    setup_engine, monkeypatch
):
    engine, _, store = setup_engine
    engine.client = None
    engine.settings.max_tokens = 321
    engine.settings.timeout = 7
    engine.settings.prices = {"model-a": {"input": 1, "output": 2}}
    save_connected_workers(store)
    workflow = Workflow(
        "mixed",
        "不同接口并行",
        "任务",
        [
            Node("A", "节点A", "a"),
            Node("B", "节点B", "b"),
            Node("C", "节点C", "a", dependencies=["A", "B"]),
        ],
    )
    run = engine.create_run(workflow)
    edited = store.get_worker("a")
    edited.api_key = "edited-secret"
    edited.base_url = "https://edited.example/v1"
    edited.extra_body["metadata"]["vendor"] = "edited"
    store.save_worker(edited)
    both_started = asyncio.Event()
    roots = set()

    async def handler(request):
        body = json.loads(request.content)
        label = _extract_label(body["messages"][1]["content"])
        node_id = label.removeprefix("节点")
        key = "b" if node_id == "B" else "a"
        assert str(request.url) == f"https://{key}.example/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer secret-{key}"
        assert body["model"] == f"model-{key}"
        assert f"prompt-{key}" in body["messages"][0]["content"]
        if node_id in {"A", "B"}:
            roots.add(node_id)
            if roots == {"A", "B"}:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
        else:
            user_xml = body["messages"][1]["content"]
            assert '<upstream id="A" label="节点A" worker="@a">\nresult-A\n</upstream>' in user_xml
            assert '<upstream id="B" label="节点B" worker="@b">\nresult-B\n</upstream>' in user_xml
        return stream_response(f"result-{node_id}")

    clients = install_mock_openai(monkeypatch, handler)
    await asyncio.wait_for(engine.execute(run), 4)
    assert run.status == "success"
    assert len(clients) == 3 and all(c.is_closed() for c in clients)
    assert "api_key" not in json.dumps(to_dict(run))


async def test_five_worker_examples_use_supplied_model_and_three_stages(tmp_path):
    settings = Settings(tmp_path / "config.json", tmp_path / "data", tmp_path)
    store = Store(settings.data_dir, backup_dir=settings.backup_dir)
    seed_examples(store, settings.model)
    workflow = store.get_workflow("review")
    assert topological_layers(workflow) == [["A", "B"], ["C", "D"], ["E"]]
    client = FakeClient()
    engine = Engine(store, settings, client)
    run = engine.create_run(workflow)
    await engine.execute(run)
    assert run.status == "success" and len(client.calls) == 5
