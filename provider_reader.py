"""provider_reader.py -- A 类（vision）源端点解析与 OpenAI 兼容调用（v2.1.0）。

保留（从旧插件 `astrbot_plugin_vision_router/provider_reader.py` 改造）：
1. **Provider 数据源读取**：优先 AstrBot 官方接口（`context.provider_manager`
   的属性 / `get_provider_config_by_id(..., merged=True)`），不可用时回退直读
   `{AstrBot data}/cmd_config.json`（路径由 `search_core.astrbot_data_dir()` 推导，
   不写死绝对路径）；
2. **provider 解析**：`resolve_source_endpoint()` 解析出 `api_base` / `api_key` /
   `model` / `vision` 标记 —— provider 形态取 AstrBot 提供商配置（支持反代提供商），
   custom 形态取源自带字段；
3. **HTTP 调用**：`httpx` 直连 OpenAI `chat/completions`，超时由调用方传入，
   失败抛异常由上层转成 `attempts[].outcome="error"`（不在此处吞异常）。

**旧版删除的代码与依据**（PRD §0 边界①「彻底删除 analyze 识图池」与 §3.1
「禁止任何形式的『质量不满意换 AI』『报错换 AI 直到成功』『兜底直判』逻辑」）：
- `pick_router_provider()`（按标签/默认 provider 选路由 LLM 的候选清单逻辑）：
  预路由与候选清单已删除，新插件只是**按配置顺序**把图交给 vision 源，无选择环节；
- `call_router_llm()`（路由 LLM 调用，含 `max_tokens=2000` 思考型兼容补丁）：
  该补丁服务于「路由 LLM 输出决策 JSON」，新插件不解析任何决策 JSON；
- `parse_router_json()`（从路由 LLM 响应里抠 `{"ai": "id"}`）：决策 JSON 已不存在；
- `list_providers()` / `find_provider_by_api_base()` / `get_provider_models()`
  （下拉/模型列表辅助）：服务于 analyze 池的 WebUI 选择，新插件按 `provider_ref`
  直接解析，无需读全量 provider 列表。

**经验保留（PRD §2.2 统一约束）**：思考档位关闭时**完全不下发** `reasoning` /
`reasoning_effort` 字段——旧实现里「思考档位 false = 不下发」正是为了修复
`reasoning: null` 触发端点 400 的问题，本模块的请求体只包含冻结的键
（`model` / `messages` / `stream`），`None` 值字段一律不出现。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger as LOG

from . import search_core

# 判定「视觉能力」的 modalities 关键字
VISION_MODALITY_HINTS = ("image", "vision")

# 兼容 OpenAI chat/completions 的 provider type
CHAT_COMPATIBLE_TYPES = ("openai_chat_completion", "openai", "openai_compatible")

# 错误文本里响应体的最大长度（PRD §2.2：HTTP 非 2xx → error 取响应体前 200 字）
ERROR_BODY_CHARS = 200


class SourceError(RuntimeError):
    """A 类源调用/解析失败（人话原因，交给上层转 `attempts[].error`）。"""


@dataclass
class ProviderSource:
    """统一 Provider 数据源（官方接口或 cmd_config fallback，两者同构）。

    - providers：已合并（含 api_base/key）的对话类 provider 条目列表；
    - sources：provider_source 条目（id -> dict）；
    - settings：provider_settings dict；
    - source_name："official"（ProviderManager 官方接口）| "cmd_config"（直读文件）。
    """

    providers: list[dict] = field(default_factory=list)
    sources: dict[str, dict] = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    source_name: str = "cmd_config"


def default_config_path() -> str:
    """AstrBot 主配置路径（`{data}/cmd_config.json`，按官方 data 目录推导）。"""
    return str(search_core.astrbot_data_dir() / "cmd_config.json")


def _resolve_env_key(key: str) -> str:
    """解析 `$ENV_VAR` / `${ENV_VAR}` 形态的 key 引用（AstrBot provider 配置支持）。"""
    if not isinstance(key, str):
        return key
    if key.startswith("$") and len(key) > 1:
        env_name = key[1:]
        if env_name.startswith("{") and env_name.endswith("}"):
            env_name = env_name[1:-1]
        if env_name:
            value = os.getenv(env_name)
            if value is None:
                LOG.warning("provider key references unset env var %s", env_name)
                return ""
            return value
    return key


def read_cmd_config(config_path: str = "") -> dict:
    """读取 AstrBot cmd_config.json；失败返回空 dict（不抛异常）。

    Args:
        config_path: 配置路径；空串回退 `default_config_path()`。

    Returns:
        解析后的配置 dict；读取或解析失败返回空 dict。
    """
    target = config_path or default_config_path()
    try:
        return json.loads(Path(target).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("failed to read cmd_config.json (%s): %s", type(exc).__name__, exc)
        return {}


def _build_provider_source(
    providers_raw: list, sources_raw: list, settings: Any
) -> ProviderSource:
    """构造 ProviderSource：仅保留对话类 provider，并合并 source 的 api_base/key。"""
    sources = {
        str(s.get("id") or ""): s
        for s in (sources_raw or [])
        if isinstance(s, dict) and s.get("id")
    }
    providers: list[dict] = []
    for entry in providers_raw or []:
        if not isinstance(entry, dict) or not entry.get("enable", True):
            continue
        ptype = entry.get("provider_type") or entry.get("type") or ""
        if ptype and ptype not in (
            "chat_completion",
            "openai_chat_completion",
            "openai",
            "openai_responses",
        ):
            continue  # 非对话类（embedding/rerank/stt/tts）跳过
        src = sources.get(str(entry.get("provider_source_id") or "")) or {}
        merged = dict(entry)
        merged.setdefault("api_base", src.get("api_base") or "")
        merged.setdefault("key", src.get("key") or src.get("api_key") or "")
        merged.setdefault("type", src.get("type") or entry.get("type") or "")
        merged.setdefault(
            "provider_type",
            entry.get("provider_type") or src.get("provider_type") or "chat_completion",
        )
        key = merged.get("key")
        if isinstance(key, list):
            merged["key"] = [_resolve_env_key(item) for item in key]
        elif isinstance(key, str):
            merged["key"] = _resolve_env_key(key)
        providers.append(merged)
    return ProviderSource(
        providers=providers,
        sources=sources,
        settings=settings if isinstance(settings, dict) else {},
    )


def load_provider_source(
    provider_manager: Any | None = None,
    config_path: str = "",
) -> ProviderSource:
    """加载统一 Provider 数据源（官方接口优先，cmd_config 直读兜底）。

    Args:
        provider_manager: AstrBot ProviderManager（duck typing，可为 None）。
        config_path: 直读回退路径；空串用 `default_config_path()`。

    Returns:
        ProviderSource 实例。
    """
    if provider_manager is not None:
        providers_raw = getattr(provider_manager, "providers_config", None)
        sources_raw = getattr(provider_manager, "provider_sources_config", None)
        settings = getattr(provider_manager, "provider_settings", None)
        if isinstance(providers_raw, list) and isinstance(sources_raw, list):
            source = _build_provider_source(providers_raw, sources_raw, settings)
            source.source_name = "official"
            return source
        LOG.debug("provider_manager lacks official config attributes, reading file")
    cfg = read_cmd_config(config_path)
    return _build_provider_source(
        cfg.get("provider") or [],
        cfg.get("provider_sources") or [],
        cfg.get("provider_settings") or {},
    )


def _is_vision_provider(entry: dict) -> bool:
    """provider 条目是否声明了视觉模态（modalities 含 image/vision）。"""
    modalities = entry.get("modalities") or []
    if isinstance(modalities, list):
        text = " ".join(str(m).lower() for m in modalities)
        return any(hint in text for hint in VISION_MODALITY_HINTS)
    return False


def _extract_key(entry: dict) -> str:
    """取 provider 条目里的密钥（`key` 可为列表，取首个非空）。"""
    for name in ("key", "api_key"):
        value = entry.get(name)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def mask_api_key(key: str) -> str:
    """密钥脱敏：仅保留前 4 位 + `***`（PRD §1.4）。"""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "***"
    return f"{text[:4]}***"


def find_provider_entry(
    provider_ref: str,
    provider_manager: Any | None = None,
    config_path: str = "",
) -> dict | None:
    """按 provider id 查找已合并的 provider 配置；未找到返回 None。

    Args:
        provider_ref: AstrBot 提供商引用 ID（如 `豆包/doubao`）。
        provider_manager: ProviderManager（官方接口优先）。
        config_path: 直读回退路径。

    Returns:
        合并 provider_source 后的配置 dict；未找到为 None。
    """
    if not provider_ref:
        return None
    getter = getattr(provider_manager, "get_provider_config_by_id", None)
    if callable(getter):
        try:
            entry = getter(provider_ref, merged=True)
        except TypeError:  # 兼容旧签名（无 merged 关键字）
            entry = getter(provider_ref)
        if isinstance(entry, dict):
            return entry
    for entry in load_provider_source(provider_manager, config_path).providers:
        if str(entry.get("id") or "") == provider_ref:
            return entry
    return None


def _entry_from_instance(provider: Any) -> dict | None:
    """从 Provider 实例或其 provider_manager 读合并配置（PRD §2.2 provider_config 路径）。"""
    if provider is None:
        return None
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict) and config:
        return dict(config)
    return None


def resolve_source_endpoint(
    source: dict,
    *,
    provider: Any | None = None,
    provider_manager: Any | None = None,
    config_path: str = "",
) -> dict:
    """解析 A 类源的实际调用端点（PRD §2.2）。

    返回 `{"api_base", "key", "model", "provider_id", "vision", "source"}`：
    - `form="provider"`：provider 实例（`context.get_provider_by_id(ref)`，失败已由
      调用方回退 `ref.split("/")[-1]` 再试）优先，其次 ProviderManager 配置；
      `model` 取该提供商默认模型（源内的手填 model 在归一化阶段已被忽略）；
    - `form="custom"`：字段直接取自源配置；`api_base` 不以 `/chat/completions`
      结尾时自动拼接。

    Args:
        source: 归一化后的 vision 源。
        provider: 已解析的 Provider 实例（可为 None）。
        provider_manager: ProviderManager（回退查询）。
        config_path: 直读回退路径。

    Returns:
        端点 dict。

    Raises:
        SourceError: `provider_not_found: <ref>` / 端点字段缺失。
    """
    if source.get("form") == "provider":
        ref = str(source.get("provider_ref") or "").strip()
        entry = _entry_from_instance(provider) or find_provider_entry(
            ref, provider_manager, config_path
        )
        if entry is None and ref:
            entry = find_provider_entry(
                ref.split("/")[-1], provider_manager, config_path
            )
        if entry is None:
            raise SourceError(f"provider_not_found: {ref}")
        api_base = str(entry.get("api_base") or "").strip()
        key = _extract_key(entry)
        model = str(entry.get("model") or "").strip()
        if not model and provider is not None:
            get_model = getattr(provider, "get_model", None)
            if callable(get_model):
                model = str(get_model() or "").strip()
        if not api_base:
            raise SourceError(f"provider_missing_api_base: {ref}")
        return {
            "api_base": api_base,
            "key": key,
            "model": model,
            "provider_id": ref,
            "vision": _is_vision_provider(entry),
            "source": "provider_ref",
        }

    api_base = str(source.get("api_base") or "").strip()
    model = str(source.get("model") or "").strip()
    if not api_base or not model:
        raise SourceError("missing_endpoint")
    return {
        "api_base": api_base,
        "key": str(source.get("api_key") or "").strip(),
        "model": model,
        "provider_id": source.get("id") or "custom",
        "vision": True,
        "source": "custom",
    }


def build_chat_url(api_base: str) -> str:
    """拼接 chat/completions 端点：不以 `/chat/completions` 结尾则自动补。"""
    url = (api_base or "").strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    return url


def build_vision_payload(
    model: str, system_prompt: str, user_prompt: str, data_uri: str
) -> dict:
    """构造 OpenAI 兼容请求体（PRD §2.2 冻结形状）。

    messages 为 system（搜图系统提示词）+ user（文本块 + `image_url` 图片块）；
    图片只走 data URI（不依赖图床）。**只输出冻结的三个键**，不额外下发
    `temperature` / `max_tokens` / `reasoning*`（避免 `None` 字段触发 400）。

    Args:
        model: 模型名。
        system_prompt: 搜图系统提示词。
        user_prompt: 用户提示词（含 VERDICT 输出要求）。
        data_uri: `data:<mime>;base64,<payload>`。

    Returns:
        可直接 `json=` 提交的请求体 dict。
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
        "stream": False,
    }


