"""Berry Melody 谱面预览图生成。

解析 ``chart/`` 目录下的谱面文本（AssetStudio 解包导出的 TextAsset），
把音符按 beat→时间换算后铺到分栏预览图上：

- 每 30 秒拆成一栏，从左到右排列，白色竖线分隔
- Tap/Drag/Hold 用 note/ 素材渲染（Drag 染黄，Hold 素材主色连续粗线；
  素材缺失回退蓝/黄/红）
- 仅用 ``#speed`` 的 BpmChange 做 beat→时间换算（忽略第三位），
  流速（BpmMove/InitialSpeed）等属性不参与
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from .constants import get_song_constants
from .render import _font
from .song import search_songs

CHART_DIR = Path(__file__).resolve().parent / "chart"

# 谱面难度优先级（缺省按定数最高选，无定数信息时按此顺序兜底）
_CHART_DIFFS = ("RU", "TT", "IL", "RL", "DM", "FL")

NOTE_DIR = Path(__file__).resolve().parent / "note"

# 音符皮肤：皮肤名 → {音符类型: 素材文件名}（类型缺失回退 Tech 对应素材）。
# 素材映射来自拆包材质（Material/*.json 的 _MainTex 引用）与 Sprite 数据。
# Tech 即游戏里的 White 皮肤（NoteSkin 字段写 Tech 的谱面用同一套素材）。
SKIN_SETS: dict[str, dict[str, str]] = {
    "Tech": {
        "Tap": "White_Tap.png",
        "Drag": "White_Drag.png",
        "Hold": "White_Hold.png",
    },
    "Luminous": {
        "Tap": "LuminousTap.png",
        "Drag": "LuminousDrag.png",
        "Hold": "LuminousHold.png",
    },
    "Berry": {
        "Tap": "Berry_Tap.png",
        "Drag": "Berry_Drag.png",
        "Hold": "Berry_Hold.png",
    },
    "Dynamix": {
        "Tap": "Dynamix_Tap.png",
        "Drag": "Dynamix_Drag.png",
        "Hold": "Dynamix_Hold.png",
        # Drag 中央装饰（不拉伸，保持比例）
        "dy_mid": "dy_mid.png",
    },
    "Dr3": {
        "Tap": "Dr3_Tap.png",
        "Drag": "Dr3_Tap.png",
        "Hold": "Dr3_Hold.png",
    },
    # 以下皮肤按材质 _MainTex 的 PathID 从 data.unity3d 提取的真实纹理
    # （同名纹理每个皮肤不同图集，如 "drag" 有 258x29/560x44/1780x304/1041x127 四版）
    "Phigros": {
        # Tap 是真实贴图（561x179，蓝核心+白边，与 flick_bg 同结构），非染色生成
        "Tap": "Phi_Tap.png",
        "Drag": "Phi_Drag.png",
        "Hold": "Phi_Hold.png",
        # Flick：flick_bg 为拉伸部分（Sprite border 279/279），flick 箭头居中不拉伸
        "FlickBg": "flick_bg.png",
        "FlickArrow": "flick.png",
    },
    "Lanota": {
        "Tap": "Berry_Tap.png",
        "Drag": "Lanota_Drag.png",
        "Hold": "Lanota_Hold.png",
        # Flick：LanotaFlickDefault/Full 材质指向 flick2（1780x304，border 216/215）
        "Flick": "flick2.png",
    },
    "qqx": {
        "Tap": "Berry_Tap.png",
        "Drag": "Berry_Drag.png",
        "Hold": "qqx_Hold.png",
    },
    "Ryceam": {
        "Tap": "Berry_Tap.png",
        "Drag": "Ryceam_Drag.png",
        "Hold": "Ryceam_Hold.png",
    },
    "Evo": {
        "Tap": "Berry_Tap.png",
        "Drag": "Evo_Drag.png",
        "Hold": "Evo_Hold.png",
    },
    "Red": {
        "Tap": "Red_Tap.png",
        "Drag": "Red_Drag.png",
        "Hold": "Red_Hold.png",
    },
}
DEFAULT_SKIN = "Tech"

# 音符类型 → 颜色（素材缺失时的回退色：Tap 蓝 / Drag 黄 / Hold 红）
NOTE_COLORS = {
    "Tap": (64, 160, 255),
    "Drag": (255, 205, 64),
    "Hold": (255, 80, 80),
}
_NOTE_TYPES = tuple(NOTE_COLORS)

# Hold 每段点数（拍, x, y）
_NOTE_SEGMENT = 3
# 黑线：深色背景上用白色细线表示（x 范围 [-3, 3]，超出轨道部分裁掉）
_BLACK_LINE_WIDTH = 2
# 黑线透明度（0-255，128 = 50%）
_BLACK_LINE_ALPHA = 128
# 折线至少需要的点数
_MIN_POLYLINE_POINTS = 2
# 公式块采样点数量上下限与缺省值
_MIN_SAMPLES = 4
_MAX_SAMPLES = 400
_DEFAULT_SAMPLES = 100

# 布局：每 15 秒一段，段宽 320px、高 2667px
_SEGMENT_SECONDS = 15.0
_COLUMN_WIDTH = 320
_COLUMN_HEIGHT = round(2000 * 4 / 3)
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
# 音符素材/段长小于该像素数时不绘制
_MIN_NOTE_PIXELS = 2
# 素材主色统计时视为不透明的 alpha 下限
_ALPHA_THRESHOLD = 100
# 提取 Lanota_Drag 蓝色核心的阈值（b 下限 / 最小绿色分量）
_BLUE_THRESHOLD = 150
_BLUE_MIN_G = 80
# Tap/Drag 素材渲染的固定高度（像素，游戏里高度不随宽度变化）
_NOTE_FIXED_HEIGHT = round(8 * 4 / 3)  # 8px 放大 1/3
# Dynamix Drag：中央装饰 dy_mid 不拉伸保持比例，细轨高度 = dy_mid 高度的 11/69
_DYNAMIX_RAIL_RATIO = 11 / 69
# Slide（Hold）透明度（0-255，128 = 50%）
_HOLD_ALPHA = round(255 * 0.5)
_ALPHA_MAX = 255
# 透视矩阵求解时的奇异阈值
_MATRIX_EPSILON = 1e-12
# 纹理段长度阈值（像素）：小于该值视为退化段跳过
_ZERO_LENGTH = 0.5

_DEFAULT_BPM = 120.0
# 结尾超出分钟分界的容差（秒），超出视为需要新一栏
_SEGMENT_EPSILON = 0.5


@dataclass(slots=True)
class Note:
    """单个音符；points 为 (时间秒, x1, 宽度) 序列（Hold 多段）。

    x1 是音符中心位置（≈ -1~1），宽度为条宽（x1 ± 宽/2 为条的两端）。
    flick 标记 Drag 是否带 ``$ Flick = True`` 属性（渲染为 Flick 音符）。
    """

    kind: str
    points: list[tuple[float, float, float]] = field(default_factory=list)
    flick: bool = False


@dataclass(slots=True)
class ChartData:
    """解析后的谱面。"""

    title: str = ""
    artist: str = ""
    charter: str = ""
    level: str = ""
    dlevel: str = ""  # 定数（chart/Info 对照表中的 DLevel）
    note_skin: str = ""  # 谱面指定皮肤（#info 的 NoteSkin 字段）
    notes: list[Note] = field(default_factory=list)
    # 黑线（#anim 的 BlackLine）：每条为 (时间秒, x) 折线
    black_lines: list[list[tuple[float, float]]] = field(default_factory=list)
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
    chart.note_skin = _find_note_skin(sections.get("anim", [])) or _parse_note_skin(
        info.get("NoteSkin") or ""
    )

    # 曲目↔谱面对照（chart/Info）：补定数与规范谱师
    name_parts = path.name.rsplit(" ", 1)
    stem = name_parts[0]
    diff = name_parts[1].removesuffix(".txt") if len(name_parts) > 1 else ""
    meta = load_song_info().get((stem, diff), {})
    chart.dlevel = meta.get("DLevel") or ""
    if meta.get("Charter"):
        chart.charter = meta["Charter"]

    changes_raw, rates, moves, offsets4, stops = _parse_speed_section(
        sections.get("speed", [])
    )
    # 过滤 0/负 BPM（防除零），同拍去重保留最后一条
    changes = sorted(
        (beat, bpm) for beat, bpm in dict(changes_raw).items() if bpm > 0
    )
    try:
        default_bpm = float(info.get("BpmText") or _DEFAULT_BPM)
    except ValueError:
        default_bpm = _DEFAULT_BPM
    # pos 空间分段点（黑线早期谱无 para 标记，坐标为 pos；pos 含速度积分与拍数偏移）
    pos_breaks = _build_pos_breaks(rates, moves, offsets4, stops)

    raw_notes = _parse_notes(sections.get("note", []))
    chart.notes = [
        Note(
            note.kind,
            [(_beat_to_time(t, changes, default_bpm), x, y) for t, x, y in note.points],
            note.flick,
        )
        for note in raw_notes
    ]
    chart.black_lines = _parse_black_lines(
        sections.get("anim", []), changes, default_bpm, pos_breaks
    )
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


def _parse_note_skin(value: str) -> str:
    """解析 NoteSkin 值（"0,Berry" → "Berry"；本地化块等无效值返回空）。"""
    text = value.strip()
    if not text or text.startswith("{"):
        return ""
    return text.split(",")[-1].strip()


def _find_note_skin(lines: list[str]) -> str:
    """在 #anim 段里找 ``NoteSkin: <序号>,<皮肤名>`` 行。"""
    for line in lines:
        if line.startswith("NoteSkin"):
            _, _, value = line.partition(":")
            return _parse_note_skin(value.strip().rstrip(";").strip())
    return ""


