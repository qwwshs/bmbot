"""Berry Melody 查分卡片渲染（Pillow）。

绘制一张 1080px 宽的深色查分卡片：玩家名、Rating 大数字与公式、
B30 榜单（含曲绘缩略图）、难度 x 等级分布表。
榜单带 OVERFLOW 溢出行与 GOAL 推分目标（推分阈值见 rating.GOAL_INCREASE）。
曲绘从 ``images/`` 目录取 ``曲名_难度.png`` / ``曲名.png``，
缩略图缓存到 ``data/thumbs/``。
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .constants import DATA_DIR
from .rating import (
    ALL_DIFFS,
    GOAL_INCREASE,
    GRADES,
    Chart,
    RatingResult,
    b30_goal_factor,
    get_grade,
    n10_goal_factor,
    target_score_for_cutoff,
    target_score_for_increase,
)

CARD_WIDTH = 1080
PAD = 40
HEADER_H = 296
ROW_H = 96
LIST_TITLE_H = 40
OVERFLOW_H = 44
FOOTER_H = 64
BG_TOP = (34, 34, 34)
BG_BOTTOM = (14, 14, 14)
ACCENT = (215, 215, 215)
TEXT_COLOR = (240, 240, 240)
MUTED_COLOR = (138, 138, 138)
DIVIDER = (58, 58, 58)

# 黑白灰阶：难度用明度区分
DIFF_COLORS = {
    "RL": (226, 226, 226),
    "IL": (198, 198, 198),
    "TT": (170, 170, 170),
    "RU": (142, 142, 142),
    "DM": (114, 114, 114),
    "FL": (86, 86, 86),
}
# 黑白灰阶：等级越高越亮
GRADE_COLORS = {
    "S": (255, 255, 255),
    "AAA+": (216, 216, 216),
    "AAA": (182, 182, 182),
    "AA": (150, 150, 150),
    "A": (120, 120, 120),
    "B": (92, 92, 92),
    "F": (66, 66, 66),
}

IMAGE_DIR = Path(__file__).resolve().parent / "images"
THUMBS_DIR = DATA_DIR / "thumbs"
THUMB_SIZE = 168

# B30 榜单列位置
_RANK_X = 98
_COVER_X = 106
_COVER_SIZE = 72
_NAME_X = 212
_NAME_MAX_W = 260
_DIFF_CX = 505
_CONST_X = 605
_SCORE_X = 775
_GRADE_CX = 865
_POT_X = 1040

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

# 随插件分发的字体目录（fonts/ 下的 otf/ttf/ttc 优先生效，如 Dream Han Sans J W20）
_FONT_DIR = Path(__file__).resolve().parent / "fonts"


@lru_cache(maxsize=1)
def _bundled_font_path() -> tuple[str, int] | None:
    """fonts/ 下第一个字体文件；TTC 合集按名称优先选 J 字面。

    返回 (路径, 字面索引)。
    """
    for ext in ("*.otf", "*.ttf", "*.ttc", "*.otc"):
        paths = sorted(_FONT_DIR.glob(ext))
        if not paths:
            continue
        path = str(paths[0])
        index = 0
        if path.lower().endswith((".ttc", ".otc")):
            for i in range(8):
                try:
                    name = ImageFont.truetype(path, 16, index=i).getname()[0]
                except (OSError, ValueError):
                    break
                if " J" in name:
                    index = i
                    break
        return (path, index)
    return None


# 中日文字体候选：Windows 与常见 Linux 发行版路径
_FONT_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # Linux：Noto CJK（Debian/Ubuntu/Fedora 等）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    # Linux：文泉驿
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    # Linux：AR PL / Droid 兜底
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        # 打包字体优先（仅一个字重，粗体/常规共用），其次粗体/常规对应文件
        bundled = _bundled_font_path()
        if bundled is not None:
            try:
                _font_cache[key] = ImageFont.truetype(
                    bundled[0], size, index=bundled[1]
                )
                return _font_cache[key]
            except OSError:
                pass
        bold_paths = [p for p in _FONT_CANDIDATES if "Bold" in p or "bd" in p]
        regular_paths = [
            p for p in _FONT_CANDIDATES if "Bold" not in p and "bd" not in p
        ]
        # 粗体优先粗体文件，常规优先常规文件（均回退另一类）
        candidates = bold_paths + regular_paths if bold else regular_paths + bold_paths
        for path in candidates:
            if Path(path).exists():
                _font_cache[key] = ImageFont.truetype(path, size)
                break
        else:
            try:
                _font_cache[key] = ImageFont.load_default(size=size)
            except TypeError:
                _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _thumb_path(name: str, diff: str) -> Path:
    digest = hashlib.md5(f"{name}|{diff}".encode()).hexdigest()[:16]
    return THUMBS_DIR / f"{digest}_{diff}.jpg"


def _center_crop_square(image: Image.Image, size: int) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    cropped = image.crop((left, top, left + side, top + side))
    return cropped.resize((size, size), Image.Resampling.LANCZOS)


def load_cover(name: str, diff: str) -> Image.Image | None:
    """按 ``曲名_难度`` → ``曲名`` 查找曲绘并生成缩略图，未找到返回 None。"""
    thumb = _thumb_path(name, diff)
    if thumb.exists():
        try:
            return Image.open(thumb).convert("RGB")
        except OSError:
            pass
    for filename in (f"{name}_{diff}.png", f"{name}.png"):
        source = IMAGE_DIR / filename
        if not source.exists():
            continue
        try:
            with Image.open(source) as im:
                square = _center_crop_square(im.convert("RGB"), THUMB_SIZE)
                THUMBS_DIR.mkdir(parents=True, exist_ok=True)
                square.save(thumb, "JPEG", quality=88)
        except OSError:
            continue
        else:
            return square
    return None


def _rounded_cover(image: Image.Image, radius: int = 16) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, image.width - 1, image.height - 1], radius=radius, fill=255
    )
    rgba = image.convert("RGBA")
    rgba.putalpha(mask)
    return rgba


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int
) -> str:
    text = " ".join(text.split())  # 折叠换行/多余空白为单空格
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _new_background(height: int) -> Image.Image:
    gradient = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        gradient.putpixel(
            (0, y), tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        )
    return gradient.resize((CARD_WIDTH, height))


def _draw_header(
    draw: ImageDraw.ImageDraw,
    y: int,
    player_name: str,
    result: RatingResult,
    archive_potential: object,
) -> int:
    draw.text((PAD, y), "BERRY MELODY · 查分", font=_font(26, bold=True), fill=ACCENT)
    draw.text(
        (CARD_WIDTH - PAD, 72),
        f"{result.rating:.2f}",
        font=_font(92, bold=True),
        fill=(255, 255, 255),
        anchor="ra",
    )
    draw.text(
        (CARD_WIDTH - PAD, 52),
        "RATING",
        font=_font(22, bold=True),
        fill=ACCENT,
        anchor="ra",
    )
    name = _ellipsize(draw, player_name, _font(44, bold=True), 780)
    draw.text((PAD, 84), name, font=_font(44, bold=True), fill=TEXT_COLOR)
    stats = [
        f"共 {result.total_charts} 首成绩",
        f"B30 计入 {len(result.b30_charts)} 首",
        f"N10 计入 {len(result.n10_charts)} 首",
    ]
    if archive_potential is not None:
        stats.append(f"存档内建 Potential: {archive_potential}")
    stats_text = _ellipsize(draw, "  ·  ".join(stats), _font(24), CARD_WIDTH - 2 * PAD)
    draw.text((PAD, 196), stats_text, font=_font(24), fill=MUTED_COLOR)
    # 未收录提示单独一行，仅在有未收录成绩时显示
    if result.missing:
        draw.text(
            (PAD, 236),
            f"⚠️ {len(result.missing)} 首成绩未在定数表中，不计入",
            font=_font(22),
            fill=MUTED_COLOR,
        )
    draw.line([(PAD, HEADER_H), (CARD_WIDTH - PAD, HEADER_H)], fill=DIVIDER, width=2)
    return HEADER_H


def _draw_row(  # noqa: PLR0913, PLR0917
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    rank: int,
    chart: Chart,
    goal: int | None,
) -> int:
    """绘制一行成绩；``goal`` 为推分目标分数（-1 表示无法推分）。"""
    cy = y + ROW_H / 2
    draw.text((_RANK_X, cy), str(rank), font=_font(26), fill=MUTED_COLOR, anchor="rm")
    cover = load_cover(chart.name, chart.diff)
    if cover is not None:
        # 缓存缩略图为 168px，显示时缩到 72px 再粘贴，避免盖住下一行
        cover = cover.resize((_COVER_SIZE, _COVER_SIZE), Image.Resampling.LANCZOS)
        cover_img = _rounded_cover(cover)
        img.paste(cover_img, (_COVER_X, y + 12), cover_img)
    else:
        draw.rounded_rectangle(
            [_COVER_X, y + 12, _COVER_X + _COVER_SIZE, y + 12 + _COVER_SIZE],
            radius=14,
            fill=(38, 38, 38),
        )
        draw.text(
            (_COVER_X + _COVER_SIZE // 2, cy),
            "♪",
            font=_font(34),
            fill=MUTED_COLOR,
            anchor="mm",
        )
    name = _ellipsize(draw, chart.original_name or chart.name, _font(30), _NAME_MAX_W)
    draw.text((_NAME_X, cy), name, font=_font(30), fill=TEXT_COLOR, anchor="lm")
    draw.text(
        (_DIFF_CX, cy),
        chart.diff,
        font=_font(24, bold=True),
        fill=DIFF_COLORS.get(chart.diff, MUTED_COLOR),
        anchor="mm",
    )
    draw.text(
        (_CONST_X, cy),
        f"{chart.constant:.1f}",
        font=_font(28),
        fill=TEXT_COLOR,
        anchor="rm",
    )
    draw.text(
        (_SCORE_X, cy),
        f"{chart.score}",
        font=_font(28),
        fill=TEXT_COLOR,
        anchor="rm",
    )
    grade = get_grade(chart.score)
    draw.text(
        (_GRADE_CX, cy),
        grade,
        font=_font(24, bold=True),
        fill=GRADE_COLORS[grade],
        anchor="mm",
    )
    draw.text(
        (_POT_X, cy),
        f"{chart.potential:.3f}",
        font=_font(28),
        fill=TEXT_COLOR,
        anchor="rm",
    )
    if goal is not None:
        draw.text(
            (_SCORE_X, cy + 21),
            f"GOAL {goal}" if goal > 0 else "GOAL 无法推分",
            font=_font(18),
            fill=MUTED_COLOR,
            anchor="rm",
        )
    draw.line([(PAD, y + ROW_H), (CARD_WIDTH - PAD, y + ROW_H)], fill=DIVIDER, width=1)
    return y + ROW_H


def _draw_overflow_separator(draw: ImageDraw.ImageDraw, y: int) -> int:
    """OVERFLOW 溢出行分隔条。"""
    draw.line([(PAD, y), (CARD_WIDTH - PAD, y)], fill=DIVIDER, width=2)
    cy = y + OVERFLOW_H / 2
    draw.text(
        (CARD_WIDTH / 2, cy),
        "OVERFLOW",
        font=_font(24, bold=True),
        fill=MUTED_COLOR,
        anchor="mm",
    )
    return y + OVERFLOW_H


def _draw_chart_list(  # noqa: PLR0913, PLR0917
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    title: str,
    subtitle: str,
    charts: list,
    empty_text: str,
    goal_factors: float | list[float] | None = None,
    overflow: list | None = None,
    overflow_factor: float = 0.0,
) -> int:
    y += 24
    draw.text((PAD, y), title, font=_font(26, bold=True), fill=TEXT_COLOR)
    if subtitle:
        draw.text(
            (CARD_WIDTH - PAD, y),
            subtitle,
            font=_font(22),
            fill=MUTED_COLOR,
            anchor="ra",
        )
    draw.text((_NAME_X, y), "曲名", font=_font(22), fill=MUTED_COLOR)
    draw.text((_DIFF_CX, y), "难度", font=_font(22), fill=MUTED_COLOR, anchor="ma")
    draw.text((_CONST_X, y), "定数", font=_font(22), fill=MUTED_COLOR, anchor="ra")
    draw.text((_SCORE_X, y), "分数", font=_font(22), fill=MUTED_COLOR, anchor="ra")
    draw.text((_GRADE_CX, y), "等级", font=_font(22), fill=MUTED_COLOR, anchor="ma")
    draw.text((_POT_X, y), "Potential", font=_font(22), fill=MUTED_COLOR, anchor="ra")
    y += LIST_TITLE_H

    if not charts:
        draw.text(
            (CARD_WIDTH / 2, y + ROW_H / 2),
            empty_text,
            font=_font(30),
            fill=MUTED_COLOR,
            anchor="mm",
        )
        return y + ROW_H + 20

    # 每行推分权重：单值对所有行生效，列表按行取值（N10 前 5/后 5 权重不同）
    factors: list[float] | None = (
        [goal_factors] * len(charts)
        if isinstance(goal_factors, float)
        else goal_factors
    )
    for index, chart in enumerate(charts):
        goal = None
        if factors:
            goal = target_score_for_increase(
                chart.score,
                chart.potential,
                chart.constant,
                GOAL_INCREASE,
                factors[index],
            )
        y = _draw_row(img, draw, y, index + 1, chart, goal)

    if overflow:
        y = _draw_overflow_separator(draw, y)
        cutoff_potential = charts[-1].potential
        for i, chart in enumerate(overflow):
            goal = target_score_for_cutoff(
                chart.score,
                chart.constant,
                GOAL_INCREASE,
                overflow_factor,
                cutoff_potential,
            )
            y = _draw_row(img, draw, y, len(charts) + i + 1, chart, goal)
    return y + 20


def _draw_matrix(
    draw: ImageDraw.ImageDraw, y: int, grade_counts: dict[str, dict[str, int]]
) -> int:
    draw.text((PAD, y), "等级分布", font=_font(26, bold=True), fill=TEXT_COLOR)
    y += 40
    col_rights: list[int] = []
    col_x = 140
    for diff in ALL_DIFFS:
        draw.text(
            (col_x + 75, y),
            diff,
            font=_font(24, bold=True),
            fill=DIFF_COLORS[diff],
            anchor="ma",
        )
        col_rights.append(col_x + 126)
        col_x += 150
    draw.text((PAD, y), "等级", font=_font(24), fill=MUTED_COLOR)
    y += 40
    for grade in GRADES:
        draw.text(
            (PAD, y + 20),
            grade,
            font=_font(24, bold=True),
            fill=GRADE_COLORS[grade],
            anchor="lm",
        )
        for diff, right in zip(ALL_DIFFS, col_rights):
            draw.text(
                (right, y + 20),
                str(grade_counts[diff][grade]),
                font=_font(24),
                fill=TEXT_COLOR,
                anchor="rm",
            )
        y += 40
    return y + 16


def _draw_footer(draw: ImageDraw.ImageDraw, y: int) -> int:
    draw.line([(PAD, y), (CARD_WIDTH - PAD, y)], fill=DIVIDER, width=2)
    y += 28
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    draw.text(
        (PAD, y),
        f"Berry Melody 查分 · 生成于 {timestamp}",
        font=_font(22),
        fill=MUTED_COLOR,
    )
    return y + 40


def render_card(
    player_name: str,
    result: RatingResult,
    grade_counts: dict[str, dict[str, int]],
    archive_potential: object = None,
) -> bytes:
    """渲染查分卡片，返回 PNG 字节。"""
    charts = result.b30_charts
    overflow_b = result.b30_overflow
    overflow_n = result.n10_overflow
    b30_list_h = LIST_TITLE_H + ROW_H * max(len(charts), 1) + 20
    if overflow_b:
        b30_list_h += OVERFLOW_H + ROW_H * len(overflow_b)
    n10_list_h = LIST_TITLE_H + ROW_H * max(len(result.n10_charts), 1) + 20
    if overflow_n:
        n10_list_h += OVERFLOW_H + ROW_H * len(overflow_n)
    matrix_h = 36 + 36 + len(GRADES) * 40 + 16
    height = PAD + HEADER_H + b30_list_h + n10_list_h + matrix_h + FOOTER_H + PAD

    img = _new_background(height)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, CARD_WIDTH, 8], fill=ACCENT)

    y = _draw_header(draw, PAD, player_name, result, archive_potential)
    y = _draw_chart_list(
        img,
        draw,
        y,
        "BEST 30",
        "",
        charts,
        "暂无成绩数据",
        goal_factors=b30_goal_factor(),
        overflow=overflow_b,
        overflow_factor=b30_goal_factor(),
    )
    n10_factors = [n10_goal_factor(i) for i in range(len(result.n10_charts))]
    y = _draw_chart_list(
        img,
        draw,
        y,
        "N10",
        "",
        result.n10_charts,
        "暂无 N10 成绩",
        goal_factors=n10_factors,
        overflow=overflow_n,
        # 溢出曲挤掉的是后 5 首（权重 0.4）
        overflow_factor=n10_goal_factor(5),
    )
    y = _draw_matrix(draw, y, grade_counts)
    _draw_footer(draw, y)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ================================================================
# 定数表图（bmchart）：按定数分列，列内歌曲每行 5 首
# ================================================================

# 难度代表色（用户指定，ARGB 去掉 alpha 前缀）
CHART_DIFF_COLORS = {
    "RU": (179, 87, 255),
    "RL": (0, 202, 255),
    "TT": (255, 81, 79),
    "IL": (255, 196, 0),
    "DM": (0, 233, 168),
    "FL": (0, 233, 168),
}

_CHART_CARD = 140  # 卡片总边长（曲绘 4:3 + 底部矩形 = 1:1 方形）
_CHART_COVER_H = 105  # 曲绘高度（宽 4 : 高 3）
_CHART_LABEL_H = _CHART_CARD - _CHART_COVER_H  # 底部难度色矩形高（补齐 1:1）
_CHART_PER_ROW = 5  # 每行歌曲数
_CHART_GAP = 10
_CHART_PAD = 14
_CHART_HEADER_W = 110  # 每行最左定数表头宽度
_CHART_TITLE_LINES = 2  # 曲名最多行数（固定占位高度）
_CHART_TITLE_MAX = 11  # 曲名字号上限
_CHART_TITLE_MIN = 6  # 曲名字号下限（放不下时截断）
_CHART_BG = (0, 0, 0)  # 纯黑背景
_CHART_TEXT = (255, 255, 255)
_CHART_DIVIDER = (60, 60, 60)
_CHART_BAND_LINE = (128, 128, 128)  # 定数行分割线：白色 50% alpha（黑底合成）
_CHART_PLACEHOLDER = (40, 40, 40)


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_w: int,
    max_lines: int,
) -> list[str] | None:
    """字符级换行；超出 max_lines 行返回 None。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        if not current:
            current = ch
            continue
        if draw.textlength(current + ch, font=font) <= max_w:
            current += ch
        else:
            lines.append(current)
            current = ch
            if len(lines) >= max_lines:
                return None
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return None
    return lines


