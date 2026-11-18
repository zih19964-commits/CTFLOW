"""FLUX 的字符画布；视图直接读取工作流和运行结果。"""

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from ctflow.graph import topological_layers
from ctflow.models import NodeRun, Workflow

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
    if status == "running":
        return Text("running" + ("." * (frame % 5 + 1)).ljust(5), BLUE)
    return Text(status, RED if status in {"failed", "blocked"} else MUTED)


def node_style(node_id: str, color: str) -> Style:
    # Rich 的点击元数据由 Textual 分发到 App，整张节点卡都可以点击。
    return Style(color=color, meta={"@click": f"app.select_node('{node_id}')"})


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
    workflow: Workflow, states: dict[str, NodeRun], selected: str, width: int, frame: int
) -> Text:
    by_id = {node.id: node for node in workflow.nodes}
    layers = [[by_id[key] for key in layer] for layer in topological_layers(workflow)]
    regular = all(
        set(node.dependencies) == {parent.id for parent in layers[depth - 1]}
        for depth, layer in enumerate(layers)
        if depth
        for node in layer
    )
    if width < 62 or len(layers) > 4 or not regular:
        result = Text()
        for node in workflow.nodes:
            if result:
                result.append("\n")
            result.append(f"{'+'.join(node.dependencies) or '任务'} ─▶ ", MUTED)
            result.append(
                f"{node.id} {fit(node.label, max(4, width - 24))}",
                node_style(node.id, BLUE if node.id == selected else TEXT),
            )
            result.append("  ")
            result.append_text(
                status_text(states[node.id].status if node.id in states else "waiting", frame)
            )
        return result

    gap = 7
    box_width = min(23, (width - gap * (len(layers) - 1)) // len(layers))
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
            bus = tx - 3
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
            for index, (x, y, char) in enumerate(route):
                if active and index % 5 == frame % 5:
                    char = "›" if char == "─" else "·"
                grid.put(x, y, char, BLUE if active else LINE)
            grid.put(tx - 1, ty, "▶", BLUE if active else ORANGE)
            if sy != ty:
                grid.put(bus, sy, "┐" if step > 0 else "┘", BLUE if active else LINE)
                grid.put(bus, ty, "└" if step > 0 else "┌", BLUE if active else LINE)
    for node in workflow.nodes:
        x, y = positions[node.id]
        status = states[node.id].status if node.id in states else "waiting"
        color = RED if status in {"failed", "blocked"} else BLUE if node.id == selected else LINE
        style = node_style(node.id, color)
        grid.put(x, y - 1, "┌" + "─" * (box_width - 2) + "┐", style)
        for offset in (0, 1):
            grid.put(x, y + offset, "│" + " " * (box_width - 2) + "│", style)
        grid.put(x + 2, y, fit(f"{node.id} {node.label}", box_width - 4), node_style(node.id, TEXT))
        state = status_text(status, frame)
        grid.put(
            x + 2, y + 1, fit(state.plain, box_width - 4), node_style(node.id, str(state.style))
        )
        grid.put(x, y + 2, "└" + "─" * (box_width - 2) + "┘", style)
    return grid.render()
