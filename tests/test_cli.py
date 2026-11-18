import json
import sys
from pathlib import Path

import pytest

from ctflow import cli
from ctflow.config import load_settings
from ctflow.engine import Engine
from ctflow.models import Usage
from ctflow.provider import Reply
from ctflow.storage import Store


class Client:
    def __init__(self, fail=False):
        self.fail = fail

    async def stream(self, model, messages, on_text):
        if self.fail:
            raise ValueError("测试模型失败")
        on_text("最终测试结果")
        return Reply("最终测试结果", usage=Usage(2, 3), status="completed")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "workspace": ".",
                "data_dir": "data",
            }
        ),
        encoding="utf-8",
    )
    return path


def inject_client(monkeypatch, fail=False):
    def factory(store, settings, on_event):
        return Engine(store, settings, client=Client(fail), on_event=on_event)

    monkeypatch.setattr(cli, "Engine", factory)


def test_cli_stdout_result_stderr_progress_and_task_override(config_path, monkeypatch, capsys):
    inject_client(monkeypatch)
    code = cli.main(["--config", str(config_path), "run", "review", "--task", "本次新任务"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out == "最终测试结果\n"
    assert "Run " in out.err
    assert "success" in out.err
    settings = load_settings(config_path)
    store = Store(settings.data_dir)
    try:
        assert store.list_runs()[0].task == "本次新任务"
        assert store.get_workflow("review").task != "本次新任务"
    finally:
        store.close()


def test_cli_runtime_failure_exit1(config_path, monkeypatch, capsys):
    inject_client(monkeypatch, fail=True)
    assert cli.main(["--config", str(config_path), "run", "review"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "测试模型失败" in out.err


def test_cli_unknown_workflow_exit2(config_path, capsys):
    assert cli.main(["--config", str(config_path), "run", "unknown"]) == 2
    assert "找不到" in capsys.readouterr().err


def test_cli_init_creates_minimal_definitions(config_path, capsys):
    assert cli.main(["--config", str(config_path), "init"]) == 0
    settings = load_settings(config_path)
    store = Store(settings.data_dir)
    workflow = store.get_workflow("review")
    try:
        assert len(workflow.nodes) == 5
        assert all(not hasattr(node, "pause_after") for node in workflow.nodes)
        assert store.list_runs() == []
    finally:
        store.close()


def test_cli_independent_worker(config_path, monkeypatch, capsys):
    inject_client(monkeypatch)
    assert cli.main(["--config", str(config_path), "worker", "judge", "--task", "独立问题"]) == 0
    assert capsys.readouterr().out == "最终测试结果\n"


@pytest.mark.parametrize("command, auto_run", [([], False), (["run", "review", "--tui"], True)])
def test_cli_tui_initial_workflow_and_auto_run(config_path, monkeypatch, command, auto_run):
    captured = {}

    class FakeApp:
        def __init__(self, store, settings, **kwargs):
            captured.update(kwargs)

        def run(self):
            captured["opened"] = True

    monkeypatch.setattr("ctflow.ui.CTFlowApp", FakeApp)
    assert cli.main(["--config", str(config_path), *command]) == 0
    assert captured["opened"]
    assert captured["workflow"].id == "review"
    assert captured["auto_run"] is auto_run


def test_models_and_live_doctor_use_selected_worker(config_path, monkeypatch, capsys):
    assert cli.main(["--config", str(config_path), "init"]) == 0
    settings = load_settings(config_path)
    store = Store(settings.data_dir)
    try:
        for key in ("code", "judge"):
            worker = store.get_worker(key)
            worker.base_url = f"https://{key}.example.test/v1"
            worker.api_key = f"test-key-{key}"
            worker.extra_body = {"vendor": key}
            store.save_worker(worker)
    finally:
        store.close()
    capsys.readouterr()
    seen = []

    class SelectedClient:
        def __init__(self, settings, worker):
            self.worker = worker
            seen.append((worker.id, worker.base_url, worker.api_key, worker.extra_body))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def list_models(self):
            return [f"{self.worker.id}/model"]

        async def stream(self, model, messages, on_text):
            assert model == self.worker.model
            return Reply("Worker连接成功", usage=Usage(1, 2), status="completed")

    monkeypatch.setattr(cli, "ModelClient", SelectedClient)
    assert cli.main(["--config", str(config_path), "models", "code"]) == 0
    assert capsys.readouterr().out == "code/model\n"
    assert cli.main(["--config", str(config_path), "doctor", "--worker", "judge", "--live"]) == 0
    output = capsys.readouterr()
    assert "Worker连接成功" in output.out
    assert "test-key" not in output.out + output.err
    assert seen == [
        ("code", "https://code.example.test/v1", "test-key-code", {"vendor": "code"}),
        ("judge", "https://judge.example.test/v1", "test-key-judge", {"vendor": "judge"}),
    ]


def test_live_doctor_requires_explicit_worker(config_path, capsys):
    assert cli.main(["--config", str(config_path), "doctor", "--live"]) == 2
    assert "--worker" in capsys.readouterr().err


def test_mcp_command_lists_worker_tools(tmp_path, capsys):
    server = Path(__file__).parent / "fixtures" / "mcp_server.py"
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "workspace": ".",
                "data_dir": "data",
                "mcp_servers": {
                    "echo": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["--config", str(path), "init"]) == 0
    settings = load_settings(path)
    store = Store(settings.data_dir)
    try:
        worker = store.get_worker("judge")
        worker.mcp_servers = ["echo"]
        store.save_worker(worker)
    finally:
        store.close()
    capsys.readouterr()
    assert cli.main(["--config", str(path), "mcp", "judge"]) == 0
    output = capsys.readouterr().out
    assert "mcp_echo_echo" in output
