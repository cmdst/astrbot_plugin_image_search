#!/usr/bin/env python3
"""migrate_v1_7.py -- v1.7 (识图路由) -> v2.0 (搜图路由 Agent) 配置迁移。

把旧插件 `astrbot_plugin_vision_router` 的配置迁移成新插件
`astrbot_plugin_image_search` v2.0.0 的配置（schema 见同目录
`_conf_schema.json`，契约见 `PRD.md` §6）。脚本**独立可执行**，不导入插件包，
只依赖标准库：

    python migrate_v1_7.py [--old PATH] [--new PATH] [--dry-run]

字段映射（PRD §6.2，逐条冻结；机制说明以 PRD 为准）：

保留（语义 1:1 对齐）:
- `route.llm_provider`        -> `search_sources[0]`（`__template_key="vision"`）
  生成 `{__template_key: "vision", enabled: true, id: "migrated_route_llm",
  name: "迁移：v1.7 路由提供商", provider_ref: <原值>}`。理由：v1.7 的
  `mode="search"` 正是把原图交给该提供商联网搜图，与新插件 vision 源同义；
  其余字段（web_search/timeout_sec/max_size_mb/max_pixels/extra_headers）
  留 schema 默认值，不搬运旧插件的 analyze 专用字段。
  **F2**：`__template_key` 必须写入（engine 源取 `"engine"`）。面板的
  template_list 校验读 `__template_key`/`template`，缺了就报「缺少模板选择，
  需要 __template_key」，迁移产物无法在面板保存。
- `route.max_rounds`          -> `search.max_rounds`（越界截断到 0~10）
- `route.max_size_mb`         -> `search.max_size_mb`
- `route.max_pixels`          -> `search.max_pixels`
- `route.cache_ttl_sec`       -> `search.cache_ttl_sec`
- `route.health_stats`        -> `search.health_stats`
- `image_url.*`               -> `image_url.*`（同名直搬，末尾斜杠去除）
- `download_timeout_sec` / `tool_hint_enabled` / `log_level` -> 同名直搬

配置保真（**F3**，线上升级路径）: 目标配置可能已被 AstrBot 按 schema
**预生成**（每个键都恰好等于 schema 默认值）。首次迁移（该输出路径还没有
迁移报告）时逐键判定：**目标键缺失，或现值仍等于 schema 默认值 → 写入 v1.7
映射值**（报告记 `<键>_restored_from_v1_7`）；**用户已改成非默认值 → 保留用户的**
（记 `kept_existing`）。已有迁移报告的重跑一律保留目标现值。这样既不会把旧
v1.7 的 `image_url.dir` / `public_base_url` 静默打成空串，也不会把用户刻意
设回默认值的键再改回去。

丢弃（附理由）:
- `route.routing_timeout_sec`：服务于 v1.7 analyze 的**关键词预路由**
  （硬边界① 已删除预路由），新插件没有任何路由 LLM 决策环节，无可映射语义。
- `ais`（整节，含 provider 引用条目与 custom 自定义端点条目）：v1.7 的
  `ais` 池服务于 analyze 的「按标签预路由 + 质量不满意换 AI」，
  1) 其专属字段（desc/tags/vision_mode/reasoning_effort/max_retry/thumbnail）
     在新插件里**没有目标位置**，强行映射等于把 analyze 语义夹带进 v2.0
     （违反硬边界①，且违反硬边界③ 的「彻底删除 analyze 识图池」）；
  2) 池内多数条目并不具备联网搜图能力，静默搬进 vision 池会让 `image_search`
     用非联网模型冒充搜图源，属静默降质；
  3) 条目里的 `api_base`/`api_key` 属于旧池的**成本与配额**，搬进 vision 源后
     会被真实调用并产生费用，静默迁移属越权变更用户成本预期。
  真正承担联网搜图的那一条提供商已由 `route.llm_provider` 覆盖，不重复产出。
  被丢弃条目全部**快照进迁移报告**（`api_key` 只记前 4 位 + `***`），
  需要它的用户按 `vision` 模板手工重建（provider_ref，或
  api_base + api_key + model 四填）。

新增（v1.7 无对应键）: `search.source_timeout_sec` / `search.total_timeout_sec`
/ `search.merge_enabled` / `search.merge_max_sources`，取 schema 默认值。

幂等性: 重复执行**不产生重复源条目** -- 按 `id` 去重，且**迁移报告**是该次迁移的
持久记录：只有「上一轮确实追加过迁移源」（报告 `source_added=true`）时，重跑才会
跳过追加，因此你手工删掉的那条不会被重新塞回来；而 AstrBot 预生成的 schema 默认
配置（空池、无报告）不会阻断迁移。已存在的新键字面量（含用户手改过的值）一律
**保留不覆盖**；缺失键按 schema 默认值补齐。同一输入 + 同一历史重复执行产出字节
一致（键序固定，配置本体不含时间戳）。

用法:
    python migrate_v1_7.py                       # 落盘迁移（旧->新默认路径）
    python migrate_v1_7.py --dry-run             # 只打印摘要，不写任何文件
    python migrate_v1_7.py --old OLD --new NEW   # 显式指定输入/输出
    python migrate_v1_7.py --config OLD --out NEW  # PRD §6.4 的等价写法

退出码（PRD §6.4 冻结）: 0 = 迁移成功，或 --dry-run；
        1 = 旧配置不存在 / 旧配置 JSON 非法 / 写入失败。
        所有失败路径的错误与指引文案一律写 stderr，且不写任何文件；
        `--dry-run` 永不写文件。
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = PLUGIN_DIR.parents[2]  # data/plugins/<plugin>/ -> repo root

# KISS: keep defaults in _conf_schema.json (the frozen contract) instead of a
# second hand-maintained copy here; the schema file is the single source of
# truth that the WebUI, this script and the tests all read.
SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"

# v1.7 stores its config under AstrBot's per-plugin config dir
# (`data/config/<plugin_dir_name>_config.json`). Override with --old.
OLD_CONFIG_DEFAULT = (
    REPO_ROOT / "data" / "config" / "astrbot_plugin_vision_router_config.json"
)
# v2.0 output must land where AstrBot reads it for the *new* plugin dir name.
NEW_CONFIG_DEFAULT = (
    REPO_ROOT / "data" / "config" / "astrbot_plugin_image_search_config.json"
)
REPORT_DEFAULT = (
    REPO_ROOT / "data" / "config" / "astrbot_plugin_image_search_migration_report.json"
)

# v1.7 legacy plugin-internal path (PRD §6.1): a copy of the *global* AstrBot
# config that v1.x used as a provider fallback. Explicitly NOT read, NOT
# migrated, NOT modified by v2.0 -- listed here only to explain the alias.
LEGACY_INTERNAL_OLD = (
    REPO_ROOT
    / "data"
    / "plugins"
    / "astrbot_plugin_vision_router"
    / "data"
    / "cmd_config.json"
)

MIGRATED_SOURCE_ID = "migrated_route_llm"
MIGRATED_SOURCE_NAME = "迁移：v1.7 路由提供商"

# route.* -> search.* straight numeric/boolean copies (PRD §6.2).
ROUTE_TO_SEARCH_KEEP: dict[str, str] = {
    "max_rounds": "max_rounds",
    "max_size_mb": "max_size_mb",
    "max_pixels": "max_pixels",
    "cache_ttl_sec": "cache_ttl_sec",
    "health_stats": "health_stats",
}
SEARCH_INT_RANGES: dict[str, tuple[int, int]] = {
    # Only the keys the script clamps; other ranges are enforced at runtime by
    # the plugin's config normalization.
    "max_rounds": (0, 10),
    "max_size_mb": (1, 100),
    "max_pixels": (1048576, 67108864),
    "cache_ttl_sec": (0, 3600),
}

# Dropped v1.7 keys with a reason recorded in the migration report.
DROPPED_KEYS: dict[str, str] = {
    "route.routing_timeout_sec": (
        "analyze 关键词预路由专用（硬边界① 已删除预路由），v2.0 无路由 LLM 决策"
        "环节，无可映射语义"
    ),
}


def mask_key(value: object) -> str:
    """Mask a secret for the migration report.

    Args:
        value: Raw secret value.

    Returns:
        The first 4 characters followed by ``***``, or ``""`` when empty.
    """
    text = str(value or "")
    return f"{text[:4]}***" if text else ""


def load_schema_defaults() -> dict:
    """Read plugin defaults from ``_conf_schema.json`` (single source of truth).

    Returns:
        A flat mapping of config paths to default values, e.g.
        ``{"search.max_rounds": 2, "log_level": "info"}``.

    Raises:
        SystemExit: If the schema file is missing or not a JSON object.
    """
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[migrate] cannot read schema {SCHEMA_PATH}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    defaults: dict[str, object] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, spec in node.items():
            if not isinstance(spec, dict):
                continue
            path = f"{prefix}{key}"
            if spec.get("type") == "object" and isinstance(spec.get("items"), dict):
                walk(spec["items"], f"{path}.")
            else:
                defaults[path] = spec.get("default")

    walk(schema, "")
    return defaults


def load_config(path: Path) -> dict:
    """Read a config JSON file, tolerating a UTF-8 BOM (PRD §6.1).

    Args:
        path: Path of the config file.

    Returns:
        Parsed config dict; ``{}`` when the file is absent.

    Raises:
        ValueError: If the file exists but is not valid JSON.
    """
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"cannot read config {path}: {exc}") from exc
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config {path} is not valid JSON: {exc}") from exc
    return cfg if isinstance(cfg, dict) else {}


def clamp_int(value: object, low: int, high: int) -> tuple[int, bool]:
    """Coerce ``value`` to an int and clamp it into ``[low, high]``.

    Args:
        value: Candidate value from the old config.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        Tuple of ``(clamped_value, was_clamped)``.
    """
    try:
        num = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return low, True
    return (num, False) if low <= num <= high else (max(low, min(high, num)), True)


def build_vision_source(route: dict) -> tuple[dict, dict | None]:
    """Build the single migrated vision source from ``route.llm_provider``.

    Args:
        route: The old ``route`` section.

    Returns:
        ``(source, issue)`` where ``issue`` is ``None`` when a source was
        produced, otherwise a dict explaining why nothing was produced.
    """
    ref = str(route.get("llm_provider") or "").strip()
    if not ref or ref == "auto":
        # v1.7 `auto` means "pick a vision provider automatically", which has no
        # v2.0 equivalent (v2.0 never guesses a search source), so nothing is
        # carried over; the user is told to pick one explicitly.
        return {}, {
            "reason": "route.llm_provider 为空或为 auto（v1.7 自动选提供商无 v2.0 对应语义）",
            "snapshot": {"llm_provider": ref},
        }
    source = {
        # F2: the WebUI/panel template_list validator reads
        # `item.get("__template_key") or item.get("template")` and rejects an
        # entry without it ("缺少模板选择，需要 __template_key"), so a migrated
        # entry must carry it to be savable from the panel.
        "__template_key": "vision",
        "enabled": True,
        "id": MIGRATED_SOURCE_ID,
        "name": MIGRATED_SOURCE_NAME,
        "provider_ref": ref,
        "web_search": True,
        "timeout_sec": 0,
        "max_size_mb": 0,
        "max_pixels": 0,
        "extra_headers": {},
    }
    return source, None


def collect_ais_report(ais: object) -> tuple[list[dict], list[str]]:
    """Snapshot every dropped ``ais`` entry for the migration report.

    Args:
        ais: The raw ``ais`` section of the old config (expected list).

    Returns:
        ``(dropped_entries, notes)`` -- one masked snapshot per dropped entry
        plus human-readable notes about malformed content.
    """
    dropped: list[dict] = []
    notes: list[str] = []
    if ais is None:
        return dropped, notes
    if not isinstance(ais, list):
        notes.append("ais_not_a_list_dropped")
        return dropped, notes
    for index, entry in enumerate(ais):
        if not isinstance(entry, dict):
            notes.append(f"ais[{index}]_not_a_dict_dropped")
            continue
        snapshot = copy.deepcopy(entry)
        if snapshot.get("api_key"):
            snapshot["api_key"] = mask_key(snapshot["api_key"])
        template_key = str(snapshot.get("__template_key") or "")
        if template_key == "custom":
            reason = (
                "custom 端点无联网能力标识，且其 api_base/api_key 属于旧池成本与"
                "配额，搬进 vision 源会被真实调用产生费用；需要请按 vision 模板"
                "手工重建（api_base + api_key + model + web_search）"
            )
        elif template_key == "provider":
            reason = (
                "provider 引用条目服务于 analyze 的按标签预路由 + 换 AI 语义，其"
                " desc/tags/vision_mode/reasoning_effort/max_retry/thumbnail 在"
                " v2.0 无目标字段；且池内多数条目不具联网搜图能力，搬入会静默降质"
            )
        else:
            reason = (
                "条目缺少 __template_key 或模板未知，无法判定语义，按整节丢弃策略处理"
            )
        if template_key == "provider" and snapshot.get("provider_ref") == "豆包/doubao":
            notes.append("ais_provider_entry_dedup_with_route_llm_provider")
        dropped.append(
            {
                "index": index,
                "template_key": template_key,
                "id": str(snapshot.get("id") or ""),
                "enabled": bool(snapshot.get("enabled")),
                "reason": reason,
                "snapshot": snapshot,
            }
        )
    return dropped, notes


def preserve_existing(
    new_cfg: dict,
    current: object,
    defaults: dict,
    report: dict,
    takeover_defaults: bool,
) -> None:
    """Reconcile the migrated values with whatever the target config already has.

    F3 (config preservation): AstrBot may pre-generate the v2.0 config from the
    schema, so every key already exists but holds the schema default. Blindly
    keeping those values would silently wipe the real v1.7 settings (e.g.
    ``image_url.dir`` / ``public_base_url`` becoming ``""``). The rule is:

    - target key missing, or its current value still equals the schema default
      -> keep the **migrated (v1.7) value** (recorded as ``*_restored_from_v1_7``);
    - the user already changed it to a non-default value -> keep **theirs**;
    - no migration report yet (first run) -> the above takeover applies; a re-run
      after a completed migration always keeps the target values as-is, so a key
      the user deliberately set back to the default is not overwritten again.

    Args:
        new_cfg: Config being built (mutated in place).
        current: Existing v2.0 config read from the output path, if any.
        defaults: Flat schema-defaults map (``{"image_url.dir": ""}``...).
        report: Migration report (mutated in place).
        takeover_defaults: True on the first migration of this output path.
    """
    if not isinstance(current, dict):
        return
    for key in new_cfg:
        if key == "search_sources" or key not in current:
            continue
        current_value = current[key]
        if key in ("search", "image_url") and isinstance(current_value, dict):
            for sub_key in new_cfg[key]:
                if sub_key not in current_value:
                    continue
                path = f"{key}.{sub_key}"
                if takeover_defaults and current_value[sub_key] == defaults.get(path):
                    if new_cfg[key][sub_key] != current_value[sub_key]:
                        report["notes"].append(f"{path}_restored_from_v1_7")
                else:
                    new_cfg[key][sub_key] = current_value[sub_key]
                    report["kept_existing"].append(path)
        elif takeover_defaults and current_value == defaults.get(key):
            if new_cfg[key] != current_value:
                report["notes"].append(f"{key}_restored_from_v1_7")
        else:
            new_cfg[key] = copy.deepcopy(current_value)
            report["kept_existing"].append(key)


def migration_report_exists(report_path: Path) -> bool:
    """Whether this output path was already migrated (a report is on disk).

    Used by the F3 config-preservation rule: only the *first* migration of an
    output path may replace schema-default values with the v1.7 ones. Once a
    report exists, the target config is authoritative so a key the user
    deliberately set back to its default is never overwritten again.

    Args:
        report_path: Path of the migration report.

    Returns:
        True when a readable report written by this script exists.
    """
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(report, dict) and report.get("from_version") == "1.7"


def report_ran_before(report_path: Path) -> bool:
    """Whether a previous run of this script already appended the migrated source.

    The report is the durable record of what the migration did: a re-run only
    skips re-appending when the *previous* run actually appended the source.
    That way "user deleted it on purpose" and "last run had nothing to migrate"
    behave correctly and differently.

    Args:
        report_path: Path of the migration report.

    Returns:
        True when a readable report says the migrated source was added.
    """
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(report, dict)
        and report.get("from_version") == "1.7"
        and bool(report.get("source_added"))
    )


def merge_search_sources(
    existing: object, migrated: dict, report: dict, migration_ran_before: bool
) -> list:
    """Merge the migrated source into an existing pool without duplicating.

    The migrated source is appended when the pool does not contain it yet and no
    previous run ever added it (``migration_ran_before`` False). Re-runs after a
    successful migration therefore never re-append it -- the user stays in
    control of the pool. A schema-default config freshly generated by AstrBot
    (empty pool, no report) does **not** block the migration.

    Args:
        existing: Existing ``search_sources`` list from the output path.
        migrated: The migrated vision source (empty when nothing to add).
        report: Migration report (mutated in place).
        migration_ran_before: True when a previous run added the migrated source.

    Returns:
        The final ``search_sources`` list, user entries first.
    """
    sources = copy.deepcopy(existing) if isinstance(existing, list) else []
    sources = [entry for entry in sources if isinstance(entry, dict)]
    if not migrated:
        return sources
    for entry in sources:
        if str(entry.get("id") or "") == MIGRATED_SOURCE_ID:
            report["notes"].append("migrated_source_already_present")
            return sources
    if migration_ran_before:
        report["notes"].append("migrated_source_absent_kept_absent")
        return sources
    sources.append(migrated)
    report["source_added"] = True
    return sources


def build_new_config(
    old: dict,
    current: object,
    defaults: dict,
    report: dict,
    migration_ran_before: bool,
    takeover_defaults: bool,
) -> dict:
    """Build the full v2.0 config from the v1.7 config plus existing v2.0 state.

    Args:
        old: Parsed v1.7 config.
        current: Existing v2.0 config at the output path (``None`` if absent).
        defaults: Flat defaults map produced by :func:`load_schema_defaults`.
        report: Migration report (mutated in place).
        migration_ran_before: True when a previous run added the migrated source.
        takeover_defaults: True when this is the first migration of the output
            path, i.e. schema-default values may be replaced by v1.7 values (F3).

    Returns:
        The new config dict in the frozen schema key order.
    """
    route = old.get("route") if isinstance(old.get("route"), dict) else {}
    old_image_url = (
        old.get("image_url") if isinstance(old.get("image_url"), dict) else {}
    )

    search = {
        "max_rounds": defaults.get("search.max_rounds", 2),
        "source_timeout_sec": defaults.get("search.source_timeout_sec", 30),
        "total_timeout_sec": defaults.get("search.total_timeout_sec", 180),
        "merge_enabled": defaults.get("search.merge_enabled", True),
        "merge_max_sources": defaults.get("search.merge_max_sources", 3),
        "cache_ttl_sec": defaults.get("search.cache_ttl_sec", 300),
        "health_stats": defaults.get("search.health_stats", True),
        "max_size_mb": defaults.get("search.max_size_mb", 20),
        "max_pixels": defaults.get("search.max_pixels", 16777216),
    }
    for old_key, new_key in ROUTE_TO_SEARCH_KEEP.items():
        if old_key not in route:
            continue
        value = route[old_key]
        if isinstance(search[new_key], bool):
            search[new_key] = bool(value)
        else:
            low, high = SEARCH_INT_RANGES[new_key]
            clamped, was_clamped = clamp_int(value, low, high)
            if was_clamped:
                report["notes"].append(f"route.{old_key}_clamped_to_{clamped}")
            search[new_key] = clamped
        report["kept"].append(f"route.{old_key} -> search.{new_key}")

    image_url = {
        "enabled": bool(
            old_image_url.get("enabled", defaults.get("image_url.enabled", False))
        ),
        "dir": str(old_image_url.get("dir") or ""),
        "port": int(old_image_url.get("port") or defaults.get("image_url.port", 8123)),
        "public_base_url": str(old_image_url.get("public_base_url") or "").rstrip("/"),
    }
    if old_image_url:
        report["kept"].append("image_url.* -> image_url.*")

    new_cfg: dict = {
        "search_sources": [],
        "search": search,
        "image_url": image_url,
        "download_timeout_sec": int(
            old.get("download_timeout_sec", defaults.get("download_timeout_sec", 20))
        ),
        "tool_hint_enabled": bool(
            old.get("tool_hint_enabled", defaults.get("tool_hint_enabled", True))
        ),
        "log_level": old.get("log_level") or defaults.get("log_level", "info"),
    }
    for key in ("download_timeout_sec", "tool_hint_enabled", "log_level"):
        if key in old:
            report["kept"].append(key)

    migrated_source, source_issue = build_vision_source(route)
    if source_issue is not None:
        report["dropped"].append(
            {
                "key": "route.llm_provider",
                "reason": source_issue["reason"],
                "snapshot": source_issue["snapshot"],
            }
        )
    else:
        report["kept"].append(
            f"route.llm_provider -> search_sources[{MIGRATED_SOURCE_ID}] (vision)"
        )

    dropped, ais_notes = collect_ais_report(old.get("ais"))
    report["dropped"].extend(dropped)
    report["notes"].extend(ais_notes)
    if "ais" in old:
        report["dropped_keys"].append("ais")
    for key, reason in DROPPED_KEYS.items():
        section, _, name = key.partition(".")
        if isinstance(old.get(section), dict) and name in old[section]:
            report["dropped_keys"].append(key)
            report["dropped"].append(
                {"key": key, "reason": reason, "snapshot": {name: old[section][name]}}
            )

    # Existing v2.0 state wins (never overwrite user edits / user-added sources).
    current_sources = (
        current.get("search_sources") if isinstance(current, dict) else None
    )
    preserve_existing(new_cfg, current, defaults, report, takeover_defaults)
    new_cfg["search_sources"] = merge_search_sources(
        current_sources, migrated_source, report, migration_ran_before
    )
    return new_cfg


def diff_summary(old_cfg: dict, new_cfg: dict) -> str:
    """Render a unified diff between the old and new config (secrets masked).

    Args:
        old_cfg: Old config (masked copy is produced internally).
        new_cfg: New config.

    Returns:
        Unified diff text; ``""`` when both sides are identical.
    """
    old_lines = json.dumps(old_cfg, ensure_ascii=False, indent=2).splitlines()
    new_lines = json.dumps(new_cfg, ensure_ascii=False, indent=2).splitlines()
    return "\n".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile="v1.7(old)", tofile="v2.0(new)", lineterm=""
        )
    )


def mask_secrets(node: object) -> object:
    """Recursively mask ``api_key`` fields so diffs never leak secrets.

    Args:
        node: Any JSON-ish value.

    Returns:
        A copy with ``api_key`` values replaced by their masked form.
    """
    if isinstance(node, dict):
        return {
            key: mask_key(value) if key == "api_key" else mask_secrets(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [mask_secrets(item) for item in node]
    return node


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="migrate_v1_7.py",
        description="Migrate the v1.7 vision-router config to the v2.0 image-search config.",
    )
    parser.add_argument(
        "--old",
        "--config",
        dest="old",
        default=str(OLD_CONFIG_DEFAULT),
        help=f"legacy v1.7 config path (default: {OLD_CONFIG_DEFAULT})",
    )
    parser.add_argument(
        "--new",
        "--out",
        dest="new",
        default=str(NEW_CONFIG_DEFAULT),
        help=f"v2.0 config path to write (default: {NEW_CONFIG_DEFAULT})",
    )
    parser.add_argument(
        "--report",
        dest="report",
        default=str(REPORT_DEFAULT),
        help=f"migration report path (default: {REPORT_DEFAULT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plain-text migration summary only; write nothing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the migration.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (0 = migrated / dry-run / success, 1 = hard failure).
    """
    args = parse_args(argv)
    old_path = Path(args.old).expanduser()
    new_path = Path(args.new).expanduser()
    report_path = Path(args.report).expanduser()

    if not old_path.is_file():
        # PRD §6.4: missing input -> target path on stderr, exit code 1, write
        # nothing. The guidance text stays, so a human still learns what to do.
        print(f"[migrate] legacy v1.7 config not found: {old_path}", file=sys.stderr)
        print(
            "[migrate] nothing to migrate -- either this deployment never used "
            "astrbot_plugin_vision_router, or the config lives elsewhere; "
            "pass --old <path> to point at it.",
            file=sys.stderr,
        )
        print("[migrate] result: failed (exit 1, no file written)", file=sys.stderr)
        return 1

    try:
        old_cfg = load_config(old_path)
        current: object = load_config(new_path) if new_path.is_file() else None
    except ValueError as exc:
        # PRD §6.4: invalid JSON -> json error on stderr, exit code 1, no writes.
        print(f"[migrate] {exc}", file=sys.stderr)
        print("[migrate] result: failed (exit 1, no file written)", file=sys.stderr)
        return 1
    defaults = load_schema_defaults()
    report: dict = {
        "schema_version": 1,
        "from_version": "1.7",
        "to_version": "2.0.0",
        "source": str(old_path),
        "target": str(new_path),
        "kept": [],
        "kept_existing": [],
        "dropped": [],
        "dropped_keys": [],
        "source_added": False,
        "notes": [],
    }
    new_cfg = build_new_config(
        old_cfg,
        current,
        defaults,
        report,
        report_ran_before(report_path),
        not migration_report_exists(report_path),
    )
    report["source_count"] = len(new_cfg["search_sources"])

    added_keys = [key for key in new_cfg if key not in old_cfg]
    report["migrated_new_top_level_keys"] = added_keys

    print(f"[migrate] old config : {old_path}")
    print(f"[migrate] new config : {new_path}")
    print(f"[migrate] report     : {report_path}")
    print(
        f"[migrate] kept={len(report['kept'])} dropped={len(report['dropped'])} "
        f"added_top_level_keys={len(added_keys)} "
        f"sources={report['source_count']} "
        f"migrated_source_added={report['source_added']}"
    )
    for note in report["notes"]:
        print(f"[migrate] note: {note}")

    if args.dry_run:
        print("[migrate] --dry-run: no file written")
        print("----- diff (secrets masked) -----")
        print(diff_summary(mask_secrets(old_cfg), mask_secrets(new_cfg)))
        print("----- end diff -----")
        return 0

    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n"
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if new_path.is_file():
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(new_path, new_path.with_name(f"{new_path.name}.bak.{stamp}"))
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        print(f"[migrate] write failed: {exc}", file=sys.stderr)
        return 1

    print(f"[migrate] wrote config: {new_path}")
    print(f"[migrate] wrote report: {report_path}")
    print("[migrate] next: restart AstrBot (or reload the plugin) to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
