r"""image_io.py -- 搜图图片 IO：引用物化 / SSRF 防护 / 体积像素校验 / 压缩 / 传输形态（v2.0.0）。

本模块由旧插件 `astrbot_plugin_vision_router/bridge_core.py` 按需裁剪搬迁（不重写
SSRF 与压缩逻辑），仅做三处对齐 PRD 的改动：

1. **图床文件名改为 32 位 sha256 前缀**（PRD §2.6 冻结白名单
   `^[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif|bmp)$`，旧实现是 24 位 sha1 前缀）；
2. **新增 `resolve_image_bytes` 的错误码语义**（PRD §1.4/§2.7/§3.1：
   `bad_ref` / `ssrf_blocked` / `download_failed` / `image_too_large`），
   旧实现的用户向中文文案不再需要（工具返回结构化 JSON）；
3. **删除 v1.x 的 vision_mode / build_transport_inputs**：v2.0 传输形态已冻结为
   vision 源一律 data URI、engine 源一律图床 URL（PRD §3.3.4），不需要按源回退；
   压缩目标也改为全局上下限 + 最长边 ≤ 4096（PRD §3.3.3）。

本模块刻意不 import astrbot（只依赖标准库 + 可选 httpx/PIL），便于离线单测。
日志一律英文（PRD §2.7）。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import secrets
import socket
from pathlib import Path

from astrbot.api import logger as LOG

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 与内置 image_server 放行规则一致：32 位小写十六进制（sha256 前 32 位）+ 受限扩展名
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif|bmp)$")
SUPPORTED_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "bmp")
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "jpg",  # bmp 统一转 jpg 压缩
}
_EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}
_REMOTE_PREFIXES = ("http://", "https://")
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[^;,]+)?(?:;[^,]*)*,(?P<payload>.*)$", re.DOTALL
)
_IMG_ATTACH_INLINE_RE = re.compile(r"\[Image Attachment:\s*(.+?)\]", re.DOTALL)

# 参考错误码（PRD §1.4 images[].error / §5 失败矩阵）
ERROR_BAD_REF = "bad_ref"
ERROR_SSRF_BLOCKED = "ssrf_blocked"
ERROR_DOWNLOAD_FAILED = "download_failed"
ERROR_IMAGE_TOO_LARGE = "image_too_large"

# 禁连网段（SSRF 防护）：回环/链路本地/私有/保留/组播/广播/未指定（PRD §3.3.2）
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

# 下载默认超时（秒）；实际由配置 download_timeout_sec 覆盖
DEFAULT_DOWNLOAD_TIMEOUT_SEC = 20

# 手动跟随重定向的跳数上限（每跳均过 SSRF 校验）
MAX_REDIRECT_HOPS = 5

# 压缩后最长边上限（PRD §3.3.3：等比缩放，最长边 ≤ 4096）
MAX_COMPRESS_EDGE = 4096

# JPEG 重编码质量：首选 85（PRD §3.3.3 冻结基线），仍超体积上限时逐档下降
_JPEG_QUALITY_LADDER = (85, 60, 40)

# 物化／读取阶段的**绝对**安全上限（v2.0.0 repair-round-3 / F14）。
#
# 语义（冻结）：该上限**不随 `search.max_size_mb` 缩放**（旧实现乘配置值，导致
# `max_size_mb=1` 时天花板塌到 4MB，照片型大图被误判 image_too_large、压缩路径不可达）；
# 它只是防止病态大文件占满内存的兜底，真正的体积约束由 `preprocess_image()` 按全局
# `max_size_mb` / `max_pixels` 压缩后校验。压缩后仍超全局上限才算 `image_too_large`。
ABSOLUTE_MATERIALIZE_LIMIT_BYTES = 100 * 1024 * 1024

# 魔数嗅探只读的前导字节数（F15：先嗅探再决定是否整体读取）
MAGIC_SNIFF_BYTES = 32

# 内联引用体积估算系数（base64 字符数 → 解码字节数上限）
_BASE64_RATIO_NUM, _BASE64_RATIO_DEN = 3, 4


class ImageTooLargeError(RuntimeError):
    """物化/下载阶段体积超过绝对物化上限（`resolve_image_bytes` 映射为 `image_too_large`）。"""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"image exceeds materialize limit {limit_bytes} bytes")
        self.limit_bytes = int(limit_bytes)


def materialize_limit_bytes() -> int:
    """物化上限（字节）：绝对常量，**与 `search.max_size_mb` 无关**（F14 冻结语义）。"""
    return ABSOLUTE_MATERIALIZE_LIMIT_BYTES


def inline_payload_size(ref: str) -> int | None:
    """估算内联引用的解码后体积（不解码，只按 base64 长度上界估算）。

    支持 `data:` URI 与 `base64://`；其它形态返回 None。

    Args:
        ref: 图片引用。

    Returns:
        估算字节数；非内联形态返回 None。
    """
    text = (ref or "").strip()
    if text.startswith("data:"):
        _, _, payload = text.partition(",")
    elif text.startswith("base64://"):
        payload = text[len("base64://") :]
    else:
        return None
    if not payload:
        return 0
    return len(payload) * _BASE64_RATIO_NUM // _BASE64_RATIO_DEN


# ---------------------------------------------------------------------------
# 基础嗅探 / 引用解析
# ---------------------------------------------------------------------------


def sniff_image_ext(data: bytes) -> str | None:
    """按文件头魔数嗅探图片扩展名（jpg/png/webp/gif/bmp）；无法识别返回 None。

    Args:
        data: 图片字节。

    Returns:
        小写扩展名（无点）；非图片返回 None。
    """
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "jpg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 2 and data[:2] == b"BM":
        return "bmp"
    return None


def parse_data_uri(ref: str) -> tuple[str | None, bytes] | None:
    """解析 data URI，返回 (mime, 字节)；格式非法返回 None。

    Args:
        ref: `data:image/png;base64,...` 形态的引用。

    Returns:
        (mime 或 None, 原始字节)；非法或空载荷返回 None。
    """
    match = _DATA_URI_RE.match(ref.strip())
    if not match:
        return None
    mime = match.group("mime") or None
    try:
        raw = base64.b64decode(match.group("payload"), validate=False)
    except Exception:  # noqa: BLE001 -- 解码异常视为非法引用
        return None
    if not raw:
        return None
    return mime, raw


def is_remote_url(ref: str) -> bool:
    """是否为 http(s) 远程 URL。"""
    return isinstance(ref, str) and ref.startswith(_REMOTE_PREFIXES)


def extract_image_ref_from_text(ref: str) -> str:
    """从 `[Image Attachment: path ...]` 占位文本中提取实际引用；非占位原样返回。

    兼容两种形态（PRD §1.2.1）：`[Image Attachment: path /tmp/a.png]`
    与 `[Image Attachment: /tmp/a.png]`。

    Args:
        ref: 原始引用文本。

    Returns:
        去掉占位包装后的引用；空输入返回空串。
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    match = _IMG_ATTACH_INLINE_RE.search(ref)
    if match:
        inner = match.group(1).strip()
        if inner.startswith("path "):
            inner = inner[5:].strip()
        return inner
    return ref


