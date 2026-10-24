"""test_schema.py -- 配置 schema / metadata / 文档面的仓库级守卫。

这些断言只依赖仓库内文件，不启动插件、不访问网络：
- `_conf_schema.json` 顶层键、双模板字段、`search` / `image_url` 默认值与类型；
- 每个 schema 节点都有 `description` / `default`，类型在 AstrBot 支持集合内；
- `metadata.yaml` 版本与仓库地址；
- README / CHANGELOG 不再引用已删除的 v1.7 迁移文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"
METADATA_PATH = PLUGIN_DIR / "metadata.yaml"
README_PATH = PLUGIN_DIR / "README.md"
CHANGELOG_PATH = PLUGIN_DIR / "CHANGELOG.md"

SUPPORTED_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
    "dict",
}

VISION_FIELDS = [
    "enabled",
    "id",
    "name",
    "provider_ref",
    "api_base",
    "api_key",
    "model",
    "web_search",
    "timeout_sec",
    "max_size_mb",
    "max_pixels",
    "extra_headers",
]

ENGINE_FIELDS = [
    "enabled",
    "id",
    "name",
    "engine",
    "api_key",
    "base_url",
    "timeout_sec",
    "extra_headers",
]


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_metadata() -> dict:
    return yaml.safe_load(METADATA_PATH.read_text(encoding="utf-8"))


def _walk_nodes(node: dict, path: str = ""):
    """Yield `(path, node)` for every schema node that declares a `type`."""
    if not isinstance(node, dict):
        return
    if "type" in node:
        yield path, node
    for key, child in (node.get("items") or {}).items():
        yield from _walk_nodes(child, f"{path}.{key}" if path else key)
    for key, child in (node.get("templates") or {}).items():
        yield from _walk_nodes(child, f"{path}.templates.{key}" if path else key)


class TestSchemaShape:
    def test_top_level_keys(self):
        schema = load_schema()
        assert list(schema) == [
            "search_sources",
            "search",
            "image_url",
            "download_timeout_sec",
            "tool_hint_enabled",
            "log_level",
        ]

    def test_vision_template_fields(self):
        schema = load_schema()
        template = schema["search_sources"]["templates"]["vision"]
        assert list(template["items"]) == VISION_FIELDS
        # 旧插件的 desc / description / tags 等字段不应再出现在配置面
        for legacy in ("desc", "description", "tags", "vision_mode", "max_retry"):
            assert legacy not in template["items"]

    def test_engine_template_fields(self):
        schema = load_schema()
        template = schema["search_sources"]["templates"]["engine"]
        assert list(template["items"]) == ENGINE_FIELDS
        # engine 模板不提供源级体积/像素上限
        assert "max_size_mb" not in template["items"]
        assert "max_pixels" not in template["items"]

    def test_search_defaults(self):
        schema = load_schema()
        items = schema["search"]["items"]
        expected = {
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
        assert {key: items[key]["default"] for key in items} == expected

    def test_image_url_defaults(self):
        schema = load_schema()
        items = schema["image_url"]["items"]
        assert {key: items[key]["default"] for key in items} == {
            "enabled": False,
            "dir": "",
            "port": 8123,
            "public_base_url": "",
        }

    def test_every_node_has_type_description_default(self):
        schema = load_schema()
        for path, node in _walk_nodes(schema):
            assert node["type"] in SUPPORTED_TYPES, path
            assert node.get("description"), path
            assert "default" in node, path

    def test_template_keys_marked_by_schema(self):
        schema = load_schema()
        templates = schema["search_sources"]["templates"]
        assert set(templates) == {"vision", "engine"}
        for name, template in templates.items():
            assert template["name"], name
            assert template["hint"], name


class TestMetadataAndDocs:
    def test_metadata_version_and_repo(self):
        metadata = load_metadata()
        assert metadata["name"] == "astrbot_plugin_image_search"
        assert metadata["version"] == "2.1.0"
        assert metadata["repo"].endswith("cmdst/astrbot_plugin_image_search")
        assert metadata["astrbot_version"] == ">=4.16,<5"

    def test_readme_mentions_core_contract(self):
        text = README_PATH.read_text(encoding="utf-8")
        for token in (
            "image_search",
            "intent",
            "search_sources",
            "image_url",
            "SauceNAO",
            "trace.moe",
        ):
            assert token in text, token

    def test_readme_documents_every_schema_top_level_key(self):
        schema = load_schema()
        text = README_PATH.read_text(encoding="utf-8")
        for key in schema:
            assert key in text, key

    def test_changelog_has_current_version(self):
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
        assert "2.1.0" in text
        assert "intent" in text

    def test_legacy_migration_files_are_gone(self):
        for name in ("MIGRATION.md", "migrate_v1_7.py"):
            assert not (PLUGIN_DIR / name).exists(), name
        readme = README_PATH.read_text(encoding="utf-8")
        # 历史说明可以提名字，但不能再给读者迁移入口/链接
        assert "](MIGRATION.md)" not in readme
        assert "python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py" not in readme
