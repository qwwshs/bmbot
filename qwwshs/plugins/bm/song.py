"""Berry Melody 单曲查询。

``/bmsong <曲名>`` 的搜索与详情构建：
- 归一化精确匹配优先，否则模糊（子串）匹配
- 成绩从绑定存档中按难度提取，0 分/无成绩的难度跳过
- 支持用户自定义别名（``/bmaddname`` 添加，别名可直接检索）
"""

from __future__ import annotations

import json
from pathlib import Path

from .rating import ALL_DIFFS, get_grade, normalize_n10_name, normalized_variants

IMAGE_DIR = Path(__file__).resolve().parent / "images"

_SEARCH_LIMIT = 20
_MIN_KEY_PARTS = 3

# 用户自定义别名：别名 -> 表内曲名（持久化于 data/bm/aliases.json）
_ALIASES: dict[str, str] = {}


def load_aliases(path: Path) -> None:
    """从文件加载用户别名映射。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ALIASES.clear()
        return
    _ALIASES.clear()
    _ALIASES.update(
        {
            str(alias).strip(): str(name)
            for alias, name in data.items()
            if str(alias).strip()
        }
    )


def save_aliases(path: Path) -> None:
    """持久化用户别名映射。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_ALIASES, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_alias(alias: str, name: str) -> None:
    """记录别名 -> 表内曲名。"""
    _ALIASES[alias.strip()] = name


def remove_alias(alias: str, name: str) -> bool:
    """删除曲名 ``name`` 下的别名 ``alias``，成功返回 True。"""
    if _ALIASES.get(alias) == name:
        del _ALIASES[alias]
        return True
    return False


def has_alias(alias: str) -> bool:
    """别名是否存在。"""
    return alias.strip() in _ALIASES


def get_aliases() -> dict[str, str]:
    """返回全部别名映射（别名 -> 表内曲名）的副本。"""
    return dict(_ALIASES)


def _resolve_alias(query: str) -> str | None:
    """查询词是否命中用户别名，返回对应的表内曲名。"""
    q = query.strip()
    if not q:
        return None
    if q in _ALIASES:
        return _ALIASES[q]
    q_norm = normalize_n10_name(q)
    for alias, name in _ALIASES.items():
        if normalize_n10_name(alias) == q_norm:
            return name
    return None


def _entry_variants(name: str, entry: dict) -> frozenset[str]:
    """条目变体集合：曲名 + 「原曲名」+「别名」（游戏内部名等）。"""
    variants = set(normalized_variants(name))
    original = str(entry.get("originalName") or "").strip()
    if original and original != name:
        variants |= normalized_variants(original)
    for alias_raw in entry.get("aliases") or []:
        alias = str(alias_raw).strip()
        if alias and alias != name:
            variants |= normalized_variants(alias)
    return frozenset(variants)


def _resolve_alias_target(constants: dict[str, dict], target: str) -> list[str]:
    """用户别名目标不在表内时（定数表改键），按曲名变体回退解析。"""
    return [
        name
        for name, entry in constants.items()
        if _entry_variants(name, entry) & normalized_variants(target)
    ]


def _fuzzy_search(constants: dict[str, dict], q_variants: frozenset[str]) -> list[str]:
    """子串模糊匹配：按子串出现位置（越靠前越相关）排序，截断到上限。"""
    hits: list[tuple[int, str]] = []
    for name, entry in constants.items():
        entry_variants = _entry_variants(name, entry)
        best = -1
        for qv in q_variants:
            for nv in entry_variants:
                pos = nv.find(qv)
                if pos != -1 and (best == -1 or pos < best):
                    best = pos
        if best != -1:
            hits.append((best, name))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [name for _, name in hits][:_SEARCH_LIMIT]