def _parse_speed_section(
    lines: list[str],
) -> tuple[
    list[tuple[float, float]],
    list[tuple[float, float]],
    list[tuple[float, float]],
    list[tuple[float, float]],
    list[float],
]:
    """解析 #speed 段。

    返回 (changes, rates, moves, offsets4, stops)：
    - changes：BpmChange(拍, bpm)
    - rates：BpmChange 第三位(拍, 速度)
    - moves：BpmMove(拍, 拍数偏移)
    - offsets4：BpmChange 第四位(拍, 拍数偏移)
    - stops：BpmStop 拍数列表
    """
    changes: list[tuple[float, float]] = []
    rates: list[tuple[float, float]] = []
    moves: list[tuple[float, float]] = []
    offsets4: list[tuple[float, float]] = []
    stops: list[float] = []
    for raw_stmt in "\n".join(lines).split(";"):
        stmt = raw_stmt.strip()
        match = re.match(
            r"BpmChange[=:]\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*(-?[\d.]+))?"
            r"\s*(?:,\s*(-?[\d.]+))?",
            stmt,
        )
        if match:
            beat = float(match.group(1))
            changes.append((beat, float(match.group(2))))
            rates.append((beat, float(match.group(3) or 1.0)))
            if match.group(4):
                offsets4.append((beat, float(match.group(4))))
            continue
        match = re.match(r"BpmMove:\s*([\d.]+)\s*,\s*(-?[\d.]+)", stmt)
        if match:
            moves.append((float(match.group(1)), float(match.group(2))))
            continue
        match = re.match(r"BpmStop:\s*([\d.]+)", stmt)
        if match:
            stops.append(float(match.group(1)))
    return changes, rates, moves, offsets4, stops


# pos 空间基础速度（无 BpmChange 速度时按 1，pos 与拍数一致）
_POS_BASE_SPEED = 1.0


def _build_pos_breaks(
    rates: list[tuple[float, float]],
    moves: list[tuple[float, float]],
    offsets4: list[tuple[float, float]],
    stops: list[float],
) -> list[tuple[float, float]]:
    """构建 pos(beat) 分段点 (beat, pos)。

    pos = ∫ BpmChange速度 db + Σ(BpmMove第二值/BpmChange第四值 拍数偏移)；
    BpmStop 段 pos 冻结（速度置 0）。
    """
    events: list[tuple[float, int, float]] = []
    for beat, rate in rates:
        events.append((beat, 1, rate))
    for beat, offset in moves:
        events.append((beat, 0, offset))
    for beat, offset in offsets4:
        events.append((beat, 0, offset))
    events.extend((beat, 2, 0.0) for beat in stops)
    events.sort(key=lambda e: (e[0], e[1]))
    breaks: list[tuple[float, float]] = [(0.0, 0.0)]
    prev_beat, rate = 0.0, _POS_BASE_SPEED
    for beat, kind, value in events:
        pos = breaks[-1][1] + (beat - prev_beat) * rate
        prev_beat = beat
        if kind == 0:
            pos += value  # 拍数偏移直接加到 pos
        elif kind == 1:
            rate = value
        else:
            rate = 0.0  # BpmStop：pos 冻结
        breaks.append((beat, pos))
    return breaks


def _pos_to_beat(pos: float, breaks: list[tuple[float, float]]) -> float:
    """pos → beat（分段线性逆函数，支持负速度段）。"""
    if not breaks:
        return pos
    for i in range(len(breaks) - 1):
        b0, p0 = breaks[i]
        b1, p1 = breaks[i + 1]
        low, high = (p0, p1) if p0 <= p1 else (p1, p0)
        if p1 != p0 and low <= pos <= high:
            return b0 + (b1 - b0) * (pos - p0) / (p1 - p0)
    # 超出末尾：按最后一段速度外推
    if len(breaks) >= _MIN_POLYLINE_POINTS:
        b0, p0 = breaks[-2]
        b1, p1 = breaks[-1]
        speed = (p1 - p0) / (b1 - b0) if b1 != b0 else _POS_BASE_SPEED
        if speed:
            return b1 + (pos - p1) / speed
    return breaks[-1][0] + pos - breaks[-1][1]


def _parse_notes(lines: list[str]) -> list[Note]:
    notes: list[Note] = []
    for raw_stmt in "\n".join(lines).split(";"):
        stmt = raw_stmt.strip()
        if not stmt:
            continue
        # 提取 Drag 附带的 :{ ... } 属性块（跨行），块内 $ Flick = True 标记 Flick
        flick = False
        block_start = re.search(r":\s*\{", stmt)
        if block_start:
            flick = "Flick = True" in stmt[block_start.end() :]
            stmt = stmt[: block_start.start()].strip()
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
            notes.append(Note(kind, [(values[0], values[1], values[2])], flick))
    return notes


def _parse_black_lines(
    lines: list[str],
    bpm_changes: list[tuple[float, float]],
    default_bpm: float,
    pos_breaks: list[tuple[float, float]],
) -> list[list[tuple[float, float]]]:
    """解析 #anim 里的 BlackLine：返回 (时间秒, x) 折线列表。

    简单形式 ``BlackLine: 拍, x, ...;`` 直接取点对；
    块形式 ``BlackLine::[ $... ];`` 按公式求值采样（Freq 个点）。
    带 ``IsParaLine`` 标记的为 para 空间（坐标即拍数）；
    早期谱无标记的为 pos 空间（坐标为速度/倍率积分后的 pos，取逆函数换算）。
    """
    out: list[list[tuple[float, float]]] = []
    for raw_stmt in "\n".join(lines).split(";"):
        stmt = raw_stmt.strip()
        if not stmt.startswith("BlackLine"):
            continue
        is_para = "IsParaLine" in stmt
        if stmt.startswith("BlackLine::["):
            curve = _eval_black_line_block(
                stmt, bpm_changes, default_bpm, pos_breaks, is_para=is_para
            )
            if curve:
                out.append(curve)
            continue
        match = re.match(
            r"BlackLine:\s*([\d.]+(?:[eE][+-]?\d+)?)\s*,\s*"
            r"(-?[\d.]+(?:[eE][+-]?\d+)?)(.*)",
            stmt,
        )
        if not match:
            continue
        # 简单线后跟 :[ 公式块 = 该线的动画版本：用公式曲线代替静态折线
        block_start = re.search(r":\s*\[", stmt)
        if block_start:
            curve = _eval_black_line_block(
                "BlackLine::[" + stmt[block_start.end() :],
                bpm_changes,
                default_bpm,
                pos_breaks,
                is_para=is_para,
            )
            if curve and "Move_Y" in stmt[block_start.end() :]:
                out.append(curve)
                continue
        # 值列表到第一个冒号为止（可能跟 :{...} / :[...] / 孤立冒号）
        body = re.split(r":", match.group(3), maxsplit=1)[0]
        points = [(float(match.group(1)), float(match.group(2)))]
        rest = [float(v) for v in body.split(",") if v.strip()]
        points.extend((rest[i], rest[i + 1]) for i in range(0, len(rest) - 1, 2))
        converted: list[tuple[float, float]] = []
        for point in points:
            beat, x = point
            if not is_para:
                beat = _pos_to_beat(beat, pos_breaks)
            converted.append((_beat_to_time(beat, bpm_changes, default_bpm), x))
        out.append(converted)
    return out