def looks_like_local_path(ref: str) -> bool:
    """是否为本地路径形态（POSIX 绝对路径或 Windows 盘符路径）。"""
    return bool(ref) and (
        ref.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", ref))
    )


# ---------------------------------------------------------------------------
# SSRF 防护
# ---------------------------------------------------------------------------


def is_blocked_host(host: str) -> bool:
    """判断主机名解析后的 IP 是否属于禁连网段（内网/回环/链路本地等）。

    Args:
        host: URL 主机名或 IP。

    Returns:
        True 表示危险（拒绝下载）；域名解析失败同样按危险处理。
    """
    if not host:
        return True
    lowered = host.strip().lower()
    if lowered == "localhost" or lowered.endswith((".local", ".localhost")):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # 解析失败按危险处理
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return True
    return False


def validate_download_url(url: str) -> str | None:
    """校验下载 URL 是否安全；安全返回原 URL，危险返回 None。

    规则：仅 http(s)；host 解析后不得命中禁连网段（PRD §3.3.2）。

    Args:
        url: 待校验的图片 URL。

    Returns:
        通过校验的原始 URL；被拦截返回 None。
    """
    if not is_remote_url(url):
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host or is_blocked_host(host):
        LOG.warning("SSRF guard blocked image url host: %s", host or "<empty>")
        return None
    return url


