"""Berry Melody Rating 计算。

算法逐行复刻自 ``bm-score(4).html``（推分目标复刻自新版 ``bm-score.html`` v3.3.3）：

- ChartPotential 为分段线性函数（满分 +3.25，其余按分数区间插值）
- B30 = 全部谱面 ChartPotential 降序前 30 张的平均值（不足 30 张仍除以 30）
- N10 = 固定 20 首曲池内前 10 张加权平均（前 5 张权重 0.6，后 5 张权重 0.4，除以 5）
- 最终 Rating = 0.8 x B30 + 0.2 x N10
- GOAL 推分目标：二分查找推 ``GOAL_INCREASE`` Rating 所需的最低分数
"""

# 分数阈值与返回分支数是算法本身的结构，逐行豁免没有意义
# ruff: noqa: PLR2004, PLR0911

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

try:
    import opencc
except ImportError:  # pragma: no cover
    opencc = None  # type: ignore[assignment]

# 日文汉字 → 简体中文补充映射（OpenCC 繁简词典未覆盖的日式汉字）
_JP_KANJI_MAP = {
    "気": "气",
    "図": "图",
    "広": "广",
    "辺": "边",
    "駅": "站",
    "斎": "斋",
    "嶋": "岛",
    "働": "动",
    "歳": "岁",
    "芸": "艺",
    "絵": "绘",
    "応": "应",
    "帰": "归",
    "読": "读",
    "鉄": "铁",
    "沢": "泽",
    "渋": "涩",
    "仮": "假",
    "訳": "译",
    "覇": "霸",
    "塩": "盐",
    "遅": "迟",
    "楽": "乐",
    "薬": "药",
    "児": "儿",
    "単": "单",
    "対": "对",
    "発": "发",
    "収": "收",
    "変": "变",
    "拡": "扩",
    "択": "择",
    "挙": "举",
    "価": "价",
    "圧": "压",
    "営": "营",
    "転": "转",
    "壊": "坏",
    "検": "检",
    "実": "实",
    "総": "总",
    "経": "经",
    "験": "验",
    "産": "产",
    "隊": "队",
    "陸": "陆",
    "陣": "阵",
    "険": "险",
    "権": "权",
    "極": "极",
    "霊": "灵",
    "剣": "剑",
    "劇": "剧",
    "撃": "击",
    "竜": "龙",
    "獣": "兽",
    "亜": "亚",
    "悪": "恶",
    "焼": "烧",
    "脳": "脑",
    "臓": "脏",
    "縦": "纵",
    "続": "续",
    "絶": "绝",
    "網": "网",
    "緑": "绿",
    "練": "练",
    "編": "编",
    "雑": "杂",
    "難": "难",
    "離": "离",
    "雰": "氛",
    "霧": "雾",
    "響": "响",
    "闘": "斗",
    "闇": "暗",
    "顕": "显",
    "類": "类",
    "飼": "饲",
    "飲": "饮",
    "飾": "饰",
    "飽": "饱",
    "飯": "饭",
    "館": "馆",
    "騎": "骑",
    "駆": "驱",
    "驚": "惊",
    "庁": "厅",
    "渓": "溪",
    "暁": "晓",
    "桜": "樱",
    "滝": "泷",
    "湧": "涌",
    "痩": "瘦",
    "癒": "愈",
    "巻": "卷",
    "巣": "巢",
    "斉": "齐",
    "嶽": "岳",
    "渇": "渴",
    "瀬": "濑",
    "達": "达",
    "適": "适",
    "捜": "搜",
    "掲": "揭",
    "昇": "升",
    "暦": "历",
    "曇": "昙",
    "曽": "曾",
    "桟": "栈",
    "拠": "据",
    "済": "济",
    "満": "满",
    "準": "准",
    "無": "无",
    "煙": "烟",
    "黒": "黑",
    "様": "样",
    "氷": "冰",
    "銀": "银",
    "淪": "沦",
    "沈": "沉",
    "畳": "叠",
    "確": "确",
    "稲": "稻",
    "穂": "穗",
    "繋": "系",
    "縄": "绳",
    "聴": "听",
    "臨": "临",
    "艦": "舰",
    "華": "华",
    "葉": "叶",
    "蘭": "兰",
    "観": "观",
    "覚": "觉",
    "計": "计",
    "討": "讨",
    "訓": "训",
    "設": "设",
    "試": "试",
    "詩": "诗",
    "話": "话",
    "講": "讲",
    "謝": "谢",
    "識": "识",
    "議": "议",
    "護": "护",
    "豊": "丰",
    "貝": "贝",
    "負": "负",
    "財": "财",
    "責": "责",
    "貫": "贯",
    "貨": "货",
    "費": "费",
    "賀": "贺",
    "資": "资",
    "賢": "贤",
    "購": "购",
    "贈": "赠",
    "趙": "赵",
    "跡": "迹",
    "躍": "跃",
    "車": "车",
    "軍": "军",
    "軽": "轻",
    "輝": "辉",
    "輪": "轮",
    "輸": "输",
    "辞": "辞",
    "聞": "闻",
    "声": "声",
    "職": "职",
    "脈": "脉",
    "興": "兴",
    "旧": "旧",
    "衛": "卫",
    "裏": "里",
    "見": "见",
    "復": "复",
    "徳": "德",
    "懐": "怀",
    "戯": "戏",
    "挿": "插",
    "掃": "扫",
    "摂": "摄",
    "揺": "摇",
    "汚": "污",
    "浜": "滨",
    "渉": "涉",
    "漢": "汉",
    "湾": "湾",
    "献": "献",
    "画": "画",
    "異": "异",
    "盤": "盘",
    "礼": "礼",
    "秘": "秘",
    "種": "种",
    "競": "竞",
    "篭": "笼",
    "簡": "简",
    "粋": "粹",
    "糸": "丝",
    "紅": "红",
    "紋": "纹",
    "織": "织",
    "義": "义",
    "姉": "姐",
    "姫": "姬",
    "嬢": "娘",
    "寛": "宽",
    "層": "层",
    "島": "岛",
    "岡": "冈",
    "帳": "账",
    "廃": "废",
    "従": "从",
    "徴": "征",
    "戻": "返",
    "歩": "步",
    "歴": "历",
    "殻": "壳",
    "殺": "杀",
}

