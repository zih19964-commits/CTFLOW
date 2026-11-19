"""CTflow 终端字符渲染：启动画面、圆角拓扑画布、状态徽标与工具调用轨迹。"""

from __future__ import annotations

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from ctflow.graph import topological_layers
from ctflow.models import NodeRun, Workflow

TEXT = "#dce3f0"
MUTED = "#7f8a9e"
BLUE = "#7aa2f7"
GREEN = "#73daca"
ORANGE = "#ff9e64"
RED = "#f7768e"
LINE = "#3b4459"

STATUS_META: dict[str, tuple[str, str]] = {
    "waiting": ("◌", MUTED),
    "ready": ("◎", ORANGE),
    "running": ("◉", BLUE),
    "success": ("✓", GREEN),
    "failed": ("✕", RED),
    "blocked": ("⊘", RED),
    "stopped": ("■", ORANGE),
    "pending": ("◌", MUTED),
}


def fit(value: str, width: int) -> str:
    text = Text(value)
    text.truncate(max(0, width), overflow="ellipsis")
    return text.plain


def status_text(status: str, frame: int = 0) -> Text:
    icon, color = STATUS_META.get(status, ("·", MUTED))
    if status == "running":
        dots = "." * (frame % 4 + 1)
        return Text(f"{icon} running{dots}".ljust(13), BLUE)
    return Text(f"{icon} {status}", color)


def node_style(node_id: str, color: str) -> Style:
    # Rich 的点击元数据由 Textual 分发到 App，整张节点卡都可以点击。
    return Style(color=color, meta={"@click": f"app.select_node('{node_id}')"})


def workflow_summary(workflow: Workflow) -> str:
    """生成一行拓扑分层摘要，例如 '3层 · A,B → C,D → E'。"""
    if not workflow.nodes:
        return "0 节点"
    try:
        layers = topological_layers(workflow)
    except ValueError:
        return f"{len(workflow.nodes)} 节点"
    chain = " → ".join(",".join(layer) for layer in layers)
    return f"{len(layers)}层 · {chain}"


def render_splash(
    worker_count: int,
    workflow_count: int,
    run_count: int,
    *,
    compact: bool = False,
) -> Text:
    """渲染 OpenCode / Codex 风格的精炼启动横幅；窄矮终端自动切换为紧凑样式。"""
    stats = f"{workflow_count} 工作流  ·  {worker_count} Workers  ·  {run_count} 历史运行"
    if compact:
        text = Text()
        text.append("◈ CTflow", "bold " + ORANGE)
        text.append("  多 Worker 依赖图终端", BLUE)
        text.append(f"   {stats}", MUTED)
        return text

    logo_lines = [
        (" ██████╗████████╗", "███████╗██╗      ██████╗ ██╗    ██╗"),
        ("██╔════╝╚══██╔══╝", "██╔════╝██║     ██╔═══██╗██║    ██║"),
        ("██║        ██║   ", "█████╗  ██║     ██║   ██║██║ █╗ ██║"),
        ("██║        ██║   ", "██╔══╝  ██║     ██║   ██║██║███╗██║"),
        ("╚██████╗   ██║   ", "██║     ███████╗╚██████╔╝╚███╔███╔╝"),
        (" ╚═════╝   ╚═╝   ", "╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝ "),
    ]
    result = Text()
    for left, right in logo_lines:
        result.append(left, "bold " + ORANGE)
        result.append(right + "\n", "bold " + BLUE)
    result.append("多 Worker 依赖图与独立对话终端", TEXT)
    result.append("   ·   ", LINE)
    result.append(stats, MUTED)
    return result


def format_tool_steps_markdown(steps: list[dict]) -> str:
    """将工具调用轨迹格式化为简洁可读的 Markdown 段落。"""
    if not steps:
        return ""
    blocks: list[str] = []
    for step in steps:
        status = step.get("status", "running")
        icon = {"ok": "✓", "error": "✕", "running": "◉"}.get(status, "·")
        ms = step.get("duration_ms", 0)
        timing = f" · {ms}ms" if status != "running" else " · 执行中…"
        args = str(step.get("arguments", "")).replace("\n", " ").strip()
        if len(args) > 120:
            args = args[:117] + "..."
        lines = [f"> **{icon} Step {step.get('step', '?')} · `{step.get('name', '')}`**{timing}"]
        if args and args != "{}":
            lines.append(f"> 参数：`{args}`")
        result_text = str(step.get("result", "")).strip()
        if result_text:
            first_line = result_text.splitlines()[0]
            if len(first_line) > 140:
                first_line = first_line[:137] + "..."
            lines.append(f"> 返回：{first_line}")
        blocks.append("  \n".join(lines))
    return "\n\n".join(blocks)


def extract_tool_steps(logs: list[dict]) -> list[dict]:
    """从节点日志中提取结构化的工具步骤记录。"""
    return [entry for entry in logs if entry.get("type") == "tool_step"]


class _Grid:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.chars = [[" " for _ in range(width)] for _ in range(height)]
        self.styles: list[list[str | Style]] = [
            [MUTED for _ in range(width)] for _ in range(height)
        ]

    def put(self, x: int, y: int, value: str, style: str | Style = MUTED) -> None:
        for char in value:
            size = cell_len(char)
            if 0 <= y < self.height and 0 <= x < self.width and x + size <= self.width:
                self.chars[y][x], self.styles[y][x] = char, style
                if size == 2:
                    self.chars[y][x + 1], self.styles[y][x + 1] = "", style
            x += size

    def render(self) -> Text:
        result = Text()
        for y in range(self.height):
            if y:
                result.append("\n")
            for x in range(self.width):
                result.append(self.chars[y][x], self.styles[y][x])
        return result


