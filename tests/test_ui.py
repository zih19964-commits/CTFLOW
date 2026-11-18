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
        assert not app.query(Button)
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
        app.screen.query_one("#field-deps", Input).value = "B"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NodeScreen)
        assert "环" in str(app.screen.query_one("#form-error", Static).render())
        app.screen.query_one("#field-label", Input).value = "不保存"
        assert await pilot.click("#dialog-close")
        await pilot.pause()
        assert store.get_workflow("first") == workflow


async def test_worker_models_arbitrary_context_skills_and_new(workbench):
    store, settings, workflow = workbench
    client = StreamClient()
    app = CTFlowApp(store, settings, workflow, client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await command(pilot, app, "/models")
        await app._model_task
        await pilot.pause()
        assert client.model_calls == 1
        assert "provider/custom-model" in app.worker_models["writer"]
        assert isinstance(app.screen, PickerScreen)
        assert app.screen.query_one("#picker-options", OptionList).option_count == 3
        await pilot.press("escape")
        await pilot.pause()
        await command(pilot, app, "/worker writer")
        assert isinstance(app.screen, WorkerScreen)
        assert "自定义上游" in app.screen.fields["base_url"][0]
        assert "MCP" in app.screen.fields["mcp_servers"][0]
        assert "协议" in app.screen.fields["protocol"][0]
        assert app.screen.query_one("#field-protocol", Select).value == "chat"
        assert app.screen.query_one("#field-name", Input).has_focus
        assert app.screen.skills == {}
        await pilot.press("escape")
        await pilot.pause()
        for name in ("analysis", "proof"):
            folder = settings.workspace / "skill" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"技能正文 {name}", encoding="utf-8")
        await command(pilot, app, "/worker writer")
        assert set(app.screen.skills) == {"analysis", "proof"}
        app.screen.query_one("#model-choice", Select).value = "provider/custom-model"
        await pilot.pause()
        assert app.screen.query_one("#field-model", Input).value == "provider/custom-model"
        app.screen.query_one("#field-model", Input).value = "any/provider/model-id"
        app.screen.query_one("#field-name", Input).value = "正式作者"
        app.screen.query_one("#field-protocol", Select).value = "responses"
        app.screen.query_one("#field-prompt", TextArea).load_text("固定上下文\n第二行")
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
        assert worker.protocol == "responses"
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
            await pilot.press("down", "tab")
            assert app.query_one("#prompt", Input).value == "/worker new"
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
        folder = settings.workspace / "skill" / f"skill-{index:02d}"
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
        assert await pilot.click("#composer", offset=(10, 2))
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
    folder = settings.workspace / "skill" / "draft-skill"
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
        assert app.worker_models["writer"] == ["configured-a", "writer/remote"]
        assert app.worker_models["reviewer"] == ["configured-b", "reviewer/remote"]
        await command(pilot, app, "/worker writer")
        assert app.screen.models == ["configured-a", "writer/remote"]
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
        assert len(app.screen.query(Button)) == 2
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
