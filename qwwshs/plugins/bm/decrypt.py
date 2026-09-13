"""Berry Melody 存档解密与协议展开。

复刻 ``bm-score(4).html`` 中 ``performDecryption`` 的流程：

1. 提取 ``<RSAKeyValue>`` 私钥 XML
2. 密文按 RSA 密钥块大小分块，先尝试 RSA-OAEP(SHA1)，失败回退 RSAES-PKCS1-V1_5
3. 拼接明文，UTF-8 解码并去除控制字符
4. 按 JSON 解析（带 ``{``/``}`` 修正兜底）
5. 展开 ``SaveProtocol_<名>`` 压缩成绩块（见 :func:`expand_protocol`）
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class DecryptError(Exception):
    """存档解密失败。"""


# 密文最短长度（RSA 块 base64 后至少百字符级别，过短说明粘贴不完整）
_MIN_CIPHER_LEN = 100
# UTF-16 明文检测的最短长度（太短无法可靠判断）
_UTF16_MIN_LEN = 40
# UTF-16LE 判定阈值：奇数位（高字节）为 NUL 的比例下限
_UTF16_ODD_NUL_RATIO = 0.5

# 协议压缩值前缀，与游戏 ProtocolUtil.GzipPrefix 一致
_GZIP_PREFIX = "GZIP1:"
# 成绩/解锁键被压成单条时的键名前缀，如 SaveProtocol_26_8_30
_PROTOCOL_PREFIX = "SaveProtocol_"
# 协议名合法字符（存档内容不可信，避免 ../ 之类的路径穿越）
_PROTOCOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# 协议模板目录：与游戏 Resources/Text/SaveProtocol 同名文件，升级只换文件不改代码
_PROTOCOL_DIR = Path(__file__).resolve().parent / "SaveProtocol"
# GZip 魔数（含 compression method = 8）
_GZIP_MAGIC = b"\x1f\x8b\x08"
# GZip 最短字节数（10 字节头 + 8 字节尾）
_GZIP_MIN_LEN = 18


def parse_account_data(text: str) -> dict:
    """自动识别并解析账号数据。

    - 包含 ``<RSAKeyValue>`` 标签 → 按原始存档 RSA 解密
    - 以 ``{`` 开头 → 直接按 JSON 解析

    两种来源都会展开协议压缩块（见 :func:`expand_protocol`），
    因此克莱因导出（压缩）与本地保存（明文）的存档解析结果一致。
    """
    # 入口兜底：剔除 NUL 等控制字符（UTF-16 无 BOM 存档被误按 UTF-8
    # 解码后，NUL 会穿插在 <RSAKeyValue> 标签中间，必须先剔除才能识别）
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text.strip())
    if "<RSAKeyValue>" in text and "</RSAKeyValue>" in text:
        return expand_protocol(decrypt_save(text))
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecryptError(f"JSON 解析失败: {exc}") from exc  # noqa: TRY003
        if not isinstance(data, dict):
            raise DecryptError("JSON 不是有效的账号数据")  # noqa: TRY003
        return expand_protocol(data)
    raise DecryptError(  # noqa: TRY003
        "无法识别账号数据：请粘贴从 <RSAKeyValue> 开始的完整存档，或解密后的 JSON"
    )


def _decode_plaintext(plaintext: bytes) -> str:
    """解密明文解码：检测 UTF-16（BOM 或奇数位大量 NUL），否则按 UTF-8。

    游戏明文为 UTF-16LE 编码的 JSON（中文等非 ASCII 字符若按 UTF-8 解码
    会错乱成 ``\\`` 等字符破坏 JSON 结构，如 ``"风尘"`` → ``"Θ\\"``）。
    """
    if plaintext.startswith((b"\xff\xfe", b"\xfe\xff")):
        return plaintext.decode("utf-16")
    if len(plaintext) > _UTF16_MIN_LEN:
        odd_bytes = plaintext[1::2]
        odd_nul_ratio = sum(b == 0 for b in odd_bytes) / len(odd_bytes)
        if odd_bytes and odd_nul_ratio > _UTF16_ODD_NUL_RATIO:
            return plaintext.decode("utf-16-le")
    return plaintext.decode("utf-8", errors="replace")


def decrypt_save(text: str) -> dict:
    """解密完整存档文本并返回账号 JSON（协议压缩块交给 :func:`expand_protocol`）。"""
    key_xml, cipher = _split_key_and_cipher(text)
    private_key = _build_private_key(key_xml)
    block_size = private_key.key_size // 8
    # 完整性检查：base64 数据字符数非法（%4==1）说明复制/传输时被截断
    data_chars = cipher.rstrip("=")
    if len(data_chars) % 4 == 1:
        raise DecryptError("存档文件不完整（密文被截断），请重新导出完整的存档文件")
    raw = base64.b64decode(cipher)
    if len(raw) % block_size != 0:
        raise DecryptError("存档文件不完整（密文长度异常），请重新导出完整的存档文件")
    plaintext = bytearray()
    for offset in range(0, len(raw), block_size):
        chunk = bytes(raw[offset : offset + block_size])
        plaintext.extend(_decrypt_block(private_key, chunk))
    decoded = _decode_plaintext(bytes(plaintext))
    decoded = re.sub(r"[\x00-\x1F\x7F-\x9F]", "", decoded)
    return _parse_json_lenient(decoded)


def _split_key_and_cipher(text: str) -> tuple[str, str]:
    """从存档文本中分离私钥 XML 与密文（与 HTML 的拆分逻辑一致）。"""
    # 兜底：剔除 NUL 等控制字符（UTF-16 无 BOM 存档按 UTF-8 解码后的产物）
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    start = text.find("<RSAKeyValue>")
    end = text.find("</RSAKeyValue>")
    if start == -1 or end == -1 or end <= start:
        raise DecryptError("未检测到完整的 <RSAKeyValue> 标签")  # noqa: TRY003
    key_xml = text[start : end + len("</RSAKeyValue>")]
    cipher = text[end + len("</RSAKeyValue>") :].strip()
    if cipher.startswith("<SecKey>"):
        cipher = cipher[len("<SecKey>") :].strip()
    cipher = re.sub(r"\s", "", cipher)
    # 先还原 URL 安全字符，再做前导 base64 段截取，
    # 避免含 -/_ 的密文在正则处被提前截断
    cipher = cipher.replace("-", "+").replace("_", "/")
    match = re.match(r"^[A-Za-z0-9+/=]+", cipher)
    if match:
        cipher = match.group(0)
    cipher = re.sub(r"[^A-Za-z0-9+/=]", "", cipher)
    cipher += "=" * (-len(cipher) % 4)
    if len(cipher) < _MIN_CIPHER_LEN:
        raise DecryptError("密文过短，可能不完整")
    return key_xml, cipher


def _find_local(root: ET.Element, tag: str) -> str | None:
    """忽略命名空间查找子元素并返回文本。"""
    for element in root.iter():
        if element.tag.split("}")[-1] == tag and element.text and element.text.strip():
            return element.text.strip()
    return None


def _build_private_key(key_xml: str) -> rsa.RSAPrivateKey:
    """由 C# RSAKeyValue XML 构造私钥。"""
    try:
        root = ET.fromstring(key_xml)
    except ET.ParseError as exc:
        raise DecryptError(f"RSAKeyValue XML 解析失败: {exc}") from exc  # noqa: TRY003

    def b64_int(tag: str) -> int:
        value = _find_local(root, tag)
        if value is None:
            raise DecryptError(f"缺少 {tag}")  # noqa: TRY003
        return int.from_bytes(base64.b64decode(value), "big")

    modulus = b64_int("Modulus")
    exponent = b64_int("Exponent")
    d = b64_int("D")
    p = b64_int("P")
    q = b64_int("Q")
    dp = b64_int("DP")
    dq = b64_int("DQ")
    iqmp = b64_int("InverseQ")
    public = rsa.RSAPublicNumbers(exponent, modulus)
    private = rsa.RSAPrivateNumbers(p, q, d, dp, dq, iqmp, public)
    return private.private_key()


def _decrypt_block(private_key: rsa.RSAPrivateKey, chunk: bytes) -> bytes:
    """解密单个 RSA 块：先 OAEP(SHA1)，失败回退 PKCS1v15。"""
    oaep = padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA1()),
        algorithm=hashes.SHA1(),
        label=None,
    )
    try:
        return private_key.decrypt(chunk, oaep)
    except ValueError:
        return private_key.decrypt(chunk, padding.PKCS1v15())


