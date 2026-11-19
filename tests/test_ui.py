"""FLUX 交互回归：临时 SQLite、真实 Engine 和可控流式客户端。"""

import asyncio
import json
from copy import deepcopy

import pytest
from rich.cells import cell_len
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Select, SelectionList, Static, TextArea

from ctflow.config import Settings
from ctflow.examples import seed_flow_examples
from ctflow.graph import to_mermaid
from ctflow.models import Node, Usage, Worker, Workflow
from ctflow.provider import Reply
from ctflow.rendering import graph
from ctflow.storage import Store
from ctflow.ui import CTFlowApp, FlowEditor, ModelPicker, NodeScreen, PickerScreen, WorkerScreen


class StreamClient:
    def __init__(self, *, fail=False, hold=False):
        self.calls = []
        self.model_calls = 0
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        if not hold:
            self.release.set()

    async def list_models(self):
        self.model_calls += 1
        return ["remote-a", "provider/custom-model"]

    async def stream(self, model, messages, on_text):
        self.calls.append((model, messages))
        on_text("实时片段")
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.fail:
            raise ValueError("可控模型失败")
        on_text("，完整结果")
        return Reply(content="实时片段，完整结果", usage=Usage(11, 7, 0.002), status="completed")


@pytest.fixture
def workbench(tmp_path):
    settings = Settings(
        tmp_path / "config.json",
        tmp_path / "data",
        tmp_path,
        model="configured-a",
        models=["configured-a", "configured-b"],
    )
    store = Store(settings.data_dir)
    store.save_worker(
        Worker(
            "writer",
            "写作者",
            "configured-a",
            "简洁回答",
            base_url="https://writer.invalid/v1",
            api_key="test-writer-secret",
        )
    )
    store.save_worker(
        Worker(
            "reviewer",
            "复核者",
            "configured-b",
            base_url="https://reviewer.invalid/v1",
            api_key="test-reviewer-secret",
        )
    )
    workflow = Workflow(
        "first",
        "第一套",
        "总任务",
        [
            Node("A", "研究", "writer", "保留的节点任务"),
            Node("B", "汇总", "reviewer", dependencies=["A"]),
        ],
    )
    store.save_workflow(workflow)
    yield store, settings, workflow
    store.close()


async def command(pilot, app, text):
    field = app.root.query_one("#prompt", Input)
    field.focus()
    field.value = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def graph_click(pilot, app, node_id="A"):
    pane = app.root.query_one("#map-pane")
    drawing = graph(app.workflow, {}, app.selected_node, pane.content_size.width, 0)
    before = drawing.plain.split(f"{node_id} ", 1)[0]
    x, y = cell_len(before.rsplit("\n", 1)[-1]), before.count("\n")
    assert await pilot.click("#map-pane Static", offset=(x + 1, y))
    await pilot.pause()


@pytest.mark.parametrize("size", [(136, 40), (100, 30), (80, 24)])
async def test_graph_click_assembly_and_small_modal(workbench, size):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=size) as pilot:
        assert not app.root.query_one("#workbench-view").query(Button)
        canvas = app.root.query_one("#map-pane Static")
        assert not canvas.styles.link_style.underline
        assert not canvas.styles.link_style_hover.underline
        await graph_click(pilot, app)
        assert isinstance(app.screen, NodeScreen)
        assert app.screen.query_one("#field-worker", Select).value == "writer"
        app.screen.query_one("#field-worker", Select).value = "reviewer"
        app.screen.query_one("#field-label", Input).value = "新研究"
        app.screen.query_one("#field-task", TextArea).load_text("节点新任务")
        footer = app.screen.query_one(".dialog-footer")
        close = app.screen.query_one("#dialog-close")
        assert 0 <= close.region.y < size[1]
        assert 0 <= footer.region.y < size[1]
        assert len(app.screen.query(Button)) == 1
        await pilot.press("ctrl+s")
        await pilot.pause()
        node = store.get_workflow(workflow.id).nodes[0]
        assert (node.worker_id, node.label, node.task) == ("reviewer", "新研究", "节点新任务")
        assert store.get_workflow(workflow.id).nodes[1].dependencies == ["A"]
        assert app.root.query_one("#prompt").region.bottom <= size[1]


async def test_node_invalid_dependencies_and_close_discards(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(80, 24)) as pilot:
        await graph_click(pilot, app)
        app.screen.query_one("#field-deps-list", SelectionList).select("B")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NodeScreen)
        assert "环" in str(app.screen.query_one("#form-error", Static).render())
        app.screen.query_one("#field-label", Input).value = "不保存"
        assert await pilot.click("#dialog-close")
        await pilot.pause()
        assert store.get_workflow("first") == workflow


async def test_worker_models_arbitrary_context_skills_mcp_and_new(workbench):
    store, settings, workflow = workbench
    settings.mcp_servers = {"echo": {"transport": "stdio", "command": "python", "args": []}}
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(pilot, app, "/models")
        await app._model_task
        await pilot.pause()
        assert client.model_calls == 1
        assert app.worker_models["writer"] == ["remote-a", "provider/custom-model"]
        assert isinstance(app.screen, PickerScreen)
        assert app.screen.query_one("#picker-options", OptionList).option_count == 2
        await pilot.press("escape")
        await pilot.pause()
        await command(pilot, app, "/worker writer")
        assert isinstance(app.screen, WorkerScreen)
        assert "自定义上游" in app.screen.fields["base_url"][0]
        assert app.screen.query_one("#field-name", Input).has_focus
        assert app.screen.skills == {}
        await pilot.press("escape")
        await pilot.pause()
        for name in ("analysis", "proof"):
            folder = settings.skills_dir / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"技能正文 {name}", encoding="utf-8")
        await command(pilot, app, "/worker writer")
        assert set(app.screen.skills) == {"analysis", "proof"}
        app.screen.query_one("#model-choice", Select).value = "provider/custom-model"
        await pilot.pause()
        assert app.screen.query_one("#field-model", Input).value == "provider/custom-model"
        app.screen.query_one("#field-model", Input).value = "any/provider/model-id"
        app.screen.query_one("#field-name", Input).value = "正式作者"
        app.screen.query_one("#field-prompt", TextArea).load_text("固定上下文\n第二行")
        mcps = app.screen.query_one("#mcp-options", SelectionList)
        mcps.select("echo")
        skills = app.screen.query_one("#skill-options", SelectionList)
        skills.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        skills.focus()
        await pilot.press("space", "down", "space")
        assert set(skills.selected) == {"analysis", "proof"}
        await pilot.press("ctrl+s")
        await pilot.pause()
        worker = store.get_worker("writer")
        assert worker.name == "正式作者"
        assert worker.model == "any/provider/model-id"
        assert worker.mcp_servers == ["echo"]
        assert worker.prompt == "固定上下文\n第二行"
        assert set(worker.skills) == {"analysis", "proof"}
        assert all(not s.startswith("@") for s in worker.skills)
        await command(pilot, app, "/worker new")
        app.screen.query_one("#field-name", Input).value = "第三个 Worker"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(store.list_workers()) == 3
        assert store.get_worker("第三个 Worker").model == settings.model


async def test_worker_picker_and_cancel_draft(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker")
        assert isinstance(app.screen, PickerScreen)
        field = app.screen.query_one("#picker-filter", Input)
        field.value = "writer"
        await pilot.pause()
        field.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, WorkerScreen)
        app.screen.query_one("#field-name", Input).value = "丢弃草稿"
        assert await pilot.click("#dialog-close")
        await pilot.pause()
        assert store.get_worker("writer").name == "写作者"


