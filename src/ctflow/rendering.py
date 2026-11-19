"""CTflow 终端字符渲染：启动画面、圆角拓扑画布、状态徽标与工具调用轨迹。"""

from __future__ import annotations

import math

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from ctflow.graph import topological_layers
from ctflow.models import NodeRun, Workflow

TEXT = "#dce3f0"
USER_TEXT = "#f3cfa4"
MUTED = "#7f8a9e"
BLUE = "#7aa2f7"
GREEN = "#73daca"
ORANGE = "#ff9e64"
RED = "#f7768e"
LINE = "#343d4f"

STATUS_META: dict[str, tuple[str, str]] = {
    "waiting": ("·", MUTED),
    "ready": ("○", ORANGE),
    "running": ("●", BLUE),
    "success": ("✓", GREEN),
    "failed": ("×", RED),
    "blocked": ("-", RED),
    "stopped": ("!", ORANGE),
    "pending": ("·", MUTED),
}

_SHADOW_GLYPHS: dict[str, tuple[str, ...]] = {
    "C": (" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"),
    "T": ("████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "),
    "F": ("███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "██║     ", "╚═╝     "),
    "L": ("██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"),
    "O": (" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "),
    "W": ("██╗    ██╗", "██║    ██║", "██║ █╗ ██║", "██║███╗██║", "╚███╔███╔╝", " ╚══╝╚══╝ "),
}
_SOLID_CHARS = frozenset({"█", "▀", "▄", "▌", "▐"})
_SHADOW_CHARS = frozenset({"╗", "╔", "╝", "╚", "║", "═"})

# 品牌双色立体切面：CT 采用明快清透的暖阳琥珀橙，FLOW 采用清澈明亮的晴空冰晶蓝
SPLASH_BG = "#121620"
_CT_SOLID_STOPS: tuple[tuple[float, str], ...] = (
    (0.0, "#f58228"),
    (0.40, "#ff9a3e"),
    (0.78, "#ffb25c"),
    (1.0, "#ffca80"),
)
_CT_SHADOW_FROM = "#a8531b"
_CT_SHADOW_TO = "#f08c3e"

_FLOW_SOLID_STOPS: tuple[tuple[float, str], ...] = (
    (0.0, "#468cf7"),
    (0.38, "#60a5fa"),
    (0.78, "#84c5ff"),
    (1.0, "#b6e3ff"),
)
_FLOW_SHADOW_FROM = "#2f5fc2"
_FLOW_SHADOW_TO = "#7ab8ff"

SPLASH_FREEZE_AT = 2.45


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


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    raw = color.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _mix_hex(c1: str, c2: str, ratio: float) -> str:
    t = max(0.0, min(1.0, ratio))
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return (
        f"#{round(r1 + (r2 - r1) * t):02x}"
        f"{round(g1 + (g2 - g1) * t):02x}"
        f"{round(b1 + (b2 - b1) * t):02x}"
    )


def _over_bg(color: str, alpha: float) -> str:
    if alpha >= 1.0:
        return color
    return _mix_hex(SPLASH_BG, color, alpha)


def _multi_grad(stops: tuple[tuple[float, str], ...], ratio: float) -> str:
    t = max(0.0, min(1.0, ratio))
    for idx in range(1, len(stops)):
        t0, c0 = stops[idx - 1]
        t1, c1 = stops[idx]
        if t <= t1:
            return _mix_hex(c0, c1, (t - t0) / (t1 - t0))
    return stops[-1][1]


def _imul32(a: int, b: int) -> int:
    prod = (a * b) & 0xFFFFFFFF
    return prod - 0x100000000 if prod & 0x80000000 else prod


def _cell_hash(x: int, y: int, salt: int) -> float:
    """确定性整格哈希：构造克制的拉丝金属微晶切面，重绘不乱闪。"""
    h = _imul32(x + 17, 374761393) ^ _imul32(y + 129, 668265263) ^ _imul32(salt, 2246822519)
    h = _imul32(h ^ ((h & 0xFFFFFFFF) >> 13), 1274126177)
    return ((h ^ ((h & 0xFFFFFFFF) >> 16)) & 0xFFFFFFFF) / 4294967296.0


def _metal_facet(base: str, highlight: str, deep: str, x: int, y: int) -> str:
    r = _cell_hash(x, y, 2027)
    if r < 0.06:
        return _mix_hex(base, highlight, 0.22)
    if r < 0.13:
        return _mix_hex(base, deep, 0.14)
    return base


def _build_shadow_art(word: str) -> tuple[str, ...]:
    rows = ["" for _ in range(6)]
    for ch in word:
        glyph = _SHADOW_GLYPHS[ch]
        width = max(len(r) for r in glyph)
        for idx in range(6):
            rows[idx] += glyph[idx].ljust(width) + " "
    return tuple(r[:-1] for r in rows)


_CT_ROWS = _build_shadow_art("CT")
_FLOW_ROWS = _build_shadow_art("FLOW")
_LOGO_GAP = 2
_LOGO_ROWS = tuple(f"{_CT_ROWS[i]}{' ' * _LOGO_GAP}{_FLOW_ROWS[i]}" for i in range(6))
_LOGO_WIDTH = len(_LOGO_ROWS[0])

# 6 个字母模块在 58 列徽标画布中的区间与错峰下落时刻：(x0, x1, is_ct, t_start, t_land)
_LOGO_MODULES: tuple[tuple[int, int, bool, float, float], ...] = (
    (0, 8, True, 0.04, 0.34),
    (9, 18, True, 0.17, 0.48),
    (20, 28, False, 0.38, 0.68),
    (29, 37, False, 0.51, 0.81),
    (38, 47, False, 0.64, 0.94),
    (48, 58, False, 0.77, 1.08),
)


def _build_col_module_map() -> tuple[tuple[bool, float, float] | None, ...]:
    mapping: list[tuple[bool, float, float] | None] = [None] * _LOGO_WIDTH
    for x0, x1, is_ct, t_start, t_land in _LOGO_MODULES:
        for col in range(x0, x1):
            mapping[col] = (is_ct, t_start, t_land)
    return tuple(mapping)


_COL_MODULE = _build_col_module_map()

_SPLASH_MIN_LEFT_W = 62
_SPLASH_MIN_RIGHT_W = 24
_SPLASH_MAX_RIGHT_W = 30
_SPLASH_COL_GAP = 3


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if x <= edge0:
        return 0.0
    if x >= edge1:
        return 1.0
    u = (x - edge0) / (edge1 - edge0)
    return u * u * (3.0 - 2.0 * u)


def _splash_columns(width: int) -> tuple[int, int, bool]:
    """计算选择页横幅左右分栏宽度：宽屏采用左居中徽标 + 右快捷栏，窄屏自动回退单栏居中。"""
    safe_w = max(_LOGO_WIDTH, width)
    if safe_w < _SPLASH_MIN_LEFT_W + _SPLASH_COL_GAP + _SPLASH_MIN_RIGHT_W:
        return safe_w, 0, False
    right_w = min(_SPLASH_MAX_RIGHT_W, max(_SPLASH_MIN_RIGHT_W, round(safe_w * 0.26)))
    left_w = safe_w - _SPLASH_COL_GAP - right_w
    if left_w < _SPLASH_MIN_LEFT_W:
        left_w = _SPLASH_MIN_LEFT_W
        right_w = safe_w - _SPLASH_COL_GAP - left_w
    if right_w < _SPLASH_MIN_RIGHT_W:
        return safe_w, 0, False
    return left_w, right_w, True


def _align_line(line: Text, width: int, align: str = "left") -> Text:
    """按终端单元格宽度对齐单行 Rich Text，确保中英文混排下列宽严格恒定。"""
    if width <= 0:
        return Text()
    out = line.copy()
    cur_w = cell_len(out.plain)
    if cur_w > width:
        out.truncate(width, overflow="ellipsis")
        cur_w = cell_len(out.plain)
    pad = max(0, width - cur_w)
    if pad == 0:
        return out
    if align == "center":
        left_pad = pad // 2
        right_pad = pad - left_pad
        res = Text(" " * left_pad)
        res.append_text(out)
        res.append(" " * right_pad)
        return res
    out.append(" " * pad)
    return out


def _render_logo_row(row_idx: int, anim_time: float, animated: bool) -> Text:
    """渲染 58 列双色立体徽标的第 row_idx 行：分块滑落咬合 + 脉冲共振 + 高光定影锁定。"""
    ct_solid_rows = [_multi_grad(_CT_SOLID_STOPS, r / 5.0) for r in range(6)]
    ct_shadow_rows = [_mix_hex(_CT_SHADOW_FROM, _CT_SHADOW_TO, r / 5.0) for r in range(6)]
    fl_solid_rows = [_multi_grad(_FLOW_SOLID_STOPS, r / 5.0) for r in range(6)]
    fl_shadow_rows = [_mix_hex(_FLOW_SHADOW_FROM, _FLOW_SHADOW_TO, r / 5.0) for r in range(6)]

    rail_flash = animated and (1.12 <= anim_time < 1.22 or 1.30 <= anim_time < 1.40)
    lock_strobe = animated and (1.48 <= anim_time < 1.58 or 1.66 <= anim_time < 1.76)
    settle_glow = (
        max(0.0, 1.0 - (anim_time - 1.76) / 0.32) if animated and 1.76 <= anim_time < 2.08 else 0.0
    )

    row_text = Text()
    for lx in range(_LOGO_WIDTH):
        mod = _COL_MODULE[lx]
        if mod is None:
            row_text.append(" ")
            continue
        is_ct, t_start, t_land = mod
        if animated:
            if anim_time < t_start:
                row_text.append(" ")
                continue
            if anim_time < t_land:
                p = (anim_time - t_start) / (t_land - t_start)
                eased = 1.0 - (1.0 - p) ** 2
                shift = max(1, round((1.0 - eased) * 6))
                falling = True
                land_pulse = 0.0
            else:
                shift = 0
                falling = False
                land_pulse = max(0.0, 1.0 - (anim_time - t_land) / 0.16)
        else:
            shift = 0
            falling = False
            land_pulse = 0.0

        src_row = row_idx + shift
        if src_row < 0 or src_row >= 6:
            row_text.append(" ")
            continue
        ch = _LOGO_ROWS[src_row][lx]
        if ch == " ":
            row_text.append(" ")
            continue

        is_solid = ch in _SOLID_CHARS
        if is_ct:
            base = (
                _metal_facet(ct_solid_rows[src_row], "#ffe6bf", "#df6d1e", lx, src_row)
                if is_solid
                else ct_shadow_rows[src_row]
            )
            if falling:
                lead = "#fff2d9" if src_row == 5 else "#ffc278"
                base = _mix_hex(base, lead, 0.58 if is_solid else 0.38)
            elif land_pulse > 0.0:
                base = _mix_hex(base, "#ffe8bf", land_pulse * 0.50)
            if rail_flash:
                base = _mix_hex(base, "#ffe4b5", 0.30)
            if lock_strobe:
                base = _mix_hex(base, "#fff3dc", 0.65 if is_solid else 0.42)
            elif settle_glow > 0.0:
                base = _mix_hex(base, "#ffe8bf", settle_glow * 0.35)
        else:
            base = (
                _metal_facet(fl_solid_rows[src_row], "#e0f4ff", "#3575e8", lx, src_row)
                if is_solid
                else fl_shadow_rows[src_row]
            )
            if falling:
                lead = "#f0f9ff" if src_row == 5 else "#93d5ff"
                base = _mix_hex(base, lead, 0.58 if is_solid else 0.38)
            elif land_pulse > 0.0:
                base = _mix_hex(base, "#d8f0ff", land_pulse * 0.50)
            if rail_flash:
                base = _mix_hex(base, "#d8f0ff", 0.30)
            if lock_strobe:
                base = _mix_hex(base, "#eef8ff", 0.65 if is_solid else 0.42)
            elif settle_glow > 0.0:
                base = _mix_hex(base, "#d8f0ff", settle_glow * 0.35)

        row_text.append(ch, base)
    return row_text


def _render_energy_rail(anim_time: float, animated: bool) -> Text:
    """渲染徽标正下方的双色承接能量轨：随模块下落蓄力 -> 双闪共振 -> 收束为对称双色短轨。"""
    if not animated or anim_time >= 1.98:
        badge = Text()
        badge.append("━━━━", "#ff9a3e")
        badge.append(" · ", MUTED)
        badge.append("━━━━", "#60a5fa")
        return badge

    rail_flash = 1.12 <= anim_time < 1.22 or 1.30 <= anim_time < 1.40
    if anim_time < 1.42:
        ct_len = len(_CT_ROWS[0])
        fl_start = ct_len + _LOGO_GAP
        fl_len = len(_FLOW_ROWS[0])
        ct_fill = round(_smoothstep(0.04, 0.48, anim_time) * ct_len)
        fl_fill = round(_smoothstep(0.38, 1.08, anim_time) * fl_len)
        rail = Text()
        for lx in range(_LOGO_WIDTH):
            if rail_flash:
                rail.append("━", "#ffe8b8" if lx < fl_start else "#dff4ff")
            elif lx < ct_fill:
                rail.append("━", "#ff9a3e")
            elif fl_start <= lx < fl_start + fl_fill:
                rail.append("━", "#60a5fa")
            else:
                rail.append(" ")
        return rail

    shrink = _smoothstep(1.42, 1.98, anim_time)
    wing = round(26 + (4 - 26) * shrink)
    rail = Text()
    rail.append("━" * wing, "#ff9a3e")
    rail.append(" · ", _over_bg(MUTED, shrink))
    rail.append("━" * wing, "#60a5fa")
    return rail


def _render_tip_row(row_idx: int, right_w: int, alpha: float) -> Text:
    """渲染右栏快捷引导行（借鉴现代 TUI Header 的 Welcome + Commands 速查结构）。"""
    if alpha <= 0.02:
        return Text()
    line = Text()
    if row_idx == 0:
        line.append("快速开始", f"bold {_over_bg('#ff9e64', alpha)}")
    elif row_idx == 1:
        line.append("双击或 → 打开所选条目", _over_bg(MUTED, alpha))
    elif row_idx == 2:
        line.append("─" * min(right_w, 24), _over_bg("#354b73", alpha))
    elif row_idx == 3:
        line.append("常用命令", f"bold {_over_bg('#60a5fa', alpha)}")
    else:
        tips = (
            ("/flow new", "新建工作流"),
            ("/worker  ", "配置 Worker"),
            ("/chat    ", "单 Worker 对话"),
            ("/mcp     ", "查看 MCP 工具"),
            ("1 / 2 / 3", "切换列表分类"),
        )
        cmd, desc = tips[row_idx - 4]
        line.append(cmd, _over_bg(TEXT, alpha))
        line.append(f"  {desc}", _over_bg(MUTED, alpha))
    return line


def render_splash(
    worker_count: int,
    workflow_count: int,
    run_count: int,
    *,
    compact: bool = False,
    anim_time: float = math.inf,
    width: int = 108,
) -> Text:
    """渲染 CTflow 双色立体徽标与仪表盘式双栏开场横幅：
    左栏水平居中（橙 CT + 蓝 FLOW 分块下落咬合、能量轨双闪收束、主副标语），右栏快捷命令速查。
    """
    stats = f"{workflow_count} 工作流  ·  {worker_count} Workers  ·  {run_count} 历史运行"
    if compact:
        text = Text()
        text.append("CT", "bold #ff9e64")
        text.append("FLOW", "bold #60a5fa")
        text.append("  多 Worker 依赖图与独立对话终端", TEXT)
        text.append(f"   {stats}", MUTED)
        return text

    animated = math.isfinite(anim_time)
    left_w, right_w, use_tips = _splash_columns(width)

    left_rows: list[Text] = [_render_logo_row(r, anim_time, animated) for r in range(6)]
    left_rows.append(_render_energy_rail(anim_time, animated))

    sub_alpha = _smoothstep(1.32, 1.92, anim_time) if animated else 1.0
    tagline = Text()
    if sub_alpha > 0.02:
        tagline.append("多 Worker 依赖图与独立对话终端", f"bold {_over_bg(TEXT, sub_alpha)}")
    left_rows.append(tagline)

    stat_alpha = _smoothstep(1.50, 2.12, anim_time) if animated else 1.0
    stat_line = Text()
    if stat_alpha > 0.02:
        stat_line.append("● ", _over_bg("#ff9e64", stat_alpha))
        stat_line.append(f"{workflow_count} 工作流", _over_bg(MUTED, stat_alpha))
        stat_line.append("   ·   ", _over_bg(LINE, stat_alpha))
        stat_line.append("● ", _over_bg("#60a5fa", stat_alpha))
        stat_line.append(f"{worker_count} Workers", _over_bg(MUTED, stat_alpha))
        stat_line.append("   ·   ", _over_bg(LINE, stat_alpha))
        stat_line.append(f"{run_count} 历史运行", _over_bg(MUTED, stat_alpha))
    left_rows.append(stat_line)

    result = Text()
    for idx, l_row in enumerate(left_rows):
        aligned_left = _align_line(l_row, left_w, align="center")
        result.append_text(aligned_left)
        if use_tips:
            sep_alpha = (
                _smoothstep(0.95 + idx * 0.05, 1.45 + idx * 0.05, anim_time) if animated else 1.0
            )
            tip_alpha = (
                _smoothstep(1.18 + idx * 0.06, 1.72 + idx * 0.06, anim_time) if animated else 1.0
            )
            result.append(" ")
            if sep_alpha > 0.02:
                result.append("│", _over_bg("#2d3a52", sep_alpha))
            else:
                result.append(" ")
            result.append(" ")
            tip_row = _render_tip_row(idx, right_w, tip_alpha)
            result.append_text(_align_line(tip_row, right_w, align="left"))
        if idx < len(left_rows) - 1:
            result.append("\n")
    return result


def format_tool_steps_markdown(steps: list[dict]) -> str:
    """将工具调用轨迹格式化为简洁可读的 Markdown 段落。"""
    if not steps:
        return ""
    blocks: list[str] = []
    for step in steps:
        status = step.get("status", "running")
        icon = {"ok": "✓", "error": "×", "running": "●"}.get(status, "·")
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
                prefix = f"L{layer_index} {deps} ─→ "
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
