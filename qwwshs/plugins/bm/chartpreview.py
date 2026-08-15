"""Berry Melody 谱面预览图生成。

解析 ``chart/`` 目录下的谱面文本（AssetStudio 解包导出的 TextAsset），
把音符按 beat→时间换算后铺到分栏预览图上：

- 每 30 秒拆成一栏，从左到右排列，白色竖线分隔
- Tap 用蓝色、Drag 用黄色、Hold（滑条）用红色
- 仅用 ``#speed`` 的 BpmChange 做 beat→时间换算（忽略第三位），
  流速（BpmMove/InitialSpeed）等属性不参与
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from .render import _font

CHART_DIR = Path(__file__).resolve().parent / "chart"

# 音符类型 → 颜色（Tap 蓝 / Drag 黄 / Hold 红）
NOTE_COLORS = {
    "Tap": (64, 160, 255),
    "Drag": (255, 205, 64),
    "Hold": (255, 80, 80),
}
_NOTE_TYPES = tuple(NOTE_COLORS)
# Hold 每段点数（拍, x, y）
_NOTE_SEGMENT = 3

# 布局：每 30 秒一段，段宽 320px、高 2000px
_SEGMENT_SECONDS = 30.0
_COLUMN_WIDTH = 320
_COLUMN_HEIGHT = 2000
_SEPARATOR_WIDTH = 4
_EDGE_PAD = 24  # 图整体左右边距
_LANE_PAD = 42  # 轨道左右留白（音符 x ∈ [-1, 1] 映射到这段范围）
_TITLE_HEIGHT = 64
_BG = (22, 24, 30)
_TITLE_COLOR = (232, 233, 238)
_SUB_COLOR = (128, 132, 142)
_GUIDE_COLOR = (62, 66, 76)
# 音符条厚度（像素，垂直方向全高）
_NOTE_THICKNESS = 7

_DEFAULT_BPM = 120.0
# 结尾超出分钟分界的容差（秒），超出视为需要新一栏
_SEGMENT_EPSILON = 0.5


@dataclass(slots=True)
class Note:
    """单个音符；points 为 (时间秒, x1, 宽度) 序列（Hold 多段）。

    x1 是音符中心位置（≈ -1~1），宽度为条宽（x1 ± 宽/2 为条的两端）。
    """

    kind: str
    points: list[tuple[float, float, float]] = field(default_factory=list)


@dataclass(slots=True)
class ChartData:
    """解析后的谱面。"""

    title: str = ""
    artist: str = ""
    charter: str = ""
    level: str = ""
    notes: list[Note] = field(default_factory=list)
    duration: float = 0.0  # 秒


def parse_chart(path: Path) -> ChartData:
    """解析谱面文本文件（UTF-8 with BOM）。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    chart = ChartData()

    # 按 # 段落收集原始行（语句可能跨行，如 Drag 附带的 :{...} 块）
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            current = line[1:].strip()
            continue
        if line and current:
            sections.setdefault(current, []).append(line)

    info = _parse_info(sections.get("info", []))
    chart.title = info.get("Title") or path.stem
    chart.artist = info.get("Artist") or ""
    chart.charter = info.get("Charter") or ""
    chart.level = info.get("Level") or ""

    bpm_changes = _parse_bpm_changes(sections.get("speed", []))
    changes = sorted(dict(bpm_changes).items())
    try:
        default_bpm = float(info.get("BpmText") or _DEFAULT_BPM)
    except ValueError:
        default_bpm = _DEFAULT_BPM

    raw_notes = _parse_notes(sections.get("note", []))
    chart.notes = [
        Note(
            note.kind,
            [(_beat_to_time(t, changes, default_bpm), x, y) for t, x, y in note.points],
        )
        for note in raw_notes
    ]
    chart.duration = max(
        (point[0] for note in chart.notes for point in note.points), default=0.0
    )
    return chart


def _parse_info(lines: list[str]) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in lines:
        key, _, value = line.partition(":")
        info[key.strip()] = value.strip().rstrip(";").strip()
    return info


