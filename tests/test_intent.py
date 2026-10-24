"""test_intent.py -- `intent` 检索线索的参数归一化 / 提示词 / 缓存隔离。

`intent` 是 v2.1.0 新增的可选工具参数：主模型把「怀疑是谁 / 哪部作品 / 什么场景」
交给搜图模型，用于缩小检索范围。本文件只验证这条链路：
- `normalize_intent()` 的空白 / 类型 / 长度处理；
- vision user 提示词中的线索段落（空值不出现、非空出现、位置在输出要求之前）；
- 工具返回体回显、缓存键随 intent 变化、缓存不会把不同线索的结果混用。
"""

from __future__ import annotations

import asyncio
import json

from conftest import config_with_sources, custom_vision_source

import main as plugin_main
from image_io import to_data_uri
from prompts import build_user_prompt
from search_core import MAX_INTENT_CHARS, normalize_intent

HIT_ANSWER = "VERDICT: HIT\n主体：初音未来\n出处：VOCALOID"


def _call(plugin, **kwargs) -> dict:
    raw = asyncio.run(plugin.image_search(event=None, **kwargs))
    assert isinstance(raw, str)
    return json.loads(raw)


def _patch_hit(monkeypatch, seen: list[dict]):
    async def fake_call(endpoint, **kwargs):
        seen.append(kwargs)
        return HIT_ANSWER

    monkeypatch.setattr(plugin_main.provider_reader, "call_vision_source", fake_call)


class TestNormalizeIntent:
    def test_trims_and_coerces(self):
        assert normalize_intent("  初音未来  ", []) == "初音未来"
        assert normalize_intent(None, []) == ""
        assert normalize_intent(12345, []) == "12345"

    def test_truncates_and_notes(self):
        notes: list[str] = []
        text = normalize_intent("线" * (MAX_INTENT_CHARS + 10), notes)
        assert len(text) == MAX_INTENT_CHARS
        assert notes == ["intent_truncated"]

    def test_short_intent_has_no_note(self):
        notes: list[str] = []
        assert normalize_intent("像初音未来", notes) == "像初音未来"
        assert notes == []


class TestPrompt:
    def test_intent_section_appears_before_output_requirements(self):
        prompt = build_user_prompt("这是谁", "看起来像初音未来")
        assert "用户问题：这是谁" in prompt
        assert "补充线索" in prompt and "看起来像初音未来" in prompt
        assert prompt.index("补充线索") < prompt.index("输出要求")

    def test_empty_intent_adds_no_section(self):
        prompt = build_user_prompt("这是谁", "")
        assert "补充线索" not in prompt
        assert "用户问题：这是谁" in prompt

    def test_intent_with_braces_is_literal(self):
        prompt = build_user_prompt("这是谁", "可能是 {角色}")
        assert "可能是 {角色}" in prompt

    def test_empty_question_still_uses_default_intent(self):
        prompt = build_user_prompt("", "场景在杭州")
        assert "未提供" in prompt
        assert "场景在杭州" in prompt


class TestToolIntent:
    def test_intent_reaches_vision_prompt_and_payload(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        seen: list[dict] = []
        _patch_hit(monkeypatch, seen)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin,
            image_refs=[to_data_uri(tiny_png_bytes, "png")],
            question="这是谁",
            intent="看起来像初音未来",
        )
        assert payload["status"] == "ok"
        assert payload["intent"] == "看起来像初音未来"
        assert payload["images"][0]["ok"] is True
        assert seen and "看起来像初音未来" in seen[0]["user_prompt"]
        assert "这是谁" in seen[0]["user_prompt"]

    def test_empty_intent_keeps_prompt_clean(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        seen: list[dict] = []
        _patch_hit(monkeypatch, seen)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")], question="这是谁"
        )
        assert payload["intent"] == ""
        assert "补充线索" not in seen[0]["user_prompt"]

    def test_truncation_is_visible_in_notes(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        seen: list[dict] = []
        _patch_hit(monkeypatch, seen)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin,
            image_refs=[to_data_uri(tiny_png_bytes, "png")],
            intent="线" * (MAX_INTENT_CHARS + 5),
        )
        assert payload["intent"] == "线" * MAX_INTENT_CHARS
        assert "intent_truncated" in payload["notes"]

    def test_different_intent_bypasses_cache(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        seen: list[dict] = []
        _patch_hit(monkeypatch, seen)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        ref = to_data_uri(tiny_png_bytes, "png")
        first = _call(plugin, image_refs=[ref], question="这是谁", intent="初音未来")
        second = _call(plugin, image_refs=[ref], question="这是谁", intent="明日方舟")
        third = _call(plugin, image_refs=[ref], question="这是谁", intent="初音未来")
        assert first["cached"] is False
        assert second["cached"] is False
        assert third["cached"] is True
        assert len(seen) == 2  # 两个不同 intent 各请求一次；第三次命中缓存