# ---------- BlackLine 块公式求值 ----------


class _FormulaError(ValueError):
    """公式语法或求值错误。"""


def _bezier_value(points: list[float], value: float) -> float:
    """贝塞尔曲线求值（Bernstein 递推）。"""
    work = list(points)
    for level in range(len(points) - 1, 0, -1):
        for i in range(level):
            work[i] = work[i] * (1 - value) + work[i + 1] * value
    return work[0]


def _c_bezier_value(points: list[float]) -> float:
    """c_bezier：控制点平滑贝塞尔（末两参数为 delta、blur）。

    与游戏 Func_Base.c_bezier 逐行一致：把 delta 所在的段两端控制点
    按相邻段长度比例向中点偏移 blur 倍，再取 3/4 点贝塞尔。
    """
    c = list(points)
    seg = len(c) - 3
    delta = c[-2]
    blur = c[-1]
    left = max(0, min(seg - 1, math.floor(delta * seg)))
    right = left + 1
    if left == 0:
        mid_mid = (c[0] + c[1]) / 2.0
        right_mid = (c[1] + c[2]) / 2.0
        mid_len = abs(c[1] - c[0])
        right_len = abs(c[2] - c[1])
        right_percent = mid_len / (right_len + mid_len)
        right_point = c[1] + right_percent * blur * (mid_mid - right_mid)
        return _bezier_value([c[0], right_point, c[1]], delta * seg - left)
    if right == seg:
        left_mid = (c[seg - 2] + c[seg - 1]) / 2.0
        mid_mid = (c[seg - 1] + c[seg]) / 2.0
        left_len = abs(c[seg - 1] - c[seg - 2])
        mid_len = abs(c[seg] - c[seg - 1])
        left_percent = mid_len / (left_len + mid_len)
        left_point = c[seg - 1] + left_percent * blur * (mid_mid - left_mid)
        return _bezier_value(
            [c[seg - 1], left_point, c[seg]], delta * seg - left
        )
    left_mid = (c[left - 1] + c[left]) / 2.0
    mid_mid = (c[left] + c[right]) / 2.0
    right_mid = (c[right] + c[right + 1]) / 2.0
    left_len = abs(c[left] - c[left - 1])
    mid_len = abs(c[right] - c[left])
    right_len = abs(c[right + 1] - c[right])
    left_percent = mid_len / (left_len + mid_len)
    right_percent = mid_len / (right_len + mid_len)
    left_point = c[left] + left_percent * blur * (mid_mid - left_mid)
    right_point = c[right] + right_percent * blur * (mid_mid - right_mid)
    return _bezier_value(
        [c[left], left_point, right_point, c[right]], delta * seg - left
    )


def _v_bezier_value(points: list[float]) -> float:
    """v_bezier：同 c_bezier，但偏移比例固定为 0.5（垂直均衡版）。"""
    c = list(points)
    seg = len(c) - 3
    delta = c[-2]
    blur = c[-1]
    left = max(0, min(seg - 1, math.floor(delta * seg)))
    right = left + 1
    if left == 0:
        mid_mid = (c[0] + c[1]) / 2.0
        right_mid = (c[1] + c[2]) / 2.0
        right_point = c[1] + 0.5 * blur * (mid_mid - right_mid)
        return _bezier_value([c[0], right_point, c[1]], delta * seg - left)
    if right == seg:
        left_mid = (c[seg - 2] + c[seg - 1]) / 2.0
        mid_mid = (c[seg - 1] + c[seg]) / 2.0
        left_point = c[seg - 1] + 0.5 * blur * (mid_mid - left_mid)
        return _bezier_value(
            [c[seg - 1], left_point, c[seg]], delta * seg - left
        )
    left_mid = (c[left - 1] + c[left]) / 2.0
    mid_mid = (c[left] + c[right]) / 2.0
    right_mid = (c[right] + c[right + 1]) / 2.0
    left_point = c[left] + 0.5 * blur * (mid_mid - left_mid)
    right_point = c[right] + 0.5 * blur * (mid_mid - right_mid)
    return _bezier_value(
        [c[left], left_point, right_point, c[right]], delta * seg - left
    )


def _easing(name: str, value: float) -> float:  # noqa: C901, PLR0911
    """缓动函数（对称系列 Quad/Cubic 等 + 游戏原式 Back/Elastic/Bounce）。"""
    t = min(1.0, max(0.0, value))
    if name == "linear":
        return t
    kind, phase = name[4:], ""
    for prefix in ("easeInOut", "easeOut", "easeIn"):
        if name.startswith(prefix):
            kind, phase = name[len(prefix) :], prefix
            break
    if kind == "Back":
        return _ease_back(phase, t)
    if kind == "Elastic":
        return _ease_elastic(phase, t)
    if kind == "Bounce":
        return _ease_bounce(phase, t)
    if kind not in ("Quad", "Cubic", "Quart", "Quint", "Sine", "Expo", "Circ"):
        raise _FormulaError(f"不支持的缓动函数 {name}")  # noqa: TRY003
    if phase == "easeIn":
        return _ease_in(kind, t)
    if phase == "easeOut":
        return 1 - _ease_in(kind, 1 - t)
    if t < 0.5:  # noqa: PLR2004
        return _ease_in(kind, 2 * t) / 2
    return 1 - _ease_in(kind, 2 - 2 * t) / 2


def _ease_back(phase: str, t: float) -> float:
    """Back 系列（游戏 Func_Base 同款系数）。"""
    c1 = 1.70158
    if phase == "easeIn":
        c3 = c1 + 1
        return c3 * t**3 - c1 * t * t
    if phase == "easeOut":
        c3 = c1 + 1
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
    c2 = c1 * 1.525
    if t < 0.5:  # noqa: PLR2004
        return (math.pow(2 * t, 2) * ((c2 + 1) * 2 * t - c2)) / 2
    return (math.pow(2 * t - 2, 2) * ((c2 + 1) * (t * 2 - 2) + c2) + 2) / 2


def _ease_elastic(phase: str, t: float) -> float:  # noqa: PLR0911
    """Elastic 系列（游戏 Func_Base 同款系数）。"""
    if phase == "easeIn":
        c4 = 2 * math.pi / 3
        if t == 0:
            return 0.0
        if t == 1:
            return 1.0
        return -math.pow(2, 10 * t - 10) * math.sin((t * 10 - 10.75) * c4)
    if phase == "easeOut":
        c4 = 2 * math.pi / 3
        if t == 0:
            return 0.0
        if t == 1:
            return 1.0
        return math.pow(2, -10 * t) * math.sin((t * 10 - 0.75) * c4) + 1
    c5 = 2 * math.pi / 4.5
    if t == 0:
        return 0.0
    if t == 1:
        return 1.0
    if t < 0.5:  # noqa: PLR2004
        return -(math.pow(2, 20 * t - 10) * math.sin((20 * t - 11.125) * c5)) / 2
    return (math.pow(2, -20 * t + 10) * math.sin((20 * t - 11.125) * c5)) / 2 + 1


def _ease_bounce(phase: str, t: float) -> float:
    """Bounce 系列（游戏 Func_Base 同款分段）。"""
    if phase == "easeIn":
        return 1 - _ease_out_bounce(t)
    if phase == "easeOut":
        return _ease_out_bounce(t)
    if t < 0.5:  # noqa: PLR2004
        return (1 - _ease_out_bounce(1 - 2 * t)) / 2
    return (1 + _ease_out_bounce(2 * t - 1)) / 2


