#!/usr/bin/env python3
"""检查曲绘覆盖：对照全量定数表，找出 images/ 里缺失的曲绘。

用法（仓库根目录）：
    python scripts/check-covers.py

游戏更新后运行，确认每首曲目都有封面。匹配逻辑与运行时一致
（song.find_cover：曲名/原曲名/别名 + 难度变体 + 空白折叠），
检查范围是全量定数表（xlsx 主表 + constants_extra 合并条目），
比只对照 chart/Info 的旧版检查更全——曾有 4 首缺失因 Info 未收录而漏检。
"""

# ruff: noqa: T201

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BM_DIR = ROOT / "qwwshs" / "plugins" / "bm"


def _load_modules() -> tuple:
    """以假包名独立加载 constants/song（包 __init__ 会触发 NoneBot 初始化）。"""
    pkg = types.ModuleType("bm_standalone")
    pkg.__path__ = [str(BM_DIR)]
    sys.modules["bm_standalone"] = pkg
    constants = importlib.import_module("bm_standalone.constants")
    song = importlib.import_module("bm_standalone.song")
    return constants, song


def main() -> int:
    constants, song = _load_modules()
    consts = constants.get_song_constants()
    print(f"定数表曲目: {len(consts)} 首")
    missing = []
    for name, entry in consts.items():
        if song.find_cover(name, entry) is None:
            missing.append((name, entry))
    if missing:
        print(f"✗ 缺失曲绘 {len(missing)} 首：")
        for name, entry in missing:
            aliases = ", ".join(str(a) for a in entry.get("aliases") or [])
            print(f"  - {name!r}（别名: {aliases or '无'}）")
        print("（个别曲目 APK 内无曲绘资源、需人工补充，见 UPDATE.md 5.1 记录）")
        return 1
    print("✓ 全部曲目都有曲绘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