# ---------------------------------------------------------------------------
# 图片引用物化
# ---------------------------------------------------------------------------


def read_local_file(path_str: str, max_bytes: int) -> tuple[bytes, str] | None:
    """读取本地图片文件；不存在/超限/非法返回 None。

    v2.0.0 repair-round-3（F15）：**先读前 32 字节判魔数**再决定是否整体读取 ——
    非图片（魔数不匹配且扩展名也不在 `SUPPORTED_EXTS` 白名单内）立即返回 None，
    绝不把文件整体读进内存；只有确认为图片且体积在 `max_bytes` 内才 `read()` 全量。

    Args:
        path_str: 本地文件路径。
        max_bytes: 允许的最大字节数（超限视为不可用）。

    Returns:
        (字节, 扩展名)；不可用返回 None。
    """
    path = Path(path_str)
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            head = handle.read(MAGIC_SNIFF_BYTES)
            ext = sniff_image_ext(head)
            if ext is None:
                # 魔数不认识：仅在扩展名白名单内才继续（兼容容器/异常头部的真图）
                suffix = path.suffix.lstrip(".").lower()
                if suffix not in SUPPORTED_EXTS:
                    return None  # 非图片：读都不读全量
                ext = suffix
            if path.stat().st_size > max_bytes:
                return None
            handle.seek(0)
            raw = handle.read()
    except OSError:
        return None
    if not raw:
        return None
    return raw, ext


def materialize_ref(ref: str, max_bytes: int) -> tuple[bytes, str] | None:
    """把内联/本地图片引用物化为 (字节, 扩展名)；不可用/超限/非法返回 None。

    支持（PRD §1.2.1）：data URI / `base64://` / 本地绝对路径 / `file://` /
    `[Image Attachment: ...]` 占位文本。http(s) URL 由 `resolve_image_bytes`
    先做 SSRF 校验与下载后再进入本函数的下游流程。

    Args:
        ref: 图片引用。
        max_bytes: 允许的最大字节数（超限视为不可用）。

    Returns:
        (字节, 扩展名)；无法物化返回 None。
    """
    ref = extract_image_ref_from_text(ref)
    if not ref:
        return None

    # 形态一：data URI
    if ref.startswith("data:"):
        parsed = parse_data_uri(ref)
        if parsed is None:
            return None
        mime, raw = parsed
        if len(raw) > max_bytes:
            return None
        ext = sniff_image_ext(raw) or _MIME_TO_EXT.get(mime or "")
        return (raw, ext) if ext else None

    # 形态二：base64:// 前缀
    if ref.startswith("base64://"):
        try:
            raw = base64.b64decode(ref[len("base64://") :], validate=False)
        except Exception:  # noqa: BLE001 -- 非法 base64 视为坏引用
            return None
        if not raw or len(raw) > max_bytes:
            return None
        ext = sniff_image_ext(raw)
        return (raw, ext) if ext else None

    # 形态三：本地文件路径（file:// 归一化）
    if ref.startswith("file://"):
        return read_local_file(ref[7:], max_bytes)
    if looks_like_local_path(ref):
        return read_local_file(ref, max_bytes)
    return None


