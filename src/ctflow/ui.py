"""FLUX 终端工作台：命令编排、直接装配、真实运行。"""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from ctflow.engine import Engine
from ctflow.graph import parse_workflow, to_mermaid, validate_workflow
from ctflow.models import Node, NodeRun, Run, Worker, Workflow, new_id, now
from ctflow.provider import ModelClient
from ctflow.rendering import BLUE, MUTED, ORANGE, RED, TEXT, fit, graph, status_text
from ctflow.tools import discover_skills, read_skills

if TYPE_CHECKING:
    from ctflow.config import Settings
    from ctflow.storage import Store


def elapsed(result: NodeRun | Run) -> str:
    start = result.created_at if isinstance(result, Run) else result.started_at
    if start is None:
        return "—"
    end = datetime.fromisoformat(result.finished_at) if result.finished_at else datetime.now(UTC)
    return f"{max(0, (end - datetime.fromisoformat(start)).total_seconds()):.1f}s"


def money(cost: float | None, currency: str) -> str:
    return "费用待配置" if cost is None else f"{cost:.4f} {currency}"


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
        self.app.accept_suggestion()


class Dialog(ModalScreen):
    """固定标题、关闭和快捷键提示，字段区独立滚动。"""

    BINDINGS = [Binding("escape", "cancel", "取消", priority=True)]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.heading = title

    def body(self) -> ComposeResult:
        yield Static("")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            with Horizontal(classes="dialog-heading"):
                yield Static(self.heading, classes="dialog-title", markup=False)
                yield Button("×", id="dialog-close", tooltip="取消未保存修改 · Esc")
            with VerticalScroll(id="dialog-fields"):
                yield from self.body()
            yield Static("", id="form-error", markup=False)
            yield Static("Ctrl+S 保存 · Esc 取消", classes="dialog-footer")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#dialog-close")
    def close_dialog(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()


class FormScreen(Dialog):
    BINDINGS = [Binding("ctrl+s", "save", "保存", priority=True)]

    def __init__(
        self,
        title: str,
        fields: dict[str, tuple[str, str]],
        *,
        multiline: tuple[str, ...] = (),
        choices: dict[str, list[tuple[str, str]]] | None = None,
        required: tuple[str, ...] = (),
        passwords: tuple[str, ...] = (),
    ) -> None:
        super().__init__(title)
        self.fields, self.multiline = fields, multiline
        self.choices, self.required = choices or {}, required
        self.passwords = passwords

    def body(self) -> ComposeResult:
        for key, (label, value) in self.fields.items():
            yield Label(label)
            if key in self.multiline:
                yield TextArea(value, id=f"field-{key}", show_line_numbers=key == "source")
            elif key in self.choices:
                yield Select(self.choices[key], value=value, allow_blank=False, id=f"field-{key}")
            else:
                yield Input(value, id=f"field-{key}", password=key in self.passwords)

    def on_mount(self) -> None:
        self.query_one(f"#field-{next(iter(self.fields))}").focus()

    def values(self) -> dict[str, str]:
        result = {}
        for key in self.fields:
            widget = self.query_one(f"#field-{key}")
            if isinstance(widget, TextArea):
                result[key] = widget.text
            else:
                value = str(widget.value)
                result[key] = value if key in self.passwords else value.strip()
        return result

    def validate(self, values: dict[str, str]) -> object:
        for key in self.required:
            if not values[key].strip():
                raise ValueError(f"请填写{self.fields[key][0]}")
        return values

    def action_save(self) -> None:
        try:
            result = self.validate(self.values())
        except (ValueError, OSError, RuntimeError) as exc:
            self.query_one("#form-error", Static).update(self.redact(str(exc)))
            return
        self.dismiss(result)

    def redact(self, text: str) -> str:
        return self.app.redact(text)


class WorkerScreen(FormScreen):
    def __init__(self, worker: Worker, models: list[str], workspace: Path) -> None:
        self.worker = deepcopy(worker)
        self.models = list(dict.fromkeys([worker.model, *models]))
        self.workspace = workspace
        self.skills = discover_skills(workspace)
        super().__init__(
            f"Worker 配置 · @{worker.id}",
            {
                "name": ("Worker 名称", worker.name),
                "model": ("模型 ID", worker.model),
                "protocol": ("上游协议", worker.protocol),
                "base_url": ("Base URL · 自定义上游根地址", worker.base_url),
                "api_key": ("API Key · 平台加密后保存在本机", worker.api_key),
                "extra_body": (
                    "协议附加参数 extra_body · JSON 对象",
                    json.dumps(worker.extra_body, ensure_ascii=False, indent=2),
                ),
                "mcp_servers": (
                    "MCP 服务器 · 逗号分隔，名称在 config.json 的 mcp_servers 中登记",
                    ", ".join(worker.mcp_servers),
                ),
                "prompt": ("固定上下文", worker.prompt),
            },
            multiline=("prompt", "extra_body"),
            required=("name", "model"),
            passwords=("api_key",),
            choices={
                "protocol": [
                    ("Chat Completions", "chat"),
                    ("OpenAI Responses", "responses"),
                    ("Anthropic Messages", "anthropic"),
                ]
            },
        )

    def body(self) -> ComposeResult:
        yield Label(f"@{self.worker.id} 的模型 · 使用下方连接草稿刷新")
        yield Button("刷新模型", id="refresh-models")
        yield Select(
            [(m, m) for m in self.models],
            value=self.worker.model,
            allow_blank=False,
            id="model-choice",
        )
        yield from super().body()
        yield Label("@技能 · Space 多选，打开配置时重新发现")
        yield Static(str(self.workspace / "skill" / "<name>" / "SKILL.md"), markup=False)
        names = list(dict.fromkeys([*self.skills, *self.worker.skills]))
        yield SelectionList[str](
            *[
                (
                    f"@{s}" + (" · 文件缺失" if s not in self.skills else ""),
                    s,
                    s in self.worker.skills,
                )
                for s in names
            ],
            id="skill-options",
        )
        if not names:
            yield Static("复制技能文件后重新打开 /worker。", markup=False)

    @on(Select.Changed, "#model-choice")
    def choose_model(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self.query_one("#field-model", Input).value = str(event.value)

    @on(Button.Pressed, "#refresh-models")
    def refresh_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        event.button.disabled = True
        event.button.label = "刷新中…"
        self.run_worker(self.refresh_models(), group="models", exclusive=True)

    async def refresh_models(self) -> None:
        button = self.query_one("#refresh-models", Button)
        feedback = self.query_one("#form-error", Static)
        feedback.update("")
        draft = None
        try:
            draft = self.draft()
            models = await self.app.fetch_models(draft)
            current_draft = self.draft()
            if (current_draft.base_url, current_draft.api_key, current_draft.extra_body) != (
                draft.base_url,
                draft.api_key,
                draft.extra_body,
            ):
                feedback.update("连接已修改，请重新刷新模型")
                return
            current = self.query_one("#field-model", Input).value
            choice = self.query_one("#model-choice", Select)
            selected = current.strip() or str(choice.value)
            self.models = list(dict.fromkeys([selected, *models]))
            # 刷新候选项不触发表单回填，保留正在编辑的模型 ID 和其余草稿。
            with self.prevent(Select.Changed):
                choice.set_options([(model, model) for model in self.models])
                choice.value = selected
            feedback.update(f"已刷新 · @{draft.id} · {len(models)} 个模型")
        except Exception as exc:
            feedback.update(draft.redact(str(exc)) if draft else self.redact(str(exc)))
        finally:
            button.disabled = False
            button.label = "刷新模型"

    def draft(self, values: dict[str, str] | None = None) -> Worker:
        values = self.values() if values is None else values
        try:
            extra_body = json.loads(values["extra_body"])
        except json.JSONDecodeError:
            raise ValueError("附加参数 extra_body 必须是合法的 JSON 对象") from None
        if not isinstance(extra_body, dict):
            raise ValueError("附加参数 extra_body 必须是 JSON 对象")
        skills = list(self.query_one("#skill-options", SelectionList).selected)
        mcp_servers = [
            name.strip().lstrip("@")
            for name in re.split(r"[,，\n]", values["mcp_servers"])
            if name.strip()
        ]
        return Worker(
            self.worker.id,
            values["name"],
            values["model"],
            values["prompt"],
            skills,
            base_url=values["base_url"],
            api_key=values["api_key"],
            extra_body=extra_body,
            mcp_servers=mcp_servers,
            protocol=values["protocol"],
        )

    def redact(self, text: str) -> str:
        draft = deepcopy(self.worker)
        draft.api_key = self.query_one("#field-api_key", Input).value
        return self.app.redact(draft.redact(text))

    def validate(self, values: dict[str, str]) -> Worker:
        super().validate(values)
        worker = self.draft(values)
        unknown = set(worker.mcp_servers) - self.app.settings.mcp_servers.keys()
        if unknown:
            raise ValueError(f"未登记的 MCP 服务器：{', '.join(sorted(unknown))}")
        skills = worker.skills
        read_skills(self.workspace, skills)
        return worker


class NodeScreen(FormScreen):
    def __init__(self, node: Node, workflow: Workflow, workers: list[Worker]) -> None:
        self.node, self.workflow = node, deepcopy(workflow)
        self.worker_catalog = {w.id: w for w in workers}
        super().__init__(
            f"{node.id} · 装配 Worker",
            {
                "worker": ("引用 Worker · /worker <id> 编辑固定装配", node.worker_id),
                "label": ("节点名称", node.label),
                "task": ("节点补充任务", node.task),
                "deps": ("直接上游节点 ID · 逗号分隔", ", ".join(node.dependencies)),
            },
            multiline=("task",),
            choices={"worker": [(f"@{w.id} · {w.name}", w.id) for w in workers]},
            required=("label",),
        )

    def validate(self, values: dict[str, str]) -> Node:
        super().validate(values)
        node = Node(
            self.node.id,
            values["label"],
            values["worker"],
            values["task"],
            [p.strip() for p in re.split(r"[,，\n]", values["deps"]) if p.strip()],
        )
        self.workflow.nodes = [node if n.id == node.id else n for n in self.workflow.nodes]
        validate_workflow(self.workflow, self.worker_catalog)
        return node


class FlowEditor(FormScreen):
    def __init__(self, workflow: Workflow, workers: list[Worker]) -> None:
        self.workflow = workflow
        self.worker_catalog = {w.id: w for w in workers}
        source = to_mermaid(workflow) if workflow.nodes else "flowchart TD\n"
        super().__init__(
            "工作流 · 编辑名称与依赖图",
            {
                "name": ("工作流名称", workflow.name),
                "task": ("总任务", workflow.task),
                "source": ('Mermaid · A["节点名称 @worker_id"]，A --> B', source),
            },
            multiline=("task", "source"),
            required=("name", "source"),
        )

    def validate(self, values: dict[str, str]) -> Workflow:
        super().validate(values)
        result = parse_workflow(
            values["source"],
            self.worker_catalog,
            workflow_id=self.workflow.id,
            name=values["name"],
            task=values["task"],
        )
        # 图文本只表达结构；同 ID 节点的补充任务保持原值。
        tasks = {n.id: n.task for n in self.workflow.nodes}
        for node in result.nodes:
            node.task = tasks.get(node.id, "")
        return result


class PickerScreen(Dialog):
    def __init__(self, title: str, options: list[tuple[str, Text]], hint: str) -> None:
        super().__init__(title)
        self.options, self.hint = options, hint

    def body(self) -> ComposeResult:
        yield Input(placeholder="输入名称或 ID 筛选", id="picker-filter")
        yield OptionList(id="picker-options", markup=False)
        yield Static(self.hint, markup=False)

    def on_mount(self) -> None:
        self.query_one(".dialog-footer", Static).update("↑↓ 选择 · Enter 打开 · Esc 取消")
        self.filter_options("")
        self.query_one("#picker-options", OptionList).focus()

    def filter_options(self, query: str) -> None:
        listing = self.query_one("#picker-options", OptionList)
        listing.clear_options()
        listing.add_options(
            Option(label, id=key)
            for key, label in self.options
            if query.casefold() in f"{key} {label.plain}".casefold()
        )
        listing.highlighted = 0 if listing.option_count else None

    @on(Input.Changed, "#picker-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_options(event.value)

    @on(Input.Submitted, "#picker-filter")
    def filter_submit(self) -> None:
        listing = self.query_one("#picker-options", OptionList)
        if listing.highlighted is not None:
            self.dismiss(listing.get_option_at_index(listing.highlighted).id)

    @on(OptionList.OptionSelected, "#picker-options")
    def choose(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)


class ModelPicker(PickerScreen):
    def __init__(self, worker: Worker, models: list[str]) -> None:
        self.worker = deepcopy(worker)
        super().__init__(
            f"模型 · Worker @{worker.id} · {worker.name}",
            [(model, Text(model, BLUE)) for model in models],
            f"选择查看 ID · /worker {worker.id} 配置模型",
        )

    def body(self) -> ComposeResult:
        yield Button("刷新模型", id="refresh-models")
        yield from super().body()

    @on(Button.Pressed, "#refresh-models")
    def refresh_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        event.button.disabled = True
        event.button.label = "刷新中…"
        self.run_worker(self.refresh_models(), group="models", exclusive=True)

    async def refresh_models(self) -> None:
        button = self.query_one("#refresh-models", Button)
        feedback = self.query_one("#form-error", Static)
        feedback.update("")
        try:
            models = await self.app.fetch_models(self.worker)
            self.app.worker_models[self.worker.id] = models
            self.options = [(model, Text(model, BLUE)) for model in models]
            self.filter_options(self.query_one("#picker-filter", Input).value)
            feedback.update(f"已刷新 · @{self.worker.id} · {len(models)} 个模型")
        except Exception as exc:
            feedback.update(self.worker.redact(str(exc)))
        finally:
            button.disabled = False
            button.label = "刷新模型"


COMMANDS = [
    ("/worker", "选择 Worker 配置"),
    ("/worker new", "新增 Worker"),
    ("/models", "刷新所选节点 Worker 的模型 · /models <worker_id>"),
    ("/flow", "选择并加载工作流"),
    ("/flow new", "新建工作流"),
    ("/flow edit", "编辑工作流名称和图"),
    ("/flow save", "保存 · 加名称则另存一套"),
    ("/run", "运行当前工作流"),
    ("/node", "装配节点 · /node <id>"),
    ("/run stop", "停止当前运行"),
    ("/view flow", "编排视图"),
    ("/view run", "执行视图"),
    ("/view result", "结果视图"),
    ("/export", "导出真实运行结果"),
]


class CTFlowApp(App[None]):
    TITLE = "CTflow / FLUX"
    CSS_PATH = "ui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", show=False, priority=True),
        Binding("ctrl+p", "commands", show=False),
        Binding("escape", "escape", show=False),
    ]

    def __init__(
        self,
        store: Store,
        settings: Settings,
        workflow: Workflow | None = None,
        auto_run: bool = False,
        client: ModelClient | None = None,
    ) -> None:
        super().__init__()
        self.store, self.settings, self.client = store, settings, client
        workflows = store.list_workflows()
        self.workflow = deepcopy(workflow) if workflow else (workflows[0] if workflows else None)
        self.auto_run = auto_run
        self.worker_models: dict[str, list[str]] = {}
        self.view_run: Run | None = None
        self.view_name = "flow"
        self.selected_node = (
            self.workflow.nodes[0].id if self.workflow and self.workflow.nodes else ""
        )
        self.engine = Engine(store, settings, client=client, on_event=self.on_engine_event)
        self.busy = False
        self._run_task: asyncio.Task | None = None
        self._model_task: asyncio.Task | None = None
        self._stop_requested = False
        self._shutting_down = False
        self.frame = 0
        self.matches: list[tuple[str, str]] = []
        self.message = "输入目标保存总任务 · / 打开命令"
        self.last_export: Path | None = None
        self._cards_key: tuple = ()
        self._reading_key: tuple = ()

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
                    "markdown.h1": "bold " + TEXT,
                    "markdown.h2": "bold " + BLUE,
                    "markdown.h3": "bold " + BLUE,
                    "markdown.code": ORANGE,
                    "markdown.link": BLUE,
                    "markdown.item.bullet": BLUE,
                }
            )
        )
        self.root.query_one("#map-pane", Pane).query_one(Static).auto_links = False
        self.root.query_one("#command-menu").display = False
        self.root.query_one("#prompt", Input).cursor_blink = False
        self.root.query_one("#prompt", Input).focus()
        self.render_workbench()
        self.set_message(self.message)
        self.set_interval(0.16, self.tick)
        if self.auto_run:
            self.call_after_refresh(self.action_run)

    def on_resize(self, event: events.Resize) -> None:
        self.root.set_class(event.size.width < 110, "compact")
        self.root.set_class(event.size.height < 28, "short")
        if self.is_mounted:
            self.call_after_refresh(self.render_workbench)

    def tick(self) -> None:
        if self.busy:
            self.frame += 1
            self.render_workbench()

    def set_message(self, text: str, *, error: bool = False) -> None:
        self.message = self.redact(text)
        self.root.query_one("#input-meta", Static).update(
            Text(self.message, RED if error else MUTED)
        )

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
        if self.busy:
            raise ValueError("运行中，请用 /run stop 停止后再编辑或切换")

    def current_workflow(self) -> Workflow:
        if self.workflow is None:
            raise ValueError("请先 /worker new 配置 Worker，再 /flow new 创建工作流")
        return self.workflow

    def save_workflow(self, workflow: Workflow) -> None:
        self.require_editable()
        workflow.source = to_mermaid(workflow)
        self.store.save_workflow(workflow)
        self.workflow, self.view_run, self.view_name = workflow, None, "flow"
        if self.selected_node not in {n.id for n in workflow.nodes}:
            self.selected_node = workflow.nodes[0].id
        self.set_message(f"已保存 · {workflow.name}")
        self.render_workbench()

    def load_workflow(self, key: str | None) -> None:
        if key is None:
            return
        self.require_editable()
        self.workflow = self.store.get_workflow(key)
        self.view_run = None
        self.selected_node, self.view_name = self.workflow.nodes[0].id, "flow"
        self.set_message(f"已加载 · {self.workflow.name}")
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
            self.view_run, self.view_name = None, "flow"
            self.set_message(f"Worker 已保存 · @{worker.id} {worker.name}")
            self.render_workbench()

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
            Workflow(new_id(), name or "新工作流", "", [Node("A", "任务", workers[0].id)])
            if new
            else deepcopy(self.current_workflow())
        )
        self.push_screen(FlowEditor(workflow, workers), self.flow_saved)

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
                self.render_workbench()

    def action_stop(self) -> None:
        if self.busy:
            self._stop_requested = True
            self.engine.stop()
            self.set_message("正在停止…")
        else:
            self.set_message("当前没有运行")

    def on_engine_event(self, event: dict) -> None:
        if not self._shutting_down:
            self.render_workbench()

    async def fetch_models(self, worker: Worker) -> list[str]:
        worker.require_api()
        if self.client is not None:
            remote = await self.client.list_models()
        else:
            async with ModelClient(self.settings, worker) as client:
                remote = await client.list_models()
        return list(dict.fromkeys([worker.model, *remote]))

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
            if (current.base_url, current.api_key, current.extra_body) != (
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
        self.engine.stop()
        tasks = [t for t in (self._run_task, self._model_task) if t is not None]
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

    async def execute_command(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            if not text.startswith("/"):
                self.require_editable()
                workflow = deepcopy(self.current_workflow())
                workflow.task = text
                self.save_workflow(workflow)
                return
            command, _, arguments = text.partition(" ")
            arguments = arguments.strip()
            if command == "/run":
                if arguments == "stop":
                    self.action_stop()
                elif not arguments:
                    await self.action_run()
                else:
                    raise ValueError("用法：/run 或 /run stop")
            elif command == "/models":
                selected = (
                    next((n for n in self.workflow.nodes if n.id == self.selected_node), None)
                    if self.workflow
                    else None
                )
                if arguments or selected:
                    self.open_models(arguments or selected.worker_id)
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
                else:
                    self.push_screen(
                        PickerScreen(
                            "Worker 配置",
                            [
                                (w.id, Text(f"@{w.id}  {w.name}\n{w.model}", BLUE))
                                for w in self.store.list_workers()
                            ],
                            "/worker new 新增 · /worker <id> 编辑",
                        ),
                        lambda key: self.open_worker(key) if key else None,
                    )
            elif command == "/node":
                if not arguments:
                    raise ValueError("用法：/node <id>")
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
                    self.push_screen(
                        PickerScreen(
                            "工作流 · 选择后加载",
                            [
                                (w.id, Text(f"{w.name}\n{len(w.nodes)} 节点 · {w.id}", BLUE))
                                for w in self.store.list_workflows()
                            ],
                            "/flow new 新建 · /flow edit 编辑 · /flow save 名称 另存",
                        ),
                        self.load_workflow,
                    )
            elif command == "/view" and arguments in {"flow", "run", "result"}:
                self.view_name = arguments
                self.render_workbench()
            elif command == "/export":
                self.export_result()
            else:
                raise ValueError("未知命令 · 输入 / 查看命令")
        except (ValueError, OSError, RuntimeError) as exc:
            self.set_message(str(exc), error=True)

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
        text = event.value.strip()
        choices = list(COMMANDS)
        if text.startswith("/worker "):
            choices.extend((f"/worker {w.id}", w.name) for w in self.store.list_workers())
        if text.startswith("/flow "):
            choices.extend((f"/flow {w.id}", w.name) for w in self.store.list_workflows())
        if text.startswith("/node ") and self.workflow:
            choices.extend((f"/node {n.id}", n.label) for n in self.workflow.nodes)
        self.matches = (
            [item for item in choices if item[0].startswith(text)] if text.startswith("/") else []
        )
        menu = self.root.query_one("#command-menu", OptionList)
        menu.clear_options()
        menu.add_options(
            Option(Text.assemble((cmd, BLUE), (f"  {hint}", MUTED))) for cmd, hint in self.matches
        )
        menu.highlighted = 0 if self.matches else None
        menu.display = bool(self.matches)

    def move_suggestion(self, step: int) -> None:
        menu = self.root.query_one("#command-menu", OptionList)
        if self.matches:
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
        if text.strip() == "/" and self.matches:
            menu = self.root.query_one("#command-menu", OptionList)
            text = self.matches[menu.highlighted or 0][0]
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
        prompt = self.root.query_one("#prompt", Input)
        prompt.value = "/"
        prompt.focus()

    def action_escape(self) -> None:
        self.root.query_one("#command-menu").display = False
        self.root.query_one("#prompt", Input).focus()

    def render_workbench(self) -> None:
        if not self.is_mounted:
            return
        run = self.view_run
        workflow = run.workflow if run and self.view_name != "flow" else self.workflow
        states = run.nodes if run else {}
        workers = run.workers if run else {w.id: w for w in self.store.list_workers()}
        head = Text(" ctflow ", "bold " + ORANGE)
        head.append(" 03 / FLUX", BLUE)
        head.append(f"   {workflow.name if workflow else '新工作台'}", MUTED)
        if run:
            head.append(f"   {run.status} · {run.usage.total_tokens} Token · {elapsed(run)}", BLUE)
        self.root.query_one("#masthead", Static).update(head)
        map_pane = self.root.query_one("#map-pane", Pane)
        map_pane.border_title = "DAG · 点击节点装配 Worker"
        drawing = (
            graph(
                workflow,
                states,
                self.selected_node,
                max(20, map_pane.content_size.width),
                self.frame,
            )
            if workflow and workflow.nodes
            else Text("/worker new 配置 Worker → /flow new 创建工作流", MUTED)
        )
        # 多路并行时给画布足够行数，同时保留正文与底部输入区。
        map_pane.styles.height = (
            max(13, min(drawing.plain.count("\n") + 3, self.size.height - 12))
            if self.size.height >= 28
            else 9
        )
        map_pane.show_content(drawing)
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
                label = Text(f"{node.id}  {node.label}\n", TEXT)
                label.append(f"@{node.worker_id}  ", MUTED)
                label.append_text(
                    status_text(states[node.id].status if node.id in states else "waiting")
                )
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
        hint = (
            f"@{worker.id} · {worker.name}\n{fit(worker.model, 28)}\n"
            f"{' '.join('@' + s for s in worker.skills) or '未选技能'} · "
            f"MCP {', '.join(worker.mcp_servers) or '无'}\n\n"
            "/worker 配置\n/flow edit 编辑图"
            if worker
            else "/worker new 新增\n/flow new 创建图"
        )
        self.root.query_one("#assembly-hint", Static).update(Text(hint, MUTED))
        reading = self.root.query_one("#reading-pane", Pane)
        reading.border_title = {
            "flow": "编排 · 总任务",
            "run": "执行 · 节点输出",
            "result": "结果",
        }[self.view_name]
        output_key = (
            self.view_name,
            self.selected_node,
            workflow.task if workflow else "",
            tuple(
                (k, s.output, s.error, s.status, s.usage.total_tokens) for k, s in states.items()
            ),
        )
        if output_key != self._reading_key:
            self._reading_key = output_key
            if self.view_name == "flow":
                content = (
                    Text(workflow.task or "输入目标并回车保存总任务，再用 /run 执行。", TEXT)
                    if workflow
                    else Text("先配置 Worker，再创建工作流。", MUTED)
                )
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
                content = Markdown(
                    f"{state.status} · {state.usage.total_tokens} Token · "
                    f"{elapsed(state)}\n\n"
                    f"{state.output or '等待模型输出…'}\n\n{state.error}"
                )
            else:
                content = Text("当前工作流还没有运行结果 · /run", MUTED)
            reading.show_content(content, follow=self.busy)
