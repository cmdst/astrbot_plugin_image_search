"""search_core.py -- 搜图路由 Agent v2.0.0 配置归一化 / 源池 / 结果装配（纯函数，无 astrbot 依赖）。

职责边界（PRD §2 / §3 / §5 冻结契约）：
1. 默认值常量：`DEFAULT_SEARCH`（PRD §2.5 九键）、`DEFAULT_IMAGE_URL`（§2.6）、
   `DEFAULT_GLOBAL`（§2.1 顶层键）；
2. `load_config()`：读插件配置（优先 AstrBot data 目录，逐级回退，绝不把某个绝对
   路径写成唯一路径）；
3. `normalize_config()`：把 `search_sources` 归一到 **keyed dict**（容忍 dict / list /
   JSON 字符串三种形态），逐键校验类型与范围，非法值回落默认值 + 英文告警，不阻断加载；
4. `enabled_sources()` / `select_candidates()`：候选源顺序（PRD §1.2 冻结：auto 先
   engine 后 vision，数组顺序，不排序、不按健康度重排）；
5. `build_image_result()` / `build_results_payload()` / `merge_results()`：返回体逐字段
   装配与多源命中合并去重（PRD §1.4 / §1.5 / §3.4）；
6. `ResultCache` / `HealthStats`：结果缓存（§3.5）与源健康统计（同一节）。

本模块不 import astrbot 内部实现（仅用 `astrbot.api.logger`），便于离线单测。
日志一律英文（PRD §2.7）。

**与验收口径的显式取舍（供 verifier / reviewer 核对）**：PRD §2.4.1 冻结
「`id` 为空 → 自动生成 `{kind}_{下标}`」，因此缺 `id` 的条目**不**被丢弃（自动补 id）；
被判为非法并「剔出可用源集合」的情况**只**依据 §2.4.1~§2.4.4：`__template_key`/形态无法
识别、vision 源 `provider_ref` 与 `api_base`+`model` 均空、engine 源 `engine` 名不在枚举内
（v2.0.0 repair 已删除原先的 `desc` 判非法规则与 `_desc_declared` 标记）。非法条目保留在
`cfg["search_sources"]`（带 `valid=False`），以便按 PRD §5 出现在 `attempts` 里
（`outcome="skipped"`、`error="invalid_source"`），但**永不参与实际请求**。
字段表（§2.2/§2.3）之外的未知键一律丢弃并逐键 WARNING（含源 id 与键名）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from astrbot.api import logger as LOG

# ---------------------------------------------------------------------------
# 默认值常量（PRD §2.1 / §2.5 / §2.6 逐键冻结）
# ---------------------------------------------------------------------------

# PRD §2.5 search 小节（9 键）
DEFAULT_SEARCH: dict = {
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

# PRD §2.5 各键取值范围（越界 → 回落默认值 + 告警，PRD §2.8）
SEARCH_RANGES: dict = {
    "max_rounds": (0, 10),
    "source_timeout_sec": (5, 300),
    "total_timeout_sec": (30, 900),
    "merge_max_sources": (1, 5),
    "cache_ttl_sec": (0, 3600),
    "max_size_mb": (1, 100),
    "max_pixels": (1048576, 67108864),
}

# PRD §2.6 image_url 小节（内置只读图床；默认关闭）
DEFAULT_IMAGE_URL: dict = {
    "enabled": False,
    "dir": "",
    "port": 8123,
    "public_base_url": "",
}

# PRD §2.1 顶层键默认值
DEFAULT_GLOBAL: dict = {
    "search_sources": [],
    "search": dict(DEFAULT_SEARCH),
    "image_url": dict(DEFAULT_IMAGE_URL),
    "download_timeout_sec": 20,
    "tool_hint_enabled": True,
    "log_level": "info",
}

DOWNLOAD_TIMEOUT_RANGE = (5, 120)
LOG_LEVELS = ("debug", "info", "warning", "error")

# 源池枚举（PRD §2.3 / §2.4.4）
SEARCH_TYPES = ("auto", "vision", "engine")
ENGINE_NAMES = ("saucenao", "ascii2d", "trace_moe")

# 归一化接受的字段（PRD §2.2 / §2.3 字段表 + 模板标记 + 本模块内部标记）。
# 表外键一律丢弃并发 WARNING（v2.0.0 repair：禁止静默吞掉未知键，
# 例如给 engine 模板误加 size/pixels —— §2.3 没有这两个字段）。
KNOWN_SOURCE_KEYS: dict[str, frozenset] = {
    "vision": frozenset(
        {
            "enabled",
            "id",
            "name",
            "desc",
            "description",
            "provider_ref",
            "api_base",
            "api_key",
            "model",
            "web_search",
            "timeout_sec",
            "max_size_mb",
            "max_pixels",
            "extra_headers",
        }
    ),
    "engine": frozenset(
        {
            "enabled",
            "id",
            "name",
            "desc",
            "description",
            "engine",
            "api_key",
            "base_url",
            "timeout_sec",
            "extra_headers",
        }
    ),
}
# 模板标记与归一化产物自身的键（不是用户配置项，不告警）
INTERNAL_SOURCE_KEYS = frozenset(
    {"__template_key", "kind", "valid", "invalid_reason", "form", "model_ignored"}
)
ENGINE_DEFAULT_NAMES = {
    "saucenao": "SauceNAO",
    "ascii2d": "ascii2d",
    "trace_moe": "trace.moe",
}

# 工具参数约束（PRD §1.2）
MAX_IMAGE_REFS = 8
MAX_QUESTION_CHARS = 500
MAX_REF_CHARS = 200
MAX_SUMMARY_CHARS = 800

# 返回体固定文案（PRD §5 失败矩阵逐字）
TOOL_NAME = "image_search"
NO_REFS_SUMMARY = "请把图片路径或图片链接一起发给我。"
NO_REFS_HINT = "请把图片路径或图片链接一起发给我。"
EMPTY_POOL_SUMMARY = "当前没有可用的搜图源。"
EMPTY_POOL_HINT = "请在插件配置 search_sources 添加 vision 或 engine 源。"
CHALLENGE_HINT = "目标站点被 Cloudflare 拦截（人机校验），可配置自建反代 base_url 或提供 api_key 后重试。"
NO_PUBLIC_URL_HINT = "图搜图引擎需要公网图片 URL：请在插件配置开启 image_url.enabled（内置只读图床）后重试。"
INTERNAL_ERROR_SUMMARY = "搜图过程中出现内部错误。"
CHALLENGE_REASON = "目标站点返回 Cloudflare 人机校验页，未取得结果"
NO_PUBLIC_URL_REASON = "图搜图引擎缺少公网图片 URL（内置图床未启用）"
INVALID_SOURCE_REASON = "搜图源配置无效（invalid_source）"
MISS_REASON = "没有查到这张图的来源。"
# F13（t12 amend 语义）：**没有任何可尝试的实测尝试**（候选为空，或候选全部因
# invalid_source / no_public_url 被 skipped）→ 聚合状态记为 empty_pool，文案人话说明
# 「没有可尝试的源」，而不是误报「所有搜图源都失败了」（那些源根本没被请求过）。
NO_ATTEMPTS_ERROR = (
    "没有可尝试的搜图源（候选源要么配置无效，要么因缺少公网图片 URL 被跳过）。"
)
SKIP_REASON_TEXT = {
    "no_public_url": "图搜图引擎缺少公网图片 URL（内置图床未启用）",
    "invalid_source": "搜图源配置无效（invalid_source）",
    "image_too_large_for_source": "图片超出该源的体积/像素上限",
}

# 总预算耗尽的图片级文案（v2.0.0 repair：与「候选源为空」区分开）
TOTAL_TIMEOUT_ERROR = "本次调用总时限已耗尽，未尝试搜图源（total_timeout）。"

# 缓存键分隔符（PRD §3.5：sha256(图片字节) + "|" + search_type + "|" + question）
CACHE_KEY_SEP = "|"

# 引用不可读类错误码（PRD §1.4 `bad_refs` 判定）
UNREADABLE_ERRORS = (
    "bad_ref",
    "ssrf_blocked",
    "download_failed",
    "image_too_large",
)

# vision 命中判定（PRD §3.3 冻结）
VERDICT_HIT_RE = r"^\s*VERDICT\s*[:：]\s*HIT\b"
VERDICT_MISS_RE = r"^\s*VERDICT\s*[:：]\s*MISS\b"
MISS_MARKERS = (
    "无法识别",
    "无法判断",
    "无法确定",
    "看不出",
    "识别不出",
    "不确定这是什么",
    "我无法回答",
    "cannot identify",
    "can't identify",
    "unable to identify",
    "not able to identify",
)


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def astrbot_data_dir() -> Path:
    """解析 AstrBot data 目录。

    优先 AstrBot 官方路径工具（`astrbot.core.utils.path_utils`，新版仓库为
    `astrbot.core.utils.astrbot_path`），其次 `ASTRBOT_ROOT` 环境变量，再次从本文件
    位置推导（`<root>/data/plugins/<plugin>/search_core.py` → `<root>/data`），
    最后才回退仓库默认绝对路径。**绝不把某个绝对路径写成唯一路径。**

    Returns:
        data 目录 Path（可能不存在）。
    """
    import importlib

    for module_name in (
        "astrbot.core.utils.path_utils",
        "astrbot.core.utils.astrbot_path",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        getter = getattr(module, "get_astrbot_data_path", None)
        if not callable(getter):
            continue
        try:
            value = str(getter() or "").strip()
        except Exception:  # noqa: BLE001 -- 探测失败换下一个来源
            continue
        if value:
            return Path(value)
    env_root = os.environ.get("ASTRBOT_ROOT", "").strip()
    if env_root:
        return Path(env_root) / "data"
    derived = Path(__file__).resolve().parents[2]  # .../<root>/data/plugins/<plugin>
    if derived.name == "data":
        return derived
    return Path("/root/AstrBot/data")  # 最后的兼容回退（非唯一路径）


def plugin_config_path() -> Path:
    """插件配置落盘路径：`{data}/config/astrbot_plugin_image_search_config.json`。"""
    return astrbot_data_dir() / "config" / "astrbot_plugin_image_search_config.json"


def legacy_config_path() -> Path:
    """v1.7 旧插件配置路径（仅供启动期迁移提示，不读取、不迁移，PRD §6.4）。"""
    return astrbot_data_dir() / "config" / "astrbot_plugin_vision_router_config.json"


def load_config(explicit: dict | None = None) -> dict:
    """读取插件原始配置。

    - `explicit`（AstrBot 注入的插件配置对象）非空 → 直接使用（WebUI 已按 schema 补齐）；
    - 否则读 `plugin_config_path()`（UTF-8-sig，兼容 BOM），失败返回空 dict（不抛异常）。

    Args:
        explicit: AstrBot 传入的插件配置（dict-like）。

    Returns:
        原始配置 dict；无配置时返回 `{}`。
    """
    if isinstance(explicit, dict) and explicit:
        return dict(explicit)
    path = plugin_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except json.JSONDecodeError as exc:
        LOG.warning("plugin config is not valid json (%s): %s", path.name, exc)
        return {}
    if not isinstance(raw, dict):
        LOG.warning("plugin config root is not an object (%s)", path.name)
        return {}
    return raw


# ---------------------------------------------------------------------------
# 逐键校验工具
# ---------------------------------------------------------------------------


def _as_bool(value, default: bool, key: str) -> bool:
    """布尔归一化：接受 bool 与 true/false/1/0 文本；无法判定回落默认值并告警。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    LOG.warning("invalid bool for key %s: %r (fallback to %s)", key, value, default)
    return default


