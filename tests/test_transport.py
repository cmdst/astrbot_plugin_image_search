"""test_transport.py -- 图片 IO / 传输形态单测（PRD §1.2.1 / §2.6 / §2.7 / §3.3）。

覆盖点：
- 魔数嗅探、data URI 构造与 MIME 映射；
- 引用物化（本地路径 / file:// / data URI / base64:// / [Image Attachment: ...]）；
- SSRF 防护（仅 http(s)；内网/回环/链路本地 IP 一律拦截，含重定向目标二次校验入口）；
- 体积 / 像素校验与压缩（超像素等比缩放 + JPEG 重编码；不可压缩时按上限拒绝）；
- 图床落盘文件名白名单（32 位 sha256 前缀，旧 24 位 sha1 名不再放行）；
- 内置只读图床真实 HTTP 行为（GET 200 / 非法名 404 / 穿越 403 / 写方法 405）。
"""

from __future__ import annotations

import io

import pytest
from conftest import data_uri, run

from image_io import (
    ABSOLUTE_MATERIALIZE_LIMIT_BYTES,
    ERROR_BAD_REF,
    ERROR_DOWNLOAD_FAILED,
    ERROR_IMAGE_TOO_LARGE,
    ERROR_SSRF_BLOCKED,
    MAGIC_SNIFF_BYTES,
    ImageTooLargeError,
    build_public_url,
    extract_image_ref_from_text,
    image_pixels,
    inline_payload_size,
    is_imgserv_filename,
    materialize_limit_bytes,
    materialize_ref,
    parse_data_uri,
    preprocess_image,
    publish_image,
    read_local_file,
    resolve_image_bytes,
    sniff_image_ext,
    to_data_uri,
    validate_download_url,
)