def collect_secrets(values=None, headers: dict | None = None, urls=()) -> list[str]:
    """收集需要在错误/日志文本里脱敏的密文（vision 源与 engine 源**共用**实现）。

    收集范围（全部按「出现即替换为前 4 位 + `***`」处理）：
    1. 显式值（vision 源的 endpoint key / engine 源的 `options["api_key"]`）；
    2. `headers` 的**全部 value**（反代鉴权常用 `api-key` / `X-Token` 等；
       嵌套 list/dict 递归取值，只看 value 不看 key 名，避免把 header 名误判为密文）；
    3. 各处 URL 的 **query 参数值**（`api_base` / `build_chat_url()` 结果 /
       `base_url` / engine endpoint），如 `https://relay/v1?api-key=SECRET`
       —— 这类 key 会随异常文本（httpx 报错常带完整 URL）回显。

    Args:
        values: 单个密文或密文容器（dict 取 value、list/tuple/set 逐项）。
        headers: 源级附加请求头。
        urls: 一个或多个可能带 query 密文的 URL 字符串。

    Returns:
        去重后的密文列表（按长度降序，保证长密文优先被替换）。
    """
    from urllib.parse import parse_qsl, urlparse

    secrets: list[str] = []

    def _add(value) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text not in secrets:
                secrets.append(text)
        elif isinstance(value, dict):
            for item in value.values():
                _add(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)

    _add(values)
    _add(headers or {})
    for raw_url in urls if isinstance(urls, (list, tuple, set)) else (urls,):
        if isinstance(raw_url, str) and "?" in raw_url:
            try:
                pairs = parse_qsl(urlparse(raw_url).query, keep_blank_values=False)
            except ValueError:
                continue
            for _, value in pairs:
                _add(value)
    # 长的先替换，避免短密文是长密文子串时切碎长密文
    return sorted(secrets, key=len, reverse=True)