def search_songs(constants: dict[str, dict], query: str) -> list[str]:
    """搜索曲目：别名/归一化变体精确匹配优先，否则按子串位置排序的模糊匹配。"""
    resolved = _resolve_alias(query)
    if resolved is not None:
        if resolved in constants:
            return [resolved]
        fallback = _resolve_alias_target(constants, resolved)
        if fallback:
            return fallback
    q_variants = normalized_variants(query)
    if not any(q_variants):
        return []
    exact = [
        name
        for name, entry in constants.items()
        if _entry_variants(name, entry) & q_variants
    ]
    if exact:
        return exact
    return _fuzzy_search(constants, q_variants)


def get_song_scores(
    data: dict, song_name: str, entry: dict | None = None
) -> list[tuple[str, int, str]]:
    """按难度顺序返回 ``(难度, 分数, 等级)``，0 分/无成绩的难度跳过。

    存档 ``BestScore_`` 键用游戏内部名，与表内曲名（显示名）可能不同，
    依次尝试 别名（内部名）→ 曲名 → 原曲名 的归一化形式。
    """
    candidates = [str(a).strip() for a in (entry or {}).get("aliases") or []]
    candidates.append(song_name)
    original = str((entry or {}).get("originalName") or "").strip()
    if original and original not in candidates:
        candidates.append(original)
    wanted = [normalize_n10_name(c) for c in candidates if c]
    index: dict[tuple[str, str], int] = {}
    for key, value in data.items():
        if not key.startswith("BestScore_"):
            continue
        parts = key.split("_")
        if len(parts) < _MIN_KEY_PARTS:
            continue
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            continue
        name = normalize_n10_name("_".join(parts[1:-1]))
        index[(name, parts[-1])] = score
    result: list[tuple[str, int, str]] = []
    for diff in ALL_DIFFS:
        score = next(
            (index[(w, diff)] for w in wanted if index.get((w, diff))), None
        )
        if score and score > 0:
            result.append((diff, score, get_grade(score)))
    return result


def format_song_detail(
    name: str, entry: dict, scores: list[tuple[str, int, str]]
) -> str:
    """构建单曲详情文本（标题为原曲名<名称>，列出各难度与谱师）。"""
    original = str(entry.get("originalName") or "").strip()
    title = f"{original}<{name}>" if original and original != name else name
    lines = [f"🎵 {title}"]
    artist = str(entry.get("artist") or "").strip()
    if artist:
        lines.append(f"曲师：{artist}")
    lines.append("")
    charters = entry.get("charter") or {}
    score_map = {diff: (score, grade) for diff, score, grade in scores}
    rows: list[str] = []
    for diff in ALL_DIFFS:
        constant = entry.get(diff)
        if constant is None or float(constant) <= 0:
            continue
        const_text = f"({constant:.1f})"
        charter = charters.get(diff, "")
        score, grade = score_map.get(diff, (None, None))
        if score is not None:
            rows.append(
                f"{diff}{const_text}  {score}  {grade}  谱师：{charter or '未知'}"
            )
        else:
            rows.append(f"{diff}{const_text}  未游玩  谱师：{charter or '未知'}")
    if rows:
        lines.append("📊 谱面与谱师")
        lines.extend(rows)
    else:
        lines.append("暂无谱面数据")
    return "\n".join(lines)


def find_cover(name: str, entry: dict) -> Path | None:
    """查找曲绘：曲名/原曲名/别名（内部名）的各难度与裸名变体。"""
    original = str(entry.get("originalName") or "").strip()
    bases = [name]
    if original and original not in bases:
        bases.append(original)
    for alias_raw in entry.get("aliases") or []:
        alias = str(alias_raw).strip()
        if alias and alias not in bases:
            bases.append(alias)
    candidates: list[str] = []
    for base in bases:
        # 内部名可能含连续空格（如 "Dream   Hard   Find"），文件名为单空格
        collapsed = " ".join(base.split())
        for variant in (base, collapsed):
            if variant and f"{variant}.png" not in candidates:
                candidates.append(f"{variant}.png")
        for diff in ALL_DIFFS:
            candidates.append(f"{base}_{diff}.png")
            candidates.append(f"{collapsed}_{diff}.png")
    for filename in candidates:
        path = IMAGE_DIR / filename
        if path.exists():
            return path
    return None