class TestSniffAndDataUri:
    @pytest.mark.parametrize(
        ("magic", "expected"),
        [
            (b"\xff\xd8\xff\xe0junk", "jpg"),
            (b"\x89PNG\r\n\x1a\nrest", "png"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
            (b"GIF89a....", "gif"),
            (b"BM\x00\x00", "bmp"),
            (b"not-an-image", None),
            (b"", None),
        ],
    )
    def test_sniff(self, magic, expected):
        assert sniff_image_ext(magic) == expected

    def test_to_data_uri_png(self, tiny_png_bytes):
        uri = to_data_uri(tiny_png_bytes, "png")
        assert uri.startswith("data:image/png;base64,")
        mime, raw = parse_data_uri(uri)
        assert mime == "image/png"
        assert raw == tiny_png_bytes

    @pytest.mark.parametrize(
        ("ext", "mime"),
        [
            ("jpg", "image/jpeg"),
            ("jpeg", "image/jpeg"),
            ("webp", "image/webp"),
            ("gif", "image/gif"),
        ],
    )
    def test_to_data_uri_mime_map(self, tiny_png_bytes, ext, mime):
        assert to_data_uri(tiny_png_bytes, ext).startswith(f"data:{mime};base64,")

    def test_parse_data_uri_rejects_garbage(self):
        assert parse_data_uri("https://example.com/a.png") is None
        assert parse_data_uri("data:image/png;base64,") is None


class TestMaterialize:
    def test_extract_attachment_placeholder(self):
        assert (
            extract_image_ref_from_text("[Image Attachment: path /tmp/a.png]")
            == "/tmp/a.png"
        )
        assert (
            extract_image_ref_from_text("[Image Attachment: /tmp/b.jpg]")
            == "/tmp/b.jpg"
        )
        assert extract_image_ref_from_text("/tmp/c.png") == "/tmp/c.png"

    def test_local_path_and_file_uri(self, tmp_path, tiny_png_bytes):
        path = tmp_path / "x.png"
        path.write_bytes(tiny_png_bytes)
        assert materialize_ref(str(path), 10 * 1024 * 1024) == (tiny_png_bytes, "png")
        assert materialize_ref(f"file://{path}", 10 * 1024 * 1024) == (
            tiny_png_bytes,
            "png",
        )

    def test_attachment_wrapper_round_trip(self, tmp_path, tiny_png_bytes):
        path = tmp_path / "y.png"
        path.write_bytes(tiny_png_bytes)
        raw = f"[Image Attachment: path {path}]"
        assert materialize_ref(raw, 10 * 1024 * 1024) == (tiny_png_bytes, "png")

    def test_data_uri_and_base64_scheme(self, tiny_png_bytes):
        assert (
            materialize_ref(data_uri(tiny_png_bytes), 10 * 1024 * 1024)[0]
            == tiny_png_bytes
        )
        import base64

        b64 = base64.b64encode(tiny_png_bytes).decode("ascii")
        assert materialize_ref(f"base64://{b64}", 10 * 1024 * 1024)[0] == tiny_png_bytes

    def test_missing_local_file(self, tmp_path):
        assert materialize_ref(str(tmp_path / "ghost.png"), 1024) is None

    def test_oversize_local_file(self, tmp_path, tiny_png_bytes):
        path = tmp_path / "big.png"
        path.write_bytes(tiny_png_bytes)
        assert materialize_ref(str(path), 4) is None

    def test_unknown_shape(self):
        assert materialize_ref("刚才那张图", 1024) is None
        assert materialize_ref("", 1024) is None


class TestResolveErrors:
    def test_empty_and_unknown_ref_is_bad_ref(self):
        assert run(resolve_image_bytes("", 20, 20))[2] == ERROR_BAD_REF
        assert run(resolve_image_bytes("刚才那张图", 20, 20))[2] == ERROR_BAD_REF
        assert run(resolve_image_bytes("   ", 20, 20))[2] == ERROR_BAD_REF

    def test_missing_file_is_bad_ref(self, tmp_path):
        data, ext, error = run(resolve_image_bytes(f"{tmp_path}/nope.png", 20, 20))
        assert (data, ext, error) == (None, None, ERROR_BAD_REF)

    def test_data_uri_ok(self, tiny_png_bytes):
        data, ext, error = run(resolve_image_bytes(data_uri(tiny_png_bytes), 20, 20))
        assert error is None
        assert (data, ext) == (tiny_png_bytes, "png")

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x.png",
            "http://localhost/x.png",
            "http://10.1.2.3/x.png",
            "http://192.168.1.10/x.png",
            "http://169.254.1.1/x.png",
            "http://[::1]/x.png",
            "http://0.0.0.0/x.png",
        ],
    )
    def test_ssrf_blocked(self, url):
        data, ext, error = run(resolve_image_bytes(url, 20, 20))
        assert (data, ext, error) == (None, None, ERROR_SSRF_BLOCKED)

    def test_validate_download_url_rules(self):
        assert validate_download_url("https://93.184.216.34/a.png") == (
            "https://93.184.216.34/a.png"
        )
        assert validate_download_url("ftp://93.184.216.34/a.png") is None
        assert validate_download_url("http://10.0.0.1/a.png") is None
        assert validate_download_url("file:///etc/passwd") is None

    def test_download_failure_is_download_failed(self, monkeypatch):
        import image_io

        async def fake_download(url, max_bytes, timeout=20):
            return None

        monkeypatch.setattr(
            image_io,
            "validate_download_url",
            lambda url: url,  # 视为公网安全 URL（不触发 DNS）
        )
        monkeypatch.setattr(image_io, "download_url_bytes", fake_download)
        data, ext, error = run(resolve_image_bytes("https://img.example/x.png", 20, 20))
        assert (data, ext, error) == (None, None, ERROR_DOWNLOAD_FAILED)

    def test_download_non_image_is_download_failed(self, monkeypatch):
        import image_io

        async def fake_download(url, max_bytes, timeout=20):
            return b"<html>not an image</html>"

        monkeypatch.setattr(image_io, "validate_download_url", lambda url: url)
        monkeypatch.setattr(image_io, "download_url_bytes", fake_download)
        data, ext, error = run(resolve_image_bytes("https://img.example/x.png", 20, 20))
        assert (data, ext, error) == (None, None, ERROR_DOWNLOAD_FAILED)