def _parse_bpm_changes(lines: list[str]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for stmt in "\n".join(lines).split(";"):
        match = re.match(r"BpmChange:\s*([\d.]+)\s*,\s*([\d.]+)", stmt.strip())
        if match:
            out.append((float(match.group(1)), float(match.group(2))))
    return out


def _parse_notes(lines: list[str]) -> list[Note]:
    notes: list[Note] = []
    for raw_stmt in "\n".join(lines).split(";"):
        stmt = raw_stmt.strip()
        if not stmt:
            continue
        # 去掉 Drag 等附带的 :{ ... } 属性块（跨行，含 :\n{ 形式）
        stmt = re.split(r":\s*\{", stmt, maxsplit=1)[0].strip()
        match = re.match(r"(\w+):\s*(.*)", stmt)
        if not match:
            continue
        kind, body = match.group(1), match.group(2)
        if kind not in _NOTE_TYPES:
            continue
        try:
            values = [float(part.strip()) for part in body.split(",") if part.strip()]
        except ValueError:
            continue
        if kind == "Hold":
            points = [
                (values[i], values[i + 1], values[i + 2])
                for i in range(0, len(values) - 2, _NOTE_SEGMENT)
            ]
            if points:
                notes.append(Note("Hold", points))
        elif len(values) >= _NOTE_SEGMENT:
            notes.append(Note(kind, [(values[0], values[1], values[2])]))
    return notes


def _beat_to_time(
    beat: float, changes: list[tuple[float, float]], default_bpm: float
) -> float:
    """把拍数换算为秒（按 BpmChange 分段积分）。"""
    total = 0.0
    prev_beat, prev_bpm = 0.0, default_bpm
    for beat_at, bpm in changes:
        if beat <= beat_at:
            break
        total += (beat_at - prev_beat) / prev_bpm * 60
        prev_beat, prev_bpm = beat_at, bpm
    total += (beat - prev_beat) / prev_bpm * 60
    return total


def find_chart(name: str) -> tuple[Path, str] | None:
    """按曲名查找谱面文件，返回 (路径, 难度)。大小写不敏感。"""
    diffs = ("RU", "TT", "IL", "RL", "DM", "FL")
    candidates = [f"{name} {diff}" for diff in diffs]
    candidates += [
        f"{variant} {diff}" for diff in diffs for variant in _name_variants(name)
    ]
    lowered = {candidate.lower() for candidate in candidates}
    for candidate in candidates:
        path = CHART_DIR / candidate
        if path.exists():
            return path, candidate.rsplit(" ", 1)[-1]
    if not CHART_DIR.is_dir():
        return None
    for path in CHART_DIR.iterdir():
        if path.is_file() and path.name.lower() in lowered:
            return path, path.name.rsplit(" ", 1)[-1]
    return None


def _name_variants(name: str) -> list[str]:
    """曲名归一化变体（小写、去多余空白）。"""
    text = re.sub(r"[_\u3000]+", " ", name.strip())
    text = re.sub(r"\s+", " ", text)
    return [text.lower(), text]


def render_chart_preview(chart: ChartData, display_name: str) -> bytes:
    """渲染谱面预览图，返回 PNG 字节。"""
    # 分钟段数：结尾只超出分界 _SEGMENT_EPSILON 以内的按整分钟算（避免空栏）
    whole, remainder = divmod(chart.duration, _SEGMENT_SECONDS)
    columns = max(1, int(whole) + (1 if remainder > _SEGMENT_EPSILON else 0))
    width = (
        _EDGE_PAD
        + columns * _COLUMN_WIDTH
        + (columns - 1) * _SEPARATOR_WIDTH
        + _EDGE_PAD
    )
    height = _TITLE_HEIGHT + _COLUMN_HEIGHT + 8
    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)

    _draw_title(draw, width, chart, display_name)
    _draw_separators(draw, columns, height)
    _draw_notes(draw, chart.notes, columns)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_title(
    draw: ImageDraw.ImageDraw, width: int, chart: ChartData, display_name: str
) -> None:
    font_title = _font(26, bold=True)
    font_sub = _font(16)
    draw.text((_EDGE_PAD, 14), display_name, font=font_title, fill=_TITLE_COLOR)
    meta = (
        f"{chart.charter} | Level {chart.level} | "
        f"时长 {chart.duration / 60:.1f} 分钟"
    )
    draw.text(
        (width - _EDGE_PAD, 22),
        meta,
        font=font_sub,
        fill=_SUB_COLOR,
        anchor="ra",
    )


