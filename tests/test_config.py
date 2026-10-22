"""test_config.py -- 插件配置读取与全局键归一化单测（PRD §2.1 / §2.6 / §2.8 / §6.4）。

覆盖点：
- `load_config()` 的显式配置优先 / 文件回退 / BOM 兼容 / 非法 JSON 不抛异常；
- data 目录解析走 AstrBot 官方路径工具，且绝不把某个绝对路径当唯一路径；
- `image_url` 小节逐键归一化（默认关闭、端口范围、前缀去尾斜杠）；
- v1.7 旧配置存在而 v2.0 新配置缺失时的迁移提示位。
"""

from __future__ import annotations

import json

from search_core import (
    DEFAULT_IMAGE_URL,
    DEFAULT_SEARCH,
    astrbot_data_dir,
    detect_migration_hint,
    legacy_config_path,
    load_config,
    normalize_config,
    plugin_config_path,
)


class TestPaths:
    def test_data_dir_is_astrbot_data(self):
        path = astrbot_data_dir()
        assert path.name == "data"
        assert path.is_absolute()

    def test_plugin_config_path(self):
        path = plugin_config_path()
        assert path.name == "astrbot_plugin_image_search_config.json"
        assert path.parent.name == "config"
        assert path.parent.parent == astrbot_data_dir()

    def test_legacy_config_path(self):
        assert legacy_config_path().name == "astrbot_plugin_vision_router_config.json"

    def test_data_dir_matches_official_helper(self):
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        assert str(astrbot_data_dir()) == str(get_astrbot_data_path())