class TestPreprocess:
    def test_under_limits_untouched(self, tiny_png_bytes):
        data, ext, changed = preprocess_image(tiny_png_bytes, "png", 20, 16777216)
        assert (data, ext, changed) == (tiny_png_bytes, "png", False)

    def test_pixels_over_limit_resized_and_reencoded(self, big_jpeg_bytes):
        data, ext, changed = preprocess_image(big_jpeg_bytes, "jpg", 20, 1048576)
        assert changed is True
        assert ext == "jpg"
        assert image_pixels(data) <= 1048576
        assert sniff_image_ext(data) == "jpg"

    def test_longest_edge_capped(self, big_jpeg_bytes):
        data, ext, changed = preprocess_image(big_jpeg_bytes, "jpg", 20, 67108864)
        # 3000x2000 未超像素上限（6M < 64M），最长边也 < 4096 → 不压缩
        assert changed is False
        assert data == big_jpeg_bytes

    def test_undecodable_oversize_returns_original(self):
        blob = b"\x00" * (2 * 1024 * 1024)
        data, ext, changed = preprocess_image(blob, "bin", 1, 0)
        assert (data, ext, changed) == (blob, "bin", False)

    def test_compressible_oversize_shrinks_below_limit(self):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL 不可用")
        # 未压缩 BMP 构造体积超限（平滑渐变 → JPEG 可压到限内）
        base = Image.linear_gradient("L").resize((2000, 2000)).convert("RGB")
        buf = io.BytesIO()
        base.save(buf, format="BMP")
        blob = buf.getvalue()
        assert len(blob) > 1024 * 1024
        data, ext, changed = preprocess_image(blob, "bmp", 1, 0)
        assert changed is True
        assert ext == "jpg"
        assert len(data) <= 1024 * 1024
        assert sniff_image_ext(data) == "jpg"

    def test_image_pixels_unknown(self):
        assert image_pixels(b"not an image") == 0


class TestPublish:
    def test_filename_is_sha256_prefix(self, tmp_path, tiny_png_bytes):
        import hashlib

        name = publish_image(tiny_png_bytes, "png", str(tmp_path))
        assert name == f"{hashlib.sha256(tiny_png_bytes).hexdigest()[:32]}.png"
        assert is_imgserv_filename(name) is True
        assert (tmp_path / name).read_bytes() == tiny_png_bytes

    def test_publish_idempotent(self, tmp_path, tiny_png_bytes):
        first = publish_image(tiny_png_bytes, "png", str(tmp_path))
        second = publish_image(tiny_png_bytes, "png", str(tmp_path))
        assert first == second
        assert len(list(tmp_path.iterdir())) == 1

    def test_unsupported_ext_rejected(self, tmp_path, tiny_png_bytes):
        with pytest.raises(ValueError):
            publish_image(tiny_png_bytes, "svg", str(tmp_path))

    def test_whitelist_rejects_legacy_sha1_name(self):
        legacy = "a" * 24 + ".png"  # v1.x 的 24 位 sha1 前缀，v2.0 不再放行
        assert is_imgserv_filename(legacy) is False
        assert is_imgserv_filename("a" * 32 + ".png") is True
        assert is_imgserv_filename("a" * 32 + ".svg") is False
        assert is_imgserv_filename("A" * 32 + ".png") is False
        assert is_imgserv_filename("../../etc/passwd") is False

    def test_build_public_url(self):
        assert (
            build_public_url("http://1.2.3.4:8123/", "ab.png")
            == "http://1.2.3.4:8123/ab.png"
        )


