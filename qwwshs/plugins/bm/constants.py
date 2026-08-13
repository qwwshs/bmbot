"""Berry Melody 定数表解析。

支持两种数据来源（优先级从高到低）：

1. ``Berry Melody定数表`` 目录下的 ``.xlsx`` 文件（zip 格式，直读）
2. 解压后的目录结构（``xl/worksheets/sheet1.xml`` + ``xl/sharedStrings.xml``）

将谱面定数解析为 ``{曲名: 定数条目}`` 的内存结构，供 rating 计算使用。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_PACKAGE_DIR = Path(__file__).resolve().parent
_TABLE_DIR = _PACKAGE_DIR / "Berry Melody定数表"
# 数据目录放在插件包之外（项目根 data/bm），避免部署覆盖插件目录时丢失绑定数据
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "bm"
_CACHE_PATH = DATA_DIR / "constants.json"

_ALL_DIFFS = ("RL", "IL", "TT", "RU", "DM", "FL")
_EXTRA_TYPE_MAP = {"RUIN": "RU", "VOID": "RU", "DREAMY": "DM", "FOOL": "FL"}


class ConstantsError(Exception):
    """定数表解析失败。"""


def _shared_strings_from_root(root: ET.Element) -> list[str]:
    return [
        "".join(t.text or "" for t in si.iter(f"{{{XLSX_NS}}}t"))
        for si in root.findall(f"{{{XLSX_NS}}}si")
    ]


def _load_shared_strings(xml_path: Path) -> list[str]:
    return _shared_strings_from_root(ET.parse(xml_path).getroot())


def _read_xlsx(path: Path) -> tuple[ET.Element, list[str]]:
    """从 .xlsx（zip）中读取 sheet1 与共享字符串。"""
    with zipfile.ZipFile(path) as archive:
        sheet_root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = _shared_strings_from_root(
                ET.fromstring(archive.read("xl/sharedStrings.xml"))
            )
    return sheet_root, shared


def _read_dir_table() -> tuple[ET.Element, list[str]]:
    """读取解压目录形式的定数表。"""
    sheet_path = _TABLE_DIR / "xl" / "worksheets" / "sheet1.xml"
    if not sheet_path.exists():
        raise ConstantsError(f"未找到解压目录定数表: {sheet_path}")  # noqa: TRY003
    shared_path = _TABLE_DIR / "xl" / "sharedStrings.xml"
    shared = _load_shared_strings(shared_path) if shared_path.exists() else []
    return ET.parse(sheet_path).getroot(), shared


def _load_table() -> tuple[ET.Element, list[str], str]:
    """按优先级加载主定数表：插件目录内的 .xlsx → 解压目录。

    返回 ``(sheet, shared, source)``，source 为 ``xlsx`` 或 ``dir``。
    """
    xlsx_files = list(_TABLE_DIR.glob("*.xlsx")) + list(_PACKAGE_DIR.glob("*.xlsx"))
    if xlsx_files:
        latest = max(xlsx_files, key=lambda p: p.stat().st_mtime)
        logger.info("使用定数表文件: %s", latest)
        return (*_read_xlsx(latest), "xlsx")
    return (*_read_dir_table(), "dir")


def _col_index(ref: str) -> int:
    """单元格引用转列下标：'A' -> 0，'AB' -> 27。"""
    index = 0
    for ch in ref:
        if ch.isalpha():
            index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def _cell_value(cell: ET.Element, shared: list[str]) -> str | float | None:
    v = cell.find(f"{{{XLSX_NS}}}v")
    if v is None or v.text is None:
        return None
    if cell.get("t") == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return None
    text = v.text.strip()
    try:
        return float(text)
    except ValueError:
        return text


def _parse_rows(root: ET.Element, shared: list[str]) -> list[list[str | float | None]]:
    rows: list[list[str | float | None]] = []
    for row in root.findall(f".//{{{XLSX_NS}}}sheetData/{{{XLSX_NS}}}row"):
        cells: dict[int, str | float | None] = {}
        for cell in row.findall(f"{{{XLSX_NS}}}c"):
            ref = cell.get("r", "")
            if not ref:
                continue
            cells[_col_index(ref)] = _cell_value(cell, shared)
        count = max(cells) + 1 if cells else 0
        rows.append([cells.get(i) for i in range(count)])
    return rows


_HEADER_MATCHERS = (
    ("title", lambda t: "曲名" in t and "原曲名" not in t),
    ("originalName", lambda t: "原曲名" in t),
    ("artist", lambda t: "曲师" in t),
    ("RL", lambda t: "REALITY谱面难度" in t),
    ("IL", lambda t: "ILLUSION谱面难度" in t),
    ("TT", lambda t: "TWIST谱面难度" in t),
    ("charterRL", lambda t: "REALITY谱面谱师" in t),
    ("charterIL", lambda t: "ILLUSION谱面谱师" in t),
    ("charterTT", lambda t: "TWIST谱面谱师" in t),
    ("extraType", lambda t: t == "追加谱面"),
    ("extraCharter", lambda t: "追加谱面谱师" in t),
    ("extraConst", lambda t: "追加谱面难度" in t),
    ("aliases", lambda t: "别名" in t),
)


def _find_header_indices(header: list[str | float | None]) -> dict[str, int]:
    """按表头文本定位列下标（与网页工具的列名匹配逻辑一致）。"""
    indices: dict[str, int] = {}
    for i, col in enumerate(header):
        text = str(col or "").strip()
        for key, match in _HEADER_MATCHERS:
            if key not in indices and text and match(text):
                indices[key] = i
                break
    return indices


def _parse_constant(cell: str | float | None) -> float | None:
    """解析定数单元格：数字直接取，文本取 ``(x.x)`` 中的数字。"""
    if cell is None:
        return None
    try:
        return float(cell)
    except (TypeError, ValueError):
        match = re.search(r"\(([\d.]+)\)", str(cell))
        return float(match.group(1)) if match else None


def _title_text(value: str | float | None) -> str:
    """曲名列文本化：纯整数数值去掉 ``.0`` 后缀（Excel 数字格式的曲名）。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def _extract_charter(
    row: list[str | float | None],
    indices: dict[str, int],
    *,
    extra_valid: bool,
) -> dict[str, str]:
    """提取谱师字段：难度 -> 谱师名义（仅非空；含追加谱面谱师）。

    ``extra_valid`` 为 False 时忽略追加谱面谱师（对应定数解析失败的谱面）。
    """

    def at(key: str) -> str | float | None:
        index = indices.get(key)
        return row[index] if index is not None and index < len(row) else None

    charter: dict[str, str] = {}
    for diff, key in (
        ("RL", "charterRL"),
        ("IL", "charterIL"),
        ("TT", "charterTT"),
    ):
        if key in indices:
            charter_name = str(at(key) or "").strip()
            if charter_name:
                charter[diff] = charter_name
    if extra_valid and "extraCharter" in indices:
        extra_charter = str(at("extraCharter") or "").strip()
        if extra_charter:
            target = _EXTRA_TYPE_MAP.get(str(at("extraType") or "").strip().upper())
            if target:
                charter[target] = extra_charter
    return charter


