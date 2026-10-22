"""Contract tests for the v1.7 -> v2.0 config migration and the doc surface.

Scope (task t4): ``_conf_schema.json`` field-by-field fidelity against the
frozen PRD §2, ``metadata.yaml`` identity/version, ``migrate_v1_7.py`` CLI
behaviour (missing input, ``--dry-run``, idempotency), and the README /
MIGRATION documents. Implementation modules (main.py / search_core.py / ...)
are deliberately not imported here -- they belong to other tasks.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"
METADATA_PATH = PLUGIN_DIR / "metadata.yaml"
MIGRATE_PATH = PLUGIN_DIR / "migrate_v1_7.py"
README_PATH = PLUGIN_DIR / "README.md"
MIGRATION_PATH = PLUGIN_DIR / "MIGRATION.md"
PRD_PATH = PLUGIN_DIR / "PRD.md"


def load_migrate_module():
    """Import ``migrate_v1_7.py`` by path (the plugin dir is not a package).

    Returns:
        The imported module object.
    """
    spec = importlib.util.spec_from_file_location("migration_under_test", MIGRATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_schema() -> dict:
    """Read ``_conf_schema.json``.

    Returns:
        Parsed schema dict.
    """
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def old_config_sample() -> dict:
    """Build a representative v1.7 config covering every mapping branch.

    Returns:
        A v1.7-shaped config dict.
    """
    return {
        "route": {
            "max_rounds": 99,
            "llm_provider": "豆包/doubao",
            "routing_timeout_sec": 25,
            "max_size_mb": 20,
            "max_pixels": 16777216,
            "cache_ttl_sec": 60,
            "health_stats": True,
        },
        "ais": [
            {
                "__template_key": "provider",
                "enabled": True,
                "id": "豆包/doubao",
                "provider_ref": "豆包/doubao",
                "tags": ["general"],
            },
            {
                "__template_key": "custom",
                "enabled": True,
                "id": "my-endpoint",
                "api_base": "https://example.com/v1",
                "api_key": "sk-supersecret-value",
                "model": "some-model",
            },
            "not-a-dict",
        ],
        "image_url": {
            "enabled": False,
            "dir": "/root/AstrBot/data/imgserv/files",
            "port": 8123,
            "public_base_url": "http://example.com:8123/",
        },
        "download_timeout_sec": 20,
        "tool_hint_enabled": True,
        "log_level": "info",
    }


def run_migration(
    tmp_path: Path,
    old: dict | None,
    *extra: str,
    report_name: str = "report.json",
) -> tuple[int, Path, Path]:
    """Run ``migrate_v1_7.main`` against temp files.

    Args:
        tmp_path: pytest tmp_path fixture.
        old: Old config payload written to the input path; ``None`` writes nothing.
        *extra: Extra CLI flags (e.g. ``--dry-run``).
        report_name: File name of the migration report (use distinct names to
            simulate independent migration histories in one tmp_path).

    Returns:
        Tuple of ``(exit_code, new_config_path, report_path)``.
    """
    module = load_migrate_module()
    old_path = tmp_path / "old_config.json"
    new_path = tmp_path / "new_config.json"
    report_path = tmp_path / report_name
    if old is not None:
        old_path.write_text(
            "\ufeff" + json.dumps(old, ensure_ascii=False), encoding="utf-8"
        )
    code = module.main(
        [
            "--old",
            str(old_path),
            "--new",
            str(new_path),
            "--report",
            str(report_path),
            *extra,
        ]
    )
    return code, new_path, report_path


# ---------------------------------------------------------------------------
# _conf_schema.json vs frozen PRD §2
# ---------------------------------------------------------------------------


def test_schema_top_level_keys_and_types():
    schema = load_schema()
    assert list(schema.keys()) == [
        "search_sources",
        "search",
        "image_url",
        "download_timeout_sec",
        "tool_hint_enabled",
        "log_level",
    ]
    assert schema["search_sources"]["type"] == "template_list"
    assert schema["search_sources"]["default"] == []
    assert schema["search"]["type"] == "object"
    assert schema["image_url"]["type"] == "object"
    assert schema["download_timeout_sec"]["default"] == 20
    assert schema["tool_hint_enabled"]["default"] is True
    assert schema["log_level"]["default"] == "info"
    assert schema["log_level"]["options"] == ["debug", "info", "warning", "error"]


def test_schema_every_node_has_description_default_and_supported_type():
    schema = load_schema()
    supported = {
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

    def walk(node: dict, path: str) -> None:
        for key, spec in node.items():
            where = f"{path}{key}"
            assert isinstance(spec.get("description"), str) and spec["description"], (
                where
            )
            assert "hint" in spec and spec["hint"], where
            assert spec["type"] in supported, (where, spec["type"])
            if spec["type"] == "template_list":
                assert "default" in spec, where
                for template_key, template in spec["templates"].items():
                    assert template.get("name"), f"{where}.{template_key}"
                    walk(template["items"], f"{where}.{template_key}.")
            elif spec["type"] in ("object", "dict") and isinstance(
                spec.get("items"), dict
            ):
                # P7/P12: container nodes carry an explicit default too ({}),
                # while the effective values come from `items` (AstrBot's
                # _parse_schema recurses over `items` and ignores this default).
                assert "default" in spec, where
                assert spec["default"] == {}, where
                walk(spec["items"], f"{where}.")
            else:
                assert "default" in spec, where

    walk(schema, "")


def test_schema_vision_template_fields_match_prd():
    vision = load_schema()["search_sources"]["templates"]["vision"]["items"]
    assert list(vision.keys()) == [
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
    assert vision["provider_ref"]["_special"] == "select_provider"
    assert vision["enabled"]["default"] is True
    assert vision["id"]["default"] == ""
    assert vision["name"]["default"] == ""
    assert vision["provider_ref"]["default"] == ""
    assert vision["api_base"]["default"] == ""
    assert vision["api_key"]["default"] == ""
    assert vision["model"]["default"] == ""
    assert vision["web_search"]["default"] is True
    assert vision["timeout_sec"]["default"] == 0
    assert vision["max_size_mb"]["default"] == 0
    assert vision["max_pixels"]["default"] == 0
    assert vision["extra_headers"]["default"] == {}


def test_schema_engine_template_fields_match_prd():
    engine = load_schema()["search_sources"]["templates"]["engine"]["items"]
    assert list(engine.keys()) == [
        "enabled",
        "id",
        "name",
        "engine",
        "api_key",
        "base_url",
        "timeout_sec",
        "extra_headers",
    ]
    assert engine["engine"]["options"] == ["saucenao", "ascii2d", "trace_moe"]
    assert engine["engine"]["default"] == ""
    assert engine["base_url"]["default"] == ""
    assert engine["api_key"]["default"] == ""
    assert engine["timeout_sec"]["default"] == 0
    assert engine["extra_headers"]["default"] == {}
    for probe in ("saucenao", "ascii2d", "trace_moe"):
        assert probe in engine["engine"]["hint"]


def test_schema_engine_hint_documents_cloudflare_and_proxy():
    engine = load_schema()["search_sources"]["templates"]["engine"]
    text = (
        engine["hint"]
        + engine["items"]["engine"]["hint"]
        + engine["items"]["base_url"]["hint"]
    )
    assert "Cloudflare" in text or "cloudflare" in text
    assert "Just a moment" in text
    assert "api_key" in text
    assert "base_url" in text


def test_schema_search_section_defaults_match_prd():
    search = load_schema()["search"]["items"]
    assert list(search.keys()) == [
        "max_rounds",
        "source_timeout_sec",
        "total_timeout_sec",
        "merge_enabled",
        "merge_max_sources",
        "cache_ttl_sec",
        "health_stats",
        "max_size_mb",
        "max_pixels",
    ]
    assert search["max_rounds"]["default"] == 2
    assert search["source_timeout_sec"]["default"] == 30
    assert search["total_timeout_sec"]["default"] == 180
    assert search["merge_enabled"]["default"] is True
    assert search["merge_max_sources"]["default"] == 3
    assert search["cache_ttl_sec"]["default"] == 300
    assert search["health_stats"]["default"] is True
    assert search["max_size_mb"]["default"] == 20
    assert search["max_pixels"]["default"] == 16777216


def test_schema_image_url_section_defaults_match_prd():
    image_url = load_schema()["image_url"]["items"]
    assert list(image_url.keys()) == ["enabled", "dir", "port", "public_base_url"]
    assert image_url["enabled"]["default"] is False
    assert image_url["dir"]["default"] == ""
    assert image_url["port"]["default"] == 8123
    assert image_url["public_base_url"]["default"] == ""


def test_schema_removed_v17_sections_are_absent():
    schema = load_schema()
    assert "ais" not in schema
    assert "route" not in schema
    assert "mode" not in schema


def test_prd_carries_the_t6_t10_clarifications():
    """P1~P12 + F2/F3 clarifications must stay in the frozen PRD text.

    These are the exact items the t6/t10 reviews raised; dropping them would
    re-open the PRD-vs-implementation mismatch they were written to close.
    """
    text = PRD_PATH.read_text(encoding="utf-8")
    for marker in (
        "image_refs: list[str] | None = None",
        "P1 澄清",
        "P2（冻结澄清）",
        "P3 + P10",
        "P4（以 §4.7 为准）",
        "P5 放宽",
        "P6（冻结澄清）",
        "**P7**",
        "P8",
        "**P9**",
        "P11 补充（物化读入上限",
        "P13-a",
        "P13-b",
        "F2：",
        "F3 配置保真",
        "skipped:<id>:<error>",
        "当前没有可用的搜图源。",
    ):
        assert marker in text, marker
    # P2: the stale `challenge=` kwarg is gone from the images[] pseudocode.
    assert "challenge=any(a.challenge" not in text
    # P6: engine template is described as 8 fields, no source-level size caps.
    assert "engine 模板不提供 `max_size_mb` / `max_pixels`" in text


# ---------------------------------------------------------------------------
# metadata.yaml
# ---------------------------------------------------------------------------


def test_metadata_identity_and_version():
    text = METADATA_PATH.read_text(encoding="utf-8")
    fields = dict(re.findall(r"^([A-Za-z_]+):\s*(.*)$", text, flags=re.MULTILINE))
    assert fields["name"] == "astrbot_plugin_image_search"
    assert fields["name"] == PLUGIN_DIR.name, "name must equal the plugin dir name"
    assert fields["display_name"] == "搜图路由 Agent"
    assert fields["version"] == "2.0.0"
    assert fields["astrbot_version"] == '">=4.16,<5"'
    assert "image_search" in text
    assert "image_analyze" not in text
    # The removed analyze pool may only be named as something that was removed.
    assert "已在 v2.0 彻底移除" in text
    assert "analyze 识图池（ais 多 AI 池" in text


# ---------------------------------------------------------------------------
# migrate_v1_7.py
# ---------------------------------------------------------------------------


def test_missing_old_config_exits_one_with_stderr_guidance(tmp_path, capsys):
    """PRD §6.4: missing input -> path on stderr, exit code 1, no writes.

    The human-facing guidance must survive the exit-code change.
    """
    code, new_path, report_path = run_migration(tmp_path, None)
    captured = capsys.readouterr()
    assert code == 1
    assert not new_path.exists()
    assert not report_path.exists()
    missing = str(tmp_path / "old_config.json")
    assert missing in captured.err, "the missing path must be reported on stderr"
    assert "not found" in captured.err
    assert "--old <path>" in captured.err, "guidance text must remain on stderr"
    assert "no file written" in captured.err
    assert "not found" not in captured.out, "errors belong on stderr, not stdout"


def test_missing_old_config_exit_code_via_subprocess(tmp_path):
    """End-to-end guard on the real process exit code (PRD §6.4)."""
    missing = tmp_path / "does_not_exist.json"
    new_path = tmp_path / "new.json"
    report_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(MIGRATE_PATH),
            "--config",
            str(missing),
            "--new",
            str(new_path),
            "--report",
            str(report_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert str(missing) in proc.stderr
    assert "not found" in proc.stderr
    assert not new_path.exists()
    assert not report_path.exists()


def test_dry_run_writes_nothing(tmp_path):
    code, new_path, report_path = run_migration(
        tmp_path, old_config_sample(), "--dry-run"
    )
    assert code == 0
    assert not new_path.exists()
    assert not report_path.exists()


def test_migration_keeps_supported_fields_and_drops_analyze_pool(tmp_path):
    code, new_path, report_path = run_migration(tmp_path, old_config_sample())
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8"))
    assert list(cfg.keys()) == [
        "search_sources",
        "search",
        "image_url",
        "download_timeout_sec",
        "tool_hint_enabled",
        "log_level",
    ]
    assert "ais" not in cfg and "route" not in cfg
    # route.* -> search.*
    assert cfg["search"]["max_rounds"] == 10  # 99 clamped into 0..10
    assert cfg["search"]["max_size_mb"] == 20
    assert cfg["search"]["max_pixels"] == 16777216
    assert cfg["search"]["cache_ttl_sec"] == 60
    assert cfg["search"]["health_stats"] is True
    assert cfg["search"]["source_timeout_sec"] == 30
    assert cfg["search"]["total_timeout_sec"] == 180
    assert cfg["search"]["merge_enabled"] is True
    assert cfg["search"]["merge_max_sources"] == 3
    # route.llm_provider -> exactly one vision source
    sources = cfg["search_sources"]
    assert len(sources) == 1
    assert sorted(sources[0].keys()) == [
        "__template_key",
        "enabled",
        "extra_headers",
        "id",
        "max_pixels",
        "max_size_mb",
        "name",
        "provider_ref",
        "timeout_sec",
        "web_search",
    ]
    assert sources[0]["__template_key"] == "vision"
    assert sources[0]["id"] == "migrated_route_llm"
    assert sources[0]["provider_ref"] == "豆包/doubao"
    assert sources[0]["enabled"] is True
    assert sources[0]["web_search"] is True
    # straight copies
    assert cfg["image_url"]["port"] == 8123
    assert cfg["image_url"]["public_base_url"] == "http://example.com:8123"
    assert cfg["download_timeout_sec"] == 20
    assert cfg["tool_hint_enabled"] is True
    assert cfg["log_level"] == "info"
    # report: dropped ais entries with masked secrets and explicit reasons
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "ais" in report["dropped_keys"]
    dropped_provider = [
        e for e in report["dropped"] if e.get("template_key") == "provider"
    ]
    dropped_custom = [e for e in report["dropped"] if e.get("template_key") == "custom"]
    assert dropped_provider and dropped_custom
    assert all(entry["reason"] for entry in report["dropped"])
    custom_snapshot = dropped_custom[0]["snapshot"]
    assert "supersecret" not in str(custom_snapshot["api_key"])
    assert custom_snapshot["api_key"].startswith("sk-s")


def test_rerun_is_idempotent_and_preserves_user_edits(tmp_path):
    code, new_path, _ = run_migration(tmp_path, old_config_sample())
    assert code == 0
    first = json.loads(new_path.read_text(encoding="utf-8"))
    count_before = len(first["search_sources"])

    # Simulate a user edit plus a user-added engine source.
    first["search"]["max_rounds"] = 5
    first["search_sources"].append(
        {
            "__template_key": "engine",
            "id": "user-ascii2d",
            "engine": "ascii2d",
            "enabled": True,
        }
    )
    new_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")

    code, _, _ = run_migration(tmp_path, old_config_sample())
    assert code == 0
    second = json.loads(new_path.read_text(encoding="utf-8"))
    ids = [src["id"] for src in second["search_sources"]]
    assert ids == ["migrated_route_llm", "user-ascii2d"]
    assert len(second["search_sources"]) == count_before + 1
    assert second["search"]["max_rounds"] == 5, "user-edited value must survive"

    # Third run: still no growth.
    code, _, _ = run_migration(tmp_path, old_config_sample())
    assert code == 0
    third = json.loads(new_path.read_text(encoding="utf-8"))
    assert [src["id"] for src in third["search_sources"]] == ids


def test_migration_is_byte_stable_on_repeat(tmp_path):
    """A clean-slate re-run must reproduce the same bytes (same history input)."""
    module = load_migrate_module()
    old_path = tmp_path / "old_config.json"
    old_path.write_text(
        json.dumps(old_config_sample(), ensure_ascii=False), encoding="utf-8"
    )
    payloads = []
    for index in range(2):
        target = tmp_path / "new_config.json"
        target.unlink(missing_ok=True)
        report = tmp_path / f"report{index}.json"  # fresh history per run
        code = module.main(
            [
                "--old",
                str(old_path),
                "--new",
                str(target),
                "--report",
                str(report),
            ]
        )
        assert code == 0
        payloads.append(target.read_text(encoding="utf-8"))
    rewritten = json.loads(payloads[0])
    assert [src["id"] for src in rewritten["search_sources"]] == ["migrated_route_llm"]
    assert payloads[0] == payloads[1]


def test_rerun_after_successful_migration_does_not_re_add_deleted_source(tmp_path):
    """Once the migration ran, a user-deleted source stays deleted on re-runs."""
    code, new_path, _ = run_migration(tmp_path, old_config_sample())
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8"))
    cfg["search_sources"] = []
    new_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    code, _, report_path = run_migration(tmp_path, old_config_sample())
    assert code == 0
    assert json.loads(new_path.read_text(encoding="utf-8"))["search_sources"] == []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "migrated_source_absent_kept_absent" in report["notes"]


def schema_default_config() -> dict:
    """Build the v2.0 config AstrBot pre-generates from the schema alone.

    Returns:
        Config dict whose every value equals the schema default.
    """
    defaults = load_migrate_module().load_schema_defaults()
    return {
        "search_sources": [],
        "search": {
            key.split(".")[1]: value
            for key, value in defaults.items()
            if key.startswith("search.")
        },
        "image_url": {
            key.split(".")[1]: value
            for key, value in defaults.items()
            if key.startswith("image_url.")
        },
        "download_timeout_sec": defaults["download_timeout_sec"],
        "tool_hint_enabled": defaults["tool_hint_enabled"],
        "log_level": defaults["log_level"],
    }


def write_precreated_config(tmp_path: Path, cfg: dict, name: str = "new.json") -> Path:
    """Write a pre-generated target config (UTF-8 BOM, like AstrBot does).

    Args:
        tmp_path: pytest tmp_path fixture.
        cfg: Config payload.
        name: Target file name.

    Returns:
        Path of the written config.
    """
    path = tmp_path / name
    path.write_text("\ufeff" + json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_schema_default_output_does_not_block_the_migrated_source(tmp_path):
    """AstrBot may pre-create the v2.0 config with schema defaults (empty pool).

    That file has no ``migrated_route_llm`` entry and no migration report yet,
    so the migration source must still be appended -- otherwise the migration
    would silently produce an empty pool.
    """
    module = load_migrate_module()
    old_path = tmp_path / "old.json"
    old_path.write_text(
        json.dumps(old_config_sample(), ensure_ascii=False), encoding="utf-8"
    )
    new_path = write_precreated_config(tmp_path, schema_default_config())
    code = module.main(
        [
            "--old",
            str(old_path),
            "--new",
            str(new_path),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8-sig"))
    assert [src["id"] for src in cfg["search_sources"]] == ["migrated_route_llm"]


def test_pregenerated_target_config_keeps_v17_values(tmp_path):
    """F3: a schema-default target must not wipe the mapped v1.7 values.

    Regression for the online-upgrade path: AstrBot pre-generates the new
    plugin config (every key = schema default, e.g. ``image_url.dir=""``), then
    the user runs the migration. Blindly keeping the existing values silently
    blanked the user's image host settings.
    """
    old = old_config_sample()  # dir=/root/AstrBot/data/imgserv/files, ...
    old_path = tmp_path / "old.json"
    old_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    new_path = write_precreated_config(tmp_path, schema_default_config())
    report_path = tmp_path / "report.json"

    code = load_migrate_module().main(
        [
            "--old",
            str(old_path),
            "--new",
            str(new_path),
            "--report",
            str(report_path),
        ]
    )
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8-sig"))
    assert cfg["image_url"]["dir"] == "/root/AstrBot/data/imgserv/files"
    assert cfg["image_url"]["public_base_url"] == "http://example.com:8123"
    assert cfg["image_url"]["enabled"] is False
    assert cfg["image_url"]["port"] == 8123
    # v1.7 cache ttl (60) must win over the schema default (300)
    assert cfg["search"]["cache_ttl_sec"] == 60
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "image_url.dir_restored_from_v1_7" in report["notes"]
    assert "image_url.public_base_url_restored_from_v1_7" in report["notes"]


def test_pregenerated_target_config_keeps_user_non_default_values(tmp_path):
    """F3: values the user already changed away from the default are kept."""
    old_path = tmp_path / "old.json"
    old_path.write_text(
        json.dumps(old_config_sample(), ensure_ascii=False), encoding="utf-8"
    )
    pregenerated = schema_default_config()
    pregenerated["image_url"]["public_base_url"] = "https://img.my.domain"
    pregenerated["image_url"]["dir"] = "/srv/user-images"
    pregenerated["search"]["merge_max_sources"] = 5
    pregenerated["log_level"] = "debug"
    new_path = write_precreated_config(tmp_path, pregenerated)
    report_path = tmp_path / "report.json"

    code = load_migrate_module().main(
        [
            "--old",
            str(old_path),
            "--new",
            str(new_path),
            "--report",
            str(report_path),
        ]
    )
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8-sig"))
    assert cfg["image_url"]["public_base_url"] == "https://img.my.domain"
    assert cfg["image_url"]["dir"] == "/srv/user-images"
    assert cfg["search"]["merge_max_sources"] == 5
    assert cfg["log_level"] == "debug"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "image_url.public_base_url" in report["kept_existing"]
    assert "log_level" in report["kept_existing"]


def test_migrated_source_passes_panel_template_validation(tmp_path):
    """F2: the panel rejects a template_list entry without ``__template_key``.

    Mirrors ``astrbot/dashboard/services/config_service.py``: it reads
    ``item.get("__template_key") or item.get("template")`` and errors with
    「缺少模板选择」 when both are empty.
    """
    code, new_path, _ = run_migration(tmp_path, old_config_sample())
    assert code == 0
    sources = json.loads(new_path.read_text(encoding="utf-8"))["search_sources"]
    assert len(sources) == 1
    assert sources[0]["__template_key"] == "vision"

    templates = load_schema()["search_sources"]["templates"]
    errors = []
    for index, item in enumerate(sources):
        template_key = item.get("__template_key") or item.get("template")
        if not template_key:
            errors.append(f"缺少模板选择 search_sources[{index}]: 需要 __template_key")
        elif template_key not in templates:
            errors.append(f"未知模板 search_sources[{index}]: {template_key}")
    assert errors == []

    # Negative control: without the key the very same check fires.
    without_key = {k: v for k, v in sources[0].items() if k != "__template_key"}
    assert not (without_key.get("__template_key") or without_key.get("template"))


def test_unmigratable_llm_provider_rerun_does_not_invent_source(tmp_path):
    old = old_config_sample()
    old["route"]["llm_provider"] = "auto"
    code, new_path, report_path = run_migration(
        tmp_path, old, report_name="report_auto.json"
    )
    assert code == 0
    cfg = json.loads(new_path.read_text(encoding="utf-8"))
    assert cfg["search_sources"] == []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert any("route.llm_provider" == entry.get("key") for entry in report["dropped"])
    code, _, _ = run_migration(tmp_path, old, report_name="report_auto.json")
    assert code == 0
    assert json.loads(new_path.read_text(encoding="utf-8"))["search_sources"] == []


def test_invalid_old_config_exits_nonzero(tmp_path, capsys):
    """PRD §6.4: invalid JSON -> json error on stderr, exit code 1, no writes."""
    module = load_migrate_module()
    old_path = tmp_path / "broken.json"
    old_path.write_text("{not json", encoding="utf-8")
    code = module.main(
        [
            "--old",
            str(old_path),
            "--new",
            str(tmp_path / "new.json"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert not (tmp_path / "new.json").exists()
    assert not (tmp_path / "report.json").exists()
    assert "not valid JSON" in captured.err
    assert "broken.json" in captured.err
    assert "not valid JSON" not in captured.out


def test_invalid_old_config_exit_code_via_subprocess(tmp_path):
    """End-to-end guard on the real process exit code for invalid JSON."""
    old_path = tmp_path / "broken.json"
    old_path.write_text("{not json", encoding="utf-8")
    new_path = tmp_path / "new.json"
    report_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(MIGRATE_PATH),
            "--config",
            str(old_path),
            "--new",
            str(new_path),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "not valid JSON" in proc.stderr
    assert not new_path.exists()
    assert not report_path.exists()


def test_default_paths_point_at_astrbot_config_dir():
    module = load_migrate_module()
    assert module.OLD_CONFIG_DEFAULT.name == "astrbot_plugin_vision_router_config.json"
    assert module.NEW_CONFIG_DEFAULT.name == "astrbot_plugin_image_search_config.json"
    assert module.OLD_CONFIG_DEFAULT.parent.name == "config"
    assert module.NEW_CONFIG_DEFAULT.parent.name == "config"


def test_prd_flag_aliases_are_equivalent(tmp_path):
    """PRD §6.4 spells the flags --config/--out; the task contract --old/--new."""
    module = load_migrate_module()
    old_path = tmp_path / "old.json"
    old_path.write_text(
        json.dumps(old_config_sample(), ensure_ascii=False), encoding="utf-8"
    )
    outputs = []
    for index, (flag_old, flag_out) in enumerate(
        (("--old", "--new"), ("--config", "--out"))
    ):
        target = tmp_path / f"out{index}.json"
        code = module.main(
            [
                flag_old,
                str(old_path),
                flag_out,
                str(target),
                "--report",
                str(tmp_path / f"report{index}.json"),
            ]
        )
        assert code == 0
        outputs.append(target.read_text(encoding="utf-8"))
    assert outputs[0] == outputs[1]


# ---------------------------------------------------------------------------
# README.md / MIGRATION.md
# ---------------------------------------------------------------------------


def test_readme_covers_vision_examples_and_engines():
    text = README_PATH.read_text(encoding="utf-8")
    for anchor in ("Gemini", "豆包", "provider_ref", "api_base", "api_key", "model"):
        assert anchor in text, anchor
    for engine in ("saucenao", "ascii2d", "trace_moe"):
        assert engine in text, engine
    assert "Cloudflare" in text
    assert "image_url" in text
    assert "image_search" in text


def test_readme_has_no_analyze_era_narrative():
    """The README must not describe the deleted analyze pool as live behaviour."""
    text = README_PATH.read_text(encoding="utf-8")
    # The old tool name is allowed exactly once: on the rename note.
    rename_lines = [line for line in text.splitlines() if "image_analyze" in line]
    assert len(rename_lines) == 1, rename_lines
    assert "改为" in rename_lines[0] and "image_search" in rename_lines[0]
    # No "v1.x 旧行为" style heading.
    for heading in re.findall(r"^#{1,6}\s+(.*)$", text, flags=re.MULTILINE):
        assert "旧行为" not in heading, heading
        assert "识图池" not in heading, heading
    # Removed mechanisms may only appear on lines that mark them as removed.
    removal_markers = ("移除", "删除", "删掉", "不做", "不再", "无对应")
    for line in text.splitlines():
        if "质量不满意换 AI" in line or "关键词预路由" in line:
            assert any(marker in line for marker in removal_markers), line
    assert "`image_search` 是本插件**唯一**注册给主模型的 LLM 工具。" in text


def test_migration_doc_has_mapping_table_and_ais_removal_statement():
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ais 识图池已被彻底移除" in text
    assert "route.llm_provider" in text
    assert "search_sources" in text
    assert "| 旧键（v1.7） | 新键（v2.0） | 说明 |" in text
    assert "| 旧键（v1.7） | 动作 | 理由 |" in text
    assert "migrate_v1_7.py" in text
    # Four migration steps, in order.
    for anchor in ("第 0 步", "第 1 步", "第 2 步", "第 3 步"):
        assert anchor in text, anchor
    assert "WebUI" in text and "重启" in text


def test_readme_config_keys_exist_in_schema():
    """Every `key` token documented in fenced JSON blocks must be a schema key."""
    schema = load_schema()
    known = set()

    def walk(node: dict) -> None:
        for key, spec in node.items():
            known.add(key)
            if spec["type"] in ("object", "dict") and isinstance(
                spec.get("items"), dict
            ):
                walk(spec["items"])
            elif spec["type"] == "template_list":
                for template in spec["templates"].values():
                    walk(template["items"])

    walk(schema)
    text = README_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "README must contain JSON config examples"
    for block in blocks:
        for key in re.findall(r'"([a-z_]+)"\s*:', block):
            if key == "__template_key":
                continue
            assert key in known, f"README documents unknown config key: {key}"
        # v1.7-only sections must never show up in a v2.0 example.
        assert '"ais"' not in block
        assert '"route"' not in block


def test_migration_doc_config_keys_exist_in_schema():
    """MIGRATION.md samples must only use real schema keys (+ __template_key)."""
    schema = load_schema()
    known = set()

    def walk(node: dict) -> None:
        for key, spec in node.items():
            known.add(key)
            if spec["type"] in ("object", "dict") and isinstance(
                spec.get("items"), dict
            ):
                walk(spec["items"])
            elif spec["type"] == "template_list":
                for template in spec["templates"].values():
                    walk(template["items"])

    walk(schema)
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "MIGRATION.md must contain the migrated-source example"
    for block in blocks:
        for key in re.findall(r'"([a-z_]+)"\s*:', block):
            if key == "__template_key":
                continue
            assert key in known, f"MIGRATION.md documents unknown config key: {key}"


def test_migration_doc_has_upgrade_section():
    """The upgraded-install scenario section must exist (F2/F3 write-up)."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "## 2. 升级已装插件的配置" in text
    assert "### 2.1 `__template_key`" in text
    assert '`__template_key` 取值' in text
    assert '| A 类视觉联网模型（`provider_ref`，或 `api_base` + `model`） | `"vision"` |' in text
    assert "| B 类图搜图引擎（SauceNAO / ascii2d / trace.moe） | `\"engine\"` |" in text
    assert "_restored_from_v1_7" in text
    assert "kept_existing" in text
