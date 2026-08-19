"""Berry Melody 查分插件。

命令：
- ``/bmhelp``：查看帮助
- ``/bmbind``：绑定 Berry Melody 存档。发送该命令后，监测该 QQ 发送的下一个
  ``.txt`` 文件（聊天文件或群文件上传均可），读取文件内容作为账号数据
  （从 ``<RSAKeyValue>`` 开始的完整存档，或已解密的 JSON），5 分钟超时
- ``/bmexport``：把绑定过的存档原样导出为游戏可导入的 FormalSave.txt
- ``/bmrating``：以图片输出该玩家的 Rating 查分
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from nonebot import get_plugin_config, on_command, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

from .charter import (
    add_primitive,
    add_related,
    all_primitives,
    get_related,
    group_of,
    is_primitive,
    load_charters,
    remove_primitive,
    remove_related,
    save_charters,
    search_charters,
)
from .chartpreview import (
    DEFAULT_SKIN,
    SKIN_SETS,
    available_diffs,
    find_chart,
    parse_chart,
    render_chart_preview,
)
from .constants import DATA_DIR, ConstantsError, get_song_constants
from .decrypt import (
    DecryptError,
    build_save_text,
    generate_save_key,
    parse_account_data,
)
from .rating import (
    ALL_DIFFS,
    N10_SONG_LIST,
    Chart,
    calculate_chart_potential,
    compute_rating,
    normalize_n10_name,
    parse_scores,
)
from .render import (
    render_card,
    render_card_new,
    render_chart_table,
    render_help_image,
    render_list_image,
    render_score_grid,
    shrink_for_send,
)
from .song import (
    add_alias,
    find_cover,
    format_song_detail,
    get_aliases,
    get_song_scores,
    has_alias,
    load_aliases,
    remove_alias,
    save_aliases,
    search_songs,
)

if TYPE_CHECKING:
    from nonebot.matcher import Matcher

__plugin_meta__ = PluginMetadata(
    name="Berry Melody 查分",
    description="Berry Melody 音游查分：绑定 txt 存档 / 查分图 / 单曲查询",
    usage=(
        "/bmhelp\n/bmbind\n/bmexport\n"
        "/bmrating [QQ号]（超管可查他人）\n/bmsong <曲名>\n/bmn10"
    ),
)


class BmConfig(BaseModel):
    """插件配置（.env 中 ``BM_ADMIN_QQS`` / ``BM_SUPER_ADMIN_QQ`` 可覆盖）。"""

    # 允许使用 /bmaddname 的 QQ 白名单（默认空，由 .env 配置）
    bm_admin_qqs: set[int] = set()
    # 超管 QQ：唯一可管理白名单的人（未配置则无人可管理）
    bm_super_admin_qq: int | None = None
    # QQ 客户端容器名（NapCat/snowluma 跑在 Docker 里，导出文件需写入容器内路径）
    bm_qq_container: str = "snowluma"


bm_config = get_plugin_config(BmConfig)

# 绑定数据存于插件包外的项目级目录，部署覆盖插件目录不会丢失
_DATA_DIR = DATA_DIR
_BINDINGS_DIR = _DATA_DIR / "bindings"
_BINDINGS_PATH = _DATA_DIR / "bindings.json"
_BINDINGS_BAK = _DATA_DIR / "bindings.json.bak"
_ALIASES_PATH = _DATA_DIR / "aliases.json"
_WHITELIST_PATH = _DATA_DIR / "whitelist.json"
_CHARTERS_PATH = _DATA_DIR / "charters.json"
_RATING_STYLES_PATH = _DATA_DIR / "ratingstyles.json"
# 旧位置（插件包内 data/），用于自动迁移
_OLD_BINDINGS_PATH = Path(__file__).resolve().parent / "data" / "bindings.json"
_FILE_WAIT_TTL = 300.0
_SONG_PICK_TTL = 120.0
_ALIAS_MAX_LEN = 30

# 插件版本：修复/小改动 +0.0.1，新增功能 +0.1
BM_VERSION = "0.8.1"

# QQ 号 -> {data: 解密后的账号 JSON, name: 玩家名, bind_time: 时间戳}
_bindings: dict[str, dict] = {}
# QQ 号 -> 等待存档文件绑定的开始时间（monotonic）
_awaiting_file: dict[str, float] = {}
# QQ 号 -> {names: 模糊搜索结果, expire: 过期时间}（bmsong 序号选择）
_song_pending: dict[str, dict] = {}
# QQ 号 -> {stage: song/diff, names: 曲名列表, song: 已选曲名, diffs: 难度列表,
#           expire: 过期时间}（bmchart 先选曲再选难度）
_chart_pending: dict[str, dict] = {}
# QQ 号 -> 音符皮肤名（/bmskin 切换，缺省 DEFAULT_SKIN；谱面指定皮肤时优先）
_chart_skins: dict[str, str] = {}
# QQ 号 -> 过期时间（bmskin 列表后的序号/皮肤名选择）
_skin_pending: dict[str, float] = {}
# QQ 号 -> {alias: 别名, names: 搜索结果, expire: 过期时间}（bmaddname 流程）
_addname_pending: dict[str, dict] = {}
# QQ 号 -> {alias: 别名, names: 搜索结果, expire: 过期时间}（bmremovename 流程）
_remove_pending: dict[str, dict] = {}
# QQ 号 -> 谱师流程状态（bmcharter / 基元谱师管理，见 handle_charter_pick）
_charter_pending: dict[str, dict] = {}

load_aliases(_ALIASES_PATH)
load_charters(_CHARTERS_PATH)


def _load_whitelist() -> set[int]:
    """白名单：.env 默认 + whitelist.json 运行时增删。"""
    whitelist = set(bm_config.bm_admin_qqs)
    try:
        data = json.loads(_WHITELIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return whitelist
    if isinstance(data, list):
        for qq in data:
            if str(qq).isdigit():
                whitelist.add(int(qq))
    return whitelist


def _save_whitelist() -> None:
    _WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WHITELIST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(_whitelist)), encoding="utf-8")
    tmp.replace(_WHITELIST_PATH)


_whitelist = _load_whitelist()


# QQ -> 查分样式（"new" 新版默认 / "old" 旧版）
_rating_styles: dict[str, str] = {}


def _load_rating_styles() -> None:
    """加载各用户的查分样式选择。"""
    try:
        data = json.loads(_RATING_STYLES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        _rating_styles.update(
            {str(k): v for k, v in data.items() if v in ("new", "old")}
        )


def _save_rating_styles() -> None:
    """持久化查分样式选择。"""
    _RATING_STYLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RATING_STYLES_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(_rating_styles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(_RATING_STYLES_PATH)


def _toggle_rating_style(qq: str) -> str:
    """切换用户的查分样式，返回新样式名。"""
    current = _rating_styles.get(qq, "new")
    new = "old" if current == "new" else "new"
    _rating_styles[qq] = new
    _save_rating_styles()
    return new


_load_rating_styles()


def _whitelist_text() -> str:
    return "、".join(map(str, sorted(_whitelist)))


def _can_manage_alias(user_id: int) -> bool:
    """是否有别名操作权限：白名单成员或超管。"""
    return user_id in _whitelist or user_id == bm_config.bm_super_admin_qq


try:
    SONG_CONSTANTS = get_song_constants()
    logger.info(f"Berry Melody 定数表加载完成：{len(SONG_CONSTANTS)} 首曲目")
except ConstantsError as exc:
    logger.error(f"Berry Melody 定数表加载失败: {exc}")
    SONG_CONSTANTS = {}


def _load_bindings() -> None:
    """加载绑定：``bindings/<qq>.json`` 每用户一个文件。

    兼容旧格式：合并文件（``bindings.json`` / 备份 / 插件内旧位置）
    中尚未拆分到新目录的用户会被补入并迁移。
    """
    if _BINDINGS_DIR.is_dir():
        for path in sorted(_BINDINGS_DIR.glob("*.json")):
            binding = _read_binding_file(path)
            if binding is not None:
                _bindings[path.stem] = binding
    if _merge_legacy_bindings():
        _save_bindings()


def _read_binding_file(path: Path) -> dict | None:
    """读取单个用户绑定文件，损坏返回 None。"""
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(f"跳过损坏的绑定文件: {path}")
        return None
    if isinstance(binding, dict) and isinstance(binding.get("data"), dict):
        return binding
    return None


def _merge_legacy_bindings() -> bool:
    """从旧合并文件补入尚未拆分的用户，返回是否有新增。"""
    migrated = False
    for path in (_BINDINGS_PATH, _BINDINGS_BAK, _OLD_BINDINGS_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for qq, binding in data.items():
            if (
                qq not in _bindings
                and isinstance(binding, dict)
                and isinstance(binding.get("data"), dict)
            ):
                _bindings[qq] = binding
                migrated = True
        break  # 取第一个可用的旧合并文件补漏
    return migrated


def _save_bindings() -> None:
    """按 QQ 拆分保存绑定：``data/bm/bindings/<qq>.json``。"""
    _BINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    for qq, binding in _bindings.items():
        path = _BINDINGS_DIR / f"{qq}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)


def _store_binding(qq: str, data: dict, raw_b64: str | None = None) -> str:
    binding: dict = {
        "data": data,
        "name": str(data.get("AccountName") or ""),
        "bind_time": time.time(),
    }
    if raw_b64:
        binding["raw_b64"] = raw_b64
    _bindings[qq] = binding
    _save_bindings()
    name = str(data.get("AccountName") or "未知玩家")
    return f"✅ 绑定成功！\n玩家：{name}\n发送 /bmrating 查看 Rating"


def _decode_text(raw: bytes) -> str:
    """按 UTF-16(BOM) → UTF-8 → GBK → 宽松替换的顺序解码文件内容。

    部分导出工具会把存档存成 UTF-16 编码（带 BOM）：这类字节流按 UTF-8
    解码不会报错，但会产生大量 NUL 字符穿插在 <RSAKeyValue> 标签中间，
    必须先按 BOM 检测 UTF-16，否则无法识别标签。
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace")


