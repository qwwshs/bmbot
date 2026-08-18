# ruff: noqa: T201
"""重建 /bmchartlist all 的全量定数表缓存图（data/bm/all_charts.jpg）。

全量表渲染耗时约一分钟，故落盘缓存、命令直接发缓存图。
restart-bot.sh 部署时自动运行本脚本；定数内容变化（哈希不同）才重建，
因此平时部署只会多花一次哈希计算的时间。
data/ 已 gitignore，缓存不入库，各机器自行生成。
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BM_DIR = ROOT / "qwwshs" / "plugins" / "bm"
CACHE_PATH = ROOT / "data" / "bm" / "all_charts.jpg"
HASH_PATH = ROOT / "data" / "bm" / "all_charts.sha256"


def _load_bm_modules() -> tuple:
    """以假包名加载 constants/render/rating（render 含相对导入，
    且包 __init__ 会触发 NoneBot 初始化，不能直接 import）。"""
    pkg = types.ModuleType("bm_standalone")
    pkg.__path__ = [str(BM_DIR)]
    sys.modules["bm_standalone"] = pkg
    constants = importlib.import_module("bm_standalone.constants")
    render = importlib.import_module("bm_standalone.render")
    rating = importlib.import_module("bm_standalone.rating")
    return constants, render, rating


def main() -> int:
    constants, render, rating = _load_bm_modules()
    consts = constants.get_song_constants()

    # 收集逻辑与插件 _collect_charts 一致（按难度列过滤非定数字段）
    charts: list[tuple[float, str, str]] = []
    for song, entry in consts.items():
        for diff in rating.ALL_DIFFS:
            constant = entry.get(diff)
            if constant is None or float(constant) <= 0:
                continue
            charts.append((float(constant), song, diff))
    charts.sort(key=lambda item: (-item[0], item[1], item[2]))
    if not charts:
        print("✗ 定数表为空，不生成缓存图")
        return 1

    # 曲绘解析结果纳入指纹：每张卡渲染的是曲绘缩略图，只对定数做哈希的话
    # 新增/替换曲绘不会触发重建（v0.7.36 踩坑——补了 3 张曲绘后缓存图
    # 仍是 ♪ 占位）。文件名 + 内容哈希，替换同名文件也能感知。
    cover_parts: list[str] = []
    for _constant, song, diff in charts:
        source = render.resolve_cover(song, diff)
        if source is None:
            cover_parts.append("-")
            continue
        file_hash = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        cover_parts.append(f"{source.name}:{file_hash}")
    digest = hashlib.sha256(
        ("\n".join(f"{c:.1f}\t{song}\t{diff}" for c, song, diff in charts)
         + "\n#covers\n"
         + "\n".join(cover_parts)).encode()
    ).hexdigest()
    if (
        CACHE_PATH.exists()
        and HASH_PATH.exists()
        and HASH_PATH.read_text(encoding="utf-8").strip() == digest
    ):
        print(f"✓ 全量定数表已是最新（{len(charts)} 条谱面），跳过重建")
        return 0

    print(f"◔ 渲染全量定数表（{len(charts)} 条谱面，约一分钟）…")
    img_bytes = render.shrink_for_send(render.render_chart_table(charts))
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(img_bytes)
    HASH_PATH.write_text(digest + "\n", encoding="utf-8")
    size_mb = len(img_bytes) / 1e6
    print(f"✓ 已生成 {CACHE_PATH.relative_to(ROOT)}（{size_mb:.1f}MB, JPEG）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
