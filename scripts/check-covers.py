#!/usr/bin/env python3
"""检查曲绘覆盖：对照 chart/Info 的曲目清单，找出 images/ 里缺失的曲绘。

用法（仓库根目录）：
    python scripts/check-covers.py

游戏更新后运行，确认新曲都有封面。曲绘文件按「内部名」或「显示名」命名
均可匹配（如 ``Infinity.png`` 对应 Info 显示名 ``IF = Infinity``）；
输出缺失曲绘的曲目清单。
"""

# ruff: noqa: T201

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = ROOT / "qwwshs" / "plugins" / "bm" / "chart"
IMAGE_DIR = ROOT / "qwwshs" / "plugins" / "bm" / "images"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())


def main() -> int:
    info_path = CHART_DIR / "Info"
    if not info_path.exists():
        print(f"✗ 未找到 {info_path}")
        return 1
    text = info_path.read_text(encoding="utf-8-sig", errors="replace")
    songs: list[tuple[str, str]] = []  # (内部名, 显示名)
    for block in re.finditer(r"Song::\s*\{\s*(.*?)\s*\};", text, flags=re.DOTALL):
        body = block.group(1)
        key = re.search(r'\$\s*Path\s*=\s*"([^"]*)"', body)
        title = re.search(r'\$\s*Title\s*=\s*"([^"]*)"', body)
        if key:
            songs.append((key.group(1), title.group(1) if title else key.group(1)))
    covers = {norm(f[:-4]) for f in os.listdir(IMAGE_DIR) if f.endswith(".png")}
    missing = [
        (key, title)
        for key, title in songs
        if norm(key) not in covers and norm(title) not in covers
    ]
    print(f"Info 曲目: {len(songs)} 首 | images/ 曲绘: {len(covers)} 个")
    if missing:
        print(f"✗ 缺失曲绘 {len(missing)} 首：")
        for key, title in missing:
            print(f"  - {title} (内部名 {key})")
        return 1
    print("✓ 全部曲目都有曲绘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