async def download_url_bytes(
    url: str, max_bytes: int, timeout_sec: int = DEFAULT_DOWNLOAD_TIMEOUT_SEC
) -> bytes | None:
    """SSRF 防护 + 流式下载远程图片字节；失败/超限/危险返回 None。

    重定向防护：手动逐跳跟随（`follow_redirects=False`），每一跳都重新过
    `validate_download_url` 校验（PRD §3.3.2「重定向后的最终 URL 需二次校验」）
    ——自动跟随重定向会在「公网 URL 302 → 内网地址」场景绕过 SSRF 校验。

    Args:
        url: 公网 http(s) 图片 URL。
        max_bytes: 允许的最大字节数。
        timeout_sec: 读取超时（秒）。

    Returns:
        图片字节；网络/HTTP/非图片等失败返回 None（由调用方映射为 `download_failed`）。

    Raises:
        ImageTooLargeError: 响应体超过 `max_bytes`（由调用方映射为 `image_too_large`，
            与「下载失败」区分开）。
    """
    current = validate_download_url(url)
    if current is None:
        LOG.warning("image url rejected by SSRF guard: %.80s", url)
        return None
    from urllib.parse import urljoin

    import httpx

    try:
        timeout = httpx.Timeout(connect=10, read=timeout_sec, write=15, pool=15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECT_HOPS + 1):
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            LOG.warning(
                                "image download redirect without location header"
                            )
                            return None
                        nxt = urljoin(current, location)
                        if validate_download_url(nxt) is None:
                            LOG.warning("redirect target blocked: %.120s", nxt)
                            return None
                        current = nxt
                        continue
                    if resp.status_code != 200:
                        LOG.warning("image download failed: HTTP %s", resp.status_code)
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > max_bytes:
                            LOG.warning("image download exceeded %d bytes", max_bytes)
                            raise ImageTooLargeError(max_bytes)
                        chunks.append(chunk)
                    return b"".join(chunks)
            LOG.warning("image download exceeded %d redirects", MAX_REDIRECT_HOPS)
            return None
    except ImageTooLargeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- 下载失败统一转错误码
        LOG.warning("image download error: %s", type(exc).__name__)
        return None


async def resolve_image_bytes(
    ref: str, max_size_mb: int, download_timeout_sec: int
) -> tuple[bytes | None, str | None, str | None]:
    """组合入口：图片引用 -> (字节, 扩展名, 错误码)。

    物化阶段使用**绝对**上限 `ABSOLUTE_MATERIALIZE_LIMIT_BYTES`（100MiB，**与
    `max_size_mb` 无关**，F14 冻结语义）：≤ 该上限的图先读入，再由上层
    `preprocess_image()` 按全局 `max_size_mb` / `max_pixels` 压缩；只有连绝对上限
    都超（读都读不进来）或压缩后仍超全局上限，才判 `image_too_large`。

    错误码（PRD §1.4 / §2.7）：
    - `bad_ref`：非字符串引用、空串、无法解析的引用、本地文件不存在、内联数据非法；
    - `ssrf_blocked`：URL 命中内网/保留网段；
    - `download_failed`：下载超时、非 200、非图片魔数；
    - `image_too_large`：本地文件 / 内联载荷 / 下载响应体超过物化安全上限。

    Args:
        ref: 原始图片引用（可含 `[Image Attachment: ...]` 包装）。
        max_size_mb: 全局体积上限（MB），由上层压缩阶段使用（不影响物化上限）。
        download_timeout_sec: 远程下载超时（秒）。

    Returns:
        (字节, 扩展名, 错误码)；成功时错误码为 None。
    """
    if not isinstance(
        ref, str
    ):  # 非字符串引用（LLM 可能给数字/对象）→ 坏引用，不抛异常
        return None, None, ERROR_BAD_REF
    ref = ref.strip()
    if not ref:
        return None, None, ERROR_BAD_REF
    ref = extract_image_ref_from_text(ref)
    if not ref:
        return None, None, ERROR_BAD_REF

    materialize_bytes = materialize_limit_bytes()

    if is_remote_url(ref):
        if validate_download_url(ref) is None:
            return None, None, ERROR_SSRF_BLOCKED
        try:
            data = await download_url_bytes(
                ref, materialize_bytes, int(download_timeout_sec)
            )
        except ImageTooLargeError:
            return None, None, ERROR_IMAGE_TOO_LARGE
        if data is None:
            return None, None, ERROR_DOWNLOAD_FAILED
        ext = sniff_image_ext(data)
        if ext is None:
            return None, None, ERROR_DOWNLOAD_FAILED
        return data, ext, None

    # 本地路径：先看文件大小，超物化上限 → image_too_large（而非 bad_ref）
    local_path = ref[7:] if ref.startswith("file://") else ref
    if looks_like_local_path(local_path):
        try:
            path = Path(local_path)
            if path.is_file() and path.stat().st_size > materialize_bytes:
                LOG.warning("local image exceeds materialize limit: %s", path.name)
                return None, None, ERROR_IMAGE_TOO_LARGE
        except OSError:
            pass

    # 内联引用：按 base64 长度估算解码后体积，超物化上限 → image_too_large
    approx_bytes = inline_payload_size(ref)
    if approx_bytes is not None and approx_bytes > materialize_bytes:
        LOG.warning(
            "inline image exceeds materialize limit (approx %d bytes)", approx_bytes
        )
        return None, None, ERROR_IMAGE_TOO_LARGE

    materialized = materialize_ref(ref, materialize_bytes)
    if materialized is None:
        return None, None, ERROR_BAD_REF
    return materialized[0], materialized[1], None


