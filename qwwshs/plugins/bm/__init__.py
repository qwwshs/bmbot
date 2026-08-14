"""Berry Melody 查分插件。

命令：
- ``/bmhelp``：查看帮助
- ``/bmbind``：绑定 Berry Melody 存档。发送该命令后，监测该 QQ 发送的下一个
  ``.txt`` 文件（聊天文件或群文件上传均可），读取文件内容作为账号数据
  （从 ``<RSAKeyValue>`` 开始的完整存档，或已解密的 JSON），5 分钟超时
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
from .constants import DATA_DIR, ConstantsError, get_song_constants
from .decrypt import DecryptError, parse_account_data
from .rating import ALL_DIFFS, compute_rating, normalize_n10_name, parse_scores
from .render import (
    render_card,
    render_card_new,
    render_chart_table,
    render_help_image,
    render_list_image,
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
    usage="/bmhelp\n/bmbind\n/bmrating\n/bmsong <曲名>",
)


class BmConfig(BaseModel):
    """插件配置（.env 中 ``BM_ADMIN_QQS`` / ``BM_SUPER_ADMIN_QQ`` 可覆盖）。"""

    # 允许使用 /bmaddname 的 QQ 白名单（默认空，由 .env 配置）
    bm_admin_qqs: set[int] = set()
    # 超管 QQ：唯一可管理白名单的人（未配置则无人可管理）
    bm_super_admin_qq: int | None = None


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
BM_VERSION = "0.5.1"

# QQ 号 -> {data: 解密后的账号 JSON, name: 玩家名, bind_time: 时间戳}
_bindings: dict[str, dict] = {}
# QQ 号 -> 等待存档文件绑定的开始时间（monotonic）
_awaiting_file: dict[str, float] = {}
# QQ 号 -> {names: 模糊搜索结果, expire: 过期时间}（bmsong 序号选择）
_song_pending: dict[str, dict] = {}
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


def _store_binding(qq: str, data: dict) -> str:
    _bindings[qq] = {
        "data": data,
        "name": str(data.get("AccountName") or ""),
        "bind_time": time.time(),
    }
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
    """读取文件内容并绑定账号，返回提示信息。"""
    try:
        raw = await _read_file(bot, data)
        content = _decode_text(raw)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return f"❌ 读取文件失败：{exc}"
    try:
        account = parse_account_data(content)
    except DecryptError as exc:
        return f"❌ 绑定失败：{exc}"
    return _store_binding(qq, account)


bm_help = on_command("bmhelp", priority=5, block=True)
bm_bind = on_command("bmbind", priority=5, block=True)
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
bm_random = on_command("bmrandom", priority=5, block=True)
bm_rating_style = on_command("bmratingstyle", priority=5, block=True)
bm_file_watch = on_message(priority=10, block=False)
bm_song_pick = on_message(priority=10, block=False)
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
    "/bmbind — 绑定存档\n"
    "   导出步骤：进入 Berry Melody，切换角色为克莱因，导出存档，\n"
    "   随后将导出的存档放入 txt 文件再发送（聊天文件或群文件均可）\n"
    "   · 如果系统为 iOS：复制存档到 Pages，新建空白文档，粘贴，\n"
    "     点击右上角的分享，选择纯文本，导出并发送\n"
    "   · 内容为从 <RSAKeyValue> 开始的完整存档（FormalSave.txt）\n"
    "   · 或已解密的 JSON 文本\n"
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
    "/bmchartlist <定数1> [定数2] [难度...] — 按定数区间生成定数表图\n"
    "   13 表示 13.0~13.5，13+ 表示 13.6~13.9，13.4 表示精确 13.4\n"
    "/bmrandom <定数1> [定数2] [难度...] — 在定数区间内随机挑一首曲目\n"
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
        "📄 请导出存档并发送 txt 文件（5 分钟内有效）\n"
        "   步骤：进入 Berry Melody → 切换角色为克莱因 → 导出存档\n"
        "   → 将导出的存档放入 txt 文件中再发送（聊天文件或群文件均可）\n"
        "   如果系统为 iOS：复制存档到 Pages，新建空白文档，粘贴，\n"
        "   点击右上角的分享，选择纯文本，导出并发送\n"
        "（内容为从 <RSAKeyValue> 开始的完整存档，或已解密的 JSON 文本）"
    )


@bm_version.handle()
async def handle_version() -> None:
    await bm_version.finish(f"🤖 Berry Melody 查分 Bot v{BM_VERSION}")


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


@bm_rating.handle()
async def handle_rating(event: MessageEvent) -> None:
    qq = str(event.user_id)
    binding = _bindings.get(qq)
    if binding is None:
        await bm_rating.finish(
            "❌ 你还没有绑定存档\n请先发送 /bmbind 并发送存档 txt 文件"
        )
    if not SONG_CONSTANTS:
        await bm_rating.finish("❌ 定数表未加载，无法查分")
    data = binding["data"]
    charts, grade_counts, missing = parse_scores(data, SONG_CONSTANTS)
    if not charts:
        await bm_rating.finish("❌ 存档中没有可计算的成绩（可能绑定了错误的存档）")
    result = compute_rating(charts, data.get("Potential"))
    result.missing = missing
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
        )
    message: Message = MessageSegment.image(img_bytes)
    if missing:
        message += f"\n⚠️ 有 {len(missing)} 首成绩未在定数表中，不计入"
    await bm_rating.finish(message)


async def _send_song_detail(matcher: Matcher, qq: str, name: str) -> None:
    """发送单曲详情：曲名/曲师/成绩文本 + 曲绘图片。"""
    entry = SONG_CONSTANTS[name]
    binding = _bindings.get(qq)
    if binding is None:
        scores: list[tuple[str, int, str]] = []
        hint = "\n💡 发送 /bmbind 绑定存档后可查看成绩"
    else:
        scores = get_song_scores(binding["data"], name)
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


def _parse_chart_args(
    args: list[str],
) -> tuple[float | None, float | None, list[str]]:
    """解析 bmchart/bmrandom 参数 → (定数下限, 定数上限, 难度列表)。

    多个定数参数的区间取并集（``11 12`` → ``[11.0, 12.5]``，顺序无关）。
    """
    ranges: list[tuple[float, float]] = []
    diffs: list[str] = []
    for token in args:
        lower = token.strip().lower()
        if lower in _DIFF_TOKENS:
            if _DIFF_TOKENS[lower] not in diffs:
                diffs.append(_DIFF_TOKENS[lower])
            continue
        ranges.append(_parse_const_token(token))
    if len(ranges) > _MAX_CONST_ARGS:
        raise ValueError("定数参数最多两个（下限和上限）")
    if not ranges:
        return None, None, diffs
    lower = min(r[0] for r in ranges)
    upper = max(r[1] for r in ranges)
    return lower, upper, diffs


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


@bm_chart.handle()
async def handle_chart(arg: Message = CommandArg()) -> None:
    """按定数区间/难度生成定数表图（/bmchartlist）。"""
    args = arg.extract_plain_text().split()
    if not args:
        await bm_chart.finish(
            "用法：/bmchartlist <定数1> [定数2] [难度...]\n"
            "例如：/bmchartlist 11 12 TT\n/bmchartlist 13+ RU\n"
            "定数：13 表示 13.0~13.5，13+ 表示 13.6~13.9，13.4 表示精确 13.4\n"
            "/bmchartlist TT（全部定数）\n不写难度表示全部难度"
        )
    if not SONG_CONSTANTS:
        await bm_chart.finish("❌ 定数表未加载")
    try:
        lower, upper, diffs = _parse_chart_args(args)
    except ValueError as exc:
        await bm_chart.finish(f"❌ {exc}")
    charts = _collect_charts(SONG_CONSTANTS, lower, upper, diffs)
    if not charts:
        await bm_chart.finish("❌ 该范围内没有符合条件的曲目")
    img_bytes = await asyncio.to_thread(render_chart_table, charts)
    await bm_chart.finish(MessageSegment.image(img_bytes))


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
        lower, upper, diffs = _parse_chart_args(args)
    except ValueError as exc:
        await bm_random.finish(f"❌ {exc}")
    charts = _collect_charts(SONG_CONSTANTS, lower, upper, diffs)
    if not charts:
        await bm_random.finish("❌ 该范围内没有符合条件的曲目")
    constant, song, diff = random.choice(charts)
    await bm_random.finish(f"🎲 {_display_name(song)}（{diff} {constant:.1f}）")


@bm_rating_style.handle()
async def handle_rating_style(event: MessageEvent) -> None:
    """切换查分样式（新版/旧版），按用户独立保存。"""
    qq = str(event.user_id)
    style = _toggle_rating_style(qq)
    name = "新版" if style == "new" else "旧版"
    await bm_rating_style.finish(
        f"✅ 已切换为{name}查分样式（下次发送 /bmrating 生效）"
    )