def graph(
    workflow: Workflow,
    states: dict[str, NodeRun],
    selected: str,
    width: int,
    frame: int,
    *,
    interactive: bool = True,
) -> Text:
    """绘制圆角字符 DAG 画布；宽屏采用分层卡片与圆角连线，窄屏平滑降级为结构化行视图。"""
    by_id = {node.id: node for node in workflow.nodes}
    layers = [[by_id[key] for key in layer] for layer in topological_layers(workflow)]
    regular = all(
        set(node.dependencies) == {parent.id for parent in layers[depth - 1]}
        for depth, layer in enumerate(layers)
        if depth
        for node in layer
    )

    def sty(node_id: str, color: str) -> str | Style:
        return node_style(node_id, color) if interactive else color

    min_card_width = 50 if len(layers) <= 3 else 62
    if width < min_card_width or len(layers) > 4 or not regular:
        result = Text()
        for layer_index, layer in enumerate(layers, start=1):
            for node in layer:
                if result:
                    result.append("\n")
                deps = "+".join(node.dependencies) or "起点"
                status = states[node.id].status if node.id in states else "waiting"
                state_badge = status_text(status, frame)
                marker = "▸ " if node.id == selected else "  "
                prefix = f"L{layer_index} {deps} ─▶ "
                worker_tag = f" @{node.worker_id}" if width >= 66 else ""
                reserved = (
                    cell_len(marker)
                    + cell_len(prefix)
                    + cell_len(node.id)
                    + 1
                    + cell_len(worker_tag)
                    + 2
                    + cell_len(state_badge.plain)
                )
                label_width = max(4, width - reserved)
                result.append(marker, BLUE if node.id == selected else MUTED)
                result.append(prefix, LINE)
                result.append(
                    f"{node.id} {fit(node.label, label_width)}",
                    sty(node.id, BLUE if node.id == selected else TEXT),
                )
                if worker_tag:
                    result.append(worker_tag, MUTED)
                result.append("  ")
                result.append_text(state_badge)
        return result

    gap = 5 if width < 66 else 7
    box_width = min(24, (width - gap * (len(layers) - 1)) // len(layers))
    rows = max(len(layer) for layer in layers)
    grid = _Grid(width, rows * 5 - 1)
    positions = {
        node.id: (column * (box_width + gap), 1 + row * 5 + (rows - len(layer)) * 2)
        for column, layer in enumerate(layers)
        for row, node in enumerate(layer)
    }

    for target in workflow.nodes:
        tx, ty = positions[target.id]
        for source_id in target.dependencies:
            sx, sy = positions[source_id]
            bus = tx - (2 if gap == 5 else 3)
            route = [(x, sy, "─") for x in range(sx + box_width, bus + 1)]
            step = 1 if ty >= sy else -1
            if ty != sy:
                route.extend((bus, y, "│") for y in range(sy + step, ty + step, step))
            route.extend((x, ty, "─") for x in range(bus + 1, tx))
            active = (
                target.id in states
                and states[target.id].status == "running"
                and states[source_id].status == "success"
            )
            done = (
                target.id in states
                and states[target.id].status == "success"
                and states[source_id].status == "success"
            )
            edge_color = BLUE if active else GREEN if done else LINE
            for index, (x, y, char) in enumerate(route):
                if active and index % 5 == frame % 5:
                    char = "›" if char == "─" else "·"
                grid.put(x, y, char, edge_color)
            grid.put(tx - 1, ty, "▸", BLUE if active else GREEN if done else ORANGE)
            if sy != ty:
                # 圆角拐点，视觉柔和不尖锐。
                grid.put(bus, sy, "╮" if step > 0 else "╯", edge_color)
                grid.put(bus, ty, "╰" if step > 0 else "╭", edge_color)

    for node in workflow.nodes:
        x, y = positions[node.id]
        state = states.get(node.id)
        status = state.status if state else "waiting"
        border_color = (
            RED
            if status in {"failed", "blocked"}
            else BLUE
            if node.id == selected or status == "running"
            else GREEN
            if status == "success"
            else LINE
        )
        style = sty(node.id, border_color)
        grid.put(x, y - 1, "╭" + "─" * (box_width - 2) + "╮", style)
        for offset in (0, 1):
            grid.put(x, y + offset, "│" + " " * (box_width - 2) + "│", style)
        grid.put(
            x + 2,
            y,
            fit(f"{node.id} {node.label}", box_width - 4),
            sty(node.id, BLUE if node.id == selected else TEXT),
        )
        if state and state.usage.total_tokens and status == "success":
            sub_plain = f"✓ {state.usage.total_tokens} tok"
            sub_color = GREEN
        elif not state:
            sub_plain = f"@{node.worker_id}"
            sub_color = MUTED
        else:
            st = status_text(status, frame)
            sub_plain = st.plain
            sub_color = str(st.style)
        grid.put(x + 2, y + 1, fit(sub_plain, box_width - 4), sty(node.id, sub_color))
        grid.put(x, y + 2, "╰" + "─" * (box_width - 2) + "╯", style)
    return grid.render()