class TestImageServerSecurity:
    def test_resolve_whitelist_and_traversal(self, tmp_path):
        from image_server import _ImageRequestHandler

        handler = object.__new__(_ImageRequestHandler)
        handler.files_dir = tmp_path
        good = "b" * 32 + ".jpg"
        assert handler._resolve(f"/{good}") == (tmp_path / good, None)
        assert handler._resolve("/") == (None, 404)
        assert handler._resolve("/" + "b" * 24 + ".jpg") == (
            None,
            404,
        )  # 旧 sha1 名前缀
        assert handler._resolve("/../../etc/passwd") == (None, 403)
        assert handler._resolve("/%2e%2e/secret") == (None, 403)
        assert handler._resolve("/" + "b" * 32 + ".png/") == (None, 403)

    def test_port_probe_skips_occupied_port(self):
        import socket

        from image_server import find_free_port

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", 0))
            sock.listen(1)
            taken = sock.getsockname()[1]
            port, note = find_free_port(taken)
        assert port != taken
        assert note == f"port {taken} is in use"

    def test_live_server_serves_whitelisted_name(self, tmp_path, tiny_png_bytes):
        import httpx

        from image_server import ImageServer, find_free_port

        name = publish_image(tiny_png_bytes, "png", str(tmp_path))
        port, _ = find_free_port(18234)
        server = ImageServer(str(tmp_path), port)
        server.start()
        try:

            async def _probe():
                async with httpx.AsyncClient(timeout=5) as client:
                    base = f"http://127.0.0.1:{server.bound_port}"
                    ok = await client.get(f"{base}/{name}")
                    missing = await client.get(f"{base}/{'c' * 32}.png")
                    traversal = await client.get(f"{base}/%2e%2e/{name}")
                    write = await client.post(f"{base}/{name}")
                    return ok, missing, traversal, write

            ok, missing, traversal, write = run(_probe())
        finally:
            server.stop()
        assert ok.status_code == 200
        assert ok.content == tiny_png_bytes
        assert ok.headers["cache-control"] == "no-store"
        assert missing.status_code == 404
        assert traversal.status_code == 403
        assert write.status_code == 405
        assert server.running is False


class TestNonStringRefs:
    @pytest.mark.parametrize("ref", [123, {}, [], None, 4.5, True])
    def test_non_string_ref_is_bad_ref(self, ref):
        """v2.0.0 repair：非 str 引用直接 bad_ref（不抛异常、不走下载/解码）。"""
        assert run(resolve_image_bytes(ref, 20, 20)) == (None, None, ERROR_BAD_REF)


