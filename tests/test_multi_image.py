"""test_multi_image.py -- 多图逐图隔离与结果装配单测（PRD §1.2 / §1.6.1 / §3.1）。

覆盖点：
- 多图按 `image_refs` 顺序独立处理，`images[]` 顺序与去重后的 refs 一致；
- 单图异常 / 单图超时只影响该图，其余图照常返回（`status="partial"`）；
- 单图不可读（`bad_ref`）不阻断其他图；
- 同一引用去重后只处理一次；超过 8 张截断并记 `refs_truncated:8`；
- 一张命中一张未命中 → `partial` + 摘要逐图列出。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import config_with_sources, custom_vision_source, variant_data_uri

import main as plugin_main
from image_io import to_data_uri

HIT_ANSWER = "VERDICT: HIT\n主体：初音未来\n出处：VOCALOID"


def _call(plugin, **kwargs) -> dict:
    raw = asyncio.run(plugin.image_search(event=None, **kwargs))
    assert isinstance(raw, str)
    return json.loads(raw)


def _patch_hit(monkeypatch, answers=None):
    """把 A 类源调用替换为命中回答（可按调用次序给不同回答）。"""
    sequence = list(answers or [])
    calls: list[str] = []

    async def fake_call(endpoint, **kwargs):
        calls.append(kwargs.get("data_uri", ""))
        if sequence:
            return sequence.pop(0)
        return HIT_ANSWER

    monkeypatch.setattr(plugin_main.provider_reader, "call_vision_source", fake_call)
    return calls


class TestMultiImageIsolation:
    def test_three_images_all_ok(self, make_plugin, monkeypatch, tiny_png_bytes):
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        refs = [to_data_uri(tiny_png_bytes, "png"), to_data_uri(tiny_png_bytes, "png")]
        payload = _call(plugin, image_refs=[refs[0], f"{refs[1]}"], question="这是谁")
        # 同一引用只处理一次（去重）
        assert len(payload["images"]) == 1
        assert payload["status"] == "ok"

    def test_per_image_exception_is_isolated(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        uri = to_data_uri(tiny_png_bytes, "png")
        original = plugin_main.image_io.resolve_image_bytes

        async def flaky(ref, max_size_mb, timeout):
            if "boom" in str(ref):
                raise RuntimeError("decoder exploded")
            return await original(ref, max_size_mb, timeout)

        monkeypatch.setattr(plugin_main.image_io, "resolve_image_bytes", flaky)
        payload = _call(plugin, image_refs=[uri, "/tmp/boom.png", uri])
        # 第 2 张与第 1 张引用不同（路径 vs data URI）→ 两张不同图 + 中间异常图
        assert [image["index"] for image in payload["images"]] == [0, 1]
        assert payload["images"][0]["ok"] is True
        assert payload["images"][1]["ok"] is False
        assert "RuntimeError" in payload["images"][1]["error"]
        assert payload["status"] == "partial"

    def test_unreadable_ref_only_affects_that_image(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        uri = to_data_uri(tiny_png_bytes, "png")
        payload = _call(plugin, image_refs=[uri, "/tmp/definitely-missing-image.png"])
        assert payload["status"] == "partial"
        assert payload["images"][0]["ok"] is True
        assert payload["images"][1]["ok"] is False
        assert payload["images"][1]["error"] == "bad_ref"
        assert payload["images"][1]["attempts"] == []

    def test_slow_image_times_out_without_blocking_others(
        self, make_plugin, monkeypatch, tiny_png_bytes, alt_png_bytes
    ):
        _patch_hit(monkeypatch)
        # 冻结时钟：总预算恒不耗尽，只让逐图 wait_for 生效（测试提速）
        monkeypatch.setattr(plugin_main.time, "time", lambda: 1000.0)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        plugin._cfg["search"]["total_timeout_sec"] = 1
        plugin._cfg["search"]["source_timeout_sec"] = 0
        original = plugin._run_one_image

        async def slow(
            cfg, index, ref, question, intent, search_type, candidates, deadline, notes
        ):
            if index == 1:
                await asyncio.sleep(5)
            return await original(
                cfg,
                index,
                ref,
                question,
                intent,
                search_type,
                candidates,
                deadline,
                notes,
            )

        plugin._run_one_image = slow
        uri = to_data_uri(tiny_png_bytes, "png")
        other = to_data_uri(alt_png_bytes, "png")
        payload = _call(plugin, image_refs=[uri, other, uri])
        assert len(payload["images"]) == 2
        assert payload["images"][1]["error"].startswith("该图处理超出总时限")
        assert payload["images"][0]["ok"] is True
        assert payload["status"] == "partial"

    def test_mixed_hit_and_miss_is_partial(
        self, make_plugin, monkeypatch, tiny_png_bytes, alt_png_bytes
    ):
        _patch_hit(monkeypatch, answers=["VERDICT: MISS\n无法确定", HIT_ANSWER])
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(
            plugin,
            image_refs=[
                to_data_uri(tiny_png_bytes, "png"),
                to_data_uri(alt_png_bytes, "png"),
            ],
        )
        assert payload["status"] == "partial"
        assert payload["ok"] is False
        assert payload["images"][0]["ok"] is False
        assert payload["images"][0]["rounds_used"] == 1
        assert payload["images"][1]["ok"] is True
        assert "图 1：" in payload["summary"] and "图 2：" in payload["summary"]


class TestRefsHandling:
    def test_refs_deduped_and_capped(self, make_plugin, monkeypatch, tiny_png_bytes):
        calls = _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        uri = to_data_uri(tiny_png_bytes, "png")
        # 同一引用重复 3 次 → 去重为 1；再给 10 个互不相同的合法引用
        refs = [uri] * 3 + [
            variant_data_uri(tiny_png_bytes, f"v{i}") for i in range(10)
        ]
        payload = _call(plugin, image_refs=refs)
        assert len(payload["images"]) == 8
        assert "refs_truncated:8" in payload["notes"]
        assert payload["images"][0]["ref"] == uri
        # 8 张图字节相同 → 第 2 张起命中结果缓存（PRD §3.5：缓存键只含图片哈希）
        assert len(calls) == 1
        assert payload["images"][0]["cached"] is False
        assert all(image["cached"] for image in payload["images"][1:])

    def test_order_follows_input(self, make_plugin, monkeypatch, tiny_png_bytes):
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        refs = [variant_data_uri(tiny_png_bytes, f"n{i}") for i in range(3)]
        payload = _call(plugin, image_refs=refs)
        assert [image["index"] for image in payload["images"]] == [0, 1, 2]
        assert [image["ref"] for image in payload["images"]] == refs

    def test_blank_ref_becomes_bad_ref_without_blocking(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        uri = to_data_uri(tiny_png_bytes, "png")
        payload = _call(plugin, image_refs=["", uri])
        assert payload["images"][0]["ok"] is False
        assert payload["images"][0]["error"] == "bad_ref"
        assert payload["images"][1]["ok"] is True

    def test_all_refs_bad_is_bad_refs_status(self, make_plugin):
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(plugin, image_refs=["刚才那张图", ""])
        assert payload["status"] == "bad_refs"
        assert payload["ok"] is False


class TestRepairRound2:
    def test_non_string_refs_become_bad_refs(self, make_plugin):
        """v2.0.0 repair：image_refs 里的非字符串元素 → bad_ref，整体 bad_refs。"""
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        payload = _call(plugin, image_refs=[123, {}])
        assert payload["status"] == "bad_refs"
        assert payload["ok"] is False
        assert [image["error"] for image in payload["images"]] == ["bad_ref", "bad_ref"]
        assert [image["ref"] for image in payload["images"]] == ["123", "{}"]

    def test_uncompressible_oversize_local_image_is_too_large(
        self, make_plugin, monkeypatch, tmp_path
    ):
        """超绝对物化上限 → image_too_large（不是 bad_ref）。"""
        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        plugin._cfg["search"]["max_size_mb"] = 1
        import image_io

        # 把 100MiB 绝对上限压到 4MiB 以便快速取证（resolve 运行期读该常量）
        monkeypatch.setattr(
            plugin_main.image_io, "ABSOLUTE_MATERIALIZE_LIMIT_BYTES", 4 * 1024 * 1024
        )
        path = tmp_path / "big.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (4 * 1024 * 1024 + 2048))
        payload = _call(plugin, image_refs=[str(path)])
        assert payload["images"][0]["error"] == "image_too_large"
        assert payload["status"] == "bad_refs"
        assert image_io.ABSOLUTE_MATERIALIZE_LIMIT_BYTES == 4 * 1024 * 1024

    def test_compressible_photo_over_old_four_times_cap_is_compressed_and_hits(
        self, make_plugin, monkeypatch, tmp_path
    ):
        """F14 回归：5MB+ 照片容器 + max_size_mb=1（旧 4× 上限=4MB 会误判 image_too_large）。

        取证口径（acceptance 要求）：贴出压缩后字节数与 outcome。
        """
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL 不可用")
        import image_io

        _patch_hit(monkeypatch)
        # 照片容器：未压缩 BMP（1900×1400 渐变 ≈ 8MB），远超旧 4× 上限（4MB），
        # 但平滑内容可被 JPEG 重编码压到全局 1MB 以内
        image = Image.linear_gradient("L").resize((1900, 1400)).convert("RGB")
        path = tmp_path / "photo.bmp"
        image.save(path, format="BMP")
        original_bytes = path.stat().st_size
        assert original_bytes > 4 * 1024 * 1024  # 旧 4× 上限必然拒绝
        assert original_bytes > 5 * 1024 * 1024  # 与 finding 的 5.2MB 场景同量级
        assert original_bytes < image_io.materialize_limit_bytes()

        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        plugin._cfg["search"]["max_size_mb"] = 1
        payload = _call(plugin, image_refs=[str(path)])
        image_obj = payload["images"][0]

        blob = path.read_bytes()
        compressed, new_ext, changed = image_io.preprocess_image(
            blob, "bmp", 1, 16777216
        )
        print(
            f"[F14] original={original_bytes}B -> compressed={len(compressed)}B "
            f"(ext={new_ext}, changed={changed}) outcome="
            f"{image_obj['attempts'][0]['outcome']} status={payload['status']}"
        )
        assert changed is True
        assert len(compressed) <= 1024 * 1024
        assert payload["status"] == "ok"
        assert image_obj["ok"] is True
        assert image_obj["transport"] == "data_uri"
        assert image_obj["attempts"][0]["outcome"] == "hit"
        assert image_obj["attempts"][0]["counted_round"] is False
        assert image_obj["error"] == ""

    def test_total_timeout_before_any_attempt_uses_timeout_wording(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        """v2.0.0 repair（进入源循环前就超预算）→ total_timeout 文案，attempts 为空。"""
        from search_core import TOTAL_TIMEOUT_ERROR

        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        plugin._cfg["search"]["total_timeout_sec"] = 0  # deadline=now → 立即耗尽
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        image = payload["images"][0]
        assert image["error"] == TOTAL_TIMEOUT_ERROR
        assert image["attempts"] == []
        assert "total_timeout" in payload["notes"]
        assert payload["status"] == "failed"

    def test_total_timeout_inside_source_loop_uses_timeout_wording(
        self, make_plugin, monkeypatch, tiny_png_bytes
    ):
        """v2.0.0 repair（源循环中途超预算、一个源都没试到）→ 同一 total_timeout 文案。"""
        from search_core import TOTAL_TIMEOUT_ERROR

        _patch_hit(monkeypatch)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        original_resolve = plugin_main.image_io.resolve_image_bytes
        real_time = plugin_main.time.time
        state = {"jumped": False}

        def clock():
            return real_time() + (10**6 if state["jumped"] else 0.0)

        async def resolve_and_jump(ref, max_size_mb, timeout):
            result = await original_resolve(ref, max_size_mb, timeout)
            state["jumped"] = True  # 物化完成后再让时钟跳表 → 循环首次检查即超预算
            return result

        monkeypatch.setattr(plugin_main.time, "time", clock)
        monkeypatch.setattr(
            plugin_main.image_io, "resolve_image_bytes", resolve_and_jump
        )
        payload = _call(plugin, image_refs=[to_data_uri(tiny_png_bytes, "png")])
        image = payload["images"][0]
        assert image["error"] == TOTAL_TIMEOUT_ERROR
        assert image["attempts"] == []
        assert "total_timeout" in payload["notes"]