def _fit_chart_title(  # noqa: PLR0913, PLR0917
    draw: ImageDraw.ImageDraw,
    text: str,
    max_w: int,
    max_size: int = _CHART_TITLE_MAX,
    min_size: int = _CHART_TITLE_MIN,
    max_lines: int = _CHART_TITLE_LINES,
) -> tuple[list[str], int]:
    """曲名换行适配：字号递减到可在固定行数内放下，放不下截断。"""
    for size in range(max_size, min_size - 1, -1):
        font = _font(size)
        lines = _wrap_lines(draw, text, font, max_w, max_lines)
        if lines is not None:
            return lines, size
    font = _font(min_size)
    truncated = text
    while True:
        lines = _wrap_lines(draw, truncated + "…", font, max_w, max_lines)
        if lines is not None:
            return lines, min_size
        if not truncated:
            return [""], min_size
        truncated = truncated[:-1]


def _draw_chart_card(  # noqa: PLR0913, PLR0917
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    song: str,
    diff: str,
) -> None:
    """绘制单张定数表卡：4:3 曲绘 + 底部难度色矩形，整体 1:1 方形。"""
    cover = load_cover(song, diff)
    if cover is not None:
        # 方形缩略图中心裁剪为 4:3 横条，再缩放到卡片曲绘区
        cover_w, cover_h = cover.size
        target_h = int(cover_w * 3 / 4)
        top = (cover_h - target_h) // 2
        cover = cover.crop((0, top, cover_w, top + target_h))
        cover = cover.resize((_CHART_CARD, _CHART_COVER_H), Image.Resampling.LANCZOS)
        img.paste(cover, (x, y))
    else:
        draw.rectangle(
            [x, y, x + _CHART_CARD - 1, y + _CHART_COVER_H - 1],
            fill=_CHART_PLACEHOLDER,
        )
        draw.text(
            (x + _CHART_CARD // 2, y + _CHART_COVER_H // 2),
            "♪",
            font=_font(40),
            fill=(120, 120, 120),
            anchor="mm",
        )
    color = CHART_DIFF_COLORS.get(diff, (140, 140, 140))
    label_y = y + _CHART_COVER_H
    draw.rectangle(
        [x, label_y, x + _CHART_CARD - 1, label_y + _CHART_LABEL_H - 1], fill=color
    )
    # 曲名区固定两行高度（字号自适应），难度固定底部一行
    max_w = _CHART_CARD - 6
    lines, size = _fit_chart_title(draw, song, max_w)
    line_h = size + 1
    for index, line in enumerate(lines):
        draw.text(
            (x + _CHART_CARD // 2, label_y + 1 + index * line_h),
            line,
            font=_font(size),
            fill=(255, 255, 255),
            anchor="ma",
        )
    draw.text(
        (x + _CHART_CARD // 2, label_y + _CHART_LABEL_H - 3),
        diff,
        font=_font(9),
        fill=(255, 255, 255),
        anchor="ms",
    )


def render_chart_table(charts: list[tuple[float, str, str]]) -> bytes:
    """绘制定数表图：每个定数一行（最左为定数表头），行内歌曲每行 5 首。

    ``charts`` 为 ``(定数, 曲名, 难度)`` 列表；纯黑背景。
    """
    groups: dict[float, list[tuple[str, str]]] = {}
    for constant, song, diff in charts:
        groups.setdefault(constant, []).append((song, diff))
    ordered = sorted(groups.items(), key=lambda item: -item[0])

    card_h = _CHART_CARD + _CHART_GAP  # 卡 1:1 + 行间距
    width = (
        _CHART_PAD
        + _CHART_HEADER_W
        + _CHART_PER_ROW * _CHART_CARD
        + (_CHART_PER_ROW - 1) * _CHART_GAP
        + _CHART_PAD
    )
    band_heights: list[int] = []
    for _constant, items in ordered:
        rows = (len(items) + _CHART_PER_ROW - 1) // _CHART_PER_ROW
        band_heights.append(rows * _CHART_CARD + max(rows - 1, 0) * _CHART_GAP)
    height = (
        _CHART_PAD
        + sum(band_heights)
        + _CHART_GAP * max(len(ordered) - 1, 0)
        + _CHART_PAD
    )

    img = Image.new("RGB", (width, height), _CHART_BG)
    draw = ImageDraw.Draw(img)
    y = _CHART_PAD
    for index, ((constant, items), band_h) in enumerate(zip(ordered, band_heights)):
        # 最左定数表头（垂直居中）
        draw.text(
            (_CHART_PAD + _CHART_HEADER_W // 2, y + band_h // 2),
            f"{constant:.1f}",
            font=_font(22, bold=True),
            fill=_CHART_TEXT,
            anchor="mm",
        )
        # 表头与卡片区竖分隔线
        divider_x = _CHART_PAD + _CHART_HEADER_W
        draw.line(
            [(divider_x, y), (divider_x, y + band_h)],
            fill=_CHART_DIVIDER,
            width=1,
        )
        for card_index, (song, diff) in enumerate(items):
            col = card_index % _CHART_PER_ROW
            row = card_index // _CHART_PER_ROW
            # 右移 2px 露出表头分隔线
            cx = divider_x + 2 + col * (_CHART_CARD + _CHART_GAP)
            cy = y + row * card_h
            _draw_chart_card(img, draw, cx, cy, song, diff)
        y += band_h
        # 相邻定数行之间的分割线（白色 50% alpha；最后一行之后不画）
        if index < len(ordered) - 1:
            line_y = y + _CHART_GAP // 2
            draw.line(
                [(_CHART_PAD, line_y), (width - _CHART_PAD, line_y)],
                fill=_CHART_BAND_LINE,
                width=1,
            )
            y += _CHART_GAP

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ================================================================
# 帮助图（/bmhelp）
# ================================================================

_HELP_BG = (18, 18, 18)
_HELP_PAD = 36
_HELP_LINE_H = 40
_HELP_TITLE = (255, 255, 255)
_HELP_CMD = (245, 245, 245)
_HELP_DESC = (170, 170, 170)
_HELP_MUTED = (140, 140, 140)
_HELP_SEP = (90, 90, 90)


def render_help_image(text: str) -> bytes:
    """把 /bmhelp 文本渲染为深色背景图片，返回 PNG 字节。

    样式：标题白色粗体；``/`` 开头的命令行命令名白色 + 说明灰色；
    缩进/``·`` 说明行与页脚灰色；``━━`` 分隔线深灰。
    """
    entries: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int], bool]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("━━"):
            entries.append((line, _font(18), _HELP_SEP, False))
        elif stripped.startswith("🎵"):
            entries.append((line, _font(30, bold=True), _HELP_TITLE, False))
        elif stripped.startswith("/"):
            entries.append((line, _font(24), _HELP_CMD, True))
        else:
            entries.append((line, _font(22), _HELP_MUTED, False))

    max_w = max(_font(24).getlength(line) for line, _, _, _ in entries)
    width = int(max_w) + 2 * _HELP_PAD
    height = _HELP_PAD * 2 + len(entries) * _HELP_LINE_H
    img = Image.new("RGB", (width, height), _HELP_BG)
    draw = ImageDraw.Draw(img)
    y = _HELP_PAD
    for line, font, color, is_command in entries:
        if is_command:
            # 命令名白色 + 描述灰色（按 "—" 分割）
            cmd, sep, desc = line.partition("—")
            draw.text((_HELP_PAD, y), cmd, font=font, fill=_HELP_TITLE)
            if sep:
                dx = font.getlength(cmd)
                draw.text((_HELP_PAD + dx, y), sep + desc, font=font, fill=_HELP_DESC)
        else:
            draw.text((_HELP_PAD, y), line, font=font, fill=color)
        y += _HELP_LINE_H

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ================================================================
# 列表图（bmsong 搜索结果 / bmcharter 候选 / 别名 / 谱师关联）
# ================================================================

_LIST_BG = (18, 18, 18)
_LIST_PAD = 32
_LIST_LINE_H = 38
_LIST_HEADER = (255, 255, 255)
_LIST_TEXT = (245, 245, 245)
_LIST_MUTED = (150, 150, 150)


def render_list_image(text: str) -> bytes:
    """把列表文本渲染为深色背景图片，返回 PNG 字节。

    样式：🎵/🔍/📋/📝 开头的标题行白色粗体；「—/…/（」开头的提示行灰色；
    其余行（序号列表项等）白色；宽度按最长行自适应。
    """
    entries: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("🎵", "🔍", "📋", "📝")):
            entries.append((line, _font(24, bold=True), _LIST_HEADER))
        elif stripped.startswith(("—", "…", "（")):
            entries.append((line, _font(20), _LIST_MUTED))
        else:
            entries.append((line, _font(22), _LIST_TEXT))
    if not entries:
        return b""
    max_w = max(font.getlength(line) for line, font, _ in entries)
    width = int(max_w) + 2 * _LIST_PAD
    height = _LIST_PAD * 2 + len(entries) * _LIST_LINE_H
    img = Image.new("RGB", (width, height), _LIST_BG)
    draw = ImageDraw.Draw(img)
    y = _LIST_PAD
    for line, font, color in entries:
        draw.text((_LIST_PAD, y), line, font=font, fill=color)
        y += _LIST_LINE_H

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ================================================================
# 新版查分卡（/bmrating 默认样式）
# ================================================================

CARD2_PLATE_H = 220  # 底板高度（保持 2:1 不放大）
CARD2_PLATE_W = 2 * CARD2_PLATE_H  # 底板宽度（2:1）
CARD2_COVER = int(CARD2_PLATE_H * 0.9)  # 曲绘 1:1 边长 = 底板高的 0.9
CARD2_COVER_M = (CARD2_PLATE_H - CARD2_COVER) // 2  # 曲绘左/上/下与底板边缘等距
CARD2_TEXT_GAP = 12  # 曲绘右缘与右侧文字区间距
CARD2_TEXT_PAD = 16  # 右侧文字区右缘内边距
CARD2_RADIUS = 10  # 底板圆角半径
CARD2_BASE = 30  # 普通文字字号（GOAL/定数，加粗）
CARD2_SCORE = 44  # 分数字号（加粗；48 时 7 位满分溢出文字区）
CARD2_TITLE_MAX = 33  # 曲名字号（原 22 的 1.5 倍；单行，过长截断加 …）
CARD2_GOAL = (204, 204, 204)  # GOAL 行颜色：alpha 80%
CARD2_INK_GAP = 10  # 文字行间墨迹最小间距
CARD2_GRADE_H = 60  # 评级图高度（原 90 的 2/3），放底板右下角
CARD2_GRADE_M = 10  # 评级图右下边距（避开上方 GOAL 行）
CARD2_GAP = 12  # 卡片间距
CARD2_PER_ROW = 5  # 每行卡片数
CARD2_PAD = 30
CARD2_BG = (55, 55, 55)  # 整张查分图背景 #FF373737
CARD2_TEXT = (240, 240, 240)
CARD2_MUTED = (150, 150, 150)
CARD2_DIVIDER = (70, 70, 70)
CARD2_ACCENT = (220, 220, 220)

# 头部：角色头像 + 名字条 + Rating 徽章
CARD2_AVATAR = 160  # 头像边长（原 320 的 1/2）
CARD2_AVATAR_X = CARD2_AVATAR // 4  # 头像右移 1/4
CARD2_AVATAR_TOP = CARD2_AVATAR // 4  # 头像顶部边距
CARD2_NAME_BAR_H = CARD2_AVATAR // 2  # 名字条高度 = 头像 1/2
CARD2_NAME_BAR_W = CARD2_AVATAR * 4  # 名字条长度 = 头像 4 倍（x 从图片起点开始）
CARD2_NAME_BAR = (40, 40, 40)  # 名字条颜色（图层在头像之下）
CARD2_NAME_FONT = CARD2_NAME_BAR_H // 2 - 8  # 玩家名字号
CARD2_RATING_BADGE_W = CARD2_NAME_BAR_W // 5  # Rating 徽章宽 = 名字条 1/5
CARD2_RATING_BADGE_H = CARD2_NAME_BAR_H // 2  # Rating 徽章高 = 名字条 1/2
CARD2_RATING_BADGE = (255, 24, 48)  # Rating 徽章颜色 #FF1830
CARD2_RATING_FONT = CARD2_RATING_BADGE_H * 3 // 5  # Rating 徽章字
CARD2_MATRIX_PAD = 8  # 等级分布黑底白框内边距


_avatar_cache: dict[str, Image.Image | None] = {}

# 存档角色 Path → images/ 头像文件名（头像文件名多为角色 EName，少数特例：
# Anki 用 Path、RA-S 去连字符、Helios 带 1、另附 EName 形式兜底）
CHAR_AVATAR_FILES = {
    "Elidia": "Elodea",
    "Orila": "Ayira",
    "Chao": "Presagio",
    "Coin": "Arcas",
    "Reporter": "Moneta",
    "qqx": "Asher Spencer",
    "Bat": "Shiroma",
    "Jinuo": "Gino",
    "Jiu": "Geopelia",
    "WuXi": "Dancy",
    "RA-S": "Ras",
    "Hitomi": "Hitomi Miyafuji",
    "Rina": "Rina Izuki",
    "Helios": "Helios1",
    "LWY": "Luvia",
    "Luna": "Luna Ambrosia",
    "Winebibber": "XJX",
    # EName 形式兜底（存档若直接存英文名）
    "Ankie": "Anki",
    "Sèat": "Seat",
    "R.R.": "RR",
}


def _load_avatar(name: str | None) -> Image.Image | None:
    """加载角色头像（images/<角色名>.png，名称不一致时查 CHAR_AVATAR_FILES）。"""
    if not name:
        return None
    if name not in _avatar_cache:
        image = None
        filename = CHAR_AVATAR_FILES.get(name, name)
        path = IMAGE_DIR / f"{filename}.png"
        if path.exists():
            try:
                with Image.open(path) as im:
                    image = im.convert("RGBA").resize(
                        (CARD2_AVATAR, CARD2_AVATAR), Image.Resampling.LANCZOS
                    )
            except OSError:
                image = None
        _avatar_cache[name] = image
    return _avatar_cache[name]


def _draw_grade_matrix(
    draw: ImageDraw.ImageDraw,
    y: int,
    grade_counts: dict,
    right: int | None = None,
) -> int:
    """等级分布表：字号与曲名相同（CARD2_TITLE_MAX），黑色背景无边框。

    ``right`` 为空时靠左（CARD2_PAD），否则整表右对齐到 ``right``；
    等级行用文字（等级色）。
    返回外框底边 y。
    """
    font = _font(CARD2_TITLE_MAX, bold=True)
    label_w = 100
    col_w = 132
    row_h = 44
    pad = CARD2_MATRIX_PAD
    matrix_w = label_w + len(ALL_DIFFS) * col_w
    left = CARD2_PAD if right is None else right - matrix_w
    box_top = y - pad
    box_bottom = y + (1 + len(GRADES)) * row_h + pad
    # 黑色背景
    draw.rectangle(
        [left - pad, box_top, left + matrix_w + pad, box_bottom],
        fill=(0, 0, 0),
    )
    cy = y + 20
    draw.text((left, cy), "等级", font=font, fill=CARD2_MUTED, anchor="lm")
    for index, diff in enumerate(ALL_DIFFS):
        draw.text(
            (left + label_w + index * col_w + col_w // 2, cy),
            diff,
            font=font,
            fill=CARD2_TEXT,
            anchor="mm",
        )
    y += row_h
    for grade in GRADES:
        cy = y + row_h // 2
        draw.text(
            (left, cy),
            grade,
            font=font,
            fill=GRADE_COLORS[grade],
            anchor="lm",
        )
        for index, diff in enumerate(ALL_DIFFS):
            draw.text(
                (left + label_w + index * col_w + col_w // 2, cy),
                str(grade_counts[diff][grade]),
                font=font,
                fill=CARD2_TEXT,
                anchor="mm",
            )
        y += row_h
    return box_bottom


SCORE_DIR = Path(__file__).resolve().parent / "score"

# 评级图片（score/ 下小写文件名；F 无图片回退文字）
_GRADE_IMAGE_FILES = {
    "S": "s.png",
    "AAA+": "aaa+.png",
    "AAA": "aaa.png",
    "AA": "aa.png",
    "A": "a.png",
    "B": "b.png",
}
_grade_image_cache: dict[tuple[str, int], Image.Image | None] = {}


def _load_grade_image(grade: str, height: int = CARD2_GRADE_H) -> Image.Image | None:
    """从 score/ 加载评级图片（裁剪透明区并缩放到指定高度），未找到返回 None。"""
    key = (grade, height)
    if key not in _grade_image_cache:
        image = None
        filename = _GRADE_IMAGE_FILES.get(grade)
        if filename:
            path = SCORE_DIR / filename
            if path.exists():
                try:
                    rgba = Image.open(path).convert("RGBA")
                    bbox = rgba.getchannel("A").getbbox()
                    if bbox:
                        rgba = rgba.crop(bbox)
                    width = max(1, round(rgba.width * height / rgba.height))
                    image = rgba.resize((width, height), Image.Resampling.LANCZOS)
                except OSError:
                    image = None
        _grade_image_cache[key] = image
    return _grade_image_cache[key]


_ink_cache: dict[tuple[str, int, bool], tuple[int, int]] = {}


def _text_ink(text: str, size: int, *, bold: bool = True) -> tuple[int, int]:
    """文字墨迹相对锚点 y（anchor='la'）的 (上, 下) 偏移，用于行间贴紧。"""
    key = (text, size, bold)
    hit = _ink_cache.get(key)
    if hit is not None:
        return hit
    font = _font(size, bold=bold)
    pad = 8
    probe = Image.new(
        "RGB", (int(font.getlength(text)) + pad * 2, size * 2 + pad * 2), (0, 0, 0)
    )
    probe_draw = ImageDraw.Draw(probe)
    probe_draw.text((pad, pad), text, font=font, fill=(255, 255, 255))
    top = bottom = None
    for yy in range(probe.height):
        for xx in range(probe.width):
            if probe.getpixel((xx, yy)) != (0, 0, 0):
                if top is None:
                    top = yy
                bottom = yy
                break
    hit = (top - pad, bottom - pad)
    _ink_cache[key] = hit
    return hit


def _draw_rating_card_new(  # noqa: PLR0913, PLR0917
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    chart: Chart,
    goal: int | None,
) -> None:
    """新版单曲卡：2:1 不透明圆角底板，内嵌 0.9 高曲绘（四边等距）。

    右侧文字区：曲名上边缘与曲绘齐平、行间墨迹间隔 10px（无分割线、无序号）：
    曲名（单行，过长截断加 …）/ 分数 / ->GOAL（alpha 80%）/ 定数；
    评级图片放底板右下角（2/3 大小）。
    """
    color = CHART_DIFF_COLORS.get(chart.diff, (140, 140, 140))
    draw.rounded_rectangle(
        [x, y, x + CARD2_PLATE_W - 1, y + CARD2_PLATE_H - 1],
        radius=CARD2_RADIUS,
        fill=color,
    )
    # 曲绘：0.9 底板高，左边缘/上边缘到底板距离相同（下边缘亦然）
    cx = x + CARD2_COVER_M
    cy = y + CARD2_COVER_M
    cover = load_cover(chart.name, chart.diff)
    if cover is not None:
        cover = cover.resize((CARD2_COVER, CARD2_COVER), Image.Resampling.LANCZOS)
        img.paste(cover, (cx, cy))
    else:
        draw.rectangle(
            [cx, cy, cx + CARD2_COVER - 1, cy + CARD2_COVER - 1],
            fill=(40, 40, 40),
        )
        draw.text(
            (cx + CARD2_COVER // 2, cy + CARD2_COVER // 2),
            "♪",
            font=_font(40),
            fill=(120, 120, 120),
            anchor="mm",
        )

    # 右侧文字区：曲名上边缘与曲绘齐平，行间墨迹间隔 CARD2_INK_GAP
    lx = cx + CARD2_COVER + CARD2_TEXT_GAP
    right = x + CARD2_PLATE_W - CARD2_TEXT_PAD
    ink_top = y + CARD2_COVER_M

    # 曲名：单行，过长截断加 …
    title_font = _font(CARD2_TITLE_MAX, bold=True)
    title_text = _ellipsize(
        draw, chart.original_name or chart.name, title_font, right - lx
    )
    t_top, t_bottom = _text_ink(title_text, CARD2_TITLE_MAX, bold=True)
    draw.text((lx, ink_top - t_top), title_text, font=title_font, fill=(255, 255, 255))
    ink_top += t_bottom - t_top + CARD2_INK_GAP

    # 分数：紧贴曲名下方
    score_text = f"{chart.score}"
    s_top, s_bottom = _text_ink(score_text, CARD2_SCORE, bold=True)
    draw.text(
        (lx, ink_top - s_top),
        score_text,
        font=_font(CARD2_SCORE, bold=True),
        fill=(255, 255, 255),
    )
    ink_top += s_bottom - s_top + CARD2_INK_GAP

    # GOAL：->目标分数（alpha 80%）；无法推分显示 ->当前分数
    if goal is not None:
        goal_text = f"->{goal}" if goal > 0 else f"->{chart.score}"
        g_top, g_bottom = _text_ink(goal_text, CARD2_BASE, bold=True)
        draw.text(
            (lx, ink_top - g_top),
            goal_text,
            font=_font(CARD2_BASE, bold=True),
            fill=CARD2_GOAL,
        )
        ink_top += g_bottom - g_top + CARD2_INK_GAP

    # 定数（左）
    const_text = f"{chart.constant:.1f}"
    c_top, _ = _text_ink(const_text, CARD2_BASE, bold=True)
    draw.text(
        (lx, ink_top - c_top),
        const_text,
        font=_font(CARD2_BASE, bold=True),
        fill=(255, 255, 255),
    )
    # 评级图片：底板右下角
    grade_image = _load_grade_image(get_grade(chart.score))
    if grade_image is not None:
        img.paste(
            grade_image,
            (
                right - grade_image.width,
                y + CARD2_PLATE_H - CARD2_GRADE_M - grade_image.height,
            ),
            grade_image,
        )


def _draw_rating_section_new(  # noqa: PLR0913, PLR0917
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    title: str,
    charts: list,
    rows: int,
    goal_factors: list[float] | None,
) -> int:
    """新版分区：标题 + 分割线 + 网格（每行 5 首，含 GOAL 推分目标）。"""
    y += 24
    draw.text((CARD2_PAD, y), title, font=_font(30, bold=True), fill=CARD2_TEXT)
    y += 34
    draw.line([(CARD2_PAD, y), (img.width - CARD2_PAD, y)], fill=CARD2_DIVIDER, width=2)
    y += 14
    card_w = CARD2_PLATE_W + CARD2_GAP
    for index, chart in enumerate(charts):
        col = index % CARD2_PER_ROW
        row = index // CARD2_PER_ROW
        cx = CARD2_PAD + col * card_w
        cy = y + row * (CARD2_PLATE_H + CARD2_GAP)
        goal = None
        if goal_factors:
            goal = target_score_for_increase(
                chart.score,
                chart.potential,
                chart.constant,
                GOAL_INCREASE,
                goal_factors[index],
            )
        _draw_rating_card_new(img, draw, cx, cy, chart, goal)
    return y + rows * (CARD2_PLATE_H + CARD2_GAP)


def render_card_new(
    player_name: str,
    result: RatingResult,
    grade_counts: dict[str, dict[str, int]],
    _archive_potential: object = None,
    character: str | None = None,
) -> bytes:
    """渲染新版查分卡，返回 PNG 字节。

    头部：左上角角色头像（按存档 CharSelect 选择）+ 名字条（玩家名居中），
    名字条右下角 Rating 徽章，右上角等级分布表（等级行用 score/ 图片）；
    B30/N10 网格（每行 5 首）。
    """
    card_w = CARD2_PLATE_W
    width = CARD2_PER_ROW * card_w + (CARD2_PER_ROW - 1) * CARD2_GAP + 2 * CARD2_PAD
    b30_rows = (len(result.b30_charts) + CARD2_PER_ROW - 1) // CARD2_PER_ROW
    n10_rows = (len(result.n10_charts) + CARD2_PER_ROW - 1) // CARD2_PER_ROW
    grid_h = CARD2_PLATE_H + CARD2_GAP
    section_h = 24 + 34 + 14
    matrix_h = (1 + len(GRADES)) * 44 + 2 * CARD2_MATRIX_PAD
    header_h = max(
        CARD2_AVATAR_TOP + CARD2_AVATAR,
        CARD2_AVATAR_TOP + matrix_h - CARD2_MATRIX_PAD,
    )
    height = (
        header_h
        + 24
        + section_h
        + b30_rows * grid_h
        + section_h
        + n10_rows * grid_h
        + CARD2_PAD
    )

    img = Image.new("RGB", (width, height), CARD2_BG)
    draw = ImageDraw.Draw(img)

    # ---------- 头部 ----------
    avatar = _load_avatar(character)
    avatar_x = CARD2_AVATAR_X
    avatar_y = CARD2_AVATAR_TOP
    bar_top = avatar_y + (CARD2_AVATAR - CARD2_NAME_BAR_H) // 2
    bar_bottom = bar_top + CARD2_NAME_BAR_H
    # 名字条：x 从整图起点 0 到 4 倍头像长，纵坐标中点与头像相同，图层在头像之下
    draw.rectangle([0, bar_top, CARD2_NAME_BAR_W, bar_bottom], fill=CARD2_NAME_BAR)
    # 玩家名：以头像横坐标中点为起点、名字条终点为终点居中
    name_mid = avatar_x + CARD2_AVATAR // 2
    name = _ellipsize(
        draw,
        player_name,
        _font(CARD2_NAME_FONT, bold=True),
        CARD2_NAME_BAR_W - name_mid - 16,
    )
    draw.text(
        ((name_mid + CARD2_NAME_BAR_W) / 2, (bar_top + bar_bottom) / 2),
        name,
        font=_font(CARD2_NAME_FONT, bold=True),
        fill=(255, 255, 255),
        anchor="mm",
    )
    # 头像：左上角，右移 1/4 边长（盖住名字条左段）
    if avatar is not None:
        img.paste(avatar, (avatar_x, avatar_y), avatar)
    else:
        draw.rectangle(
            [
                avatar_x,
                avatar_y,
                avatar_x + CARD2_AVATAR - 1,
                avatar_y + CARD2_AVATAR - 1,
            ],
            fill=(48, 48, 48),
        )
        draw.text(
            (avatar_x + CARD2_AVATAR // 2, avatar_y + CARD2_AVATAR // 2),
            "♪",
            font=_font(CARD2_AVATAR // 2),
            fill=(120, 120, 120),
            anchor="mm",
        )
    # Rating 徽章：名字条右下角
    badge_x = CARD2_NAME_BAR_W - CARD2_RATING_BADGE_W
    badge_y = bar_bottom
    draw.rectangle(
        [badge_x, badge_y, CARD2_NAME_BAR_W, badge_y + CARD2_RATING_BADGE_H],
        fill=CARD2_RATING_BADGE,
    )
    draw.text(
        (badge_x + CARD2_RATING_BADGE_W // 2, badge_y + CARD2_RATING_BADGE_H // 2),
        f"{result.rating:.2f}",
        font=_font(CARD2_RATING_FONT, bold=True),
        fill=(255, 255, 255),
        anchor="mm",
    )
    # 等级分布：右上角（保持大小）
    _draw_grade_matrix(draw, CARD2_AVATAR_TOP, grade_counts, right=width - CARD2_PAD)

    y = header_h + 24

    b30_factors = [b30_goal_factor()] * len(result.b30_charts)
    n10_factors = [n10_goal_factor(i) for i in range(len(result.n10_charts))]
    y = _draw_rating_section_new(
        img, draw, y, "B30", result.b30_charts, b30_rows, b30_factors
    )
    y = _draw_rating_section_new(
        img, draw, y, "N10", result.n10_charts, n10_rows, n10_factors
    )

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