def _file_name(data: dict) -> str:
    """从文件段提取文件名：优先 name，其次从 file/url 路径提取。"""
    name = str(data.get("name") or "").strip()
    if name:
        return name
    for key in ("file", "url"):
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        path = value.split("?", 1)[0].removeprefix("file://").lstrip("/")
        extracted = Path(path).name
        if extracted:
            return extracted
    return ""


def _is_txt_name(name: str) -> bool:
    """是否为可接受的存档文件名：.txt 或无法判断扩展名时放行。"""
    suffix = Path(name).suffix.lower()
    return not suffix or suffix == ".txt"


def _decode_base64(value: str) -> bytes | None:
    """解码 base64 内容（支持 base64:// 前缀），失败返回 None。"""
    value = value.removeprefix("base64://")
    try:
        return base64.b64decode(value, validate=False)
    except (ValueError, TypeError):
        return None


async def _fetch_bytes(url: str) -> bytes | None:
    try:
        # 短超时：NapCat 文件服务的 url 常为本机地址，远程部署时不可达，
        # 快速失败回退到 get_file 等途径，避免长时间卡住
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except (httpx.HTTPError, OSError):
        return None


async def _read_local_path(raw: str) -> bytes | None:
    """读取本地绝对路径文件，路径不可用返回 None。"""
    path = raw
    if path.startswith("file://"):
        path = path[len("file://") :]
        # 仅 Windows 盘符形式 file:///C:/x → C:/x；Linux 绝对路径保留原样
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":  # noqa: PLR2004
            path = path[1:]
    if not path or not Path(path).is_absolute():
        return None
    try:
        return await asyncio.to_thread(Path(path).read_bytes)
    except OSError:
        return None


async def _get_file_via_api(bot: Bot, file_id: str) -> dict:
    """调用 OneBot V11 get_file 动作，返回 data 字典。"""
    try:
        result = await bot.call_api("get_file", file_id=file_id)
    except Exception as exc:
        logger.error(f"get_file 调用失败: {exc}")
        raise ValueError("get_file失败") from exc
    if not isinstance(result, dict):
        raise TypeError("get_file返回异常")
    logger.info(f"get_file 返回: {result}")
    return result


async def _content_from_result(result: dict) -> bytes:
    """从 get_file 返回的 data（base64/url/file 字段）中解析文件内容。"""
    for key in ("base64", "url", "file"):
        value = str(result.get(key) or "").strip()
        if not value:
            continue
        if key == "base64" or value.startswith("base64://"):
            content = _decode_base64(value)
            if content is not None:
                logger.debug(f"get_file 来源: {key} base64")
                return content
        if value.startswith(("http://", "https://")):
            content = await _fetch_bytes(value)
            if content is not None:
                logger.debug(f"get_file 来源: {key} url")
                return content
        content = await _read_local_path(value)
        if content is not None:
            logger.debug(f"get_file 来源: {key} 本地路径")
            return content
    raise ValueError("无法获取文件内容")


async def _read_file(bot: Bot, data: dict) -> bytes:
    """按 url → base64 → 本地路径 → get_file API 的顺序获取文件内容。"""
    url = str(data.get("url") or "").strip()
    if url:
        content = await _fetch_bytes(url)
        if content is not None:
            logger.debug(f"文件来源: url {url}")
            return content
        logger.warning(f"url 下载失败: {url}")
    raw_file = str(data.get("file") or "").strip()
    if raw_file.startswith("base64://"):
        content = _decode_base64(raw_file)
        if content is not None:
            logger.debug("文件来源: base64 内容")
            return content
    content = await _read_local_path(raw_file)
    if content is not None:
        logger.debug(f"文件来源: 本地路径 {raw_file}")
        return content
    if raw_file:
        logger.warning(f"本地路径不可读: {raw_file!r}")
    if not raw_file:
        raise ValueError("文件段无有效字段")
    # 用 NapCat 提供的 file_id（UUID）调 get_file，缺失时回退 file 字段
    file_id = str(data.get("file_id") or "").strip() or raw_file
    logger.warning(f"尝试 get_file: {file_id!r}")
    result = await _get_file_via_api(bot, file_id)
    return await _content_from_result(result)


async def _process_file(bot: Bot, qq: str, data: dict) -> str:
    """读取文件内容并绑定账号，返回提示信息。

    存档为 RSA 格式时把原始文件字节存为 base64（供 /bmexport 原样导出，
    保证游戏可直接导入，无需重新加密）。
    """
    try:
        raw = await _read_file(bot, data)
        content = _decode_text(raw)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return f"❌ 读取文件失败：{exc}"
    try:
        account = parse_account_data(content)
    except DecryptError as exc:
        return f"❌ 绑定失败：{exc}"
    raw_b64 = (
        base64.b64encode(raw).decode("ascii") if "<RSAKeyValue>" in content else None
    )
    return _store_binding(qq, account, raw_b64)


bm_help = on_command("bmhelp", priority=5, block=True)
bm_bind = on_command("bmbind", priority=5, block=True)
bm_export = on_command("bmexport", priority=5, block=True)
bm_export_revoke = on_message(priority=10, block=False)
bm_rating = on_command("bmrating", priority=5, block=True)
bm_song = on_command("bmsong", priority=5, block=True)
bm_version = on_command("bmbotversion", priority=5, block=True)
bm_addname = on_command("bmaddname", priority=5, block=True)
bm_remove_name = on_command("bmremovename", priority=5, block=True)
bm_name_list = on_command("bmnamelist", priority=5, block=True)
bm_whitelist_add = on_command("bmaddtowhitelist", priority=5, block=True)
bm_whitelist_remove = on_command("bmremovefromwhitelist", priority=5, block=True)
bm_charter = on_command("bmcharter", priority=5, block=True)
bm_charter_setup = on_command("bmsetuptheprimitivecharter", priority=5, block=True)
bm_charter_related = on_command("bmrelatedcharter", priority=5, block=True)
bm_charter_remove = on_command("bmremoverelatedcharter", priority=5, block=True)
bm_charter_list = on_command("bmrelatedcharterlist", priority=5, block=True)
bm_charter_unset = on_command("bmremovetheprimitivecharter", priority=5, block=True)
bm_chart = on_command("bmchartlist", priority=5, block=True)
bm_chart_preview = on_command("bmchart", priority=5, block=True)
bm_skin = on_command("bmskin", priority=5, block=True)
bm_skin_pick = on_message(priority=10, block=False)
bm_random = on_command("bmrandom", priority=5, block=True)
bm_n10 = on_command("bmn10", priority=5, block=True)
bm_rating_style = on_command("bmratingstyle", priority=5, block=True)
bm_file_watch = on_message(priority=10, block=False)
bm_song_pick = on_message(priority=10, block=False)
bm_chart_pick = on_message(priority=10, block=False)
bm_addname_pick = on_message(priority=10, block=False)
bm_remove_pick = on_message(priority=10, block=False)
bm_charter_pick = on_message(priority=10, block=False)
bm_group_upload = on_notice(priority=5, block=False)

_load_bindings()


# 帮助文本（/bmhelp 渲染为图片）
_HELP_TEXT = (
    "🎵 Berry Melody 查分 Bot\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "/bmhelp — 查看本帮助\n"
    "/bmbind — 绑定存档（任选一种方法）\n"
    "   ① 安卓通用（所有机型）：\n"
    "      /bmbind → 打开文件管理 → Android → data →\n"
    "      com.skywaystudio.BerryMelody → files →\n"
    "      长按 FormalSave.txt 并发送至刚才的群聊\n"
    "      （部分机型选 data 后可能跳转一次，其他步骤照常）\n"
    "   ② 游戏内导出：进入 Berry Melody，切换角色为克莱因，导出存档，\n"
    "      将导出的存档放入 txt 文件再发送（聊天文件或群文件均可）\n"
    "   ③ iOS：复制存档到 Pages，新建空白文档，粘贴，\n"
    "      点右上角分享 → 纯文本 → 导出并发送\n"
    "   ④ vivo/iQOO：复制存档进原子笔记/备忘录，点右上角 →\n"
    "      导出文件 → 文本，成功后点分享发到群里\n"
    "   ⑤ OPPO：导出方法同①安卓通用法\n"
    "   内容：从 <RSAKeyValue> 开始的完整存档（FormalSave.txt），\n"
    "   或已解密的 JSON 文本\n"
    "/bmexport — 导出自己绑定过的存档（游戏可直接导入的 FormalSave.txt）\n"
    "/bmrating — 以图片输出你的 Rating 查分\n"
    "/bmratingstyle — 切换查分样式（新版/旧版）\n"
    "/bmsong <曲名> — 单曲查询（支持模糊搜索）\n"
    "/bmaddname <别名> — 为歌曲添加自定义别名（白名单）\n"
    "/bmremovename <别名> — 删除歌曲别名（白名单）\n"
    "/bmnamelist — 查看全部别名对应关系（白名单）\n"
    "/bmaddtowhitelist <QQ> — 添加白名单（超管）\n"
    "/bmremovefromwhitelist <QQ> — 移除白名单（超管）\n"
    "/bmcharter <谱师> — 按谱师查询谱面（回复序号查看歌曲详情）\n"
    "/bmsetuptheprimitivecharter <谱师> — 设置基元谱师名义（白名单）\n"
    "/bmremovetheprimitivecharter <谱师> — 移除基元谱师名义（白名单）\n"
    "/bmrelatedcharter <基元谱师> — 添加关联谱师名义（白名单）\n"
    "/bmremoverelatedcharter <基元谱师> — 解除关联谱师名义（白名单）\n"
    "/bmrelatedcharterlist — 查看全部谱师关联（白名单）\n"
    "/bmchartlist <定数1> [定数2] [难度...] [分数] — 按定数区间生成定数表图\n"
    "   13 表示 13.0~13.5，13+ 表示 13.6~13.9，13.4 表示精确 13.4\n"
    "   末尾加 score 启用成绩渲染模式（需绑定存档）\n"
    "/bmrandom <定数1> [定数2] [难度...] — 在定数区间内随机挑一首曲目\n"
"/bmchart <曲名> — 谱面预览图（先选曲目再选难度）\n"
"/bmn10 — 查看 N10 固定曲池（20 首）\n"
"/bmskin — 切换谱面预览的音符皮肤\n"
"/bmbotversion — 查看 bot 版本\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📱 存档位置：/Android/data/com.skywaystudio.BerryMelody/files/FormalSave.txt"
)


