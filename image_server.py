r"""image_server.py -- 插件内置只读 HTTP 图床服务（v2.1.0，只读，零第三方依赖）。

用途（PRD §2.6）：**仅 B 类 engine 源需要公网图片 URL**（SauceNAO / ascii2d /
trace.moe 都只接受 URL）；vision 源一律走 data URI，不依赖本服务。因此本服务
默认 `enabled=false`，用户在 WebUI 打开 `image_url.enabled` 后才启动。

安全设计（PRD §2.6 冻结约束）：
- **只读**：仅 GET/HEAD，其余一律 405；
- **文件名白名单**：`^[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif|bmp)$`
  （sha256 前 32 位；v1.x 的 24 位 sha1 前缀已废弃，与 image_io.publish_image 对齐）；
- **URL 解码防目录穿越**：unquote 解码后强制过白名单正则——点号/斜杠/编码穿越
  （`../`、`%2e%2e`、`%5c` 等）全部拦截；解码后含路径分隔符/`..` 的穿越尝试返回
  403，白名单不匹配返回 404；
- **无目录列表**：根路径/目录路径一律 404；
- 绑定 `0.0.0.0`（哈希文件名不可猜测）；单文件大小上限 30MB；
  `Cache-Control: no-store`（临时图片不缓存）。

生命周期（由 main.py 驱动）：
- `ImageServer.start()`：目录就绪（mkdir + 可写探测）→ 端口探测（被占用自动 +1，
  最多 10 次，PRD §2.6）→ 启动 daemon 线程；
- `ImageServer.stop()`：shutdown 服务器并回收线程（幂等）。

本模块零 astrbot 依赖（仅标准库），可独立单测。日志一律英文（PRD §2.7）。
"""

from __future__ import annotations

import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# 合法文件名：32 位小写十六进制（sha256 前 32 位）+ 受限图片扩展名
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif|bmp)$")

# 扩展名 -> Content-Type 映射
_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

# 单文件大小上限（字节）：超限视为异常文件拒绝服务，防误放巨型文件
MAX_SERVE_BYTES = 30 * 1024 * 1024

# 端口自动探测上限（配置 port 起最多 +10 次，PRD §2.6）
MAX_PORT_PROBE = 10

logger = logging.getLogger("image_search.image_server")