def _ease_out_bounce(t: float) -> float:
    """easeOutBounce（游戏同款 n1=7.5625 / d1=2.75 分段）。"""
    n1 = 7.5625
    d1 = 2.75
    if t < 1 / d1:
        return n1 * t * t
    if t < 2 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    if t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    t -= 2.625 / d1
    return n1 * t * t + 0.984375


def _ease_in(kind: str, t: float) -> float:  # noqa: PLR0911
    if kind == "Quad":
        return t * t
    if kind == "Cubic":
        return t * t * t
    if kind == "Quart":
        return t**4
    if kind == "Quint":
        return t**5
    if kind == "Sine":
        return 1 - math.cos(t * math.pi / 2)
    if kind == "Expo":
        return 0.0 if t == 0 else math.pow(2, 10 * (t - 1))
    return 1 - math.sqrt(1 - t * t)  # Circ


# 表达式节点：("num", v) / ("var", n) / ("op", op, l, r) / ("neg", n) /
# ("call", name, [args])
_Node = tuple


def _compile_expr(text: str) -> _Node:
    """编译公式表达式为 AST（tokenize 一次，之后按采样点直接求值）。"""
    tokens = re.findall(r"\d+\.?\d*|\.\d+|[a-zA-Z_][a-zA-Z0-9_]*|[+\-*/%^(),]", text)
    parser = _FormulaParser(tokens)
    node = parser.parse()
    if parser.pos != len(tokens):
        raise _FormulaError(f"公式末尾有多余内容: {text!r}")  # noqa: TRY003
    return node


def _eval_formula(text: str, env: dict[str, float]) -> float:
    """编译并求值公式表达式（数字/变量/四则/函数调用）。"""
    return _eval_node(_compile_expr(text), env)


def _eval_node(node: _Node, env: dict[str, float]) -> float:  # noqa: PLR0911
    """按环境求值已编译的 AST。"""
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        return env.get(node[1], 0.0)  # 游戏 FormulaVar 对未定义变量返回 0
    if kind == "neg":
        return -_eval_node(node[1], env)
    if kind == "op":
        op, left, right = node[1], _eval_node(node[2], env), _eval_node(node[3], env)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return left / right
        if op == "%":
            return left % right
        return math.pow(left, right)
    name, args = node[1], [_eval_node(arg, env) for arg in node[2]]
    return _call_function(name, args)


class _FormulaParser:
    """递归下降表达式解析器（编译为 AST）。"""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> _Node:
        return self._expr()

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _expr(self) -> _Node:
        node = self._term()
        while self._peek() in ("+", "-"):
            op = self.tokens[self.pos]
            self.pos += 1
            node = ("op", op, node, self._term())
        return node

    def _term(self) -> _Node:
        node = self._unary()
        while self._peek() in ("*", "/", "%"):
            op = self.tokens[self.pos]
            self.pos += 1
            node = ("op", op, node, self._unary())
        return node

    def _unary(self) -> _Node:
        if self._peek() in ("+", "-"):
            op = self.tokens[self.pos]
            self.pos += 1
            node = self._unary()
            return node if op == "+" else ("neg", node)
        return self._power()

    def _power(self) -> _Node:
        base = self._atom()
        if self._peek() in ("^", "**"):
            self.pos += 1
            return ("op", "^", base, self._power())
        return base

    def _atom(self) -> _Node:
        token = self._peek()
        if token is None:
            raise _FormulaError("公式意外结束")
        if token[0].isdigit() or token[0] == ".":
            self.pos += 1
            return ("num", float(token))
        if token == "(":
            self.pos += 1
            node = self._expr()
            if self._peek() != ")":
                raise _FormulaError("缺少右括号")
            self.pos += 1
            return node
        if token[0].isalpha():
            self.pos += 1
            if self._peek() == "(":
                return self._call(token)
            return ("var", token)
        raise _FormulaError(f"无法识别的符号 {token!r}")  # noqa: TRY003

    def _call(self, name: str) -> _Node:
        self.pos += 1  # 跳过 (
        args: list[_Node] = []
        if self._peek() != ")":
            while True:
                args.append(self._expr())
                if self._peek() != ",":
                    break
                self.pos += 1
        if self._peek() != ")":
            raise _FormulaError(f"函数 {name} 缺少右括号")  # noqa: TRY003
        self.pos += 1
        return ("call", name, args)


_FUNCTIONS: dict[str, Callable[..., float]] = {}


def _register(name: str) -> Callable:
    def decorator(fn: Callable[..., float]) -> Callable:
        _FUNCTIONS[name] = fn
        return fn

    return decorator


# bezier 至少 2 个控制点 + 1 个 value
_BEZIER_MIN_ARGS = 3
# c/v_bezier 至少 2 个控制点 + delta + blur
_BEZIER_EXT_MIN_ARGS = 4


def _call_function(name: str, args: list[float]) -> float:
    if name == "bezier":
        if len(args) < _BEZIER_MIN_ARGS:
            raise _FormulaError("bezier 至少需要 2 个控制点 + value")  # noqa: TRY003
        return _bezier_value(args[:-1], args[-1])
    if name == "pi":
        return math.pi  # 旧版需要占位参数，忽略
    if name in _FUNCTIONS:
        return _FUNCTIONS[name](*args)
    if name.startswith("ease"):
        if len(args) != 1:
            raise _FormulaError(f"缓动函数 {name} 需要 1 个参数")  # noqa: TRY003
        return _easing(name, args[0])
    raise _FormulaError(f"未支持函数 {name}")  # noqa: TRY003


@_register("sin")
def _f_sin(value: float) -> float:
    return math.sin(value)


@_register("cos")
def _f_cos(value: float) -> float:
    return math.cos(value)


@_register("tan")
def _f_tan(value: float) -> float:
    return math.tan(value)


@_register("arcsin")
def _f_arcsin(value: float) -> float:
    return math.asin(min(1.0, max(-1.0, value)))


@_register("arccos")
def _f_arccos(value: float) -> float:
    return math.acos(min(1.0, max(-1.0, value)))


@_register("arctan")
def _f_arctan(value: float) -> float:
    return math.atan(value)


@_register("abs")
def _f_abs(value: float) -> float:
    return abs(value)


@_register("pow")
def _f_pow(a: float, n: float) -> float:
    return math.pow(a, n)


@_register("exp")
def _f_exp(value: float) -> float:
    return math.exp(value)


@_register("clamp")
def _f_clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


@_register("map")
def _f_map(value: float, a: float, b: float) -> float:
    return a + (b - a) * value


@_register("remap")
def _f_remap(value: float, a: float, b: float) -> float:
    return (value - a) / (b - a) if b != a else 0.0


@_register("between")
def _f_between(value: float, a: float, b: float) -> float:
    return 1.0 if a <= value <= b else 0.0


@_register("outof")
def _f_outof(value: float, a: float, b: float) -> float:
    return 0.0 if a <= value <= b else 1.0


@_register("less")
def _f_less(value: float, other: float) -> float:
    return 1.0 if value < other else 0.0


@_register("greater")
def _f_greater(value: float, other: float) -> float:
    return 1.0 if value > other else 0.0


@_register("equal")
def _f_equal(value: float, other: float) -> float:
    return 1.0 if value == other else 0.0


@_register("unequal")
def _f_unequal(value: float, other: float) -> float:
    return 0.0 if value == other else 1.0


@_register("c_bezier")
def _f_c_bezier(*args: float) -> float:
    if len(args) < _BEZIER_EXT_MIN_ARGS:
        raise _FormulaError("c_bezier 至少需要 2 个控制点 + delta + blur")  # noqa: TRY003
    return _c_bezier_value(list(args))


@_register("v_bezier")
def _f_v_bezier(*args: float) -> float:
    if len(args) < _BEZIER_EXT_MIN_ARGS:
        raise _FormulaError("v_bezier 至少需要 2 个控制点 + delta + blur")  # noqa: TRY003
    return _v_bezier_value(list(args))


