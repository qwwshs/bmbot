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
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BM_DIR = ROOT / "qwwshs" / "plugins" / "bm"

# 已知无曲绘、属预期的曲目（键为定数表曲名，比较经 NFC 规范化——
# 日文假名浊点在定数表里是分解形式 は+゙，手打常为预组合 ば）。
# 清单与 UPDATE.md 5.1 保持同步：
# - 游戏已下架（曲绘已删，勿再补充）
# - 当前 APK 无曲绘源（需人工从游戏截图补充，补充后从本清单移除）
EXPECTED_MISSING = {
    "Varcolac",
    "始め恋",
    "MIRЯOЯ",
}

_EXPECTED_MISSING_NFC = {unicodedata.normalize("NFC", n) for n in EXPECTED_MISSING}


def _is_expected_missing(name: str) -> bool:
    return unicodedata.normalize("NFC", name) in _EXPECTED_MISSING_NFC


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
    unexpected = [(n, e) for n, e in missing if not _is_expected_missing(n)]
    if missing:
        print(f"缺失曲绘 {len(missing)} 首：")
        for name, entry in missing:
            mark = "（预期，见 UPDATE.md 5.1）" if _is_expected_missing(name) else ""
            aliases = ", ".join(str(a) for a in entry.get("aliases") or [])
            print(f"  - {name!r}（别名: {aliases or '无'}）{mark}")
    if unexpected:
        print(f"✗ 存在 {len(unexpected)} 首非预期缺失，请补充曲绘或更新预期清单")
        return 1
    if missing:
        print(f"✓ 仅有预期内缺失（{len(missing)} 首，已下架/无源）")
    else:
        print("✓ 全部曲目都有曲绘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