def _draw_separators(draw: ImageDraw.ImageDraw, columns: int, height: int) -> None:
    """白色竖线分隔各分钟段，并画每段轨道边界淡线。"""
    for i in range(1, columns):
        x = _EDGE_PAD + i * (_COLUMN_WIDTH + _SEPARATOR_WIDTH) - _SEPARATOR_WIDTH
        draw.rectangle(
            (x, _TITLE_HEIGHT, x + _SEPARATOR_WIDTH, height - 8), fill=(255, 255, 255)
        )
    for i in range(columns):
        left = _EDGE_PAD + i * (_COLUMN_WIDTH + _SEPARATOR_WIDTH)
        right = left + _COLUMN_WIDTH
        draw.line((left, _TITLE_HEIGHT, left, height - 8), fill=_GUIDE_COLOR)
        draw.line((right, _TITLE_HEIGHT, right, height - 8), fill=_GUIDE_COLOR)


def _draw_notes(
    draw: ImageDraw.ImageDraw, notes: list[Note], columns: int
) -> None:
    for note in notes:
        color = NOTE_COLORS[note.kind]
        if note.kind == "Hold":
            _draw_hold(draw, note, color, columns)
        else:
            for point in note.points:
                _draw_note_bar(draw, point, color, columns)


def _draw_note_bar(
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float, float],
    color: tuple[int, int, int],
    columns: int,
) -> None:
    """Tap/Drag 横向条：x1 为中心，width 为宽度。"""
    t, x1, width = point
    col = _note_col(t, columns)
    px = _note_x(x1, col)
    half = width * (_COLUMN_WIDTH - 2 * _LANE_PAD) / 4
    py = _note_y(t, col)
    thickness = _NOTE_THICKNESS / 2
    draw.rounded_rectangle(
        (px - half, py - thickness, px + half, py + thickness),
        radius=3,
        fill=color,
    )


def _draw_hold(
    draw: ImageDraw.ImageDraw,
    note: Note,
    color: tuple[int, int, int],
    columns: int,
) -> None:
    """Hold 填充多边形：中心路径左右各扩半宽，按分钟段分组绘制。"""
    half_scale = (_COLUMN_WIDTH - 2 * _LANE_PAD) / 4
    groups: dict[int, list[tuple[float, float, float]]] = {}
    for t, x1, width in note.points:
        col = _note_col(t, columns)
        px = _note_x(x1, col)
        half = width * half_scale
        groups.setdefault(col, []).append((px, _note_y(t, col), half))
    for pts in groups.values():
        if len(pts) == 1:
            px, py, half = pts[0]
            thickness = _NOTE_THICKNESS / 2
            draw.rounded_rectangle(
                (px - half, py - thickness, px + half, py + thickness),
                radius=3,
                fill=color,
            )
            continue
        lefts = [(px - half, py) for px, py, half in pts]
        rights = [(px + half, py) for px, py, half in reversed(pts)]
        draw.polygon(lefts + rights, fill=color)


def _note_col(t: float, columns: int) -> int:
    return min(int(t // _SEGMENT_SECONDS), columns - 1)


def _note_x(x: float, col: int) -> float:
    """音符中心 x（-1~1）→ 像素。"""
    return (
        _EDGE_PAD
        + col * (_COLUMN_WIDTH + _SEPARATOR_WIDTH)
        + _LANE_PAD
        + (x + 1) / 2 * (_COLUMN_WIDTH - 2 * _LANE_PAD)
    )


def _note_y(t: float, col: int) -> float:
    """全局时间秒 → 段内 y 像素。"""
    return (
        _TITLE_HEIGHT
        + ((t - col * _SEGMENT_SECONDS) / _SEGMENT_SECONDS) * _COLUMN_HEIGHT
    )