class TestMaterializeCap:
    def test_cap_is_absolute_and_independent_of_config(self):
        """F14：物化上限是绝对常量（100MiB），**不随 max_size_mb 缩放**。"""
        assert ABSOLUTE_MATERIALIZE_LIMIT_BYTES == 100 * 1024 * 1024
        assert materialize_limit_bytes() == ABSOLUTE_MATERIALIZE_LIMIT_BYTES
        import inspect

        import image_io

        source = inspect.getsource(image_io.materialize_limit_bytes)
        assert "ABSOLUTE_MATERIALIZE_LIMIT_BYTES" in source
        # 实现体（去掉 docstring）不得再乘配置值，且函数不接受 max_size_mb 形参
        body = source.replace(image_io.materialize_limit_bytes.__doc__ or "", "")
        assert "max_size_mb" not in body
        assert (
            list(inspect.signature(image_io.materialize_limit_bytes).parameters) == []
        )

    def test_materialize_limit_is_not_derived_from_config(self, monkeypatch):
        import image_io

        before = image_io.materialize_limit_bytes()
        monkeypatch.setattr(image_io, "ABSOLUTE_MATERIALIZE_LIMIT_BYTES", 4096)
        assert image_io.materialize_limit_bytes() == 4096
        assert before == 100 * 1024 * 1024

    def test_inline_payload_size_estimate(self, tiny_png_bytes):
        uri = data_uri(tiny_png_bytes)
        assert inline_payload_size(uri) == len(uri.split(",", 1)[1]) * 3 // 4
        assert inline_payload_size("/tmp/a.png") is None
        assert inline_payload_size("https://x.invalid/a.png") is None

    def test_compressible_local_oversize_is_materialized_then_compressed(
        self, tmp_path, monkeypatch
    ):
        """本地图超全局上限但未超物化上限 → 可读入，再由 preprocess 压到限内。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL 不可用")
        # 未压缩 BMP（2000x2000 渐变 ≈ 12MB）> 全局 4MB，但 < 物化上限 16MB
        image = Image.linear_gradient("L").resize((2000, 2000)).convert("RGB")
        path = tmp_path / "big.bmp"
        image.save(path, format="BMP")
        assert path.stat().st_size > 4 * 1024 * 1024
        assert path.stat().st_size < materialize_limit_bytes()

        data, ext, error = run(resolve_image_bytes(str(path), 4, 20))
        assert error is None and ext == "bmp"
        compressed, new_ext, changed = preprocess_image(data, ext, 4, 16777216)
        assert changed is True and new_ext == "jpg"
        assert len(compressed) <= 4 * 1024 * 1024

    def test_local_oversize_beyond_cap_is_too_large(self, tmp_path, monkeypatch):
        """超过物化上限（读都读不进来）→ image_too_large，而不是 bad_ref。"""
        import image_io

        # 用 patch 后的 4MiB 上限替代真实的 100MiB，避免测试写巨型文件
        monkeypatch.setattr(
            image_io, "ABSOLUTE_MATERIALIZE_LIMIT_BYTES", 4 * 1024 * 1024
        )
        path = tmp_path / "huge.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (4 * 1024 * 1024))
        data, ext, error = run(resolve_image_bytes(str(path), 1, 20))
        assert (data, ext, error) == (None, None, ERROR_IMAGE_TOO_LARGE)

    def test_inline_oversize_beyond_cap_is_too_large(self, monkeypatch):
        import base64

        import image_io

        monkeypatch.setattr(
            image_io, "ABSOLUTE_MATERIALIZE_LIMIT_BYTES", 4 * 1024 * 1024
        )
        payload = base64.b64encode(b"\x00" * (4 * 1024 * 1024 + 4096)).decode()
        data, ext, error = run(resolve_image_bytes(f"base64://{payload}", 1, 20))
        assert (data, ext, error) == (None, None, ERROR_IMAGE_TOO_LARGE)

    def test_non_image_file_is_not_read_fully(self, tmp_path, monkeypatch):
        """F15：非图片文件只读前 32 字节嗅探，绝不整体读入。"""
        import pathlib as _pathlib

        path = tmp_path / "photo.bin"  # 未知扩展名 + 非图片魔数
        path.write_bytes(b"PK\x03\x04" + b"x" * (2 * 1024 * 1024))
        reads: list[int] = []
        real_open = _pathlib.Path.open

        def spy_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            real_read = handle.read

            def read(size=-1):
                reads.append(size)
                return real_read(size)

            handle.read = read
            return handle

        monkeypatch.setattr(_pathlib.Path, "open", spy_open)
        assert read_local_file(str(path), 100 * 1024 * 1024) is None
        assert reads == [MAGIC_SNIFF_BYTES], f"非图片不应整体读取，实际读取批次={reads}"

    def test_image_file_is_read_fully_after_sniff(self, tmp_path):
        """对照：确认是图片后才整体读取（32 字节嗅探 + 全量读）。"""
        path = tmp_path / "ok.png"
        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        path.write_bytes(blob)
        assert read_local_file(str(path), 1024) == (blob, "png")

    def test_download_oversize_is_too_large_not_download_failed(self, monkeypatch):
        """下载路径超体积 → image_too_large（PRD §1.4 语义区分）。"""
        import image_io

        async def fake_download(url, max_bytes, timeout=20):
            raise ImageTooLargeError(max_bytes)

        monkeypatch.setattr(image_io, "validate_download_url", lambda url: url)
        monkeypatch.setattr(image_io, "download_url_bytes", fake_download)
        data, ext, error = run(resolve_image_bytes("https://img.example/x.png", 20, 20))
        assert (data, ext, error) == (None, None, ERROR_IMAGE_TOO_LARGE)

    def test_materialize_ref_still_caps_at_given_bytes(self, tmp_path, tiny_png_bytes):
        """物化上限由调用方传入：materialize_ref 自身仍按参数拒绝（旧契约不变）。"""
        path = tmp_path / "x.png"
        path.write_bytes(tiny_png_bytes)
        assert materialize_ref(str(path), 4) is None
        assert materialize_ref(str(path), len(tiny_png_bytes)) == (
            tiny_png_bytes,
            "png",
        )
