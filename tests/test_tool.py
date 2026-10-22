"""test_tool.py -- image_search 工具契约单测（PRD §1.1~§1.6 / §3.1 / §4.7 / §5）。

覆盖点：
- 工具 docstring 可被 `docstring_parser` 解析出三个参数名与类型注释（否则
  `register_llm_tool` 会抛「参数缺少类型注释」）；AstrBot 工具注册表里只有
  `image_search` 一个本插件工具，名字不含 analyze 语义；
- 空源池 → 友好引导 JSON（指向插件配置 search_sources），不抛异常；
- 未传图 → `status="no_refs"`；
- A 类 vision 源命中 → `status="ok"`、返回体逐字段齐备、摘要含来源归属；
- 源失败 / 明确未命中 → 自动尝试下一源，`attempts` 逐源记录（含 id 与人话原因）；
- 全部源失败 → `status="failed"` + 逐源失败摘要；
- provider 引用形态不下发手填 model；
- `on_llm_request` 注入图片路径与 image_search 工具提示（受 tool_hint_enabled 控制、幂等）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import docstring_parser
import pytest
from conftest import (
    FakeContext,
    FakeHttpxResponse,
    config_with_sources,
    custom_vision_source,
    doubao_provider,
    engine_source,
    install_fake_httpx,
    vision_source,
)

import main as plugin_main
from image_io import to_data_uri
from provider_reader import SourceError
from search_core import build_attempt, build_image_result

TIMEOUT_LIMIT = 10

HIT_ANSWER = "VERDICT: HIT\n主体：初音未来\n出处：VOCALOID / Crypton Future Media"


class TestToolDocstring:
    def test_docstring_parses_three_typed_params(self):
        func = plugin_main.ImageSearchPlugin.image_search
        parsed = docstring_parser.parse(func.__doc__ or "")
        params = {param.arg_name: param.type_name for param in parsed.params}
        assert params == {
            "image_refs": "list[string]",
            "question": "string",
            "search_type": "string",
        }
        assert parsed.description
        assert "联网搜图" in parsed.description

    def test_only_one_llm_tool_named_image_search(self):
        source = inspect.getsource(plugin_main)
        assert re.findall(r'filter\.llm_tool\(name="([^"]+)"\)', source) == [
            "image_search"
        ]
        assert "image_analyze" not in source

    def test_registered_in_astrbot_tool_registry(self):
        from astrbot.core.provider.register import llm_tools

        tool = next(
            (item for item in llm_tools.func_list if item.name == "image_search"), None
        )
        assert tool is not None, "image_search 未注册进 AstrBot 工具表"
        schema = tool.parameters
        assert schema["type"] == "object"
        properties = schema["properties"]
        # 恰好三个参数：AstrBot 注入的 event 不得出现在工具 schema 里
        assert set(properties) == {"image_refs", "question", "search_type"}
        assert "event" not in properties
        assert properties["image_refs"]["type"] == "array"
        assert properties["image_refs"]["items"] == {"type": "string"}
        assert properties["question"]["type"] == "string"
        assert properties["search_type"]["type"] == "string"
        assert "analyze" not in tool.name

    def test_tool_signature_defaults(self):
        signature = inspect.signature(plugin_main.ImageSearchPlugin.image_search)
        assert signature.parameters["question"].default == ""
        assert signature.parameters["search_type"].default == "auto"
        assert signature.parameters["image_refs"].default is None


def _call(plugin, **kwargs) -> dict:
    """同步调用工具并解析返回 JSON 字符串。"""
    raw = asyncio.run(plugin.image_search(event=None, **kwargs))
    assert isinstance(raw, str)
    return json.loads(raw)


class TestEmptyPool:
    def test_empty_pool_is_friendly_json(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources()))
        payload = _call(
            plugin, image_refs=["data:image/png;base64,AAAA"], question="这是谁"
        )
        assert payload["status"] == "empty_pool"
        assert payload["ok"] is False
        assert payload["tool"] == "image_search"
        assert "search_sources" in payload["hint"]
        assert payload["images"] == []
        assert payload["summary"]

    def test_empty_pool_does_not_call_any_source(self, make_plugin, monkeypatch):
        calls = []

        async def fake_call(*args, **kwargs):
            calls.append(kwargs)
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(config_with_sources()))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "empty_pool"
        assert calls == []

    def test_all_invalid_sources_is_empty_pool(self, make_plugin):
        plugin = asyncio.run(
            make_plugin(config_with_sources(engine_source(engine="google_lens")))
        )
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "empty_pool"

    def test_engine_only_without_image_url_is_empty_pool_with_image_url_hint(
        self, make_plugin
    ):
        """F13：候选全部因 no_public_url 被 skipped = 没有可尝试的源 → empty_pool。"""
        from search_core import NO_ATTEMPTS_ERROR

        plugin = asyncio.run(
            make_plugin(config_with_sources(engine_source(), search={"max_rounds": 2}))
        )
        payload = _call(
            plugin, image_refs=["data:image/png;base64,AAAA"], search_type="engine"
        )
        assert payload["status"] == "empty_pool"
        assert payload["ok"] is False
        assert payload["images"][0]["error"] == NO_ATTEMPTS_ERROR
        assert "没有可尝试" in payload["summary"]
        assert payload["images"][0]["attempts"][0]["error"] == "no_public_url"
        assert "image_url" in payload["hint"]
        assert "skipped:saucenao-1:no_public_url" in payload["notes"]


class TestNoRefs:
    def test_missing_refs(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        payload = _call(plugin, image_refs=None)
        assert payload["status"] == "no_refs"
        assert payload["ok"] is False
        assert "图片" in payload["summary"]

    def test_non_list_refs(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        payload = _call(plugin, image_refs="/tmp/a.jpg")
        assert payload["status"] == "no_refs"


class TestVisionHit:
    def test_custom_source_hit(self, make_plugin, monkeypatch, tiny_png_bytes):
        seen: list[dict] = []

        async def fake_call(endpoint, **kwargs):
            seen.append({"endpoint": endpoint, **kwargs})
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")], question="这是谁"
        )
        assert payload["status"] == "ok"
        assert payload["ok"] is True
        assert payload["question"] == "这是谁"
        image = payload["images"][0]
        assert image["ok"] is True
        assert image["transport"] == "data_uri"
        assert image["source"] == {
            "kind": "vision",
            "name": "自建联网视觉端点",
            "id": "custom-net",
            "engine": "",
        }
        assert image["match"] is None
        assert image["answer"].startswith("主体：初音未来")
        assert image["attempts"][0]["outcome"] == "hit"
        assert image["attempts"][0]["counted_round"] is False
        assert image["rounds_used"] == 0
        assert "视觉联网模型 自建联网视觉端点" in payload["summary"]
        assert seen and seen[0]["data_uri"].startswith("data:image/png;base64,")

    def test_provider_source_uses_provider_model(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        seen: list[dict] = []

        async def fake_call(endpoint, **kwargs):
            seen.append(endpoint)
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        raw = config_with_sources(vision_source(model="hand-written-model"))
        plugin = asyncio.run(
            make_plugin(raw, FakeContext(providers={"豆包/doubao": doubao_provider()}))
        )
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert payload["status"] == "ok"
        assert seen[0]["model"] == "doubao-seed-1.6-vision"

    def test_cache_hit_skips_network(self, make_plugin, monkeypatch, tiny_png_bytes):
        calls = []

        async def fake_call(endpoint, **kwargs):
            calls.append(1)
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        ref = to_data_uri(tiny_png_bytes, "png")
        first = _call(plugin, image_refs=[ref], question="这是谁")
        second = _call(plugin, image_refs=[ref], question="这是谁")
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["images"][0]["cached"] is True
        assert second["images"][0]["ok"] is True
        assert len(calls) == 1

    def test_invalid_search_type_falls_back_with_note(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin,
            image_refs=["data:image/png;base64,AAAA"],
            search_type="banana",
        )
        assert payload["search_type"] == "auto"
        assert "invalid_search_type:banana" in payload["notes"]


class TestFailover:
    def _two_custom_sources(self):
        first = custom_vision_source(
            id="a-net", name="A 源", api_base="https://a.invalid/v1"
        )
        second = custom_vision_source(
            id="b-net", name="B 源", api_base="https://b.invalid/v1"
        )
        return config_with_sources(first, second)

    def test_source_error_switches_to_next_source(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            if "a.invalid" in endpoint["api_base"]:
                raise SourceError("HTTP 500: upstream broken")
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(self._two_custom_sources()))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "ok"
        attempts = payload["images"][0]["attempts"]
        assert [a["outcome"] for a in attempts] == ["error", "hit"]
        assert attempts[0]["id"] == "a-net"
        assert "HTTP 500" in attempts[0]["error"]
        assert attempts[0]["counted_round"] is False
        assert payload["images"][0]["source"]["id"] == "b-net"

    def test_miss_counts_round_and_switches(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            if "a.invalid" in endpoint["api_base"]:
                return "VERDICT: MISS\n无法确定"
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(self._two_custom_sources()))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        attempts = payload["images"][0]["attempts"]
        assert [a["outcome"] for a in attempts] == ["miss", "hit"]
        assert attempts[0]["counted_round"] is True
        assert payload["images"][0]["rounds_used"] == 1

    def test_max_rounds_zero_stops_after_first_miss(self, make_plugin, monkeypatch):
        calls = []

        async def fake_call(endpoint, **kwargs):
            calls.append(endpoint["api_base"])
            return "VERDICT: MISS\n无法确定"

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        config = self._two_custom_sources()
        config["search"] = {"max_rounds": 0}
        plugin = asyncio.run(make_plugin(config))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "failed"
        assert len(calls) == 1
        assert payload["images"][0]["attempts"][0]["outcome"] == "miss"
        assert "没有查到这张图的来源" in payload["summary"]

    def test_all_sources_fail_reports_each_source(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            raise SourceError(f"boom {endpoint['api_base']}")

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(self._two_custom_sources()))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "failed"
        assert payload["ok"] is False
        image = payload["images"][0]
        assert {a["id"] for a in image["attempts"]} == {"a-net", "b-net"}
        assert all(a["outcome"] == "error" and a["error"] for a in image["attempts"])
        assert image["error"]

    def test_timeout_is_isolated_per_source(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        import asyncio as aio

        async def slow_call(endpoint, **kwargs):
            if "a.invalid" in endpoint["api_base"]:
                await aio.sleep(5)
                return HIT_ANSWER
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", slow_call
        )
        config = self._two_custom_sources()
        config["search"] = {"source_timeout_sec": 5}
        plugin = asyncio.run(make_plugin(config))
        # 源级外层硬闸 = timeout + 5s；把源超时压到 0.2s 便于快速触发
        plugin._cfg["search"]["source_timeout_sec"] = 5
        for source in plugin._cfg["search_sources"].values():
            source["timeout_sec"] = 1
        plugin_main._PER_SOURCE_GRACE_SEC = 0.2
        try:
            payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        finally:
            plugin_main._PER_SOURCE_GRACE_SEC = 5.0
        attempts = payload["images"][0]["attempts"]
        assert attempts[0]["outcome"] == "timeout"
        assert attempts[0]["counted_round"] is False
        assert payload["status"] == "ok"

    def test_engine_error_switches_to_vision(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        async def fake_run_engine(engine, image, cfg, timeout_sec):
            assert (
                cfg["_engine_opts"]["saucenao"]["base_url"] == "https://relay.invalid"
            )
            return {
                "engine": engine,
                "ok": False,
                "match": None,
                "error": "上游 502",
                "challenge": False,
                "miss": False,
            }

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        config = config_with_sources(
            engine_source(base_url="https://relay.invalid", api_key="k"),
            custom_vision_source(),
        )
        plugin = asyncio.run(make_plugin(config))
        image_uri = "data:image/png;base64,AAAA"
        payload = _call(plugin, image_refs=[image_uri])
        attempts = payload["images"][0]["attempts"]
        # 图床关闭 → engine 源被跳过，vision 源正常命中
        assert attempts[0]["outcome"] == "skipped"
        assert attempts[0]["error"] == "no_public_url"
        assert payload["status"] == "ok"


class TestEnginePath:
    def test_engine_hit_via_run_engine(self, make_plugin, monkeypatch, tiny_png_bytes):
        calls = []

        async def fake_run_engine(engine, image, cfg, timeout_sec):
            calls.append((engine, image, timeout_sec))
            return {
                "engine": engine,
                "ok": True,
                "match": {
                    "similarity": 92.41,
                    "title": "作品名",
                    "author": "作者",
                    "links": ["https://x.invalid/1"],
                    "extra": "",
                },
                "error": "",
                "challenge": False,
                "miss": False,
            }

        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        config = config_with_sources(engine_source())
        plugin = asyncio.run(make_plugin(config))
        # 模拟图床就绪（避免真实起服务/联网）
        plugin._public_base = "http://127.0.0.1:8123"
        plugin._image_dir = "/tmp"
        monkeypatch.setattr(
            plugin_main.image_io,
            "publish_image",
            lambda data, ext, directory: "d" * 32 + ".png",
        )
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert payload["status"] == "ok"
        image = payload["images"][0]
        assert image["transport"] == "url"
        assert image["source"]["engine"] == "saucenao"
        assert image["match"]["similarity"] == 92.41
        assert image["answer"] == ""
        assert calls and calls[0][1]["url"] == f"http://127.0.0.1:8123/{'d' * 32}.png"

    def test_engine_challenge_is_recorded(self, make_plugin, monkeypatch):
        async def fake_run_engine(engine, image, cfg, timeout_sec):
            return {
                "engine": engine,
                "ok": False,
                "match": None,
                "error": "目标站点返回 Cloudflare 人机校验页，未取得结果。",
                "challenge": True,
                "miss": False,
            }

        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        plugin = asyncio.run(make_plugin(config_with_sources(engine_source())))
        plugin._public_base = "http://127.0.0.1:8123"
        plugin._image_dir = "/tmp"
        monkeypatch.setattr(
            plugin_main.image_io,
            "publish_image",
            lambda data, ext, directory: "e" * 32 + ".png",
        )
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "failed"
        image = payload["images"][0]
        assert image["attempts"][0]["outcome"] == "challenge"
        assert image["attempts"][0]["challenge"] is True
        assert image["attempts"][0]["counted_round"] is False
        assert "Cloudflare" in payload["hint"]


class _FakeRequest:
    def __init__(self, image_urls):
        self.image_urls = list(image_urls)
        self.extra_user_content_parts: list = []


class TestToolHintHook:
    def test_injects_paths_and_hint(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        req = _FakeRequest(["/root/AstrBot/data/temp/a.jpg"])
        asyncio.run(plugin.on_llm_request(None, req))
        texts = [part["text"] for part in req.extra_user_content_parts]
        assert any(
            "[Image Attachment: path /root/AstrBot/data/temp/a.jpg]" in t for t in texts
        )
        hint = next(t for t in texts if "image-search-hint" in t)
        assert "image_search" in hint
        assert "联网搜图" in hint

    def test_idempotent_second_call(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        req = _FakeRequest(["/tmp/a.jpg"])
        asyncio.run(plugin.on_llm_request(None, req))
        first_parts = list(req.extra_user_content_parts)
        asyncio.run(plugin.on_llm_request(None, req))
        assert req.extra_user_content_parts == first_parts

    def test_disabled_by_config(self, make_plugin):
        raw = config_with_sources(vision_source())
        raw["tool_hint_enabled"] = False
        plugin = asyncio.run(make_plugin(raw))
        req = _FakeRequest(["/tmp/a.jpg"])
        asyncio.run(plugin.on_llm_request(None, req))
        assert req.extra_user_content_parts == []

    def test_hook_swallows_errors(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))

        class _Broken:
            image_urls = ["/tmp/a.jpg"]

            @property
            def extra_user_content_parts(self):
                raise RuntimeError("boom")

        asyncio.run(plugin.on_llm_request(None, _Broken()))  # 不应抛异常


class TestInternalSafety:
    def test_internal_exception_becomes_failed_json(self, make_plugin, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("exploded")

        monkeypatch.setattr(plugin_main.ImageSearchPlugin, "_run_search", boom)
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "failed"
        assert payload["ok"] is False
        assert "内部错误类型：RuntimeError" in payload["hint"]

    def test_helper_builders_keep_prd_keys(self):
        source = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        attempt = build_attempt(source, "miss", elapsed_ms=12)
        assert set(attempt) == {
            "kind",
            "name",
            "id",
            "engine",
            "outcome",
            "counted_round",
            "error",
            "challenge",
            "elapsed_ms",
        }
        assert attempt["counted_round"] is True
        assert build_attempt(source, "timeout")["counted_round"] is False
        image = build_image_result(0, "a")
        assert image["error"] == ""
        assert image["merged_from"] == []

    def test_api_key_never_leaks_into_payload(self, make_plugin, monkeypatch):
        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        config = config_with_sources(
            custom_vision_source(api_key="sk-super-secret-9999")
        )
        plugin = asyncio.run(make_plugin(config))
        raw = asyncio.run(
            plugin.image_search(event=None, image_refs=["data:image/png;base64,AAAA"])
        )
        assert "sk-super-secret-9999" not in raw


class TestImageUrlIntegration:
    def test_image_url_enabled_publishes_and_terminate_stops_server(
        self, make_plugin, monkeypatch, tiny_png_bytes, tmp_path
    ):
        """`image_url.enabled=true` → 内置只读图床真实启动、图片落盘、URL 传输可用。"""
        from image_io import is_imgserv_filename
        from image_server import find_free_port

        port, _ = find_free_port(19234)
        config = config_with_sources(engine_source(), search={"cache_ttl_sec": 0})
        config["image_url"] = {
            "enabled": True,
            "dir": str(tmp_path / "imgserv"),
            "port": port,
            "public_base_url": "http://127.0.0.1:9",
        }
        captured: dict = {}

        async def fake_run_engine(engine, image, cfg, timeout_sec):
            captured["url"] = image["url"]
            return {
                "engine": engine,
                "ok": False,
                "match": None,
                "error": "上游 502",
                "challenge": False,
                "miss": False,
            }

        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        plugin = asyncio.run(make_plugin(config))
        assert plugin._image_server is not None
        assert plugin._image_server.running is True
        # 配置了公网前缀 → 不做公网 IP 外探
        assert plugin._public_base == "http://127.0.0.1:9"

        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        image = payload["images"][0]
        assert image["transport"] == "url"
        assert image["attempts"][0]["outcome"] == "error"
        name = captured["url"].rsplit("/", 1)[-1]
        assert captured["url"] == f"http://127.0.0.1:9/{name}"
        assert is_imgserv_filename(name) is True
        assert (tmp_path / "imgserv" / name).read_bytes() == tiny_png_bytes

        asyncio.run(plugin.terminate())
        assert plugin._image_server is None


class TestMultiSourceMerge:
    def test_engine_and_vision_hits_are_merged(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        """PRD §3.4：命中后继续尝试后续源并合并去重（engine 结果 + vision 回答）。"""

        async def fake_run_engine(engine, image, cfg, timeout_sec):
            return {
                "engine": engine,
                "ok": True,
                "match": {
                    "similarity": 92.41,
                    "title": "作品名",
                    "author": "作者",
                    "links": ["https://x.invalid/1"],
                    "extra": "",
                },
                "error": "",
                "challenge": False,
                "miss": False,
            }

        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        config = config_with_sources(
            engine_source(),
            custom_vision_source(id="v-one", name="源一"),
            custom_vision_source(
                id="v-two", name="源二", api_base="https://v2.invalid/v1"
            ),
        )
        plugin = asyncio.run(make_plugin(config))
        plugin._public_base = "http://127.0.0.1:8123"
        plugin._image_dir = "/tmp"
        monkeypatch.setattr(
            plugin_main.image_io,
            "publish_image",
            lambda data, ext, directory: "f" * 32 + ".png",
        )
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        image = payload["images"][0]
        assert payload["status"] == "ok"
        # source 取第一个命中源（engine）
        assert image["source"]["kind"] == "engine"
        assert image["source"]["engine"] == "saucenao"
        # 两个 vision 源回答完全相同 → 按 answer sha1 去重，只保留一条
        assert [item["id"] for item in image["merged_from"]] == ["saucenao-1", "v-one"]
        assert image["match"]["title"] == "作品名"
        assert image["answer"].startswith("主体：初音未来")
        assert [a["outcome"] for a in image["attempts"]] == ["hit", "hit", "hit"]
        summary = payload["summary"]
        assert "图搜图引擎 SauceNAO" in summary
        assert "视觉联网模型 源一" in summary
        assert "源二" not in summary


class TestRepairRound2Secrets:
    def test_error_text_masks_key_headers_and_query(
        self, make_plugin, monkeypatch, caplog, tiny_png_bytes
    ):
        """v2.0.0 repair：endpoint key + extra_headers 全部 value + URL query 值统一脱敏。

        端点回显这些明文时，工具 JSON 与日志都不得出现明文。
        """
        import logging

        key = "sk-endpoint-secret-0001"
        header_secret = "relay-header-secret-0002"
        query_secret = "relay-query-secret-0003"
        body = f"unauthorized: key={key} header={header_secret} query={query_secret}"
        install_fake_httpx(monkeypatch, FakeHttpxResponse(401, text=body))
        config = config_with_sources(
            custom_vision_source(
                api_base=f"https://relay.invalid/v1?api-key={query_secret}",
                api_key=key,
                extra_headers={"X-Relay-Token": header_secret},
            )
        )
        plugin = asyncio.run(make_plugin(config))
        with caplog.at_level(logging.WARNING):
            raw = asyncio.run(
                plugin.image_search(
                    event=None, image_refs=[to_data_uri(tiny_png_bytes, "png")]
                )
            )
        payload = json.loads(raw)
        for secret in (key, header_secret, query_secret):
            assert secret not in raw, f"{secret} 泄漏进工具 JSON"
            assert secret not in caplog.text, f"{secret} 泄漏进日志"
        attempt_error = payload["images"][0]["attempts"][0]["error"]
        assert attempt_error.startswith("HTTP 401:")
        assert "sk-e***" in attempt_error or "***" in attempt_error

    def test_masked_text_is_reused_by_log_and_attempt(
        self, make_plugin, monkeypatch, caplog, tiny_png_bytes
    ):
        """main 的日志与 attempts 必须复用同一份已脱敏文本（不再重新拼接）。"""
        import logging

        key = "sk-reuse-secret-7777"
        install_fake_httpx(
            monkeypatch, FakeHttpxResponse(500, text=f"upstream died with {key}")
        )
        config = config_with_sources(custom_vision_source(api_key=key))
        plugin = asyncio.run(make_plugin(config))
        with caplog.at_level(logging.WARNING):
            payload = json.loads(
                asyncio.run(
                    plugin.image_search(
                        event=None, image_refs=[to_data_uri(tiny_png_bytes, "png")]
                    )
                )
            )
        attempt_error = payload["images"][0]["attempts"][0]["error"]
        masked_line = next(
            line for line in caplog.text.splitlines() if "failed" in line
        )
        assert key not in masked_line
        assert f"HTTP 500: upstream died with sk-r***" == attempt_error
        assert "sk-r***" in masked_line


class TestRepairRound2Visibility:
    def test_provider_model_ignored_is_visible_in_log_and_notes(
        self, make_plugin, monkeypatch, caplog, tiny_png_bytes
    ):
        """v2.0.0 repair：provider 形态忽略手填 model 必须在启动日志与 notes 明示。"""
        import logging

        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        config = config_with_sources(vision_source(model="hand-written-model"))
        with caplog.at_level(logging.WARNING):
            plugin = asyncio.run(
                make_plugin(
                    config, FakeContext(providers={"豆包/doubao": doubao_provider()})
                )
            )
        assert "hand-written-model" in caplog.text
        assert "ignored" in caplog.text
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert "provider_model_ignored:doubao-search" in payload["notes"]
        assert payload["status"] == "ok"

    def test_custom_source_has_no_model_ignored_note(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        async def fake_call(endpoint, **kwargs):
            return HIT_ANSWER

        monkeypatch.setattr(
            plugin_main.provider_reader, "call_vision_source", fake_call
        )
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert not [
            note
            for note in payload["notes"]
            if note.startswith("provider_model_ignored")
        ]


class TestRepairRound2EngineSize:
    def test_engine_source_is_not_skipped_for_source_level_size(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        """engine 模板没有 size/pixels 字段 → 不因源级上限被 skipped。"""

        async def fake_run_engine(engine, image, cfg, timeout_sec):
            return {
                "engine": engine,
                "ok": True,
                "match": {
                    "similarity": 92.41,
                    "title": "作品名",
                    "author": "作者",
                    "links": [],
                    "extra": "",
                },
                "error": "",
                "challenge": False,
                "miss": False,
            }

        import astrbot_plugin_image_search.search_engines as search_engines

        monkeypatch.setattr(search_engines, "run_engine", fake_run_engine)
        plugin = asyncio.run(make_plugin(config_with_sources(engine_source())))
        plugin._public_base = "http://127.0.0.1:8123"
        plugin._image_dir = "/tmp"
        monkeypatch.setattr(
            plugin_main.image_io,
            "publish_image",
            lambda data, ext, directory: "b" * 32 + ".png",
        )
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        attempts = payload["images"][0]["attempts"]
        assert payload["status"] == "ok"
        assert all(a["error"] != "image_too_large_for_source" for a in attempts)
        assert [a["outcome"] for a in attempts] == ["hit"]


class TestRepairRound2Hook:
    def test_path_in_prompt_adds_no_attachment_part(self, make_plugin):
        """v2.0.0 repair：req.prompt 已含路径时不再追加 [Image Attachment: ...]。"""
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        req = _FakeRequest(["/root/AstrBot/data/temp/a.jpg"])
        req.prompt = "看看这张 /root/AstrBot/data/temp/a.jpg 是谁"
        asyncio.run(plugin.on_llm_request(None, req))
        # 注意：工具提示正文里也含 "[Image Attachment: path ...]" 字样，
        # 因此这里按「以该前缀开头」判定真正的附件块
        attachment_parts = [
            part
            for part in req.extra_user_content_parts
            if part["text"].startswith("[Image Attachment:")
        ]
        assert attachment_parts == []
        hint_parts = [
            part
            for part in req.extra_user_content_parts
            if "image-search-hint" in part["text"]
        ]
        assert len(hint_parts) == 1

    def test_parts_do_not_grow_when_prompt_has_path_and_hint_present(self, make_plugin):
        from main import _TOOL_HINT_TEXT

        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        req = _FakeRequest(["/tmp/a.jpg"])
        req.prompt = "[Image Attachment: path /tmp/a.jpg] 这是谁"
        req.extra_user_content_parts = [{"type": "text", "text": _TOOL_HINT_TEXT}]
        before = json.dumps(req.extra_user_content_parts, ensure_ascii=False)
        asyncio.run(plugin.on_llm_request(None, req))
        assert len(req.extra_user_content_parts) == 1
        assert json.dumps(req.extra_user_content_parts, ensure_ascii=False) == before

    def test_path_only_in_parts_still_deduped(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(vision_source())))
        req = _FakeRequest(["/tmp/a.jpg"])
        req.extra_user_content_parts = [
            {"type": "text", "text": "[Image Attachment: path /tmp/a.jpg]"}
        ]
        asyncio.run(plugin.on_llm_request(None, req))
        texts = [part["text"] for part in req.extra_user_content_parts]
        assert sum(text.startswith("[Image Attachment:") for text in texts) == 1
        assert len(texts) == 2  # 原附件块 + 新注入的工具提示


class _FakeEngineResponse:
    """最小 httpx.Response 替身（engine 只用 text / status_code / headers）。"""

    def __init__(self, status_code: int = 200, text: str = "", headers=None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {"content-type": "application/json"})


def _install_fake_engine_client(monkeypatch, *, response=None, exc=None) -> None:
    """把 search_engines 用的 httpx.AsyncClient 换成假客户端（记录调用）。"""
    import astrbot_plugin_image_search.search_engines as search_engines

    class _Client:
        last: dict = {}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, params=None, **kwargs):
            type(self).last = {"url": url, "params": params}
            if exc is not None:
                raise exc
            return response

        async def post(self, url, data=None, **kwargs):
            type(self).last = {"url": url, "params": data}
            if exc is not None:
                raise exc
            return response

    monkeypatch.setattr(search_engines.httpx, "AsyncClient", _Client)


def _engine_ready_plugin(plugin, monkeypatch):
    """让 engine 源以为图床已就绪（避免真实起服务/联网）。"""
    plugin._public_base = "http://127.0.0.1:8123"
    plugin._image_dir = "/tmp"
    monkeypatch.setattr(
        plugin_main.image_io,
        "publish_image",
        lambda data, ext, directory: "c" * 32 + ".png",
    )
    return plugin


class TestEngineLayerSanitization:
    """F11：engine 层 5 个错误出口 + 解析错误都必须脱敏（与 vision 层共用实现）。"""

    def test_engine_4xx_body_echoing_api_key_is_masked(
        self, make_plugin, monkeypatch, caplog, tiny_png_bytes
    ):
        import logging

        key = "sk-engine-secret-0001"
        body = json.dumps(
            {"error": f"invalid api_key {key} for /search.php?api_key={key}&url=x"}
        )
        _install_fake_engine_client(
            monkeypatch, response=_FakeEngineResponse(403, text=body)
        )
        plugin = asyncio.run(
            make_plugin(config_with_sources(engine_source(api_key=key)))
        )
        _engine_ready_plugin(plugin, monkeypatch)
        with caplog.at_level(logging.WARNING):
            raw = asyncio.run(
                plugin.image_search(
                    event=None, image_refs=[to_data_uri(tiny_png_bytes, "png")]
                )
            )
        payload = json.loads(raw)
        assert key not in raw, "工具 JSON 泄漏了 engine api_key"
        assert key not in caplog.text, "日志泄漏了 engine api_key"
        error = payload["images"][0]["attempts"][0]["error"]
        assert "返回错误" in error
        assert "sk-e***" in error
        assert payload["status"] == "failed"

    def test_engine_exception_echoing_url_and_key_is_masked(
        self, make_plugin, monkeypatch, caplog, tiny_png_bytes
    ):
        import httpx
        import logging

        key = "sk-engine-secret-0002"
        base_secret = "relay-base-secret-0003"
        boom = httpx.ConnectError(
            f"failed to connect to https://relay.invalid/search.php"
            f"?api_key={key}&token={base_secret}"
        )
        _install_fake_engine_client(monkeypatch, exc=boom)
        config = config_with_sources(
            engine_source(
                api_key=key,
                base_url=f"https://relay.invalid/search.php?token={base_secret}",
            )
        )
        plugin = asyncio.run(make_plugin(config))
        _engine_ready_plugin(plugin, monkeypatch)
        with caplog.at_level(logging.WARNING):
            raw = asyncio.run(
                plugin.image_search(
                    event=None, image_refs=[to_data_uri(tiny_png_bytes, "png")]
                )
            )
        for secret in (key, base_secret):
            assert secret not in raw, f"工具 JSON 泄漏了 {secret}"
            assert secret not in caplog.text, f"日志泄漏了 {secret}"
        error = json.loads(raw)["images"][0]["attempts"][0]["error"]
        assert "网络请求失败" in error
        assert "***" in error

    def test_main_reverifies_engine_error_outside_adapter(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        """main._run_engine_source 入口再验一次：适配器返回未脱敏文本也会被兜住。"""
        import astrbot_plugin_image_search.search_engines as search_engines

        key = "sk-engine-secret-0004"

        async def leaky_run_engine(engine, image, cfg, timeout_sec):
            return {
                "engine": engine,
                "ok": False,
                "match": None,
                "error": f"上游拒绝：api_key={key}",
                "challenge": False,
                "miss": False,
            }

        monkeypatch.setattr(search_engines, "run_engine", leaky_run_engine)
        plugin = asyncio.run(
            make_plugin(config_with_sources(engine_source(api_key=key)))
        )
        _engine_ready_plugin(plugin, monkeypatch)
        raw = asyncio.run(
            plugin.image_search(
                event=None, image_refs=[to_data_uri(tiny_png_bytes, "png")]
            )
        )
        assert key not in raw
        assert "sk-e***" in json.loads(raw)["images"][0]["attempts"][0]["error"]


class TestRepairRound3Semantics:
    """F12 / F13：空池语义与图床引导条件。"""

    def test_mixed_skipped_and_vision_failure_is_failed_without_image_url_hint(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        async def boom(endpoint, **kwargs):
            raise SourceError("HTTP 500: upstream broken")

        monkeypatch.setattr(plugin_main.provider_reader, "call_vision_source", boom)
        config = config_with_sources(engine_source(), custom_vision_source())
        plugin = asyncio.run(make_plugin(config))
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert payload["status"] == "failed"
        assert "所有搜图源都失败了" in payload["summary"]
        # F12：auto 下有 vision 源真实失败 → 不得误报「开启 image_url.enabled」
        assert payload["hint"] == ""
        # F13：被跳过的源明细进 notes
        assert "skipped:saucenao-1:no_public_url" in payload["notes"]
        requested = [a["id"] for a in payload["images"][0]["attempts"]]
        assert requested == ["saucenao-1", "custom-net"]

    def test_all_invalid_pool_is_empty_pool_with_search_sources_hint(self, make_plugin):
        plugin = asyncio.run(
            make_plugin(config_with_sources(engine_source(engine="google_lens")))
        )
        payload = _call(plugin, image_refs=["data:image/png;base64,AAAA"])
        assert payload["status"] == "empty_pool"
        assert "search_sources" in payload["hint"]
        assert "image_url" not in payload["hint"]

    def test_engine_only_pool_gets_image_url_hint(self, make_plugin):
        """纯 engine 候选池（search_type=engine）+ 图床关闭 → 引导开启图床。"""
        plugin = asyncio.run(make_plugin(config_with_sources(engine_source())))
        payload = _call(
            plugin, image_refs=["data:image/png;base64,AAAA"], search_type="engine"
        )
        assert payload["status"] == "empty_pool"
        assert "image_url.enabled" in payload["hint"]

    def test_auto_with_vision_success_has_no_image_url_hint(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        async def hit(endpoint, **kwargs):
            return HIT_ANSWER

        monkeypatch.setattr(plugin_main.provider_reader, "call_vision_source", hit)
        config = config_with_sources(engine_source(), custom_vision_source())
        plugin = asyncio.run(make_plugin(config))
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        assert payload["status"] == "ok"
        assert payload["hint"] == ""