async def test_flow_switch_autosave_source_preserves_tasks_and_restart(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "第一套新任务")
        assert len(app.workflow.nodes) == 2
        assert store.get_workflow("first").task == "第一套新任务"
        await command(pilot, app, "/flow edit")
        assert isinstance(app.screen, FlowEditor)
        app.screen.query_one("#field-name", Input).value = "第一套已改名"
        app.screen.query_one("#field-task", TextArea).load_text("表单保存的总任务")
        app.screen._switch_mode("source")
        await pilot.pause()
        app.screen.query_one("#field-source", TextArea).load_text(
            'flowchart TD\nA["改名研究 @writer"]\nB["汇总 @reviewer"]\nA --> B'
        )
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert store.get_workflow("first").nodes[0].task == "保留的节点任务"
        assert store.get_workflow("first").task == "表单保存的总任务"
        await command(pilot, app, "/flow save 第二套")
        second = deepcopy(app.workflow)
        assert second.id != "first"
        await command(pilot, app, "第二套专用任务")
        await command(pilot, app, "/flow")
        field = app.screen.query_one("#picker-filter", Input)
        field.value = "第一套"
        await pilot.pause()
        field.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.workflow.id == "first"
        assert app.workflow.task == "表单保存的总任务"
        await command(pilot, app, "/flow new 第三套")
        assert isinstance(app.screen, FlowEditor)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(store.list_workflows()) == 3
        await command(pilot, app, "/flow " + second.id)
        assert app.workflow.task == "第二套专用任务"
    restarted_store = Store(settings.data_dir)
    try:
        restarted = CTFlowApp(
            restarted_store, settings, workflow=restarted_store.get_workflow(second.id)
        )
        async with restarted.run_test(size=(80, 24)):
            assert restarted.workflow.task == "第二套专用任务"
            assert restarted_store.get_workflow("first").nodes[0].task == "保留的节点任务"
    finally:
        restarted_store.close()


async def test_invalid_flow_stays_open_and_escape_discards(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(pilot, app, "/flow edit")
        app.screen._switch_mode("source")
        await pilot.pause()
        app.screen.query_one("#field-source", TextArea).load_text(
            'flowchart TD\nA["未知 @missing"]'
        )
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, FlowEditor)
        assert "未知 Worker" in str(app.screen.query_one("#form-error", Static).render())
        await pilot.press("escape")
        await pilot.pause()
        assert store.get_workflow("first") == workflow


async def test_run_busy_stream_success_real_usage_export_and_edit_after(workbench):
    store, settings, workflow = workbench
    client = StreamClient(hold=True)
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(136, 40)) as pilot:
        await app.action_run()
        first_task = app._run_task
        assert app.busy
        await app.action_run()
        assert app._run_task is first_task
        assert len(store.list_runs()) == 1
        await asyncio.wait_for(client.started.wait(), 3)
        await pilot.pause()
        assert "实时片段" in app.view_run.nodes["A"].output
        assert "实时片段" in app.export_screenshot()
        for text in (
            "/flow edit",
            "/worker writer",
            "/flow new",
            "/flow first",
            "/node A",
            "不应保存",
        ):
            await app.execute_command(text)
            assert len(app.screen_stack) == 1
        assert store.get_workflow("first").task == "总任务"
        client.release.set()
        await asyncio.wait_for(first_task, 3)
        await pilot.pause()
        assert not app.busy
        assert app.view_run.status == "success"
        assert len(client.calls) == 2
        assert app.view_run.usage.total_tokens == 36
        assert app.view_run.usage.cost == pytest.approx(0.004)
        assert "实时片段，完整结果" in client.calls[1][1][-1]["content"]
        await command(pilot, app, "/view result")
        assert "完整结果" in app.export_screenshot()
        await command(pilot, app, "/export")
        exported = app.last_export.read_text(encoding="utf-8")
        assert app.view_run.id in exported
        assert "实时片段，完整结果" in exported
        assert "Token: 36" in exported
        await command(pilot, app, "/view flow")
        await command(pilot, app, "/worker writer")
        assert isinstance(app.screen, WorkerScreen)
        await pilot.press("escape")


@pytest.mark.parametrize("mode", ["failure", "stop", "immediate-stop", "exit"])
async def test_failure_stop_and_exit_await_tasks(workbench, mode):
    store, settings, workflow = workbench
    client = StreamClient(fail=mode == "failure", hold=mode != "failure")
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await app.action_run()
        task = app._run_task
        if mode == "immediate-stop":
            app.action_stop()
        else:
            await asyncio.wait_for(client.started.wait(), 3)
            if mode == "stop":
                await command(pilot, app, "/run stop")
        if mode != "exit":
            await asyncio.wait_for(task, 3)
            assert not app.busy
            if mode == "failure":
                assert app.view_run.status == "failed"
                assert app.view_run.nodes["A"].status == "failed"
                assert app.view_run.nodes["B"].status == "blocked"
                await command(pilot, app, "/view run")
                assert "可控模型失败" in app.export_screenshot()
            else:
                assert app.view_run.status == "stopped"
                assert all(s.status == "stopped" for s in app.view_run.nodes.values())
    assert task.done()
    assert not app.engine.active
    if mode in {"exit", "stop"}:
        assert client.cancelled.is_set()
    assert store.get_run(app.view_run.id).status in {"failed", "stopped"}


async def test_commands_keyboard_suggestions_and_empty_store(tmp_path):
    settings = Settings(tmp_path / "config.json", tmp_path / "data", tmp_path)
    store = Store(settings.data_dir)
    try:
        app = CTFlowApp(store, settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app.query_one("#command-menu").display
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/worker"
            await pilot.pause()
            await pilot.press("down", "tab")
            assert prompt.value == "/worker new"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WorkerScreen)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(store.list_workers()) == 1
            await command(pilot, app, "/flow new")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(store.list_workflows()) == 1
            await command(pilot, app, "只有一个节点的任务")
            assert len(app.workflow.nodes) == 1
            await command(pilot, app, "/export")
            assert "没有运行结果" in app.message
    finally:
        store.close()


def test_graph_animation_only_success_to_running(workbench):
    from ctflow.models import NodeRun

    _, _, workflow = workbench
    states = {"A": NodeRun("A", status="success"), "B": NodeRun("B", status="waiting")}
    assert graph(workflow, states, "A", 100, 0).plain == graph(workflow, states, "A", 100, 1).plain
    states["B"].status = "running"
    assert graph(workflow, states, "A", 100, 0).plain != graph(workflow, states, "A", 100, 1).plain
    assert to_mermaid(workflow).startswith("flowchart TD")


async def test_load_and_worker_save_clear_previous_run(workbench):
    store, settings, workflow = workbench
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.action_run()
        await app._run_task
        assert app.view_run.status == "success"
        await command(pilot, app, "/worker writer")
        app.screen.query_one("#field-model", Input).value = "new-model"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.view_run is None
        assert "new-model" in app.export_screenshot()
        await command(pilot, app, "/flow first")
        assert app.view_run is None
        assert "new-model" in app.export_screenshot()
        await command(pilot, app, "/view run")
        assert "实时片段" not in app.export_screenshot()
        await command(pilot, app, "/node B")
        assert isinstance(app.screen, NodeScreen)
        await pilot.press("escape")
        await command(pilot, app, "/node missing")
        assert "找不到节点" in app.message