@bm_help.handle()
async def handle_help() -> None:
    img_bytes = await asyncio.to_thread(render_help_image, _HELP_TEXT)
    await bm_help.finish(MessageSegment.image(img_bytes))


@bm_bind.handle()
async def handle_bind(event: MessageEvent) -> None:
    qq = str(event.user_id)
    _awaiting_file[qq] = time.monotonic()
    await bm_bind.finish(
        "📄 请导出存档并发送 txt 文件（5 分钟内有效），任选一种方法：\n"
        "① 安卓通用：/bmbind → 文件管理 → Android → data →\n"
        "   com.skywaystudio.BerryMelody → files →\n"
        "   长按 FormalSave.txt 并发送至刚才的群聊\n"
        "   （部分机型选 data 后可能跳转一次，其他步骤照常）\n"
        "② 游戏内导出：切换角色为克莱因 → 导出存档 → 放入 txt 文件发送\n"
        "③ iOS：复制存档到 Pages → 分享 → 纯文本 → 导出并发送\n"
        "④ vivo/iQOO：复制进原子笔记 → 导出文件 → 文本 → 分享到本群\n"
        "⑤ OPPO：导出方法同①安卓通用法\n"
        "（内容为从 <RSAKeyValue> 开始的完整存档，或已解密的 JSON 文本）"
    )


@bm_export_revoke.handle()
async def handle_export_revoke(bot: Bot, event: MessageEvent) -> None:
    """导出文件后 60 秒内回复 1：立即撤回（删除群文件）。"""
    qq = str(event.user_id)
    if qq not in _pending_export_revoke:
        return
    if event.message.extract_plain_text().strip() != "1":
        return
    if await _revoke_export_file(bot, qq):
        await bm_export_revoke.send("✅ 已撤回导出文件")
    else:
        await bm_export_revoke.send("⚠️ 撤回失败，请手动删除群文件")


@bm_version.handle()
async def handle_version() -> None:
    await bm_version.finish(f"🤖 Berry Melody 查分 Bot v{BM_VERSION}")


# QQ 客户端（NapCat/snowluma）跑在 Docker 容器里：导出文件需写入容器内
# 数据卷（/app/data），上传 API 传容器内路径；主机路径容器看不到
_CONTAINER_EXPORT_DIR = "/app/data/exports"
# 导出文件消息的自动撤回时间（秒）；期间用户回复 1 立即撤回
_EXPORT_REVOKE_SECONDS = 60
# 群文件 busid（upload_group_file 上传的群文件固定为 102）
_GROUP_FILE_BUSID = 102
# QQ -> (群号, 群文件 file_id)。QQ 协议下 bot 发的文件消息无法撤回，
# 只能删除群文件存储（实测有效）；私聊无删除 API，不登记
_pending_export_revoke: dict[str, tuple[int, str]] = {}
# 自动撤回任务引用（防 GC，完成后自动移除）
_revoke_tasks: set[asyncio.Task] = set()


async def _copy_export_to_container(qq: str, content: bytes) -> str:
    """把导出文件写入 QQ 客户端容器数据卷，返回容器内路径。"""
    container_path = f"{_CONTAINER_EXPORT_DIR}/{qq}.txt"
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        bm_config.bm_qq_container,
        "sh",
        "-c",
        f"mkdir -p {_CONTAINER_EXPORT_DIR} && cat > {container_path}",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate(content)
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"写入 QQ 容器失败：{detail}")  # noqa: TRY003
    return container_path


async def _remove_export_from_container(qq: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        bm_config.bm_qq_container,
        "rm",
        "-f",
        f"{_CONTAINER_EXPORT_DIR}/{qq}.txt",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _revoke_export_file(bot: Bot, qq: str) -> bool:
    """撤回导出的文件：删除群文件存储（QQ 协议下文件消息本身不可撤回）。

    返回是否成功；未登记（已撤回/私聊）时视为成功。
    """
    pending = _pending_export_revoke.pop(qq, None)
    if pending is None:
        return True
    group_id, file_id = pending
    try:
        await bot.call_api(
            "delete_group_file",
            group_id=group_id,
            file_id=file_id,
            busid=_GROUP_FILE_BUSID,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"删除导出群文件失败 (file_id={file_id}): {exc}")
        return False
    logger.info(f"已删除导出群文件 {file_id}")
    return True


async def _auto_revoke_export(bot: Bot, qq: str) -> None:
    """导出 _EXPORT_REVOKE_SECONDS 秒后自动删除文件（已撤回则跳过）。"""
    await asyncio.sleep(_EXPORT_REVOKE_SECONDS)
    if qq not in _pending_export_revoke:
        return
    await _revoke_export_file(bot, qq)


@bm_export.handle()
async def handle_export(
    bot: Bot, event: MessageEvent
) -> None:
    """导出自己的存档（游戏可直接导入的 FormalSave.txt）。

    绑定时保存过原始文件则原样返回；旧绑定未保存原始文件的，
    用新生成的 RSA 密钥把账号 JSON 重新加密为游戏格式导出。
    群聊导出后 _EXPORT_REVOKE_SECONDS 秒内自动删除群文件，期间回复 1
    立即删除；私聊无删除 API，不自动撤回。
    """
    qq = str(event.user_id)
    binding = _bindings.get(qq)
    if binding is None:
        await bm_export.finish("❌ 尚未绑定存档，请先 /bmbind 绑定后再导出")
    note = ""
    raw_b64 = binding.get("raw_b64")
    if raw_b64:
        try:
            content = base64.b64decode(raw_b64)
        except ValueError:
            await bm_export.finish("❌ 存档数据损坏，请重新 /bmbind")
    else:
        # 旧绑定：用新密钥重新加密（游戏用存档内嵌私钥解密，新密钥可直接导入）
        try:
            key = generate_save_key()
            content = build_save_text(key, binding["data"]).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"导出重新加密失败: {exc}")
            await bm_export.finish(f"❌ 重新加密导出失败：{exc}")
        note = "（旧绑定存档，已用新密钥重新加密，可直接导入游戏）"
    try:
        container_path = await _copy_export_to_container(qq, content)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"写入 QQ 容器失败: {exc}")
        await bm_export.finish(f"❌ 写入 QQ 客户端容器失败：{exc}")
    # 上传到群文件/私聊文件；群文件返回 file_id 用于撤回删除
    file_id: str | None = None
    try:
        if isinstance(event, GroupMessageEvent):
            result = await bot.call_api(
                "upload_group_file",
                group_id=event.group_id,
                file=container_path,
                name="FormalSave.txt",
            )
            if isinstance(result, dict):
                file_id = result.get("file_id")
        else:
            await bot.call_api(
                "upload_private_file",
                user_id=event.user_id,
                file=container_path,
                name="FormalSave.txt",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"导出上传失败: {exc}")
        await _remove_export_from_container(qq)
        await bm_export.finish(f"❌ 上传存档文件失败：{exc}")
    await _remove_export_from_container(qq)
    if file_id:
        _pending_export_revoke[qq] = (event.group_id, file_id)
        task = asyncio.create_task(_auto_revoke_export(bot, qq))
        _revoke_tasks.add(task)
        task.add_done_callback(_revoke_tasks.discard)
        revoke_note = (
            f"文件将在 {_EXPORT_REVOKE_SECONDS} 秒后自动撤回，期间回复 1 可立即撤回"
        )
    else:
        revoke_note = "⚠️ 私聊导出无法自动撤回，请及时保存并手动删除文件"
    await bm_export.finish(
        f"✅ 已导出存档（FormalSave.txt）{note}\n{revoke_note}\n"
        "保存到游戏存档目录后重进游戏即可导入：\n"
        "Android/data/com.skywaystudio.BerryMelody/files/"
    )


@bm_file_watch.handle()
async def handle_file_watch(bot: Bot, event: MessageEvent) -> None:
    """监测等待绑定用户的聊天文件（私聊/群聊文件消息）。"""
    qq = str(event.user_id)
    start = _awaiting_file.get(qq)
    if start is None:
        return
    now = time.monotonic()
    if now - start > _FILE_WAIT_TTL:
        _awaiting_file.pop(qq, None)
        await bm_file_watch.send("⏰ 等待文件超时，绑定已取消，请重新发送 /bmbind")
        return
    file_seg = next((seg for seg in event.message if seg.type == "file"), None)
    if file_seg is None:
        _awaiting_file[qq] = now
        await bm_file_watch.send(
            "📄 等待的是存档 txt 文件，请直接发送文件（不要发文本）"
        )
        return
    name = _file_name(file_seg.data)
    if not _is_txt_name(name):
        _awaiting_file[qq] = now
        await bm_file_watch.send(f"⚠️ 收到文件 {name!r}，不是 .txt 存档，请重新发送")
        return
    _awaiting_file.pop(qq, None)
    logger.info(f"绑定文件段: {file_seg.data}")
    result = await _process_file(bot, qq, file_seg.data)
    await bm_file_watch.send(result)
    # 群聊中读取完存档后撤回文件消息（保护存档隐私），仅当 bot 是群管理
    if isinstance(event, GroupMessageEvent):
        await _revoke_file_message(bot, event)


