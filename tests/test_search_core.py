"""test_search_core.py -- search_core 契约单测（PRD §1.2 / §2 / §3.4 / §3.5 / §5）。

覆盖点：
- `search_sources` 三种形态（list / dict / JSON 字符串）都归一为 keyed dict；
- 非法条目（无法识别模板 / vision 端点缺失 / engine 名非法 / desc 空）被剔出可用源
  并留 `valid=False` + 英文告警，不抛异常；
- id 去重与自动生成（PRD §2.4.1）；
- `merge_results` 去重 / 顺序 / 相似度取最高 / `merge_max_sources` 上限；
- 空池与未命中文案（PRD §5）；
- `merge_enabled` 等 search 键的默认值与越界回落；
- 结果缓存键公式与健康统计。
"""

from __future__ import annotations

import logging

import pytest
from conftest import (
    config_with_sources,
    custom_vision_source,
    engine_source,
    vision_source,
)

from search_core import (
    DEFAULT_GLOBAL,
    aggregate_status,
    DEFAULT_IMAGE_URL,
    DEFAULT_SEARCH,
    EMPTY_POOL_HINT,
    EMPTY_POOL_SUMMARY,
    HealthStats,
    ResultCache,
    build_image_result,
    build_results_payload,
    enabled_sources,
    merge_results,
    normalize_config,
    normalize_question,
    normalize_refs,
    normalize_search,
    normalize_search_type,
    normalize_sources,
    render_hint,
    render_summary,
    select_candidates,
    source_size_limits,
    source_timeout_sec,
    vision_verdict,
)


class TestDefaults:
    def test_search_defaults_match_prd(self):
        assert DEFAULT_SEARCH == {
            "max_rounds": 2,
            "source_timeout_sec": 30,
            "total_timeout_sec": 180,
            "merge_enabled": True,
            "merge_max_sources": 3,
            "cache_ttl_sec": 300,
            "health_stats": True,
            "max_size_mb": 20,
            "max_pixels": 16777216,
        }

    def test_image_url_defaults_match_prd(self):
        assert DEFAULT_IMAGE_URL == {
            "enabled": False,
            "dir": "",
            "port": 8123,
            "public_base_url": "",
        }

    def test_global_default_keys(self):
        assert set(DEFAULT_GLOBAL) == {
            "search_sources",
            "search",
            "image_url",
            "download_timeout_sec",
            "tool_hint_enabled",
            "log_level",
        }

    def test_out_of_range_falls_back_to_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            search = normalize_search(
                {
                    "max_rounds": 99,
                    "source_timeout_sec": 1,
                    "total_timeout_sec": "abc",
                    "merge_max_sources": 0,
                    "cache_ttl_sec": -5,
                    "max_size_mb": 200,
                    "max_pixels": 10,
                    "merge_enabled": "not-a-bool",
                }
            )
        assert search == DEFAULT_SEARCH
        assert "out-of-range int for key max_rounds" in caplog.text
        assert "invalid int for key total_timeout_sec" in caplog.text

    def test_valid_values_kept(self):
        search = normalize_search(
            {"max_rounds": 5, "merge_enabled": False, "merge_max_sources": 2}
        )
        assert search["max_rounds"] == 5
        assert search["merge_enabled"] is False
        assert search["merge_max_sources"] == 2
        assert search["total_timeout_sec"] == 180

    def test_config_idempotent(self):
        raw = config_with_sources(vision_source(), engine_source())
        once = normalize_config(raw)
        twice = normalize_config(once)
        assert once["search_sources"] == twice["search_sources"]
        assert once["search"] == twice["search"]
        assert once["_engine_opts"] == twice["_engine_opts"]
        assert list(once["search_sources"]) == list(twice["search_sources"])

    def test_log_level_invalid_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = normalize_config({"log_level": "verbose"})
        assert cfg["log_level"] == "info"
        assert "invalid log_level" in caplog.text

    def test_tool_hint_and_download_timeout(self):
        cfg = normalize_config({"tool_hint_enabled": False, "download_timeout_sec": 5})
        assert cfg["tool_hint_enabled"] is False
        assert cfg["download_timeout_sec"] == 5
        assert (
            normalize_config({"download_timeout_sec": 999})["download_timeout_sec"]
            == 20
        )