async def test_pending_run_cancelled_before_coroutine_starts(workbench):
    store, settings, workflow = workbench
    client = StreamClient(hold=True)
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(80, 24)):
        await app.action_run()
        assert app.view_run.status == "pending"
        await app.on_unmount()
        assert app._run_task.done()
        assert not client.calls
        assert not app.busy
        persisted = store.get_run(app.view_run.id)
        assert persisted.status == "stopped"
        assert all(state.status == "stopped" for state in persisted.nodes.values())


async def test_auto_run_and_models_list_selection(workbench):
    store, settings, workflow = workbench
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, auto_run=True, client=client)
    async with app.run_test(size=(136, 40)) as pilot:
        await pilot.pause()
        await app._run_task
        assert len(store.list_runs()) == 1
        await command(pilot, app, "/models")
        await app._model_task
        await pilot.pause()
        field = app.screen.query_one("#picker-filter", Input)
        field.value = "provider/custom-model"
        await pilot.pause()
        field.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "provider/custom-model" in app.message


async def test_running_animation_keeps_keyboard_node_selection(workbench):
    store, settings, workflow = workbench
    client = StreamClient(hold=True)
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.action_run()
        await asyncio.wait_for(client.started.wait(), 3)
        cards = app.root.query_one("#node-cards", OptionList)
        cards.focus()
        await pilot.press("down")
        await pilot.pause(0.35)
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_node == "B"
        app.action_stop()
        await app._run_task


