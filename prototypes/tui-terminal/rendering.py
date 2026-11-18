"""工作台的只读字符视图，所有视图读取同一份运行记录。"""

from __future__ import annotations

from model import Demo, Node
from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

TEXT = "#e4e7ec"
MUTED = "#858d9a"
BLUE = "#75a7ff"
ORANGE = "#fab283"
RED = "#f28b91"
LINE = "#414b5c"


def fit(value: str, width: int) -> str:
    text = Text(value)
    text.truncate(max(0, width), overflow="ellipsis")
    return text.plain


def status_text(status: str, frame: int = 0) -> Text:
    """点动画只属于 running 行，固定宽度避免界面跳动。"""
    if status == "running":
        return Text("running" + ("." * (frame % 5 + 1)).ljust(5), BLUE)
    return Text(status, RED if status == "defeat" else MUTED)


def node_time(node: Node, elapsed: float) -> float:
    if node.started is None:
        return 0
    return max(0, (node.ended if node.ended is not None else elapsed) - node.started)


def node_card(demo: Demo, node: Node, width: int, frame: int) -> Text:
    worker = demo.workers[node.worker]
    result = Text(f"{node.id}  {fit(node.name, max(4, width - len(worker.key) - 6))} ", TEXT)
    result.append(f"@{worker.key}", BLUE if node.id == demo.selected_id else MUTED)
    result.append("\n   ")
    result.append_text(status_text(node.status, frame))
    result.append(f" {node.tokens:,}t {node_time(node, demo.elapsed):.0f}s", MUTED)
    return result


def _layers(demo: Demo) -> list[list[Node]]:
    levels: dict[str, int] = {}
    pending = list(demo.nodes)
    while pending:
        ready = [node for node in pending if all(key in levels for key in node.deps)]
        for node in ready:
            levels[node.id] = max((levels[key] + 1 for key in node.deps), default=0)
            pending.remove(node)
    return [
        [node for node in demo.nodes if levels[node.id] == depth]
        for depth in range(max(levels.values(), default=0) + 1)
    ]