_converter_cache: dict[str, object] = {}


def _get_converter(config: str):
    """懒加载 OpenCC 转换器，不可用时返回 None。"""
    if opencc is None:
        return None
    if config not in _converter_cache:
        try:
            _converter_cache[config] = opencc.OpenCC(config)
        except (OSError, ValueError):
            _converter_cache[config] = None
    return _converter_cache[config]


def _jp_to_simplified(text: str) -> str:
    """日文汉字转简体中文（未收录的字保留原样）。"""
    return "".join(_JP_KANJI_MAP.get(ch, ch) for ch in text)


@lru_cache(maxsize=8192)
def normalized_variants(name: str) -> frozenset[str]:
    """曲名的归一化变体集合：日文汉字转简 + 简繁双向转换。

    用于简体中文匹配繁体中文与日本汉字。
    """
    variants = {normalize_n10_name(name)}
    base = _jp_to_simplified(name)
    variants.add(normalize_n10_name(base))
    for source in (name, base):
        for config in ("s2t", "t2s"):
            converter = _get_converter(config)
            if converter is None:
                continue
            try:
                variants.add(normalize_n10_name(converter.convert(source)))
            except (OSError, ValueError):
                continue
    return frozenset(variants)


ALL_DIFFS = ("RL", "IL", "TT", "RU", "DM", "FL")
GRADES = ("S", "AAA+", "AAA", "AA", "A", "B", "F")

# N10 固定曲池（Rainy Waltz / Nini / Melusia 的 RUIN 不计入，
# 通过归一化时剥离 ``（RU）`` 后缀实现）
N10_SONG_LIST = [
    "Spiritworks",
    "xu",
    "Chara",
    "Rocky Buinne",
    "Seculo Seculorum",
    "EmbellishOUR",
    "caelumize",
    "Link to Collapse",
    "FaXeanos",
    "Leviathan",
    "find me",
    "Ether Vortex Final",
    "Ether Vortex",
    "BLACK DIAMOND",
    "Fallen Angel",
    "Beep",
    "SurrE4L1stic",
    "Infinity",
    "GIFT",
    "Double Life",
]


@dataclass(slots=True)
class Chart:
    """单张谱面的成绩与潜力。"""

    name: str
    diff: str
    constant: float
    score: int
    potential: float
    original_name: str = ""
    internal_names: tuple[str, ...] = ()


@dataclass(slots=True)
class RatingResult:
    """一次 Rating 计算的完整结果。"""

    b30_charts: list[Chart]
    n10_charts: list[Chart]
    b30_avg: float
    n10_avg: float
    rating: float
    total_charts: int
    missing: list[str]
    archive_potential: object = None
    # OVERFLOW 溢出榜：主榜之后的谱面（B30 取第 31~33，N10 取第 11~13）
    b30_overflow: list[Chart] = field(default_factory=list)
    n10_overflow: list[Chart] = field(default_factory=list)