async def test_worker_scroll_keeps_close_and_shortcuts_visible(workbench):
    _, settings, workflow = workbench
    store = workbench[0]
    for index in range(20):
        folder = settings.skills_dir / f"skill-{index:02d}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("固定技能上下文", encoding="utf-8")
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(pilot, app, "/worker writer")
        fields = app.screen.query_one("#dialog-fields", VerticalScroll)
        fields.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert fields.scroll_y > 0
        close, footer = (
            app.screen.query_one("#dialog-close"),
            app.screen.query_one(".dialog-footer"),
        )
        assert close.region.y < 24
        assert footer.region.bottom <= 24
        assert await pilot.click("#dialog-close")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_sidebar_single_click_selects_double_click_configures(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        assert await pilot.click("#node-cards", offset=(3, 3))
        await pilot.pause()
        assert app.selected_node == "B"
        assert len(app.screen_stack) == 1
        assert await pilot.click("#node-cards", offset=(3, 3), times=2)
        await pilot.pause()
        assert len(app.screen_stack) == 2
        assert isinstance(app.screen, NodeScreen)
        assert app.screen.node.id == "B"
        await pilot.click("#dialog-close")
        await pilot.pause()
        assert await pilot.click("#node-cards", offset=(3, 10), times=2)
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_composer_click_focus_text_selection_and_steady_cursor(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.root.query_one("#prompt", Input)
        app.root.query_one("#node-cards").focus()
        assert await pilot.click("#input-meta", offset=(2, 0))
        await pilot.pause()
        assert prompt.has_focus
        assert not prompt.cursor_blink
        prompt.value = "目标 ABC"
        await pilot.pause()
        await pilot.mouse_down("#prompt", offset=(0, 0))
        await pilot.hover("#prompt", offset=(8, 0))
        await pilot.mouse_up("#prompt", offset=(8, 0))
        assert prompt.selected_text == "目标 ABC"
        await pilot.press("ctrl+c")
        assert app.clipboard == "目标 ABC"
        app.set_message("状态 XYZ")
        await pilot.pause()
        await pilot.mouse_down("#input-meta", offset=(0, 0))
        await pilot.hover("#input-meta", offset=(8, 0))
        await pilot.mouse_up("#input-meta", offset=(8, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "状态 XYZ"
        await pilot.press("ctrl+c")
        assert app.clipboard == "状态 XYZ"
        await pilot.pause(0.6)
        assert not prompt.cursor_blink


async def test_worker_refresh_models_preserves_unsaved_fields(workbench):
    store, settings, workflow = workbench
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "未保存名字"
        screen.query_one("#field-model", Input).value = "custom/draft"
        screen.query_one("#field-prompt", TextArea).load_text("未保存的上下文")
        button = screen.query_one("#refresh-models", Button)
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#refresh-models")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is screen
        assert len(app.screen_stack) == 2
        assert client.model_calls == 1
        assert "provider/custom-model" in screen.models
        assert screen.query_one("#field-name", Input).value == "未保存名字"
        assert screen.query_one("#field-model", Input).value == "custom/draft"
        assert screen.query_one("#field-prompt", TextArea).text == "未保存的上下文"
        assert not button.disabled
        assert store.get_worker("writer").name == "写作者"
        screen.query_one("#model-choice", Select).value = "provider/custom-model"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert store.get_worker("writer").model == "provider/custom-model"


@pytest.mark.parametrize("draft_window", [True, False])
async def test_model_refresh_discards_result_if_connection_changes(workbench, draft_window):
    store, settings, workflow = workbench
    started, release = asyncio.Event(), asyncio.Event()

    class Client(StreamClient):
        async def list_models(self):
            started.set()
            await release.wait()
            return ["old-provider-model"]

    app = CTFlowApp(store, settings, workflow, client=Client())
    async with app.run_test(size=(100, 30)) as pilot:
        if draft_window:
            await command(pilot, app, "/worker writer")
            screen = app.screen
            button = screen.query_one("#refresh-models", Button)
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click("#refresh-models")
            await asyncio.wait_for(started.wait(), 3)
            screen.query_one("#field-base_url", Input).value = "https://new.example.test/v1"
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "old-provider-model" not in screen.models
            assert "连接已修改" in str(screen.query_one("#form-error", Static).render())
        else:
            await command(pilot, app, "/models writer")
            await asyncio.wait_for(started.wait(), 3)
            worker = store.get_worker("writer")
            worker.base_url = "https://new.example.test/v1"
            store.save_worker(worker)
            release.set()
            await app._model_task
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert "writer" not in app.worker_models
            assert "连接已修改" in app.message


@pytest.mark.parametrize("size", [(136, 40), (100, 30), (80, 24)])
async def test_parallel_flow_canvas_shows_all_branches(workbench, size):
    store, settings, _ = workbench
    judge = deepcopy(store.get_worker("writer"))
    judge.id = "judge"
    store.save_worker(judge)
    seed_flow_examples(store, judge)
    app = CTFlowApp(store, settings, store.get_workflow("decision-3121"))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pane = app.root.query_one("#map-pane")
        assert pane.max_scroll_y == 0
        assert app.root.query_one("#reading-pane").content_size.height >= 3
        assert app.root.query_one("#prompt").region.bottom <= size[1]
        await graph_click(pilot, app, "C")
        assert isinstance(app.screen, NodeScreen)
        assert app.screen.node.worker_id == "scout-risks"


async def test_models_dialog_refresh_button_keeps_filter(workbench):
    store, settings, workflow = workbench
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/models")
        await app._model_task
        await pilot.pause()
        screen = app.screen
        screen.query_one("#picker-filter", Input).value = "provider/"
        await pilot.pause()
        assert await pilot.click("#refresh-models")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert client.model_calls == 2
        assert app.screen is screen
        assert screen.query_one("#picker-filter", Input).value == "provider/"
        assert screen.query_one("#picker-options", OptionList).option_count == 1


@pytest.mark.parametrize("close_while_loading", [False, True])
async def test_worker_refresh_error_or_close(workbench, close_while_loading):
    store, settings, workflow = workbench
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Client(StreamClient):
        async def list_models(self):
            started.set()
            if not close_while_loading:
                raise ValueError("服务商暂不可用")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    app = CTFlowApp(store, settings, workflow, client=Client())
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        screen.query_one("#field-model", Input).value = "draft-model"
        button = screen.query_one("#refresh-models", Button)
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#refresh-models")
        await asyncio.wait_for(started.wait(), 3)
        if close_while_loading:
            await pilot.click("#dialog-close")
            await asyncio.wait_for(cancelled.wait(), 3)
            await pilot.pause()
            assert len(app.screen_stack) == 1
        else:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not button.disabled
            assert "服务商暂不可用" in str(screen.query_one("#form-error", Static).render())
            assert screen.query_one("#field-model", Input).value == "draft-model"


@pytest.fixture
def provider_clients(monkeypatch):
    class FakeModelClient:
        calls: list[Worker] = []
        fail = False

        def __init__(self, settings, worker):
            self.worker = deepcopy(worker)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def list_models(self):
            self.calls.append(self.worker)
            if self.fail:
                raise ValueError(f"连接失败 · {self.worker.api_key}")
            return [f"{self.worker.id}/remote"]

    monkeypatch.setattr("ctflow.ui.ModelClient", FakeModelClient)
    return FakeModelClient


async def test_worker_connections_save_independently_and_reopen(workbench):
    store, settings, workflow = workbench
    original = store.get_worker("reviewer")
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        screen.query_one("#field-base_url", Input).value = "https://new-writer.invalid/v1"
        screen.query_one("#field-api_key", Input).value = "test-replaced-writer-secret"
        screen.query_one("#field-extra_body", TextArea).load_text('{"temperature": 0.2}')
        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = store.get_worker("writer")
        assert saved.base_url == "https://new-writer.invalid/v1"
        assert bool(saved.api_key == "test-replaced-writer-secret")
        assert saved.extra_body == {"temperature": 0.2}
        other = store.get_worker("reviewer")
        assert other.base_url == original.base_url
        assert bool(other.api_key == original.api_key)
        await command(pilot, app, "/worker writer")
        assert bool(app.screen.query_one("#field-api_key", Input).value == saved.api_key)
        await pilot.press("escape")
    reopened = Store(settings.data_dir)
    try:
        assert bool(reopened.get_worker("writer").api_key == saved.api_key)
        assert bool(reopened.get_worker("reviewer").api_key == original.api_key)
    finally:
        reopened.close()


async def test_refresh_uses_unsaved_connection_and_keeps_all_drafts(workbench, provider_clients):
    store, settings, workflow = workbench
    folder = settings.skills_dir / "draft-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("草稿技能", encoding="utf-8")
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        drafts = {
            "name": "草稿作者",
            "model": "draft-model",
            "base_url": "https://draft.invalid/v1",
            "api_key": "test-draft-secret",
        }
        for key, value in drafts.items():
            screen.query_one(f"#field-{key}", Input).value = value
        extra = '{\n  "temperature": 0.7, "options": {"thinking": true}\n}'
        screen.query_one("#field-extra_body", TextArea).load_text(extra)
        screen.query_one("#field-prompt", TextArea).load_text("草稿固定上下文")
        screen.query_one("#skill-options", SelectionList).select("draft-skill")
        await screen.refresh_models()
        await pilot.pause()
        received = provider_clients.calls[-1]
        assert received.id == "writer"
        assert received.base_url == drafts["base_url"]
        assert bool(received.api_key == drafts["api_key"])
        assert received.extra_body == json.loads(extra)
        assert all(
            screen.query_one(f"#field-{key}", Input).value == value for key, value in drafts.items()
        )
        assert screen.query_one("#field-extra_body", TextArea).text == extra
        assert screen.query_one("#field-prompt", TextArea).text == "草稿固定上下文"
        assert screen.query_one("#skill-options", SelectionList).selected == ["draft-skill"]
        assert store.get_worker("writer").base_url == "https://writer.invalid/v1"
        assert not app.worker_models  # 未保存连接的模型列表只留在当前窗。


async def test_models_are_worker_scoped_and_connection_save_clears_cache(
    workbench,
    provider_clients,
):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        for worker_id in ("writer", "reviewer"):
            await command(pilot, app, f"/models {worker_id}")
            await app._model_task
            await pilot.pause()
            assert isinstance(app.screen, ModelPicker)
            assert app.screen.worker.id == worker_id
            assert worker_id in app.screen.heading
            await pilot.press("escape")
            await pilot.pause()
        assert app.worker_models["writer"] == ["writer/remote"]
        assert app.worker_models["reviewer"] == ["reviewer/remote"]
        await command(pilot, app, "/worker writer")
        assert app.screen.models == ["writer/remote"]
        app.screen.query_one("#field-base_url", Input).value = "https://changed.invalid/v1"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "writer" not in app.worker_models
        assert "reviewer" in app.worker_models
        await command(pilot, app, "/worker writer")
        assert app.screen.models == ["configured-a"]
        await pilot.press("escape")
        await command(pilot, app, "/worker new")
        assert not app.screen.query_one("#field-base_url", Input).value
        assert not app.screen.query_one("#field-api_key", Input).value
        assert app.screen.models == [settings.model]


@pytest.mark.parametrize("close", ["escape", "button"])
async def test_password_mask_and_cancel_never_persist_credentials(workbench, close):
    store, settings, workflow = workbench
    original = store.get_worker("writer")
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        field = app.screen.query_one("#field-api_key", Input)
        field.value = "test-cancelled-secret"
        field.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert field.password
        assert "•" in field.render_line(0).text
        assert all(key not in app.export_screenshot() for key in (original.api_key, field.value))
        assert len(app.screen.query(Button)) == 3
        if close == "escape":
            await pilot.press("escape")
        else:
            await pilot.click("#dialog-close")
        await pilot.pause()
        assert bool(store.get_worker("writer").api_key == original.api_key)
        await command(pilot, app, "/worker new")
        app.screen.query_one("#field-api_key", Input).value = "test-new-cancelled-secret"
        await pilot.press("escape")
        await pilot.pause()
        assert len(store.list_workers()) == 2


async def test_models_default_selected_node_and_picker_button_target(workbench, provider_clients):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        assert await pilot.click("#node-cards", offset=(3, 3))
        await pilot.pause()
        assert app.selected_node == "B"
        await command(pilot, app, "/models")
        await app._model_task
        await pilot.pause()
        assert app.screen.worker.id == "reviewer"
        assert provider_clients.calls[-1].id == "reviewer"
        app.selected_node = "A"
        assert await pilot.click("#refresh-models")
        await app.workers.wait_for_complete()
        assert provider_clients.calls[-1].id == "reviewer"
        await pilot.press("escape")
        await pilot.pause()
        app.selected_node = ""
        await command(pilot, app, "/models")
        assert isinstance(app.screen, PickerScreen)
        app.screen.query_one("#picker-filter", Input).value = "writer"
        await pilot.pause()
        app.screen.query_one("#picker-filter", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        await app._model_task
        await pilot.pause()
        assert app.screen.worker.id == "writer"


@pytest.mark.parametrize("extra", ["[]", "null", "{bad json}"])
async def test_worker_extra_body_requires_json_object(workbench, provider_clients, extra):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        screen.query_one("#field-extra_body", TextArea).load_text(extra)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert "JSON 对象" in str(screen.query_one("#form-error", Static).render())
        await screen.refresh_models()
        assert not provider_clients.calls
        assert store.get_worker("writer").extra_body == {}


async def test_worker_empty_connection_saves_draft_but_cannot_refresh(workbench, provider_clients):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker new")
        screen = app.screen
        for required in ("name", "model"):
            field = screen.query_one(f"#field-{required}", Input)
            original = field.value
            field.value = " "
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is screen
            assert "请填写" in str(screen.query_one("#form-error", Static).render())
            field.value = original
        await screen.refresh_models()
        assert not provider_clients.calls
        assert "base_url" in str(screen.query_one("#form-error", Static).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(store.list_workers()) == 3
        new_worker = store.get_worker(screen.worker.id)
        assert not new_worker.base_url and not new_worker.api_key


async def test_draft_refresh_error_redacts_unsaved_key(workbench, provider_clients):
    store, settings, workflow = workbench
    provider_clients.fail = True
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(100, 30)) as pilot:
        await command(pilot, app, "/worker writer")
        screen = app.screen
        field = screen.query_one("#field-api_key", Input)
        field.value = "test-unsaved-error-secret"
        await screen.refresh_models()
        error = str(screen.query_one("#form-error", Static).render())
        assert bool(field.value not in error)
        assert "连接失败" in error
        assert bool(field.value not in app.export_screenshot())
        assert not screen.query_one("#refresh-models", Button).disabled


async def test_messages_and_export_redact_store_and_in_memory_run_keys(workbench):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow, client=StreamClient())
    async with app.run_test(size=(100, 30)):
        await app.action_run()
        await app._run_task
        snapshot = app.view_run.workers["writer"]
        snapshot.api_key = "test-old-run-secret"
        current = store.get_worker("writer")
        current.api_key = "test-current-store-secret"
        store.save_worker(current)
        secrets = [snapshot.api_key, current.api_key, store.get_worker("reviewer").api_key]
        raw = " ".join(secrets)
        app.set_message(raw, error=True)
        assert all(secret not in app.message for secret in secrets)
        app.view_run.nodes["A"].output = raw
        app.view_run.nodes["B"].error = raw
        app.export_result()
        exported = app.last_export.read_text(encoding="utf-8")
        assert all(secret not in exported for secret in secrets)
        app.view_run = None
        app.set_message(raw, error=True)
        assert all(secret not in app.message for secret in secrets)


async def test_hub_splash_tabs_preview_and_codex_arrow_navigation(workbench):
    store, settings, workflow = workbench
    second = Workflow(
        "second",
        "第二套评审流",
        "验证选择页切换与预览",
        [Node("A", "调研", "writer"), Node("B", "裁决", "reviewer", dependencies=["A"])],
    )
    store.save_workflow(second)
    app = CTFlowApp(store, settings, workflow=workflow, start_in_hub=True)
    async with app.run_test(size=(136, 40)) as pilot:
        await pilot.pause()
        assert app.page == "hub"
        assert app.root.query_one("#hub-view").display
        assert not app.root.query_one("#workbench-view").display
        # 验证启动画面横幅与圆角实时预览图渲染
        splash_text = str(app.root.query_one("#splash-banner", Static).render())
        assert "多 Worker 依赖图" in splash_text
        preview_text = str(app.root.query_one("#hub-preview Static", Static).render())
        assert "╭" in preview_text

        # 按 2 切换到 Workers 标签（首项置顶 @architect，下移选中业务角色），再按 1 切回 Workflows
        await pilot.press("2")
        await pilot.pause()
        assert app.hub_tab == "workers"
        assert "architect" in str(app.root.query_one("#hub-preview Static", Static).render())
        await pilot.press("down")
        await pilot.pause()
        assert "reviewer" in str(
            app.root.query_one("#hub-preview Static", Static).render()
        ) or "writer" in str(app.root.query_one("#hub-preview Static", Static).render())

        await pilot.press("1")
        await pilot.pause()
        assert app.hub_tab == "workflows"

        # 筛选并按 → 一键进入第二套工作流
        filter_box = app.root.query_one("#hub-filter", Input)
        filter_box.value = "第二套"
        await pilot.pause()
        app.root.query_one("#hub-list", OptionList).focus()
        await pilot.press("right")
        await pilot.pause()
        assert app.page == "workbench"
        assert app.workflow.id == "second"

        # 输入框有文字时按 ← 只移动光标，不误触返回 Hub
        prompt = app.root.query_one("#prompt", Input)
        prompt.focus()
        prompt.value = "abc"
        prompt.cursor_position = 3
        await pilot.press("left")
        await pilot.pause()
        assert app.page == "workbench"
        assert prompt.cursor_position == 2

        # 空输入框按 ← 丝滑退回 Hub 选择页（Codex 风格）
        prompt.value = ""
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert app.page == "hub"


async def test_upstream_switch_cleans_stale_extra_body_and_old_model(workbench, monkeypatch):
    """复现并根治痛点 1：从阿里千问切换上游到 DeepSeek 后，旧模型与旧 extra_body 零残留。"""
    store, settings, workflow = workbench
    qwen_worker = Worker(
        "qwen",
        "原阿里角色",
        "qwen3.8-max",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-old-aliyun-secret",
        extra_body={"enable_thinking": False, "enable_search": True},
    )
    store.save_worker(qwen_worker)

    class UpstreamMockClient:
        def __init__(self, settings, worker):
            self.worker = deepcopy(worker)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def list_models(self):
            assert "deepseek" in self.worker.base_url
            assert self.worker.extra_body == {}
            return ["deepseek-chat", "deepseek-reasoner"]

    monkeypatch.setattr("ctflow.ui.ModelClient", UpstreamMockClient)
    app = CTFlowApp(store, settings, workflow)
    app.worker_models["qwen"] = ["qwen3.8-max", "qwen-plus"]

    async with app.run_test(size=(120, 36)) as pilot:
        await command(pilot, app, "/worker qwen")
        screen = app.screen
        assert isinstance(screen, WorkerScreen)
        assert "enable_thinking" in screen.query_one("#field-extra_body", TextArea).text

        # 更换上游 Base URL 和 API Key
        screen.query_one("#field-base_url", Input).value = "https://api.deepseek.com/v1"
        screen.query_one("#field-api_key", Input).value = "sk-new-deepseek-secret"
        await pilot.pause()

        # 旧上游专属 extra_body 与模型下拉列表已自动净化清空
        assert json.loads(screen.query_one("#field-extra_body", TextArea).text) == {}
        assert screen.models == []

        # 点击刷新模型：不残留 qwen3.8-max，自动切换到新上游首个模型 deepseek-chat
        await screen.refresh_models()
        await pilot.pause()
        assert screen.models == ["deepseek-chat", "deepseek-reasoner"]
        assert "qwen3.8-max" not in screen.models
        assert screen.query_one("#field-model", Input).value == "deepseek-chat"

        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = store.get_worker("qwen")
        assert saved.base_url == "https://api.deepseek.com/v1"
        assert saved.model == "deepseek-chat"
        assert saved.extra_body == {}


async def test_visual_flow_builder_templates_add_edit_delete_without_mermaid(workbench):
    """验证痛点 3：零手写 Mermaid 语法，可视化模板 + 节点增删 + 多选上游依赖构建工作流。"""
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(120, 36)) as pilot:
        await command(pilot, app, "/flow new 可视化新流程")
        editor = app.screen
        assert isinstance(editor, FlowEditor)
        assert editor.mode == "visual"

        # 1. 点选一键模板：2→2→1 并行评审
        btn_review = editor.query_one("#tpl-review", Button)
        btn_review.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#tpl-review")
        await pilot.pause()
        assert len(editor.draft_workflow.nodes) == 5

        # 2. 点击「+ 添加节点」，在 NodeScreen 勾选上游 E 并保存
        btn_add = editor.query_one("#flow-add-node", Button)
        btn_add.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#flow-add-node")
        await pilot.pause()
        assert isinstance(app.screen, NodeScreen)
        app.screen.query_one("#field-label", Input).value = "归档输出"
        deps = app.screen.query_one("#field-deps-list", SelectionList)
        deps.deselect_all()
        deps.select("E")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, FlowEditor)
        assert len(editor.draft_workflow.nodes) == 6
        added = next(n for n in editor.draft_workflow.nodes if n.label == "归档输出")
        assert added.dependencies == ["E"]

        # 3. 切到 Mermaid 源码确认双向同步，再切回可视化并保存
        editor._switch_mode("source")
        await pilot.pause()
        assert "归档输出" in editor.query_one("#field-source", TextArea).text
        editor._switch_mode("visual")
        await pilot.pause()

        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = store.get_workflow("可视化新流程")
        assert len(saved.nodes) == 6
        assert app.workflow.id == saved.id


async def test_single_worker_chat_mcp_and_architect_flow_apply_rollback(workbench):
    """验证单聊 MCP 工具展示、@architect 隔离不可删除，以及 /flow apply 建图与 /rollback 回滚。"""
    import sys
    from pathlib import Path

    mcp_script = Path(__file__).parent / "fixtures" / "mcp_server.py"
    store, settings, workflow = workbench
    settings.mcp_servers = {
        "echo": {"transport": "stdio", "command": sys.executable, "args": [str(mcp_script)]}
    }
    writer = store.get_worker("writer")
    writer.mcp_servers = ["echo"]
    store.save_worker(writer)

    class ChatToolClient:
        def __init__(self) -> None:
            self.step = 0

        def append_tool_round(self, messages, reply, results):
            for call, res in results:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": res})

        async def stream(self, model, messages, on_text, tools=None):
            self.step += 1
            if self.step == 1:
                return Reply(
                    tool_calls=[
                        {"id": "t1", "name": "mcp_echo_echo", "arguments": '{"text":"CTflow"}'}
                    ],
                    usage=Usage(6, 2, 0.001),
                    status="tool_calls",
                )
            if self.step == 2:
                assert "MCP: CTflow" in messages[-1]["content"]
                answer = "已调用 MCP，返回：MCP: CTflow"
                on_text(answer)
                return Reply(answer, usage=Usage(9, 5, 0.002), status="completed")
            # @architect 回复带 mermaid 代码块
            mermaid_ans = (
                "为你生成如下双节点评审图：\n\n"
                '```mermaid\nflowchart TD\n    A["初稿 @writer"] --> B["复核 @reviewer"]\n```'
            )
            on_text(mermaid_ans)
            return Reply(mermaid_ans, usage=Usage(12, 8, 0.002), status="completed")

    app = CTFlowApp(store, settings, workflow, client=ChatToolClient(), start_in_hub=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        # Hub 切到 Workers：首项为 @architect 且禁止删除
        await pilot.press("2")
        await pilot.pause()
        listing = app.root.query_one("#hub-list", OptionList)
        assert listing.get_option_at_index(0).id == "architect"
        await pilot.press("d")
        await pilot.pause()
        assert "不可删除" in app.message

        # 筛选进入 writer 单聊，验证 MCP 多步调用卡片
        app.root.query_one("#hub-filter", Input).value = "writer"
        await pilot.pause()
        listing.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.page == "chat" and app.chat_worker_id == "writer"

        await command(pilot, app, "请测试 MCP 工具")
        assert app._chat_task is not None
        await asyncio.wait_for(app._chat_task, 3)
        await pilot.pause()

        screen_text = app.export_screenshot()
        assert "mcp_echo_echo" in screen_text
        assert "已调用" in screen_text and "CTflow" in screen_text
        assert app.chat_session.turns[0].assistant == "已调用 MCP，返回：MCP: CTflow"
        assert not (settings.data_dir / "chats").exists()

        await command(pilot, app, "/clear")
        assert app.chat_session.turns == []

        # 切换到 @architect（关闭外部 npx 以纯对话测试 /flow apply 建图与 /rollback 回滚）
        await command(pilot, app, "/chat architect")
        assert app.chat_worker_id == "architect"
        app.settings.architect["base_url"] = "https://arch.invalid/v1"
        app.store._secrets.set("architect", "arch-key")
        from unittest.mock import patch

        with patch("ctflow.architect.shutil.which", return_value=None):
            await command(pilot, app, "帮我设计一个初稿加复核的流程")
            assert app._chat_task is not None
            await asyncio.wait_for(app._chat_task, 3)
            await pilot.pause()

        await command(pilot, app, "/flow apply 自动生成流")
        assert app.page == "workbench"
        assert app.workflow.name == "自动生成流"
        assert len(store.list_workflows()) == 2

        # 一键回滚到建图前的快照
        await command(pilot, app, "/rollback")
        assert len(store.list_workflows()) == 1
        assert app.workflow.id == "first"


@pytest.mark.parametrize("size", [(136, 40), (100, 30), (80, 24)])
async def test_rounded_canvas_and_multi_size_hub_layouts(workbench, size):
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow, start_in_hub=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.page == "hub"
        assert app.root.query_one("#hub-footer").region.bottom <= size[1]
        # 进入工作台验证圆角节点（宽屏）与底部输入框不越界
        await pilot.press("enter")
        await pilot.pause()
        assert app.page == "workbench"
        assert app.root.query_one("#prompt").region.bottom <= size[1]
        if size[0] >= 100:
            drawing = graph(workflow, {}, "A", 80, 0).plain
            assert "╭" in drawing and "╰" in drawing and "┌" not in drawing


async def test_hub_tab_no_white_flash_and_worker_single_vs_double_click(workbench):
    """验证 Hub 标签切换无中间底色跳变，且 Worker 列表单击预览、双击才进入。"""
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow, start_in_hub=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        workers_btn = app.root.query_one("#hub-tab-workers", Button)
        base_bg = workers_btn.styles.background
        assert workers_btn.active_effect_duration == 0
        assert workers_btn.styles.background_tint.a == 0

        # 点击切换到 Workers 标签，背景色与 tint 保持完全恒定，焦点回到列表
        assert await pilot.click("#hub-tab-workers")
        await pilot.pause()
        assert app.hub_tab == "workers"
        assert workers_btn.styles.background == base_bg
        assert workers_btn.styles.background_tint.a == 0
        assert workers_btn.styles.tint.a == 0
        workers_btn.add_class("-active")
        assert workers_btn.styles.background == base_bg
        assert workers_btn.styles.tint.a == 0
        workers_btn.remove_class("-active")

        listing = app.root.query_one("#hub-list", OptionList)
        assert listing.has_focus
        second_id = listing.get_option_at_index(1).id

        # 鼠标单击第二项（每项 2 行高，offset y=2 命中第二项）：仅切换右侧详情预览，不进入单聊
        assert await pilot.click("#hub-list", offset=(4, 2))
        await pilot.pause()
        assert app.page == "hub"
        assert listing.highlighted == 1
        preview_text = str(app.root.query_one("#hub-preview Static", Static).render())
        assert f"@{second_id}" in preview_text

        # 鼠标双击第二项：进入对应 Worker 单聊
        assert await pilot.click("#hub-list", offset=(4, 2), times=2)
        await pilot.pause()
        assert app.page == "chat"
        assert app.chat_worker_id == second_id


async def test_no_link_underlines_sidebar_labels_dedup_chat_and_no_emojis(workbench):
    """验证所有链接无下划线、侧栏操作文案、单聊按钮去重、单聊顶栏精简及跨平台无表情符号。"""
    store, settings, workflow = workbench
    banned_icons = set("💬⚙✎❖⌘◷⚠▶★◈◌◎◉⊘■↻")
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        for selector in ("#masthead", "#sidebar-actions", "#assembly-hint", "#map-pane Static"):
            widget = app.root.query_one(selector, Static)
            assert not widget.styles.link_style.underline
            assert not widget.styles.link_style_hover.underline

        masthead_text = str(app.root.query_one("#masthead", Static).render())
        sidebar_actions_text = str(app.root.query_one("#sidebar-actions", Static).render())
        hint_text = str(app.root.query_one("#assembly-hint", Static).render())

        assert "[单聊]" not in masthead_text
        assert "[单聊]" in hint_text
        assert "[配置 Worker]" in hint_text
        assert "+节点 · 编排 · →运行" in sidebar_actions_text
        assert not (banned_icons & set(app.export_screenshot()))

        # 进入单聊模式验证顶栏已移除 [配置] 与 [新对话]
        await command(pilot, app, "/chat writer")
        chat_head_text = str(app.root.query_one("#masthead", Static).render())
        assert "[配置]" not in chat_head_text
        assert "[新对话]" not in chat_head_text
        assert "[工作台]" in chat_head_text
        assert not (banned_icons & set(app.export_screenshot()))


async def test_chat_color_distinction_new_alias_and_universal_copy_command(workbench):
    """验证单聊问答颜色区分、/clear (new) 双触发词，以及单聊和工作流通用的 /copy 命令。"""
    from rich.console import Group
    from rich.markdown import Markdown

    from ctflow.rendering import BLUE, ORANGE, USER_TEXT

    store, settings, workflow = workbench

    class TurnClient:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, model, messages, on_text, tools=None):
            self.turn += 1
            text = f"第{self.turn}轮回复内容 · 附带密钥 test-writer-secret"
            on_text(text)
            return Reply(content=text, usage=Usage(8, 4, 0.001), status="completed")

    app = CTFlowApp(store, settings, workflow, client=TurnClient())
    async with app.run_test(size=(120, 36)) as pilot:
        await command(pilot, app, "/chat writer")
        await command(pilot, app, "第一轮我的提问")
        await asyncio.wait_for(app._chat_task, 3)
        await command(pilot, app, "第二轮我的追问")
        await asyncio.wait_for(app._chat_task, 3)
        await pilot.pause()

        # 验证提问与回复采用不同颜色组件渲染
        visual = app.root.query_one("#chat-pane Static", Static).render()
        rendered = getattr(visual, "_renderable", visual)
        assert isinstance(rendered, Group)
        items = list(rendered.renderables)
        user_headers = [x for x in items if hasattr(x, "plain") and "你 · 第 1 轮" in x.plain]
        user_bodies = [x for x in items if hasattr(x, "plain") and x.plain == "第一轮我的提问"]
        ai_headers = [x for x in items if hasattr(x, "plain") and "@writer · 写作者" in x.plain]
        md_blocks = [x for x in items if isinstance(x, Markdown)]
        assert user_headers and ORANGE in str(user_headers[0].style)
        assert user_bodies and USER_TEXT in str(user_bodies[0].style)
        assert ai_headers and BLUE in str(ai_headers[0].spans[0].style)
        assert len(md_blocks) == 2

        # 单聊模式下 /copy 复制最后一轮回答，并自动脱敏密钥
        await command(pilot, app, "/copy")
        assert "第2轮回复内容" in app.clipboard
        assert "test-writer-secret" not in app.clipboard
        assert "第 2 轮回答" in app.message

        # 验证命令提示展示 /clear (new)，且可用 /new 调用开启新对话
        prompt = app.root.query_one("#prompt", Input)
        prompt.value = "/ne"
        await pilot.pause()
        assert any(label == "/clear (new)" and cmd == "/new" for cmd, label, _ in app.matches)
        await pilot.press("tab")
        assert prompt.value == "/new"
        await pilot.press("enter")
        await pilot.pause()
        assert app.chat_session.turns == []

        # 切换到工作台验证通用 /copy 复制工作流最终汇合节点结果
        await command(pilot, app, "/hub")
        await pilot.press("1", "enter")
        await pilot.pause()
        assert app.page == "workbench"
        await command(pilot, app, "/copy")
        assert "没有运行结果" in app.message

        await app.action_run()
        await asyncio.wait_for(app._run_task, 3)
        await pilot.pause()
        await command(pilot, app, "/copy")
        # 节点 B 是终点节点（第 4 次模型调用），复制的是终点节点最终结果且脱敏
        assert "第4轮回复内容" in app.clipboard
        assert "第3轮回复内容" not in app.clipboard
        assert "test-writer-secret" not in app.clipboard
        assert "已复制最终结果" in app.message


def test_3d_splash_logo_rendering():
    """验证仪表盘双栏选择页横幅与开场动画：橙 CT + 蓝 FLOW 分块滑落咬合、能量轨双闪、右侧快捷栏。"""
    import math

    from rich.cells import cell_len

    from ctflow.rendering import _SOLID_CHARS, _hex_to_rgb, _render_logo_row, render_splash

    # 1. 入场第 0 秒至终态：等高 9 行隐形占位，每行终端单元格宽度严格一致，不跳高不抖动
    frame_start = render_splash(5, 3, 2, anim_time=0.0, width=116)
    frame_final = render_splash(5, 3, 2, anim_time=math.inf, width=116)
    assert frame_start.plain.count("\n") == frame_final.plain.count("\n") == 8
    assert "╔" not in frame_start.plain
    final_lines = frame_final.plain.splitlines()
    assert len({cell_len(line) for line in final_lines}) == 1
    assert all("│" in line for line in final_lines)
    assert "快速开始" in frame_final.plain and "常用命令" in frame_final.plain

    # 2. 阶段 1（t=0.30s）：左侧 CT 模块正从顶部滑落咬合，右侧 FLOW 尚未入场
    logo_row_ct = _render_logo_row(0, anim_time=0.30, animated=True)
    assert any(ch != " " for ch in logo_row_ct.plain[:18])
    assert logo_row_ct.plain[20:].strip() == ""

    # 3. 阶段 2（t=0.85s）与阶段 3（t=1.16s 脉冲双闪 / t=1.52s 定影高光）：FLOW 级联落位并触发闪光
    logo_row_flow = _render_logo_row(0, anim_time=0.85, animated=True)
    assert "█" in logo_row_flow.plain[20:]
    frame_flash = render_splash(5, 3, 2, anim_time=1.16, width=116)
    assert "━" * 20 in frame_flash.plain
    logo_strobe = _render_logo_row(0, anim_time=1.52, animated=True)
    logo_final_row0 = _render_logo_row(0, anim_time=math.inf, animated=False)
    assert logo_strobe != logo_final_row0

    # 4. 定格终态：保留 CT 明快暖橙色系 + FLOW 清澈亮蓝色系，底座收束为双色短轨
    assert "━━━━ · ━━━━" in frame_final.plain
    ct_rgb = [
        _hex_to_rgb(str(sp.style))
        for sp in logo_final_row0.spans
        if str(sp.style).startswith("#") and sp.start < 18
    ]
    flow_rgb = [
        _hex_to_rgb(str(sp.style))
        for sp in logo_final_row0.spans
        if str(sp.style).startswith("#") and 20 <= sp.start < 58
    ]
    ct_solid_rgb = [
        _hex_to_rgb(str(sp.style))
        for sp in logo_final_row0.spans
        if str(sp.style).startswith("#")
        and sp.start < 18
        and any(ch in _SOLID_CHARS for ch in logo_final_row0.plain[sp.start : sp.end])
    ]
    flow_solid_rgb = [
        _hex_to_rgb(str(sp.style))
        for sp in logo_final_row0.spans
        if str(sp.style).startswith("#")
        and 20 <= sp.start < 58
        and any(ch in _SOLID_CHARS for ch in logo_final_row0.plain[sp.start : sp.end])
    ]
    assert ct_rgb and all(r > g > b for r, g, b in ct_rgb)
    assert ct_solid_rgb and min(r for r, _, _ in ct_solid_rgb) >= 220
    assert flow_rgb and all(b > g >= r or b > r for r, g, b in flow_rgb)
    assert flow_solid_rgb and min(b for _, _, b in flow_solid_rgb) >= 230

    # 5. 窄屏自动收起右栏保持单栏居中，紧凑模式降级为单行双色 CTFLOW
    narrow = render_splash(5, 3, 2, anim_time=math.inf, width=72)
    assert "│" not in narrow.plain and "多 Worker 依赖图" in narrow.plain
    compact = render_splash(5, 3, 2, compact=True)
    assert "CTFLOW" in compact.plain
    assert "\n" not in compact.plain


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
async def test_flow_editor_scrolling_visual_and_source(workbench, size):
    """验证工作流可视化编排与源码模式均支持滚轮与键盘翻页滚动，预览拓扑自适应宽度且固定顶底栏。"""
    from textual import events

    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=size) as pilot:
        await command(pilot, app, "/flow new 滚动验证流程")
        editor = app.screen
        assert isinstance(editor, FlowEditor)
        fields = editor.query_one("#dialog-fields", VerticalScroll)
        assert fields.max_scroll_y > 0

        # 鼠标滚轮在表单子控件上向下滚动可驱动外层滚动区
        name_input = editor.query_one("#field-name", Input)
        name_input.post_message(
            events.MouseScrollDown(
                name_input,
                x=2,
                y=1,
                delta_x=0,
                delta_y=1,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
            )
        )
        await pilot.pause()
        assert fields.scroll_y > 0

        # 焦点位于尚未溢出的总任务 TextArea 时，PageUp / PageDown 转发给外层弹窗滚动
        fields.scroll_home(animate=False, immediate=True)
        task_area = editor.query_one("#field-task", TextArea)
        task_area.focus()
        await pilot.pause()
        assert task_area.max_scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert fields.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert fields.scroll_y == 0

        # 套用 2→2→1 五节点三阶段模板，滚到底部后完整展示拓扑预览框，且不超出容器宽度折行
        tpl_btn = editor.query_one("#tpl-review", Button)
        tpl_btn.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#tpl-review")
        await pilot.pause()
        fields.scroll_end(animate=False, immediate=True)
        await pilot.pause()

        preview_box = editor.query_one("#flow-preview", Static)
        rendered_preview = preview_box.render()
        max_line_cells = max(cell_len(line) for line in str(rendered_preview).splitlines())
        assert max_line_cells <= preview_box.content_size.width
        assert preview_box.region.bottom <= fields.region.bottom

        close_btn = editor.query_one("#dialog-close", Button)
        footer = editor.query_one(".dialog-footer", Static)
        assert 0 <= close_btn.region.y < size[1]
        assert footer.region.bottom <= size[1]

        # 切换到 Mermaid 源码模式：长源码内部优先响应翻页，到底后继续翻动外层弹窗
        fields.scroll_home(animate=False, immediate=True)
        editor._switch_mode("source")
        await pilot.pause()
        if size[1] <= 24:
            assert fields.max_scroll_y > 0
        source_area = editor.query_one("#field-source", TextArea)
        many_lines = "flowchart TD\n" + "\n".join(f'N{i}["节点{i} @writer"]' for i in range(1, 22))
        source_area.load_text(many_lines)
        source_area.focus()
        await pilot.pause()
        assert source_area.max_scroll_y > 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert source_area.scroll_y > 0


async def test_dag_conflict_live_feedback_and_save_blocking(workbench):
    """验证自环、回环与 ID 冲突在可视化装配、源码编辑与保存层均实时报错并禁止保存。"""
    store, settings, workflow = workbench
    app = CTFlowApp(store, settings, workflow)
    async with app.run_test(size=(110, 32)) as pilot:
        # 1. NodeScreen 编辑起点 A：下游 B 标注成环提示，勾选后立即实时报错且禁止 Ctrl+S 保存
        await graph_click(pilot, app, "A")
        assert isinstance(app.screen, NodeScreen)
        deps_list = app.screen.query_one("#field-deps-list", SelectionList)
        option_prompt = str(deps_list.get_option_at_index(0).prompt)
        assert "勾选将成环" in option_prompt
        deps_list.select("B")
        await pilot.pause()
        live_err = str(app.screen.query_one("#form-error", Static).render())
        assert "A → B → A" in live_err and "禁止保存" in live_err
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NodeScreen)
        assert store.get_workflow("first") == workflow

        # 取消勾选冲突依赖后错误自动消除，允许正常保存
        deps_list.deselect("B")
        await pilot.pause()
        assert str(app.screen.query_one("#form-error", Static).render()) == ""
        await pilot.press("escape")
        await pilot.pause()

        # 2. NodeScreen 新增节点：填入大小写冲突 ID 'a' 实时报错并禁止保存
        await command(pilot, app, "/node new")
        assert isinstance(app.screen, NodeScreen)
        app.screen.query_one("#field-node_id", Input).value = "a"
        await pilot.pause()
        assert "大小写冲突" in str(app.screen.query_one("#form-error", Static).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NodeScreen)
        await pilot.press("escape")
        await pilot.pause()

        # 3. FlowEditor 源码模式：自环与多跳回环实时预警，禁止切回可视化且禁止保存
        await command(pilot, app, "/flow edit")
        editor = app.screen
        assert isinstance(editor, FlowEditor)
        editor._switch_mode("source")
        await pilot.pause()
        source_box = editor.query_one("#field-source", TextArea)

        source_box.load_text('flowchart TD\nA["自环 @writer"] --> A')
        await pilot.pause()
        assert "自环" in str(editor.query_one("#form-error", Static).render())
        editor._switch_mode("visual")
        assert editor.mode == "source"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is editor

        source_box.load_text(
            'flowchart TD\nA["一 @writer"] --> B["二 @reviewer"] --> C["三 @writer"] --> A'
        )
        await pilot.pause()
        err_text = str(editor.query_one("#form-error", Static).render())
        assert "A → B → C → A" in err_text and "禁止保存" in err_text
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is editor
        await pilot.press("escape")
        await pilot.pause()
        assert store.get_workflow("first") == workflow

        # 4. App 控制层 save_workflow 强校验防线：直接传入成环 Workflow 同样抛出异常禁止落盘
        bad = Workflow(
            "first",
            "非法环",
            "任务",
            [
                Node("A", "一", "writer", dependencies=["B"]),
                Node("B", "二", "reviewer", dependencies=["A"]),
            ],
        )
        with pytest.raises(ValueError, match="A → B → A"):
            app.save_workflow(bad)
        assert store.get_workflow("first") == workflow