class _ImageRequestHandler(BaseHTTPRequestHandler):
    """只读图片请求处理器：GET/HEAD 返回白名单文件字节，其余一律拒绝。"""

    # 类属性由 ImageServer 注入
    files_dir: Path = Path("/tmp")
    server_version = "image-search-imageserver/2.0"
    protocol_version = "HTTP/1.1"  # 长连接，第三方抓取更友好

    def _resolve(self, raw_path: str) -> tuple[Path | None, int | None]:
        """解析请求路径：返回 (目标文件 或 None, 拒绝状态码 或 None)。

        规则：
        - unquote 解码失败 / 解码后含路径分隔符或 ..（穿越尝试）→ (None, 403)；
        - 白名单不匹配（含目录路径、非法名、根路径）→ (None, 404)；
        - 命中白名单 → (files_dir/name, None)。
        """
        try:
            name = unquote(urlparse(raw_path).path).lstrip("/")
        except Exception:  # noqa: BLE001 -- 解码异常一律视为非法
            return None, 403
        if not name:
            return None, 404  # 根路径：无目录列表
        # 目录穿越尝试（解码后仍含路径分隔符或 .. 段）
        if "/" in name or "\\" in name or ".." in name:
            return None, 403
        if not _FILENAME_RE.match(name):
            return None, 404
        return self.files_dir / name, None

    def _send_simple(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, head_only: bool) -> None:
        target, reject = self._resolve(self.path)
        if reject is not None:
            self._send_simple(
                reject, b"forbidden\n" if reject == 403 else b"not found\n"
            )
            return
        if target is None:
            self._send_simple(404, b"not found\n")
            return
        try:
            stat = target.stat()
        except OSError:
            self._send_simple(404, b"not found\n")
            return
        if stat.st_size > MAX_SERVE_BYTES:
            self._send_simple(413, b"file too large\n")
            return
        content_type = _CONTENT_TYPES.get(
            target.suffix.lstrip(".").lower(), "application/octet-stream"
        )
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if head_only:
                return
            with target.open("rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（第三方抓完即断很常见），非错误
            pass
        except OSError as exc:
            logger.warning("failed to read file %s: %s", target.name, exc)
            try:
                self._send_simple(500, b"read error\n")
            except Exception:  # noqa: BLE001 -- 响应发送失败无需处理
                pass

    # ---- HTTP 方法 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- http.server 命名约定
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def _reject_write(self) -> None:
        # 只读服务：明确拒绝一切写方法
        self._send_simple(405, b"read-only service\n")

    do_POST = _reject_write
    do_PUT = _reject_write
    do_DELETE = _reject_write
    do_PATCH = _reject_write

    # ---- 日志降噪 ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        code = args[1] if len(args) >= 2 else "?"
        level = logging.DEBUG if str(code).startswith(("2", "3")) else logging.INFO
        logger.log(level, "%s %s", self.address_string(), fmt % args)


def find_free_port(
    start_port: int, max_probe: int = MAX_PORT_PROBE
) -> tuple[int, str | None]:
    """从 start_port 起探测空闲 TCP 端口（最多尝试 max_probe 次）。

    Args:
        start_port: 起始端口。
        max_probe: 最多探测次数（PRD §2.6 冻结为 10）。

    Returns:
        (实际可用端口, 提示文本或 None)。

    Raises:
        RuntimeError: 探测范围内端口全部被占用。
    """
    port = max(int(start_port), 1)
    note = None
    for _ in range(max_probe):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                if note is None:
                    note = f"port {start_port} is in use"
                port += 1
                continue
            return port, note
    raise RuntimeError(
        f"image_url port probe failed: {start_port}~{start_port + max_probe - 1} all in use"
    )


class ImageServer:
    """插件内置只读图床服务（线程生命周期管理）。

    用法：
        server = ImageServer(directory, port=8123)
        server.start()          # 目录就绪 + 端口探测 + 启动线程
        ...
        server.stop()           # 幂等停止
    """

    def __init__(self, directory: str, port: int = 8123) -> None:
        self.directory = Path(directory)
        self.port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port_note: str | None = None  # 端口探测提示（如 "port 8123 is in use"）
        self.bound_port: int | None = None  # 实际绑定端口

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动服务（幂等：已在运行则直接返回）。目录就绪 + 端口探测 + 线程启动。

        Raises:
            RuntimeError: 目录不可写或端口全部被占用/监听失败。
        """
        if self._httpd is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        probe = self.directory / ".image_server_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"image_url directory is not writable: {self.directory} ({exc})"
            ) from None

        bound, note = find_free_port(self.port)
        self.bound_port = bound
        self.port_note = note

        handler = type(
            "Handler", (_ImageRequestHandler,), {"files_dir": self.directory}
        )
        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", bound), handler)
        except OSError as exc:
            raise RuntimeError(f"image_url server failed to start: {exc}") from None

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="image-search-image-server",
            daemon=True,
        )
        self._thread.start()
        if note:
            logger.warning(
                "image_url port %d is in use, switched to %d", self.port, bound
            )
        else:
            logger.info(
                "image_url server started: dir=%s port=%d (0.0.0.0, read-only)",
                self.directory,
                bound,
            )

    def stop(self) -> None:
        """停止服务（幂等）。"""
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        thread = self._thread
        self._thread = None
        try:
            httpd.shutdown()
            httpd.server_close()
        except OSError:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        logger.info("image_url server stopped (port=%s)", self.bound_port)

    @property
    def running(self) -> bool:
        """服务是否在运行。"""
        return self._httpd is not None

    def public_base_url(self, auto_base: str) -> str:
        """返回该服务的公网前缀：配置前缀优先；为空时用 auto_base（探测公网 IP）。

        Args:
            auto_base: 调用方探测出的公网前缀（`http://<公网IP>:<bound_port>`）。

        Returns:
            去尾部斜杠的公网前缀。
        """
        base = (auto_base or "").strip()
        if base:
            return base.rstrip("/")
        return f"http://127.0.0.1:{self.bound_port or self.port}"
