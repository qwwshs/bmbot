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
    },
    "Dr3": {
        "Tap": "Dr3_Tap.png",
        "Drag": "Dr3_Tap.png",
        "Hold": "Dr3_Hold.png",
    },
    # 以下皮肤按材质 _MainTex 的 PathID 从 data.unity3d 提取的真实纹理
    # （同名纹理每个皮肤不同图集，如 "drag" 有 258x29/560x44/1780x304/1041x127 四版）
    "Phigros": {
        "Tap": "Berry_Tap.png",
        "Drag": "Phi_Drag.png",
        "Hold": "Phi_Hold.png",
    },
    "Lanota": {
        "Tap": "Berry_Tap.png",
        "Drag": "Lanota_Drag.png",
        "Hold": "Lanota_Hold.png",
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
# 音符素材/段长小于该像素数时不绘制
_MIN_NOTE_PIXELS = 2
# 素材主色统计时视为不透明的 alpha 下限
_ALPHA_THRESHOLD = 100
# Slide（Hold）透明度（0-255，191 = 75%）
_HOLD_ALPHA = round(255 * 0.75)

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


def _easing(name: str, value: float) -> float:
    """easings.net 缓动函数（Quad/Cubic 系列）。"""
    t = min(1.0, max(0.0, value))
    if name == "linear":
        return t
    kind, phase = name[4:], ""
    for prefix in ("easeInOut", "easeOut", "easeIn"):
        if name.startswith(prefix):
            kind, phase = name[len(prefix) :], prefix
            break
    if kind not in ("Quad", "Cubic", "Quart", "Quint", "Sine", "Expo", "Circ"):
        raise _FormulaError(f"不支持的缓动函数 {name}")  # noqa: TRY003
    if phase == "easeIn":
        return _ease_in(kind, t)
    if phase == "easeOut":
        return 1 - _ease_in(kind, 1 - t)
    if t < 0.5:  # noqa: PLR2004
        return _ease_in(kind, 2 * t) / 2
    return 1 - _ease_in(kind, 2 - 2 * t) / 2


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
        return env[node[1]]
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
    _draw_notes(image, draw, chart.notes, columns, skin)

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


def _draw_notes(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    notes: list[Note],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> None:
    """音符绘制：note/ 素材优先，缺失时回退彩色条。

    Hold 统一画到透明叠加层（75% 透明度）最后合成，保证盖在黑线上、
    被 Tap/Drag 盖住。
    """
    hold_overlay: Image.Image | None = None
    for note in notes:
        if note.kind == "Hold":
            if hold_overlay is None:
                hold_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            _draw_hold_polygon(ImageDraw.Draw(hold_overlay), note, columns, skin)
        else:
            for point in note.points:
                if not _draw_note_image(image, note.kind, point, columns, skin):
                    _draw_note_bar(draw, point, NOTE_COLORS[note.kind], columns)
    if hold_overlay is not None:
        image.paste(hold_overlay, (0, 0), hold_overlay)


def _note_pixel_width(width: float) -> float:
    """音符宽度值（x2）→ 像素宽。"""
    return max(1.0, width * (_COLUMN_WIDTH - 2 * _LANE_PAD) / 2)


_note_image_cache: dict[tuple[str, str], Image.Image | None] = {}


def _note_image(kind: str, skin: str = DEFAULT_SKIN) -> Image.Image | None:
    """加载指定皮肤的素材，皮肤/类型缺失时回退 White 对应素材。"""
    key = (skin, kind)
    if key not in _note_image_cache:
        image = None
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


def _draw_note_image(
    image: Image.Image,
    kind: str,
    point: tuple[float, float, float],
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> bool:
    """用指定皮肤素材画 Tap/Drag：宽度拉伸到音符宽度，高度按素材比例。

    Tech 皮肤的 Drag（wipe）染黄色，其余皮肤保持素材本色。
    """
    note_image = _note_image(kind, skin)
    if note_image is None:
        return False
    if kind == "Drag" and skin == "Tech":
        tinted = _tinted_note_image(kind, NOTE_COLORS["Drag"], skin)
        if tinted is not None:
            note_image = tinted
    t, x1, width = point
    col = _note_col(t, columns)
    px = round(_note_x(x1, col))
    py = round(_note_y(t, col))
    target_w = round(_note_pixel_width(width))
    if target_w <= _MIN_NOTE_PIXELS:
        return False
    # 高度按素材宽高比（游戏九宫格上下 border 为 0，高度全拉伸）
    target_h = max(1, round(target_w * note_image.height / note_image.width))
    resized = note_image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    image.paste(resized, (px - resized.width // 2, py - resized.height // 2), resized)
    return True


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


def _draw_hold_polygon(
    draw: ImageDraw.ImageDraw,
    note: Note,
    columns: int,
    skin: str = DEFAULT_SKIN,
) -> None:
    """Slide（Hold）纯色块：中心路径左右各扩半宽画填充多边形。

    颜色取指定皮肤 Hold 素材主色（素材是平条），透明度由叠加层控制。
    """
    color = _hold_color(skin)
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
            draw.ellipse(
                (px - half, py - half, px + half, py + half),
                fill=(*color, _HOLD_ALPHA),
            )
            continue
        lefts = [(px - half, py) for px, py, half in pts]
        rights = [(px + half, py) for px, py, half in reversed(pts)]
        draw.polygon(lefts + rights, fill=(*color, _HOLD_ALPHA))


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
