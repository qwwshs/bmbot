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
                charts[(current, diff)] = {
                    "dlevel": fields.get("DLevel", ""),
                    "charter": fields.get("Charter", ""),
                }
    return songs, charts


def normalize(name: str) -> str:
    """曲名归一化（小写、去多余空白），用于匹配定数表已有曲目。"""
    text = re.sub(r"[_\u3000]+", " ", name.strip())
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


def build_updates(
    songs: dict[str, dict], charts: dict[tuple[str, str], dict]
) -> tuple[dict[str, dict], list[str], list[str]]:
    """对比 Info 与定数表，返回 (待写条目, 新增报告, 补充报告)。

    只同步 Info 中出现的曲目（含定数/谱师/曲师）；已存在的曲目仅补充
    缺失的难度定数与谱师，不覆盖已有字段。
    """
    known = {normalize(t): t for t in load_constants_standalone()}
    base_table = load_constants_standalone()
    updates: dict[str, dict] = {}
    added: list[str] = []
    filled: list[str] = []
    for song_key, song in songs.items():
        title = song["title"] or song_key
        canonical = known.get(normalize(title))
        base = base_table.get(canonical) if canonical else None
        entry = empty_entry(title)
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
            updates[title] = patch
            parts = [f"{d}={v}" for d, v in missing.items() if d != "charter"]
            if "charter" in missing:
                parts.append(
                    "谱师 " + ", ".join(f"{d}:{n}" for d, n in missing["charter"].items())
                )
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


def main() -> int:
    if not CHART_DIR.is_dir():
        print(f"✗ 未找到谱面目录: {CHART_DIR}")
        return 1
    songs, charts = parse_info()
    if not songs:
        print("✗ chart/Info 缺失或为空（游戏更新后请一并上传 Info 文件）")
        return 1
    print(f"Info: {len(songs)} 首曲目, {len(charts)} 张谱面对照")
    updates, added, filled = build_updates(songs, charts)
    if not updates:
        print("✓ 定数表已是最新，无新增/补充")
        return 0
    EXTRA_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if EXTRA_PATH.exists():
        try:
            existing = json.loads(EXTRA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(updates)
    EXTRA_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✚ 新增 {len(added)} 首曲目")
    for line in added:
        print(f"  - {line}")
    if filled:
        print(f"◔ 补充 {len(filled)} 首已有曲目的缺失难度")
        for line in filled:
            print(f"  - {line}")
    print(f"已写入 {EXTRA_PATH.relative_to(ROOT)}（bot 启动时自动合并）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
