"""单次请求核心回归：依赖、并发、停止、快照和实际用量。"""

import asyncio
import json
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
        label = messages[1]["content"].split("当前节点：", 1)[1].split("\n", 1)[0]
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
    store = Store(settings.data_dir)
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


async def test_parallel_join_context_usage_and_snapshot(setup_engine):
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
    messages = {label: messages for _, label, messages in client.calls}
    upstream = json.loads(messages["E"][1]["content"].split("直接上游结果：", 1)[1])
    assert {item["node"] for item in upstream} == {"C", "D"}
    assert len({id(value) for value in messages.values()}) == 5
    assert store.get_run(run.id).nodes["E"].output == "E的结果"
    assert engine.run is run and not engine.active
    assert events[-1] == {"type": "run", "run_id": run.id, "node_id": None, "status": "success"}
    assert {event["type"] for event in events} == {"run", "node", "log", "usage", "output"}


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
    assert client.counts == {"A": 1, "B": 1}
    assert store.get_run(run.id).status == "stopped"
    client.releases.clear()
    next_run = engine.create_run(review_graph())
    await engine.execute(next_run)
    assert next_run.status == "success"


async def test_external_cancellation_is_propagated_and_persisted(setup_engine):
    engine, client, store = setup_engine
    client.releases["A"] = asyncio.Event()
    run = engine.create_run(review_graph())
    task = asyncio.create_task(engine.execute(run))
    await started(client, "A")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.status == "stopped" and not engine.active
    assert client.active == 0
    assert all(state.status not in {"running", "ready", "waiting"} for state in run.nodes.values())
    assert store.get_run(run.id).status == "stopped"


async def test_stop_from_initial_event_prevents_all_requests(setup_engine):
    engine, client, _ = setup_engine
    engine.on_event = lambda event: engine.stop() if event.get("status") == "running" else None
    run = engine.create_run(review_graph())
    await engine.execute(run)
    assert run.status == "stopped"
    assert all(state.status == "stopped" for state in run.nodes.values())
    assert client.calls == []


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
    assert run.usage.total_tokens == 15
    assert store.get_run(run.id).nodes["C"].status == "blocked"


@pytest.mark.parametrize(
    ("reason", "calls", "content"),
    [
        ("completed", [{"id": "call"}], "请求工具"),
        ("incomplete", [], "截断结果"),
        ("failed", [], "受限结果"),
        ("", [], "连接提前结束"),
        ("completed", [], " "),
    ],
)
async def test_invalid_response_fails_once_and_keeps_paid_usage(
    setup_engine, reason, calls, content
):
    engine, client, _ = setup_engine
    requests = []

    async def stream(model, messages, on_text):
        requests.append(messages)
        on_text(content)
        return Reply(content, calls, Usage(10, 20, 0.002), reason)

    client.stream = stream
    workflow = Workflow(
        "invalid",
        "拒绝响应",
        "任务",
        [Node("A", "A", "worker"), Node("B", "B", "worker", dependencies=["A"])],
    )
    run = engine.create_run(workflow)
    await engine.execute(run)
    assert run.status == "failed"
    assert run.nodes["A"].status == "failed" and run.nodes["A"].error
    assert run.nodes["B"].status == "blocked"
    assert len(requests) == 1
    assert run.usage.total_tokens == 30
    assert run.usage.cost == pytest.approx(0.002)
    with pytest.raises(ValueError, match="创建新的 Run"):
        await engine.execute(run)
    assert len(requests) == 1


async def test_skill_and_worker_snapshots_survive_edit_and_storage_roundtrip(
    setup_engine, tmp_path
):
    engine, client, store = setup_engine
    folder = tmp_path / "skill" / "reviewer"
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
    assert messages[0]["content"] == "这是固定上下文\n\n技能 @reviewer：\n先给结论，再列证据。"
    assert "总任务：覆盖的总任务" in messages[1]["content"]
    assert "节点补充任务：独立节点任务" in messages[1]["content"]
    assert run.workers["worker"].skills == ["reviewer"]
    assert run.skill_contexts["worker"] == "技能 @reviewer：\n先给结论，再列证据。"