def calculate_chart_potential(score: int, constant: float) -> float:
    """单谱面 ChartPotential（与 HTML 的 calculateChartPotential 一致）。"""
    if score == 1_000_000:
        return constant + 3.25
    if score >= 997500:
        return constant + 3 + ((score - 997500) // 100) * 0.01
    if score >= 990000:
        return constant + 1.5 + ((score - 990000) // 50) * 0.01
    if score >= 975000:
        return constant + ((score - 975000) // 150) * 0.01
    if score >= 950000:
        return constant - ((975000 - score) // 50) * 0.01
    if score >= 900000:
        return max(constant - 5, 0) / 2.0
    return 0.0


def get_grade(score: int) -> str:
    """分数对应等级：S / AAA+ / AAA / AA / A / B / F。"""
    if score == 1_000_000:
        return "S"
    if score >= 980000:
        return "AAA+"
    if score >= 950000:
        return "AAA"
    if score >= 900000:
        return "AA"
    if score >= 800000:
        return "A"
    if score >= 600000:
        return "B"
    return "F"


# 推分阈值：GOAL 目标分数按「推 0.005 Rating」计算（与网页工具默认一致）
GOAL_INCREASE = 0.005


def b30_goal_factor() -> float:
    """B30 单曲推分权重（0.8 / 30，复刻网页）。"""
    return 0.8 / 30


def n10_goal_factor(index: int) -> float:
    """N10 单曲推分权重：前 5 首 0.2 x 0.6 / 5，后 5 首 0.2 x 0.4 / 5。"""
    return 0.2 * (0.6 if index < 5 else 0.4) / 5


def target_score_for_increase(
    score: int,
    potential: float,
    constant: float,
    increase: float,
    factor: float,
) -> int:
    """主榜推分目标：推 ``increase`` Rating 所需的最低分数，达不到返回 -1。

    复刻网页 ``calculateTargetScore``：二分查找满足
    ``(ChartPotential(mid) - potential) * factor >= increase`` 的最小 mid。
    """
    t = float(increase) if float(increase) > 0 else GOAL_INCREASE
    max_potential = calculate_chart_potential(1_000_000, constant)
    if (max_potential - potential) * factor + 1e-9 < t:
        return -1
    low, high, result = score, 1_000_000, 1_000_000
    while low <= high:
        mid = (low + high) // 2
        if (calculate_chart_potential(mid, constant) - potential) * factor + 1e-9 >= t:
            result = mid
            high = mid - 1
        else:
            low = mid + 1
    return result


def target_score_for_cutoff(
    score: int,
    constant: float,
    increase: float,
    factor: float,
    cutoff_potential: float,
) -> int:
    """溢出榜推分目标：潜力超过 ``cutoff_potential`` 所需的最低分数，达不到返回 -1。

    复刻网页 ``calculateOverflowTargetScore``：目标潜力为
    ``cutoff_potential + increase / factor``（即挤进主榜后再推 0.005）。
    """
    t = float(increase) if float(increase) > 0 else GOAL_INCREASE
    target_potential = cutoff_potential + t / factor
    max_potential = calculate_chart_potential(1_000_000, constant)
    if max_potential + 1e-9 < target_potential:
        return -1
    low, high, result = score, 1_000_000, 1_000_000
    while low <= high:
        mid = (low + high) // 2
        if calculate_chart_potential(mid, constant) + 1e-9 >= target_potential:
            result = mid
            high = mid - 1
        else:
            low = mid + 1
    return result


def normalize_n10_name(name: str) -> str:
    """归一化曲名用于 N10 曲池匹配（剥离 ``（RU）`` 后缀等）。"""
    text = re.sub(r"[（(]\s*RU\s*[)）]", "", name.strip(), flags=re.IGNORECASE)
    text = re.sub(r"[_\u3000]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


# N10 固定曲池白名单（曲名归一化后的小写形式）
_N10_WHITELIST = frozenset(normalize_n10_name(name) for name in N10_SONG_LIST)


def _normalized_index(constants: dict[str, dict]) -> dict[str, str]:
    """构建曲名归一化变体 → 表内规范曲名的索引。

    **别名（游戏内部名）先注册**：存档 BestScore 键是内部名，当一首歌的
    别名与另一首的显示名归一化后相同（Remix 的内部名 ``Ether Vortex``
    与原版显示名 ``Ether Vortex``）时，该键必须归属别名所属曲目，否则
    两首歌的成绩会全部归到显示名曲目（v0.7.37 踩坑）。

    变体覆盖大小写、下划线/全角空格、``（RU）`` 后缀、
    简体/繁体/日文汉字的差异，并纳入条目的「原曲名」（中文/日文原名）
    与「别名」列（内部名等存档键写法）。
    """
    index: dict[str, str] = {}
    for name, entry in constants.items():
        for alias_raw in entry.get("aliases") or []:
            alias = str(alias_raw).strip()
            if alias:
                for variant in normalized_variants(alias):
                    index.setdefault(variant, name)
    for name, entry in constants.items():
        for variant in normalized_variants(name):
            index.setdefault(variant, name)
        original = str(entry.get("originalName") or "").strip()
        if original and original != name:
            for variant in normalized_variants(original):
                index.setdefault(variant, name)
    return index


def _resolve_name(
    constants: dict[str, dict], norm_index: dict[str, str], name: str
) -> tuple[dict | None, str]:
    """按曲名查定数表（含归一化变体回退），返回 (条目, 表内规范曲名)。

    不做显示名精确短路：传入的是存档内部名，与某曲显示名相同的情况
    由别名优先的归一化索引裁决（见 ``_normalized_index``）。
    """
    for variant in normalized_variants(name):
        canonical = norm_index.get(variant)
        if canonical is not None:
            return constants[canonical], canonical
    return None, name


def parse_scores(
    data: dict, constants: dict[str, dict]
) -> tuple[list[Chart], dict[str, dict[str, int]], list[str]]:
    """从解密存档中提取成绩。

    返回 ``(charts, grade_counts, missing)``：

    - ``charts``：能与定数表匹配的谱面（constant > 0），用于 B30/N10
    - ``grade_counts``：各难度 x 等级的出现次数（含未收录定数的谱面）
    - ``missing``：难度合法但定数表未收录的 ``曲名 (难度)`` 列表
    """
    charts: list[Chart] = []
    missing: list[str] = []
    grade_counts: dict[str, dict[str, int]] = {
        diff: dict.fromkeys(GRADES, 0) for diff in ALL_DIFFS
    }
    norm_index = _normalized_index(constants)
    for key, value in data.items():
        if not key.startswith("BestScore_"):
            continue
        parts = key.split("_")
        if len(parts) < 3:
            continue
        diff = parts[-1]
        name = "_".join(parts[1:-1])
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            continue
        grade = get_grade(score)
        if diff in ALL_DIFFS:
            grade_counts[diff][grade] += 1
        entry, name = _resolve_name(constants, norm_index, name)
        if diff not in ALL_DIFFS or entry is None:
            if diff in ALL_DIFFS:
                missing.append(f"{name} ({diff})")
            continue
        constant = entry.get(diff)
        if constant is not None and float(constant) > 0:
            constant = float(constant)
            charts.append(
                Chart(
                    name=name,
                    diff=diff,
                    constant=constant,
                    score=score,
                    potential=calculate_chart_potential(score, constant),
                    original_name=str(entry.get("originalName") or name),
                    internal_names=tuple(
                        str(a).strip()
                        for a in entry.get("aliases") or []
                        if str(a).strip()
                    ),
                )
            )
    return charts, grade_counts, _dedupe_missing(missing)


def _dedupe_missing(missing: list[str]) -> list[str]:
    """同一曲目多个难度只保留一条未收录提示。"""
    seen: set[str] = set()
    unique: list[str] = []
    for item in missing:
        song = item.split(" (", 1)[0]
        if song not in seen:
            seen.add(song)
            unique.append(item)
    return unique


def compute_rating(
    charts: list[Chart], archive_potential: object = None
) -> RatingResult:
    """计算 B30 / N10 / 最终 Rating。"""
    sorted_charts = sorted(charts, key=lambda c: c.potential, reverse=True)
    b30_charts = sorted_charts[:30]
    b30_avg = sum(c.potential for c in b30_charts) / 30

    # N10 曲池按内部名（别名）匹配：N10_SONG_LIST 存的是内部名，
    # 显示名与内部名不同的曲目（Ether Vortex 两首）也能正确入选
    eligible = [
        c
        for c in sorted_charts
        if any(
            normalize_n10_name(n) in _N10_WHITELIST
            for n in (c.name, *c.internal_names)
        )
    ]
    eligible.sort(key=lambda c: (-c.potential, -c.constant, -c.score))
    n10_charts = eligible[:10]
    n10_sum = sum(
        c.potential * (0.6 if i < 5 else 0.4) for i, c in enumerate(n10_charts)
    )
    n10_avg = n10_sum / 5

    rating = 0.8 * b30_avg + 0.2 * n10_avg
    return RatingResult(
        b30_charts=b30_charts,
        n10_charts=n10_charts,
        b30_avg=b30_avg,
        n10_avg=n10_avg,
        rating=rating,
        total_charts=len(charts),
        missing=[],
        archive_potential=archive_potential,
        b30_overflow=sorted_charts[30:33],
        n10_overflow=eligible[10:13],
    )
