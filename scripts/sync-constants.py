#!/usr/bin/env python3
"""把新谱面数据自动同步进定数表。

扫描 ``qwwshs/plugins/bm/chart/Info``（游戏解包的 Song::/Chart:: 对照，
含曲名/曲师/谱师/定数），把定数表中缺失的曲目、以及已有曲目缺失的
难度定数/谱师，追加写入 ``data/bm/constants_extra.json`` —— bot 加载
定数表时自动合并（见 ``constants.py`` 的 ``get_song_constants``）。

用法（仓库根目录）：
    python scripts/sync-constants.py

该脚本在 ``scripts/restart-bot.sh`` 部署流程中自动执行；运行结果位于
gitignore 的 ``data/`` 下，不会污染仓库。输出报告说明新增/补充了哪些
曲目，可定期人工审核后合并进 ``qwwshs/plugins/bm/constexcel.xlsx``
正式定数表（补充表条目优先于主表，已存在的字段不会被覆盖）。
"""

# ruff: noqa: T201

from __future__ import annotations

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# 仓库根目录（本文件位于 <root>/scripts/ 下）
ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = ROOT / "qwwshs" / "plugins" / "bm" / "chart"
EXTRA_PATH = ROOT / "data" / "bm" / "constants_extra.json"

# 谱面难度：Info 对照只含 RL/IL/TT，其余难度留空待人工补
_ALL_DIFFS = ("RL", "IL", "TT", "RU", "DM", "FL")

# 追加谱面类型（写入「追加谱面」列，决定定数进哪个难度）
_EXTRA_DIFF_TYPES = {"RU": "RUIN", "DM": "DREAMY", "FL": "FOOL"}

_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# 写入 xlsx 时的列角色 → 表头文本匹配（与 constants.py 的 _HEADER_MATCHERS
# 同规则）：列位置按表头定位，定数表调整列序后仍能写对列
_COLUMN_MATCHERS = (
    ("title", lambda t: "曲名" in t and "原曲名" not in t),
    ("artist", lambda t: "曲师" in t),
    ("painter", lambda t: "画师" in t),
    ("charterRL", lambda t: "REALITY谱面谱师" in t),
    ("RL", lambda t: "REALITY谱面难度" in t),
    ("charterIL", lambda t: "ILLUSION谱面谱师" in t),
    ("IL", lambda t: "ILLUSION谱面难度" in t),
    ("charterTT", lambda t: "TWIST谱面谱师" in t),
    ("TT", lambda t: "TWIST谱面难度" in t),
    ("extraType", lambda t: t == "追加谱面"),
    ("extraCharter", lambda t: "追加谱面谱师" in t),
    ("extraConst", lambda t: "追加谱面难度" in t),
    ("aliases", lambda t: "别名" in t),
)


def _col_index(ref: str) -> int | None:
    """单元格引用（如 ``AB12``）→ 0 起的列下标。"""
    letters = "".join(ch for ch in ref if ch.isalpha())
    if not letters:
        return None
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _col_letter(index: int) -> str:
    """0 起的列下标 → 列字母。"""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _cell_text(cell: ET.Element, shared: list[str]) -> str | None:
    """单元格文本：兼容共享字符串与内联字符串（官方导出格式）。"""
    value = cell.find(f"{{{_XLSX_NS}}}v")
    if cell.get("t") == "s" and value is not None and value.text:
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return None
    inline = cell.find(f"{{{_XLSX_NS}}}is")
    if inline is not None:
        return "".join(t.text or "" for t in inline.iter(f"{{{_XLSX_NS}}}t"))
    value = cell.find(f"{{{_XLSX_NS}}}v")
    return value.text if value is not None else None


def _header_columns(header: ET.Element, shared: list[str]) -> dict[str, int]:
    """按表头文本定位列角色（表头缺列时该角色不写入）。"""
    columns: dict[str, int] = {}
    for cell in header.findall(f"{{{_XLSX_NS}}}c"):
        index = _col_index(cell.get("r", ""))
        if index is None:
            continue
        text = (_cell_text(cell, shared) or "").strip()
        if not text:
            continue
        for role, match in _COLUMN_MATCHERS:
            if role not in columns and match(text):
                columns[role] = index
                break
    return columns