def test_missing_skill_fails_at_creation_without_saving_run(setup_engine):
    engine, client, store = setup_engine
    worker = store.get_worker("worker")
    worker.skills = ["missing"]
    store.save_worker(worker)
    with pytest.raises(ValueError, match="缺少文件"):
        engine.create_run(review_graph())
    assert store.list_runs() == [] and client.calls == []


async def test_errors_redact_configured_secret(setup_engine):
    engine, client, store = setup_engine
    worker = store.get_worker("worker")
    worker.api_key = "fake-private-key"
    store.save_worker(worker)

    async def stream(model, messages, on_text):
        raise ValueError("错误 fake-private-key")

    client.stream = stream
    run = engine.create_run(review_graph())
    await engine.execute(run)
    state = store.get_run(run.id).nodes["A"]
    assert state.error == "错误 [密钥已隐藏]"
    assert "fake-private-key" not in json.dumps(state.logs)


async def test_owned_client_context_is_closed(setup_engine, monkeypatch):
    engine, client, _ = setup_engine
    lifecycle = []

    class OwnedClient(FakeClient):
        async def __aenter__(self):
            lifecycle.append("enter")
            return self

        async def __aexit__(self, *exc):
            lifecycle.append("exit")

    owned = OwnedClient()
    monkeypatch.setattr("ctflow.engine.ModelClient", lambda settings, worker: owned)
    engine.client = None
    run = engine.create_run(review_graph())
    await engine.execute(run)
    assert run.status == "success"
    assert lifecycle == ["enter", "exit"] * 5
    assert not engine.active and client.calls == []


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
            Node("A", "A", "a"),
            Node("B", "B", "b"),
            Node("C", "C", "a", dependencies=["A", "B"]),
        ],
    )
    run = engine.create_run(workflow)
    edited = store.get_worker("a")
    edited.api_key = "edited-secret"
    edited.base_url = "https://edited.example/v1"
    edited.extra_body["metadata"]["vendor"] = "edited"
    store.save_worker(edited)
    assert run.workers["a"].api_key == "secret-a"
    assert run.workers["a"].extra_body == {"metadata": {"vendor": "a"}}
    assert all(not item.api_key for item in store.get_run(run.id).workers.values())
    both_started = asyncio.Event()
    roots = set()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        label = body["messages"][1]["content"].split("当前节点：", 1)[1].split("\n", 1)[0]
        requests.append((label, request, body))
        key = "b" if label == "B" else "a"
        assert str(request.url) == f"https://{key}.example/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer secret-{key}"
        assert body["model"] == f"model-{key}"
        assert body["metadata"] == {"vendor": key}
        assert body["max_tokens"] == 321
        assert body["messages"][0]["content"] == f"prompt-{key}"
        if label in {"A", "B"}:
            roots.add(label)
            if roots == {"A", "B"}:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
        else:
            upstream = json.loads(body["messages"][1]["content"].split("直接上游结果：", 1)[1])
            assert {item["node"]: item["result"] for item in upstream} == {
                "A": "result-A",
                "B": "result-B",
            }
        return stream_response(f"result-{label}")

    clients = install_mock_openai(monkeypatch, handler)
    await asyncio.wait_for(engine.execute(run), 4)
    assert run.status == "success", [node.error for node in run.nodes.values()]
    assert {label for label, _, _ in requests[:2]} == {"A", "B"}
    assert [label for label, _, _ in requests[2:]] == ["C"]
    assert len(clients) == 3 and len({id(client) for client in clients}) == 3
    assert all(client.is_closed() and client.timeout == 7 for client in clients)
    assert run.nodes["A"].usage.cost == pytest.approx(0.00002)
    assert run.nodes["B"].usage.cost is None
    assert run.usage.total_tokens == 45
    assert "secret-a" not in repr(run) and "secret-b" not in repr(run)
    assert "api_key" not in json.dumps(to_dict(run))