def sanitize_secrets(text: str, secrets: list[str]) -> str:
    """按密文清单脱敏：任何出现的密文替换为前 4 位 + `***`。

    engine 层（`search_engines.py`）与 vision 层共用本函数，保证两条链路
    在拼 error 前用的是同一套脱敏规则。
    """
    if not text:
        return ""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, mask_api_key(secret))
    return text


def extract_message_text(payload: dict) -> str:
    """从 chat/completions 响应里取正文：`content`，为空回退 `reasoning_content`。

    Args:
        payload: 响应 JSON。

    Returns:
        正文文本（可能为空串）。
    """
    try:
        message = payload["choices"][0].get("message") or {}
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or message.get("reasoning_content") or "").strip()


async def call_vision_source(
    endpoint: dict,
    *,
    system_prompt: str,
    user_prompt: str,
    data_uri: str,
    timeout_sec: float,
    extra_headers: dict | None = None,
) -> str:
    """调用 A 类 vision 源（OpenAI chat/completions 兼容），返回回答文本。

    Args:
        endpoint: `resolve_source_endpoint()` 的返回。
        system_prompt: 搜图系统提示词（`prompts.SEARCH_SYSTEM_PROMPT`）。
        user_prompt: 搜图用户提示词。
        data_uri: 图片 data URI。
        timeout_sec: 该源生效超时（秒）。
        extra_headers: 源级附加请求头。

    Returns:
        模型回答正文（可能为空串 → 上层按 MISS 处理）；`content` 为空时回退
        `reasoning_content`（思考型模型容错，行为与旧实现一致）。

    Raises:
        SourceError: 网络异常 / HTTP 非 2xx / 响应非 JSON。
    """
    import httpx

    key = str(endpoint.get("key") or "")
    url = build_chat_url(str(endpoint.get("api_base") or ""))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for name, value in (extra_headers or {}).items():
        headers[str(name)] = str(value)
    # 密文清单在发请求前就固定：endpoint key + extra_headers 全部 value + URL query 值
    secrets = collect_secrets(
        endpoint.get("key"),
        extra_headers,
        (url, endpoint.get("api_base"), endpoint.get("base_url")),
    )
    body = build_vision_payload(
        str(endpoint.get("model") or ""), system_prompt, user_prompt, data_uri
    )
    timeout = httpx.Timeout(
        connect=min(10.0, float(timeout_sec)),
        read=float(timeout_sec),
        write=min(30.0, float(timeout_sec)),
        pool=10.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 -- 网络异常交上层转 error
        detail = sanitize_secrets(str(exc)[:ERROR_BODY_CHARS].strip(), secrets)
        suffix = f": {detail}" if detail else ""
        raise SourceError(f"网络请求失败（{type(exc).__name__}{suffix}）") from None
    if resp.status_code < 200 or resp.status_code >= 300:
        detail = sanitize_secrets((resp.text or "")[:ERROR_BODY_CHARS].strip(), secrets)
        raise SourceError(f"HTTP {resp.status_code}: {detail}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        raise SourceError("响应不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise SourceError("响应不是 JSON 对象")
    return extract_message_text(payload)
