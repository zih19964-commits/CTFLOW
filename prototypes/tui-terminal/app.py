"""CTflow：五种共享状态、可直接操作的终端工作台设计。"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from model import Demo, Node
from rendering import (
    BLUE,
    MUTED,
    ORANGE,
    TEXT,
    assembly_hint,
    map_content,
    node_card,
    reading,
    status_text,
)
from rich.text import Text
from rich.theme import Theme
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

DESIGNS = {
    "harbor": ("01 / HARBOR", "蓝岸 · 均衡工作台"),
    "ribbon": ("02 / RIBBON", "路线 · 紧凑编排"),
    "flux": ("03 / FLUX", "流向 · 活跃画布"),
    "folio": ("04 / FOLIO", "书页 · 成稿阅读"),
    "ledger": ("05 / LEDGER", "叙事 · 轻量记录"),
}
VIEWS = {"flow": "编排", "run": "执行", "result": "成稿"}
EXPORTS = Path(__file__).resolve().parents[2] / "artifacts" / "tui-terminal" / "exports"


@dataclass(frozen=True)
class Suggestion:
    value: str
    title: str
    hint: str = ""
    submit: bool = True

    def render(self) -> Text:
        result = Text(self.title, BLUE)
        if self.hint:
            result.append(f"  {self.hint}", MUTED)
        return result


def flow_source(demo: Demo) -> str:
    definitions = [f"{node.id} @{node.worker} {node.name}" for node in demo.nodes]
    edges = [f"{','.join(node.deps)} -> {node.id}" for node in demo.nodes if node.deps]
    return "\n".join(definitions + [""] + edges)


def parse_flow(source: str) -> list[Node]:
    nodes: dict[str, Node] = {}
    edges: list[tuple[list[str], list[str]]] = []
    for line_number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "->" in line:
            pieces = line.split("->")
            if len(pieces) != 2:
                raise ValueError(f"第 {line_number} 行：一次连线使用一个 ->")
            left, right = ([value.strip() for value in part.split(",")] for part in pieces)
            if not all(left + right):
                raise ValueError(f"第 {line_number} 行：箭头两侧需要节点 ID")
            edges.append((left, right))
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_-]*)\s+@([\w-]+)(?:\s+(.+))?", line)
        if match is None:
            raise ValueError(f"第 {line_number} 行：节点格式为 A @research 资料研究")
        node_id, worker, name = match.groups()
        if node_id in nodes:
            raise ValueError(f"第 {line_number} 行：节点 {node_id} 重复")
        nodes[node_id] = Node(id=node_id, name=name or worker, worker=worker, deps=())
    for sources, targets in edges:
        for target in targets:
            if target not in nodes:
                raise ValueError(f"连线引用了不存在的节点 {target}")
            nodes[target].deps += tuple(sources)
    return list(nodes.values())


class Pane(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static(classes="content", markup=False)

    def show_content(self, content: object, *, follow: bool = False) -> None:
        at_bottom = self.scroll_y >= self.max_scroll_y - 1
        self.query_one(Static).update(content)
        if follow and at_bottom:
            self.call_after_refresh(self.scroll_end, animate=False)


class CommandInput(Input):
    BINDINGS = [
        Binding("up", "suggest(-1)", show=False, priority=True),
        Binding("down", "suggest(1)", show=False, priority=True),
        Binding("tab", "complete", show=False, priority=True),
    ]

    def action_suggest(self, step: int) -> None:
        self.app.move_suggestion(step)

    def action_complete(self) -> None:
        self.app.accept_suggestion(execute=False)


class WorkerPicker(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "choose", show=False, priority=True),
    ]

    def __init__(self, node_id: str) -> None:
        super().__init__()
        self.node_id = node_id

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(f"{self.node_id}  装配 Worker", id="picker-title")
            yield Input(placeholder="搜索角色、名称或能力", id="worker-filter")
            yield OptionList(id="worker-options", markup=False)
            yield Static(
                "↑↓ 选择 · Enter 装配 · Esc 返回\n复用预设的模型、提示词与工具能力",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        self.refresh_options("")
        self.query_one(Input).focus()

    def refresh_options(self, query: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        for worker in self.app.demo.workers.values():
            if query.casefold() not in f"{worker.key} {worker.name} {worker.summary}".casefold():
                continue
            label = Text(f"@{worker.key}  {worker.name}\n", BLUE)
            label.append(f"{worker.model} · {worker.summary}", MUTED)
            options.add_option(Option(label, id=worker.key))
        options.highlighted = 0 if options.option_count else None

    @on(Input.Changed, "#worker-filter")
    def filter_workers(self, event: Input.Changed) -> None:
        self.refresh_options(event.value)

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            options.highlighted = ((options.highlighted or 0) + step) % options.option_count
            options.scroll_to_highlight()

    def action_choose(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self.dismiss(options.get_option_at_index(options.highlighted).id)

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FlowEditor(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("ctrl+s,ctrl+enter", "save", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-box"):
            yield Static("工作流 · 节点引用 Worker，箭头表达依赖", id="editor-title")
            yield TextArea(flow_source(self.app.demo), show_line_numbers=True, id="flow-editor")
            yield Static(
                "A @research 资料研究    A,B -> C,D\nCtrl+S 应用 · Esc 返回", id="editor-hint"
            )

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def action_save(self) -> None:
        try:
            self.app.demo.replace_flow(parse_flow(self.query_one(TextArea).text))
        except ValueError as exc:
            self.app.notify(str(exc), severity="error", timeout=5)
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class TerminalLab(App[None]):
    TITLE = "CTflow / Workbench"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", show=False, priority=True),
        Binding("ctrl+p", "commands", show=False, priority=True),
        Binding("escape", "escape", show=False, priority=True),
        *[
            Binding(f"f{index}", f"design('{name}')", show=False, priority=True)
            for index, name in enumerate(DESIGNS, 1)
        ],
    ]

    def __init__(self, design: str = "harbor") -> None:
        super().__init__()
        self.demo = Demo()
        self.design_name = design
        self.view_name = "flow"
        self.node_focused = False
        self.matches: list[Suggestion] = []
        self.frame = 0
        self.last_tick = time.monotonic()
        self._card_ids: tuple[str, ...] = ()
        self._reading_key: tuple = ()
        self._map_key: tuple = ()
        self.last_export: Path | None = None

    @property
    def root(self):
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Static(id="masthead", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="main-area"):
                yield Pane(id="map-pane", can_focus=False)
                yield Pane(id="reading-pane")
            with Vertical(id="sidebar"):
                yield OptionList(id="node-cards", markup=False)
                yield Static(id="assembly-hint", markup=False)
        yield OptionList(id="command-menu", markup=False)
        with Vertical(id="composer"):
            yield CommandInput(placeholder="描述目标，或输入 /", id="prompt", select_on_focus=False)
            yield Static(id="input-meta", markup=False)

    def on_mount(self) -> None:
        self.console.push_theme(
            Theme(
                {
                    "markdown.h1": "bold #e4e7ec",
                    "markdown.h2": "bold #75a7ff",
                    "markdown.h3": "bold #75a7ff",
                    "markdown.code": "#fab283",
                    "markdown.link": "#75a7ff",
                    "markdown.item.bullet": "#75a7ff",
                }
            )
        )
        self.root.query_one("#command-menu").display = False
        self.action_design(self.design_name)
        self.root.query_one("#prompt").focus()
        self.animation_timer = self.set_interval(0.25, self.tick)
        self.call_after_refresh(self.paint)

    def on_resize(self, event: events.Resize) -> None:
        self.root.set_class(event.size.width < 100, "narrow")
        self.root.set_class(event.size.width < 75, "tiny")
        self._map_key = ()
        self.call_after_refresh(self.paint)

    def on_key(self, event: events.Key) -> None:
        if event.character == "/" and not isinstance(self.focused, Input | TextArea):
            if self.screen is self.root:
                event.prevent_default()
                event.stop()
                self.set_prompt("/")

    def action_design(self, name: str) -> None:
        self.design_name = name
        for design in DESIGNS:
            self.root.set_class(design == name, design)
        self._map_key = ()
        self._reading_key = ()
        self.call_after_refresh(self.paint)

    def set_view(self, name: str) -> None:
        self.view_name = name
        self.node_focused = name == "run"
        self._reading_key = ()
        self._map_key = ()
        self.paint()

    def paint(self) -> None:
        if not self.is_mounted:
            return
        demo = self.demo
        heading = Text(" ctflow ", "bold " + ORANGE)
        heading.append(f" {DESIGNS[self.design_name][0]}  /  {VIEWS[self.view_name]} ", BLUE)
        heading.append(f" · {demo.workflow_name}", MUTED)
        self.root.query_one("#masthead", Static).update(heading)
        cards = self.root.query_one("#node-cards", OptionList)
        card_width = max(12, cards.content_size.width - 3)
        ids = tuple(node.id for node in demo.nodes)
        if ids != self._card_ids:
            cards.clear_options()
            cards.add_options(
                Option(node_card(demo, node, card_width, self.frame), id=node.id)
                for node in demo.nodes
            )
            self._card_ids = ids
        else:
            for node in demo.nodes:
                cards.replace_option_prompt(node.id, node_card(demo, node, card_width, self.frame))
        desired = ids.index(demo.selected_id)
        if cards.highlighted != desired:
            cards.highlighted = desired
        self.root.query_one("#assembly-hint", Static).update(assembly_hint(demo))
        meta = Text(" ")
        meta.append_text(status_text(demo.status, self.frame))
        meta.append(f"  {demo.tokens:,} tok · {demo.elapsed:.1f}s", TEXT)
        meta.append("    / 命令 · F1–F5 方案 · 本地模拟", MUTED)
        self.root.query_one("#input-meta", Static).update(meta)
        map_pane = self.root.query_one("#map-pane", Pane)
        map_pane.display = self.view_name != "result"
        width = map_pane.content_size.width
        if width > 0 and map_pane.display:
            map_key = (
                demo.revision,
                self.frame if demo.busy else 0,
                width,
                self.design_name,
                self.view_name,
            )
            if map_key != self._map_key:
                map_pane.border_title = (
                    "并行时间线 →"
                    if self.view_name == "run" and self.design_name != "flux"
                    else "工作流  ·  上游 → 下游"
                )
                map_pane.show_content(
                    map_content(demo, width, self.frame, self.design_name, self.view_name)
                )
                self._map_key = map_key
        node = demo.selected
        key = (
            self.view_name,
            self.node_focused,
            node.id,
            node.worker,
            node.output,
            node.error,
            demo.artifact,
            tuple(demo.messages),
        )
        if key != self._reading_key:
            reader = self.root.query_one("#reading-pane", Pane)
            reader.border_title = (
                "成稿 · /export"
                if self.view_name == "result"
                else ("节点输出" if self.node_focused else "任务与对话")
            )
            reader.show_content(
                reading(demo, self.view_name, focused=self.node_focused),
                follow=self.view_name != "result",
            )
            self._reading_key = key

    def tick(self) -> None:
        now = time.monotonic()
        dt, self.last_tick = now - self.last_tick, now
        if self.demo.busy:
            self.demo.tick(dt)
            self.frame += 1
            self.paint()

    @on(OptionList.OptionSelected, "#node-cards")
    def choose_node(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        node_id = event.option.id
        self.demo.select(node_id)
        self.node_focused = True
        self.paint()
        if not self.demo.busy and self.view_name == "flow":
            self.open_worker_picker(node_id)
        else:
            target = "#prompt" if self.demo.busy else "#node-cards"
            self.root.query_one(target).focus()

    @on(OptionList.OptionHighlighted, "#node-cards")
    def highlight_node(self, event: OptionList.OptionHighlighted) -> None:
        cards = self.root.query_one("#node-cards", OptionList)
        if cards.has_focus and event.option.id is not None:
            self.demo.select(event.option.id)
            self.node_focused = True
            self.call_after_refresh(self.paint)

    def open_worker_picker(self, node_id: str) -> None:
        if self.demo.busy:
            raise ValueError("运行时可查看输出；结束后再装配 Worker")

        def apply(worker: str | None) -> None:
            if worker is not None:
                self.demo.assign(node_id, worker)
                self.node_focused = True
                self.paint()
            self.root.query_one("#prompt").focus()

        self.push_screen(WorkerPicker(node_id), apply)

    def suggestions(self, value: str) -> list[Suggestion]:
        if not value.startswith("/"):
            return []
        if " " not in value:
            roots = [
                Suggestion("/flow", "/flow", "工作流 · 文本编排"),
                Suggestion("/node ", "/node", "节点 · 装配 Worker", False),
                Suggestion("/run", "/run", "执行当前工作流"),
                Suggestion("/view ", "/view", "内容与设计方案", False),
                Suggestion("/export", "/export", "导出 Markdown"),
            ]
            return [item for item in roots if value in item.value]
        command, rest = value.split(" ", 1)
        if command == "/view":
            choices = [
                Suggestion(f"/view {name}", f"{name} · {title}") for name, title in VIEWS.items()
            ]
            choices += [Suggestion(f"/view {name}", DESIGNS[name][1]) for name in DESIGNS]
        elif command == "/flow":
            choices = [
                Suggestion(f"/flow {name}", name, hint)
                for name, hint in (
                    ("review", "五节点 · 并行与汇合"),
                    ("serial", "五节点 · 串行"),
                    ("new", "从一张节点卡开始"),
                )
            ]
        elif command == "/run":
            choices = [
                Suggestion("/run stop", "stop", "停止当前运行"),
                Suggestion("/run defeat", "defeat", "体验节点失败与下游受阻"),
            ]
        elif command == "/node":
            pieces = rest.split(" ", 1)
            if len(pieces) == 2 and pieces[0] in {node.id for node in self.demo.nodes}:
                node_id, query = pieces
                choices = [
                    Suggestion(f"/node {node_id} @{key}", f"@{key} · {worker.name}", worker.summary)
                    for key, worker in self.demo.workers.items()
                ]
                return [
                    item
                    for item in choices
                    if query.casefold() in item.value.split(" ", 2)[2].casefold()
                ]
            choices = [
                Suggestion(f"/node {node.id}", f"{node.id} · {node.name}", f"@{node.worker}")
                for node in self.demo.nodes
            ]
            choices.append(
                Suggestion("/node add @writer ", "add · 添加节点", "填入节点名称", False)
            )
        else:
            return []
        return [
            item
            for item in choices
            if rest.casefold() in (item.value + item.title + item.hint).casefold()
        ]

    @on(Input.Changed, "#prompt")
    def changed(self, event: Input.Changed) -> None:
        self.matches = self.suggestions(event.value)
        menu = self.root.query_one("#command-menu", OptionList)
        menu.clear_options()
        menu.add_options(
            Option(item.render(), id=str(index)) for index, item in enumerate(self.matches)
        )
        menu.display = bool(self.matches)
        menu.highlighted = 0 if self.matches else None

    def set_prompt(self, value: str) -> None:
        prompt = self.root.query_one("#prompt", Input)
        prompt.value = value
        prompt.cursor_position = len(value)
        prompt.focus()

    def move_suggestion(self, step: int) -> None:
        menu = self.root.query_one("#command-menu", OptionList)
        if self.matches:
            menu.highlighted = ((menu.highlighted or 0) + step) % len(self.matches)
            menu.scroll_to_highlight()

    def accept_suggestion(self, *, execute: bool) -> None:
        menu = self.root.query_one("#command-menu", OptionList)
        if not self.matches or menu.highlighted is None:
            return
        item = self.matches[menu.highlighted]
        if execute and item.submit:
            self.set_prompt("")
            self.execute(item.value)
        else:
            self.set_prompt(item.value)

    @on(OptionList.OptionSelected, "#command-menu")
    def menu_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.accept_suggestion(execute=True)

    @on(Input.Submitted, "#prompt")
    def submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        exact = any(item.value.strip() == value for item in self.matches)
        if self.matches and (not exact or value in {"/node", "/view"}):
            self.accept_suggestion(execute=True)
            return
        self.set_prompt("")
        self.execute(value)

    def execute(self, value: str) -> None:
        try:
            if not value.startswith("/"):
                self.demo.plan(value, serial="串行" in value)
                self.node_focused = False
                self.set_view("flow")
            else:
                command, _, argument = value.partition(" ")
                argument = argument.strip()
                if command == "/flow":
                    if argument:
                        self.demo.load_flow(argument)
                        self.set_view("flow")
                    else:
                        if self.demo.busy:
                            raise ValueError("结束当前运行后再修改工作流")
                        self.push_screen(FlowEditor(), lambda saved: self.paint())
                elif command == "/node":
                    self.node_command(argument)
                elif command == "/run":
                    if argument == "stop":
                        self.demo.stop()
                    elif argument in {"", "defeat"}:
                        self.demo.start("defeat" if argument else "normal")
                        self.set_view("run")
                        self.last_tick = time.monotonic()
                    else:
                        raise ValueError("使用 /run 启动、/run stop 停止、/run defeat 体验失败")
                elif command == "/view":
                    if argument in DESIGNS:
                        self.action_design(argument)
                    elif argument in VIEWS:
                        self.set_view(argument)
                    elif not argument:
                        self.set_prompt("/view ")
                    else:
                        raise ValueError("输入 /view 查看内容与五种设计")
                elif command == "/export":
                    self.export(argument)
                else:
                    raise ValueError("输入 / 查看五个命令入口")
            self.paint()
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=5)
        self.root.query_one("#command-menu").display = False

    def node_command(self, argument: str) -> None:
        parts = argument.split()
        if not parts:
            self.root.query_one("#node-cards").focus()
            return
        if parts[0] == "add":
            if len(parts) < 3 or not parts[1].startswith("@"):
                raise ValueError("用法：/node add @writer 节点名称")
            self.demo.add_node(" ".join(parts[2:]), parts[1][1:])
            self.node_focused = True
            return
        self.demo.select(parts[0])
        self.node_focused = True
        if len(parts) == 1:
            if self.demo.busy or self.view_name != "flow":
                self.root.query_one("#prompt").focus()
            else:
                self.open_worker_picker(parts[0])
        elif parts[1].startswith("@") and len(parts) == 2:
            self.demo.assign(parts[0], parts[1][1:])
        elif parts[1] == "after":
            deps = tuple(part.strip() for part in "".join(parts[2:]).split(",") if part.strip())
            self.demo.set_dependencies(parts[0], deps)
        elif parts[1] == "task" and len(parts) > 2:
            self.demo.set_task(parts[0], " ".join(parts[2:]))
        else:
            raise ValueError("节点操作：/node A @worker · /node A after B,C · /node A task 任务")

    def export(self, filename: str) -> None:
        if not self.demo.artifact:
            raise ValueError("运行结束后，可导出成稿")
        if filename and (
            Path(filename).name != filename or not filename.endswith(".md") or "\\" in filename
        ):
            raise ValueError("导出文件名使用 .md，不含目录")
        name = filename or f"ctflow-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}.md"
        EXPORTS.mkdir(parents=True, exist_ok=True)
        destination = EXPORTS / name
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(self.demo.artifact)
        self.last_export = destination
        self.notify(f"已导出 {destination}", timeout=8)

    def action_commands(self) -> None:
        if self.screen is self.root:
            self.set_prompt("/")

    def action_escape(self) -> None:
        if isinstance(self.screen, WorkerPicker | FlowEditor):
            self.screen.action_cancel()
            return
        self.set_prompt("")
        self.node_focused = False
        self.root.query_one("#command-menu").display = False
        self.paint()


def main() -> None:
    parser = argparse.ArgumentParser(description="CTflow 可玩终端工作台")
    parser.add_argument("--design", choices=DESIGNS, default="harbor")
    args = parser.parse_args()
    TerminalLab(args.design).run()


if __name__ == "__main__":
    main()