class TestSourceShapes:
    def test_list_shape(self):
        cfg = normalize_config(config_with_sources(engine_source(), vision_source()))
        assert isinstance(cfg["search_sources"], dict)
        assert list(cfg["search_sources"]) == ["saucenao-1", "doubao-search"]
        assert cfg["search_sources"]["saucenao-1"]["kind"] == "engine"
        assert cfg["search_sources"]["doubao-search"]["kind"] == "vision"

    def test_dict_shape(self):
        raw = {
            "search_sources": {
                "a": engine_source(id="a", name="A"),
                "b": vision_source(id="b", name="B"),
            }
        }
        cfg = normalize_config(raw)
        assert list(cfg["search_sources"]) == ["a", "b"]
        assert cfg["search_sources"]["a"]["__template_key"] == "engine"
        assert cfg["search_sources"]["b"]["__template_key"] == "vision"

    def test_json_string_shape(self):
        import json

        raw = {"search_sources": json.dumps([engine_source(), vision_source()])}
        cfg = normalize_config(raw)
        assert isinstance(cfg["search_sources"], dict)
        assert list(cfg["search_sources"]) == ["saucenao-1", "doubao-search"]

    def test_json_string_dict_shape(self):
        import json

        raw = {"search_sources": json.dumps({"x": engine_source(id="x")})}
        cfg = normalize_config(raw)
        assert list(cfg["search_sources"]) == ["x"]

    def test_broken_json_string_is_empty_pool(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = normalize_config({"search_sources": "[{not json"})
        assert cfg["search_sources"] == {}
        assert "search_sources json string is invalid" in caplog.text

    def test_non_list_is_empty_pool(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = normalize_config({"search_sources": 42})
        assert cfg["search_sources"] == {}
        assert "treated as empty pool" in caplog.text

    def test_non_dict_elements_skipped(self, caplog):
        with caplog.at_level(logging.WARNING):
            sources = normalize_sources([engine_source(), "oops", 7])
        assert list(sources) == ["saucenao-1"]
        assert caplog.text.count("is not an object, skipped") == 2

    def test_unrecognized_template_skipped(self, caplog):
        with caplog.at_level(logging.WARNING):
            sources = normalize_sources([{"id": "mystery", "foo": 1}])
        assert sources == {}
        assert "no recognizable template" in caplog.text


class TestSourceValidation:
    def test_engine_name_invalid_kept_but_unusable(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = normalize_config(
                config_with_sources(engine_source(engine="google_lens"))
            )
        source = cfg["search_sources"]["saucenao-1"]
        assert source["valid"] is False
        assert source["invalid_reason"] == "invalid_engine"
        assert "is invalid (invalid_engine)" in caplog.text
        assert enabled_sources(cfg) == []
        # 非法源仍保留在配置中，便于按 PRD §5 记入 attempts
        assert len(enabled_sources(cfg, include_invalid=True)) == 1

    def test_vision_missing_endpoint_invalid(self):
        cfg = normalize_config(
            config_with_sources(vision_source(provider_ref="", api_base="", model=""))
        )
        assert cfg["search_sources"]["doubao-search"]["valid"] is False
        assert (
            cfg["search_sources"]["doubao-search"]["invalid_reason"]
            == "missing_endpoint"
        )

    def test_desc_blank_no_longer_invalidates(self):
        """v2.0.0 repair：删除 desc 判非法规则 —— 非法只依据 §2.4.1~§2.4.4。"""
        cfg = normalize_config(config_with_sources(engine_source(desc="   ")))
        source = cfg["search_sources"]["saucenao-1"]
        assert source["valid"] is True
        assert source["invalid_reason"] == ""
        assert source["desc"] == ""

    def test_desc_absent_is_valid(self):
        cfg = normalize_config(config_with_sources(engine_source()))
        assert cfg["search_sources"]["saucenao-1"]["valid"] is True

    def test_desc_declared_is_valid_and_passthrough(self):
        cfg = normalize_config(config_with_sources(engine_source(desc="图搜图备用")))
        assert cfg["search_sources"]["saucenao-1"]["desc"] == "图搜图备用"
        assert cfg["search_sources"]["saucenao-1"]["valid"] is True

    def test_invalidity_matrix_follows_2_4_1_to_4(self):
        """非法判定矩阵：只由模板可识别 / vision 端点 / engine 名决定。"""
        cases = [
            ({"engine": "saucenao"}, True, ""),
            ({"engine": "saucenao", "desc": ""}, True, ""),
            ({"engine": "google_lens"}, False, "invalid_engine"),
            ({"engine": ""}, False, "invalid_engine"),
            ({"provider_ref": "x/y"}, True, ""),
            ({"api_base": "https://x.invalid/v1", "model": "m"}, True, ""),
            ({"api_base": "https://x.invalid/v1"}, False, "missing_endpoint"),
            ({"id": "no-template-hint"}, None, None),
        ]
        for raw, expected_valid, expected_reason in cases:
            sources = normalize_sources([raw])
            if expected_valid is None:
                assert sources == {}, raw
                continue
            source = next(iter(sources.values()))
            assert source["valid"] is expected_valid, raw
            assert source["invalid_reason"] == expected_reason, raw

    def test_unknown_key_warns_with_id_and_key(self, caplog):
        """字段表外的键被丢弃时必须告警（含源 id 与键名）。"""
        import logging

        with caplog.at_level(logging.WARNING):
            cfg = normalize_config(
                config_with_sources(engine_source(max_pixels=4096, weird="x"))
            )
        source = cfg["search_sources"]["saucenao-1"]
        assert "max_pixels" not in source and "weird" not in source
        assert "unknown key 'max_pixels' is ignored" in caplog.text
        assert "unknown key 'weird' is ignored" in caplog.text
        assert "saucenao-1" in caplog.text
        # §2.4.6 兜底：engine 源没有源级字段 → 生效上限 = 全局上限
        assert source_size_limits(cfg, source) == (20, 16777216)

    def test_vision_known_keys_do_not_warn(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            normalize_config(config_with_sources(vision_source(max_pixels=1048576)))
        assert "unknown key" not in caplog.text

    def test_normalized_source_has_no_unknown_keys(self):
        """归一化产物自身的键不算未知键（幂等：再次归一化不产生告警）。"""
        cfg = normalize_config(
            config_with_sources(
                vision_source(model="hand-model"), engine_source(desc="")
            )
        )
        assert "model_ignored" in cfg["search_sources"]["doubao-search"]
        assert "unknown key" not in " ".join(
            str(value) for value in cfg["search_sources"].values()
        )

    def test_id_autogenerated_by_kind_and_index(self):
        cfg = normalize_config(
            {"search_sources": [{"engine": "saucenao"}, {"provider_ref": "x/y"}]}
        )
        assert list(cfg["search_sources"]) == ["engine_0", "vision_1"]
        assert cfg["search_sources"]["engine_0"]["name"] == "SauceNAO"
        assert cfg["search_sources"]["vision_1"]["name"] == "vision_1"

    def test_engine_default_name(self):
        cfg = normalize_config(
            config_with_sources(
                engine_source(id="a", name="", engine="trace_moe"),
                engine_source(id="b", name="", engine="ascii2d"),
            )
        )
        assert cfg["search_sources"]["a"]["name"] == "trace.moe"
        assert cfg["search_sources"]["b"]["name"] == "ascii2d"

    def test_id_dedup_suffix(self):
        cfg = normalize_config(
            config_with_sources(
                engine_source(id="dup"),
                engine_source(id="dup", engine="ascii2d"),
                engine_source(id="dup", engine="trace_moe"),
            )
        )
        assert list(cfg["search_sources"]) == ["dup", "dup_2", "dup_3"]

    def test_disabled_source_excluded(self):
        cfg = normalize_config(config_with_sources(engine_source(enabled=False)))
        assert enabled_sources(cfg) == []

    def test_provider_form_ignores_hand_model(self):
        cfg = normalize_config(
            config_with_sources(vision_source(model="hand-written-model"))
        )
        source = cfg["search_sources"]["doubao-search"]
        assert source["form"] == "provider"
        assert source["model"] == ""
        assert source["model_ignored"] == "hand-written-model"

    def test_custom_form_keeps_model(self):
        cfg = normalize_config(config_with_sources(custom_vision_source()))
        source = cfg["search_sources"]["custom-net"]
        assert source["form"] == "custom"
        assert source["model"] == "gemini-3.5-flash"

    def test_engine_opts_injected(self):
        cfg = normalize_config(
            config_with_sources(
                engine_source(base_url="https://relay.invalid", api_key="k1")
            )
        )
        assert cfg["_engine_opts"]["saucenao"] == {
            "base_url": "https://relay.invalid",
            "api_key": "k1",
            "extra_headers": {},
        }


class TestCandidates:
    def test_auto_order_engine_first_then_vision(self):
        cfg = normalize_config(
            config_with_sources(
                vision_source(id="v1"),
                engine_source(id="e1"),
                vision_source(id="v2"),
                engine_source(id="e2", engine="ascii2d"),
            )
        )
        assert [s["id"] for s in select_candidates(cfg, "auto")] == [
            "e1",
            "e2",
            "v1",
            "v2",
        ]

    def test_engine_only_and_vision_only(self):
        cfg = normalize_config(
            config_with_sources(vision_source(id="v1"), engine_source(id="e1"))
        )
        assert [s["id"] for s in select_candidates(cfg, "engine")] == ["e1"]
        assert [s["id"] for s in select_candidates(cfg, "vision")] == ["v1"]
        empty = normalize_config(config_with_sources(vision_source(id="v1")))
        assert select_candidates(empty, "engine") == []

    def test_invalid_source_present_in_candidates(self):
        cfg = normalize_config(
            config_with_sources(
                engine_source(id="bad", engine="google_lens"), vision_source(id="v1")
            )
        )
        candidates = select_candidates(cfg, "auto")
        assert [s["id"] for s in candidates] == ["bad", "v1"]
        assert any(s["valid"] for s in candidates)

    def test_timeout_inheritance_and_override(self):
        cfg = normalize_config(
            config_with_sources(
                vision_source(id="a", timeout_sec=0),
                vision_source(id="b", timeout_sec=7),
            )
        )
        sources = cfg["search_sources"]
        assert source_timeout_sec(cfg, sources["a"]) == 30.0
        assert source_timeout_sec(cfg, sources["b"]) == 7.0

    def test_size_limits_inherit_and_override(self):
        cfg = normalize_config(
            config_with_sources(
                vision_source(id="a", max_size_mb=0, max_pixels=0),
                vision_source(id="b", max_size_mb=5, max_pixels=1048576),
            )
        )
        sources = cfg["search_sources"]
        assert source_size_limits(cfg, sources["a"]) == (20, 16777216)
        assert source_size_limits(cfg, sources["b"]) == (5, 1048576)
        # engine 源无该字段 → 沿用全局
        eng = normalize_config(config_with_sources(engine_source()))["search_sources"]
        assert source_size_limits(cfg, eng["saucenao-1"]) == (20, 16777216)


class TestMergeResults:
    @staticmethod
    def _engine_hit(title: str, author: str, similarity: float) -> dict:
        return {
            "ok": True,
            "miss": False,
            "challenge": False,
            "answer": "",
            "error": "",
            "transport": "url",
            "match": {
                "similarity": similarity,
                "title": title,
                "author": author,
                "links": ["https://x.invalid/1"],
                "extra": "",
            },
        }

    @staticmethod
    def _vision_hit(answer: str) -> dict:
        return {
            "ok": True,
            "miss": False,
            "challenge": False,
            "answer": answer,
            "error": "",
            "transport": "data_uri",
            "match": None,
        }

    def test_head_source_and_merchants_order(self):
        head = {"kind": "engine", "name": "SauceNAO", "id": "e1", "engine": "saucenao"}
        second = {"kind": "vision", "name": "豆包", "id": "v1", "engine": ""}
        merged = merge_results(
            [
                (head, self._engine_hit("作品名", "作者", 92.41)),
                (second, self._vision_hit("角色名")),
            ],
            3,
        )
        assert merged["source"] == {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        assert [item["id"] for item in merged["merged_from"]] == ["e1", "v1"]
        assert merged["merged_from"][1]["similarity"] is None
        assert merged["merged_from"][1]["title"] == "角色名"
        assert merged["answer"] == "角色名"

    def test_duplicate_engine_hits_deduped(self):
        head = {"kind": "engine", "name": "A", "id": "e1", "engine": "saucenao"}
        dup = {"kind": "engine", "name": "B", "id": "e2", "engine": "ascii2d"}
        merged = merge_results(
            [
                (head, self._engine_hit("作品名", "作者", 50.0)),
                (dup, self._engine_hit("作品名 ", "作者", 90.0)),
            ],
            3,
        )
        assert [item["id"] for item in merged["merged_from"]] == ["e1"]

    def test_match_takes_highest_similarity(self):
        head = {"kind": "engine", "name": "A", "id": "e1", "engine": "saucenao"}
        second = {"kind": "engine", "name": "B", "id": "e2", "engine": "trace_moe"}
        merged = merge_results(
            [
                (head, self._engine_hit("低", "a", 10.0)),
                (second, self._engine_hit("高", "b", 91.23)),
            ],
            3,
        )
        assert merged["match"]["title"] == "高"
        assert [item["title"] for item in merged["merged_from"]] == ["低", "高"]

    def test_max_sources_cap_and_none_similarity_scored_zero(self):
        sources = [
            {"kind": "engine", "name": f"S{i}", "id": f"e{i}", "engine": "saucenao"}
            for i in range(5)
        ]
        hits = [
            (sources[0], self._engine_hit("t0", "a0", None)),
            (sources[1], self._engine_hit("t1", "a1", 5.0)),
            (sources[2], self._engine_hit("t2", "a2", 99.0)),
        ]
        merged = merge_results(hits, 2)
        assert [item["id"] for item in merged["merged_from"]] == ["e0", "e1"]
        assert merged["match"]["title"] == "t1"

    def test_no_hits_returns_none(self):
        assert merge_results([], 3) is None
        failed = {"kind": "vision", "name": "A", "id": "v1", "engine": ""}
        assert merge_results([(failed, {"ok": False})], 3) is None


class TestVisionVerdict:
    def test_explicit_hit_strips_verdict_line(self):
        result = vision_verdict("VERDICT: HIT\n主体：初音未来\n出处：VOCALOID")
        assert result["ok"] is True
        assert result["answer"] == "主体：初音未来\n出处：VOCALOID"

    def test_explicit_miss(self):
        result = vision_verdict("verdict：miss\n看不出")
        assert result["miss"] is True
        assert result["ok"] is False

    def test_marker_words_mean_miss(self):
        assert vision_verdict("这张图我无法确定是什么角色")["miss"] is True
        assert vision_verdict("I cannot identify this character")["miss"] is True

    def test_short_answer_is_miss(self):
        assert vision_verdict("嗯")["miss"] is True
        assert vision_verdict("")["miss"] is True

    def test_plain_nonempty_answer_is_hit(self):
        result = vision_verdict("这是《原神》里的钟离，出自米哈游游戏原神。")
        assert result["ok"] is True
        assert result["answer"].startswith("这是《原神》")


class TestPayloadsAndHints:
    def test_image_result_keys(self):
        image = build_image_result(0, "/tmp/a.jpg", ok=True, transport="data_uri")
        assert set(image) == {
            "index",
            "ref",
            "ok",
            "transport",
            "source",
            "match",
            "answer",
            "merged_from",
            "attempts",
            "rounds_used",
            "cached",
            "error",
        }
        assert image["source"] is None
        assert image["transport"] == "data_uri"

    def test_ref_truncated_to_200_chars(self):
        image = build_image_result(0, "x" * 500)
        assert len(image["ref"]) == 200

    def test_payload_status_and_ok_flag(self):
        import json

        ok_image = build_image_result(0, "a", ok=True)
        failed_image = build_image_result(1, "b", error="bad_ref")
        payload = json.loads(build_results_payload([ok_image, failed_image]))
        assert payload["tool"] == "image_search"
        assert payload["status"] == "partial"
        assert payload["ok"] is False
        assert payload["search_type"] == "auto"
        assert set(payload) == {
            "tool",
            "ok",
            "status",
            "search_type",
            "question",
            "images",
            "summary",
            "hint",
            "elapsed_ms",
            "cached",
            "notes",
        }
        only_ok = json.loads(build_results_payload([ok_image]))
        assert only_ok["ok"] is True and only_ok["status"] == "ok"

    def test_all_unreadable_is_bad_refs(self):
        import json

        images = [build_image_result(i, "a", error="bad_ref") for i in range(2)]
        assert json.loads(build_results_payload(images))["status"] == "bad_refs"

    def test_empty_pool_hint_mentions_search_sources(self):
        assert "search_sources" in EMPTY_POOL_HINT
        assert EMPTY_POOL_SUMMARY == "当前没有可用的搜图源。"
        assert render_hint([], "empty_pool") == EMPTY_POOL_HINT

    def test_challenge_hint_requires_no_error_or_timeout(self):
        """v2.0.0 repair：存在 challenge 且无 ok / error / timeout 才给 CF 引导。"""
        from search_core import CHALLENGE_HINT, build_attempt

        source = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        challenge = build_attempt(source, "challenge", error="cf", challenge=True)
        skipped = build_attempt(source, "skipped", error="no_public_url")
        cases = [
            ([challenge], CHALLENGE_HINT),  # 纯 challenge
            ([challenge, skipped], CHALLENGE_HINT),  # challenge + 被跳过，仍无结果
            ([challenge, build_attempt(source, "miss")], CHALLENGE_HINT),  # miss 不阻断
            ([challenge, build_attempt(source, "error", error="boom")], ""),
            ([challenge, build_attempt(source, "timeout", error="t")], ""),
        ]
        for attempts, expected in cases:
            image = build_image_result(0, "a", attempts=attempts, error="cf")
            assert render_hint([image], "failed") == expected, attempts
        # 有命中（ok）时不谈风控
        hit_image = build_image_result(
            0, "a", ok=True, attempts=[challenge, build_attempt(source, "hit")]
        )
        assert render_hint([hit_image], "ok") == ""

    def test_image_url_hint_needs_no_public_url_skip_and_no_hit(self):
        """v2.0.0 repair：存在被 no_public_url 跳过的 engine 源且无命中即触发。"""
        from search_core import NO_PUBLIC_URL_HINT, build_attempt

        engine = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        vision = {"kind": "vision", "name": "豆包", "id": "v1", "engine": ""}
        skipped = build_attempt(engine, "skipped", error="no_public_url")
        miss = build_attempt(engine, "miss")
        image = build_image_result(0, "a", attempts=[skipped], error="no url")
        assert render_hint([image], "failed") == NO_PUBLIC_URL_HINT
        mixed = build_image_result(0, "a", attempts=[skipped, miss], error="no url")
        assert render_hint([mixed], "failed") == NO_PUBLIC_URL_HINT
        hit_image = build_image_result(
            0,
            "a",
            ok=True,
            transport="data_uri",
            attempts=[skipped, build_attempt(vision, "hit")],
        )
        assert render_hint([hit_image], "partial") == ""
        plain = build_image_result(0, "a", attempts=[miss], error="miss")
        assert render_hint([plain], "failed") == ""

    def test_render_hint_ignores_empty_attempts(self):
        image = build_image_result(0, "a", attempts=[], error="bad_ref")
        assert render_hint([image], "failed") == ""

    def test_total_timeout_error_is_distinct_from_empty_candidates(self):
        from search_core import TOTAL_TIMEOUT_ERROR, describe_attempts

        assert describe_attempts([]) == "没有可用的搜图源。"
        assert describe_attempts([]) != TOTAL_TIMEOUT_ERROR
        assert "total_timeout" in TOTAL_TIMEOUT_ERROR

    def test_summary_renders_hit_and_miss(self):
        source = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        image = build_image_result(
            0,
            "a",
            ok=True,
            match={
                "similarity": 92.41,
                "title": "作品名",
                "author": "作者",
                "links": [],
                "extra": "",
            },
            merged_from=[
                {
                    "kind": "engine",
                    "name": "SauceNAO",
                    "id": "e1",
                    "similarity": 92.41,
                    "title": "作品名",
                }
            ],
            source=source,
        )
        text = render_summary([image])
        assert "图 1" in text
        assert "作品名" in text
        assert "92.41" in text
        assert "图搜图引擎 SauceNAO" in text
        miss = build_image_result(0, "a", error="没有查到这张图的来源。")
        assert render_summary([miss]).startswith("图 1：没有查到这张图的来源。")

    def test_summary_capped_at_800_chars(self):
        images = [
            build_image_result(
                i, "a", ok=True, answer="很长的回答" * 40, merged_from=[]
            )
            for i in range(12)
        ]
        assert len(render_summary(images)) <= 800


class TestInputNormalization:
    def test_non_list_refs_is_empty(self):
        notes: list[str] = []
        assert normalize_refs("not-a-list", notes) == []
        assert normalize_refs(None, notes) == []
        assert notes == []

    def test_refs_dedupe_keep_order_and_cap(self):
        notes: list[str] = []
        refs = normalize_refs(
            ["/tmp/a.jpg", "/tmp/a.jpg ", "/tmp/b.jpg"]
            + [f"/tmp/{i}.jpg" for i in range(10)],
            notes,
        )
        assert refs[:2] == ["/tmp/a.jpg", "/tmp/b.jpg"]
        assert len(refs) == 8
        assert "refs_truncated:8" in notes

    def test_blank_ref_kept_for_bad_ref(self):
        notes: list[str] = []
        assert normalize_refs(["", "/tmp/a.jpg"], notes) == ["", "/tmp/a.jpg"]

    def test_search_type_normalization(self):
        notes: list[str] = []
        assert normalize_search_type(" ENGINE ", notes) == "engine"
        assert normalize_search_type("banana", notes) == "auto"
        assert normalize_search_type(None, notes) == "auto"
        assert notes == ["invalid_search_type:banana"]

    def test_question_truncation(self):
        notes: list[str] = []
        question = normalize_question("问" * 600, notes)
        assert len(question) == 500
        assert notes == ["question_truncated"]
        assert normalize_question(None, notes) == ""
        assert normalize_question(12345, notes) == "12345"


class TestCacheAndHealth:
    def test_cache_key_formula(self):
        import hashlib

        key = ResultCache.make_key(b"abc", "auto", "这是谁")
        expected = f"{hashlib.sha256(b'abc').hexdigest()}|auto|这是谁"
        assert key == expected

    def test_cache_ttl_zero_disables(self):
        cache = ResultCache(0)
        cache.set("k", {"index": 0})
        assert cache.get("k") is None

    def test_cache_roundtrip_is_deep_copied(self):
        cache = ResultCache(300)
        image = build_image_result(0, "a", ok=True)
        cache.set("k", image)
        cached = cache.get("k")
        assert cached == image
        cached["index"] = 99
        assert cache.get("k")["index"] == 0

    def test_cache_expiry(self, monkeypatch):
        import search_core

        clock = {"now": 1000.0}
        monkeypatch.setattr(search_core.time, "time", lambda: clock["now"])
        cache = ResultCache(1)
        cache.set("k", {"index": 0})
        assert cache.get("k") == {"index": 0}
        clock["now"] = 1002.0
        assert cache.get("k") is None

    def test_health_stats_summary(self):
        from search_core import build_attempt

        health = HealthStats()
        source = {"kind": "vision", "name": "豆包", "id": "v1", "engine": ""}
        health.record(
            [
                build_attempt(source, "hit", elapsed_ms=100),
                build_attempt(source, "miss", elapsed_ms=300),
                build_attempt(source, "error", error="boom", elapsed_ms=200),
            ]
        )
        summary = health.summary()
        assert (
            "v1(attempts=3 hits=1 misses=1 errors=1 challenges=0 avg_ms=200)" in summary
        )
        health.reset()
        assert health.summary() == ""


class TestDetectPublicIp:
    def test_proxy_env_returns_local_ip(self, monkeypatch):
        import search_core

        monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
        ip = search_core.detect_public_ip()
        assert ip and isinstance(ip, str)


@pytest.mark.parametrize(
    ("search_type", "expected"),
    [("auto", ["e1", "v1"]), ("engine", ["e1"]), ("vision", ["v1"])],
)
def test_select_candidates_parametrized(search_type, expected):
    cfg = normalize_config(
        config_with_sources(engine_source(id="e1"), vision_source(id="v1"))
    )
    assert [s["id"] for s in select_candidates(cfg, search_type)] == expected


class TestRepairRound3Semantics:
    """t12 / F12 / F13：空池语义、真实尝试判定与图床引导条件。"""

    def test_has_real_attempt_semantics(self):
        from search_core import build_attempt, has_real_attempt

        source = {"kind": "vision", "name": "豆包", "id": "v1", "engine": ""}
        assert has_real_attempt([]) is False
        assert (
            has_real_attempt([build_attempt(source, "skipped", error="invalid_source")])
            is False
        )
        for outcome in ("hit", "miss", "error", "timeout", "challenge"):
            assert has_real_attempt([build_attempt(source, outcome)]) is True, outcome

    def test_aggregate_status_no_attempts_is_empty_pool(self):
        from search_core import NO_ATTEMPTS_ERROR

        images = [build_image_result(0, "a", error=NO_ATTEMPTS_ERROR)]
        assert aggregate_status(images) == "empty_pool"
        mixed = images + [build_image_result(1, "b", error="bad_ref")]
        assert aggregate_status(mixed) == "failed"
        assert (
            aggregate_status([build_image_result(0, "a", error="bad_ref")])
            == "bad_refs"
        )

    def test_describe_attempts_all_skipped_uses_failure_wording(self):
        from search_core import build_attempt, describe_attempts

        source = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        attempts = [
            build_attempt(source, "skipped", error="no_public_url"),
            build_attempt(source, "skipped", error="invalid_source"),
        ]
        text = describe_attempts(attempts)
        assert text.startswith("所有搜图源都失败了（")
        assert "公网图片 URL" in text

    def test_render_hint_empty_pool_variants(self):
        from search_core import (
            EMPTY_POOL_HINT,
            NO_PUBLIC_URL_HINT,
            build_attempt,
        )

        engine = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        skipped_url = build_attempt(engine, "skipped", error="no_public_url")
        skipped_invalid = build_attempt(engine, "skipped", error="invalid_source")
        # 全部因缺公网 URL 跳过 → 引导开图床
        image = build_image_result(0, "a", attempts=[skipped_url], error="no attempts")
        assert render_hint([image], "empty_pool") == NO_PUBLIC_URL_HINT
        # 全部因配置非法跳过 → 引导配 search_sources
        image = build_image_result(
            0, "a", attempts=[skipped_invalid], error="no attempts"
        )
        assert render_hint([image], "empty_pool") == EMPTY_POOL_HINT
        # 没有任何 attempts（候选为空）→ 同样引导配 search_sources
        assert (
            render_hint([build_image_result(0, "a")], "empty_pool") == EMPTY_POOL_HINT
        )

    def test_image_url_hint_needs_engine_only_or_no_real_vision_attempt(self):
        """F12：auto 下 vision 源真实失败/超时 → 不得给图床引导。"""
        from search_core import NO_PUBLIC_URL_HINT, build_attempt

        engine = {
            "kind": "engine",
            "name": "SauceNAO",
            "id": "e1",
            "engine": "saucenao",
        }
        vision = {"kind": "vision", "name": "豆包", "id": "v1", "engine": ""}
        skipped_url = build_attempt(engine, "skipped", error="no_public_url")
        cases = [
            # 纯 engine 候选池（search_type=engine）→ 引导
            ([skipped_url], "engine", NO_PUBLIC_URL_HINT),
            # auto 但 vision 侧只有 skipped（无真实尝试）→ 引导
            (
                [skipped_url, build_attempt(vision, "skipped", error="invalid_source")],
                "auto",
                NO_PUBLIC_URL_HINT,
            ),
            # auto 且 vision 真实失败 → 不引导（不得误报开启 image_url.enabled）
            (
                [skipped_url, build_attempt(vision, "error", error="HTTP 500")],
                "auto",
                "",
            ),
            # auto 且 vision 超时 → 不引导
            ([skipped_url, build_attempt(vision, "timeout", error="t")], "auto", ""),
            # auto 且 vision 命中 → 不引导
            ([skipped_url, build_attempt(vision, "hit")], "auto", ""),
        ]
        for attempts, search_type, expected in cases:
            image = build_image_result(0, "a", attempts=attempts, error="x")
            assert render_hint([image], "failed", search_type) == expected, attempts