class TestLoadConfig:
    def test_explicit_config_wins(self):
        assert load_config({"tool_hint_enabled": False}) == {"tool_hint_enabled": False}

    def test_empty_explicit_falls_back_to_file(self, tmp_path, monkeypatch):
        path = tmp_path / "astrbot_plugin_image_search_config.json"
        path.write_text(json.dumps({"log_level": "debug"}), encoding="utf-8")
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        assert load_config({}) == {"log_level": "debug"}
        assert load_config(None) == {"log_level": "debug"}

    def test_bom_tolerated(self, tmp_path, monkeypatch):
        path = tmp_path / "cfg.json"
        path.write_text('{"log_level": "warning"}', encoding="utf-8-sig")
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        assert load_config() == {"log_level": "warning"}

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "search_core.plugin_config_path", lambda: tmp_path / "nope.json"
        )
        assert load_config() == {}

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch, caplog):
        path = tmp_path / "cfg.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        assert load_config() == {}
        assert "not valid json" in caplog.text

    def test_non_object_root_returns_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "cfg.json"
        path.write_text("[1, 2]", encoding="utf-8")
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        assert load_config() == {}

    def test_load_then_normalize_off_file(self, tmp_path, monkeypatch):
        path = tmp_path / "cfg.json"
        path.write_text(
            json.dumps(
                {
                    "search_sources": [
                        {"__template_key": "engine", "engine": "trace_moe"}
                    ],
                    "search": {"max_rounds": 1},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        cfg = normalize_config(load_config())
        assert list(cfg["search_sources"]) == ["engine_0"]
        assert cfg["search"]["max_rounds"] == 1
        assert cfg["search"]["merge_max_sources"] == DEFAULT_SEARCH["merge_max_sources"]


class TestImageUrlSection:
    def test_defaults(self):
        assert normalize_config({})["image_url"] == DEFAULT_IMAGE_URL

    def test_disabled_by_default(self):
        assert normalize_config({})["image_url"]["enabled"] is False

    def test_port_range(self):
        assert normalize_config({"image_url": {"port": 0}})["image_url"]["port"] == 8123
        assert (
            normalize_config({"image_url": {"port": 70000}})["image_url"]["port"]
            == 8123
        )
        assert (
            normalize_config({"image_url": {"port": 9000}})["image_url"]["port"] == 9000
        )

    def test_public_base_url_strips_trailing_slash(self):
        cfg = normalize_config(
            {"image_url": {"public_base_url": "http://1.2.3.4:8123/"}}
        )
        assert cfg["image_url"]["public_base_url"] == "http://1.2.3.4:8123"

    def test_dir_kept(self):
        cfg = normalize_config({"image_url": {"dir": "/tmp/imgsrv"}})
        assert cfg["image_url"]["dir"] == "/tmp/imgsrv"

    def test_non_dict_section_is_default(self):
        cfg = normalize_config({"image_url": "oops"})
        assert cfg["image_url"] == DEFAULT_IMAGE_URL


class TestGlobalSections:
    def test_all_search_keys_present_with_defaults(self):
        cfg = normalize_config({})
        assert cfg["search"] == DEFAULT_SEARCH
        assert set(cfg["search"]) == set(DEFAULT_SEARCH)

    def test_missing_config_is_empty_pool(self):
        cfg = normalize_config({})
        assert cfg["search_sources"] == {}
        assert cfg["tool_hint_enabled"] is True
        assert cfg["download_timeout_sec"] == 20
        assert cfg["log_level"] == "info"

    def test_none_input(self):
        cfg = normalize_config(None)
        assert cfg["search_sources"] == {}
        assert cfg["search"] == DEFAULT_SEARCH

    def test_migration_hint_flag(self, tmp_path, monkeypatch):
        new_path = tmp_path / "new.json"
        old_path = tmp_path / "old.json"
        monkeypatch.setattr("search_core.plugin_config_path", lambda: new_path)
        monkeypatch.setattr("search_core.legacy_config_path", lambda: old_path)
        assert detect_migration_hint() is False  # 两者都不存在
        old_path.write_text("{}", encoding="utf-8")
        assert detect_migration_hint() is True  # 新缺失 + 旧存在 → 提示迁移
        new_path.write_text("{}", encoding="utf-8")
        assert detect_migration_hint() is False  # 新配置已存在 → 不再提示


class TestConfigSource:
    def test_star_injected_config_wins_over_file(
        self, tmp_path, monkeypatch, make_plugin
    ):
        """配置来源 = Star 注入的 self.config（与旧插件一致），文件仅作回退。"""
        import asyncio

        from conftest import config_with_sources, custom_vision_source

        path = tmp_path / "astrbot_plugin_image_search_config.json"
        path.write_text(json.dumps({"search_sources": []}), encoding="utf-8")
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        plugin = asyncio.run(make_plugin(config_with_sources(custom_vision_source())))
        assert "custom-net" in plugin._cfg["search_sources"]

    def test_empty_injected_config_falls_back_to_file(
        self, tmp_path, monkeypatch, make_plugin
    ):
        import asyncio

        path = tmp_path / "astrbot_plugin_image_search_config.json"
        path.write_text(
            json.dumps(
                {"search_sources": [{"__template_key": "engine", "engine": "ascii2d"}]}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("search_core.plugin_config_path", lambda: path)
        plugin = asyncio.run(make_plugin({}))
        assert list(plugin._cfg["search_sources"]) == ["engine_0"]

    def test_data_dir_is_not_a_single_hardcoded_path(self):
        """data 目录必须优先官方工具，硬编码绝对路径只能是最后一档回退。"""
        import inspect

        import search_core

        source = inspect.getsource(search_core.astrbot_data_dir)
        assert "get_astrbot_data_path" in source
        assert source.index("get_astrbot_data_path") < source.index(
            "/root/AstrBot/data"
        )
        assert "ASTRBOT_ROOT" in source

    def test_data_dir_env_override_when_official_helper_missing(
        self, monkeypatch, tmp_path
    ):
        import importlib

        import search_core

        real_import = importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name.startswith("astrbot.core.utils"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        # search_core 内部惰性 import importlib，替换同名模块属性即可拦截官方路径工具
        monkeypatch.setattr(importlib, "import_module", fake_import)
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        assert search_core.astrbot_data_dir() == tmp_path / "data"


class TestLegacyDirUntouched:
    """旧插件目录零改动的独立证据（不依赖 `git status`：data/ 在 .gitignore 内）。"""

    @staticmethod
    def _legacy_dir():
        from pathlib import Path

        return Path(__file__).resolve().parents[2] / "astrbot_plugin_vision_router"

    @staticmethod
    def _baseline_file():
        from pathlib import Path

        return Path(__file__).resolve().parents[1] / "PRD.md"

    def test_no_legacy_file_is_newer_than_v2_baseline(self):
        legacy = self._legacy_dir()
        baseline = self._baseline_file()
        if not legacy.is_dir():
            pytest.skip("旧插件目录不存在")
        if not baseline.is_file():
            pytest.skip("v2 基准文件（新插件 PRD.md）缺失")
        baseline_mtime = baseline.stat().st_mtime
        newer = sorted(
            str(path.relative_to(legacy))
            for path in legacy.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
            and path.stat().st_mtime > baseline_mtime
        )
        assert newer == [], f"旧插件目录出现新写入：{newer}"

    def test_legacy_tree_snapshot_is_nonempty(self):
        legacy = self._legacy_dir()
        if not legacy.is_dir():
            pytest.skip("旧插件目录不存在")
        files = [
            path
            for path in legacy.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        assert len(files) >= 50  # 冻结快照：60 个文件（排除 __pycache__）