class _Grid:
    """按终端单元格放置字符，中文的第二个字符格显式留空。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.chars = [[" " for _ in range(width)] for _ in range(height)]
        self.styles = [[MUTED for _ in range(width)] for _ in range(height)]

    def put(self, x: int, y: int, value: str, style: str = MUTED) -> None:
        for char in value:
            size = cell_len(char)
            if 0 <= y < self.height and 0 <= x < self.width and x + size <= self.width:
                self.chars[y][x] = char
                self.styles[y][x] = style
                if size == 2:
                    self.chars[y][x + 1] = ""
                    self.styles[y][x + 1] = style
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
    demo: Demo, width: int, frame: int = 0, *, animated: bool = False, compact: bool = False
) -> Text:
    layers = _layers(demo)
    regular = all(
        set(node.deps) == {parent.id for parent in layers[depth - 1]}
        for depth, layer in enumerate(layers)
        if depth
        for node in layer
    )
    if not regular:
        # 非整层汇合图逐条显示真实入边，避免共享总线暗示不存在的依赖。
        result = Text()
        for index, node in enumerate(demo.nodes):
            if index:
                result.append("\n")
            result.append(f"{'+'.join(node.deps) or '任务'} ", MUTED)
            active = animated and node.status == "running"
            route = "".join("›" if active and slot == frame % 4 else "─" for slot in range(4))
            result.append(route + "▶ ", BLUE if active else LINE)
            result.append(f"{node.id} {node.name}", BLUE if node.id == demo.selected_id else TEXT)
        return result
    if compact or width < 62 or len(layers) > 4:
        result = Text()
        for index, layer in enumerate(layers):
            if index:
                active = animated and any(node.status == "running" for node in layer)
                if animated:
                    particle = "•" if active and frame % 2 == 0 else "│"
                    arrow = "▼" if active and frame % 2 else "↓"
                    result.append(
                        f"\n        {particle}\n        {arrow}\n", BLUE if active else LINE
                    )
                else:
                    result.append("\n        ↓  全部上游完成后交接\n", LINE)
            for offset, node in enumerate(layer):
                if offset:
                    result.append("   ∥   ", LINE)
                result.append(
                    f"[{node.id} {fit(node.name, max(4, width // len(layer) - 10))}]",
                    BLUE if node.id == demo.selected_id else TEXT,
                )
        return result
    count = len(layers)
    gap = 7
    box_width = min(23, (width - gap * (count - 1)) // count)
    row_step = 5 if animated else 4
    height = max(len(layer) for layer in layers) * row_step - 1
    grid = _Grid(width, height)
    positions: dict[str, tuple[int, int]] = {}
    by_id = {node.id: node for node in demo.nodes}
    for column, layer in enumerate(layers):
        x = column * (box_width + gap)
        for row, node in enumerate(layer):
            y = 1 + row * row_step + (max(len(part) for part in layers) - len(layer)) * 2
            positions[node.id] = (x, y)
    # 入边仅在目标节点 running 时流动，箭头始终保留，表达输入交接方向。
    for target in demo.nodes:
        tx, ty = positions[target.id]
        for source_id in target.deps:
            sx, sy = positions[source_id]
            start = sx + box_width
            bus = tx - 3
            route: list[tuple[int, int, str]] = []
            for x in range(start, bus + 1):
                route.append((x, sy, "─"))
            step = 1 if ty >= sy else -1
            if ty != sy:
                for y in range(sy + step, ty + step, step):
                    route.append((bus, y, "│"))
            for x in range(bus + 1, tx):
                route.append((x, ty, "─"))
            active = (
                animated and target.status == "running" and by_id[source_id].status == "completed"
            )
            for index, (x, y, char) in enumerate(route):
                if active and index % 5 == frame % 5:
                    char = "›" if char == "─" else "·"
                grid.put(x, y, char, BLUE if active else LINE)
            grid.put(tx - 1, ty, "▶", BLUE if active else ORANGE)
            if sy != ty:
                grid.put(bus, sy, "┐" if step > 0 else "┘", BLUE if active else LINE)
                grid.put(bus, ty, "└" if step > 0 else "┌", BLUE if active else LINE)
    for node in demo.nodes:
        x, y = positions[node.id]
        color = BLUE if node.id == demo.selected_id else LINE
        if node.status == "defeat":
            color = RED
        grid.put(x, y - 1, "┌" + "─" * (box_width - 2) + "┐", color)
        grid.put(x, y, "│" + " " * (box_width - 2) + "│", color)
        grid.put(x + 2, y, fit(f"{node.id} {node.name}", box_width - 4), TEXT)
        if animated:
            grid.put(x, y + 1, "│" + " " * (box_width - 2) + "│", color)
            state = status_text(node.status, frame)
            grid.put(x + 2, y + 1, fit(state.plain, box_width - 4), str(state.style))
        grid.put(x, y + (2 if animated else 1), "└" + "─" * (box_width - 2) + "┘", color)
    return grid.render()


def timeline(demo: Demo, width: int, frame: int = 0) -> Text:
    """固定刻度使已结束区段保持原位，持续动画仅来自状态文字。"""
    result = Text("并行交接  →  时间向右\n\n", MUTED)
    chart_width = max(7, width - 36)
    seconds_per_cell = max(0.6, sum(node.duration for node in demo.nodes) / chart_width)
    for node in demo.nodes:
        result.append(f"{node.id} {fit(node.name, 10):<8} ", TEXT)
        if node.started is None:
            result.append("·" * chart_width, LINE)
        else:
            start = min(chart_width - 1, int(node.started / seconds_per_cell))
            span = (
                3
                if node.ended is None
                else max(1, int((node.ended - node.started) / seconds_per_cell))
            )
            bar = " " * start + "━" * min(span, chart_width - start)
            result.append(bar.ljust(chart_width), LINE if node.status != "running" else BLUE)
        result.append("  ")
        result.append_text(status_text(node.status, frame))
        result.append("\n")
    return result


def map_content(demo: Demo, width: int, frame: int, design: str, view: str) -> RenderableType:
    if view == "run" and design != "flux":
        return timeline(demo, width, frame)
    compact = design in {"ribbon", "folio"} or width < 62
    return graph(demo, width, frame, animated=design == "flux", compact=compact)


def reading(demo: Demo, view: str, *, focused: bool) -> RenderableType:
    if view == "result":
        return Markdown(
            demo.artifact
            or "# 成稿\n\n运行工作流后，在这里阅读最终报告。\n\n输入 `/export` 导出 Markdown。",
            style=TEXT,
        )
    if focused or view == "run":
        node = demo.selected
        worker = demo.workers[node.worker]
        details = Text(f"{node.id}  {node.name}    @{node.worker} · {worker.model}\n", TEXT)
        details.append(f"上游：{', '.join(node.deps) or '工作流目标'}\n", MUTED)
        if node.error:
            details.append(f"\n{node.error}\n", RED)
        if node.output:
            body = node.output
        elif node.status == "running":
            body = f"## {worker.name}\n\n正在执行：{node.task or demo.task}\n\n{worker.summary}。"
        else:
            body = "等待上游交接。" if node.deps else "已装配 Worker，输入 `/run` 开始。"
        return Group(details, Markdown(body, style=TEXT))
    parts: list[RenderableType] = []
    for role, message in demo.messages[-14:]:
        label = "你" if role == "user" else "ctflow"
        parts.extend(
            (
                Text(label, ORANGE if role == "user" else BLUE),
                Markdown(message, style=TEXT),
                Text(""),
            )
        )
    return Group(*parts)


def assembly_hint(demo: Demo) -> Text:
    node = demo.selected
    worker = demo.workers[node.worker]
    text = Text(f"{node.id}  @{worker.key}\n", BLUE)
    text.append(f"{worker.model}\n{worker.summary}\n\n", MUTED)
    text.append("点击节点查看输出" if demo.busy else "点击 / Enter 装配\n/node 查看可用操作", MUTED)
    return text
