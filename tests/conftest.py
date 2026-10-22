"""conftest.py -- 搜图路由 Agent v2.0.0 插件单测公共夹具。

导入方式沿用旧插件约定：插件目录是 AstrBot namespace package（无 `__init__.py`，
运行时以 `data.plugins.<plugin_dir>` 导入），测试把 `data/plugins` 加入 `sys.path`，
以 `astrbot_plugin_image_search.<module>` 导入，并注册**新模块名**短名便于测试文件
直接 `from search_core import ...`（不注册旧插件的 router_core / vision_core 等名字）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path

import pytest

PLUGINS_ROOT = str(Path(__file__).resolve().parent.parent.parent)  # .../data/plugins
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

# 以 namespace package 方式导入，保证相对导入（from . import x）可用
import astrbot_plugin_image_search.image_io as _image_io  # noqa: E402
import astrbot_plugin_image_search.image_server as _image_server  # noqa: E402
import astrbot_plugin_image_search.main as _main  # noqa: E402
import astrbot_plugin_image_search.prompts as _prompts  # noqa: E402
import astrbot_plugin_image_search.provider_reader as _provider_reader  # noqa: E402
import astrbot_plugin_image_search.search_core as _search_core  # noqa: E402

# 注册短名（只注册 v2.0 新模块名）
sys.modules.setdefault("search_core", _search_core)
sys.modules.setdefault("provider_reader", _provider_reader)
sys.modules.setdefault("prompts", _prompts)
sys.modules.setdefault("image_io", _image_io)
sys.modules.setdefault("image_server", _image_server)
sys.modules.setdefault("main", _main)


# ---------------------------------------------------------------------------
# 假 AstrBot 运行时对象（duck typing，不依赖真实 ProviderManager）
# ---------------------------------------------------------------------------


class FakeProvider:
    """假 Provider 实例：只需 `provider_config` 与 `get_model()`（PRD §4.7）。"""

    def __init__(self, provider_config: dict) -> None:
        self.provider_config = dict(provider_config)

    def get_model(self) -> str:
        return str(self.provider_config.get("model") or "")


class FakeProviderManager:
    """假 ProviderManager：提供 `providers_config` / `provider_sources_config`。"""

    def __init__(self, providers: list[dict] | None = None) -> None:
        self.providers_config = list(providers or [])
        self.provider_sources_config: list[dict] = []
        self.provider_settings: dict = {}

    def get_provider_config_by_id(self, provider_id: str, *, merged: bool = False):
        for entry in self.providers_config:
            if entry.get("id") == provider_id:
                return dict(entry)
        return None


class FakeContext:
    """假 Context：`get_provider_by_id()` + `provider_manager`（PRD §4.7 两条路径）。"""

    def __init__(
        self,
        providers: dict[str, FakeProvider] | None = None,
        provider_manager: FakeProviderManager | None = None,
    ) -> None:
        self._providers = dict(providers or {})
        self.provider_manager = provider_manager

    def get_provider_by_id(self, provider_id: str):
        return self._providers.get(provider_id)


def doubao_provider(**overrides) -> FakeProvider:
    """样例 A 类 provider 引用（视觉 + 联网，key 为假值）。"""
    config = {
        "id": "豆包/doubao",
        "api_base": "https://ark.example.invalid/api/v3",
        "key": "sk-doubao-test-0001",
        "model": "doubao-seed-1.6-vision",
        "modalities": ["text", "image"],
        "provider_type": "chat_completion",
    }
    config.update(overrides)
    return FakeProvider(config)


def vision_source(**overrides) -> dict:
    """样例 vision 源（provider 形态）。"""
    source = {
        "__template_key": "vision",
        "id": "doubao-search",
        "name": "豆包联网搜索",
        "provider_ref": "豆包/doubao",
        "web_search": True,
        "timeout_sec": 0,
        "enabled": True,
    }
    source.update(overrides)
    return source


def custom_vision_source(**overrides) -> dict:
    """样例 vision 源（自定义 OpenAI 兼容端点形态）。"""
    source = {
        "__template_key": "vision",
        "id": "custom-net",
        "name": "自建联网视觉端点",
        "provider_ref": "",
        "api_base": "https://relay.example.invalid/v1",
        "api_key": "sk-custom-test-0002",
        "model": "gemini-3.5-flash",
        "web_search": True,
        "enabled": True,
    }
    source.update(overrides)
    return source


def engine_source(**overrides) -> dict:
    """样例 engine 源（B 类）。"""
    source = {
        "__template_key": "engine",
        "id": "saucenao-1",
        "name": "SauceNAO",
        "engine": "saucenao",
        "api_key": "",
        "base_url": "",
        "enabled": True,
    }
    source.update(overrides)
    return source


def config_with_sources(*sources, search: dict | None = None) -> dict:
    """组装一份原始配置（search_sources 为 list 形态）。"""
    return {
        "search_sources": list(sources),
        "search": dict(search or {}),
        "image_url": {"enabled": False},
        "download_timeout_sec": 20,
        "tool_hint_enabled": True,
        "log_level": "info",
    }


@pytest.fixture()
def tiny_png_bytes() -> bytes:
    """1x1 红色 PNG（纯标准库构造，无 PIL 依赖）。"""
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        data = tag + payload
        return (
            struct.pack(">I", len(payload))
            + data
            + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture()
def alt_png_bytes() -> bytes:
    """另一张 1x1 蓝色 PNG（与 `tiny_png_bytes` 字节不同，用于多图缓存/隔离测试）。"""
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        data = tag + payload
        return (
            struct.pack(">I", len(payload))
            + data
            + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\x00\x00\xff"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture()
def big_jpeg_bytes() -> bytes:
    """PIL 生成的 3000x2000 JPEG（压缩/像素测试用；无 PIL 时跳过）。"""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL 不可用")
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (120, 180, 240)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def data_uri(data: bytes, mime: str = "image/png") -> str:
    """构造 data URI（测试辅助）。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def variant_data_uri(data: bytes, tag: str) -> str:
    """构造带附加参数段的合法 data URI：引用互不相同，但解出的字节相同。

    用于「多图逐图处理」与「缓存键只含图片哈希」两类断言的区分（PRD §1.2.1 / §3.5）。
    """
    return f"data:image/png;{tag};base64,{base64.b64encode(data).decode('ascii')}"


@pytest.fixture()
def make_plugin():
    """构造已 `initialize()` 的插件实例（可选假 context）。"""

    async def _make(raw_config: dict, context=None):
        plugin = _main.ImageSearchPlugin(
            context if context is not None else FakeContext(), raw_config
        )
        await plugin.initialize()
        return plugin

    return _make


def run(coro):
    """同步测试里跑一段协程（沿用旧插件测试的 asyncio.run 约定）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 假 httpx 客户端（离线断言请求体/脱敏，不发真实请求）
# ---------------------------------------------------------------------------


class FakeHttpxResponse:
    """最小 httpx.Response 替身：状态码 + 文本 + 可选 JSON。"""

    def __init__(self, status_code: int = 200, text: str = "", payload=None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHttpxClient:
    """替换 `httpx.AsyncClient`：记录最后一次请求，返回预置响应。"""

    last_request: dict = {}

    def __init__(self, response: FakeHttpxResponse) -> None:
        self.response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_request = {"url": url, "json": json, "headers": headers}
        return self.response


def install_fake_httpx(monkeypatch, response: FakeHttpxResponse) -> None:
    """把 `httpx.AsyncClient` 换成假客户端（返回 `response`，并记录请求）。"""
    import httpx

    client = FakeHttpxClient(response)

    def factory(*args, **kwargs):
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
