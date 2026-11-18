"""真实 Textual 字符屏幕中的布局、装配、命令与输出验收。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from app import DESIGNS, FlowEditor, TerminalLab, WorkerPicker, parse_flow  # noqa: E402
from model import Demo  # noqa: E402
from rendering import graph  # noqa: E402
from textual.widgets import Input, OptionList, TextArea  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("design", DESIGNS)
@pytest.mark.parametrize("size", [(136, 40), (100, 30), (80, 24)])
async def test_layout_keeps_command_and_cards_visible(design, size):
    app = TerminalLab(design)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for selector in ("#node-cards", "#prompt", "#reading-pane"):
            region = app.query_one(selector).region
            assert region.width > 0 and region.height > 0
            assert region.x >= 0 and region.right <= size[0]
            assert region.y >= 0 and region.bottom <= size[1]
        assert not app.query_one("#map-pane").can_focus
        identity = id(app.demo)
        app.execute("/run")
        await pilot.press("f3")
        await pilot.pause()
        assert id(app.demo) == identity
        assert app.demo.status == "running"
        assert app.query_one("#node-cards", OptionList).option_count == 5
        app.execute("/run stop")
        assert app.demo.status == "defeat"


@pytest.mark.asyncio
async def test_slash_completion_and_worker_assembly():
    app = TerminalLab()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("/", "n", "o", "d", "e", "tab")
        await pilot.pause()
        assert app.query_one("#prompt", Input).value == "/node "
        await pilot.press("A", "enter")
        await pilot.pause()
        assert isinstance(app.screen, WorkerPicker)
        app.screen.query_one("#worker-filter", Input).value = "writer"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.demo.selected.worker == "writer"
        assert app.demo.status == "waiting"
        assert app.screen is app.root


@pytest.mark.asyncio
async def test_card_mouse_selection_and_keyboard_navigation():
    app = TerminalLab()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#map-pane", offset=(38, 2))
        await pilot.pause()
        assert app.demo.selected_id == "A"
        await pilot.click("#node-cards", offset=(4, 4))
        await pilot.pause()
        assert app.demo.selected_id == "C"
        if isinstance(app.screen, WorkerPicker):
            await pilot.press("escape")
            await pilot.pause()
        assert app.screen is app.root
        cards = app.query_one("#node-cards", OptionList)
        cards.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.demo.selected_id == "D"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, WorkerPicker)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_editor_applies_valid_graph_and_keeps_invalid_edit_open():
    app = TerminalLab()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.execute("/flow")
        await pilot.pause()
        assert isinstance(app.screen, FlowEditor)
        editor = app.screen.query_one(TextArea)
        editor.load_text("A @research 调查\nB @writer 成稿\nA -> B\nB -> A")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, FlowEditor)
        assert len(app.demo.nodes) == 5
        editor.load_text("A @research 调查\nB @writer 成稿\nA -> B")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is app.root
        assert len(app.demo.nodes) == 2
        assert app.demo.nodes[1].deps == ("A",)


@pytest.mark.asyncio
async def test_four_states_failure_report_and_utf8_export(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "EXPORTS", tmp_path)
    app = TerminalLab("flux")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.execute("/run defeat")
        app.demo.tick(100)
        app.paint()
        assert app.demo.status == "defeat"
        assert next(node for node in app.demo.nodes if node.id == "E").started is None
        app.execute("/view result")
        app.execute("/export 评审报告.md")
        assert app.last_export.read_text(encoding="utf-8") == app.demo.artifact
        assert "模拟故障" in app.demo.artifact
        assert not app.query_one("#map-pane").display


def test_canvas_direction_and_animation_are_limited_to_running_targets():
    demo = Demo()
    assert graph(demo, 98, 0, animated=True).plain == graph(demo, 98, 1, animated=True).plain
    demo.start()
    demo.tick(5.5)
    first = graph(demo, 98, 0, animated=True).plain.splitlines()
    second = graph(demo, 98, 1, animated=True).plain.splitlines()
    assert first != second
    assert graph(demo, 50, 0, animated=True).plain != graph(demo, 50, 1, animated=True).plain
    assert "▶" in "".join(first) and "◀" not in "".join(first)
    # E 尚在等待，C/D 指向 E 的右半部保持静止。
    assert [line[55:] for line in first] == [line[55:] for line in second]
    assert all(isinstance(span.style, str) for span in graph(demo, 98, 0).spans)
    demo.tick(100)
    assert graph(demo, 98, 0, animated=True).plain == graph(demo, 98, 3, animated=True).plain
    assert graph(demo, 50, 0, animated=True).plain == graph(demo, 50, 3, animated=True).plain


def test_editor_parser_preserves_worker_and_expands_parallel_edges():
    nodes = parse_flow("A @research 调查\nB @code 分析\nC @judge 裁决\nA,B -> C")
    demo = Demo()
    demo.replace_flow(nodes)
    assert demo.nodes[2].deps == ("A", "B")
    assert demo.nodes[2].worker == "judge"


def test_asymmetric_dependencies_display_exact_sources_without_shared_bus():
    demo = Demo()
    demo.set_dependencies("C", ("A",))
    demo.set_dependencies("D", ("B",))
    lines = graph(demo, 98).plain.splitlines()
    assert any(line.startswith("A ") and "C 支持论证" in line for line in lines)
    assert any(line.startswith("B ") and "D 反对论证" in line for line in lines)
    assert any(line.startswith("C+D ") and "E 综合裁决" in line for line in lines)