_BLOCK_STMT_RE = re.compile(r"\$?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(.*))?$")


def _eval_black_line_block(
    stmt: str,
    bpm_changes: list[tuple[float, float]],
    default_bpm: float,
    pos_breaks: list[tuple[float, float]],
    *,
    is_para: bool,
) -> list[tuple[float, float]] | None:
    """求值 BlackLine::[ ... ] 公式块，返回 (时间秒, x) 折线。

    y 为相对 Move_Y 的拍数偏移（para）或 pos 偏移（pos，取逆函数）；
    x 为相对 Move_X 的横向偏移；``$ Mirror`` 时 x 取反。
    """
    start = stmt.find("[")
    end = stmt.find("]", start + 1)
    if start == -1 or end == -1:
        return None
    statements = _parse_block_statements(stmt[start + 1 : end])
    if not statements:
        return None
    # 编译一次，采样时直接求值 AST（避免每个采样点重复分词/解析）
    try:
        compiled = [
            (name, _compile_expr(expr) if expr else None) for name, expr in statements
        ]
    except _FormulaError:
        return None
    freq = _block_freq(compiled)
    sample_count = max(_MIN_SAMPLES, min(int(freq), _MAX_SAMPLES))

    points: list[tuple[float, float]] = []
    for i in range(sample_count + 1):
        point = _sample_black_line(compiled, i / sample_count, freq)
        if point is not None:
            beat, x = point
            if not is_para:
                beat = _pos_to_beat(beat, pos_breaks)
            points.append(
                (max(0.0, _beat_to_time(beat, bpm_changes, default_bpm)), x)
            )
    return points if len(points) >= _MIN_POLYLINE_POINTS else None


def _parse_block_statements(text: str) -> list[tuple[str, str]]:
    """把公式块文本解析为 (变量名, 表达式) 列表。"""
    statements: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stmt_match = _BLOCK_STMT_RE.match(line)
        if stmt_match:
            statements.append((stmt_match.group(1), stmt_match.group(2) or ""))
    return statements


def _block_freq(compiled: list[tuple[str, _Node | None]]) -> float:
    """读取 $ Freq（采样点频率），缺省 100。"""
    for name, node in compiled:
        if name == "Freq" and node is not None:
            try:
                return float(_eval_node(node, {"delta": 0.0}))
            except (ValueError, ZeroDivisionError):
                return _DEFAULT_SAMPLES
    return _DEFAULT_SAMPLES


def _sample_black_line(
    compiled: list[tuple[str, _Node | None]],
    delta: float,
    freq: float,
) -> tuple[float, float] | None:
    """求值一个采样点的 (拍/pos, x)。"""
    env = {"delta": delta, "Freq": freq, "Move_X": 0.0, "Move_Y": 0.0}
    mirror = False
    try:
        for name, node in compiled:
            if node is None:
                if name == "Mirror":
                    mirror = True
                continue
            env[name] = _eval_node(node, env)
    except (ValueError, ZeroDivisionError):
        return None
    beat = env.get("Move_Y", 0.0) + env.get("y", 0.0)
    x = env.get("Move_X", 0.0) + env.get("x", 0.0)
    if mirror:
        x = -x
    return beat, x


def _beat_to_time(
    beat: float, changes: list[tuple[float, float]], default_bpm: float
) -> float:
    """把拍数换算为秒（按 BpmChange 分段积分）。"""
    total = 0.0
    prev_beat, prev_bpm = 0.0, default_bpm if default_bpm > 0 else _DEFAULT_BPM
    for beat_at, bpm in changes:
        if beat <= beat_at:
            break
        total += (beat_at - prev_beat) / prev_bpm * 60
        prev_beat, prev_bpm = beat_at, bpm
    total += (beat - prev_beat) / prev_bpm * 60
    return total


def find_chart(name: str, diff: str | None = None) -> tuple[Path, str] | None:
    """按 bmsong 同款检索解析曲名，再按难度定位谱面文件。

    ``diff`` 缺省时选已收录难度中定数最高的（无定数信息则按文件顺序）。
    """
    constants = get_song_constants()
    names = search_songs(constants, name) or [name]
    for canonical in names:
        result = _locate_chart(constants, canonical, diff)
        if result is not None:
            return result
    return None


def available_diffs(song: str) -> list[str]:
    """返回该曲目已收录谱面的难度列表（按 _CHART_DIFFS 顺序）。"""
    return [diff for diff in _CHART_DIFFS if _chart_file(song, diff) is not None]


def _locate_chart(
    constants: dict[str, dict], song: str, diff: str | None
) -> tuple[Path, str] | None:
    """按难度在 chart/ 下定位谱面文件（大小写不敏感索引兜底）。"""
    if diff is not None:
        diff = diff.upper()
        if diff not in _CHART_DIFFS:
            return None
        return _chart_file(song, diff)
    entry = constants.get(song) or {}
    ordered = sorted(
        (d for d in _CHART_DIFFS if float(entry.get(d) or 0) > 0),
        key=lambda d: float(entry[d]),
        reverse=True,
    )
    for d in ordered + [d for d in _CHART_DIFFS if d not in ordered]:
        result = _chart_file(song, d)
        if result is not None:
            return result
    return None


def _chart_file(song: str, diff: str) -> tuple[Path, str] | None:
    """查找 ``chart/<曲名> <难度>[.txt]``，直接路径优先，索引（小写）兜底。"""
    for suffix in ("", ".txt"):
        direct = CHART_DIR / f"{song} {diff}{suffix}"
        if direct.exists():
            return direct, diff
    index = _chart_index()
    for candidate in (song, *_name_variants(song)):
        for filename in index.get(candidate.lower(), []):
            base = filename[:-4] if filename.lower().endswith(".txt") else filename
            if base.endswith(f" {diff}"):
                return CHART_DIR / filename, diff
    return None


def _chart_index() -> dict[str, list[str]]:
    """chart/ 下所有谱面文件名（去难度、小写）→ 文件名列表。"""
    index: dict[str, list[str]] = {}
    if not CHART_DIR.is_dir():
        return index
    for path in CHART_DIR.iterdir():
        if not path.is_file():
            continue
        stem = path.name.rsplit(" ", 1)[0]
        index.setdefault(stem.lower(), []).append(path.name)
    return index


# ---------- 曲目↔谱面对照（chart/Info） ----------


@lru_cache(maxsize=1)
def load_song_info() -> dict[tuple[str, str], dict[str, str]]:
    """解析 ``chart/Info``（Song::/Chart:: 对照，含定数/等级/谱师）。

    键为 ``(曲目内部名, 难度)``，值为 ``{title, artist, level, dlevel,
    charter, color}``。文件缺失时返回空 dict。
    """
    data: dict[tuple[str, str], dict[str, str]] = {}
    path = CHART_DIR / "Info"
    if not path.exists():
        return data
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    current_song = ""
    for block in re.finditer(
        r"(Song|Chart)::\s*\{\s*(.*?)\s*\};", text, flags=re.DOTALL
    ):
        kind, body = block.group(1), block.group(2)
        fields = _parse_info_fields(body)
        if kind == "Song":
            current_song = fields.get("Path", "")
            if current_song:
                data.setdefault((current_song, ""), fields)
            continue
        diff = fields.get("Path", "").upper()
        if current_song and diff:
            data[(current_song, diff)] = fields
    return data


def _parse_info_fields(body: str) -> dict[str, str]:
    """解析 Info 块内的 ``$ Key = Value`` 字段（一行可多个，值可带引号）。"""
    fields: dict[str, str] = {}
    for match in re.finditer(
        r"\$\s*([A-Za-z_]\w*)\s*=\s*(?:\"([^\"]*)\"|([^$\n;]+))", body
    ):
        name = match.group(1)
        value = (match.group(2) or match.group(3) or "").strip().rstrip(",")
        fields[name] = value
    return fields


def _name_variants(name: str) -> list[str]:
    """曲名归一化变体（小写、去多余空白）。"""
    text = re.sub(r"[_\u3000]+", " ", name.strip())
    text = re.sub(r"\s+", " ", text)
    return [text.lower(), text]


