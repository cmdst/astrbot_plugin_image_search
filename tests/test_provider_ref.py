"""test_provider_ref.py -- A 类源 provider 引用解析与 OpenAI 兼容调用单测（PRD §2.2 / §4.7）。

覆盖点：
- provider 引用解析：Provider 实例优先，ProviderManager 配置兜底；
- `provider_ref.split("/")[-1]` 二次查找（PRD §4.7）与 `provider_not_found` 人话错误；
- provider 形态忽略手填 model（model 唯一来源 = AstrBot 提供商）；
- vision 标记（modalities 含 image/vision）；
- 请求体为 OpenAI 兼容格式且含 `image_url` 内容块、只下发冻结键（无 None 字段）；
- HTTP 非 2xx → 人话错误且密钥脱敏；`content` 空则回退 `reasoning_content`。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import (
    FakeContext,
    FakeProvider,
    FakeProviderManager,
    custom_vision_source,
    doubao_provider,
    vision_source,
)

from image_io import to_data_uri
from prompts import SEARCH_SYSTEM_PROMPT, build_user_prompt
from provider_reader import (
    SourceError,
    build_chat_url,
    build_vision_payload,
    call_vision_source,
    extract_message_text,
    mask_api_key,
    resolve_source_endpoint,
)
from search_core import normalize_config

DATA_URI = "data:image/png;base64,iVBORw0KGgo="


def _source(raw: dict) -> dict:
    """归一化后的单个源（keyed dict 里取出）。"""
    return normalize_config({"search_sources": [raw]})["search_sources"][raw["id"]]


class TestVisionFlag:
    def test_vision_provider_flag(self):
        source = _source(vision_source())
        endpoint = resolve_source_endpoint(source, provider=doubao_provider())
        assert endpoint["vision"] is True

    def test_non_vision_provider_flag(self):
        source = _source(vision_source())
        provider = FakeProvider(
            {
                "id": "豆包/doubao",
                "api_base": "https://ark.example.invalid/v1",
                "key": "sk-x",
                "model": "text-only",
                "modalities": ["text"],
            }
        )
        assert resolve_source_endpoint(source, provider=provider)["vision"] is False


class TestEndpointResolution:
    def test_provider_instance_wins(self):
        source = _source(vision_source())
        endpoint = resolve_source_endpoint(source, provider=doubao_provider())
        assert endpoint == {
            "api_base": "https://ark.example.invalid/api/v3",
            "key": "sk-doubao-test-0001",
            "model": "doubao-seed-1.6-vision",
            "provider_id": "豆包/doubao",
            "vision": True,
            "source": "provider_ref",
        }

    def test_provider_manager_fallback_without_instance(self):
        manager = FakeProviderManager(
            [
                {
                    "id": "doubao",
                    "api_base": "https://ark.example.invalid/v1",
                    "key": "sk-manager-lookup",
                    "model": "doubao-lite",
                    "modalities": ["text", "image"],
                }
            ]
        )
        source = _source(vision_source(provider_ref="doubao"))
        endpoint = resolve_source_endpoint(source, provider_manager=manager)
        assert endpoint["key"] == "sk-manager-lookup"
        assert endpoint["model"] == "doubao-lite"

    def test_provider_config_getter_path(self):
        manager = FakeProviderManager(
            [
                {
                    "id": "a/b",
                    "api_base": "https://x.invalid/v1",
                    "key": "k",
                    "model": "m",
                }
            ]
        )
        source = _source(vision_source(provider_ref="a/b"))
        assert resolve_source_endpoint(source, provider_manager=manager)[
            "api_base"
        ] == ("https://x.invalid/v1")

    def test_provider_not_found(self):
        source = _source(vision_source(provider_ref="不存在的提供商/x"))
        with pytest.raises(SourceError) as excinfo:
            resolve_source_endpoint(source, provider_manager=FakeProviderManager([]))
        assert str(excinfo.value).startswith("provider_not_found: ")

    def test_provider_missing_api_base(self):
        provider = FakeProvider({"id": "x", "key": "k", "model": "m"})
        source = _source(vision_source())
        with pytest.raises(SourceError) as excinfo:
            resolve_source_endpoint(source, provider=provider)
        assert "provider_missing_api_base" in str(excinfo.value)

    def test_custom_form_endpoint(self):
        source = _source(custom_vision_source())
        endpoint = resolve_source_endpoint(source)
        assert endpoint["api_base"] == "https://relay.example.invalid/v1"
        assert endpoint["key"] == "sk-custom-test-0002"
        assert endpoint["model"] == "gemini-3.5-flash"
        assert endpoint["source"] == "custom"

    def test_provider_form_ignores_hand_written_model(self):
        source = _source(vision_source(model="hand-written-model"))
        assert source["model"] == ""
        endpoint = resolve_source_endpoint(source, provider=doubao_provider())
        assert endpoint["model"] == "doubao-seed-1.6-vision"

    def test_build_chat_url(self):
        assert (
            build_chat_url("https://x.invalid/v1")
            == "https://x.invalid/v1/chat/completions"
        )
        assert (
            build_chat_url("https://x.invalid/v1/chat/completions")
            == "https://x.invalid/v1/chat/completions"
        )

    def test_key_list_takes_first(self):
        provider = FakeProvider(
            {
                "id": "x",
                "api_base": "https://x.invalid/v1",
                "key": ["", "sk-second", "sk-third"],
                "model": "m",
            }
        )
        source = _source(vision_source(provider_ref="x"))
        assert resolve_source_endpoint(source, provider=provider)["key"] == "sk-second"


class TestProviderLookupFallback:
    def test_main_resolves_tail_id(self, make_plugin):
        provider = doubao_provider()
        context = FakeContext(providers={"doubao": provider})
        source = _source(vision_source(provider_ref="豆包/doubao"))

        async def _resolve():
            plugin = await make_plugin({"search_sources": [vision_source()]}, context)
            return plugin._resolve_provider(source)

        assert asyncio.run(_resolve()) is provider

    def test_main_returns_none_when_absent(self, make_plugin):
        source = _source(vision_source(provider_ref="nope/also-nope"))

        async def _resolve():
            plugin = await make_plugin(
                {"search_sources": [vision_source()]}, FakeContext()
            )
            return plugin._resolve_provider(source)

        assert asyncio.run(_resolve()) is None

    def test_custom_source_needs_no_provider(self, make_plugin):
        source = _source(custom_vision_source())

        async def _resolve():
            plugin = await make_plugin(
                {"search_sources": [custom_vision_source()]}, FakeContext()
            )
            return plugin._resolve_provider(source)

        assert asyncio.run(_resolve()) is None


class TestMasking:
    def test_mask_api_key(self):
        assert mask_api_key("sk-1234567890") == "sk-1***"
        assert mask_api_key("abc") == "***"
        assert mask_api_key("") == ""


class TestPayload:
    def test_openai_compatible_body_with_image_block(self, tiny_png_bytes):
        uri = to_data_uri(tiny_png_bytes, "png")
        body = build_vision_payload(
            "doubao-seed-1.6-vision",
            SEARCH_SYSTEM_PROMPT,
            build_user_prompt("这是谁"),
            uri,
        )
        assert set(body) == {"model", "messages", "stream"}
        assert body["stream"] is False
        assert body["messages"][0] == {
            "role": "system",
            "content": SEARCH_SYSTEM_PROMPT,
        }
        user = body["messages"][1]
        assert user["role"] == "user"
        assert user["content"][0]["type"] == "text"
        assert "VERDICT" in user["content"][0]["text"]
        assert user["content"][1] == {"type": "image_url", "image_url": {"url": uri}}
        # 不下发思考/采样参数（避免 None 字段触发端点 400）
        assert "reasoning" not in body
        assert "reasoning_effort" not in body
        assert "temperature" not in body
        assert all(value is not None for value in body.values())

    def test_empty_question_uses_default_intent(self):
        prompt = build_user_prompt("")
        assert "未提供" in prompt and "这是谁" in prompt


class TestExtractMessageText:
    def test_content(self):
        payload = {"choices": [{"message": {"content": " hello "}}]}
        assert extract_message_text(payload) == "hello"

    def test_reasoning_content_fallback(self):
        payload = {
            "choices": [{"message": {"content": None, "reasoning_content": " think "}}]
        }
        assert extract_message_text(payload) == "think"

    def test_bad_shape(self):
        assert extract_message_text({}) == ""
        assert extract_message_text({"choices": []}) == ""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """替换 httpx.AsyncClient：记录请求体和头，返回预置响应。"""

    last: dict = {}

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last = {"url": url, "json": json, "headers": headers}
        return self.response


def _install_fake_client(monkeypatch, response: _FakeResponse) -> None:
    client = _FakeAsyncClient(response)

    def factory(*args, **kwargs):
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class TestCallVisionSource:
    def test_success_returns_text(self, monkeypatch):
        _install_fake_client(
            monkeypatch,
            _FakeResponse(
                200,
                payload={
                    "choices": [{"message": {"content": "VERDICT: HIT\n初音未来"}}]
                },
            ),
        )
        endpoint = {
            "api_base": "https://x.invalid/v1",
            "key": "sk-test-1234567890",
            "model": "m",
        }
        text = asyncio.run(
            call_vision_source(
                endpoint,
                system_prompt=SEARCH_SYSTEM_PROMPT,
                user_prompt=build_user_prompt("这是谁"),
                data_uri=DATA_URI,
                timeout_sec=5,
                extra_headers={"api-key": "relay-token"},
            )
        )
        assert text.startswith("VERDICT: HIT")
        assert _FakeAsyncClient.last["url"] == "https://x.invalid/v1/chat/completions"
        assert (
            _FakeAsyncClient.last["headers"]["Authorization"]
            == "Bearer sk-test-1234567890"
        )
        assert _FakeAsyncClient.last["headers"]["api-key"] == "relay-token"
        assert (
            _FakeAsyncClient.last["json"]["messages"][1]["content"][1]["image_url"][
                "url"
            ]
            == DATA_URI
        )

    def test_http_error_masks_key(self, monkeypatch):
        _install_fake_client(
            monkeypatch, _FakeResponse(401, text="bad key sk-test-1234567890 rejected")
        )
        endpoint = {
            "api_base": "https://x.invalid/v1/chat/completions",
            "key": "sk-test-1234567890",
        }
        with pytest.raises(SourceError) as excinfo:
            asyncio.run(
                call_vision_source(
                    endpoint,
                    system_prompt="s",
                    user_prompt="u",
                    data_uri=DATA_URI,
                    timeout_sec=5,
                )
            )
        message = str(excinfo.value)
        assert message.startswith("HTTP 401:")
        assert "sk-test-1234567890" not in message
        assert "sk-t***" in message

    def test_network_error_is_source_error(self, monkeypatch):
        class _Boom:
            def __call__(self, *args, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, *args, **kwargs):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Boom())
        with pytest.raises(SourceError) as excinfo:
            asyncio.run(
                call_vision_source(
                    {"api_base": "https://x.invalid/v1", "key": "k"},
                    system_prompt="s",
                    user_prompt="u",
                    data_uri=DATA_URI,
                    timeout_sec=5,
                )
            )
        assert "网络请求失败" in str(excinfo.value)

    def test_non_json_response_is_source_error(self, monkeypatch):
        _install_fake_client(monkeypatch, _FakeResponse(200, payload=None))
        with pytest.raises(SourceError) as excinfo:
            asyncio.run(
                call_vision_source(
                    {"api_base": "https://x.invalid/v1", "key": "k"},
                    system_prompt="s",
                    user_prompt="u",
                    data_uri=DATA_URI,
                    timeout_sec=5,
                )
            )
        assert "JSON" in str(excinfo.value)