@bm_group_upload.handle()
async def handle_group_upload(bot: Bot, event: GroupUploadNoticeEvent) -> None:
    """监测等待绑定用户的群文件上传（部分客户端走群文件通知而非消息）。"""
    qq = str(event.user_id)
    if qq not in _awaiting_file:
        return
    if not _is_txt_name(event.file.name):
        _awaiting_file[qq] = time.monotonic()
        await bot.send_group_msg(
            group_id=event.group_id,
            message=f"⚠️ 收到文件 {event.file.name!r}，不是 .txt 存档，请重新发送",
        )
        return
    _awaiting_file.pop(qq, None)
    try:
        # 适配器未内置 get_group_file_url，经 call_api 调用并取 data.url
        result = await bot.call_api(
            "get_group_file_url",
            group_id=event.group_id,
            file_id=event.file.id,
            busid=event.file.busid,
        )
        url = result.get("url") if isinstance(result, dict) else str(result)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"获取群文件地址失败: {exc}")
        await bot.send_group_msg(
            group_id=event.group_id, message=f"❌ 获取文件地址失败：{exc}"
        )
        return
    result = await _process_file(bot, qq, {"url": url})
    await bot.send_group_msg(group_id=event.group_id, message=result)
    # 读取完存档后删除群文件（保护存档隐私），仅当 bot 是群管理
    await _revoke_group_file(bot, event)