def _parse_json_lenient(text: str) -> dict:
    """解析解密明文为 JSON（带 ``{``/``}`` 修正兜底）。"""
    if not text:
        raise DecryptError("明文为空")
    if not text.lstrip().startswith("{"):
        text = "{" + text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fixed = text
        if fixed.strip().startswith("{") and not fixed.strip().endswith("}"):
            fixed = fixed + "}"
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as exc:
            raise DecryptError(f"JSON 解析失败: {exc}") from exc  # noqa: TRY003
    if not isinstance(data, dict):
        raise DecryptError("解密结果不是有效的账号数据")
    return data


def _protocol_keys(name: str) -> tuple[str, ...]:
    """读取 ``SaveProtocol/<名>.txt`` 的键序模板。

    模板与游戏 ``Resources/Text/SaveProtocol/<名>.txt`` 逐字节同源：按逗号
    分隔键名、允许换行排版。游戏更新协议时（键序即数据顺序，不可改写）
    只需把新的模板文件放进目录，代码无需改动。
    """
    if not _PROTOCOL_NAME_RE.fullmatch(name):
        raise DecryptError(f"存档协议名不合法：{name!r}")
    path = _PROTOCOL_DIR / f"{name}.txt"
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DecryptError(  # noqa: TRY003
            f"缺少存档协议模板 {name}.txt（游戏更新协议后需同步放入"
            f" {_PROTOCOL_DIR}），请反馈给 bot 维护者"
        ) from exc
    keys = [key.strip() for key in content.split(",")]
    # 末尾逗号/空行产生的空段丢弃，其余位置的空段视为模板损坏
    while keys and not keys[-1]:
        keys.pop()
    if not keys or any(not key for key in keys):
        raise DecryptError(f"存档协议模板 {name}.txt 存在空键")  # noqa: TRY003
    if len(set(keys)) != len(keys):
        raise DecryptError(f"存档协议模板 {name}.txt 存在重复键")  # noqa: TRY003
    return tuple(keys)