# ---------------------------------------------------------------------------
# PIL 预处理（大小/像素校验 + 降采样压缩）
# ---------------------------------------------------------------------------


def _pil_image():
    """惰性导入 PIL.Image；不可用时返回 None（模块可无 PIL 加载）。"""
    try:
        from PIL import Image

        return Image
    except Exception:  # noqa: BLE001 -- 无 PIL 时仅关闭压缩能力
        return None


def _try_pil_open(data: bytes):
    """尝试用 PIL 打开图片；失败返回 None（不抛异常）。"""
    try:
        from io import BytesIO

        image_cls = _pil_image()
        if image_cls is None:
            return None
        return image_cls.open(BytesIO(data))
    except Exception:  # noqa: BLE001 -- 解码失败由调用方处理
        return None


def image_pixels(data: bytes) -> int:
    """探测图片像素数（宽×高）；无法解码返回 0。

    Args:
        data: 图片字节。

    Returns:
        像素总数；非图片或 PIL 不可用时为 0（调用方按「未知」处理）。
    """
    image = _try_pil_open(data)
    if image is None:
        return 0
    try:
        width, height = image.size
    except Exception:  # noqa: BLE001 -- 异常视为未知
        return 0
    return int(width) * int(height)


def preprocess_image(
    data: bytes, ext: str, max_size_mb: int, max_pixels: int
) -> tuple[bytes, str, bool]:
    """校验并压缩图片，返回 (字节, 扩展名, 是否发生变更)。

    规则（PRD §3.3.3）：
    1. 未超 `max_size_mb` 且未超 `max_pixels` → 原样返回，`changed=False`；
    2. 超限 → 等比缩放（最长边 ≤ 4096）+ 重编码 JPEG（首选 quality 85）；
       压缩后仍超体积上限 → 用原图返回（`changed=False`），由调用方按上限校验
       并返回 `image_too_large`；
    3. 无法用 PIL 解码的字节 → 原样返回（由调用方按上限校验）。

    Args:
        data: 图片字节。
        ext: 嗅探得到的扩展名。
        max_size_mb: 全局体积上限（MB）。
        max_pixels: 全局像素上限（宽×高）。

    Returns:
        (字节, 扩展名, 是否变更)；本函数不抛异常。
    """
    max_bytes = max(int(max_size_mb), 1) * 1024 * 1024
    size_over = len(data) > max_bytes

    image = _try_pil_open(data)
    pixels = 0
    width = height = 0
    if image is not None:
        try:
            width, height = image.size
            pixels = int(width) * int(height)
        except Exception:  # noqa: BLE001 -- 尺寸读取失败按未知处理
            image = None
    pixels_over = int(max_pixels) > 0 and pixels > int(max_pixels)

    if not size_over and not pixels_over:
        return data, ext, False
    if image is None:
        # 非标准图片（无法解码）：无法压缩，交调用方按上限校验
        return data, ext, False

    image_cls = _pil_image()
    if image_cls is None:
        return data, ext, False
    try:
        from io import BytesIO

        target_pixels = int(max_pixels) if int(max_pixels) > 0 else pixels
        ratio = min((target_pixels / pixels) ** 0.5, 1.0) if pixels else 1.0
        longest = max(width, height)
        if longest > MAX_COMPRESS_EDGE:
            ratio = min(ratio, MAX_COMPRESS_EDGE / longest)
        new_width = max(1, int(width * ratio))
        new_height = max(1, int(height * ratio))
        img = image.convert("RGB")  # RGB 转换同时铺白底，无需单独处理透明通道
        if (new_width, new_height) != (width, height):
            img = img.resize((new_width, new_height), image_cls.LANCZOS)
        compressed = b""
        for quality in _JPEG_QUALITY_LADDER:
            out = BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            compressed = out.getvalue()
            if len(compressed) <= max_bytes:
                break
        if not compressed or len(compressed) > max_bytes:
            LOG.warning("image compression could not satisfy the size limit")
            return data, ext, False
        LOG.info(
            "image compressed: %dx%d -> %dx%d, %dKB -> %dKB",
            width,
            height,
            new_width,
            new_height,
            len(data) // 1024,
            len(compressed) // 1024,
        )
        return compressed, "jpg", True
    except Exception as exc:  # noqa: BLE001 -- 压缩失败用原图，由调用方按上限校验
        LOG.warning("image compression failed: %s", type(exc).__name__)
        return data, ext, False


