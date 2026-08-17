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
from pathlib import Path

# 仓库根目录（本文件位于 <root>/scripts/ 下）
ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = ROOT / "qwwshs" / "plugins" / "bm" / "chart"
EXTRA_PATH = ROOT / "data" / "bm" / "constants_extra.json"

# 谱面难度：Info 对照只含 RL/IL/TT，其余难度留空待人工补
_ALL_DIFFS = ("RL", "IL", "TT", "RU", "DM", "FL")


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
            updates[title] = entry
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
    """
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
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

        def cell_text(cell: ET.Element) -> str | None:
            """单元格文本：兼容共享字符串与内联字符串（官方导出格式）。"""
            v = cell.find(f"{{{ns}}}v")
            if cell.get("t") == "s" and v is not None and v.text:
                try:
                    return shared[int(v.text)]
                except (ValueError, IndexError):
                    return None
            inline = cell.find(f"{{{ns}}}is")
            if inline is not None:
                return "".join(t.text or "" for t in inline.iter(f"{{{ns}}}t"))
            return None

        # 已有曲名（归一化）避免重复追加
        known = set()
        header = rows[0]
        title_col = "A"
        for cell in header.findall(f"{{{ns}}}c"):
            name = cell_text(cell)
            if name and "曲名" in name and "原曲名" not in name:
                title_col = cell.get("r", "A")[0]
        for row in rows[1:]:
            for cell in row.findall(f"{{{ns}}}c"):
                if cell.get("r", "").startswith(title_col):
                    text = cell_text(cell)
                    if text:
                        known.add(normalize(text))
                    break
        # 追加行
        new_row = last_row
        added_count = 0
        skipped = 0
        for title, entry in entries.items():
            if normalize(title) in known:
                skipped += 1
                continue
            new_row += 1
            cells = {
                "A": title,
                "B": entry.get("originalName") or title,
                "C": entry.get("artist") or "",
                "H": entry.get("RL"),
                "J": entry.get("IL"),
                "L": entry.get("TT"),
                "G": entry.get("charter", {}).get("RL", ""),
                "I": entry.get("charter", {}).get("IL", ""),
                "K": entry.get("charter", {}).get("TT", ""),
            }
            # 追加谱面（RU/DM/FL）→ M/N/O 列
            extra_map = {"RU": "RUIN", "DM": "DREAMY", "FL": "FOOL"}
            for diff, extra_type in extra_map.items():
                if entry.get(diff) is not None:
                    cells["M"] = extra_type
                    cells["N"] = entry.get("charter", {}).get(diff, "")
                    cells["O"] = entry.get(diff)
                    break
            row_el = ET.SubElement(data, f"{{{ns}}}row")
            row_el.set("r", str(new_row))
            row_el.set("spans", "1:16")
            for col, value in cells.items():
                if value in (None, ""):
                    continue
                cell = ET.SubElement(row_el, f"{{{ns}}}c")
                cell.set("r", f"{col}{new_row}")
                if isinstance(value, float):
                    cell.set("s", "13")
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
                    cell.set("s", "12")
                    cell.set("t", "s")
                    v_el = ET.SubElement(cell, f"{{{ns}}}v")
                    v_el.text = str(idx)
                else:
                    # 官方导出格式（内联字符串）：新行同样用内联写法
                    cell.set("s", "12")
                    cell.set("t", "inlineStr")
                    is_el = ET.SubElement(cell, f"{{{ns}}}is")
                    t_el = ET.SubElement(is_el, f"{{{ns}}}t")
                    t_el.text = str(value)
            added_count += 1
            known.add(normalize(title))
        if added_count == 0:
            return 0, skipped
        # 写回 xlsx：内存构建新 zip 后直接覆盖写（文件可能被 Excel 以共享读打开，
        # unlink/replace 会被锁，wb 覆盖写可行）
        import io

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