def _decode_protocol_value(value: str) -> str:
    """协议值 → 逗号串：``GZIP1:`` 前缀按 GZip 解压，其它值原样返回（旧格式）。"""
    if not value.startswith(_GZIP_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(_GZIP_PREFIX) :])
    except (binascii.Error, ValueError) as exc:
        raise DecryptError(f"存档协议压缩数据 Base64 解码失败: {exc}") from exc  # noqa: TRY003
    # 与游戏 DecompressContent 一致的格式校验
    if len(raw) < _GZIP_MIN_LEN or not raw.startswith(_GZIP_MAGIC):
        raise DecryptError("存档协议压缩数据不是有效的 GZip 格式")  # noqa: TRY003
    try:
        content = gzip.decompress(raw)
    except (OSError, EOFError) as exc:
        raise DecryptError(f"存档协议解压失败: {exc}") from exc  # noqa: TRY003
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptError(f"存档协议明文不是有效的 UTF-8: {exc}") from exc  # noqa: TRY003


def expand_protocol(data: dict, *, inplace: bool = False) -> dict:
    """展开存档中的 ``SaveProtocol_<名>`` 压缩成绩块，还原成明文键。

    游戏 26_8_30 起，克莱因导出/转移的存档会把 ``<曲名>/Unlock`` 与全部
    ``BestScore_`` / ``BestCombo_`` 键按协议模板顺序压成一条
    ``GZIP1:`` + Base64 的值（本地保存的存档仍是明文键），游戏读取时用
    ``ProtocolUtil.Decompress`` 还原。此处复刻同一流程，使两种存档
    （以及直接粘贴的 JSON）解析后得到相同的键值。

    空位代表该键在存档中不存在（游戏压缩时用空字符串占位）。
    默认返回副本；``inplace=True`` 时直接修改并返回原字典。
    模板缺失或数据不合法时报错——否则会把「无成绩」当成真实成绩。
    """
    names = sorted(
        key[len(_PROTOCOL_PREFIX) :] for key in data if key.startswith(_PROTOCOL_PREFIX)
    )
    if not names:
        return data if inplace else dict(data)
    result = data if inplace else dict(data)
    for name in names:
        keys = _protocol_keys(name)
        value = result.pop(_PROTOCOL_PREFIX + name)
        if value is None:
            raise DecryptError(f"存档协议 {name} 数据为 null")  # noqa: TRY003
        values = _decode_protocol_value(str(value)).split(",")
        if len(values) != len(keys):
            raise DecryptError(  # noqa: TRY003
                f"存档协议 {name} 数据条数与模板不一致：{len(values)} != {len(keys)}"
            )
        for template_key, item in zip(keys, values):
            if not item:
                continue
            if template_key in result:
                raise DecryptError(  # noqa: TRY003
                    f"存档协议 {name} 数据与明文键同时存在：{template_key}"
                )
            result[template_key] = item
    return result