def _as_int(value, default: int, key: str, low: int, high: int) -> int:
    """整数归一化：非整数或越界 → 回落默认值并告警（PRD §2.8）。"""
    if isinstance(value, bool):
        LOG.warning("invalid int for key %s: %r (fallback to %s)", key, value, default)
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        LOG.warning("invalid int for key %s: %r (fallback to %s)", key, value, default)
        return default
    if number < low or number > high:
        LOG.warning(
            "out-of-range int for key %s: %r (range %s~%s, fallback to %s)",
            key,
            number,
            low,
            high,
            default,
        )
        return default
    return number


def _as_str(value, default: str = "") -> str:
    """字符串归一化：None → 默认值；其余 str() 后去首尾空白。"""
    if value is None:
        return default
    return str(value).strip()


def _as_dict(value) -> dict:
    """object 归一化：非 dict 回落空 dict。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_nonneg_int(value, default: int = 0) -> int:
    """非负整数归一化：负数/非法值回落默认值（源级限额字段用）。"""
    try:
        number = int(str(value).strip()) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


# ---------------------------------------------------------------------------
# search_sources：三种形态归一 + 源校验
# ---------------------------------------------------------------------------


def _source_items(raw_sources) -> list:
    """把 search_sources 的三种配置形态统一成「有序元素列表」。

    容忍（PRD §2.8 + 验收口径）：
    - `list`：WebUI `template_list` 常规形态；
    - `dict`：keyed dict（旧配置迁移 / 手工改配置）；
    - JSON 字符串：`"[{...}]"` 或 `'{"a": {...}}'`；
    其余（含 None / 数字）→ 视为空池并告警。
    """
    if isinstance(raw_sources, str):
        text = raw_sources.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            LOG.warning("search_sources json string is invalid: %s", exc)
            return []
        return _source_items(parsed)
    if isinstance(raw_sources, dict):
        items: list = []
        for key, value in raw_sources.items():
            if not isinstance(value, dict):
                LOG.warning(
                    "search_sources entry %r is not an object, skipped", str(key)
                )
                continue
            item = dict(value)
            item.setdefault("id", key)
            items.append(item)
        return items
    if isinstance(raw_sources, list):
        return list(raw_sources)
    if raw_sources in (None, ""):
        return []
    LOG.warning("search_sources is not a list/dict/json, treated as empty pool")
    return []


def _detect_kind(item: dict) -> str | None:
    """判定条目属于哪类源模板；无法识别返回 None（该条目被剔除）。"""
    template_key = _as_str(item.get("__template_key")).lower()
    if template_key in ("vision", "engine"):
        return template_key
    kind = _as_str(item.get("kind")).lower()
    if kind in ("vision", "engine"):
        return kind
    if "engine" in item:
        # 只要声明了 engine 键就按 engine 模板处理：空/非法值 → invalid_source
        # （PRD §2.3「非法值或空 → 该源 invalid，运行期跳过并记日志」）
        return "engine"
    if any(key in item for key in ("provider_ref", "api_base", "model", "web_search")):
        return "vision"
    return None


def _unique_source_id(desired: str, used: dict[str, int]) -> str:
    """源 id 去重（PRD §2.4.1）：冲突时追加 `_2`、`_3`（按出现次数）。"""
    if desired not in used:
        used[desired] = 1
        return desired
    suffix = used[desired] + 1
    while f"{desired}_{suffix}" in used:
        suffix += 1
    used[desired] = suffix
    used[f"{desired}_{suffix}"] = 1
    return f"{desired}_{suffix}"


def _warn_unknown_keys(kind: str, source_id: str, raw: dict) -> None:
    """对归一化会丢弃的未知键发 WARNING（含源 id 与键名），避免静默吞配置。

    Args:
        kind: `"vision"` / `"engine"`。
        source_id: 该源归一化后的 id。
        raw: 原始条目 dict。
    """
    known = KNOWN_SOURCE_KEYS.get(kind, frozenset()) | INTERNAL_SOURCE_KEYS
    for key in raw:
        if isinstance(key, str) and key not in known:
            LOG.warning(
                "search source %s: unknown key %r is ignored (template %s)",
                source_id,
                key,
                kind,
            )


def normalize_search_source(
    kind: str, index: int, raw: dict, used_ids: dict[str, int]
) -> dict:
    """归一化单个搜图源（PRD §2.2 / §2.3 / §2.4 冻结规则）。

    Args:
        kind: `"vision"` 或 `"engine"`。
        index: 数组下标（用于 id/name 自动生成）。
        raw: 原始条目 dict。
        used_ids: 已用 id 计数表（原地更新，用于去重）。

    Returns:
        归一化后的源 dict；含 `valid` / `invalid_reason` 标记（非法源不参与请求）。
    """
    source_id = _unique_source_id(_as_str(raw.get("id")) or f"{kind}_{index}", used_ids)
    _warn_unknown_keys(kind, source_id, raw)
    # v2.0.0 repair：删除 desc 判非法规则 —— 非法性**只**依据 PRD §2.4.1~§2.4.4
    # （模板可识别 / vision provider_ref 或 api_base+model / engine 名在枚举内）。
    # `desc`（及 `description`）仅作为非 PRD 的可选描述字段透传，不参与任何判定。
    desc = _as_str(raw.get("desc") or raw.get("description"))
    invalid_reason = ""

    if kind == "engine":
        engine = _as_str(raw.get("engine")).lower()
        if engine not in ENGINE_NAMES:
            invalid_reason = invalid_reason or "invalid_engine"
        default_name = ENGINE_DEFAULT_NAMES.get(engine, source_id)
        return {
            "__template_key": "engine",
            "kind": "engine",
            "id": source_id,
            "name": _as_str(raw.get("name")) or default_name,
            "desc": desc,
            "enabled": _as_bool(raw.get("enabled", True), True, f"{source_id}.enabled"),
            "valid": not invalid_reason,
            "invalid_reason": invalid_reason,
            "engine": engine,
            "api_key": _as_str(raw.get("api_key")),
            "base_url": _as_str(raw.get("base_url")),
            "timeout_sec": _as_nonneg_int(raw.get("timeout_sec")),
            "extra_headers": _as_dict(raw.get("extra_headers")),
        }

    provider_ref = _as_str(raw.get("provider_ref"))
    api_base = _as_str(raw.get("api_base"))
    api_key = _as_str(raw.get("api_key"))
    hand_model = _as_str(raw.get("model"))
    if provider_ref:
        form = "provider"
        # provider 形态忽略手填 model：model 唯一来源 = AstrBot 提供商配置（PRD §4.7）
        model = ""
    elif api_base and hand_model:
        form = "custom"
        model = hand_model
    else:
        form = ""
        model = hand_model
        invalid_reason = invalid_reason or "missing_endpoint"
    return {
        "__template_key": "vision",
        "kind": "vision",
        "id": source_id,
        "name": _as_str(raw.get("name")) or source_id,
        "desc": desc,
        "enabled": _as_bool(raw.get("enabled", True), True, f"{source_id}.enabled"),
        "valid": not invalid_reason,
        "invalid_reason": invalid_reason,
        "form": form,
        "provider_ref": provider_ref,
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "model_ignored": hand_model if provider_ref else "",
        "web_search": _as_bool(
            raw.get("web_search", True), True, f"{source_id}.web_search"
        ),
        "timeout_sec": _as_nonneg_int(raw.get("timeout_sec")),
        "max_size_mb": _as_nonneg_int(raw.get("max_size_mb")),
        "max_pixels": _as_nonneg_int(raw.get("max_pixels")),
        "extra_headers": _as_dict(raw.get("extra_headers")),
    }


def normalize_sources(raw_sources) -> dict[str, dict]:
    """把 search_sources 归一到 keyed dict（保序，key = 源 id）。

    非法条目（无法识别模板 / vision 端点缺失 / engine 名非法 / desc 空）**保留**，
    但 `valid=False`：它们永不发起请求，仅按 PRD §5 出现在 `attempts`。

    Args:
        raw_sources: list / dict / JSON 字符串形态的源池。

    Returns:
        `{id: 归一化源 dict}`，顺序与原数组一致。
    """
    sources: dict[str, dict] = {}
    used_ids: dict[str, int] = {}
    for index, item in enumerate(_source_items(raw_sources)):
        if not isinstance(item, dict):
            LOG.warning("search_sources[%d] is not an object, skipped", index)
            continue
        kind = _detect_kind(item)
        if kind is None:
            LOG.warning(
                "search_sources[%d] has no recognizable template (vision/engine), skipped",
                index,
            )
            continue
        source = normalize_search_source(kind, index, item, used_ids)
        if not source["valid"]:
            LOG.warning(
                "search source %s is invalid (%s), it will be skipped at run time",
                source["id"],
                source["invalid_reason"],
            )
        sources[source["id"]] = source
    return sources


def _engine_opts(sources: dict[str, dict]) -> dict[str, dict]:
    """构造 `cfg["_engine_opts"][engine]`（PRD §4.1 只读注入；首个启用源生效）。"""
    opts: dict[str, dict] = {}
    for source in sources.values():
        if source["kind"] != "engine":
            continue
        engine = source.get("engine") or ""
        if not engine or engine in opts:
            continue
        opts[engine] = {
            "base_url": source.get("base_url", ""),
            "api_key": source.get("api_key", ""),
            "extra_headers": dict(source.get("extra_headers") or {}),
        }
    return opts


# ---------------------------------------------------------------------------
# 配置归一化
# ---------------------------------------------------------------------------


def normalize_search(raw_search) -> dict:
    """归一化 `search` 小节（9 键，PRD §2.5 默认值与范围）。"""
    raw = _as_dict(raw_search)
    out: dict = {}
    for key, default in DEFAULT_SEARCH.items():
        if key not in raw:
            out[key] = default
            continue
        if isinstance(default, bool):
            out[key] = _as_bool(raw.get(key), default, key)
        else:
            low, high = SEARCH_RANGES[key]
            out[key] = _as_int(raw.get(key), default, key, low, high)
    return out


def normalize_image_url(raw_image_url) -> dict:
    """归一化 `image_url` 小节（PRD §2.6；默认关闭）。"""
    raw = _as_dict(raw_image_url)
    out = dict(DEFAULT_IMAGE_URL)
    out["enabled"] = _as_bool(raw.get("enabled", False), False, "image_url.enabled")
    out["dir"] = _as_str(raw.get("dir"))
    out["port"] = _as_int(raw.get("port", 8123), 8123, "image_url.port", 1, 65535)
    out["public_base_url"] = _as_str(raw.get("public_base_url")).rstrip("/")
    return out


def normalize_config(raw: dict | None) -> dict:
    """插件配置总归一化（PRD §2.8：逐键校验，非法回落默认值 + 告警，不阻断加载）。

    Args:
        raw: 原始配置（AstrBot 注入对象或 JSON dict）。

    Returns:
        规范化配置 dict：`search_sources` 为 keyed dict，另含 `_engine_opts`
        只读注入（PRD §4.1）。
    """
    raw = _as_dict(raw)
    sources = normalize_sources(
        raw.get("search_sources", DEFAULT_GLOBAL["search_sources"])
    )
    search = normalize_search(raw.get("search", DEFAULT_GLOBAL["search"]))
    image_url = normalize_image_url(raw.get("image_url", DEFAULT_GLOBAL["image_url"]))
    download_timeout = _as_int(
        raw.get("download_timeout_sec", DEFAULT_GLOBAL["download_timeout_sec"]),
        DEFAULT_GLOBAL["download_timeout_sec"],
        "download_timeout_sec",
        *DOWNLOAD_TIMEOUT_RANGE,
    )
    tool_hint = _as_bool(
        raw.get("tool_hint_enabled", DEFAULT_GLOBAL["tool_hint_enabled"]),
        DEFAULT_GLOBAL["tool_hint_enabled"],
        "tool_hint_enabled",
    )
    log_level = _as_str(raw.get("log_level") or DEFAULT_GLOBAL["log_level"]).lower()
    if log_level not in LOG_LEVELS:
        LOG.warning("invalid log_level %r (fallback to info)", raw.get("log_level", ""))
        log_level = "info"
    return {
        "search_sources": sources,
        "search": search,
        "image_url": image_url,
        "download_timeout_sec": download_timeout,
        "tool_hint_enabled": tool_hint,
        "log_level": log_level,
        "_engine_opts": _engine_opts(sources),
    }


# ---------------------------------------------------------------------------
# 源池视图与候选顺序
# ---------------------------------------------------------------------------


def _sources_of(cfg: dict) -> list[dict]:
    """取 cfg 中全部源（保序）；兼容未归一化的 list 形态。"""
    sources = (cfg or {}).get("search_sources") or {}
    if isinstance(sources, dict):
        return list(sources.values())
    return list(normalize_sources(sources).values())


def enabled_sources(cfg: dict, include_invalid: bool = False) -> list[dict]:
    """按配置顺序返回启用源。

    Args:
        cfg: 归一化配置。
        include_invalid: True 时包含 `valid=False` 的非法源（用于 attempts 记录）。

    Returns:
        源 dict 列表（数组顺序，不排序）。
    """
    out = []
    for source in _sources_of(cfg):
        if not source.get("enabled", True):
            continue
        if not source.get("valid", True) and not include_invalid:
            continue
        out.append(source)
    return out


def select_candidates(cfg: dict, search_type: str) -> list[dict]:
    """按 `search_type` 选择候选源（PRD §1.2 冻结顺序）。

    - `auto`：先全部启用 engine 源，再全部启用 vision 源（各自数组顺序）；
    - `engine` / `vision`：仅该类，数组顺序（为空时不回落另一类）；
    - 非法源一并返回（运行期记 `skipped`），由调用方按「是否有 `valid=True` 源」
      判定 `empty_pool`（PRD §5）。

    Args:
        cfg: 归一化配置。
        search_type: `auto` / `vision` / `engine`。

    Returns:
        候选源列表。
    """
    sources = enabled_sources(cfg, include_invalid=True)
    if search_type == "engine":
        return [s for s in sources if s["kind"] == "engine"]
    if search_type == "vision":
        return [s for s in sources if s["kind"] == "vision"]
    return [s for s in sources if s["kind"] == "engine"] + [
        s for s in sources if s["kind"] == "vision"
    ]


def source_timeout_sec(cfg: dict, source: dict) -> float:
    """该源生效超时（秒）：`src.timeout_sec`（>0）覆盖 `search.source_timeout_sec`。"""
    default = float((cfg.get("search") or {}).get("source_timeout_sec", 30))
    value = int(source.get("timeout_sec") or 0)
    return float(value) if value > 0 else default


def source_size_limits(cfg: dict, source: dict) -> tuple[int, int]:
    """该源生效图片上限 `(max_size_mb, max_pixels)`；0 表示沿用全局（PRD §2.4.6）。"""
    search = cfg.get("search") or {}
    max_size_mb = int(source.get("max_size_mb") or 0) or int(
        search.get("max_size_mb", 20)
    )
    max_pixels = int(source.get("max_pixels") or 0) or int(
        search.get("max_pixels", 16777216)
    )
    return max_size_mb, max_pixels


# ---------------------------------------------------------------------------
# 工具入参归一化（PRD §1.2）
# ---------------------------------------------------------------------------


def normalize_search_type(value, notes: list[str]) -> str:
    """`search_type` 归一化：大小写/空白不敏感；非法值按 `auto` 并记 note。"""
    text = _as_str(value).lower()
    if not text:
        return "auto"
    if text in SEARCH_TYPES:
        return text
    notes.append(f"invalid_search_type:{value}")
    return "auto"


def normalize_question(value, notes: list[str]) -> str:
    """`question` 归一化：非 str 强转；超 500 字截断并记 `question_truncated`。"""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = text.strip()
    if len(text) > MAX_QUESTION_CHARS:
        notes.append("question_truncated")
        return text[:MAX_QUESTION_CHARS]
    return text


def normalize_refs(image_refs, notes: list[str]) -> list[str]:
    """`image_refs` 归一化：保序去重、最多 8 张（超出记 `refs_truncated:8`）。

    非 list → 返回空列表（调用方返回 `status="no_refs"`，PRD §1.2）。
    空串元素保留（PRD §1.2.1：空串 → 该图 `ok=false`、`error="bad_ref"`）。

    Args:
        image_refs: 工具入参。
        notes: 追加说明列表（原地更新）。

    Returns:
        归一化后的引用列表。
    """
    if not isinstance(image_refs, list):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in image_refs:
        key = (item.strip() if isinstance(item, str) else str(item)).strip()
        if key in seen:
            continue
        seen.add(key)
        refs.append(item.strip() if isinstance(item, str) else item)
    if len(refs) > MAX_IMAGE_REFS:
        notes.append(f"refs_truncated:{MAX_IMAGE_REFS}")
        refs = refs[:MAX_IMAGE_REFS]
    return refs


def ref_display(ref) -> str:
    """引用在返回体中的展示形态：字符串化并截断到 200 字符（PRD §1.4）。"""
    text = ref if isinstance(ref, str) else str(ref)
    return text[:MAX_REF_CHARS]


# ---------------------------------------------------------------------------
# 结果装配（PRD §1.4 / §1.5）
# ---------------------------------------------------------------------------


def build_attempt(
    source: dict,
    outcome: str,
    *,
    error: str = "",
    challenge: bool = False,
    elapsed_ms: int = 0,
) -> dict:
    """构造 `attempts[]` 元素（PRD §1.4：仅 `miss` 计入轮数）。"""
    return {
        "kind": source["kind"],
        "name": source["name"],
        "id": source["id"],
        "engine": source.get("engine", "") if source["kind"] == "engine" else "",
        "outcome": outcome,
        "counted_round": outcome == "miss",
        "error": error,
        "challenge": bool(challenge),
        "elapsed_ms": int(elapsed_ms),
    }


def build_image_result(
    index: int,
    ref: str,
    *,
    ok: bool = False,
    transport: str = "",
    source: dict | None = None,
    match: dict | None = None,
    answer: str = "",
    merged_from: list[dict] | None = None,
    attempts: list[dict] | None = None,
    rounds_used: int = 0,
    cached: bool = False,
    error: str = "",
) -> dict:
    """构造 `images[]` 元素（键名与类型逐字段对齐 PRD §1.4）。

    **字段集合的取舍（供 verifier / reviewer 核对）**：以 §1.4 的 `images[]` 冻结字段表
    为准，因此**不含** `challenge`（伪代码 §3.1 里的 `challenge=` 参数不在该表中）；
    挑战语义统一由 `attempts[].challenge`（§1.4 attempts 表）+ `hint` 承载。
    `error` 在图片级失败时取 §1.2.1/§5 的参考错误码（`bad_ref` / `ssrf_blocked` /
    `download_failed` / `image_too_large`），源级失败时取人话归因（§5）。

    Args:
        index: 0 起算的图片序号。
        ref: 归一化后的引用（自动截断 200 字符）。
        ok: 该图是否命中。
        transport: `data_uri` / `url` / `""`。
        source: 最终采纳源 `{kind,name,id,engine}`；未命中为 None。
        match: engine 命中结构；vision 命中或未命中为 None。
        answer: vision 原始回答（已剥 VERDICT 首行）；否则 ""。
        merged_from: 合并来源明细。
        attempts: 逐源尝试明细。
        rounds_used: 该图消耗轮数。
        cached: 是否命中缓存。
        error: 失败原因（人话 / 参考错误码）；命中时为 ""。

    Returns:
        可直接序列化的图片结果 dict。
    """
    return {
        "index": int(index),
        "ref": ref_display(ref),
        "ok": bool(ok),
        "transport": transport or "",
        "source": source,
        "match": match,
        "answer": answer or "",
        "merged_from": list(merged_from or []),
        "attempts": list(attempts or []),
        "rounds_used": int(rounds_used),
        "cached": bool(cached),
        "error": error or "",
    }


def aggregate_status(images: list[dict]) -> str:
    """按 PRD §1.4 聚合 `status`（`no_refs` / `empty_pool` 由调用方显式指定）。"""
    if not images:
        return "failed"
    if all(image.get("error") == NO_ATTEMPTS_ERROR for image in images):
        return "empty_pool"  # F13：没有任何可尝试的实测尝试
    flags = [bool(image.get("ok")) for image in images]
    if all(flags):
        return "ok"
    if any(flags):
        return "partial"
    if all(image.get("error") in UNREADABLE_ERRORS for image in images):
        return "bad_refs"
    return "failed"


def build_results_payload(
    images: list[dict],
    *,
    status: str | None = None,
    search_type: str = "auto",
    question: str = "",
    summary: str = "",
    hint: str = "",
    elapsed_ms: int = 0,
    notes: list[str] | None = None,
) -> str:
    """装配工具返回值（JSON 字符串，PRD §1.4 顶层字段逐个出现）。

    Args:
        images: 逐图结果列表。
        status: 显式状态；None 时由 `aggregate_status()` 推导。
        search_type: 实际生效的源选择策略。
        question: 截断后的用户问题回显。
        summary: 面向主模型的可读摘要。
        hint: 运维/配置引导文案。
        elapsed_ms: 本次调用总耗时（毫秒）。
        notes: 补充说明列表。

    Returns:
        JSON 字符串（`ensure_ascii=False`）。
    """
    final_status = status or aggregate_status(images)
    payload = {
        "tool": TOOL_NAME,
        "ok": final_status == "ok",
        "status": final_status,
        "search_type": search_type,
        "question": question,
        "images": images,
        "summary": summary,
        "hint": hint,
        "elapsed_ms": int(elapsed_ms),
        "cached": bool(images) and all(bool(image.get("cached")) for image in images),
        "notes": list(notes or []),
    }
    return json.dumps(payload, ensure_ascii=False)


def build_failure_payload(
    status: str,
    summary: str,
    hint: str = "",
    *,
    search_type: str = "auto",
    question: str = "",
    notes: list[str] | None = None,
    elapsed_ms: int = 0,
    images: list[dict] | None = None,
) -> str:
    """装配「未能进入逐图流水线」的失败返回（`no_refs` / `empty_pool` / 内部错误）。

    Args:
        status: `no_refs` / `empty_pool` / `failed`。
        summary: 可读摘要。
        hint: 引导文案。
        search_type: 实际生效的源选择策略。
        question: 问题回显。
        notes: 补充说明。
        elapsed_ms: 耗时（毫秒）。
        images: 逐图结果（内部错误时可携带已处理结果）。

    Returns:
        JSON 字符串。
    """
    return build_results_payload(
        list(images or []),
        status=status,
        search_type=search_type,
        question=question,
        summary=summary,
        hint=hint,
        elapsed_ms=elapsed_ms,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 合并去重（PRD §3.4）
# ---------------------------------------------------------------------------


def _vision_title(answer: str) -> str:
    """vision 回答的短标题（`merged_from[].title`）：首个非空行，截断 60 字符。"""
    for line in (answer or "").splitlines():
        text = line.strip()
        if text:
            return text[:60]
    return ""


def _hit_dedupe_key(result: dict) -> tuple | None:
    """命中结果的去重键（PRD §3.4.2）。

    engine → `(title.lower, author.lower)`；vision → `("vision", sha1(answer.strip).hexdigest())`。
    """
    match = result.get("match")
    if match:
        return (
            "engine-match",
            _as_str(match.get("title")).lower(),
            _as_str(match.get("author")).lower(),
        )
    answer = (result.get("answer") or "").strip()
    if answer:
        return ("vision-answer", hashlib.sha1(answer.encode("utf-8")).hexdigest())
    return None


def merge_results(hits: list[tuple[dict, dict]], max_sources: int) -> dict | None:
    """多源命中合并去重（PRD §3.4）。

    Args:
        hits: `[(source, run_result)]`，顺序即命中发生顺序。
        max_sources: `search.merge_max_sources`（1~5）。

    Returns:
        合并结果 dict（`source` / `match` / `answer` / `merged_from` / `transport`）；
        无有效命中返回 None。
    """
    if not hits:
        return None
    head_source, head_result = hits[0]
    merged_from: list[dict] = []
    seen: set[tuple] = set()
    best_match: dict | None = None
    best_similarity = -1.0
    answer = ""
    for source, result in hits:
        if len(merged_from) >= max(int(max_sources), 1):
            break
        if not result.get("ok"):
            continue
        key = _hit_dedupe_key(result)
        if key is None or key in seen:
            continue
        seen.add(key)
        match = result.get("match") or None
        if match:
            similarity = match.get("similarity")
            score = float(similarity) if similarity is not None else 0.0
            if score > best_similarity:
                best_similarity = score
                best_match = match
            merged_from.append(
                {
                    "kind": source["kind"],
                    "name": source["name"],
                    "id": source["id"],
                    "similarity": similarity,
                    "title": _as_str(match.get("title")),
                }
            )
        else:
            text = (result.get("answer") or "").strip()
            answer = answer or text
            merged_from.append(
                {
                    "kind": source["kind"],
                    "name": source["name"],
                    "id": source["id"],
                    "similarity": None,
                    "title": _vision_title(text),
                }
            )
    if not merged_from:
        return None
    head_match = head_result.get("match") or None
    return {
        "source": {
            "kind": head_source["kind"],
            "name": head_source["name"],
            "id": head_source["id"],
            "engine": head_source.get("engine", "")
            if head_source["kind"] == "engine"
            else "",
        },
        "match": best_match if best_match is not None else head_match,
        "answer": answer,
        "merged_from": merged_from,
        "transport": head_result.get("transport") or "",
    }


# ---------------------------------------------------------------------------
# vision 命中判定（PRD §3.3）
# ---------------------------------------------------------------------------


def vision_verdict(answer: str) -> dict:
    """按 PRD §3.3 判定 vision 源回答的 HIT / MISS，并剥掉 `VERDICT:` 首行。

    Args:
        answer: 源返回的原始文本。

    Returns:
        `{"ok": bool, "miss": bool, "answer": str, "error": str}`。
    """
    import re

    text = (answer or "").strip()
    lines = text.splitlines()
    first_line = ""
    for index, line in enumerate(lines):
        if line.strip():
            first_line = line.strip()
            lines = lines[index + 1 :]
            break
    body = "\n".join(lines).strip()
    if re.match(VERDICT_HIT_RE, first_line, re.IGNORECASE):
        return {"ok": True, "miss": False, "answer": body or text, "error": ""}
    if re.match(VERDICT_MISS_RE, first_line, re.IGNORECASE):
        return {"ok": False, "miss": True, "answer": "", "error": ""}
    if len(text) < 4 or any(marker in text for marker in MISS_MARKERS):
        return {"ok": False, "miss": True, "answer": "", "error": ""}
    return {"ok": True, "miss": False, "answer": text, "error": ""}


# ---------------------------------------------------------------------------
# 摘要与引导文案（PRD §1.4 / §5）
# ---------------------------------------------------------------------------


def source_label(kind: str, name: str) -> str:
    """来源归属标注（PRD §3.4.4）：`图搜图引擎 X` / `视觉联网模型 X`。"""
    return f"{'图搜图引擎' if kind == 'engine' else '视觉联网模型'} {name}"


def has_real_attempt(attempts: list[dict]) -> bool:
    """是否发生过**真实尝试**（除 `skipped` 之外的任何 outcome 都算）。

    `hit` / `miss` / `error` / `timeout` / `challenge` 都代表真的请求过该源；
    只有 `skipped`（invalid_source / no_public_url / image_too_large_for_source）
    表示源从未被调用 —— 这种情况按 t12 的 F13 语义归为 `empty_pool`。

    Args:
        attempts: 逐源尝试明细。

    Returns:
        True 表示至少请求过一个源。
    """
    return any(a.get("outcome") != "skipped" for a in attempts)


def describe_attempts(attempts: list[dict]) -> str:
    """把逐源尝试明细翻成人话失败原因（PRD §5 失败矩阵）。"""
    if not attempts:
        return "没有可用的搜图源。"
    outcomes = [a.get("outcome") for a in attempts]
    if "challenge" in outcomes:
        return f"{CHALLENGE_REASON}。"
    if set(outcomes) <= {"skipped"}:
        # F13：被跳过的源从未被请求 → 统一按「最先失败原因」的人话表述收口
        # （逐源跳过明细由调用方写入 notes `skipped:<id>:<error>`）
        raw_reason = attempts[0].get("error") or ""
        reason = SKIP_REASON_TEXT.get(raw_reason, raw_reason or "unknown")
        return f"所有搜图源都失败了（{reason}）。"
    if all(outcome == "miss" for outcome in outcomes):
        return MISS_REASON
    first_error = next(
        (
            a.get("error") or ""
            for a in attempts
            if a.get("outcome") in ("error", "timeout")
        ),
        "",
    )
    if first_error:
        return f"所有搜图源都失败了（{first_error}）。"
    return "所有搜图源都失败了。"


def render_summary(images: list[dict]) -> str:
    """渲染面向主模型的可读摘要（中文，≤ 800 字，禁止塞原始 JSON）。"""
    lines: list[str] = []
    for image in images:
        number = int(image.get("index", 0)) + 1
        if not image.get("ok"):
            reason = image.get("error") or "未取得结果"
            if reason[-1] not in "。！？.!?":
                reason += "。"
            lines.append(f"图 {number}：{reason}")
            continue
        match = image.get("match") or {}
        answer = (image.get("answer") or "").strip()
        sources = image.get("merged_from") or []
        credited = [
            source_label(item.get("kind", ""), item.get("name", ""))
            for item in sources
            if item.get("kind") in ("engine", "vision")
        ]
        parts: list[str] = []
        if match:
            title = match.get("title") or ""
            author = match.get("author") or ""
            similarity = match.get("similarity")
            detail = f"疑似出自《{title}》" if title else "图搜图引擎命中"
            if similarity is not None:
                detail += f"（相似度 {float(similarity):.2f}%）"
            if author:
                detail += f"，作者：{author}"
            parts.append(detail)
            if not title and not author:
                links = match.get("links") or []
                if links:
                    parts.append(f"参考链接：{links[0]}")
        if answer:
            parts.append(" ".join(answer.split())[:200])
        if not parts:
            parts.append("已命中")
        if credited:
            parts.append("来源：" + " / ".join(dict.fromkeys(credited)))
        lines.append(f"图 {number}：" + "，".join(parts) + "。")
    text = "\n".join(lines)
    if len(text) > MAX_SUMMARY_CHARS:
        return text[: MAX_SUMMARY_CHARS - 1] + "…"
    return text


def render_hint(images: list[dict], status: str, search_type: str = "auto") -> str:
    """渲染运维/配置引导文案（PRD §1.4 hint / §5 矩阵）。

    v2.0.0 repair 后的两条触发条件（供 reviewer 对照 PRD 待 amend 处）：

    1. **Cloudflare 引导**：存在 `challenge` 尝试，且**没有任何** `ok` / `error` /
       `timeout`（即整单确实被风控挡住，而不是「某个源被拦、别的源正常回答/报错」）；
       `miss` 与 `skipped` 不阻断该引导。
    2. **图床引导**：存在被 `no_public_url` 跳过的源（只有 `valid=True` 的 engine 源
       会走到该分支）且**无任何命中**（说明命中缺口可由开启 `image_url.enabled` 补上）。

    Args:
        images: 逐图结果列表。
        status: 聚合状态（`empty_pool` / `no_refs` 直接给对应文案）。
        search_type: 实际生效的源选择策略（保留参数，便于后续按策略细化文案）。

    Returns:
        引导文案；无需引导时为 `""`。
    """
    if status == "no_refs":
        return NO_REFS_HINT
    attempts = [a for image in images for a in (image.get("attempts") or [])]
    has_hit = any(image.get("ok") for image in images)
    no_public_url_skipped = any(
        a.get("outcome") == "skipped" and a.get("error") == "no_public_url"
        for a in attempts
    )
    # 「真实尝试」= 除 skipped 外的任何 outcome（vision 侧真的被请求过）
    real_vision_attempt = any(
        a.get("kind") == "vision" and a.get("outcome") != "skipped" for a in attempts
    )
    if status == "empty_pool":
        # F13：无任何可尝试的实测尝试 —— 全因缺公网 URL 跳过 → 引导开图床，否则引导配源
        if no_public_url_skipped and not real_vision_attempt:
            return NO_PUBLIC_URL_HINT
        return EMPTY_POOL_HINT
    if not attempts:
        return ""
    outcomes = [a.get("outcome") for a in attempts]
    if (
        "challenge" in outcomes
        and not has_hit
        and "error" not in outcomes
        and "timeout" not in outcomes
    ):
        return CHALLENGE_HINT
    # F12：图床引导只在「纯 engine 候选池」或「没有任何 vision 真实尝试」时给出，
    # auto 下 vision 源真实失败/超时不得误报「开启 image_url.enabled」
    if (
        not has_hit
        and no_public_url_skipped
        and (search_type == "engine" or not real_vision_attempt)
    ):
        return NO_PUBLIC_URL_HINT
    return ""


# ---------------------------------------------------------------------------
# 结果缓存与健康统计（PRD §3.5）
# ---------------------------------------------------------------------------


class ResultCache:
    """逐图结果缓存：键 = `sha256(图片字节) | search_type | question`（PRD §3.5）。"""

    def __init__(self, ttl_sec: int = 0) -> None:
        self.ttl_sec = max(int(ttl_sec or 0), 0)
        self._items: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def make_key(data: bytes, search_type: str, question: str) -> str:
        """按 PRD §3.5 冻结公式计算缓存键。"""
        digest = hashlib.sha256(data).hexdigest()
        return CACHE_KEY_SEP.join((digest, search_type or "auto", question or ""))

    def get(self, key: str) -> dict | None:
        """取缓存（深拷贝）；过期或未命中返回 None。"""
        if self.ttl_sec <= 0:
            return None
        item = self._items.get(key)
        if not item:
            return None
        stored_at, payload = item
        if time.time() - stored_at > self.ttl_sec:
            self._items.pop(key, None)
            return None
        return copy.deepcopy(payload)

    def set(self, key: str, image_obj: dict) -> None:
        """写缓存（深拷贝；ttl<=0 时不写，即关闭缓存）。"""
        if self.ttl_sec <= 0 or not key:
            return
        self._items[key] = (time.time(), copy.deepcopy(image_obj))

    def clear(self) -> None:
        """清空缓存。"""
        self._items.clear()


class HealthStats:
    """各源成功率/耗时统计（不影响候选顺序，PRD §3.5）。"""

    def __init__(self) -> None:
        self._stats: dict[str, dict] = {}

    def record(self, attempts: list[dict]) -> None:
        """累计一批 attempts 的统计（原地更新）。"""
        for attempt in attempts or []:
            key = f"{attempt.get('id') or '?'}"
            entry = self._stats.setdefault(
                key,
                {
                    "attempts": 0,
                    "hits": 0,
                    "misses": 0,
                    "errors": 0,
                    "challenges": 0,
                    "elapsed_ms": 0,
                },
            )
            entry["attempts"] += 1
            entry["elapsed_ms"] += int(attempt.get("elapsed_ms") or 0)
            outcome = attempt.get("outcome")
            if outcome == "hit":
                entry["hits"] += 1
            elif outcome == "miss":
                entry["misses"] += 1
            elif outcome == "challenge":
                entry["challenges"] += 1
            elif outcome in ("error", "timeout"):
                entry["errors"] += 1

    def summary(self) -> str:
        """英文一行摘要（`terminate()` 输出用）；无数据返回空串。"""
        if not self._stats:
            return ""
        parts = []
        for key, entry in sorted(self._stats.items()):
            avg = entry["elapsed_ms"] // max(entry["attempts"], 1)
            parts.append(
                f"{key}(attempts={entry['attempts']} hits={entry['hits']} "
                f"misses={entry['misses']} errors={entry['errors']} "
                f"challenges={entry['challenges']} avg_ms={avg})"
            )
        return "source health: " + " ".join(parts)

    def reset(self) -> None:
        """清空统计。"""
        self._stats.clear()


# ---------------------------------------------------------------------------
# 运行期辅助
# ---------------------------------------------------------------------------

_PUBLIC_IP_PROBES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://ifconfig.me/ip",
)


def detect_public_ip(timeout_sec: float = 3.0) -> str:
    """探测公网出口 IP（供 `image_url.public_base_url` 留空时拼接图床 URL）。

    代理环境下外探返回的是代理出口 IP，无意义，直接回退本机地址（PRD §2.6
    「留空 → `http://<自动探测公网 IP>:<bound_port>`」）。

    Args:
        timeout_sec: 单个探测源超时（秒）。

    Returns:
        公网 IP 文本；探测不到时回退本机地址。
    """
    import ipaddress

    if os.environ.get("http_proxy") or os.environ.get("https_proxy"):
        return _local_ip()
    import urllib.request

    for probe in _PUBLIC_IP_PROBES:
        try:
            with urllib.request.urlopen(probe, timeout=timeout_sec) as resp:  # noqa: S310
                text = resp.read(64).decode("utf-8", "ignore").strip()
            return str(ipaddress.ip_address(text))
        except Exception:  # noqa: BLE001 -- 探测源失败换下一个
            continue
    return _local_ip()


def _local_ip() -> str:
    """本机对外地址（不实际发包）；失败回退环回地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def image_url_dir(cfg: dict) -> str:
    """图床落盘目录：`image_url.dir`；空 → `{plugin_data}/astrbot_plugin_image_search/imgserv`。

    Args:
        cfg: 归一化配置。

    Returns:
        目录绝对路径字符串。
    """
    configured = _as_str((cfg.get("image_url") or {}).get("dir"))
    if configured:
        return configured
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:  # noqa: BLE001 -- 官方路径不可用时回退 data 目录
        base = astrbot_data_dir() / "plugin_data"
    return str(base / "astrbot_plugin_image_search" / "imgserv")


def detect_migration_hint() -> bool:
    """新配置缺失但旧 v1.7 配置存在时返回 True（启动期只打一条 WARNING，PRD §6.4）。"""
    try:
        return (not plugin_config_path().exists()) and legacy_config_path().exists()
    except OSError:
        return False