async def _revoke_file_message(bot: Bot, event: GroupMessageEvent) -> None:
    """撤回群内发送的存档文件消息（无条件尝试，失败仅记日志）。"""
    try:
        await bot.delete_msg(message_id=event.message_id)
        logger.info(f"已撤回存档文件消息 {event.message_id}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"撤回存档消息失败 (mid={event.message_id}): {exc}")


async def _revoke_group_file(bot: Bot, event: GroupUploadNoticeEvent) -> None:
    """删除群文件中的存档（无条件尝试，失败仅记日志）。"""
    try:
        await bot.call_api(
            "delete_group_file",
            group_id=event.group_id,
            file_id=event.file.id,
            busid=event.file.busid,
        )
        logger.info(f"已删除群文件 {event.file.name}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"删除群文件失败: {exc}")


async def _render_rating_image(qq: str, data: dict) -> tuple[bytes, str]:
    """按绑定存档渲染查分图，返回 (图片字节, 未收录提示文本)。"""
    charts, grade_counts, missing = parse_scores(data, SONG_CONSTANTS)
    if not charts:
        raise ValueError("存档中没有可计算的成绩（可能绑定了错误的存档）")
    result = compute_rating(charts, data.get("Potential"))
    result.missing = missing
    binding = _bindings.get(qq) or {}
    player_name = binding.get("name") or str(data.get("AccountName") or "未知玩家")
    style = _rating_styles.get(qq, "new")
    if style == "old":
        img_bytes = await asyncio.to_thread(
            render_card, player_name, result, grade_counts, data.get("Potential")
        )
    else:
        img_bytes = await asyncio.to_thread(
            render_card_new,
            player_name,
            result,
            grade_counts,
            data.get("Potential"),
            data.get("CharSelect"),
            data.get("Glass"),
            data.get("Quantum"),
        )
    note = f"\n⚠️ 有 {len(missing)} 首成绩未在定数表中，不计入" if missing else ""
    return img_bytes, note


@bm_rating.handle()
async def handle_rating(event: MessageEvent, arg: Message = CommandArg()) -> None:
    query = arg.extract_plain_text().strip()
    # 超管查询他人：/bmrating <QQ号>
    if query:
        if event.user_id != bm_config.bm_super_admin_qq:
            await bm_rating.finish("❌ 查询他人查分卡仅限超管使用")
        if not query.isdigit():
            await bm_rating.finish("用法：/bmrating [QQ号]（超管可查询指定玩家）")
        target = _bindings.get(query)
        if target is None:
            await bm_rating.finish(f"❌ QQ {query} 没有绑定存档")
        if not SONG_CONSTANTS:
            await bm_rating.finish("❌ 定数表未加载，无法查分")
        data = target["data"]
        await bm_rating.send(f"⏳ 正在生成 QQ {query} 的查分卡，请稍候…")
        try:
            img_bytes, note = await _render_rating_image(query, data)
        except ValueError as exc:
            await bm_rating.finish(f"❌ QQ {query} 的{exc}")
        await bm_rating.finish(MessageSegment.image(img_bytes) + note)
    # 普通自查
    qq = str(event.user_id)
    binding = _bindings.get(qq)
    if binding is None:
        await bm_rating.finish(
            "❌ 你还没有绑定存档\n请先发送 /bmbind 并发送存档 txt 文件"
        )
    if not SONG_CONSTANTS:
        await bm_rating.finish("❌ 定数表未加载，无法查分")
    data = binding["data"]
    await bm_rating.send("⏳ 生成中，请稍候…")
    try:
        img_bytes, note = await _render_rating_image(qq, data)
    except ValueError as exc:
        await bm_rating.finish(f"❌ {exc}")
    await bm_rating.finish(MessageSegment.image(img_bytes) + note)


async def _send_song_detail(matcher: Matcher, qq: str, name: str) -> None:
    """发送单曲详情：曲名/曲师/成绩文本 + 曲绘图片。"""
    entry = SONG_CONSTANTS[name]
    binding = _bindings.get(qq)
    if binding is None:
        scores: list[tuple[str, int, str]] = []
        hint = "\n💡 发送 /bmbind 绑定存档后可查看成绩"
    else:
        scores = get_song_scores(binding["data"], name, entry)
        hint = ""
    text = format_song_detail(name, entry, scores) + hint
    cover = find_cover(name, entry)
    if cover is not None:
        # 以字节发送（base64），远程部署时 NapCat 无法访问服务器本地路径
        cover_bytes = await asyncio.to_thread(cover.read_bytes)
        await matcher.finish(
            MessageSegment.text(text) + MessageSegment.image(cover_bytes)
        )
    await matcher.finish(text)


@bm_song.handle()
async def handle_song(event: MessageEvent, arg: Message = CommandArg()) -> None:
    qq = str(event.user_id)
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_song.finish("用法：/bmsong <曲名>\n例如：/bmsong ether vortex")
    if not SONG_CONSTANTS:
        await bm_song.finish("❌ 定数表未加载，无法查询")
    names = search_songs(SONG_CONSTANTS, query)
    if not names:
        await bm_song.finish(f"❌ 未找到与「{query}」相关的曲目")
    if len(names) == 1:
        # 单个命中（精确或唯一模糊）直接返回详情
        await _send_song_detail(bm_song, qq, names[0])
        return
    _song_pending[qq] = {"names": names, "expire": time.monotonic() + _SONG_PICK_TTL}
    lines = [f"🔍 找到 {len(names)} 首相关曲目，回复序号查看详情："]
    lines.extend(f"{i + 1}. {_display_name(name)}" for i, name in enumerate(names))
    lines.append(f"（{_SONG_PICK_TTL:.0f} 秒内有效）")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_song.finish(MessageSegment.image(img_bytes))


def _display_name(name: str) -> str:
    """列表展示名：有原曲名显示「原曲名<名称>」，无则只显示名称。"""
    entry = SONG_CONSTANTS.get(name)
    if entry is None:
        return name
    original = str(entry.get("originalName") or "").strip()
    if original and original != name:
        return f"{original}<{name}>"
    return name


@bm_song_pick.handle()
async def handle_song_pick(event: MessageEvent) -> None:
    """bmsong 模糊搜索结果的选择：接收纯数字序号。"""
    qq = str(event.user_id)
    state = _song_pending.get(qq)
    if state is None:
        return
    if time.monotonic() > state["expire"]:
        _song_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    if not text.isdigit():
        return
    index = int(text)
    names = state["names"]
    _song_pending.pop(qq, None)
    if index < 1 or index > len(names):
        await bm_song_pick.send(f"❌ 序号无效（1-{len(names)}），请重新 /bmsong 查询")
        return
    await _send_song_detail(bm_song_pick, qq, names[index - 1])


@bm_addname.handle()
async def handle_addname(event: MessageEvent, arg: Message = CommandArg()) -> None:
    """添加歌曲别名：/bmaddname <别名> → 输入原名 → 绑定。"""
    qq = str(event.user_id)
    if not _can_manage_alias(event.user_id):
        await bm_addname.finish("❌ 你没有权限使用此命令")
    alias = arg.extract_plain_text().strip()
    if not alias:
        await bm_addname.finish("用法：/bmaddname <别名>\n例如：/bmaddname 黑猫")
    if len(alias) > _ALIAS_MAX_LEN:
        await bm_addname.finish(f"❌ 别名过长（最多 {_ALIAS_MAX_LEN} 字）")
    _addname_pending[qq] = {
        "alias": alias,
        "names": [],
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    await bm_addname.finish(
        f"📝 已收到别名「{alias}」\n请输入对应的原曲名（支持模糊搜索）："
    )


@bm_addname_pick.handle()
async def handle_addname_pick(event: MessageEvent) -> None:
    """bmaddname 流程：接收原名（搜索）或序号（选择结果）。"""
    qq = str(event.user_id)
    state = _addname_pending.get(qq)
    if state is None:
        return
    if time.monotonic() > state["expire"]:
        _addname_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    if not text:
        return
    if state["names"]:
        await _pick_addname_result(qq, state, text)
    else:
        await _search_addname_name(qq, state, text)


async def _search_addname_name(qq: str, state: dict, text: str) -> None:
    """bmaddname：输入原名 → 搜索曲目。"""
    if not SONG_CONSTANTS:
        _addname_pending.pop(qq, None)
        await bm_addname_pick.send("❌ 定数表未加载，已取消")
        return
    names = search_songs(SONG_CONSTANTS, text)
    if not names:
        _addname_pending.pop(qq, None)
        await bm_addname_pick.send(f"❌ 未找到与「{text}」相关的曲目，已取消")
        return
    if len(names) == 1:
        _addname_pending.pop(qq, None)
        await _finish_addname(bm_addname_pick, state["alias"], names[0])
        return
    state["names"] = names
    lines = [f"🔍 找到 {len(names)} 首相关曲目，回复序号确认："]
    lines.extend(f"{i + 1}. {_display_name(n)}" for i, n in enumerate(names))
    await bm_addname_pick.send("\n".join(lines))


async def _pick_addname_result(qq: str, state: dict, text: str) -> None:
    """bmaddname：接收序号选择结果。"""
    if not text.isdigit():
        return
    index = int(text)
    names = state["names"]
    _addname_pending.pop(qq, None)
    if index < 1 or index > len(names):
        await bm_addname_pick.send(
            f"❌ 序号无效（1-{len(names)}），已取消，请重新 /bmaddname"
        )
        return
    await _finish_addname(bm_addname_pick, state["alias"], names[index - 1])


async def _finish_addname(matcher: Matcher, alias: str, name: str) -> None:
    """绑定别名并持久化。"""
    add_alias(alias, name)
    save_aliases(_ALIASES_PATH)
    await matcher.send(
        f"✅ 别名已绑定：{alias} → {_display_name(name)}\n发送 /bmsong {alias} 即可检索"
    )


@bm_whitelist_add.handle()
async def handle_whitelist_add(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """将 QQ 加入别名操作白名单（仅超管）。"""
    if event.user_id != bm_config.bm_super_admin_qq:
        await bm_whitelist_add.finish("❌ 你没有权限使用此命令")
    qq = arg.extract_plain_text().strip()
    if not qq.isdigit():
        await bm_whitelist_add.finish("用法：/bmaddtowhitelist <QQ号>")
    _whitelist.add(int(qq))
    _save_whitelist()
    await bm_whitelist_add.finish(
        f"✅ 已将 {qq} 加入白名单\n当前白名单：{_whitelist_text()}"
    )


@bm_whitelist_remove.handle()
async def handle_whitelist_remove(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """将 QQ 移出别名操作白名单（仅超管）。"""
    if event.user_id != bm_config.bm_super_admin_qq:
        await bm_whitelist_remove.finish("❌ 你没有权限使用此命令")
    qq = arg.extract_plain_text().strip()
    if not qq.isdigit():
        await bm_whitelist_remove.finish("用法：/bmremovefromwhitelist <QQ号>")
    _whitelist.discard(int(qq))
    _save_whitelist()
    await bm_whitelist_remove.finish(
        f"✅ 已将 {qq} 移出白名单\n当前白名单：{_whitelist_text()}"
    )


@bm_remove_name.handle()
async def handle_remove_name(event: MessageEvent, arg: Message = CommandArg()) -> None:
    """删除歌曲别名：/bmremovename <别名> → 输入原名。"""
    if not _can_manage_alias(event.user_id):
        await bm_remove_name.finish("❌ 你没有权限使用此命令")
    qq = str(event.user_id)
    alias = arg.extract_plain_text().strip()
    if not alias:
        await bm_remove_name.finish(
            "用法：/bmremovename <别名>\n例如：/bmremovename 黑猫"
        )
    if not has_alias(alias):
        await bm_remove_name.finish(f"❌ 别名「{alias}」不存在")
    _remove_pending[qq] = {
        "alias": alias,
        "names": [],
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    await bm_remove_name.finish(
        f"📝 已收到别名「{alias}」\n请输入对应的原曲名（支持模糊搜索）："
    )


@bm_remove_pick.handle()
async def handle_remove_pick(event: MessageEvent) -> None:
    """bmremovename 流程：输入原名（搜索）或序号（选择结果）。"""
    qq = str(event.user_id)
    state = _remove_pending.get(qq)
    if state is None:
        return
    if time.monotonic() > state["expire"]:
        _remove_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    if not text:
        return
    if state["names"]:
        await _pick_remove_result(qq, state, text)
    else:
        await _search_remove_name(qq, state, text)


async def _search_remove_name(qq: str, state: dict, text: str) -> None:
    """bmremovename：输入原名 → 搜索曲目。"""
    if not SONG_CONSTANTS:
        _remove_pending.pop(qq, None)
        await bm_remove_pick.send("❌ 定数表未加载，已取消")
        return
    names = search_songs(SONG_CONSTANTS, text)
    if not names:
        _remove_pending.pop(qq, None)
        await bm_remove_pick.send(f"❌ 未找到与「{text}」相关的曲目，已取消")
        return
    if len(names) == 1:
        _remove_pending.pop(qq, None)
        await _finish_remove(bm_remove_pick, state["alias"], names[0])
        return
    state["names"] = names
    lines = [f"🔍 找到 {len(names)} 首相关曲目，回复序号确认："]
    lines.extend(
        f"{i + 1}. {_display_name(n)} {state['alias']}" for i, n in enumerate(names)
    )
    lines.append(f"（{_SONG_PICK_TTL:.0f} 秒内有效）")
    await bm_remove_pick.send("\n".join(lines))


async def _pick_remove_result(qq: str, state: dict, text: str) -> None:
    """bmremovename：接收序号选择结果。"""
    if not text.isdigit():
        return
    index = int(text)
    names = state["names"]
    _remove_pending.pop(qq, None)
    if index < 1 or index > len(names):
        await bm_remove_pick.send(
            f"❌ 序号无效（1-{len(names)}），已取消，请重新 /bmremovename"
        )
        return
    await _finish_remove(bm_remove_pick, state["alias"], names[index - 1])


async def _finish_remove(matcher: Matcher, alias: str, name: str) -> None:
    """删除别名并持久化。"""
    if remove_alias(alias, name):
        save_aliases(_ALIASES_PATH)
        await matcher.send(f"✅ 已移除别名：{_display_name(name)} 的「{alias}」")
    else:
        await matcher.send(f"❌ 未找到「{alias}」对应的别名（不存在或不属于 {name}）")


@bm_name_list.handle()
async def handle_name_list(event: MessageEvent) -> None:
    """输出全部别名与曲目的对应关系（同一首歌的别名放一行）。"""
    if not _can_manage_alias(event.user_id):
        await bm_name_list.finish("❌ 你没有权限使用此命令")
    aliases = get_aliases()
    if not aliases:
        await bm_name_list.finish("📭 当前没有别名")
    by_song: dict[str, list[str]] = {}
    for alias, name in aliases.items():
        by_song.setdefault(name, []).append(alias)
    lines = [f"📋 别名列表（共 {len(aliases)} 条）："]
    for name in sorted(by_song):
        display = _display_name(name)
        lines.append(f"{display} <- {'，'.join(sorted(by_song[name]))}")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_name_list.finish(MessageSegment.image(img_bytes))


def _resolve_n10_entry(
    internal: str,
) -> tuple[str, dict] | None:
    """通过内部名（别名）在定数表中查找 N10 曲目，返回 (显示名, 条目)。"""
    for name, entry in SONG_CONSTANTS.items():
        aliases = entry.get("aliases") or []
        if internal in aliases:
            return name, entry
    return None


@bm_n10.handle()
async def handle_n10() -> None:
    """输出 N10 固定曲池（20 首）及定数。"""
    if not SONG_CONSTANTS:
        await bm_n10.finish("❌ 定数表未加载")
    lines = ["🎵 N10 固定曲池（20 首）", "━━━━━━━━━━━━━━━━━━"]
    found = 0
    for internal in N10_SONG_LIST:
        resolved = _resolve_n10_entry(internal)
        if resolved is None:
            lines.append(f"❓ {internal}（未在定数表中）")
            continue
        display, entry = resolved
        found += 1
        consts: list[str] = []
        for diff in ("RL", "IL", "TT", "RU", "DM", "FL"):
            val = entry.get(diff)
            if val is not None:
                consts.append(f"{diff} {val}")
        lines.append(f"{display}  {'  '.join(consts)}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"共 {found}/{len(N10_SONG_LIST)} 首已收录")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_n10.finish(MessageSegment.image(img_bytes))


# 谱师查询与管理（bmcharter / 基元谱师名义）
# ================================================================


def _charter_entries(charter_name: str) -> list[tuple[str, list[str]]]:
    """按谱师名义收集其（含关联名义组）制作的谱面，按曲目聚合难度。

    返回 ``[(曲名, [难度...])]``，难度按 RL/IL/TT/RU/DM/FL 顺序。
    """
    group_norms = {normalize_n10_name(name) for name in group_of(charter_name)}
    by_song: dict[str, list[str]] = {}
    for song, entry in SONG_CONSTANTS.items():
        for diff, charter in (entry.get("charter") or {}).items():
            if charter and normalize_n10_name(charter) in group_norms:
                by_song.setdefault(song, []).append(diff)
    diff_order = {diff: index for index, diff in enumerate(ALL_DIFFS)}
    charts = sorted(
        by_song.items(), key=lambda item: (normalize_n10_name(item[0]), item[0])
    )
    for _song, diffs in charts:
        diffs.sort(key=lambda diff: diff_order.get(diff, 99))
    return charts


async def _send_charter_list(matcher: Matcher, state: dict) -> None:
    """渲染并发送谱师谱面列表（全部曲目一张图，全局连续序号供选择）。"""
    charter_name = state["charter_name"]
    charts: list[tuple[str, list[str]]] = state["charts"]
    lines = [f"🎵 谱师：{charter_name}"]
    group = group_of(charter_name)
    if len(group) > 1:
        others = "、".join(sorted(name for name in group if name != charter_name))
        lines.append(f"（含关联名义：{others}）")
    lines.extend(
        f"{i + 1}. {_display_name(song)} {'/'.join(diffs)}"
        for i, (song, diffs) in enumerate(charts)
    )
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await matcher.finish(MessageSegment.image(img_bytes))


async def _show_charter_charts(matcher: Matcher, qq: str, charter_name: str) -> None:
    """输出谱师谱面列表（一张图），供序号选择歌曲详情。"""
    charts = _charter_entries(charter_name)
    if not charts:
        await matcher.finish(f"❌ 谱师「{charter_name}」暂无谱面记录")
    _charter_pending[qq] = {
        "action": "chart_pick",
        "charter_name": charter_name,
        "charts": charts,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    await _send_charter_list(matcher, _charter_pending[qq])


@bm_charter.handle()
async def handle_charter(event: MessageEvent, arg: Message = CommandArg()) -> None:
    """按谱师查询谱面，流程与 bmsong 相同（可回复序号选择）。"""
    qq = str(event.user_id)
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_charter.finish("用法：/bmcharter <谱师>\n例如：/bmcharter qqxqqx")
    if not SONG_CONSTANTS:
        await bm_charter.finish("❌ 定数表未加载，无法查询")
    names = search_charters(SONG_CONSTANTS, query)
    if not names:
        await bm_charter.finish(f"❌ 未找到与「{query}」相关的谱师")
    if len(names) == 1:
        await _show_charter_charts(bm_charter, qq, names[0])
        return
    _charter_pending[qq] = {
        "action": "charter_pick",
        "names": names,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"🔍 找到 {len(names)} 位谱师，回复序号查看其谱面："]
    lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
    lines.append(f"（{_SONG_PICK_TTL:.0f} 秒内有效）")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_charter.finish(MessageSegment.image(img_bytes))


@bm_charter_setup.handle()
async def handle_charter_setup(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """设置基元谱师名义（本名），谱师必须能在定数表中查到。"""
    qq = str(event.user_id)
    if not _can_manage_alias(event.user_id):
        await bm_charter_setup.finish("❌ 你没有权限使用此命令")
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_charter_setup.finish("用法：/bmsetuptheprimitivecharter <谱师名义>")
    if not SONG_CONSTANTS:
        await bm_charter_setup.finish("❌ 定数表未加载")
    names = search_charters(SONG_CONSTANTS, query)
    if not names:
        await bm_charter_setup.finish(f"❌ 未在定数表中找到谱师「{query}」")
    if len(names) == 1:
        add_primitive(names[0])
        save_charters(_CHARTERS_PATH)
        await bm_charter_setup.finish(f"✅ 已将「{names[0]}」设为基元谱师名义（本名）")
    _charter_pending[qq] = {
        "action": "setup_pick",
        "names": names,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"🔍 找到 {len(names)} 位谱师，回复序号选择："]
    lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_charter_setup.finish(MessageSegment.image(img_bytes))


async def _ask_related_name(matcher: Matcher, qq: str, primitive: str) -> None:
    """进入等待输入关联谱师名义的状态。"""
    if not is_primitive(primitive):
        await matcher.finish(
            f"❌ 「{primitive}」还不是基元谱师名义\n"
            "请先用 /bmsetuptheprimitivecharter 设置本名"
        )
    _charter_pending[qq] = {
        "action": "related_name",
        "primitive": primitive,
        "names": [],
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    await matcher.finish(
        f"📝 基元谱师名义「{primitive}」\n"
        "请输入要关联的谱师名义（马甲/合作名义，需能在定数表中查到）："
    )


async def _ask_remove_name(matcher: Matcher, qq: str, primitive: str) -> None:
    """进入等待输入解除关联谱师名义的状态。"""
    if not is_primitive(primitive):
        await matcher.finish(
            f"❌ 「{primitive}」还不是基元谱师名义\n"
            "请先用 /bmsetuptheprimitivecharter 设置本名"
        )
    _charter_pending[qq] = {
        "action": "remove_name",
        "primitive": primitive,
        "names": [],
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    await matcher.finish(
        f"📝 基元谱师名义「{primitive}」\n请输入要解除关联的谱师名义："
    )


@bm_charter_related.handle()
async def handle_charter_related(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """添加基元谱师名义的马甲/合作名义，流程与 bmaddname 相同。"""
    qq = str(event.user_id)
    if not _can_manage_alias(event.user_id):
        await bm_charter_related.finish("❌ 你没有权限使用此命令")
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_charter_related.finish("用法：/bmrelatedcharter <基元谱师名义>")
    if not SONG_CONSTANTS:
        await bm_charter_related.finish("❌ 定数表未加载")
    names = search_charters(SONG_CONSTANTS, query)
    if not names:
        await bm_charter_related.finish(f"❌ 未在定数表中找到谱师「{query}」")
    if len(names) > 1:
        _charter_pending[qq] = {
            "action": "related_prim_pick",
            "names": names,
            "expire": time.monotonic() + _SONG_PICK_TTL,
        }
        lines = [f"🔍 找到 {len(names)} 位谱师，回复序号选择基元谱师名义："]
        lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
        img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
        await bm_charter_related.finish(MessageSegment.image(img_bytes))
        return
    await _ask_related_name(bm_charter_related, qq, names[0])


@bm_charter_remove.handle()
async def handle_charter_remove(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """解除基元谱师名义与另一谱师名义的关联，流程与 bmremovename 相同。"""
    qq = str(event.user_id)
    if not _can_manage_alias(event.user_id):
        await bm_charter_remove.finish("❌ 你没有权限使用此命令")
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_charter_remove.finish("用法：/bmremoverelatedcharter <基元谱师名义>")
    if not SONG_CONSTANTS:
        await bm_charter_remove.finish("❌ 定数表未加载")
    names = search_charters(SONG_CONSTANTS, query)
    if not names:
        await bm_charter_remove.finish(f"❌ 未在定数表中找到谱师「{query}」")
    if len(names) > 1:
        _charter_pending[qq] = {
            "action": "remove_prim_pick",
            "names": names,
            "expire": time.monotonic() + _SONG_PICK_TTL,
        }
        lines = [f"🔍 找到 {len(names)} 位谱师，回复序号选择基元谱师名义："]
        lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
        img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
        await bm_charter_remove.finish(MessageSegment.image(img_bytes))
        return
    await _ask_remove_name(bm_charter_remove, qq, names[0])


async def _remove_primitive_and_reply(matcher: Matcher, name: str) -> None:
    """移除基元谱师名义（连同其关联名义）并回复结果。"""
    if not is_primitive(name):
        await matcher.finish(f"❌ 「{name}」还不是基元谱师名义")
    related = get_related(name)
    remove_primitive(name)
    save_charters(_CHARTERS_PATH)
    message = f"✅ 已移除基元谱师名义「{name}」"
    if related:
        message += f"（同时解除 {len(related)} 个关联名义：{'、'.join(related)}）"
    await matcher.finish(message)


@bm_charter_unset.handle()
async def handle_charter_unset(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """移除基元谱师名义（本名），连同其关联名义一并解除。"""
    qq = str(event.user_id)
    if not _can_manage_alias(event.user_id):
        await bm_charter_unset.finish("❌ 你没有权限使用此命令")
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_charter_unset.finish("用法：/bmremovetheprimitivecharter <谱师名义>")
    if not SONG_CONSTANTS:
        await bm_charter_unset.finish("❌ 定数表未加载")
    names = search_charters(SONG_CONSTANTS, query)
    if not names:
        await bm_charter_unset.finish(f"❌ 未在定数表中找到谱师「{query}」")
    if len(names) == 1:
        await _remove_primitive_and_reply(bm_charter_unset, names[0])
        return
    _charter_pending[qq] = {
        "action": "remove_primitive_pick",
        "names": names,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"🔍 找到 {len(names)} 位谱师，回复序号选择："]
    lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_charter_unset.finish(MessageSegment.image(img_bytes))


@bm_charter_list.handle()
async def handle_charter_list(event: MessageEvent) -> None:
    """列出所有基元谱师与其关联名义。"""
    if not _can_manage_alias(event.user_id):
        await bm_charter_list.finish("❌ 你没有权限使用此命令")
    primitives = all_primitives()
    if not primitives:
        await bm_charter_list.finish("📋 当前没有基元谱师")
    lines = [f"📋 谱师关联列表（共 {len(primitives)} 位基元谱师）："]
    for primitive in sorted(primitives):
        related = get_related(primitive)
        lines.append(f"{primitive}：{' / '.join(related) if related else '（无关联）'}")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_charter_list.finish(MessageSegment.image(img_bytes))


async def _apply_related_change(primitive: str, related: str, op: str) -> None:
    """执行关联/解除关联并持久化。"""
    if op == "related":
        if normalize_n10_name(related) == normalize_n10_name(primitive):
            await bm_charter_pick.send("❌ 不能把基元谱师自己关联给自己")
            return
        if is_primitive(related):
            await bm_charter_pick.send(
                f"❌ 「{related}」已是基元谱师名义（本名），不能作为关联名义"
            )
            return
        if not add_related(primitive, related):
            await bm_charter_pick.send(f"❌ 「{related}」与「{primitive}」已存在关联")
            return
        save_charters(_CHARTERS_PATH)
        await bm_charter_pick.send(f"✅ 已关联：{primitive} ← {related}")
    else:
        if not remove_related(primitive, related):
            await bm_charter_pick.send(f"❌ 「{related}」与「{primitive}」没有关联")
            return
        save_charters(_CHARTERS_PATH)
        await bm_charter_pick.send(f"✅ 已解除：{primitive} 与 {related} 的关联")


async def _handle_charter_name_input(qq: str, state: dict, text: str) -> None:
    """等待输入谱师名义的阶段：搜索并执行关联/解除关联。"""
    _charter_pending.pop(qq, None)
    names = search_charters(SONG_CONSTANTS, text)
    primitive = state["primitive"]
    if not names:
        await bm_charter_pick.send(f"❌ 未在定数表中找到谱师「{text}」")
        return
    if len(names) == 1:
        await _apply_related_change(primitive, names[0], state["action"].split("_")[0])
        return
    _charter_pending[qq] = {
        **state,
        "action": f"{state['action']}_pick",
        "names": names,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"🔍 找到 {len(names)} 位谱师，回复序号选择："]
    lines.extend(f"{i + 1}. {name}" for i, name in enumerate(names))
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_charter_pick.send(MessageSegment.image(img_bytes))


async def _handle_charter_number_pick(qq: str, state: dict, index: int) -> None:
    """序号选择阶段：按动作分发到对应流程。"""
    action = state["action"]
    if action == "chart_pick":
        charts = state["charts"]
        if index < 1 or index > len(charts):
            await bm_charter_pick.send(f"❌ 序号无效（1-{len(charts)}）")
            return
        _charter_pending.pop(qq, None)
        song, _diffs = charts[index - 1]
        await _send_song_detail(bm_charter_pick, qq, song)
        return
    names = state.get("names") or []
    if index < 1 or index > len(names):
        await bm_charter_pick.send(f"❌ 序号无效（1-{len(names)}）")
        return
    picked = names[index - 1]
    _charter_pending.pop(qq, None)
    await _apply_charter_pick_action(qq, state, picked)


async def _apply_charter_pick_action(qq: str, state: dict, picked: str) -> None:
    """执行选中的谱师流程动作。"""
    action = state["action"]
    if action == "charter_pick":
        await _show_charter_charts(bm_charter_pick, qq, picked)
    elif action == "setup_pick":
        add_primitive(picked)
        save_charters(_CHARTERS_PATH)
        await bm_charter_pick.send(f"✅ 已将「{picked}」设为基元谱师名义（本名）")
    elif action == "related_prim_pick":
        await _ask_related_name(bm_charter_pick, qq, picked)
    elif action == "remove_prim_pick":
        await _ask_remove_name(bm_charter_pick, qq, picked)
    elif action == "related_pick":
        await _apply_related_change(state["primitive"], picked, "related")
    elif action == "remove_pick":
        await _apply_related_change(state["primitive"], picked, "remove")
    elif action == "remove_primitive_pick":
        await _remove_primitive_and_reply(bm_charter_pick, picked)


@bm_charter_pick.handle()
async def handle_charter_pick(event: MessageEvent) -> None:
    """谱师流程的序号/名义输入选择（含基元谱师管理的多步流程）。"""
    qq = str(event.user_id)
    state = _charter_pending.get(qq)
    if state is None:
        return
    if time.monotonic() > state["expire"]:
        _charter_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    action = state["action"]
    # 等待输入「谱师名义」的阶段：收到的是名字而非序号
    if action in ("related_name", "remove_name"):
        if text:
            await _handle_charter_name_input(qq, state, text)
        return
    if text.isdigit():
        await _handle_charter_number_pick(qq, state, int(text))


# ================================================================
# 定数区间查询（bmchartlist / bmrandom）
# ================================================================

# 难度 token -> 难度（大小写不敏感）
_DIFF_TOKENS = {diff.lower(): diff for diff in ALL_DIFFS}
_MAX_CONST_ARGS = 2
# 谱面数超过该值时渲染耗时较长（全量表可达数十秒），先发「生成中」提示
_CHART_SLOW_THRESHOLD = 300
# /bmchartlist all 的全量表缓存图（scripts/build-all-charts.py 生成，
# restart-bot.sh 部署时自动重建；缺失时命令会现场渲染并落盘）
_ALL_CHARTS_CACHE = DATA_DIR / "all_charts.jpg"


def _parse_const_token(token: str) -> tuple[float, float]:
    """解析定数 token → (下限, 上限)（闭区间）。

    - 整数 ``13`` → ``[13.0, 13.5]``（该档不含 ``+`` 的下半档）
    - ``13+`` → ``[13.6, 13.9]``（``+`` 到下一个整数档，不含下一个整数）
    - 小数 ``13.4`` → ``[13.4, 13.4]``（精确定数）
    """
    text = token.strip()
    if text.endswith("+"):
        base = text[:-1]
        if not base or "." in base:
            raise ValueError(  # noqa: TRY003
                f"「{token}」不合法：+ 仅用于整数档位（如 13+）"
            )
        try:
            value = float(base)
        except ValueError as exc:
            raise ValueError(f"无法识别的定数「{token}」") from exc
        return value + 0.6, value + 0.9
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"无法识别的定数「{token}」") from exc
    if "." in text:
        return value, value
    return value, value + 0.5


def _score_from_archive(
    data: dict,
    song: str,
    diff: str,
    entry: dict,
) -> int:
    """从存档中取指定谱面的分数（0 = 未找到）。

    存档键用游戏内部名，与表内曲名（显示名）可能不同，
    依次尝试 别名 → 曲名 → 原曲名 的归一化形式。
    """
    candidates = [str(a).strip() for a in entry.get("aliases") or []]
    candidates.append(song)
    original = str(entry.get("originalName") or "").strip()
    if original and original not in candidates:
        candidates.append(original)
    wanted = {normalize_n10_name(c) for c in candidates if c}
    prefix = "BestScore_"
    for key, value in data.items():
        if not key.startswith(prefix):
            continue
        parts = key.split("_")
        if len(parts) < 3 or parts[-1] != diff:  # noqa: PLR2004
            continue
        name = normalize_n10_name("_".join(parts[1:-1]))
        if name in wanted:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                pass
    return 0


def _parse_chart_args(
    args: list[str],
) -> tuple[float | None, float | None, list[str], bool]:
    """解析 bmchart/bmrandom 参数 → (定数下限, 定数上限, 难度列表, 是否显示成绩)。

    多个定数参数的区间取并集（``11 12`` → ``[11.0, 12.5]``，顺序无关）。
    ``all`` 表示不限定数区间（可与难度连用，如 ``all TT``）。
    末尾输入 ``score`` 启用成绩渲染模式（需先绑定存档）。
    """
    ranges: list[tuple[float, float]] = []
    diffs: list[str] = []
    has_all = False
    show_score = False
    for token in args:
        lower = token.strip().lower()
        if lower == "all":
            has_all = True
            continue
        if lower == "score":
            show_score = True
            continue
        if lower in _DIFF_TOKENS:
            if _DIFF_TOKENS[lower] not in diffs:
                diffs.append(_DIFF_TOKENS[lower])
            continue
        ranges.append(_parse_const_token(token))
    if has_all and ranges:
        raise ValueError(  # noqa: TRY003
            "all 表示全部定数，不能与定数区间同时使用"
        )
    if len(ranges) > _MAX_CONST_ARGS:
        raise ValueError("定数参数最多两个（下限和上限）")
    if has_all or not ranges:
        return None, None, diffs, show_score
    lower = min(r[0] for r in ranges)
    upper = max(r[1] for r in ranges)
    return lower, upper, diffs, show_score


def _collect_charts(
    constants: dict[str, dict],
    lower: float | None,
    upper: float | None,
    diffs: list[str],
) -> list[tuple[float, str, str]]:
    """收集 ``[lower, upper]`` 定数区间的指定难度曲目，按定数降序。"""
    targets = diffs or list(ALL_DIFFS)
    charts: list[tuple[float, str, str]] = []
    for song, entry in constants.items():
        for diff in targets:
            constant = entry.get(diff)
            if constant is None or float(constant) <= 0:
                continue
            value = float(constant)
            if lower is not None and value < lower:
                continue
            if upper is not None and value > upper:
                continue
            charts.append((value, song, diff))
    charts.sort(key=lambda item: (-item[0], item[1], item[2]))
    return charts


async def _handle_chart_score(
    event: MessageEvent,
    charts: list[tuple[float, str, str]],
) -> None:
    """bmchartlist score 模式：查找绑定存档中的真实成绩并用 CARD2 渲染。"""
    qq = str(event.user_id)
    binding = _bindings.get(qq)
    if binding is None:
        await bm_chart.finish(
            "❌ 尚未绑定存档，请先 /bmbind 绑定后再使用 score 模式"
        )
    data = binding["data"]
    rated: list[Chart] = []
    for constant, song, diff in charts:
        entry = SONG_CONSTANTS.get(song) or {}
        score = _score_from_archive(data, song, diff, entry)
        if score <= 0:
            continue
        potential = calculate_chart_potential(score, constant)
        rated.append(
            Chart(
                name=song,
                diff=diff,
                constant=constant,
                score=score,
                potential=potential,
                original_name=str(entry.get("originalName") or song),
            )
        )
    if not rated:
        await bm_chart.finish("❌ 该范围内没有你打过的谱面")
    rated.sort(key=lambda c: c.potential, reverse=True)
    img_bytes = await asyncio.to_thread(render_score_grid, rated)
    await bm_chart.finish(
        MessageSegment.image(
            await asyncio.to_thread(shrink_for_send, img_bytes)
        )
    )


@bm_chart.handle()
async def handle_chart(event: MessageEvent, arg: Message = CommandArg()) -> None:
    """按定数区间/难度生成定数表图（/bmchartlist）。"""
    args = arg.extract_plain_text().split()
    if not args:
        await bm_chart.finish(
            "用法：/bmchartlist <定数1> [定数2] [难度...]\n"
            "例如：/bmchartlist 11 12 TT\n/bmchartlist 13+ RU\n"
            "定数：13 表示 13.0~13.5，13+ 表示 13.6~13.9，13.4 表示精确 13.4\n"
            "/bmchartlist all 输出全部谱面定数（可加难度，如 all TT）\n"
            "/bmchartlist TT（全部定数）\n不写难度表示全部难度\n"
            "末尾加 score 启用成绩渲染模式，如 /bmchartlist 11 12 TT score"
        )
    if not SONG_CONSTANTS:
        await bm_chart.finish("❌ 定数表未加载")
    try:
        lower, upper, diffs, show_score = _parse_chart_args(args)
    except ValueError as exc:
        await bm_chart.finish(f"❌ {exc}")
    charts = _collect_charts(SONG_CONSTANTS, lower, upper, diffs)
    if not charts:
        await bm_chart.finish("❌ 该范围内没有符合条件的曲目")
    # 成绩渲染模式：用 bmrating 同款 CARD2 卡片显示绑定存档中的真实成绩
    if show_score:
        await _handle_chart_score(event, charts)
    if lower is None and upper is None and not diffs:
        # 全量定数表（/bmchartlist all）：渲染慢，优先发磁盘缓存图
        if _ALL_CHARTS_CACHE.exists():
            await bm_chart.finish(
                MessageSegment.image(
                    await asyncio.to_thread(_ALL_CHARTS_CACHE.read_bytes)
                )
            )
        await bm_chart.send("⏳ 首次生成全量定数表，约需一分钟，请稍候…")
        img_bytes = await asyncio.to_thread(render_chart_table, charts)
        img_bytes = await asyncio.to_thread(shrink_for_send, img_bytes)
        await asyncio.to_thread(_ALL_CHARTS_CACHE.write_bytes, img_bytes)
        await bm_chart.finish(MessageSegment.image(img_bytes))
    if len(charts) >= _CHART_SLOW_THRESHOLD:
        # 全量定数表渲染耗时较长（可达数十秒），先发提示
        await bm_chart.send("⏳ 生成中，请稍候…")
    img_bytes = await asyncio.to_thread(render_chart_table, charts)
    await bm_chart.finish(
        MessageSegment.image(await asyncio.to_thread(shrink_for_send, img_bytes))
    )


@bm_chart_preview.handle()
async def handle_chart_preview(
    event: MessageEvent, arg: Message = CommandArg()
) -> None:
    """生成谱面预览图：先选曲（同 bmsong 检索），再选难度。"""
    qq = str(event.user_id)
    query = arg.extract_plain_text().strip()
    if not query:
        await bm_chart_preview.finish(
            "用法：/bmchart <曲名>\n例如：/bmchart ether vortex\n"
            "先选择曲目，再选择难度（支持别名/模糊搜索）"
        )
    if not SONG_CONSTANTS:
        await bm_chart_preview.finish("❌ 定数表未加载，无法查询")
    names = search_songs(SONG_CONSTANTS, query)
    if not names:
        # 定数表外的谱面（如隐藏谱 MIRЯOЯ）按文件名直接找
        direct = await asyncio.to_thread(find_chart, query)
        if direct is None:
            await bm_chart_preview.finish(f"❌ 未找到与「{query}」相关的曲目")
        song = direct[0].name.rsplit(" ", 1)[0]
        await _ask_chart_difficulty(bm_chart_preview, qq, song)
        return
    if len(names) == 1:
        await _ask_chart_difficulty(bm_chart_preview, qq, names[0])
        return
    _chart_pending[qq] = {
        "stage": "song",
        "names": names,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"🔍 找到 {len(names)} 首相关曲目，回复序号查看详情："]
    lines.extend(f"{i + 1}. {_display_name(name)}" for i, name in enumerate(names))
    lines.append(f"（{_SONG_PICK_TTL:.0f} 秒内有效）")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await bm_chart_preview.finish(MessageSegment.image(img_bytes))


async def _ask_chart_difficulty(matcher: Matcher, qq: str, song: str) -> None:
    """进入 bmchart 选难度阶段（仅一个难度时直接出图）。"""
    diffs = await asyncio.to_thread(available_diffs, song)
    if not diffs:
        await matcher.finish("❌ 该曲目没有谱面文件")
    if len(diffs) == 1:
        await _render_chart_image(matcher, qq, song, diffs[0])
        return
    _chart_pending[qq] = {
        "stage": "diff",
        "song": song,
        "diffs": diffs,
        "expire": time.monotonic() + _SONG_PICK_TTL,
    }
    lines = [f"📌 请选择 {_display_name(song)} 的难度，回复序号："]
    lines.extend(f"{i + 1}. {diff}" for i, diff in enumerate(diffs))
    lines.append(f"（{_SONG_PICK_TTL:.0f} 秒内有效）")
    img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
    await matcher.finish(MessageSegment.image(img_bytes))


async def _render_chart_image(
    matcher: Matcher, qq: str, song: str, diff: str
) -> None:
    """按曲名+难度渲染谱面预览图并发送（谱面指定皮肤优先，否则用玩家皮肤）。"""
    found = await asyncio.to_thread(find_chart, song, diff)
    if found is None:
        await matcher.finish("❌ 该曲目没有对应难度的谱面文件")
    path, found_diff = found
    try:
        chart = await asyncio.to_thread(parse_chart, path)
    except (ValueError, OSError):
        await matcher.finish("❌ 该谱面解析失败，请换一首试试")
    if not chart.notes:
        await matcher.finish("❌ 谱面中没有音符数据")
    if chart.note_skin == "Berry" or chart.note_skin not in SKIN_SETS:
        # Berry（默认皮肤）或未知皮肤：用玩家选择的显示皮肤
        skin = _chart_skins.get(qq, DEFAULT_SKIN)
    else:
        # 联动/特色谱面保留谱面指定皮肤
        skin = chart.note_skin
    img_bytes = await asyncio.to_thread(
        render_chart_preview,
        chart,
        f"{_display_name(song)} ({found_diff})",
        skin,
    )
    await matcher.finish(MessageSegment.image(img_bytes))


@bm_skin.handle()
async def handle_skin(event: MessageEvent, arg: Message = CommandArg()) -> None:
    """切换 /bmchart 音符皮肤：/bmskin 查看全部，回复序号或直接发皮肤名切换。"""
    qq = str(event.user_id)
    text = arg.extract_plain_text().strip()
    names = list(SKIN_SETS)
    current = _chart_skins.get(qq, DEFAULT_SKIN)
    if not text:
        _skin_pending[qq] = time.monotonic() + _SONG_PICK_TTL
        lines = [f"🎨 音符皮肤（当前：{current}）"]
        lines.extend(
            f"{i + 1}. {name}{' ←' if name == current else ''}"
            for i, name in enumerate(names)
        )
        lines.append(f"回复序号或直接发送皮肤名切换（{_SONG_PICK_TTL:.0f} 秒内有效）")
        img_bytes = await asyncio.to_thread(render_list_image, "\n".join(lines))
        await bm_skin.finish(MessageSegment.image(img_bytes))
    _skin_pending.pop(qq, None)
    name = _resolve_skin_choice(text, names)
    if name is None:
        await bm_skin.finish(f"❌ 未找到皮肤「{text}」，可用：{'/'.join(names)}")
    _chart_skins[qq] = name
    await bm_skin.finish(f"✅ 已切换音符皮肤为 {name}")


def _resolve_skin_choice(text: str, names: list[str]) -> str | None:
    """解析皮肤选择：数字序号或皮肤名（大小写不敏感）。"""
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(names):
            return names[index - 1]
        return None
    for name in names:
        if name.lower() == text.lower():
            return name
    return None


@bm_skin_pick.handle()
async def handle_skin_pick(event: MessageEvent) -> None:
    """bmskin 列表后的序号/皮肤名选择（回复纯文本）。"""
    qq = str(event.user_id)
    expire = _skin_pending.get(qq)
    if expire is None:
        return
    if time.monotonic() > expire:
        _skin_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    name = _resolve_skin_choice(text, list(SKIN_SETS)) if text else None
    if name is None:
        return  # 无关消息不打扰
    _skin_pending.pop(qq, None)
    _chart_skins[qq] = name
    await bm_skin_pick.send(f"✅ 已切换音符皮肤为 {name}")


@bm_chart_pick.handle()
async def handle_chart_pick(event: MessageEvent) -> None:
    """bmchart 的曲目/难度选择：接收纯数字序号。"""
    qq = str(event.user_id)
    state = _chart_pending.get(qq)
    if state is None:
        return
    if time.monotonic() > state["expire"]:
        _chart_pending.pop(qq, None)
        return
    text = event.get_plaintext().strip()
    if not text.isdigit():
        return
    index = int(text)
    _chart_pending.pop(qq, None)
    if state["stage"] == "song":
        names = state["names"]
        if index < 1 or index > len(names):
            await bm_chart_pick.send(
                f"❌ 序号无效（1-{len(names)}），请重新 /bmchart 查询"
            )
            return
        await _ask_chart_difficulty(bm_chart_pick, qq, names[index - 1])
        return
    diffs = state["diffs"]
    if index < 1 or index > len(diffs):
        await bm_chart_pick.send(
            f"❌ 序号无效（1-{len(diffs)}），请重新 /bmchart 查询"
        )
        return
    await _render_chart_image(bm_chart_pick, qq, state["song"], diffs[index - 1])


@bm_random.handle()
async def handle_random(arg: Message = CommandArg()) -> None:
    """按定数区间/难度随机挑一首曲目（文字返回）。"""
    args = arg.extract_plain_text().split()
    if not args:
        await bm_random.finish(
            "用法：/bmrandom <定数1> [定数2] [难度...]（参数同 /bmchartlist）"
        )
    if not SONG_CONSTANTS:
        await bm_random.finish("❌ 定数表未加载")
    try:
        lower, upper, diffs, _score = _parse_chart_args(args)
    except ValueError as exc:
        await bm_random.finish(f"❌ {exc}")
    charts = _collect_charts(SONG_CONSTANTS, lower, upper, diffs)
    if not charts:
        await bm_random.finish("❌ 该范围内没有符合条件的曲目")
    constant, song, diff = random.choice(charts)
    text = f"🎲 {_display_name(song)}（{diff} {constant:.1f}）"
    cover = find_cover(song, SONG_CONSTANTS[song])
    if cover is not None:
        # 以字节发送（base64），远程部署时协议端无法访问服务器本地路径
        cover_bytes = await asyncio.to_thread(cover.read_bytes)
        await bm_random.finish(
            MessageSegment.text(text) + MessageSegment.image(cover_bytes)
        )
    await bm_random.finish(text)


@bm_rating_style.handle()
async def handle_rating_style(event: MessageEvent) -> None:
    """切换查分样式（新版/旧版），按用户独立保存。"""
    qq = str(event.user_id)
    style = _toggle_rating_style(qq)
    name = "新版" if style == "new" else "旧版"
    await bm_rating_style.finish(
        f"✅ 已切换为{name}查分样式（下次发送 /bmrating 生效）"
    )