def _parse_entry(
    row: list[str | float | None], indices: dict[str, int]
) -> tuple[str, dict] | None:
    title_idx = indices.get("title")
    title = (
        _title_text(row[title_idx])
        if title_idx is not None and title_idx < len(row)
        else ""
    )
    if not title:
        return None

    def at(key: str) -> str | float | None:
        index = indices.get(key)
        return row[index] if index is not None and index < len(row) else None

    original_name = (
        str(at("originalName") or "").strip() if "originalName" in indices else ""
    )
    entry: dict = {
        "RL": _parse_constant(at("RL")),
        "IL": _parse_constant(at("IL")),
        "TT": _parse_constant(at("TT")),
        "RU": None,
        "DM": None,
        "FL": None,
        "aliases": [],
        "artist": str(at("artist") or "").strip() if "artist" in indices else "",
        "originalName": original_name,
        # 谱师：难度 -> 谱师名义（仅非空；合作名义保留原文如 A&B&C）
        "charter": {},
    }
    if not entry["originalName"]:
        entry["originalName"] = title
    if "aliases" in indices:
        entry["aliases"] = [
            alias.strip()
            for alias in re.split(r"[,，]", str(at("aliases") or ""))
            if alias.strip()
        ]
    extra_const: float | None = None
    if "extraType" in indices and "extraConst" in indices:
        extra_type = str(at("extraType") or "").strip().upper()
        extra_const = _parse_constant(at("extraConst"))
        if extra_const is not None:
            target = _EXTRA_TYPE_MAP.get(extra_type)
            if target:
                entry[target] = extra_const
    entry["charter"] = _extract_charter(
        row, indices, extra_valid=extra_const is not None
    )
    return title, entry


def _parse_table(sheet_root: ET.Element, shared: list[str]) -> dict[str, dict]:
    """从工作表解析出 ``{曲名: 定数条目}``。"""
    rows = _parse_rows(sheet_root, shared)
    if not rows:
        raise ConstantsError("定数表为空")
    indices = _find_header_indices(rows[0])
    if "title" not in indices:
        raise ConstantsError("找不到【曲名】表头")
    songs: dict[str, dict] = {}
    for row in rows[1:]:
        parsed = _parse_entry(row, indices)
        if parsed is not None:
            title, entry = parsed
            songs[title] = entry
    return songs


def load_constants() -> dict[str, dict]:
    """解析定数表，返回 ``{曲名: 定数条目}``。

    主表为 .xlsx 或解压目录；条目的 ``originalName`` 为「原曲名」列
    （中文/日文原名），用于中文搜索与显示。
    定数表缺失或解析失败时抛出 :class:`ConstantsError`。
    """
    sheet_root, shared, _ = _load_table()
    return _parse_table(sheet_root, shared)


_cache: dict[str, dict[str, dict]] = {}


def get_song_constants() -> dict[str, dict]:
    """获取定数表（模块级缓存），并写入 JSON 缓存供排查。"""
    if "songs" not in _cache:
        songs = load_constants()
        _cache["songs"] = songs
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(
                json.dumps(songs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.warning("无法写入定数表缓存: %s", _CACHE_PATH)
    return _cache["songs"]
