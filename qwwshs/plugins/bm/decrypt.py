"""Berry Melody 存档解密。

复刻 ``bm-score(4).html`` 中 ``performDecryption`` 的流程：

1. 提取 ``<RSAKeyValue>`` 私钥 XML
2. 密文按 RSA 密钥块大小分块，先尝试 RSA-OAEP(SHA1)，失败回退 RSAES-PKCS1-V1_5
3. 拼接明文，UTF-8 解码并去除控制字符
4. 按 JSON 解析（带 ``{``/``}`` 修正兜底）
"""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class DecryptError(Exception):
    """存档解密失败。"""


# 密文最短长度（RSA 块 base64 后至少百字符级别，过短说明粘贴不完整）
_MIN_CIPHER_LEN = 100


def parse_account_data(text: str) -> dict:
    """自动识别并解析账号数据。

    - 包含 ``<RSAKeyValue>`` 标签 → 按原始存档 RSA 解密
    - 以 ``{`` 开头 → 直接按 JSON 解析
    """
    # 入口兜底：剔除 NUL 等控制字符（UTF-16 无 BOM 存档被误按 UTF-8
    # 解码后，NUL 会穿插在 <RSAKeyValue> 标签中间，必须先剔除才能识别）
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text.strip())
    if "<RSAKeyValue>" in text and "</RSAKeyValue>" in text:
        return decrypt_save(text)
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecryptError(f"JSON 解析失败: {exc}") from exc  # noqa: TRY003
        if not isinstance(data, dict):
            raise DecryptError("JSON 不是有效的账号数据")  # noqa: TRY003
        return data
    raise DecryptError(  # noqa: TRY003
        "无法识别账号数据：请粘贴从 <RSAKeyValue> 开始的完整存档，或解密后的 JSON"
    )


def decrypt_save(text: str) -> dict:
    """解密完整存档文本并返回账号 JSON。"""
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
    decoded = plaintext.decode("utf-8", errors="replace")
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
