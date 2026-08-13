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

from .constants import DATA_DIR, ConstantsError, get_song_constants
from .decrypt import DecryptError, parse_account_data
from .rating import compute_rating, parse_scores
from .render import render_card
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
# 旧位置（插件包内 data/），用于自动迁移
_OLD_BINDINGS_PATH = Path(__file__).resolve().parent / "data" / "bindings.json"
_FILE_WAIT_TTL = 300.0
_SONG_PICK_TTL = 120.0
_ALIAS_MAX_LEN = 30

# 插件版本：修复/小改动 +0.0.1，新增功能 +0.1
BM_VERSION = "0.1.0"

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

load_aliases(_ALIASES_PATH)


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
    """按 UTF-8 → GBK → 宽松替换的顺序解码文件内容。"""
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
bm_file_watch = on_message(priority=10, block=False)
bm_song_pick = on_message(priority=10, block=False)
bm_addname_pick = on_message(priority=10, block=False)
bm_remove_pick = on_message(priority=10, block=False)
bm_group_upload = on_notice(priority=5, block=False)

_load_bindings()


@bm_help.handle()
async def handle_help() -> None:
    await bm_help.finish(
        "🎵 Berry Melody 查分 Bot\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/bmhelp — 查看本帮助\n"
        "/bmbind — 绑定存档\n"
        "   发送后请在 5 分钟内发送存档 txt 文件（聊天文件或群文件均可）\n"
        "   · 内容为从 <RSAKeyValue> 开始的完整存档（FormalSave.txt）\n"
        "   · 或已解密的 JSON 文本\n"
        "/bmrating — 以图片输出你的 Rating 查分\n"
        "/bmsong <曲名> — 单曲查询（支持模糊搜索）\n"
        "/bmaddname <别名> — 为歌曲添加自定义别名（白名单）\n"
        "/bmremovename <别名> — 删除歌曲别名（白名单）\n"
        "/bmnamelist — 查看全部别名对应关系（白名单）\n"
        "/bmaddtowhitelist <QQ> — 添加白名单（超管）\n"
        "/bmremovefromwhitelist <QQ> — 移除白名单（超管）\n"
        "/bmbotversion — 查看 bot 版本\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📱 存档位置：/Android/data/com.skywaystudio.BerryMelody/files/FormalSave.txt"
    )


@bm_bind.handle()
async def handle_bind(event: MessageEvent) -> None:
    qq = str(event.user_id)
    _awaiting_file[qq] = time.monotonic()
    await bm_bind.finish(
        "📄 请在 5 分钟内发送你的存档 txt 文件\n"
        "（从 <RSAKeyValue> 开始的完整存档，或已解密的 JSON 文本）"
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
    img_bytes = await asyncio.to_thread(
        render_card, player_name, result, grade_counts, data.get("Potential")
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
    await bm_song.finish("\n".join(lines))


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
    """输出全部别名与曲目的对应关系。"""
    if not _can_manage_alias(event.user_id):
        await bm_name_list.finish("❌ 你没有权限使用此命令")
    aliases = get_aliases()
    if not aliases:
        await bm_name_list.finish("📭 当前没有别名")
    lines = [f"📋 别名列表（共 {len(aliases)} 条）："]
    lines.extend(
        f"{alias} → {_display_name(name)}" for alias, name in sorted(aliases.items())
    )
    await bm_name_list.finish("\n".join(lines))
