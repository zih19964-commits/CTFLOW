"""CTflow 模态弹窗与可视化构建器：Worker 装配、可视化工作流构建、节点依赖与确认框。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
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

from ctflow.graph import parse_workflow, to_mermaid, topological_layers, validate_workflow
from ctflow.models import ARCHITECT_ID, Node, Worker, Workflow, upstream_signature
from ctflow.rendering import BLUE, GREEN, MUTED, ORANGE, RED, TEXT, graph
from ctflow.tools import discover_skills, read_skills


def _next_node_id(existing: set[str]) -> str:
    """按 A..Z、N1..N99 顺序生成不冲突的节点 ID。"""
    for code in range(ord("A"), ord("Z") + 1):
        candidate = chr(code)
        if candidate not in existing:
            return candidate
    index = 1
    while f"N{index}" in existing:
        index += 1
    return f"N{index}"


def _build_template_nodes(kind: str, workers: list[Worker]) -> list[Node]:
    """根据所选结构模板和现有 Worker 列表生成开箱即用的节点拓扑。"""
    if not workers:
        raise ValueError("请先新增至少一个 Worker")

    def pick(preferred: tuple[str, ...], fallback_index: int) -> str:
        by_id = {w.id: w.id for w in workers}
        for key in preferred:
            if key in by_id:
                return key
        return workers[fallback_index % len(workers)].id

    if kind == "single":
        return [
            Node("A", "核心任务", pick(("judge", "research"), 0), "独立完成目标任务并给出结论。")
        ]
    if kind == "serial":
        return [
            Node(
                "A", "信息收集", pick(("scout-facts", "research"), 0), "收集关键事实、前提与约束。"
            ),
            Node(
                "B",
                "分析推导",
                pick(("synthesis", "code"), 1),
                "基于收集到的事实进行推理分析。",
                ["A"],
            ),
            Node(
                "C",
                "独立复核",
                pick(("judge", "reviewer"), 2),
                "核验推理链与结论，指出遗漏与建议。",
                ["B"],
            ),
        ]
    if kind == "review":
        return [
            Node("A", "资料调研", pick(("research", "scout-facts"), 0), "收集背景事实与参考依据。"),
            Node(
                "B", "技术评估", pick(("code", "scout-practice"), 1), "评估实现可行性与技术约束。"
            ),
            Node(
                "C",
                "正向论证",
                pick(("pro", "support"), 2),
                "总结优势、收益与推进条件。",
                ["A", "B"],
            ),
            Node(
                "D",
                "风险挑战",
                pick(("con", "challenge"), 3),
                "指出风险、反例与改进建议。",
                ["A", "B"],
            ),
            Node(
                "E", "综合裁决", pick(("judge",), 4), "权衡正反两面，给出可执行结论。", ["C", "D"]
            ),
        ]
    return [
        Node("A", "核实事实", pick(("scout-facts", "research"), 0), "核实议题关键事实与来源。"),
        Node("B", "实践案例", pick(("scout-practice", "code"), 1), "调查可比方案与实践结果。"),
        Node("C", "反例风险", pick(("scout-risks", "con"), 2), "寻找反例、约束与失败教训。"),
        Node(
            "D",
            "整合证据",
            pick(("synthesis",), 3),
            "合并三路结果，整理共识、冲突与缺口。",
            ["A", "B", "C"],
        ),
        Node(
            "E",
            "支持视角",
            pick(("support", "pro"), 4),
            "基于证据提出最强支持论证与推进条件。",
            ["D"],
        ),
        Node(
            "F",
            "反对视角",
            pick(("challenge", "con"), 5),
            "基于证据提出最强反对论证与替代方案。",
            ["D"],
        ),
        Node("G", "最终裁决", pick(("judge",), 6), "比较双方论据并作出最终决策建议。", ["E", "F"]),
    ]


class Dialog(ModalScreen):
    """圆角模态对话框基类：固定标题、关闭键与底栏，字段区独立滚动。"""

    BINDINGS = [
        Binding("escape", "cancel", "取消", priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
    ]

    def __init__(self, title: str, *, wide: bool = False) -> None:
        super().__init__()
        self.heading = title
        self._wide = wide

    def body(self) -> ComposeResult:
        yield Static("")

    def footer_hint(self) -> str:
        return "Ctrl+S 保存 · Esc 取消"

    def compose(self) -> ComposeResult:
        classes = "dialog dialog-wide" if self._wide else "dialog"
        with Vertical(classes=classes):
            with Horizontal(classes="dialog-heading"):
                yield Static(self.heading, classes="dialog-title", markup=False)
                yield Button("×", id="dialog-close", tooltip="取消未保存修改 · Esc")
            with VerticalScroll(id="dialog-fields"):
                yield from self.body()
            yield Static("", id="form-error", markup=False)
            yield Static(self.footer_hint(), classes="dialog-footer", markup=False)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"page_up", "page_down"}:
            focused = self.focused
            if focused is not None and focused.id != "dialog-fields":
                max_y = getattr(focused, "max_scroll_y", 0)
                cur_y = getattr(focused, "scroll_y", 0)
                if action == "page_up" and max_y > 0 and cur_y > 0:
                    return False
                if action == "page_down" and max_y > 0 and cur_y < max_y:
                    return False
        return True

    def action_page_up(self) -> None:
        self.query_one("#dialog-fields", VerticalScroll).scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.query_one("#dialog-fields", VerticalScroll).scroll_page_down(animate=False)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def set_feedback(self, message: str, *, tone: str = "error") -> None:
        color = {"error": RED, "ok": GREEN, "info": BLUE, "warn": ORANGE}.get(tone, RED)
        self.query_one("#form-error", Static).update(Text(message, color))

    @on(Button.Pressed, "#dialog-close")
    def close_dialog(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()


class ConfirmScreen(Dialog):
    """简洁圆角二次确认对话框，用于删除等不可逆操作。"""

    BINDINGS = [
        Binding("enter", "confirm", "确认", priority=True),
        Binding("escape", "cancel", "取消", priority=True),
    ]

    def __init__(self, title: str, prompt_text: str, confirm_label: str = "确认删除") -> None:
        super().__init__(title)
        self.prompt_text = prompt_text
        self.confirm_label = confirm_label

    def footer_hint(self) -> str:
        return "Enter 确认 · Esc 取消"

    def body(self) -> ComposeResult:
        yield Static(self.prompt_text, classes="confirm-body", markup=False)
        with Horizontal(classes="action-row"):
            yield Button(self.confirm_label, id="confirm-yes", classes="btn-danger")
            yield Button("取消", id="confirm-no", classes="btn-subtle")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-yes")
    def on_yes(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def on_no(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(False)


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
        wide: bool = False,
    ) -> None:
        super().__init__(title, wide=wide)
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
        first_key = next(iter(self.fields), None)
        if first_key:
            self.query_one(f"#field-{first_key}").focus()

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
            self.set_feedback(self.redact(str(exc)), tone="error")
            return
        self.dismiss(result)

    def redact(self, text: str) -> str:
        return self.app.redact(text)


class WorkerScreen(FormScreen):
    """Worker 配置窗口：统一 Chat Completions 上游连接，MCP 与 Skill 均采用多选框勾选。"""

    def __init__(self, worker: Worker, models: list[str], skills_dir: Path) -> None:
        self.worker = deepcopy(worker)
        self.is_architect = worker.id == ARCHITECT_ID
        self.models = (
            list(dict.fromkeys(models)) if models else ([worker.model] if worker.model else [])
        )
        self.skills_dir = skills_dir
        self.skills = {} if self.is_architect else discover_skills(skills_dir)
        self._origin_upstream = upstream_signature(worker.base_url)
        self._last_upstream = self._origin_upstream
        self._origin_extra_text = json.dumps(worker.extra_body, ensure_ascii=False, indent=2)
        self._upstream_changed = False
        self._mounted = False
        fields: dict[str, tuple[str, str]] = {
            "name": ("Worker 名称", worker.name),
            "base_url": ("Base URL · OpenAI 兼容自定义上游根地址", worker.base_url),
            "api_key": ("API Key · 保存于系统凭据管理器（keyring）", worker.api_key),
            "model": ("模型 ID · 刷新后选择或手动填写", worker.model),
            "extra_body": (
                "附加请求参数 extra_body · JSON 对象（切换上游将自动清空旧参数）",
                self._origin_extra_text,
            ),
        }
        multiline: tuple[str, ...] = ("extra_body",)
        if not self.is_architect:
            fields["prompt"] = ("固定上下文", worker.prompt)
            multiline = ("prompt", "extra_body")
        heading = (
            f"编排助手连接配置 · @{worker.id} [内置隔离]"
            if self.is_architect
            else f"Worker 配置 · @{worker.id}"
        )
        super().__init__(
            heading,
            fields,
            multiline=multiline,
            required=("name", "model"),
            passwords=("api_key",),
            wide=True,
        )

    def body(self) -> ComposeResult:
        for key in ("name", "base_url", "api_key"):
            label, value = self.fields[key]
            yield Label(label)
            yield Input(value, id=f"field-{key}", password=key in self.passwords)

        yield Label(f"@{self.worker.id} 可用模型 · 使用上方连接草稿刷新")
        with Horizontal(classes="action-row"):
            yield Button("刷新模型", id="refresh-models", classes="btn-primary")
            yield Button("清空 extra_body", id="reset-extra", classes="btn-subtle")
        options = [(m, m) for m in self.models]
        selected_val = self.worker.model if self.worker.model in self.models else Select.BLANK
        yield Select(
            options,
            value=selected_val,
            allow_blank=True,
            prompt="选择模型或点击「刷新模型」获取列表",
            id="model-choice",
        )

        for key in ("model", "extra_body"):
            label, value = self.fields[key]
            yield Label(label)
            if key in self.multiline:
                yield TextArea(value, id=f"field-{key}")
            else:
                yield Input(value, id=f"field-{key}")

        if self.is_architect:
            yield Static(
                "安全隔离：@architect 的编排规范提示词与受管目录白名单已在源码中只读锁定。",
                classes="field-note",
                markup=False,
            )
            return

        registered_mcp = sorted(self.app.settings.mcp_servers.keys())
        mcp_names = list(dict.fromkeys([*registered_mcp, *self.worker.mcp_servers]))
        yield Label("MCP 服务器 · Space 多选（在 extensions/mcp.json 中登记）")
        if mcp_names:
            yield SelectionList[str](
                *[
                    (
                        f"{m}" + (" · 未在 mcp.json 登记" if m not in registered_mcp else ""),
                        m,
                        m in self.worker.mcp_servers,
                    )
                    for m in mcp_names
                ],
                id="mcp-options",
            )
        else:
            yield Static(
                "尚未登记外部 MCP，可在 extensions/mcp.json 中按标准 mcpServers 格式添加。",
                classes="field-note",
                markup=False,
            )

        yield Label("@技能 · Space 多选，打开配置时自动发现")
        yield Static(
            str(self.skills_dir / "<name>" / "SKILL.md"),
            classes="field-note",
            markup=False,
        )
        skill_names = list(dict.fromkeys([*self.skills, *self.worker.skills]))
        yield SelectionList[str](
            *[
                (
                    f"@{s}" + (" · 文件缺失" if s not in self.skills else ""),
                    s,
                    s in self.worker.skills,
                )
                for s in skill_names
            ],
            id="skill-options",
        )
        if not skill_names:
            yield Static(
                "将技能放入 extensions/skills/<name>/SKILL.md 后重新打开配置即可勾选。",
                classes="field-note",
                markup=False,
            )

        yield Label(self.fields["prompt"][0])
        yield TextArea(self.fields["prompt"][1], id="field-prompt")

    def on_mount(self) -> None:
        super().on_mount()
        self._mounted = True

    def _handle_upstream_change(self) -> None:
        """上游根地址变化时自动清空旧上游专属参数与模型缓存，防止供应商参数残留。"""
        if not self._mounted:
            return
        new_sig = upstream_signature(self.query_one("#field-base_url", Input).value)
        if new_sig == self._last_upstream:
            return
        prev_had_url = bool(self._last_upstream)
        self._last_upstream = new_sig
        if not prev_had_url:
            return

        self._upstream_changed = True
        cleaned_parts: list[str] = []
        if self.models:
            self.models = []
            choice = self.query_one("#model-choice", Select)
            with self.prevent(Select.Changed):
                choice.set_options([])
                choice.clear()
            cleaned_parts.append("模型列表缓存")

        extra_widget = self.query_one("#field-extra_body", TextArea)
        if (
            self._origin_extra_text.strip() not in ("", "{}")
            and extra_widget.text.strip() == self._origin_extra_text.strip()
        ):
            extra_widget.load_text("{}")
            cleaned_parts.append("旧上游 extra_body")

        if cleaned_parts:
            self.set_feedback(
                f"连接已修改（上游变更）：已自动清空{'与'.join(cleaned_parts)}，请重新刷新模型",
                tone="warn",
            )
        else:
            self.set_feedback("连接已修改（上游变更），请重新刷新模型", tone="info")

    @on(Input.Changed, "#field-base_url")
    def base_url_changed(self, event: Input.Changed) -> None:
        self._handle_upstream_change()

    @on(Button.Pressed, "#reset-extra")
    def reset_extra_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one("#field-extra_body", TextArea).load_text("{}")
        self.set_feedback("已将 extra_body 重置为 {}", tone="info")

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
        self.set_feedback("", tone="info")
        draft = None
        try:
            draft = self.draft(allow_blank_model=True)
            remote = await self.app.fetch_models(draft)
            current_draft = self.draft(allow_blank_model=True)
            if (current_draft.base_url, current_draft.api_key, current_draft.extra_body) != (
                draft.base_url,
                draft.api_key,
                draft.extra_body,
            ):
                self.set_feedback("连接已修改，请重新刷新模型", tone="warn")
                return

            model_input = self.query_one("#field-model", Input)
            current = model_input.value.strip()
            choice = self.query_one("#model-choice", Select)

            auto_switched = ""
            if remote and (not current or (self._upstream_changed and current not in remote)):
                selected = remote[0]
                model_input.value = selected
                self.models = list(remote)
                auto_switched = selected
            elif current and current not in remote:
                self.models = list(dict.fromkeys([current, *remote]))
                selected = current
            else:
                self.models = list(remote)
                selected = current if current in remote else (remote[0] if remote else "")

            with self.prevent(Select.Changed):
                choice.set_options([(model, model) for model in self.models])
                if selected and selected in self.models:
                    choice.value = selected
                else:
                    choice.clear()

            self._origin_upstream = upstream_signature(draft.base_url)
            self._last_upstream = self._origin_upstream
            self._upstream_changed = False
            if auto_switched:
                self.set_feedback(
                    f"已刷新 · @{draft.id} · {len(remote)} 个模型（已自动切换为 {auto_switched}）",
                    tone="ok",
                )
            else:
                self.set_feedback(f"已刷新 · @{draft.id} · {len(remote)} 个模型", tone="ok")
        except Exception as exc:
            message = draft.redact(str(exc)) if draft else self.redact(str(exc))
            self.set_feedback(message, tone="error")
        finally:
            button.disabled = False
            button.label = "刷新模型"

    def draft(
        self, values: dict[str, str] | None = None, *, allow_blank_model: bool = False
    ) -> Worker:
        values = self.values() if values is None else values
        raw_extra = values["extra_body"].strip() or "{}"
        try:
            extra_body = json.loads(raw_extra)
        except json.JSONDecodeError:
            raise ValueError("附加参数 extra_body 必须是合法的 JSON 对象") from None
        if not isinstance(extra_body, dict):
            raise ValueError("附加参数 extra_body 必须是 JSON 对象")

        model_id = values["model"].strip()
        if not model_id and allow_blank_model:
            model_id = "__refresh__"

        if self.is_architect:
            return Worker(
                self.worker.id,
                values["name"].strip(),
                model_id,
                self.worker.prompt,
                [],
                base_url=values["base_url"].strip(),
                api_key=values["api_key"],
                extra_body=extra_body,
                mcp_servers=list(self.worker.mcp_servers),
            )

        skills = list(self.query_one("#skill-options", SelectionList).selected)
        mcp_widgets = self.query("#mcp-options")
        mcp_servers = list(mcp_widgets.first(SelectionList).selected) if mcp_widgets else []
        return Worker(
            self.worker.id,
            values["name"].strip(),
            model_id,
            values["prompt"],
            skills,
            base_url=values["base_url"].strip(),
            api_key=values["api_key"],
            extra_body=extra_body,
            mcp_servers=mcp_servers,
        )

    def redact(self, text: str) -> str:
        draft = deepcopy(self.worker)
        draft.api_key = self.query_one("#field-api_key", Input).value
        return self.app.redact(draft.redact(text))

    def validate(self, values: dict[str, str]) -> Worker:
        super().validate(values)
        worker = self.draft(values)
        if not self.is_architect:
            unknown = set(worker.mcp_servers) - self.app.settings.mcp_servers.keys()
            if unknown:
                raise ValueError(f"未登记的 MCP 服务器：{', '.join(sorted(unknown))}")
            read_skills(self.skills_dir, worker.skills)
        return worker


class NodeScreen(FormScreen):
    """节点装配与新增弹窗：通过可视化多选框选择上游依赖。"""

    def __init__(
        self,
        node: Node,
        workflow: Workflow,
        workers: list[Worker],
        *,
        is_new: bool = False,
    ) -> None:
        self.node = deepcopy(node)
        self.workflow = deepcopy(workflow)
        self.worker_list = list(workers)
        self.worker_catalog = {w.id: w for w in workers}
        self.is_new = is_new
        title = f"新增节点 · {node.id}" if is_new else f"节点 {node.id} · 装配 Worker 与依赖"
        fields: dict[str, tuple[str, str]] = {}
        if is_new:
            fields["node_id"] = ("节点 ID · 字母/数字/下划线（默认自动分配）", node.id)
        fields.update(
            {
                "worker": ("绑定 Worker · /worker <id> 可编辑连接与提示词", node.worker_id),
                "label": ("节点名称", node.label),
                "task": ("节点补充任务（可选，与总任务及 Worker 提示词叠加）", node.task),
            }
        )
        super().__init__(
            title,
            fields,
            multiline=("task",),
            choices={
                "worker": [(f"@{w.id} · {w.name} ({w.model})", w.id) for w in workers],
            },
            required=("node_id", "label") if is_new else ("label",),
        )

    def _downstream_ids(self) -> set[str]:
        if self.is_new:
            return set()
        children: dict[str, list[str]] = {n.id: [] for n in self.workflow.nodes}
        for item in self.workflow.nodes:
            if item.id == self.node.id:
                continue
            for dep in item.dependencies:
                if dep in children:
                    children[dep].append(item.id)
        visited: set[str] = set()
        queue = list(children.get(self.node.id, ()))
        while queue:
            curr = queue.pop(0)
            if curr not in visited:
                visited.add(curr)
                queue.extend(children.get(curr, ()))
        return visited

    def body(self) -> ComposeResult:
        yield from super().body()
        other_nodes = [n for n in self.workflow.nodes if n.id != self.node.id]
        downstream = self._downstream_ids()
        yield Label("直接上游依赖 · Space 或点击多选（不选则为起始节点）")
        if other_nodes:
            yield SelectionList[str](
                *[
                    (
                        f"{n.id} · {n.label} (@{n.worker_id})"
                        + (" · 下游节点（勾选将成环）" if n.id in downstream else ""),
                        n.id,
                        n.id in self.node.dependencies,
                    )
                    for n in other_nodes
                ],
                id="field-deps-list",
            )
        else:
            yield Static(
                "当前工作流暂无其他节点，保存后此节点将作为起点。",
                classes="field-note",
                markup=False,
            )

    def _build_candidate(
        self, values: dict[str, str], *, placeholder_label: bool = False
    ) -> tuple[Node, Workflow]:
        node_id = values["node_id"].strip() if self.is_new else self.node.id
        label = values["label"].strip() or ("_" if placeholder_label else "")
        deps_widgets = self.query("#field-deps-list")
        dependencies = list(deps_widgets.first(SelectionList).selected) if deps_widgets else []
        node = Node(
            node_id,
            label,
            values["worker"],
            values["task"],
            dependencies,
        )
        candidate = deepcopy(self.workflow)
        if self.is_new:
            candidate.nodes.append(node)
        else:
            candidate.nodes = [node if n.id == node.id else n for n in candidate.nodes]
        return node, candidate

    def _check_live_dag_conflict(self) -> None:
        if not self.is_mounted:
            return
        try:
            _, candidate = self._build_candidate(self.values(), placeholder_label=True)
            validate_workflow(candidate, self.worker_catalog)
        except ValueError as exc:
            self.set_feedback(f"{self.redact(str(exc))}（冲突已禁止保存）", tone="error")
        else:
            self.set_feedback("", tone="info")

    @on(SelectionList.SelectedChanged, "#field-deps-list")
    def deps_selection_changed(self) -> None:
        self._check_live_dag_conflict()

    @on(Input.Changed, "#field-node_id")
    def node_id_changed(self) -> None:
        self._check_live_dag_conflict()

    def validate(self, values: dict[str, str]) -> Node:
        super().validate(values)
        node, candidate = self._build_candidate(values)
        validate_workflow(candidate, self.worker_catalog)
        return node


class FlowEditor(Dialog):
    """可视化工作流构建器 + 高级 Mermaid 源码双模式。"""

    BINDINGS = [
        Binding("ctrl+s", "save", "保存", priority=True),
        Binding("escape", "cancel", "取消", priority=True),
    ]

    def __init__(self, workflow: Workflow, workers: list[Worker], *, is_new: bool = False) -> None:
        self.draft_workflow = deepcopy(workflow)
        self.worker_list = list(workers)
        self.worker_catalog = {w.id: w for w in workers}
        self.is_new = is_new
        self.mode = "visual"
        self.selected_node_id = workflow.nodes[0].id if workflow.nodes else ""
        self._last_preview_width = 0
        title = "新建工作流 · 可视化编排" if is_new else f"编辑工作流 · {workflow.name}"
        super().__init__(title, wide=True)

    def footer_hint(self) -> str:
        return "Ctrl+S 保存 · 模板一键搭结构 · +添加 / Enter编辑节点 · Esc 取消"

    def body(self) -> ComposeResult:
        yield Label("工作流名称")
        yield Input(self.draft_workflow.name, id="field-name", placeholder="例如：方案多视角评审")
        yield Label("总任务描述")
        yield TextArea(self.draft_workflow.task, id="field-task")

        with Horizontal(classes="mode-bar"):
            yield Button("可视化编排", id="tab-visual", classes="mode-tab active")
            yield Button("Mermaid 源码（高级）", id="tab-source", classes="mode-tab")

        with Vertical(id="panel-visual"):
            yield Label("快捷模板 · 一键生成拓扑骨架（自动分配已有 Worker）")
            with Horizontal(classes="template-row"):
                yield Button("单节点", id="tpl-single", classes="btn-chip")
                yield Button("1→1→1 串行", id="tpl-serial", classes="btn-chip")
                yield Button("2→2→1 并行评审", id="tpl-review", classes="btn-chip")
                yield Button("3→1→2→1 辩论裁决", id="tpl-debate", classes="btn-chip")

            yield Label("节点与依赖编排 · 无需手写语法")
            with Horizontal(classes="action-row"):
                yield Button("+ 添加节点", id="flow-add-node", classes="btn-primary")
                yield Button("编辑选中节点", id="flow-edit-node", classes="btn-subtle")
                yield Button("删除选中节点", id="flow-del-node", classes="btn-danger")

            yield OptionList(id="flow-nodes", markup=False)
            yield Label("实时拓扑预览")
            yield Static(id="flow-preview", classes="preview-box", markup=False)

        with Vertical(id="panel-source"):
            yield Label('Mermaid 语法 · A["节点名称 @worker_id"]，A --> B')
            try:
                source = (
                    to_mermaid(self.draft_workflow)
                    if self.draft_workflow.nodes
                    else "flowchart TD\n"
                )
            except ValueError:
                source = self.draft_workflow.source or "flowchart TD\n"
            yield TextArea(source, id="field-source", show_line_numbers=True)

    def on_mount(self) -> None:
        self.query_one("#panel-source").display = False
        self._refresh_visual()
        self.call_after_refresh(self._refresh_preview_if_resized)
        self.query_one("#field-name", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted and self.mode == "visual":
            self.call_after_refresh(self._refresh_preview_if_resized)

    def _preview_width(self) -> int:
        preview_box = self.query_one("#flow-preview", Static)
        if preview_box.content_size.width > 0:
            return preview_box.content_size.width
        dialog_inner = min(112, int(self.app.size.width * 0.92)) - 11
        return max(40, dialog_inner)

    def _update_preview(self) -> None:
        preview_box = self.query_one("#flow-preview", Static)
        if self.draft_workflow.nodes:
            width = self._preview_width()
            self._last_preview_width = width
            try:
                validate_workflow(self.draft_workflow, self.worker_catalog)
                preview = graph(
                    self.draft_workflow, {}, self.selected_node_id, width, 0, interactive=False
                )
            except ValueError as exc:
                preview = Text(f"拓扑冲突，无法显示与运行：{exc}", RED)
                self.set_feedback(f"{self.app.redact(str(exc))}（冲突已禁止保存）", tone="error")
        else:
            self._last_preview_width = 0
            preview = Text("暂无节点，请点击上方「+ 添加节点」或选择一键模板。", MUTED)
        preview_box.update(preview)

    def _refresh_preview_if_resized(self) -> None:
        if self.is_mounted and self.mode == "visual" and self.draft_workflow.nodes:
            if self._preview_width() != self._last_preview_width:
                self._update_preview()

    def _sync_source_from_draft(self) -> None:
        source = to_mermaid(self.draft_workflow) if self.draft_workflow.nodes else "flowchart TD\n"
        self.query_one("#field-source", TextArea).load_text(source)

    def _sync_draft_from_source(self) -> None:
        raw = self.query_one("#field-source", TextArea).text
        parsed = parse_workflow(
            raw,
            self.worker_catalog,
            workflow_id=self.draft_workflow.id,
            name=self.query_one("#field-name", Input).value.strip() or self.draft_workflow.name,
            task=self.query_one("#field-task", TextArea).text,
        )
        old_tasks = {n.id: n.task for n in self.draft_workflow.nodes}
        for node in parsed.nodes:
            node.task = old_tasks.get(node.id, "")
        self.draft_workflow = parsed
        if self.selected_node_id not in {n.id for n in parsed.nodes}:
            self.selected_node_id = parsed.nodes[0].id if parsed.nodes else ""

    def _refresh_visual(self) -> None:
        listing = self.query_one("#flow-nodes", OptionList)
        listing.clear_options()
        nodes = self.draft_workflow.nodes
        if nodes:
            try:
                layers = topological_layers(self.draft_workflow)
                depth_map = {nid: idx for idx, layer in enumerate(layers, start=1) for nid in layer}
                ordered_ids = [nid for layer in layers for nid in layer]
                by_id = {n.id: n for n in nodes}
                ordered = [by_id[nid] for nid in ordered_ids]
            except ValueError:
                depth_map = {}
                ordered = list(nodes)
            for node in ordered:
                layer_tag = f"L{depth_map[node.id]} " if node.id in depth_map else ""
                deps = ", ".join(node.dependencies) if node.dependencies else "起点"
                label = Text()
                label.append(f"{layer_tag}", MUTED)
                label.append(f"{node.id} · {node.label}", TEXT)
                label.append(f"  @{node.worker_id}", BLUE)
                label.append(f"  ← {deps}", ORANGE if node.dependencies else GREEN)
                listing.add_option(Option(label, id=node.id))
            if self.selected_node_id not in {n.id for n in ordered}:
                self.selected_node_id = ordered[0].id
            listing.highlighted = next(
                (i for i, n in enumerate(ordered) if n.id == self.selected_node_id), 0
            )
        self._update_preview()

    def _switch_mode(self, target: str) -> None:
        if target == self.mode:
            return
        self.set_feedback("", tone="info")
        if target == "source":
            try:
                self._sync_source_from_draft()
            except ValueError as exc:
                self.set_feedback(str(exc), tone="error")
                return
            self.mode = "source"
            self.query_one("#panel-visual").display = False
            self.query_one("#panel-source").display = True
            self.query_one("#tab-visual", Button).remove_class("active")
            self.query_one("#tab-source", Button).add_class("active")
            self.query_one("#field-source", TextArea).focus()
        else:
            try:
                self._sync_draft_from_source()
            except ValueError as exc:
                self.set_feedback(str(exc), tone="error")
                return
            self.mode = "visual"
            self.query_one("#panel-source").display = False
            self.query_one("#panel-visual").display = True
            self.query_one("#tab-source", Button).remove_class("active")
            self.query_one("#tab-visual", Button).add_class("active")
            self._refresh_visual()

    @on(Button.Pressed, "#tab-visual")
    def click_tab_visual(self, event: Button.Pressed) -> None:
        event.stop()
        self._switch_mode("visual")

    @on(Button.Pressed, "#tab-source")
    def click_tab_source(self, event: Button.Pressed) -> None:
        event.stop()
        self._switch_mode("source")

    @on(TextArea.Changed, "#field-source")
    def source_changed(self, event: TextArea.Changed) -> None:
        if not self.is_mounted or self.mode != "source":
            return
        try:
            parse_workflow(
                event.text_area.text,
                self.worker_catalog,
                workflow_id=self.draft_workflow.id,
                name=self.query_one("#field-name", Input).value.strip() or self.draft_workflow.name,
            )
        except ValueError as exc:
            self.set_feedback(f"{self.app.redact(str(exc))}（冲突已禁止保存）", tone="error")
        else:
            self.set_feedback("", tone="info")

    @on(Button.Pressed, "#tpl-single")
    @on(Button.Pressed, "#tpl-serial")
    @on(Button.Pressed, "#tpl-review")
    @on(Button.Pressed, "#tpl-debate")
    def apply_template(self, event: Button.Pressed) -> None:
        event.stop()
        kind = (event.button.id or "").removeprefix("tpl-")
        try:
            self.draft_workflow.nodes = _build_template_nodes(kind, self.worker_list)
            self.selected_node_id = self.draft_workflow.nodes[0].id
            self._refresh_visual()
            self._sync_source_from_draft()
            self.set_feedback(
                f"已套用模板（{len(self.draft_workflow.nodes)} 个节点），可继续微调", tone="ok"
            )
        except ValueError as exc:
            self.set_feedback(str(exc), tone="error")

    @on(OptionList.OptionHighlighted, "#flow-nodes")
    def node_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id:
            self.selected_node_id = event.option.id
            self._update_preview()

    @on(OptionList.OptionSelected, "#flow-nodes")
    def node_double_activated(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option and event.option.id:
            self.selected_node_id = event.option.id
            self._open_edit_node()

    @on(Button.Pressed, "#flow-add-node")
    def add_node_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        existing_ids = {n.id for n in self.draft_workflow.nodes}
        next_id = _next_node_id(existing_ids)
        default_deps = [self.selected_node_id] if self.selected_node_id in existing_ids else []
        new_node = Node(next_id, f"步骤 {next_id}", self.worker_list[0].id, "", default_deps)

        def on_added(result: Node | None) -> None:
            if result is not None:
                self.draft_workflow.nodes.append(result)
                self.selected_node_id = result.id
                self._refresh_visual()
                self._sync_source_from_draft()
                self.set_feedback(f"已添加节点 {result.id} · {result.label}", tone="ok")

        self.app.push_screen(
            NodeScreen(new_node, self.draft_workflow, self.worker_list, is_new=True), on_added
        )

    def _open_edit_node(self) -> None:
        target = next((n for n in self.draft_workflow.nodes if n.id == self.selected_node_id), None)
        if target is None:
            self.set_feedback("请先在列表中选择一个节点", tone="warn")
            return

        def on_edited(result: Node | None) -> None:
            if result is not None:
                self.draft_workflow.nodes = [
                    result if n.id == result.id else n for n in self.draft_workflow.nodes
                ]
                self._refresh_visual()
                self._sync_source_from_draft()
                self.set_feedback(f"已更新节点 {result.id} · {result.label}", tone="ok")

        self.app.push_screen(NodeScreen(target, self.draft_workflow, self.worker_list), on_edited)

    @on(Button.Pressed, "#flow-edit-node")
    def edit_node_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        self._open_edit_node()

    @on(Button.Pressed, "#flow-del-node")
    def delete_node_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        if len(self.draft_workflow.nodes) <= 1:
            self.set_feedback("工作流至少需要保留一个节点", tone="warn")
            return
        removed_id = self.selected_node_id
        self.draft_workflow.nodes = [
            Node(
                n.id,
                n.label,
                n.worker_id,
                n.task,
                [dep for dep in n.dependencies if dep != removed_id],
            )
            for n in self.draft_workflow.nodes
            if n.id != removed_id
        ]
        self.selected_node_id = self.draft_workflow.nodes[0].id
        self._refresh_visual()
        self._sync_source_from_draft()
        self.set_feedback(f"已删除节点 {removed_id}（并清理下游失效连线）", tone="info")

    def action_save(self) -> None:
        try:
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                raise ValueError("请填写工作流名称")
            task = self.query_one("#field-task", TextArea).text
            if self.mode == "source":
                self._sync_draft_from_source()
            result = deepcopy(self.draft_workflow)
            result.name = name
            result.task = task
            validate_workflow(result, self.worker_catalog)
            result.source = to_mermaid(result)
        except (ValueError, OSError, RuntimeError) as exc:
            self.set_feedback(self.app.redact(str(exc)), tone="error")
            return
        self.dismiss(result)


class PickerScreen(Dialog):
    def __init__(self, title: str, options: list[tuple[str, Text]], hint: str) -> None:
        super().__init__(title)
        self.options, self.hint = options, hint

    def footer_hint(self) -> str:
        return "↑↓ 选择 · Enter 打开 · Esc 取消"

    def body(self) -> ComposeResult:
        yield Input(placeholder="输入名称或 ID 实时筛选…", id="picker-filter")
        yield OptionList(id="picker-options", markup=False)
        yield Static(self.hint, classes="field-note", markup=False)

    def on_mount(self) -> None:
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

    @on(events.Key)
    def handle_arrow_navigation(self, event: events.Key) -> None:
        if self.query_one("#picker-filter", Input).has_focus and event.key in {"down", "up"}:
            listing = self.query_one("#picker-options", OptionList)
            if listing.option_count:
                listing.focus()
                event.prevent_default()


class ModelPicker(PickerScreen):
    def __init__(self, worker: Worker, models: list[str]) -> None:
        self.worker = deepcopy(worker)
        super().__init__(
            f"模型列表 · @{worker.id} · {worker.name}",
            [(model, Text(model, BLUE)) for model in models],
            f"Enter 立即切换模型 · /worker {worker.id} 编辑上游连接",
        )

    def body(self) -> ComposeResult:
        yield Button("刷新模型", id="refresh-models", classes="btn-primary")
        yield from super().body()

    @on(Button.Pressed, "#refresh-models")
    def refresh_clicked(self, event: Button.Pressed) -> None:
        event.stop()
        event.button.disabled = True
        event.button.label = "刷新中…"
        self.run_worker(self.refresh_models(), group="models", exclusive=True)

    async def refresh_models(self) -> None:
        button = self.query_one("#refresh-models", Button)
        self.set_feedback("", tone="info")
        try:
            models = await self.app.fetch_models(self.worker)
            self.app.worker_models[self.worker.id] = models
            self.options = [(model, Text(model, BLUE)) for model in models]
            self.filter_options(self.query_one("#picker-filter", Input).value)
            self.set_feedback(f"已刷新 · @{self.worker.id} · {len(models)} 个模型", tone="ok")
        except Exception as exc:
            self.set_feedback(self.worker.redact(str(exc)), tone="error")
        finally:
            button.disabled = False
            button.label = "刷新模型"