async def test_owned_clients_close_on_stop_and_waiting_nodes_never_create_clients(
    setup_engine, monkeypatch
):
    engine, _, store = setup_engine
    engine.client = None
    engine.settings.concurrency = 1
    save_connected_workers(store)
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    clients = install_mock_openai(monkeypatch, handler)
    run = engine.create_run(
        Workflow(
            "stop",
            "停止",
            "任务",
            [
                Node("A", "A", "a"),
                Node("B", "B", "b"),
                Node("C", "C", "a", dependencies=["A"]),
            ],
        )
    )
    task = asyncio.create_task(engine.execute(run))
    await asyncio.wait_for(started.wait(), 2)
    assert run.nodes["B"].status == "ready"
    engine.stop()
    await asyncio.wait_for(task, 3)
    assert len(clients) == 1 and clients[0].is_closed()
    assert run.status == "stopped"
    assert all(state.status == "stopped" for state in run.nodes.values())


async def test_real_request_error_redacts_all_worker_keys_and_closes_clients(
    setup_engine, monkeypatch
):
    engine, _, store = setup_engine
    engine.client = None
    save_connected_workers(store)
    events = []
    engine.on_event = events.append

    def handler(request):
        if request.url.host == "a.example":
            return httpx.Response(500, json={"error": {"message": "secret-a secret-b"}})
        return stream_response("independent result")

    clients = install_mock_openai(monkeypatch, handler)
    run = engine.create_run(
        Workflow(
            "error",
            "错误",
            "任务",
            [
                Node("A", "A", "a"),
                Node("B", "B", "b"),
                Node("C", "C", "b", dependencies=["A"]),
            ],
        )
    )
    await engine.execute(run)
    assert run.nodes["A"].status == "failed"
    assert run.nodes["B"].status == "success"
    assert run.nodes["C"].status == "blocked"
    assert run.nodes["A"].error.count("[密钥已隐藏]") == 2
    assert len(clients) == 2 and all(client.is_closed() for client in clients)
    assert "secret-a" not in repr(events) and "secret-b" not in repr(events)
    assert "secret-a" not in repr(run) and "secret-b" not in repr(run)
    for path in (store.data_dir / "runs").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "secret-a" not in text and "secret-b" not in text


async def test_missing_connection_fails_only_its_branch_after_run_creation(
    setup_engine, monkeypatch
):
    engine, _, store = setup_engine
    engine.client = None
    save_connected_workers(store)
    worker = store.get_worker("a")
    worker.api_key = ""
    store.save_worker(worker)
    clients = install_mock_openai(monkeypatch, lambda request: stream_response("result"))
    run = engine.create_run(
        Workflow(
            "missing",
            "缺少连接",
            "任务",
            [
                Node("A", "A", "a"),
                Node("B", "B", "b"),
                Node("C", "C", "a", dependencies=["A"]),
            ],
        )
    )
    assert run.status == "pending"
    await engine.execute(run)
    assert run.status == "failed"
    assert run.nodes["A"].status == "failed" and "api_key" in run.nodes["A"].error
    assert run.nodes["B"].status == "success"
    assert run.nodes["C"].status == "blocked"
    assert len(clients) == 1 and clients[0].is_closed()


async def test_five_worker_examples_use_supplied_model_and_three_stages(tmp_path):
    settings = Settings(tmp_path / "config.json", tmp_path / "data", tmp_path)
    store = Store(settings.data_dir)
    try:
        seed_examples(store, settings.model)
        workers = store.list_workers()
        workflow = store.get_workflow("review")
        assert len(workers) == 11
        assert {worker.model for worker in workers} == {"qwen3.8-max"}
        assert all(not worker.base_url and not worker.api_key for worker in workers)
        assert topological_layers(workflow) == [["A", "B"], ["C", "D"], ["E"]]
        client = FakeClient()
        engine = Engine(store, settings, client)
        run = engine.create_run(workflow)
        await engine.execute(run)
        assert run.status == "success" and len(client.calls) == 5
    finally:
        store.close()