def render_chart_preview(
    chart: ChartData, display_name: str, skin: str = DEFAULT_SKIN
) -> bytes:
    """渲染谱面预览图，返回 PNG 字节。``skin`` 为音符皮肤名。"""
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
    _draw_black_lines(image, chart.black_lines, columns)
    if _draw_hold_layer(image, chart.notes, columns, skin):
        # 跨栏 Slide 画完整多边形后，重画分隔线盖住越界部分
        _draw_separators(draw, columns, height)
    _draw_tap_drag(image, draw, chart.notes, columns, skin)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_title(
    draw: ImageDraw.ImageDraw, width: int, chart: ChartData, display_name: str
) -> None:
    font_title = _font(26, bold=True)
    font_sub = _font(16)
    draw.text((_EDGE_PAD, 14), display_name, font=font_title, fill=_TITLE_COLOR)
    duration_text = f"时长 {chart.duration / 60:.1f} 分钟"
    if chart.dlevel:
        meta = f"{chart.charter} | 定数 {chart.dlevel} | {duration_text}"
    else:
        meta = f"{chart.charter} | Level {chart.level} | {duration_text}"
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


def _draw_black_lines(
    image: Image.Image,
    black_lines: list[list[tuple[float, float]]],
    columns: int,
) -> None:
    """黑线折线：按分钟段分组，x 裁到轨道范围内，50% 透明度叠加。"""
    if not black_lines:
        return
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for line in black_lines:
        for group in _group_by_column(line, columns):
            points = [_black_line_point(t, x, columns) for t, x in group]
            if len(points) >= _MIN_POLYLINE_POINTS:
                draw.line(
                    points,
                    fill=(255, 255, 255, _BLACK_LINE_ALPHA),
                    width=_BLACK_LINE_WIDTH,
                    joint="curve",
                )
    image.paste(overlay, (0, 0), overlay)


def _black_line_point(t: float, x: float, columns: int) -> tuple[float, float]:
    """黑线点坐标 → 像素（x 裁到轨道 [-1, 1] 内）。"""
    col = _note_col(t, columns)
    return _note_x(max(-1.0, min(1.0, x)), col), _note_y(t, col)


def _group_by_column(
    points: list[tuple[float, float]], columns: int
) -> list[list[tuple[float, float]]]:
    """把 (时间, x) 点按分钟段分组（跨段折线拆开）。"""
    groups: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    last_col = -1
    for t, x in points:
        col = _note_col(t, columns)
        if current and col != last_col:
            groups.append(current)
            current = []
        current.append((t, x))
        last_col = col
    if current:
        groups.append(current)
    return groups


