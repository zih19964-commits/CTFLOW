"""CTflow Studio (FLUX v2) 终端工作台：启动选择页、可视化编排、多步执行与单 Worker 对话。"""

from __future__ import annotations

import asyncio
import ctypes
import math
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group
from rich.markdown import Markdown
from rich.rule import Rule
from rich.style import Style
from rich.text import Text
from rich.theme import Theme
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ctflow import __version__
from ctflow.builtin_tools import BUILTIN_TOOL_CATALOG
from ctflow.chat import (
    ChatSession,
    export_chat_markdown,
    load_latest_chat,
    run_chat_turn,
)
from ctflow.engine import Engine
from ctflow.graph import to_mermaid, validate_workflow
from ctflow.models import Node, NodeRun, Run, Worker, Workflow, new_id, now, upstream_label
from ctflow.provider import ModelClient
from ctflow.rendering import (
    BLUE,
    GREEN,
    LINE,
    MUTED,
    ORANGE,
    RED,
    SPLASH_FREEZE_AT,
    TEXT,
    USER_TEXT,
    extract_tool_steps,
    fit,
    format_tool_steps_markdown,
    graph,
    render_splash,
    status_text,
    workflow_summary,
)
from ctflow.ui_screens import (
    ConfirmScreen,
    Dialog,
    FlowEditor,
    FormScreen,
    ModelPicker,
    NodeScreen,
    PickerScreen,
    WorkerScreen,
    _next_node_id,
)

if TYPE_CHECKING:
    from ctflow.config import Settings
    from ctflow.storage import Store

__all__ = [
    "CTFlowApp",
    "ConfirmScreen",
    "Dialog",
    "FlowEditor",
    "FormScreen",
    "ModelPicker",
    "NodeScreen",
    "PickerScreen",
    "WorkerScreen",
]


def elapsed(result: NodeRun | Run) -> str:
    start = result.created_at if isinstance(result, Run) else result.started_at
    if start is None:
        return "—"
    end = datetime.fromisoformat(result.finished_at) if result.finished_at else datetime.now(UTC)
    return f"{max(0, (end - datetime.fromisoformat(start)).total_seconds()):.1f}s"


def money(cost: float | None, currency: str) -> str:
    return "费用待配置" if cost is None else f"{cost:.4f} {currency}"


def _link(label: str, action: str, color: str = BLUE, *, bold: bool = False) -> tuple[str, Style]:
    return (label, Style(color=color, bold=bold, meta={"@click": f"app.{action}"}))


def _copy_system_clipboard(text: str) -> None:
    """向操作系统原生剪贴板同步写入文本，补充终端 OSC 52 在长文本或部分壳层下的限制。"""
    if sys.platform == "win32":
        cf_unicodetext = 13
        gmem_moveable = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        raw = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return
        handle = None
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(gmem_moveable, len(raw))
            if not handle:
                return
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                return
            try:
                ctypes.memmove(locked, raw, len(raw))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(cf_unicodetext, handle):
                kernel32.GlobalFree(handle)
        finally:
            user32.CloseClipboard()
        return

    for cmd in (
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=1.5, check=False)
            except OSError:
                pass
            return


class HubOptionList(OptionList):
    """Hub 选择页列表：Enter 直接进入，鼠标单击仅高亮切换预览（双击由 App 处理进入）。"""

    BINDINGS = [Binding("enter", "activate_item", show=False, priority=True)]

    def action_activate_item(self) -> None:
        self.app._activate_hub_item()


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

    def on_key(self, event: events.Key) -> None:
        # 借鉴 Codex CLI：空输入框按 ← 直接退回 Workflow/Worker 选择页，输入文字时正常移动光标。
        if event.key == "left" and not self.value:
            event.stop()
            event.prevent_default()
            self.app.action_back_to_hub()
        elif event.key in {"pageup", "pagedown"}:
            event.stop()
            event.prevent_default()
            self.app.scroll_active_pane(up=event.key == "pageup")

    def action_suggest(self, step: int) -> None:
        self.app.move_suggestion(step)

    def action_complete(self) -> None:
        self.app.accept_suggestion()


COMMANDS = [
    ("/hub", "返回工作流与 Worker 选择页（快捷键 ←）"),
    ("/chat", "进入单 Worker 独立对话 · /chat <worker_id>"),
    ("/copy", "复制最后回答或工作流最终结果"),
    ("/clear (new)", "单聊模式下开启新对话"),
    ("/worker", "选择 Worker 配置"),
    ("/worker new", "新增 Worker"),
    ("/models", "刷新所选节点 Worker 的模型 · /models <worker_id>"),
    ("/flow", "选择并加载工作流"),
    ("/flow new", "可视化新建工作流"),
    ("/flow edit", "可视化编辑当前工作流"),
    ("/flow save", "保存 · 加名称则另存一套"),
    ("/node", "装配节点 · /node <id> 或 /node new"),
    ("/node new", "向当前工作流新增节点"),
    ("/run", "运行当前工作流"),
    ("/run stop", "停止当前运行或对话"),
    ("/view flow", "编排视图"),
    ("/view run", "执行视图（含工具步骤）"),
    ("/view result", "结果汇总视图"),
    ("/export", "导出运行结果或单聊记录"),
]


def _match_command_choice(label: str, typed: str) -> str | None:
    """根据命令展示标签与当前输入匹配实际可执行命令，支持 /clear (new) 双触发词。"""
    if label == "/clear (new)":
        if "/new".startswith(typed):
            return "/new"
        if "/clear new".startswith(typed) and typed.startswith("/clear "):
            return "/clear new"
        if "/clear".startswith(typed) or label.startswith(typed):
            return "/clear"
        return None
    return label if label.startswith(typed) else None


