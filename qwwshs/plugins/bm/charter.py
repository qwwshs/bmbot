"""Berry Melody 谱师查询与管理。

- ``/bmcharter``：按谱师查谱面（自动扩展基元谱师的关联名义组）
- 基元谱师名义（本名）+ 马甲/合作名义关联，持久化于 ``data/bm/charters.json``
- 谱师名义必须能在定数表（``charter`` 字段）中查到，管理命令仅白名单可用
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .rating import normalize_n10_name, normalized_variants

if TYPE_CHECKING:
    from pathlib import Path

_SEARCH_LIMIT = 20

# 基元谱师名义 -> 关联谱师名义（马甲/合作名义）列表
_PRIMITIVES: dict[str, list[str]] = {}


def load_charters(path: Path) -> None:
    """从文件加载基元谱师关联数据。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _PRIMITIVES.clear()
        return
    _PRIMITIVES.clear()
    for raw_primitive, raw_related in data.items():
        primitive = str(raw_primitive).strip()
        if not primitive:
            continue
        related_list = raw_related if isinstance(raw_related, list) else []
        _PRIMITIVES[primitive] = [
            str(item).strip() for item in related_list if str(item).strip()
        ]


def save_charters(path: Path) -> None:
    """持久化基元谱师关联数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(_PRIMITIVES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def is_primitive(name: str) -> bool:
    """谱师名义是否为基元谱师（本名）。"""
    norm = normalize_n10_name(name)
    return any(normalize_n10_name(primitive) == norm for primitive in _PRIMITIVES)


def add_primitive(name: str) -> None:
    """把谱师名义设为基元谱师（本名）。"""
    _PRIMITIVES.setdefault(name.strip(), [])


def add_related(primitive: str, related: str) -> bool:
    """给基元谱师添加关联名义（马甲/合作名义），已存在返回 False。"""
    primitive = primitive.strip()
    related = related.strip()
    rel_norm = normalize_n10_name(related)
    existing = _PRIMITIVES.setdefault(primitive, [])
    if any(normalize_n10_name(item) == rel_norm for item in existing):
        return False
    existing.append(related)
    return True


def remove_related(primitive: str, related: str) -> bool:
    """解除基元谱师与某关联名义的关联，成功返回 True。"""
    rel_norm = normalize_n10_name(related)
    existing = _PRIMITIVES.get(primitive.strip(), [])
    for index, item in enumerate(existing):
        if normalize_n10_name(item) == rel_norm:
            del existing[index]
            return True
    return False


def get_related(primitive: str) -> list[str]:
    """返回基元谱师的关联名义列表。"""
    return list(_PRIMITIVES.get(primitive.strip(), []))


def all_primitives() -> list[str]:
    """返回全部基元谱师名义。"""
    return list(_PRIMITIVES)


def group_of(name: str) -> frozenset[str]:
    """返回谱师名义所属的「名义组」：本名 + 全部关联。

    若 ``name`` 是基元或某基元的关联，返回整组；否则只含自身。
    """
    norm = normalize_n10_name(name)
    for primitive, related in _PRIMITIVES.items():
        if normalize_n10_name(primitive) == norm:
            return frozenset((primitive, *related))
    for primitive, related in _PRIMITIVES.items():
        for item in related:
            if normalize_n10_name(item) == norm:
                return frozenset((primitive, *related))
    return frozenset((name,))


def list_all_charters(constants: dict[str, dict]) -> list[str]:
    """收集定数表中出现的全部谱师名义（去重、排序）。"""
    names: set[str] = set()
    for entry in constants.values():
        for charter in (entry.get("charter") or {}).values():
            if charter:
                names.add(charter)
    return sorted(names)


def search_charters(constants: dict[str, dict], query: str) -> list[str]:
    """搜索谱师：归一化变体精确匹配优先，否则按子串位置排序的模糊匹配。"""
    q_variants = normalized_variants(query)
    if not any(q_variants):
        return []
    all_names = list_all_charters(constants)
    exact = [name for name in all_names if normalized_variants(name) & q_variants]
    if exact:
        return exact
    hits: list[tuple[int, str]] = []
    for name in all_names:
        best = -1
        for qv in q_variants:
            for nv in normalized_variants(name):
                pos = nv.find(qv)
                if pos != -1 and (best == -1 or pos < best):
                    best = pos
        if best != -1:
            hits.append((best, name))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [name for _, name in hits][:_SEARCH_LIMIT]