def _row_values(title: str, entry: dict) -> dict[str, str | float]:
    """条目 → 「列角色: 值」。曲名用显示名、内部名进别名列（主表既有约定）。"""
    charter = entry.get("charter") or {}
    values: dict[str, str | float] = {
        "title": str(entry.get("originalName") or title),
        "artist": str(entry.get("artist") or ""),
        "painter": str(entry.get("painter") or ""),
    }
    for diff in ("RL", "IL", "TT"):
        name = str(charter.get(diff) or "")
        if name:
            values[f"charter{diff}"] = name
        const = entry.get(diff)
        if const is not None:
            values[diff] = const
    for diff, extra_type in _EXTRA_DIFF_TYPES.items():
        if entry.get(diff) is None:
            continue
        values["extraType"] = extra_type
        values["extraCharter"] = str(charter.get(diff) or "")
        values["extraConst"] = entry[diff]
        break
    aliases = [str(a).strip() for a in entry.get("aliases") or [] if str(a).strip()]
    if aliases:
        values["aliases"] = ", ".join(aliases)
    return values


def load_constants_standalone() -> dict[str, dict]:
    """以独立模块加载 constants.py（避免触发 NoneBot 初始化）。"""
    path = ROOT / "qwwshs" / "plugins" / "bm" / "constants.py"
    spec = importlib.util.spec_from_file_location("bm_constants", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_constants()


def parse_info() -> tuple[dict[str, dict], dict[tuple[str, str], dict]]:
    """解析 chart/Info：返回 (歌曲信息, 难度信息)。

    - 歌曲信息：``{内部名: {title, artist, painter}}``
    - 难度信息：``{(内部名, 难度): {dlevel, charter}}``
    """
    songs: dict[str, dict] = {}
    charts: dict[tuple[str, str], dict] = {}
    path = CHART_DIR / "Info"
    if not path.exists():
        return songs, charts
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    current = ""
    for block in re.finditer(
        r"(Song|Chart)::\s*\{\s*(.*?)\s*\};", text, flags=re.DOTALL
    ):
        kind, body = block.group(1), block.group(2)
        fields: dict[str, str] = {}
        for match in re.finditer(
            r"\$\s*([A-Za-z_]\w*)\s*=\s*(?:\"([^\"]*)\"|([^$\n;]+))", body
        ):
            name = match.group(1)
            value = (match.group(2) or match.group(3) or "").strip().rstrip(",")
            fields[name] = value
        if kind == "Song":
            key = fields.get("Path", "")
            if key:
                current = key
                songs[key] = {
                    "title": fields.get("Title", ""),
                    "artist": fields.get("Artist", ""),
                    "painter": fields.get("Painter", ""),
                }
        else:
            diff = fields.get("Path", "").upper()
            if current and diff:
                # Level = alpha（未定级占位谱面，如 DLevel=0.001）跳过
                if not re.search(r"\d", fields.get("Level", "")):
                    continue
                charts[(current, diff)] = {
                    "dlevel": fields.get("DLevel", ""),
                    "charter": fields.get("Charter", ""),
                }
    return songs, charts


def normalize(name: str) -> str:
    """曲名归一化（小写、去多余空白），用于匹配定数表已有曲目。

    Info 里 ``/space`` 是空格的字面量（游戏内部名），先转成空格再合并空白。
    """
    text = re.sub(r"/space", " ", name.strip())
    text = re.sub(r"[_\u3000]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _to_float(value: str) -> float | None:
    """解析定数（"11.6" / "11+" → 11.6 / 11）。"""
    match = re.search(r"([\d.]+)", value or "")
    return float(match.group(1)) if match else None


def empty_entry(title: str) -> dict:
    """定数表条目骨架（与 constants.py 的 _parse_entry 结构一致）。"""
    return {
        "RL": None,
        "IL": None,
        "TT": None,
        "RU": None,
        "DM": None,
        "FL": None,
        "aliases": [],
        "artist": "",
        "painter": "",
        "originalName": title,
        "charter": {},
    }


def build_updates(  # noqa: C901, PLR0912, PLR0915
    songs: dict[str, dict], charts: dict[tuple[str, str], dict]
) -> tuple[dict[str, dict], list[str], list[str]]:
    """对比 Info 与定数表，返回 (待写条目, 新增报告, 补充报告)。

    只同步 Info 中出现的曲目（含定数/谱师/曲师）；已存在的曲目仅补充
    缺失的难度定数与谱师，不覆盖已有字段。
    """
    known: dict[str, str] = {}
    for t, entry in load_constants_standalone().items():
        known.setdefault(normalize(t), t)
        for alias in entry.get("aliases") or []:
            known.setdefault(normalize(str(alias)), t)
    base_table = load_constants_standalone()
    updates: dict[str, dict] = {}
    added: list[str] = []
    filled: list[str] = []
    for song_key, song in songs.items():
        # 曲名用内部名（存档 BestScore_ 键与主表曲名都用内部名，
        # 如 "Infinity" / "Magic Sink"；显示名 "IF = Infinity" 存 originalName）
        title = song_key
        display = song["title"] or song_key
        canonical = known.get(normalize(title))
        base = base_table.get(canonical) if canonical else None
        entry = empty_entry(title)
        entry["originalName"] = display
        if song["artist"]:
            entry["artist"] = song["artist"]
        if song.get("painter"):
            entry["painter"] = song["painter"]
        for diff in _ALL_DIFFS:
            chart = charts.get((song_key, diff))
            if chart is None:
                continue
            dlevel = _to_float(chart["dlevel"])
            if dlevel is not None:
                entry[diff] = dlevel
            if chart["charter"]:
                entry["charter"][diff] = chart["charter"]
        if canonical:
            # 已有曲目：只保留确实缺失的字段
            if base is None:
                continue
            missing: dict = {}
            for diff in _ALL_DIFFS:
                if entry[diff] is not None and base.get(diff) is None:
                    missing[diff] = entry[diff]
            for diff, name in entry["charter"].items():
                if name and not base.get("charter", {}).get(diff):
                    missing.setdefault("charter", {})[diff] = name
            if not entry.get("artist") and base.get("artist"):
                entry["artist"] = base["artist"]
            if not missing:
                continue
            patch = empty_entry(title)
            for diff, value in missing.items():
                if diff == "charter":
                    patch["charter"].update(value)
                else:
                    patch[diff] = value
            # 用表内规范名作键：运行时合并按键查主表，内部名≠表名会生成幽灵条目
            updates[canonical or title] = patch
            parts = [f"{d}={v}" for d, v in missing.items() if d != "charter"]
            if "charter" in missing:
                charter_parts = ", ".join(
                    f"{d}:{n}" for d, n in missing["charter"].items()
                )
                parts.append(f"谱师 {charter_parts}")
            filled.append(f"{title} | 补充 {', '.join(parts)}")
        else:
            # 主表以显示名作曲名、内部名进别名列（既有约定）；补充表用同一个键，
            # 否则 --apply 入库后内部名键与主表显示名键错开，运行时会多出幽灵条目
            if normalize(display) != normalize(title):
                entry["aliases"] = [title]
            updates[display] = entry
            consts = ", ".join(
                f"{d}={entry[d]}" for d in _ALL_DIFFS if entry[d] is not None
            )
            charter = ", ".join(
                f"{d}:{n}" for d, n in sorted(entry["charter"].items())
            )
            added.append(
                f"{title} | 曲师: {entry['artist'] or '?'} | {consts or '无定数'}"
                f"{' | 谱师: ' + charter if charter else ''}"
            )
    return updates, added, filled


def apply_to_xlsx(  # noqa: C901, PLR0912, PLR0915
    entries: dict[str, dict],
) -> tuple[int, int]:
    """把补充曲目正式追加进 constexcel.xlsx（新增行），返回 (新增行数, 跳过数)。

    仅追加主表中没有的曲目；已有曲目由运行时补充表机制补缺失字段。
    列位置按表头文本定位（同 ``constants.py`` 的匹配规则），行样式沿用同列
    已有单元格——定数表调整列序或样式后仍能写对。
    """
    import io
    import zipfile

    ns = _XLSX_NS
    xlsx_path = ROOT / "qwwshs" / "plugins" / "bm" / "constexcel.xlsx"
    with zipfile.ZipFile(xlsx_path) as zf:
        shared_root = None
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in si.iter(f"{{{ns}}}t"))
                for si in shared_root.findall(f"{{{ns}}}si")
            ]
        sheet_root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        data = sheet_root.find(f".//{{{ns}}}sheetData")
        rows = data.findall(f"{{{ns}}}row")
        if not rows:
            return 0, 0
        last_row = max(int(r.get("r", "0")) for r in rows)

        columns = _header_columns(rows[0], shared)
        if "title" not in columns:
            print("✗ 定数表未找到【曲名】表头，跳过写入")
            return 0, 0

        # 已有曲名（归一化）避免重复追加：曲名列 + 别名列（别名存内部名，
        # 新曲曲名胜在曲名列，只查曲名列会在下次 --apply 重复追加）
        known: set[str] = set()
        # 各列沿用已有单元格的样式（官方导出格式的定数带 "0.0" 数字格式），
        # 取众数避免个别手改单元格的样式带偏
        style_count: dict[int, Counter] = {}
        for row in rows[1:]:
            for cell in row.findall(f"{{{ns}}}c"):
                index = _col_index(cell.get("r", ""))
                if index is None:
                    continue
                text = _cell_text(cell, shared)
                if not text:
                    continue
                if cell.get("s"):
                    style_count.setdefault(index, Counter())[str(cell.get("s"))] += 1
                if index == columns.get("title"):
                    known.add(normalize(text))
                elif index == columns.get("aliases"):
                    known.update(
                        normalize(part)
                        for part in re.split(r"[,，]", text)
                        if part.strip()
                    )
        column_style = {
            index: counter.most_common(1)[0][0]
            for index, counter in style_count.items()
        }
        # 追加行
        new_row = last_row
        added_count = 0
        skipped = 0
        for title, entry in entries.items():
            if normalize(title) in known:
                skipped += 1
                continue
            new_row += 1
            values = _row_values(title, entry)
            row_el = ET.SubElement(data, f"{{{ns}}}row")
            row_el.set("r", str(new_row))
            row_el.set("spans", "1:16")
            for role, value in values.items():
                if value in (None, ""):
                    continue
                index = columns.get(role)
                if index is None:
                    continue
                cell = ET.SubElement(row_el, f"{{{ns}}}c")
                cell.set("r", f"{_col_letter(index)}{new_row}")
                style = column_style.get(index)
                if style:
                    cell.set("s", style)
                if isinstance(value, float):
                    v_el = ET.SubElement(cell, f"{{{ns}}}v")
                    v_el.text = repr(value)
                elif shared_root is not None:
                    # 共享字符串表存在：追加到 sharedStrings.xml
                    text = str(value)
                    if text in shared:
                        idx = shared.index(text)
                    else:
                        idx = len(shared)
                        shared.append(text)
                        si = ET.SubElement(shared_root, f"{{{ns}}}si")
                        t_el = ET.SubElement(si, f"{{{ns}}}t")
                        t_el.text = text
                    cell.set("t", "s")
                    v_el = ET.SubElement(cell, f"{{{ns}}}v")
                    v_el.text = str(idx)
                else:
                    # 官方导出格式（内联字符串）：新行同样用内联写法
                    cell.set("t", "inlineStr")
                    is_el = ET.SubElement(cell, f"{{{ns}}}is")
                    t_el = ET.SubElement(is_el, f"{{{ns}}}t")
                    t_el.text = str(value)
            added_count += 1
            known.add(normalize(str(values.get("title") or title)))
            known.add(normalize(title))
            for alias in str(values.get("aliases") or "").split(","):
                if alias.strip():
                    known.add(normalize(alias))
        if added_count == 0:
            return 0, skipped
        # 写回 xlsx：内存构建新 zip 后直接覆盖写（文件可能被 Excel 以共享读打开，
        # unlink/replace 会被锁，wb 覆盖写可行）
        sheet_xml = ET.tostring(sheet_root, encoding="utf-8", xml_declaration=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(xlsx_path) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                data_bytes = src.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data_bytes = sheet_xml
                elif (
                    item.filename == "xl/sharedStrings.xml"
                    and shared_root is not None
                ):
                    data_bytes = ET.tostring(
                        shared_root, encoding="utf-8", xml_declaration=True
                    )
                dst.writestr(item, data_bytes)
        with xlsx_path.open("wb") as fh:
            fh.write(buf.getvalue())
        return added_count, skipped


def main() -> int:
    import sys as _sys

    apply_mode = "--apply" in _sys.argv[1:]
    if not CHART_DIR.is_dir():
        print(f"✗ 未找到谱面目录: {CHART_DIR}")
        return 1
    songs, charts = parse_info()
    if not songs:
        print("✗ chart/Info 缺失或为空（游戏更新后请一并上传 Info 文件）")
        return 1
    print(f"Info: {len(songs)} 首曲目, {len(charts)} 张谱面对照")
    updates, added, filled = build_updates(songs, charts)
    if updates:
        # 补充表幂等重建：只保留本次与主表对比的结果（内部名），
        # 避免旧版本（显示名）条目录入
        EXTRA_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXTRA_PATH.write_text(
            json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"✚ 新增 {len(added)} 首曲目")
        for line in added:
            print(f"  - {line}")
        if filled:
            print(f"◔ 补充 {len(filled)} 首已有曲目的缺失难度")
            for line in filled:
                print(f"  - {line}")
        print(f"已写入 {EXTRA_PATH.relative_to(ROOT)}（bot 启动时自动合并）")
    else:
        print("✓ 定数表已是最新，无新增/补充")
    if apply_mode:
        print("\n--apply：合并进正式定数表 constexcel.xlsx")
        added_count, skipped = apply_to_xlsx(updates)
        print(f"  ✔ 新增 {added_count} 行（跳过已在主表的 {skipped} 首）")
        print("  请检查后提交 constexcel.xlsx 并部署")
    return 0


if __name__ == "__main__":
    sys.exit(main())