def _draw_hold_layer(
    image: Image.Image,
    notes: list[Note],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> bool:
    """绘制全部 Slide（Hold）到叠加层并合成，返回是否画了 Hold。

    每个 Hold 画完整多边形（跨栏连续，不拆分），重叠处透明度累加。
    """
    hold_overlay: Image.Image | None = None
    for note in notes:
        if note.kind != "Hold":
            continue
        if hold_overlay is None:
            hold_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        _draw_hold_on_layer(hold_overlay, note, columns, skin)
    if hold_overlay is not None:
        image.paste(hold_overlay, (0, 0), hold_overlay)
        return True
    return False


def _draw_tap_drag(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    notes: list[Note],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> None:
    """绘制 Tap/Drag（在 Slide 与分隔线之上）；素材缺失时回退彩色条。"""
    for note in notes:
        if note.kind == "Hold":
            continue
        for point in note.points:
            drawn = False
            if note.flick:
                drawn = _draw_flick_note(image, point, columns, skin)
            if not drawn and skin == "Dynamix" and note.kind == "Drag":
                drawn = _draw_dynamix_drag(image, point, columns)
            if not drawn:
                drawn = _draw_note_image(image, note.kind, point, columns, skin)
            if not drawn:
                _draw_note_bar(draw, point, NOTE_COLORS[note.kind], columns)


def _note_pixel_width(width: float) -> float:
    """音符宽度值（x2）→ 像素宽（x2=1 占满整个轨道）。"""
    return max(1.0, width * (_COLUMN_WIDTH - 2 * _LANE_PAD))


# 各素材 Sprite 的 9-slice 左右边框（data.unity3d 的 m_Border，像素）。
# 只有 Flick 素材按真实边框拉伸，其余 note 按「中间 1 像素」统一规则。
_SPRITE_CAPS: dict[str, tuple[int, int]] = {
    "flick_bg.png": (279, 279),
    "flick2.png": (216, 215),
}


def _crop_alpha(image: Image.Image) -> Image.Image:
    """裁剪到 alpha 包围盒，去掉透明边距（使所有 note 拉伸后可见高度一致）。"""
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return image
    return image.crop(bbox)


def _resize_note_stretched(
    image: Image.Image,
    target_w: int,
    target_h: int,
    left_cap: int | None = None,
    right_cap: int | None = None,
) -> Image.Image:
    """9-slice 拉伸：左右 cap 保留原样，中间区域拉伸到目标宽。

    cap 未指定时取中间 1 像素列（左右各半）；宽度不变小时退化整体缩放。
    高度始终整体缩放到目标高。
    """
    src_w, src_h = image.size
    if target_w <= src_w:
        return image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    if left_cap is None or right_cap is None:
        mid = src_w // 2
        left_cap, right_cap = mid, src_w - mid - 1
    left_cap = min(left_cap, src_w // 2)
    right_cap = min(right_cap, src_w - left_cap - 1)
    left = image.crop((0, 0, left_cap, src_h))
    middle = image.crop((left_cap, 0, src_w - right_cap, src_h))
    right = image.crop((src_w - right_cap, 0, src_w, src_h))
    middle_w = max(1, target_w - left_cap - right_cap)
    middle = middle.resize((middle_w, src_h), Image.Resampling.NEAREST)
    out = Image.new("RGBA", (target_w, src_h))
    out.paste(left, (0, 0))
    out.paste(middle, (left_cap, 0))
    out.paste(right, (target_w - right_cap, 0))
    return out.resize((target_w, target_h), Image.Resampling.LANCZOS)


_note_image_cache: dict[tuple[str, str], Image.Image | None] = {}


def _note_image(kind: str, skin: str = DEFAULT_SKIN) -> Image.Image | None:
    """加载指定皮肤的素材，皮肤/类型缺失时回退 White 对应素材。

    Lanota 的 Tap 无独立贴图，由 Lanota_Drag 的蓝色部分染白生成。
    """
    key = (skin, kind)
    if key not in _note_image_cache:
        image = None
        if skin == "Lanota" and kind == "Tap":
            image = _lanota_tap_texture()
        if image is None:
            files = SKIN_SETS.get(skin, {})
            for filename in (files.get(kind), SKIN_SETS[DEFAULT_SKIN].get(kind)):
                if not filename:
                    continue
                path = NOTE_DIR / filename
                if path.exists():
                    try:
                        image = Image.open(path).convert("RGBA")
                    except OSError:
                        image = None
                    break
        _note_image_cache[key] = image
    return _note_image_cache[key]


@lru_cache(maxsize=1)
def _lanota_tap_texture() -> Image.Image | None:
    """生成 Lanota Tap 素材：Lanota_Drag 的蓝色核心染成白色（金色边框保留）。"""
    path = NOTE_DIR / "Lanota_Drag.png"
    if not path.exists():
        return None
    try:
        base = Image.open(path).convert("RGBA")
    except OSError:
        return None
    pixels = list(base.getdata())
    recolored = [
        (255, 255, 255, alpha)
        if (b > _BLUE_THRESHOLD and b > r and g > _BLUE_MIN_G)
        else (r, g, b, alpha)
        for r, g, b, alpha in pixels
    ]
    out = Image.new("RGBA", base.size)
    out.putdata(recolored)
    return out


_tinted_cache: dict[tuple[str, str, tuple[int, int, int]], Image.Image] = {}


def _tinted_note_image(
    kind: str, color: tuple[int, int, int], skin: str = DEFAULT_SKIN
) -> Image.Image | None:
    """按目标色染色的素材（逐通道乘 color/255），带缓存。"""
    key = (skin, kind, color)
    if key not in _tinted_cache:
        base = _note_image(kind, skin)
        if base is None:
            return None
        r_ch, g_ch, b_ch, a_ch = base.split()
        r_ch = r_ch.point(lambda v, f=color[0] / 255: int(v * f))
        g_ch = g_ch.point(lambda v, f=color[1] / 255: int(v * f))
        b_ch = b_ch.point(lambda v, f=color[2] / 255: int(v * f))
        _tinted_cache[key] = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
    return _tinted_cache[key]


_hold_color_cache: dict[str, tuple[int, int, int] | None] = {}


def _hold_color(skin: str = DEFAULT_SKIN) -> tuple[int, int, int]:
    """指定皮肤 Hold 素材主色（素材是平条，直接用色画连续粗线），缺失回退红。"""
    if skin not in _hold_color_cache:
        color: tuple[int, int, int] | None = None
        image = _note_image("Hold", skin)
        if image is not None:
            pixels = [p[:3] for p in image.getdata() if p[3] > _ALPHA_THRESHOLD]
            if pixels:
                color = tuple(
                    sum(c[i] for c in pixels) // len(pixels) for i in range(3)
                )
        _hold_color_cache[skin] = color
    return _hold_color_cache[skin] or NOTE_COLORS["Hold"]


# 已拉伸的 note 素材缓存：同一 (皮肤, 类型, 目标宽) 只拉伸一次
# （大纹理如 Lanota 1780x304 每音符都做裁剪+缩放会非常慢）
_note_resized_cache: dict[tuple[str, str, int], Image.Image] = {}


def _resized_note_texture(
    kind: str, skin: str, target_w: int, target_h: int
) -> Image.Image | None:
    """裁透明边并 9-slice 拉伸到目标宽，按 (皮肤, 类型, 目标宽) 缓存。"""
    key = (kind, skin, target_w)
    resized = _note_resized_cache.get(key)
    if resized is None:
        base = _note_image(kind, skin)
        if base is None:
            return None
        if kind == "Drag" and skin == "Tech":
            tinted = _tinted_note_image(kind, NOTE_COLORS["Drag"], skin)
            if tinted is not None:
                base = tinted
        resized = _resize_note_stretched(_crop_alpha(base), target_w, target_h)
        _note_resized_cache[key] = resized
    return resized


def _draw_note_image(
    image: Image.Image,
    kind: str,
    point: tuple[float, float, float],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> bool:
    """用指定皮肤素材画 Tap/Drag：宽度拉伸到音符宽度，高度固定。

    Tech 皮肤的 Drag（wipe）染黄色，其余皮肤保持素材本色。
    """
    t, x1, width = point
    col = _note_col(t, columns)
    px = round(_note_x(x1, col))
    py = round(_note_y(t, col))
    target_w = round(_note_pixel_width(width))
    if target_w <= _MIN_NOTE_PIXELS:
        return False
    # 高度固定（游戏里音符高度不随宽度变化），宽度 9-slice 拉伸（中间 1 像素列）
    resized = _resized_note_texture(kind, skin, target_w, _NOTE_FIXED_HEIGHT)
    if resized is None:
        return False
    image.paste(resized, (px - resized.width // 2, py - resized.height // 2), resized)
    return True


# Dynamix Drag 素材缓存：dy_mid 固定尺寸只算一次，细轨按目标宽缓存
_dy_rail_cache: dict[int, Image.Image] = {}


@lru_cache(maxsize=1)
def _dy_mid_scaled() -> Image.Image:
    """dy_mid 裁剪透明边后缩放到 note 高度（保持比例）。"""
    mid = _note_image("dy_mid", "Dynamix")
    mid_cropped = _crop_alpha(mid)
    mid_w = max(1, round(mid_cropped.width * _NOTE_FIXED_HEIGHT / mid_cropped.height))
    return mid_cropped.resize((mid_w, _NOTE_FIXED_HEIGHT), Image.Resampling.LANCZOS)


def _draw_dynamix_drag(
    image: Image.Image,
    point: tuple[float, float, float],
    columns: int,
) -> bool:
    """Dynamix Drag：中央 dy_mid（缩放至 note 高度、保持比例）+ 细轨 Dynamix_Drag。

    细轨 9-slice 拉伸到音符宽度，高度 = dy_mid 高度的 11/69，两者纵坐标居中。
    """
    rail = _note_image("Drag", "Dynamix")
    mid = _note_image("dy_mid", "Dynamix")
    if rail is None or mid is None:
        return False
    t, x1, width = point
    col = _note_col(t, columns)
    px = round(_note_x(x1, col))
    py = round(_note_y(t, col))
    target_w = round(_note_pixel_width(width))
    if target_w <= _MIN_NOTE_PIXELS:
        return False
    # dy_mid 与普通 note 同高（不参与拉伸、保持比例）；固定尺寸只算一次
    mid_scaled = _dy_mid_scaled()
    # 细轨高度 = dy_mid 高度的 11/69；按目标宽缓存
    rail_h = max(1, round(_NOTE_FIXED_HEIGHT * _DYNAMIX_RAIL_RATIO))
    rail_img = _dy_rail_cache.get(target_w)
    if rail_img is None:
        rail_img = _resize_note_stretched(_crop_alpha(rail), target_w, rail_h)
        _dy_rail_cache[target_w] = rail_img
    image.paste(
        rail_img, (px - rail_img.width // 2, py - rail_img.height // 2), rail_img
    )
    image.paste(
        mid_scaled,
        (px - mid_scaled.width // 2, py - mid_scaled.height // 2),
        mid_scaled,
    )
    return True


# Phigros Flick 素材缓存：flick_bg 条按目标宽缓存，箭头固定尺寸只算一次
_flick_bar_cache: dict[int, Image.Image] = {}


@lru_cache(maxsize=1)
def _flick_arrow_scaled() -> Image.Image:
    """flick 箭头裁透明边后缩放到 note 高度（保持比例）。"""
    arrow = _note_image("FlickArrow", "Phigros")
    arrow_cropped = _crop_alpha(arrow)
    arrow_h = _NOTE_FIXED_HEIGHT
    arrow_w = max(1, round(arrow_cropped.width * arrow_h / arrow_cropped.height))
    return arrow_cropped.resize(
        (arrow_w, arrow_h), Image.Resampling.LANCZOS
    )


def _draw_flick_note(
    image: Image.Image,
    point: tuple[float, float, float],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> bool:
    """Flick 音符：Phigros = flick_bg 拉伸 + flick 箭头居中（不拉伸）；
    Lanota = flick2 拉伸。皮肤无 flick 素材时返回 False 回退普通 Drag。"""
    t, x1, width = point
    col = _note_col(t, columns)
    px = round(_note_x(x1, col))
    py = round(_note_y(t, col))
    target_w = round(_note_pixel_width(width))
    if target_w <= _MIN_NOTE_PIXELS:
        return False
    if skin == "Phigros":
        bg = _note_image("FlickBg", skin)
        if bg is None or _note_image("FlickArrow", skin) is None:
            return False
        caps = _SPRITE_CAPS.get("flick_bg.png")
        bar = _flick_bar_cache.get(target_w)
        if bar is None:
            bar = _resize_note_stretched(
                _crop_alpha(bg), target_w, _NOTE_FIXED_HEIGHT, caps[0], caps[1]
            )
            _flick_bar_cache[target_w] = bar
        image.paste(
            bar, (px - bar.width // 2, py - bar.height // 2), bar
        )
        # 箭头裁透明边后保持比例缩放到普通 note 高度（不参与横向拉伸），居中
        arrow_scaled = _flick_arrow_scaled()
        image.paste(
            arrow_scaled,
            (px - arrow_scaled.width // 2, py - arrow_scaled.height // 2),
            arrow_scaled,
        )
        return True
    if skin == "Lanota":
        tex = _note_image("Flick", skin)
        if tex is None:
            return False
        caps = _SPRITE_CAPS.get("flick2.png")
        resized = _resize_note_stretched(
            tex, target_w, _NOTE_FIXED_HEIGHT, caps[0], caps[1]
        )
        image.paste(
            resized, (px - resized.width // 2, py - resized.height // 2), resized
        )
        return True
    return False


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


def _perspective_matrix(
    src_pts: list[tuple[float, float]], dst_pts: list[tuple[float, float]]
) -> tuple[float, float, float, float, float, float, float, float]:
    """解 8 元线性方程组，求把 dst 四点映射到 src 四点的透视系数。

    PIL PERSPECTIVE 系数为输出像素 → 输入像素：
        u = (a x + b y + c) / (g x + h y + 1)
        v = (d x + e y + f) / (g x + h y + 1)
    """
    matrix: list[list[float]] = []
    for (x, y), (u, v) in zip(dst_pts, src_pts):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    for col in range(8):
        pivot = max(range(col, 8), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        pivot_val = matrix[col][col]
        if abs(pivot_val) < _MATRIX_EPSILON:
            raise ValueError("degenerate quad")  # noqa: TRY003
        for row in range(8):
            if row == col:
                continue
            factor = matrix[row][col] / pivot_val
            for c in range(col, 9):
                matrix[row][c] -= factor * matrix[col][c]
    return tuple(matrix[i][8] / matrix[i][i] for i in range(8))


def _draw_hold_segment(  # noqa: PLR0913, PLR0917
    layer: Image.Image,
    slice_img: Image.Image,
    p1: tuple[float, float, float],
    p2: tuple[float, float, float],
    left: int,
    top: int,
) -> None:
    """把 Hold 素材切片透视映射到 p1→p2 段四边形并叠加到 layer（50% alpha）。"""
    x1, y1, h1 = p1
    x2, y2, h2 = p2
    sw, sh = slice_img.size
    quad = [
        (x1 - h1 - left, y1 - top),
        (x1 + h1 - left, y1 - top),
        (x2 + h2 - left, y2 - top),
        (x2 - h2 - left, y2 - top),
    ]
    qx = [p[0] for p in quad]
    qy = [p[1] for p in quad]
    bx0, by0 = min(qx), min(qy)
    bx1, by1 = max(qx), max(qy)
    bw, bh = round(bx1 - bx0) + 1, round(by1 - by0) + 1
    if bw < 1 or bh < 1:
        return
    dst = [(qx[i] - bx0, qy[i] - by0) for i in range(4)]
    src = [(0, 0), (sw, 0), (sw, sh), (0, sh)]
    try:
        coeffs = _perspective_matrix(src, dst)
    except ValueError:
        return
    warped = slice_img.transform(
        (bw, bh), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BILINEAR
    )
    if _HOLD_ALPHA < _ALPHA_MAX:
        a_ch = warped.getchannel("A").point(lambda v: v * _HOLD_ALPHA // _ALPHA_MAX)
        warped.putalpha(a_ch)
    # alpha_composite：半透明源做正规 src-over 叠加（paste 会按 mask 混合 RGB）
    layer.alpha_composite(warped, (round(bx0), round(by0)))


def _draw_hold_on_layer(  # noqa: C901, PLR0912, PLR0915
    overlay: Image.Image,
    note: Note,
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> None:
    """把一个 Slide（Hold）画到叠加层，alpha 逐层累加。

    跨分栏处插入边界点，每列的多边形都延伸到栏边界（避免截断）。
    有 Hold 素材时按路径分段做透视纹理映射（素材纵向切片对应各段长度），
    素材缺失回退纯色多边形。每个 Hold 画到自身包围盒大小的图层上再合成。
    """
    texture = _note_image("Hold", skin)
    if texture is not None:
        texture = _crop_alpha(texture)
    color = _hold_color(skin)
    half_scale = (_COLUMN_WIDTH - 2 * _LANE_PAD) / 2
    # 1) 跨栏边界处插入 (t, x, 宽, 列) 点：边界点同时加入两列，
    #    使每列的多边形都延伸到栏边界（避免截断）
    raw = list(note.points)
    expanded: list[tuple[float, float, float, int]] = []
    for p1, p2 in zip(raw, raw[1:]):
        c1 = _note_col(p1[0], columns)
        c2 = _note_col(p2[0], columns)
        expanded.append((p1[0], p1[1], p1[2], c1))
        if c1 != c2:
            boundary = (c1 + 1) * _SEGMENT_SECONDS
            frac = (boundary - p1[0]) / (p2[0] - p1[0])
            x_b = p1[1] + (p2[1] - p1[1]) * frac
            w_b = p1[2] + (p2[2] - p1[2]) * frac
            expanded.append((boundary, x_b, w_b, c1))  # 上一列末尾（栏顶）
            expanded.append((boundary, x_b, w_b, c2))  # 下一列开头（栏底）
    if raw:
        expanded.append(
            (raw[-1][0], raw[-1][1], raw[-1][2], _note_col(raw[-1][0], columns))
        )
    # 2) 转像素坐标（带列号）
    pts: list[tuple[float, float, float, int]] = []
    for t, x, width, col in expanded:
        px = _note_x(x, col)
        half = width * half_scale
        pts.append((px, _note_y(t, col), half, col))
    if not pts:
        return
    # 3) 包围盒（±1px 抗锯齿余量）
    xs = [px - half for px, py, half, _ in pts]
    xs += [px + half for px, py, half, _ in pts]
    ys = [py for px, py, half, _ in pts]
    if len(note.points) == 1:
        thickness = _NOTE_THICKNESS / 2
        _, py, _, _ = pts[0]
        ys.extend((py - thickness, py + thickness))
    left, top = max(0, int(min(xs)) - 1), max(0, int(min(ys)) - 1)
    right, bottom = (
        min(overlay.width - 1, int(max(xs)) + 1),
        min(overlay.height - 1, int(max(ys)) + 1),
    )
    if right <= left or bottom <= top:
        return
    layer = Image.new(
        "RGBA", (right - left + 1, bottom - top + 1), (0, 0, 0, 0)
    )
    # 4) 按段绘制：同列相邻两点组成一个纹理四边形
    if texture is not None and len(pts) > 1:
        segments: list[
            tuple[tuple[float, float, float], tuple[float, float, float], float]
        ] = []
        total = 0.0
        for (x1, y1, h1, c1), (x2, y2, h2, c2) in zip(pts, pts[1:]):
            if c1 != c2:
                continue
            length = math.hypot(x2 - x1, y2 - y1)
            if length < _ZERO_LENGTH:
                continue
            segments.append(((x1, y1, h1), (x2, y2, h2), length))
            total += length
        if segments and total > _ZERO_LENGTH:
            tw, th = texture.size
            cum = 0.0
            for p1, p2, length in segments:
                y0 = int(th * cum / total)
                cum += length
                y1t = max(y0 + 1, int(th * cum / total))
                slice_img = texture.crop((0, y0, tw, min(th, y1t)))
                _draw_hold_segment(layer, slice_img, p1, p2, left, top)
    else:
        ldraw = ImageDraw.Draw(layer)
        groups: dict[int, list[tuple[float, float, float]]] = {}
        for px, py, half, col in pts:
            groups.setdefault(col, []).append((px, py, half))
        for group_pts in groups.values():
            if len(group_pts) == 1:
                # 单点组：横向小条（避免出现圆球）
                px, py, half = group_pts[0]
                thickness = _NOTE_THICKNESS / 2
                ldraw.rounded_rectangle(
                    (
                        px - half - left,
                        py - thickness - top,
                        px + half - left,
                        py + thickness - top,
                    ),
                    radius=3,
                    fill=(*color, _HOLD_ALPHA),
                )
                continue
            lefts = [(px - half - left, py - top) for px, py, half in group_pts]
            rights = [
                (px + half - left, py - top)
                for px, py, half in reversed(group_pts)
            ]
            ldraw.polygon(lefts + rights, fill=(*color, _HOLD_ALPHA))
    region = overlay.crop((left, top, right + 1, bottom + 1))
    overlay.paste(Image.alpha_composite(region, layer), (left, top))


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
    """全局时间秒 → 段内 y 像素（时间小的在下方，大的在上方）。"""
    progress = (t - col * _SEGMENT_SECONDS) / _SEGMENT_SECONDS
    return _TITLE_HEIGHT + (1 - progress) * _COLUMN_HEIGHT