# ---------------------------------------------------------------------------
# 传输形态：图床落盘（engine 用 URL） / data URI（vision 用）
# ---------------------------------------------------------------------------


def publish_image(data: bytes, ext: str, imgserv_dir: str) -> str:
    """把图片字节写入图床目录，返回文件名（确定性内容哈希名，幂等续期）。

    Args:
        data: 图片字节。
        ext: 扩展名（可带点）。
        imgserv_dir: 图床目录。

    Returns:
        `sha256 前 32 位.扩展名` 形态的文件名。

    Raises:
        ValueError: 扩展名不在支持集合内。
        OSError: 目录创建或写入失败（调用方决定如何降级）。
    """
    ext = ext.lstrip(".").lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"unsupported ext: {ext}")
    digest = hashlib.sha256(data).hexdigest()[:32]
    filename = f"{digest}.{ext}"
    directory = Path(imgserv_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if target.exists():
        try:
            os.utime(target, None)  # 幂等续期
        except OSError:
            pass
        return filename
    tmp = directory / f".tmp_{digest}_{os.getpid()}_{secrets.token_hex(4)}"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)  # 原子写入
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return filename


def build_public_url(base_url: str, filename: str) -> str:
    """拼接公网 URL：base_url 去尾部斜杠 + 文件名（URL 无 /files/ 前缀）。"""
    return f"{base_url.rstrip('/')}/{filename}"


def to_data_uri(data: bytes, ext: str) -> str:
    """图片字节 -> data URI（vision 源的唯一传输形态，PRD §3.3.4）。

    Args:
        data: 图片字节。
        ext: 扩展名（可带点）。

    Returns:
        `data:<mime>;base64,<payload>`。
    """
    ext = ext.lstrip(".").lower()
    mime = _EXT_TO_MIME.get(ext) or f"image/{ext or 'jpeg'}"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def is_imgserv_filename(name: str) -> bool:
    """校验文件名是否符合内置图床放行规则（PRD §2.6 白名单）。"""
    return bool(_FILENAME_RE.match(name))