class CTFlowApp(App[None]):
    TITLE = "CTflow Studio"
    CSS_PATH = "ui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", show=False, priority=True),
        Binding("ctrl+p", "commands", show=False),
        Binding("ctrl+k", "back_to_hub", show=False),
        Binding("escape", "escape", show=False),
    ]

    def __init__(
        self,
        store: Store,
        settings: Settings,
        workflow: Workflow | None = None,
        auto_run: bool = False,
        client: ModelClient | None = None,
        *,
        start_in_hub: bool = False,
    ) -> None:
        super().__init__()
        self.store, self.settings, self.client = store, settings, client
        workflows = store.list_workflows()
        self.workflow = deepcopy(workflow) if workflow else (workflows[0] if workflows else None)
        self.auto_run = auto_run
        self.page = "hub" if (start_in_hub and not auto_run) else "workbench"
        self.hub_tab = "workflows"
        self.hub_query = ""
        self.hub_items: list[tuple[str, str]] = []
        self.worker_models: dict[str, list[str]] = {}
        self.view_run: Run | None = None
        self.view_name = "flow"
        self.selected_node = (
            self.workflow.nodes[0].id if self.workflow and self.workflow.nodes else ""
        )
        self.chat_worker_id: str = ""
        self.chat_session: ChatSession | None = None
        self.chat_busy = False
        self.engine = Engine(store, settings, client=client, on_event=self.on_engine_event)
        self.busy = False
        self._run_task: asyncio.Task | None = None
        self._model_task: asyncio.Task | None = None
        self._chat_task: asyncio.Task | None = None
        self._stop_requested = False
        self._shutting_down = False
        self.frame = 0
        self.matches: list[tuple[str, str, str]] = []
        self._suggestion_moved = False
        self.message = "输入目标保存总任务 · / 打开命令 · 空框按 ← 返回选择页"
        self.last_export: Path | None = None
        self._cards_key: tuple = ()
        self._reading_key: tuple = ()
        self._chat_key: tuple = ()
        self._splash_start: float = 0.0
        self._splash_frozen: bool = True
        self._splash_counts: tuple[int, int, int] = (0, 0, 0)
        self._splash_timer = None

    @property
    def _ui_ready(self) -> bool:
        return self.is_mounted and not self._shutting_down and bool(self.screen_stack)

    @property
    def root(self):
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Static(id="masthead", markup=False)

        with Vertical(id="hub-view"):
            yield Static(id="splash-banner", markup=False)
            with Horizontal(id="hub-toolbar"):
                yield Button("Workflows", id="hub-tab-workflows", classes="hub-tab active")
                yield Button("Workers", id="hub-tab-workers", classes="hub-tab")
                yield Button("Runs", id="hub-tab-runs", classes="hub-tab")
                yield Input(placeholder="按 / 筛选名称、ID 或模型…", id="hub-filter")
            with Horizontal(id="hub-body"):
                with Vertical(id="hub-list-box"):
                    yield HubOptionList(id="hub-list", markup=False)
                yield Pane(id="hub-preview", can_focus=False)
            yield Static(id="hub-footer", markup=False)

        with Vertical(id="workbench-view"):
            with Horizontal(id="workspace"):
                with Vertical(id="main-area"):
                    yield Pane(id="map-pane", can_focus=False)
                    yield Pane(id="reading-pane")
                with Vertical(id="sidebar"):
                    yield Static(id="sidebar-actions", markup=False)
                    yield OptionList(id="node-cards", markup=False)
                    yield Static(id="assembly-hint", markup=False)

        with Vertical(id="chat-view"):
            yield Pane(id="chat-pane")

        yield OptionList(id="command-menu", markup=False)
        with Vertical(id="composer"):
            yield CommandInput(
                placeholder="描述目标，或输入 /（空框按 ← 返回选择页）",
                id="prompt",
                select_on_focus=False,
            )
            yield Static(id="input-meta", markup=False)

    def on_mount(self) -> None:
        self.console.push_theme(
            Theme(
                {
                    "markdown.h1": "bold " + TEXT,
                    "markdown.h2": "bold " + BLUE,
                    "markdown.h3": "bold " + ORANGE,
                    "markdown.code": ORANGE,
                    "markdown.link": BLUE,
                    "markdown.item.bullet": BLUE,
                    "markdown.block_quote": GREEN,
                }
            )
        )
        for selector in ("#masthead", "#sidebar-actions", "#assembly-hint"):
            self.root.query_one(selector, Static).auto_links = False
        for tab_btn in self.root.query(".hub-tab"):
            if isinstance(tab_btn, Button):
                tab_btn.active_effect_duration = 0
        splash = self.root.query_one("#splash-banner", Static)
        splash.border_title = f"CTflow v{__version__}"
        self.root.query_one("#map-pane", Pane).query_one(Static).auto_links = False
        self.root.query_one("#command-menu").display = False
        prompt = self.root.query_one("#prompt", Input)
        prompt.cursor_blink = False
        if self.page == "hub" and not self.is_headless:
            self._splash_start = time.monotonic()
            self._splash_frozen = False
            self._splash_timer = self.set_interval(0.04, self._tick_splash)
        self._apply_page_visibility()
        self.render_current()
        self.set_message(self.message)
        self.set_interval(0.16, self.tick)
        if self.page == "hub":
            self.root.query_one("#hub-list", OptionList).focus()
        else:
            prompt.focus()
        if self.auto_run:
            self.call_after_refresh(self.action_run)

    def on_resize(self, event: events.Resize) -> None:
        self.root.set_class(event.size.width < 104, "compact")
        self.root.set_class(event.size.height < 28, "short")
        if self.is_mounted:
            self.call_after_refresh(self.render_current)

    def tick(self) -> None:
        if self.busy or self.chat_busy:
            self.frame += 1
            self.render_current()

    def _freeze_splash(self) -> None:
        self._splash_frozen = True
        if self._splash_timer is not None:
            self._splash_timer.stop()
            self._splash_timer = None

    def _tick_splash(self) -> None:
        if not self._ui_ready or self.page != "hub" or self._splash_frozen:
            return
        self._update_splash_banner()

    def _update_splash_banner(self) -> None:
        if not self._ui_ready:
            return
        banner = self.root.query_one("#splash-banner", Static)
        compact_splash = self.size.height < 30 or self.size.width < 82
        inner_width = (
            banner.content_size.width
            if banner.content_size.width > 0
            else max(60, self.size.width - 8)
        )
        if self._splash_frozen:
            anim_time = math.inf
        else:
            elapsed_s = max(0.0, time.monotonic() - self._splash_start)
            if elapsed_s >= SPLASH_FREEZE_AT:
                self._freeze_splash()
                anim_time = math.inf
            else:
                anim_time = elapsed_s
        worker_cnt, workflow_cnt, run_cnt = self._splash_counts
        banner.update(
            render_splash(
                worker_cnt,
                workflow_cnt,
                run_cnt,
                compact=compact_splash,
                anim_time=anim_time,
                width=inner_width,
            )
        )

    def _apply_page_visibility(self) -> None:
        if self.page != "hub":
            self._freeze_splash()
        self.root.query_one("#hub-view").display = self.page == "hub"
        self.root.query_one("#workbench-view").display = self.page == "workbench"
        self.root.query_one("#chat-view").display = self.page == "chat"
        self.root.query_one("#composer").display = self.page != "hub"
        if self.page == "hub":
            self.root.query_one("#command-menu").display = False

    def render_current(self) -> None:
        if not self._ui_ready:
            return
        if self.page == "hub":
            self.render_hub()
        elif self.page == "chat":
            self.render_chat()
        else:
            self.render_workbench()

    def scroll_active_pane(self, *, up: bool) -> None:
        if not self._ui_ready:
            return
        selector = "#chat-pane" if self.page == "chat" else "#reading-pane"
        pane = self.root.query_one(selector, Pane)
        if up:
            pane.scroll_page_up(animate=False)
        else:
            pane.scroll_page_down(animate=False)

    def _write_clipboard(self, text: str) -> None:
        self.copy_to_clipboard(text)
        if not self.is_headless:
            _copy_system_clipboard(text)

    def set_message(self, text: str, *, error: bool = False) -> None:
        self.message = self.redact(text)
        if self._ui_ready:
            self.root.query_one("#input-meta", Static).update(
                Text(self.message, RED if error else MUTED)
            )
            if self.page == "hub":
                self._render_hub_footer(error=error)

    def redact(self, text: str) -> str:
        workers = self.store.list_workers()
        if self.view_run:
            workers.extend(self.view_run.workers.values())
        if self.engine.run and self.engine.run is not self.view_run:
            workers.extend(self.engine.run.workers.values())
        # 先遮盖长密钥，避免短密钥替换后留下长密钥的尾部。
        for worker in sorted(workers, key=lambda w: len(w.api_key), reverse=True):
            text = worker.redact(text)
        return text

    def require_editable(self) -> None:
        if self.busy or self.chat_busy:
            raise ValueError("任务执行中，请用 /run stop 停止后再编辑或切换")

    def current_workflow(self) -> Workflow:
        if self.workflow is None:
            raise ValueError("请先 /worker new 配置 Worker，再 /flow new 创建工作流")
        return self.workflow

    # ==================== 页面导航动作（Codex 风格 ← / →） ====================
    def action_back_to_hub(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if self.page == "hub":
            return
        self.page = "hub"
        self._apply_page_visibility()
        self.render_hub()
        self.call_after_refresh(self.render_hub)
        self.root.query_one("#hub-list", OptionList).focus()

    def action_open_workbench(self) -> None:
        self.page = "workbench"
        self._apply_page_visibility()
        prompt = self.root.query_one("#prompt", Input)
        prompt.placeholder = "描述目标，或输入 /（空框按 ← 返回选择页）"
        self.render_workbench()
        self.call_after_refresh(self.render_workbench)
        prompt.focus()

    def action_switch_view(self, name: str) -> None:
        if name in {"flow", "run", "result"}:
            self.view_name = name
            self.page = "workbench"
            self._apply_page_visibility()
            self.render_workbench()

    def action_quick_flow_picker(self) -> None:
        try:
            self.require_editable()
            self._open_flow_picker()
        except ValueError as exc:
            self.set_message(str(exc), error=True)

    def action_add_node_quick(self) -> None:
        try:
            self.require_editable()
            workflow = deepcopy(self.current_workflow())
            workers = self.store.list_workers()
            if not workers:
                raise ValueError("请先新增 Worker")
            next_id = _next_node_id({n.id for n in workflow.nodes})
            deps = (
                [self.selected_node] if self.selected_node in {n.id for n in workflow.nodes} else []
            )
            new_node = Node(next_id, f"步骤 {next_id}", workers[0].id, "", deps)

            def on_added(node: Node | None) -> None:
                if node is not None:
                    updated = deepcopy(self.current_workflow())
                    updated.nodes.append(node)
                    self.selected_node = node.id
                    self.save_workflow(updated)

            self.push_screen(NodeScreen(new_node, workflow, workers, is_new=True), on_added)
        except ValueError as exc:
            self.set_message(str(exc), error=True)

    def action_edit_flow_quick(self) -> None:
        try:
            self.open_flow_editor()
        except ValueError as exc:
            self.set_message(str(exc), error=True)

    def action_toggle_run(self) -> None:
        if self.busy:
            self.action_stop()
        else:
            asyncio.create_task(self.action_run())

    def action_open_chat_current(self) -> None:
        worker_id = ""
        if self.workflow and self.selected_node:
            node = next((n for n in self.workflow.nodes if n.id == self.selected_node), None)
            if node:
                worker_id = node.worker_id
        if not worker_id:
            workers = self.store.list_workers()
            worker_id = workers[0].id if workers else ""
        if not worker_id:
            self.set_message("请先新增 Worker 再开启对话", error=True)
            return
        self.open_chat(worker_id)

    def action_edit_selected_worker(self) -> None:
        if self.workflow and self.selected_node:
            node = next((n for n in self.workflow.nodes if n.id == self.selected_node), None)
            if node:
                try:
                    self.open_worker(node.worker_id)
                except ValueError as exc:
                    self.set_message(str(exc), error=True)

    def action_clear_chat(self) -> None:
        if not self.chat_worker_id:
            return
        if self.chat_busy:
            self.set_message("对话回复中，请先 /run stop 停止", error=True)
            return
        worker = self.store.get_worker(self.chat_worker_id)
        self.chat_session = ChatSession(new_id(), worker.id, worker.name, worker.model)
        self._chat_key = ()
        self.set_message(f"已开启 @{worker.id} 新对话")
        self.render_chat()

    # ==================== Hub 选择页逻辑 ====================
    def _hub_selected_key(self) -> str | None:
        listing = self.root.query_one("#hub-list", OptionList)
        if listing.highlighted is None or not listing.option_count:
            return None
        return listing.get_option_at_index(listing.highlighted).id

    def _set_hub_tab(self, tab: str) -> None:
        self.hub_tab = tab
        for name in ("workflows", "workers", "runs"):
            btn = self.root.query_one(f"#hub-tab-{name}", Button)
            btn.set_class(name == tab, "active")
        self.render_hub()
        if self._ui_ready and not self.root.query_one("#hub-filter", Input).has_focus:
            self.root.query_one("#hub-list", OptionList).focus()

    def _render_hub_footer(self, *, error: bool = False) -> None:
        hints = {
            "workflows": (
                "↑↓/单击 预览 · 双击/Enter 进入 · n 新建 · e 编辑"
                " · r 运行 · c 复制 · d 删除 · / 筛选"
            ),
            "workers": (
                "↑↓/单击 预览 · 双击/Enter 单聊 · n 新建 · e 配置 · m 刷新模型 · d 删除 · / 筛选"
            ),
            "runs": "↑↓/单击 预览 · 双击/Enter 回放 · 1/2/3 切标签 · / 筛选",
        }[self.hub_tab]
        footer = self.root.query_one("#hub-footer", Static)
        if error:
            footer.update(Text(self.message, RED))
        else:
            footer.update(Text(hints, MUTED))

    def render_hub(self) -> None:
        if not self._ui_ready:
            return
        workers = self.store.list_workers()
        workflows = self.store.list_workflows()
        runs = self.store.list_runs(limit=30)

        head = Text()
        head.append(" CTflow ", "bold " + ORANGE)
        head.append(" 选择页 ", BLUE)
        head.append("  ·  ", LINE)
        head.append("1 Workflows  2 Workers  3 Runs  ·  双击或 → 进入", MUTED)
        if self.workflow:
            head.append("   ")
            head.append(
                *_link(f"返回当前：{fit(self.workflow.name, 22)} ›", "open_workbench()", BLUE)
            )
        self.root.query_one("#masthead", Static).update(head)

        self._splash_counts = (len(workers), len(workflows), len(runs))
        self._update_splash_banner()

        self.root.query_one("#hub-tab-workflows", Button).label = f"Workflows ({len(workflows)})"
        self.root.query_one("#hub-tab-workers", Button).label = f"Workers ({len(workers)})"
        self.root.query_one("#hub-tab-runs", Button).label = f"Runs ({len(runs)})"

        query = self.hub_query.strip().casefold()
        listing = self.root.query_one("#hub-list", OptionList)
        prev_key = self._hub_selected_key()
        listing.clear_options()
        list_box = self.root.query_one("#hub-list-box")

        if self.hub_tab == "workflows":
            list_box.border_title = "工作流列表 · 单击预览 / 双击或 Enter 加载"
            latest_by_flow: dict[str, Run] = {}
            for item in runs:
                latest_by_flow.setdefault(item.workflow.id, item)
            for wf in workflows:
                haystack = f"{wf.id} {wf.name} {wf.task}".casefold()
                if query and query not in haystack:
                    continue
                is_current = self.workflow is not None and wf.id == self.workflow.id
                last_run = latest_by_flow.get(wf.id)
                row = Text()
                row.append("▸ " if is_current else "  ", ORANGE if is_current else MUTED)
                row.append(fit(wf.name, 26), "bold " + TEXT if is_current else TEXT)
                row.append(f"  ({len(wf.nodes)} 节点)\n", MUTED)
                row.append(f"  {fit(workflow_summary(wf), 34)}", BLUE)
                if last_run:
                    row.append("  ·  ", LINE)
                    row.append_text(status_text(last_run.status))
                listing.add_option(Option(row, id=wf.id))
        elif self.hub_tab == "workers":
            list_box.border_title = "Worker 角色库 · 单击预览 / 双击或 Enter 对话"
            for wk in workers:
                haystack = f"{wk.id} {wk.name} {wk.model} {wk.base_url} {wk.protocol}".casefold()
                if query and query not in haystack:
                    continue
                ready = bool(wk.base_url.strip() and wk.api_key.strip() and wk.model.strip())
                row = Text()
                row.append("● " if ready else "○ ", GREEN if ready else ORANGE)
                row.append(f"@{wk.id} · {wk.name}", TEXT)
                row.append(f"  [{wk.protocol}]\n", BLUE)
                tool_count = len(wk.builtin_tools) + len(wk.mcp_servers)
                extra_info = f" · {tool_count} 工具" if tool_count else ""
                model_str = fit(wk.model or "未设模型", 22)
                host_str = fit(upstream_label(wk.base_url), 22)
                row.append(f"  {model_str} · {host_str}{extra_info}", MUTED)
                listing.add_option(Option(row, id=wk.id))
        else:
            list_box.border_title = "历史运行 · 单击预览 / 双击或 Enter 回放"
            for item in runs:
                haystack = f"{item.id} {item.workflow.name} {item.task} {item.status}".casefold()
                if query and query not in haystack:
                    continue
                row = Text()
                row.append_text(status_text(item.status))
                row.append(f"  {item.workflow.name}\n", TEXT)
                stamp = item.created_at[:19].replace("T", " ")
                row.append(
                    f"  {stamp} · {item.usage.total_tokens} tok · {elapsed(item)}",
                    MUTED,
                )
                listing.add_option(Option(row, id=item.id))

        if listing.option_count:
            target_idx = 0
            if prev_key is not None:
                for idx in range(listing.option_count):
                    if listing.get_option_at_index(idx).id == prev_key:
                        target_idx = idx
                        break
            elif self.hub_tab == "workflows" and self.workflow:
                for idx in range(listing.option_count):
                    if listing.get_option_at_index(idx).id == self.workflow.id:
                        target_idx = idx
                        break
            listing.highlighted = target_idx
        self._render_hub_preview()
        self._render_hub_footer()

    def _render_hub_preview(self) -> None:
        if not self._ui_ready:
            return
        preview = self.root.query_one("#hub-preview", Pane)
        key = self._hub_selected_key()
        if key is None:
            preview.border_title = "预览"
            empty_msg = {
                "workflows": "未找到工作流。按 n 打开可视化工作流构建器新建一个。",
                "workers": "未找到 Worker。按 n 新建一个可复用 Worker。",
                "runs": "尚无运行记录。先加载一个工作流并执行 /run。",
            }[self.hub_tab]
            preview.show_content(Text(empty_msg, MUTED))
            return

        width = max(36, preview.content_size.width or 56)
        if self.hub_tab == "workflows":
            try:
                wf = self.store.get_workflow(key)
            except ValueError:
                return
            preview.border_title = f"工作流预览 · {wf.name}"
            doc = Text()
            doc.append(f"{wf.name}", "bold " + TEXT)
            doc.append(f"  ({wf.id})\n", MUTED)
            doc.append("总任务：", BLUE)
            doc.append((wf.task or "未填写总任务") + "\n\n", TEXT)
            doc.append("拓扑结构\n", BLUE)
            try:
                doc.append_text(graph(wf, {}, "", width, 0, interactive=False))
            except ValueError as exc:
                doc.append(f"拓扑冲突，无法显示与运行：{exc}", RED)
            doc.append("\n\n节点装配清单\n", BLUE)
            for node in wf.nodes:
                deps = ", ".join(node.dependencies) if node.dependencies else "起点"
                doc.append(f"  {node.id} · {node.label}", TEXT)
                doc.append(f"  @{node.worker_id}", BLUE)
                doc.append(f"  ← {deps}\n", MUTED)
            preview.show_content(doc)
        elif self.hub_tab == "workers":
            try:
                wk = self.store.get_worker(key)
            except ValueError:
                return
            preview.border_title = f"Worker 详情 · @{wk.id}"
            ready = bool(wk.base_url.strip() and wk.api_key.strip() and wk.model.strip())
            doc = Text()
            doc.append(f"@{wk.id} · {wk.name}  ", "bold " + TEXT)
            doc.append("● 已就绪\n\n" if ready else "○ 连接待配置\n\n", GREEN if ready else ORANGE)
            doc.append("上游协议：", MUTED)
            doc.append(f"{wk.protocol}\n", BLUE)
            doc.append("Base URL：", MUTED)
            doc.append(f"{wk.base_url or '未填写'}\n", TEXT)
            doc.append("模型 ID：", MUTED)
            doc.append(f"{wk.model or '未填写'}\n", BLUE)
            doc.append("附加参数：", MUTED)
            doc.append(f"{wk.extra_body or '{}'}\n", ORANGE if wk.extra_body else MUTED)
            builtin_labels = [
                BUILTIN_TOOL_CATALOG[t]["label"]
                for t in wk.builtin_tools
                if t in BUILTIN_TOOL_CATALOG
            ]
            doc.append("内置工具：", MUTED)
            doc.append(f"{', '.join(builtin_labels) or '无'}\n", GREEN if builtin_labels else MUTED)
            doc.append("MCP 服务：", MUTED)
            doc.append(f"{', '.join(wk.mcp_servers) or '无'}\n", TEXT)
            doc.append("本地技能：", MUTED)
            doc.append(f"{' '.join('@' + s for s in wk.skills) or '无'}\n\n", TEXT)
            doc.append("固定上下文\n", BLUE)
            doc.append(wk.prompt or "（未填写固定提示词）", TEXT if wk.prompt else MUTED)
            preview.show_content(doc)
        else:
            try:
                run = self.store.get_run(key)
            except ValueError:
                return
            preview.border_title = f"运行快照 · {run.id}"
            doc = Text()
            doc.append(f"{run.workflow.name}  ", "bold " + TEXT)
            doc.append_text(status_text(run.status))
            doc.append(
                f"\n时间：{run.created_at[:19].replace('T', ' ')}  ·  "
                f"Token：{run.usage.total_tokens}  ·  耗时：{elapsed(run)}  ·  "
                f"{money(run.usage.cost, self.settings.currency)}\n",
                MUTED,
            )
            doc.append(f"任务：{run.task}\n\n", TEXT)
            for node in run.workflow.nodes:
                st = run.nodes[node.id]
                doc.append(f"{node.id} {node.label}  ", TEXT)
                doc.append_text(status_text(st.status))
                doc.append(f"  {st.usage.total_tokens} tok\n", MUTED)
                if st.error:
                    doc.append(f"  错误：{fit(st.error, 70)}\n", RED)
                elif st.output:
                    first = st.output.strip().splitlines()[0]
                    doc.append(f"  {fit(first, 70)}\n", MUTED)
            preview.show_content(doc)

    @on(Button.Pressed, "#hub-tab-workflows")
    def click_hub_workflows(self, event: Button.Pressed) -> None:
        event.stop()
        self._set_hub_tab("workflows")

    @on(Button.Pressed, "#hub-tab-workers")
    def click_hub_workers(self, event: Button.Pressed) -> None:
        event.stop()
        self._set_hub_tab("workers")

    @on(Button.Pressed, "#hub-tab-runs")
    def click_hub_runs(self, event: Button.Pressed) -> None:
        event.stop()
        self._set_hub_tab("runs")

    @on(Input.Changed, "#hub-filter")
    def hub_filter_changed(self, event: Input.Changed) -> None:
        self.hub_query = event.value
        self.render_hub()

    @on(Input.Submitted, "#hub-filter")
    def hub_filter_submit(self) -> None:
        self._activate_hub_item()

    @on(OptionList.OptionHighlighted, "#hub-list")
    def hub_list_highlighted(self) -> None:
        self._render_hub_preview()

    @on(OptionList.OptionSelected, "#hub-list")
    def hub_list_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._render_hub_preview()

    @on(events.Click, "#hub-list")
    def double_click_hub_item(self, event: events.Click) -> None:
        if event.button == 1 and event.chain == 2:
            index = event.style.meta.get("option")
            if index is not None:
                listing = self.root.query_one("#hub-list", OptionList)
                self._activate_hub_item(listing.get_option_at_index(index).id)

    def _activate_hub_item(self, key: str | None = None) -> None:
        selected = key or self._hub_selected_key()
        if not selected:
            return
        try:
            if self.hub_tab == "workflows":
                self.load_workflow(selected)
                self.action_open_workbench()
            elif self.hub_tab == "workers":
                self.open_chat(selected)
            else:
                self.load_historical_run(selected)
        except ValueError as exc:
            self.set_message(str(exc), error=True)

    def load_historical_run(self, run_id: str) -> None:
        self.require_editable()
        run = self.store.get_run(run_id)
        self.workflow = deepcopy(run.workflow)
        self.view_run = run
        self.selected_node = run.workflow.nodes[0].id if run.workflow.nodes else ""
        self.view_name = "run"
        self.action_open_workbench()
        self.set_message(f"已加载历史运行 · {run.id} ({run.status})")

    def _hub_delete_selected(self) -> None:
        key = self._hub_selected_key()
        if not key:
            return
        try:
            self.require_editable()
        except ValueError as exc:
            self.set_message(str(exc), error=True)
            return

        if self.hub_tab == "workflows":
            wf = self.store.get_workflow(key)

            def on_confirm_wf(ok: bool | None) -> None:
                if not ok:
                    return
                try:
                    self.store.delete_workflow(wf.id)
                    if self.workflow and self.workflow.id == wf.id:
                        remaining = self.store.list_workflows()
                        self.workflow = remaining[0] if remaining else None
                        self.selected_node = (
                            self.workflow.nodes[0].id
                            if self.workflow and self.workflow.nodes
                            else ""
                        )
                    self.set_message(f"已删除工作流 · {wf.name}")
                    self.render_hub()
                except (ValueError, RuntimeError) as exc:
                    self.set_message(str(exc), error=True)

            self.push_screen(
                ConfirmScreen(
                    "删除工作流", f"确定删除工作流「{wf.name}」({wf.id})？历史运行记录会保留。"
                ),
                on_confirm_wf,
            )
        elif self.hub_tab == "workers":
            wk = self.store.get_worker(key)

            def on_confirm_wk(ok: bool | None) -> None:
                if not ok:
                    return
                try:
                    self.store.delete_worker(wk.id)
                    self.worker_models.pop(wk.id, None)
                    self.set_message(f"已删除 Worker · @{wk.id}")
                    self.render_hub()
                except (ValueError, RuntimeError) as exc:
                    self.set_message(str(exc), error=True)

            self.push_screen(
                ConfirmScreen("删除 Worker", f"确定删除 Worker「{wk.name}」(@{wk.id})？"),
                on_confirm_wk,
            )

    def on_key(self, event: events.Key) -> None:
        if len(self.screen_stack) > 1:
            return
        focused = self.focused
        in_text_input = isinstance(focused, Input)

        if self.page in {"workbench", "chat"}:
            if event.key == "left" and not in_text_input:
                event.stop()
                event.prevent_default()
                self.action_back_to_hub()
            return

        # Hub 键盘快捷键：像 Codex / OpenCode 一样全键盘无阻。
        filter_input = self.root.query_one("#hub-filter", Input)
        listing = self.root.query_one("#hub-list", OptionList)
        if filter_input.has_focus:
            if event.key in {"down", "up"}:
                listing.focus()
                event.stop()
                event.prevent_default()
            elif event.key == "escape":
                filter_input.value = ""
                listing.focus()
                event.stop()
                event.prevent_default()
            return

        key = event.key
        if key == "right":
            event.stop()
            event.prevent_default()
            self._activate_hub_item()
        elif key == "1":
            event.stop()
            self._set_hub_tab("workflows")
        elif key == "2":
            event.stop()
            self._set_hub_tab("workers")
        elif key == "3":
            event.stop()
            self._set_hub_tab("runs")
        elif key == "tab":
            event.stop()
            event.prevent_default()
            order = ["workflows", "workers", "runs"]
            self._set_hub_tab(order[(order.index(self.hub_tab) + 1) % len(order)])
        elif key == "slash":
            event.stop()
            event.prevent_default()
            filter_input.focus()
        elif key == "n":
            event.stop()
            try:
                if self.hub_tab == "workers":
                    self.open_worker("new")
                else:
                    self.open_flow_editor(new=True)
            except ValueError as exc:
                self.set_message(str(exc), error=True)
        elif key == "e":
            event.stop()
            selected = self._hub_selected_key()
            if not selected:
                return
            try:
                if self.hub_tab == "workflows":
                    self.load_workflow(selected)
                    self.open_flow_editor()
                elif self.hub_tab == "workers":
                    self.open_worker(selected)
            except ValueError as exc:
                self.set_message(str(exc), error=True)
        elif key == "r" and self.hub_tab == "workflows":
            event.stop()
            selected = self._hub_selected_key()
            if selected:
                try:
                    self.load_workflow(selected)
                    self.action_open_workbench()
                    asyncio.create_task(self.action_run())
                except ValueError as exc:
                    self.set_message(str(exc), error=True)
        elif key == "c":
            event.stop()
            selected = self._hub_selected_key()
            if not selected:
                return
            if self.hub_tab == "workers":
                self.open_chat(selected)
            elif self.hub_tab == "workflows":
                try:
                    self.require_editable()
                    cloned = deepcopy(self.store.get_workflow(selected))
                    cloned.id = new_id()
                    cloned.name = f"{cloned.name} (副本)"
                    self.save_workflow(cloned, switch_to_workbench=False)
                    self.render_hub()
                except ValueError as exc:
                    self.set_message(str(exc), error=True)
        elif key == "m" and self.hub_tab == "workers":
            event.stop()
            selected = self._hub_selected_key()
            if selected:
                self.open_models(selected)
        elif key in {"d", "delete"}:
            event.stop()
            self._hub_delete_selected()

    # ==================== 工作流与 Worker 编排管理 ====================
    def save_workflow(self, workflow: Workflow, *, switch_to_workbench: bool = True) -> None:
        self.require_editable()
        workers = {w.id: w for w in self.store.list_workers()}
        validate_workflow(workflow, workers)
        workflow.source = to_mermaid(workflow)
        self.store.save_workflow(workflow)
        self.workflow, self.view_run, self.view_name = workflow, None, "flow"
        if self.selected_node not in {n.id for n in workflow.nodes}:
            self.selected_node = workflow.nodes[0].id
        self.set_message(f"已保存 · {workflow.name}")
        if switch_to_workbench:
            self.page = "workbench"
            self._apply_page_visibility()
            self.render_workbench()
        else:
            self.render_current()

    def load_workflow(self, key: str | None) -> None:
        if key is None:
            return
        self.require_editable()
        self.workflow = self.store.get_workflow(key)
        self.view_run = None
        self.selected_node, self.view_name = self.workflow.nodes[0].id, "flow"
        self.set_message(f"已加载 · {self.workflow.name}")
        if self.page != "hub":
            self.page = "workbench"
            self._apply_page_visibility()
            self.render_workbench()

    def open_worker(self, key: str) -> None:
        self.require_editable()
        worker = (
            Worker(new_id(), "新 Worker", self.settings.model)
            if key == "new"
            else self.store.get_worker(key.lstrip("@"))
        )
        self.push_screen(
            WorkerScreen(worker, self.worker_models.get(worker.id, []), self.settings.workspace),
            self.worker_saved,
        )

    def worker_saved(self, worker: Worker | None) -> None:
        if worker is not None:
            self.store.save_worker(worker)
            self.worker_models.pop(worker.id, None)
            self.view_run = None
            if self.page == "workbench":
                self.view_name = "flow"
            if self.chat_session and self.chat_session.worker_id == worker.id:
                self.chat_session.worker_name = worker.name
                self.chat_session.model = worker.model
                self._chat_key = ()
            self.set_message(f"Worker 已保存 · @{worker.id} {worker.name}")
            self.render_current()

    def action_select_node(self, node_id: str) -> None:
        try:
            workflow = self.current_workflow()
            node = next((n for n in workflow.nodes if n.id == node_id), None)
            if node is None:
                raise ValueError(f"找不到节点：{node_id}")
            self.selected_node = node_id
            self.render_workbench()
            self.require_editable()
            self.push_screen(NodeScreen(node, workflow, self.store.list_workers()), self.node_saved)
        except ValueError as exc:
            self.set_message(str(exc), error=True)

    def node_saved(self, node: Node | None) -> None:
        if node is not None:
            workflow = deepcopy(self.current_workflow())
            workflow.nodes = [node if n.id == node.id else n for n in workflow.nodes]
            self.save_workflow(workflow)

    def flow_saved(self, workflow: Workflow | None) -> None:
        if workflow is not None:
            self.save_workflow(workflow)

    def open_flow_editor(self, *, new: bool = False, name: str = "") -> None:
        self.require_editable()
        workers = self.store.list_workers()
        if not workers:
            raise ValueError("请先用 /worker new 新增 Worker")
        workflow = (
            Workflow(new_id(), name or "新工作流", "", [Node("A", "核心任务", workers[0].id)])
            if new
            else deepcopy(self.current_workflow())
        )
        self.push_screen(FlowEditor(workflow, workers, is_new=new), self.flow_saved)

    def _open_flow_picker(self) -> None:
        self.push_screen(
            PickerScreen(
                "工作流 · 选择并加载",
                [
                    (w.id, Text(f"{w.name}\n  {workflow_summary(w)} · {w.id}", BLUE))
                    for w in self.store.list_workflows()
                ],
                "/flow new 可视化新建 · /flow edit 编辑 · /flow save 名称 另存",
            ),
            self.load_workflow,
        )

    # ==================== 单 Worker 独立多轮对话 ====================
    def open_chat(self, worker_id: str) -> None:
        self.require_editable()
        worker = self.store.get_worker(worker_id.lstrip("@"))
        self.chat_worker_id = worker.id
        if self.chat_session is None or self.chat_session.worker_id != worker.id:
            restored = load_latest_chat(self.settings.data_dir, worker.id)
            self.chat_session = restored or ChatSession(
                new_id(), worker.id, worker.name, worker.model
            )
        self.page = "chat"
        self._chat_key = ()
        self._apply_page_visibility()
        prompt = self.root.query_one("#prompt", Input)
        prompt.placeholder = (
            f"向 @{worker.id} ({worker.name}) 发消息，或输入 /（空框按 ← 返回选择页）"
        )
        self.set_message(
            f"单 Worker 对话 · @{worker.id} · /copy 复制 · /clear(new) 新对话"
            " · /worker 配置 · ← 返回"
        )
        self.render_chat()
        prompt.focus()

    async def send_chat_message(self, text: str) -> None:
        if self.chat_busy:
            self.set_message("正在回复中，请先 /run stop 停止", error=True)
            return
        if not self.chat_worker_id:
            self.set_message("请先选择 Worker", error=True)
            return
        worker = self.store.get_worker(self.chat_worker_id)
        if self.chat_session is None or self.chat_session.worker_id != worker.id:
            self.chat_session = ChatSession(new_id(), worker.id, worker.name, worker.model)
        self.chat_busy = True
        self.set_message(f"@{worker.id} 思考中… · /run stop 停止")
        self._chat_task = asyncio.create_task(self._execute_chat_turn(worker, text))
        self.render_chat()

    async def _execute_chat_turn(self, worker: Worker, text: str) -> None:
        assert self.chat_session is not None
        try:
            turn = await run_chat_turn(
                self.chat_session,
                worker,
                self.settings,
                text,
                client=self.client,
                on_update=self._on_chat_update,
            )
            if not self._shutting_down:
                if turn.status == "success":
                    total_tok = self.chat_session.usage.total_tokens
                    self.set_message(
                        f"回复完成 · 本轮 {turn.usage.total_tokens} Token · 累计 {total_tok} Token"
                    )
                else:
                    self.set_message(turn.error or f"对话状态：{turn.status}", error=True)
        except asyncio.CancelledError:
            if not self._shutting_down:
                self.set_message("对话已停止")
        except Exception as exc:
            if not self._shutting_down:
                self.set_message(self.redact(str(exc)), error=True)
        finally:
            self.chat_busy = False
            if not self._shutting_down:
                self.render_chat()

    def _on_chat_update(self) -> None:
        if not self._shutting_down and self.page == "chat":
            self.render_chat()

    def render_chat(self) -> None:
        if not self._ui_ready or not self.chat_worker_id:
            return
        try:
            worker = self.store.get_worker(self.chat_worker_id)
        except ValueError:
            self.action_back_to_hub()
            return
        session = self.chat_session or ChatSession(new_id(), worker.id, worker.name, worker.model)
        usage = session.usage

        head = Text()
        head.append(*_link(" ‹ 列表 ", "back_to_hub()", ORANGE, bold=True))
        head.append(" │ ", LINE)
        head.append(f"@{worker.id} · {worker.name}", "bold " + TEXT)
        head.append(f"  [{fit(worker.model, 24)} · {worker.protocol}]", BLUE)
        if self.workflow:
            head.append("   ")
            head.append(*_link("[工作台]", "open_workbench()", GREEN))
        cost_text = money(usage.cost, self.settings.currency)
        head.append(
            f"   {len(session.turns)} 轮 · {usage.total_tokens} tok · {cost_text}",
            MUTED,
        )
        self.root.query_one("#masthead", Static).update(head)

        pane = self.root.query_one("#chat-pane", Pane)
        tools_summary = []
        if worker.builtin_tools:
            tools_summary.append(f"内置:{','.join(worker.builtin_tools)}")
        if worker.mcp_servers:
            tools_summary.append(f"MCP:{','.join(worker.mcp_servers)}")
        if worker.skills:
            tools_summary.append(f"技能:{','.join('@' + s for s in worker.skills)}")
        pane.border_title = f"单 Worker 对话 · {upstream_label(worker.base_url)}" + (
            f" · {' | '.join(tools_summary)}" if tools_summary else ""
        )

        chat_key = (
            session.id,
            worker.model,
            len(session.turns),
            tuple(
                (t.status, t.assistant, t.error, len(t.tool_steps), t.usage.total_tokens)
                for t in session.turns
            ),
            self.frame if self.chat_busy else 0,
        )
        if chat_key == self._chat_key:
            return
        self._chat_key = chat_key

        if not session.turns:
            intro = Text()
            intro.append(f"@{worker.id} · {worker.name}\n", "bold " + BLUE)
            host = upstream_label(worker.base_url)
            intro.append(
                f"模型：{worker.model}  ·  协议：{worker.protocol}  ·  上游：{host}\n",
                MUTED,
            )
            if tools_summary:
                intro.append(f"已装配能力：{' · '.join(tools_summary)}\n", GREEN)
            else:
                intro.append(
                    "未开启工具：输入 /worker 可勾选内置文件/搜索工具或 MCP 实现多步调用。\n",
                    ORANGE,
                )
            if worker.prompt:
                intro.append(f"\n固定上下文：{fit(worker.prompt, 240)}\n", MUTED)
            intro.append("\n直接输入问题并回车即可开始多轮对话。", TEXT)
            pane.show_content(intro)
            return

        # 提问与回复冷暖分色：用户采用暖橙/暖杏，Worker 采用科技蓝/冷白 Markdown，轮间柔和分隔。
        blocks: list[object] = []
        for idx, turn in enumerate(session.turns, start=1):
            if idx > 1:
                blocks.extend([Text(""), Rule(style="#262d3c"), Text("")])
            user_header = Text(f"▸ 你 · 第 {idx} 轮", "bold " + ORANGE)
            user_body = Text(turn.user, USER_TEXT)
            blocks.extend([user_header, Text(""), user_body, Text("")])

            badge_text, badge_color = {
                "running": (f"● 生成中{'.' * (self.frame % 4 + 1)}", BLUE),
                "success": (f"✓ {turn.usage.total_tokens} tok", GREEN),
                "failed": ("× 失败", RED),
                "stopped": ("! 已停止", ORANGE),
            }.get(turn.status, (turn.status, MUTED))
            assistant_header = Text()
            assistant_header.append(f"@{worker.id} · {worker.name}", "bold " + BLUE)
            assistant_header.append(f"  ({badge_text})", badge_color)
            blocks.extend([assistant_header, Text("")])

            md_parts: list[str] = []
            tool_md = format_tool_steps_markdown(turn.tool_steps)
            if tool_md:
                md_parts.append(tool_md)
            if turn.assistant:
                md_parts.append(turn.assistant)
            if md_parts:
                blocks.append(Markdown("\n\n".join(md_parts)))
            elif turn.status == "running":
                blocks.append(Text("等待模型输出…", MUTED))
            if turn.error:
                if md_parts:
                    blocks.append(Text(""))
                blocks.append(Text(f"错误：{turn.error}", RED))

        pane.show_content(Group(*blocks), follow=self.chat_busy)

    # ==================== 工作流执行引擎交互 ====================
    async def action_run(self) -> None:
        if self.busy:
            self.set_message("已有运行 · /run stop 停止", error=True)
            return
        # 在后台 Task 调度前同步占用，连续 Enter 只能创建一次请求。
        self.busy, self._stop_requested = True, False
        try:
            self.view_run = self.engine.create_run(self.current_workflow())
        except (ValueError, OSError, RuntimeError) as exc:
            self.busy = False
            self.set_message(str(exc), error=True)
            return
        self.page = "workbench"
        self._apply_page_visibility()
        self.view_name = "run"
        self._run_task = asyncio.create_task(self.execute_run(self.view_run))
        self.set_message("运行中 · /run stop 停止")
        self.render_workbench()

    async def execute_run(self, run: Run) -> None:
        try:
            if self._stop_requested:
                run.status, run.finished_at = "stopped", now()
                for state in run.nodes.values():
                    state.status, state.finished_at = "stopped", run.finished_at
                self.store.save_run(run)
            else:
                await self.engine.execute(run)
            if not self._shutting_down:
                self.set_message(f"{run.status} · {run.usage.total_tokens} Token · {elapsed(run)}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._shutting_down:
                self.set_message(str(exc), error=True)
        finally:
            self.busy = False
            if not self._shutting_down:
                self.render_current()

    def action_stop(self) -> None:
        if self.busy:
            self._stop_requested = True
            self.engine.stop()
            self.set_message("正在停止工作流…")
        elif self.chat_busy and self._chat_task and not self._chat_task.done():
            self._chat_task.cancel()
            self.set_message("正在停止单聊回复…")
        else:
            self.set_message("当前没有运行")

    def on_engine_event(self, event: dict) -> None:
        if not self._shutting_down and self.page == "workbench":
            self.render_workbench()

    # ==================== 模型刷新（净化：绝不混入旧上游模型） ====================
    async def fetch_models(self, worker: Worker) -> list[str]:
        worker.require_api()
        if self.client is not None:
            remote = await self.client.list_models()
        else:
            async with ModelClient(self.settings, worker) as client:
                remote = await client.list_models()
        return list(dict.fromkeys(remote))

    def open_models(self, key: str | None) -> None:
        if key is None:
            return
        worker = self.store.get_worker(key.lstrip("@"))
        if self._model_task is None or self._model_task.done():
            self.set_message(f"正在刷新 Worker @{worker.id} 的模型…")
            self._model_task = asyncio.create_task(self.refresh_models(worker))

    async def refresh_models(self, worker: Worker) -> None:
        try:
            models = await self.fetch_models(worker)
            current = self.store.get_worker(worker.id)
            if (current.protocol, current.base_url, current.api_key, current.extra_body) != (
                worker.protocol,
                worker.base_url,
                worker.api_key,
                worker.extra_body,
            ):
                self.set_message(f"Worker @{worker.id} 的连接已修改，请重新刷新模型")
                return
            if not self._shutting_down:
                self.worker_models[worker.id] = models
                self.set_message(f"Worker @{worker.id} 模型列表已刷新 · {len(models)} 个模型")
                self.push_screen(
                    ModelPicker(worker, models),
                    lambda model: self.model_selected(worker.id, model),
                )
        except Exception as exc:
            if not self._shutting_down:
                self.set_message(worker.redact(str(exc)), error=True)

    def model_selected(self, worker_id: str, model: str | None) -> None:
        if model:
            self.set_message(f"模型 ID：{model} · /worker {worker_id} 中选择或填写")

    async def on_unmount(self) -> None:
        self._shutting_down = True
        self._freeze_splash()
        self.engine.stop()
        tasks = [t for t in (self._run_task, self._model_task, self._chat_task) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Task 尚未进入 execute 时取消，也必须终结已持久化的 pending 快照。
        if self.view_run and self.view_run.status in {"pending", "running"}:
            self.view_run.status, self.view_run.finished_at = "stopped", now()
            for state in self.view_run.nodes.values():
                if state.status in {"waiting", "ready", "running"}:
                    state.status, state.finished_at = "stopped", self.view_run.finished_at
            self.store.save_run(self.view_run)
        self.busy = False
        self.chat_busy = False

    # ==================== 斜杠命令与输入处理 ====================
    async def execute_command(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            if not text.startswith("/"):
                if self.page == "chat":
                    await self.send_chat_message(text)
                    return
                self.require_editable()
                workflow = deepcopy(self.current_workflow())
                workflow.task = text
                self.save_workflow(workflow)
                return

            command, _, arguments = text.partition(" ")
            arguments = arguments.strip()
            if command == "/hub":
                self.action_back_to_hub()
            elif command == "/chat":
                if arguments:
                    self.open_chat(arguments)
                elif self.page == "chat" and self.chat_worker_id:
                    self.open_chat(self.chat_worker_id)
                else:
                    workers = self.store.list_workers()
                    if not workers:
                        raise ValueError("请先 /worker new 新增 Worker")
                    selected_worker = None
                    if self.workflow and self.selected_node:
                        node = next(
                            (n for n in self.workflow.nodes if n.id == self.selected_node), None
                        )
                        selected_worker = node.worker_id if node else None
                    if selected_worker:
                        self.open_chat(selected_worker)
                    else:
                        self.push_screen(
                            PickerScreen(
                                "单 Worker 对话 · 选择角色",
                                [
                                    (
                                        w.id,
                                        Text(
                                            f"@{w.id}  {w.name}\n  {w.model} · {w.protocol}", BLUE
                                        ),
                                    )
                                    for w in workers
                                ],
                                "选择一个 Worker 开启多轮独立对话",
                            ),
                            lambda key: self.open_chat(key) if key else None,
                        )
            elif command in {"/clear", "/new"}:
                if (command == "/clear" and arguments not in {"", "new"}) or (
                    command == "/new" and arguments
                ):
                    raise ValueError("用法：/clear 或 /new")
                if self.page == "chat":
                    self.action_clear_chat()
                else:
                    raise ValueError("/clear (new) 仅用于单 Worker 对话模式")
            elif command == "/copy":
                self.copy_latest_output(arguments)
            elif command == "/run":
                if arguments == "stop":
                    self.action_stop()
                elif not arguments:
                    await self.action_run()
                else:
                    raise ValueError("用法：/run 或 /run stop")
            elif command == "/stop":
                self.action_stop()
            elif command == "/models":
                if arguments:
                    self.open_models(arguments)
                elif self.page == "chat" and self.chat_worker_id:
                    self.open_models(self.chat_worker_id)
                else:
                    selected = (
                        next((n for n in self.workflow.nodes if n.id == self.selected_node), None)
                        if self.workflow
                        else None
                    )
                    if selected:
                        self.open_models(selected.worker_id)
                    else:
                        self.push_screen(
                            PickerScreen(
                                "模型 · 选择 Worker 连接",
                                [
                                    (w.id, Text(f"@{w.id}  {w.name}", BLUE))
                                    for w in self.store.list_workers()
                                ],
                                "/models <worker_id> 刷新该 Worker 的模型",
                            ),
                            self.open_models,
                        )
            elif command == "/worker":
                self.require_editable()
                if arguments:
                    self.open_worker(arguments)
                elif self.page == "chat" and self.chat_worker_id:
                    self.open_worker(self.chat_worker_id)
                else:
                    self.push_screen(
                        PickerScreen(
                            "Worker 配置",
                            [
                                (
                                    w.id,
                                    Text(
                                        f"@{w.id}  {w.name}\n"
                                        f"  {w.model} · {upstream_label(w.base_url)}",
                                        BLUE,
                                    ),
                                )
                                for w in self.store.list_workers()
                            ],
                            "/worker new 新增 · /worker <id> 编辑",
                        ),
                        lambda key: self.open_worker(key) if key else None,
                    )
            elif command == "/node":
                if arguments == "new":
                    self.action_add_node_quick()
                elif not arguments:
                    raise ValueError("用法：/node <id> 或 /node new")
                else:
                    self.action_select_node(arguments)
            elif command == "/flow":
                self.require_editable()
                action, _, value = arguments.partition(" ")
                if action == "new":
                    self.open_flow_editor(new=True, name=value.strip())
                elif action == "edit":
                    self.open_flow_editor()
                elif action == "save":
                    workflow = deepcopy(self.current_workflow())
                    if value.strip():
                        workflow.id, workflow.name = new_id(), value.strip()
                    self.save_workflow(workflow)
                elif action:
                    self.load_workflow(arguments)
                else:
                    self._open_flow_picker()
            elif command == "/view" and arguments in {"flow", "run", "result"}:
                self.action_switch_view(arguments)
            elif command == "/export":
                if self.page == "chat" and self.chat_session and self.chat_session.turns:
                    self.last_export = export_chat_markdown(
                        self.store.data_dir,
                        self.chat_session,
                        self.settings.currency,
                        self.redact,
                    )
                    self.set_message(f"已导出对话 · {self.last_export}")
                else:
                    self.export_result()
            else:
                raise ValueError("未知命令 · 输入 / 查看命令")
        except (ValueError, OSError, RuntimeError) as exc:
            self.set_message(str(exc), error=True)

    def copy_latest_output(self, target_node: str = "") -> None:
        """通用快捷复制：单聊模式复制最近一轮回答，工作台模式复制工作流最终结果或指定节点。"""
        if self.page == "chat":
            if target_node:
                raise ValueError("单聊模式下直接输入 /copy 即可复制最后回答")
            if not self.chat_session or not self.chat_session.turns:
                raise ValueError("当前对话还没有可复制的回答")
            for idx in range(len(self.chat_session.turns), 0, -1):
                answer = self.chat_session.turns[idx - 1].assistant.strip()
                if answer:
                    clean = self.redact(answer)
                    self._write_clipboard(clean)
                    self.set_message(
                        f"已复制 @{self.chat_session.worker_id} 第 {idx} 轮回答"
                        f"（{len(clean)} 字符）"
                    )
                    return
            raise ValueError("当前对话还没有可复制的回答")

        run = self.view_run
        if run is None:
            raise ValueError("当前工作流没有运行结果，请先 /run")
        if target_node:
            if target_node not in run.nodes:
                raise ValueError(f"找不到节点：{target_node}")
            node_out = run.nodes[target_node].output.strip()
            if not node_out:
                raise ValueError(f"节点 {target_node} 尚无输出可复制")
            clean = self.redact(node_out)
            self._write_clipboard(clean)
            self.set_message(f"已复制节点 {target_node} 结果（{len(clean)} 字符）")
            return

        parents = {p for n in run.workflow.nodes for p in n.dependencies}
        terminals = [n for n in run.workflow.nodes if n.id not in parents]
        ready = [
            (n, run.nodes[n.id].output.strip())
            for n in terminals
            if n.id in run.nodes and run.nodes[n.id].output.strip()
        ]
        if not ready:
            raise ValueError("当前工作流尚无最终结果可复制")
        raw = (
            ready[0][1]
            if len(ready) == 1
            else "\n\n".join(f"## {n.label}\n\n{out}" for n, out in ready)
        )
        clean = self.redact(raw)
        self._write_clipboard(clean)
        target_label = ready[0][0].label if len(ready) == 1 else f"{len(ready)} 个终点节点"
        self.set_message(f"已复制最终结果 · {target_label}（{len(clean)} 字符）")

    def export_result(self) -> None:
        run = self.view_run
        if run is None:
            raise ValueError("当前工作流没有运行结果，请先 /run")
        lines = [f"# {run.workflow.name}", f"Run: {run.id} · {run.status}", run.task]
        for node in run.workflow.nodes:
            state = run.nodes[node.id]
            lines.extend([f"## {node.id} · {node.label} · {state.status}", state.output])
            if state.error:
                lines.append(f"错误：{state.error}")
        lines.append(
            f"Token: {run.usage.total_tokens} · {elapsed(run)} · "
            f"{money(run.usage.cost, self.settings.currency)}"
        )
        folder = self.store.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        self.last_export = folder / f"run-{run.id}-{new_id()}.md"
        self.last_export.write_text(self.redact("\n\n".join(lines)) + "\n", encoding="utf-8")
        self.set_message(f"已导出 · {self.last_export}")

    @on(Input.Changed, "#prompt")
    def prompt_changed(self, event: Input.Changed) -> None:
        self._suggestion_moved = False
        text = event.value.strip()
        choices = list(COMMANDS)
        if text.startswith("/worker "):
            choices.extend((f"/worker {w.id}", w.name) for w in self.store.list_workers())
        if text.startswith("/chat "):
            choices.extend((f"/chat {w.id}", w.name) for w in self.store.list_workers())
        if text.startswith("/flow "):
            choices.extend((f"/flow {w.id}", w.name) for w in self.store.list_workflows())
        if text.startswith("/node ") and self.workflow:
            choices.extend((f"/node {n.id}", n.label) for n in self.workflow.nodes)
        matched: list[tuple[str, str, str]] = []
        if text.startswith("/"):
            for label, hint in choices:
                exec_cmd = _match_command_choice(label, text)
                if exec_cmd is not None:
                    matched.append((exec_cmd, label, hint))
        self.matches = matched
        menu = self.root.query_one("#command-menu", OptionList)
        menu.clear_options()
        menu.add_options(
            Option(Text.assemble((label, BLUE), (f"  {hint}", MUTED)))
            for _, label, hint in self.matches
        )
        menu.highlighted = 0 if self.matches else None
        menu.display = bool(self.matches)

    def move_suggestion(self, step: int) -> None:
        menu = self.root.query_one("#command-menu", OptionList)
        if self.matches:
            self._suggestion_moved = True
            menu.highlighted = ((menu.highlighted or 0) + step) % len(self.matches)
            menu.scroll_to_highlight()

    def accept_suggestion(self) -> None:
        menu = self.root.query_one("#command-menu", OptionList)
        if menu.highlighted is not None and self.matches:
            prompt = self.root.query_one("#prompt", Input)
            prompt.value = self.matches[menu.highlighted][0]
            prompt.cursor_position = len(prompt.value)

    @on(OptionList.OptionSelected, "#command-menu")
    async def click_command(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        text = self.matches[event.option_index][0]
        self.root.query_one("#prompt", Input).value = ""
        await self.execute_command(text)

    @on(Input.Submitted, "#prompt")
    async def submit(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value
        if (text.strip() == "/" or self._suggestion_moved) and self.matches:
            menu = self.root.query_one("#command-menu", OptionList)
            text = self.matches[menu.highlighted or 0][0]
        self._suggestion_moved = False
        event.input.value = ""
        await self.execute_command(text)

    @on(OptionList.OptionSelected, "#node-cards")
    def select_card(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.selected_node = event.option.id or ""
        self.render_workbench()

    @on(events.Click, "#node-cards")
    def double_click_card(self, event: events.Click) -> None:
        if event.button == 1 and event.chain == 2:
            index = event.style.meta.get("option")
            if index is not None:
                cards = self.root.query_one("#node-cards", OptionList)
                self.action_select_node(cards.get_option_at_index(index).id)

    @on(events.Click, "#composer")
    @on(events.Click, "#input-meta")
    def focus_composer(self, event: events.Click) -> None:
        if event.button == 1:
            self.root.query_one("#prompt", Input).focus()

    def action_commands(self) -> None:
        if self.page == "hub":
            self.root.query_one("#hub-filter", Input).focus()
            return
        prompt = self.root.query_one("#prompt", Input)
        prompt.value = "/"
        prompt.focus()

    def action_escape(self) -> None:
        if self.page == "hub":
            if self.workflow:
                self.action_open_workbench()
            return
        self.root.query_one("#command-menu").display = False
        self.root.query_one("#prompt", Input).focus()

    # ==================== Workflow 工作台渲染 ====================
    def render_workbench(self) -> None:
        if not self._ui_ready:
            return
        run = self.view_run
        workflow = run.workflow if run and self.view_name != "flow" else self.workflow
        states = run.nodes if run else {}
        workers = run.workers if run else {w.id: w for w in self.store.list_workers()}

        head = Text()
        head.append(*_link(" ‹ 列表 ", "back_to_hub()", ORANGE, bold=True))
        head.append(" │ ", LINE)
        flow_title = f"{fit(workflow.name, 24) if workflow else '新工作台'} ▾"
        head.append(*_link(flow_title, "quick_flow_picker()", TEXT, bold=True))
        head.append("  ")
        for key, title in (("flow", "编排"), ("run", "执行"), ("result", "结果")):
            active = self.view_name == key
            head.append(
                *_link(
                    f"[{title}]",
                    f"switch_view('{key}')",
                    BLUE if active else MUTED,
                    bold=active,
                )
            )
            head.append(" ")
        if run:
            done_count = sum(1 for item in run.nodes.values() if item.status == "success")
            head.append(
                f" ·  {run.status} {done_count}/{len(run.nodes)} · "
                f"{run.usage.total_tokens} tok · {elapsed(run)}",
                BLUE,
            )
        self.root.query_one("#masthead", Static).update(head)

        map_pane = self.root.query_one("#map-pane", Pane)
        map_pane.border_title = "DAG 拓扑 · 点击节点装配 Worker"
        # 切换页面首帧尚未完成布局时，用终端宽度估算画布可用列数，避免退化为窄列截断。
        sidebar_reserved = 30 if self.size.width < 104 else 40
        canvas_width = (
            map_pane.content_size.width
            if map_pane.content_size.width > 0
            else max(20, self.size.width - sidebar_reserved)
        )
        if workflow and workflow.nodes:
            try:
                drawing = graph(
                    workflow,
                    states,
                    self.selected_node,
                    max(20, canvas_width),
                    self.frame,
                )
            except ValueError as exc:
                drawing = Text(f"拓扑冲突，无法显示与运行：{exc}", RED)
        else:
            drawing = Text("/worker new 配置 Worker → /flow new 可视化创建工作流", MUTED)
        # 多路并行时给画布足够行数，同时保留正文与底部输入区。
        map_pane.styles.height = (
            max(13, min(drawing.plain.count("\n") + 3, self.size.height - 13))
            if self.size.height >= 28
            else 9
        )
        map_pane.show_content(drawing)

        sidebar = self.root.query_one("#sidebar")
        sidebar.border_title = f"节点 ({len(workflow.nodes) if workflow else 0})"
        actions_line = Text()
        actions_line.append(*_link("+节点", "add_node_quick()", BLUE))
        actions_line.append(" · ", LINE)
        actions_line.append(*_link("编排", "edit_flow_quick()", TEXT))
        actions_line.append(" · ", LINE)
        if self.busy:
            actions_line.append(*_link("停止", "toggle_run()", RED, bold=True))
        else:
            actions_line.append(*_link("→运行", "toggle_run()", GREEN, bold=True))
        self.root.query_one("#sidebar-actions", Static).update(actions_line)

        key = (
            tuple(
                (
                    n.id,
                    n.label,
                    n.worker_id,
                    states[n.id].status if n.id in states else "waiting",
                    states[n.id].usage.total_tokens if n.id in states else 0,
                )
                for n in workflow.nodes
            )
            if workflow
            else ()
        )
        cards = self.root.query_one("#node-cards", OptionList)
        if key != self._cards_key:
            self._cards_key = key
            cards.clear_options()
            for node in workflow.nodes if workflow else []:
                st = states.get(node.id)
                label = Text(f"{node.id}  {fit(node.label, 20)}\n", TEXT)
                label.append(f"@{node.worker_id}  ", MUTED)
                label.append_text(status_text(st.status if st else "waiting"))
                if st and st.usage.total_tokens:
                    label.append(f" · {st.usage.total_tokens}t", MUTED)
                label.append("\n")
                cards.add_option(Option(label, id=node.id))
        if (
            workflow
            and self.selected_node in {n.id for n in workflow.nodes}
            and not cards.has_focus
        ):
            cards.highlighted = next(
                i for i, n in enumerate(workflow.nodes) if n.id == self.selected_node
            )

        selected = (
            next((n for n in workflow.nodes if n.id == self.selected_node), None)
            if workflow
            else None
        )
        worker = workers.get(selected.worker_id) if selected else None
        if worker:
            hint_doc = Text()
            hint_doc.append(f"@{worker.id} · {worker.name}\n", "bold " + TEXT)
            hint_doc.append(f"{fit(worker.model, 28)} · {worker.protocol}\n", BLUE)
            tool_cnt = len(worker.builtin_tools) + len(worker.mcp_servers)
            hint_doc.append(
                f"工具 {tool_cnt} · 技能 {len(worker.skills)}\n",
                GREEN if tool_cnt else MUTED,
            )
            hint_doc.append(*_link("[配置 Worker]", "edit_selected_worker()", BLUE))
            hint_doc.append("  ", MUTED)
            hint_doc.append(*_link("[单聊]", "open_chat_current()", GREEN))
            self.root.query_one("#assembly-hint", Static).update(hint_doc)
        else:
            self.root.query_one("#assembly-hint", Static).update(
                Text("/worker new 新增\n/flow new 可视化创建", MUTED)
            )

        reading = self.root.query_one("#reading-pane", Pane)
        reading.border_title = {
            "flow": "编排 · 总任务与节点说明",
            "run": "执行 · 流式输出与工具步骤",
            "result": "结果汇总",
        }[self.view_name]
        output_key = (
            self.view_name,
            self.selected_node,
            workflow.task if workflow else "",
            selected.task if selected else "",
            tuple(
                (k, s.output, s.error, s.status, s.usage.total_tokens, len(s.logs))
                for k, s in states.items()
            ),
        )
        if output_key != self._reading_key:
            self._reading_key = output_key
            if self.view_name == "flow":
                if workflow:
                    flow_doc = Text()
                    flow_doc.append("总任务\n", "bold " + BLUE)
                    flow_doc.append(
                        (workflow.task or "在下方直接输入目标并回车保存总任务，再用 /run 执行。")
                        + "\n",
                        TEXT,
                    )
                    if selected:
                        deps = ", ".join(selected.dependencies) if selected.dependencies else "起点"
                        flow_doc.append(
                            f"\n当前节点：{selected.id} · {selected.label}"
                            f"（@{selected.worker_id} · 上游：{deps}）\n",
                            ORANGE,
                        )
                        if selected.task:
                            flow_doc.append(selected.task, MUTED)
                    content: object = flow_doc
                else:
                    content = Text("先配置 Worker，再可视化创建工作流（按 ← 打开选择页）。", MUTED)
            elif run and self.view_name == "result":
                parents = {p for n in run.workflow.nodes for p in n.dependencies}
                content = Markdown(
                    "\n\n".join(
                        f"## {n.label} · {run.nodes[n.id].status}\n\n"
                        f"{run.nodes[n.id].output}\n\n{run.nodes[n.id].error}"
                        for n in run.workflow.nodes
                        if n.id not in parents
                    )
                )
            elif run and self.selected_node in run.nodes:
                state = run.nodes[self.selected_node]
                tool_steps = extract_tool_steps(state.logs)
                parts = [
                    f"**{state.status}** · {state.usage.total_tokens} Token · "
                    f"{elapsed(state)} · {money(state.usage.cost, self.settings.currency)}"
                ]
                tool_md = format_tool_steps_markdown(tool_steps)
                if tool_md:
                    parts.append(tool_md)
                parts.append(state.output or "等待模型输出…")
                if state.error:
                    parts.append(f"**错误**：{state.error}")
                content = Markdown("\n\n".join(parts))
            else:
                content = Text("当前工作流还没有运行结果 · 输入 /run 或点击右侧 →运行", MUTED)
            reading.show_content(content, follow=self.busy)