def _private_key_to_xml(key: rsa.RSAPrivateKey) -> str:
    """RSA 私钥 → .NET ``RSAKeyValue`` XML（游戏存档头格式）。"""
    numbers = key.private_numbers()
    public = numbers.public_numbers

    def b64_bytes(value: int) -> str:
        return base64.b64encode(
            value.to_bytes((value.bit_length() + 7) // 8, "big")
        ).decode("ascii")

    return (
        "<RSAKeyValue>"
        f"<Modulus>{b64_bytes(public.n)}</Modulus>"
        f"<Exponent>{b64_bytes(public.e)}</Exponent>"
        f"<P>{b64_bytes(numbers.p)}</P>"
        f"<Q>{b64_bytes(numbers.q)}</Q>"
        f"<DP>{b64_bytes(numbers.dmp1)}</DP>"
        f"<DQ>{b64_bytes(numbers.dmq1)}</DQ>"
        f"<InverseQ>{b64_bytes(numbers.iqmp)}</InverseQ>"
        f"<D>{b64_bytes(numbers.d)}</D>"
        "</RSAKeyValue>"
    )


def generate_save_key() -> rsa.RSAPrivateKey:
    """生成新的存档密钥（与游戏一致：1024 位；存档内嵌私钥，新密钥可直接导入）。"""
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)


def build_save_text(key: rsa.RSAPrivateKey, account: dict) -> str:
    """把账号 JSON 加密为游戏可导入的存档文本（与游戏导出格式逐字节一致）。"""
    plaintext = json.dumps(
        account, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-16-le")
    # PKCS1v15 每块最大明文 = 块大小 - 11（1024 位 = 117 字节）
    max_plain = key.key_size // 8 - 11
    cipher = bytearray()
    for offset in range(0, len(plaintext), max_plain):
        chunk = plaintext[offset : offset + max_plain]
        cipher.extend(key.public_key().encrypt(bytes(chunk), padding.PKCS1v15()))
    return (
        f"{_private_key_to_xml(key)}<SecKey>"
        f"{base64.b64encode(bytes(cipher)).decode('ascii')}"
    )
