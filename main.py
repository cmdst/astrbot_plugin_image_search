"""main.py -- 搜图路由 Agent 插件（astrbot_plugin_image_search）v2.0.0。

**唯一 LLM 工具 `image_search`**：主模型遇到「图中角色/场景是谁、出自哪里」类问题时，
把消息里的图片引用传给本工具，插件按配置的**搜图源池**顺序联网检索，失败自动换下一个
源，多源命中合并去重，最后把结构化 JSON（+ 可读摘要）回传主模型，由主模型组织回复。

- **A 类 vision 源**（PRD §4.7）：AstrBot 提供商引用（可为反代提供商）或自定义
  OpenAI 兼容端点；图片一律以 data URI 交给模型，提示词要求「先看图 → 联网检索 →
  给出识别结论 / 出处作品 / 佐证来源 / 不确定项」（`prompts.py`）。
- **B 类 engine 源**（PRD §4.1）：SauceNAO / ascii2d / trace.moe，只接受**公网图片
  URL**（图床 `image_url.enabled=false` 时全部记 `skipped(no_public_url)`，
  `search_type=auto` 下仍由 vision 源兜住）。
- **逐图独立 + 超时隔离**：每张图 `asyncio.wait_for` 独立预算，单图异常/超时只影响
  该图；每个源同样独立 `wait_for`（PRD §3.1），失败/超时/挑战页**不计轮数**、
  只有明确 `miss` 计轮（PRD §3.2 `search.max_rounds`）。
- **彻底删除 analyze 识图池**（PRD §0 边界①）：本插件不注册任何识图/转述工具，
  无 ais 多 AI 池、无关键词预路由、无「质量不满意换 AI」、无兜底直判；`image_search`
  是本插件唯一的工具入口。

配置见 PRD §2（`_conf_schema.json`）；迁移见 §6（`migrate_v1_7.py`）。
"""

from __future__ import annotations

import asyncio
import time
import traceback

from astrbot.api import logger as LOG
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star

from . import image_io, image_server, prompts, provider_reader, search_core

# ---------------------------------------------------------------------------
# 工具提示（注入消息文本，引导主模型遇图调用 image_search）
# ---------------------------------------------------------------------------

_TOOL_HINT_TEXT = (
    "（联网搜图提示：若用户问的是图中**主体的身份、出处、来历**——「这是谁」"
    "「这是哪个角色」「出自哪部作品」「什么梗」「背景是哪里」「帮我搜一下这张图」——"
    "请调用工具 image_search(image_refs=[<图片路径1>, <图片路径2>, ...], "
    'question=<用户原话>, search_type="auto")。image_refs 直接传上面的 '
    "[Image Attachment: path ...] 路径，支持单图或多图；"
    "插件会按配置的搜图源池联网检索并把结果返回给你，你据此用自然语言回答用户。"
    "若只是要求识别/转述图中文字、描述画面、提取表格数据，**不要**调用本工具，"
    "直接用你自己的多模态能力回答。[image-search-hint]"
)

_TOOL_HINT_MARKER = "[image-search-hint]"

# 单源外层硬闸宽限（PRD §3.1：`asyncio.wait_for(..., timeout=src.timeout + 5)`）
_PER_SOURCE_GRACE_SEC = 5.0

# 挑战页告警节流（PRD §5：每源每运行期最多 3 条，避免刷屏）
_MAX_CHALLENGE_LOGS = 3


class ImageSearchPlugin(Star):
    """搜图路由 Agent 插件：唯一工具 image_search（多图）+ 可配置搜图源池 + 失败换源。"""

    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._cfg: dict = {}
        self._cache = search_core.ResultCache(0)
        self._health = search_core.HealthStats()
        self._image_server: image_server.ImageServer | None = None
        self._image_dir = ""
        self._public_base = ""
        self._challenge_logs: dict[str, int] = {}

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """激活：配置归一化 → 可选启动内置只读图床 → 一行激活日志（PRD §2.8）。

        配置非法值由 `search_core.normalize_config()` 回落默认值并告警，不阻断加载。
        """
        cfg = search_core.normalize_config(search_core.load_config(self.config))
        self._cfg = cfg
        self._apply_log_level(cfg.get("log_level", "info"))
        self._cache = search_core.ResultCache(
            (cfg.get("search") or {}).get("cache_ttl_sec", 0)
        )
        self._health = search_core.HealthStats()
        self._challenge_logs = {}

        image_url_cfg = cfg.get("image_url") or {}
        self._image_dir = search_core.image_url_dir(cfg)
        if image_url_cfg.get("enabled"):
            server = image_server.ImageServer(
                self._image_dir, int(image_url_cfg.get("port") or 8123)
            )
            try:
                server.start()
            except RuntimeError as exc:
                LOG.warning("image_url server unavailable for this run: %s", exc)
                server = None
            self._image_server = server
            if server is not None:
                # 配置了公网前缀就直接用（避免无谓的外探等待），留空才探测公网 IP
                configured_base = str(
                    image_url_cfg.get("public_base_url") or ""
                ).strip()
                if not configured_base:
                    configured_base = (
                        f"http://{search_core.detect_public_ip()}:{server.bound_port}"
                    )
                self._public_base = server.public_base_url(configured_base)
                LOG.info(
                    "image_url ready: dir=%s public_base=%s (bound_port=%s)",
                    server.directory,
                    self._public_base,
                    server.bound_port,
                )
        else:
            LOG.info(
                "image_url.enabled=false: engine sources will be skipped (no_public_url)"
            )

        if search_core.detect_migration_hint():
            LOG.warning(
                "legacy v1.7 config found but v2.0 config missing; "
                "run migrate_v1_7.py to migrate search sources (PRD section 6)"
            )

        enabled = search_core.enabled_sources(cfg)
        vision_ids = [s["id"] for s in enabled if s["kind"] == "vision"]
        engine_ids = [s["id"] for s in enabled if s["kind"] == "engine"]
        # provider 形态忽略手填 model 的可见性（PRD §4.7 优先；§2.2 措辞待 amend）
        for source in enabled:
            if source.get("model_ignored"):
                LOG.warning(
                    "source %s uses provider reference %s: hand-written model %r is "
                    "ignored, the provider default model is used (PRD section 4.7)",
                    source["id"],
                    source.get("provider_ref"),
                    source["model_ignored"],
                )
        LOG.info(
            "image_search v2.0.0 activated: vision_sources=%s engine_sources=%s "
            "search_type_default=auto max_rounds=%s image_url=%s",
            vision_ids,
            engine_ids,
            (cfg.get("search") or {}).get("max_rounds"),
            "on" if image_url_cfg.get("enabled") else "off",
        )

    async def terminate(self) -> None:
        """禁用/重载：停止内置图床 + 输出一行源健康统计（PRD §3.5）。"""
        server = self._image_server
        self._image_server = None
        if server is not None:
            try:
                server.stop()
            except Exception as exc:  # noqa: BLE001 -- 停止失败不影响卸载
                LOG.warning("image_url server stop failed: %s", exc)
        if (self._cfg.get("search") or {}).get("health_stats", True):
            summary = self._health.summary()
            if summary:
                LOG.info("image_search %s", summary)
        self._health.reset()
        self._cache.clear()

    def _apply_log_level(self, level: str) -> None:
        """把配置 `log_level` 应用到插件专属 logger（不可用时静默跳过）。"""
        import logging

        mapping = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }
        plugin_logger = getattr(self, "logger", None)
        set_level = getattr(plugin_logger, "setLevel", None)
        if callable(set_level):
            try:
                set_level(mapping.get(level, logging.INFO))
            except Exception:  # noqa: BLE001 -- 日志级别设置失败不影响插件
                pass

    # ------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="image_search")
    async def image_search(
        self,
        event: AstrMessageEvent,
        image_refs: list[str] | None = None,
        question: str = "",
        search_type: str = "auto",
    ) -> str:
        """联网搜图：识别图中主体的身份与来历（这是谁 / 出自哪里 / 什么梗 / 哪个角色 / 帮我搜一下）。

        当用户发来图片并询问图中**主体的身份、出处、来源**时调用本工具：例如「这是谁」「这是哪个角色」
        「出自哪部作品」「什么梗」「帮我搜一下这张图」「背景是哪里」。工具会联网检索并把结果返回给你，
        你据此用自然语言回答用户。

        Args:
            image_refs (list[string]): 待搜索的图片引用列表，单图或多图。直接使用消息里给出的
                图片路径/URL（如 /root/AstrBot/data/temp/xxx.jpg 或 [Image Attachment: path ...]）。
            question (string): 用户关于这些图片的原话，例如「这是谁」「出自哪里」。没有就留空。
            search_type (string): 可选。auto（默认，先图搜图引擎再视觉联网模型）/ engine（只用图搜图
                引擎）/ vision（只用视觉联网模型）。一般不需要指定，保持 auto。
        """
        started = time.time()
        try:
            return await self._run_search(image_refs, question, search_type, started)
        except Exception as exc:  # noqa: BLE001 -- 工具不得把异常抛给主模型（PRD §1.6.2）
            LOG.error(
                "image_search internal error: %s\n%s", exc, traceback.format_exc()
            )
            return search_core.build_failure_payload(
                "failed",
                search_core.INTERNAL_ERROR_SUMMARY,
                hint=f"内部错误类型：{type(exc).__name__}",
                elapsed_ms=self._elapsed_ms(started),
            )

    # ------------------------------------------------------------ 执行链路

    async def _run_search(
        self, image_refs, question, search_type, started: float
    ) -> str:
        """工具主链路（PRD §3.1）：归一化 → 候选源 → 逐图独立处理 → 结构化 JSON。"""
        cfg = self._active_cfg()
        notes: list[str] = []
        effective_type = search_core.normalize_search_type(search_type, notes)
        clean_question = search_core.normalize_question(question, notes)
        refs = search_core.normalize_refs(image_refs, notes)
        if not refs:
            LOG.info("image_search called without usable image_refs")
            return search_core.build_failure_payload(
                "no_refs",
                search_core.NO_REFS_SUMMARY,
                search_core.NO_REFS_HINT,
                search_type=effective_type,
                question=clean_question,
                notes=notes,
                elapsed_ms=self._elapsed_ms(started),
            )

        candidates = search_core.select_candidates(cfg, effective_type)
        # provider 形态的 model 语义在 notes 里也明示一次（除启动日志外的第二处可见性）
        for source in candidates:
            if source.get("model_ignored"):
                notes.append(f"provider_model_ignored:{source['id']}")
        if not any(source["valid"] for source in candidates):
            LOG.warning(
                "no usable search source for search_type=%s; configure search_sources",
                effective_type,
            )
            return search_core.build_failure_payload(
                "empty_pool",
                search_core.EMPTY_POOL_SUMMARY,
                search_core.EMPTY_POOL_HINT,
                search_type=effective_type,
                question=clean_question,
                notes=notes,
                elapsed_ms=self._elapsed_ms(started),
            )

        search = cfg.get("search") or {}
        deadline = started + float(search.get("total_timeout_sec", 180))
        per_image_budget = float(search.get("total_timeout_sec", 180)) + float(
            search.get("source_timeout_sec", 30)
        )
        results: list[dict] = []
        for index, ref in enumerate(refs):
            try:
                image_obj = await asyncio.wait_for(
                    self._run_one_image(
                        cfg,
                        index,
                        ref,
                        clean_question,
                        effective_type,
                        candidates,
                        deadline,
                        notes,
                    ),
                    timeout=per_image_budget,
                )
            except asyncio.TimeoutError:
                LOG.warning(
                    "image %d exceeded the per-image budget (%.0fs); isolated",
                    index,
                    per_image_budget,
                )
                image_obj = search_core.build_image_result(
                    index,
                    ref,
                    error="该图处理超出总时限（逐图超时隔离，其余图片不受影响）。",
                )
            except Exception as exc:  # noqa: BLE001 -- 单图异常不阻断其他图
                LOG.warning("image %d failed (%s); isolated", index, type(exc).__name__)
                image_obj = search_core.build_image_result(
                    index, ref, error=f"该图处理异常（{type(exc).__name__}）。"
                )
            results.append(image_obj)
            if search.get("health_stats", True):
                self._health.record(image_obj.get("attempts") or [])

        status = search_core.aggregate_status(results)
        payload = search_core.build_results_payload(
            results,
            status=status,
            search_type=effective_type,
            question=clean_question,
            summary=search_core.render_summary(results),
            hint=search_core.render_hint(results, status, effective_type),
            elapsed_ms=self._elapsed_ms(started),
            notes=notes,
        )
        LOG.info(
            "image_search finished: images=%d status=%s elapsed_ms=%d",
            len(results),
            status,
            self._elapsed_ms(started),
        )
        return payload

    async def _run_one_image(
        self,
        cfg: dict,
        index: int,
        ref,
        question: str,
        search_type: str,
        candidates: list[dict],
        deadline: float,
        notes: list[str],
    ) -> dict:
        """单图流水线（PRD §3.1 步骤 1~7）：物化 → 预处理 → 传输形态 → 逐源尝试 → 合并。"""
        search = cfg.get("search") or {}
        deadline_exhausted = False
        if time.time() >= deadline:
            if "total_timeout" not in notes:  # notes 去重（逐图检查会重复命中）
                notes.append("total_timeout")
            return search_core.build_image_result(
                index, ref, error=search_core.TOTAL_TIMEOUT_ERROR
            )

        # ---- 1) 物化引用（本地路径 / file:// / http(s) / data URI / base64://）
        data, ext, error = await image_io.resolve_image_bytes(
            ref,
            int(search.get("max_size_mb", 20)),
            int(cfg.get("download_timeout_sec", 20)),
        )
        if data is None or not ext:
            LOG.warning("image %d unreadable: %s", index, error)
            return search_core.build_image_result(
                index, ref, error=error or image_io.ERROR_BAD_REF
            )

        # ---- 2) 预处理：体积/像素校验 + 压缩（压缩失败用原图，按上限校验）
        data, ext, changed = image_io.preprocess_image(
            data,
            ext,
            int(search.get("max_size_mb", 20)),
            int(search.get("max_pixels", 0)),
        )
        if changed:
            LOG.debug("image %d compressed for transport", index)
        if len(data) > int(search.get("max_size_mb", 20)) * 1024 * 1024:
            LOG.warning(
                "image %d still exceeds the size limit after compression", index
            )
            return search_core.build_image_result(
                index, ref, error=image_io.ERROR_IMAGE_TOO_LARGE
            )
        pixels = image_io.image_pixels(data)

        # ---- 3) 传输形态：vision 一律 data URI，engine 一律图床 URL
        data_uri = image_io.to_data_uri(data, ext)
        public_url = self._publish_image(data, ext)
        image_input = {"data_uri": data_uri, "url": public_url}

        # ---- 4) 缓存（键含图片哈希 + 生效 search_type + question；命中即零网络请求）
        cache_key = search_core.ResultCache.make_key(data, search_type, question)
        cached = self._cache.get(cache_key)
        if cached is not None:
            LOG.debug("cache hit for image %d", index)
            # 缓存键只含图片哈希（PRD §3.5），同字节不同引用的图片各自回填自己的
            # index / ref（引用是逐图属性，不属于图片内容）
            cached["index"] = index
            cached["ref"] = search_core.ref_display(ref)
            cached["cached"] = True
            return cached

        # ---- 5) 逐源尝试（轮数隔离 + 单源超时隔离 + 失败换源）
        attempts: list[dict] = []
        hits: list[tuple[dict, dict]] = []
        rounds = 0
        last_transport = ""
        merge_enabled = bool(search.get("merge_enabled", True))
        merge_max = int(search.get("merge_max_sources", 3))
        max_rounds = int(search.get("max_rounds", 2))
        for source in candidates:
            if time.time() >= deadline:
                if "total_timeout" not in notes:
                    notes.append("total_timeout")
                deadline_exhausted = True
                break
            if len(hits) >= merge_max:
                break
            if not merge_enabled and hits:
                break
            if not source.get("valid", True):
                attempts.append(
                    search_core.build_attempt(source, "skipped", error="invalid_source")
                )
                continue
            transport = "url" if source["kind"] == "engine" else "data_uri"
            if source["kind"] == "engine" and not public_url:
                attempts.append(
                    search_core.build_attempt(source, "skipped", error="no_public_url")
                )
                continue
            if self._exceeds_source_limits(cfg, source, data, pixels):
                attempts.append(
                    search_core.build_attempt(
                        source, "skipped", error="image_too_large_for_source"
                    )
                )
                continue

            timeout_sec = search_core.source_timeout_sec(cfg, source)
            last_transport = transport
            source_started = time.time()
            try:
                result = await asyncio.wait_for(
                    self._run_source(cfg, source, image_input, question, timeout_sec),
                    timeout=timeout_sec + _PER_SOURCE_GRACE_SEC,
                )
            except asyncio.TimeoutError:
                elapsed = self._elapsed_ms(source_started)
                LOG.warning("source %s timeout after %.0fs", source["id"], timeout_sec)
                attempts.append(
                    search_core.build_attempt(
                        source,
                        "timeout",
                        error=f"源超时（>{timeout_sec:.0f}s）",
                        elapsed_ms=elapsed,
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 -- 源失败只换源，不计轮
                elapsed = self._elapsed_ms(source_started)
                # provider_reader 已在异常文本内脱敏（endpoint key / extra_headers /
                # URL query）；此处**原样复用**该文本，不重新拼接 endpoint 信息
                safe_error = self._safe_error(exc)
                LOG.warning("source %s failed: %s", source["id"], safe_error)
                attempts.append(
                    search_core.build_attempt(
                        source, "error", error=safe_error, elapsed_ms=elapsed
                    )
                )
                continue

            elapsed = self._elapsed_ms(source_started)
            if result.get("ok"):
                result["transport"] = transport
                hits.append((source, result))
                attempts.append(
                    search_core.build_attempt(source, "hit", elapsed_ms=elapsed)
                )
                continue
            if result.get("challenge"):
                self._log_challenge(source)
                attempts.append(
                    search_core.build_attempt(
                        source,
                        "challenge",
                        error=result.get("error") or search_core.CHALLENGE_REASON,
                        challenge=True,
                        elapsed_ms=elapsed,
                    )
                )
                continue
            if result.get("miss"):
                rounds += 1
                attempts.append(
                    search_core.build_attempt(
                        source,
                        "miss",
                        error=result.get("error") or "",
                        elapsed_ms=elapsed,
                    )
                )
                if rounds >= max_rounds:
                    break
                continue
            source_error = result.get("error") or ""
            LOG.warning("source %s failed: %s", source["id"], source_error)
            attempts.append(
                search_core.build_attempt(
                    source, "error", error=source_error, elapsed_ms=elapsed
                )
            )

        # ---- 6) 合并去重 / 失败归因
        merged = search_core.merge_results(hits, merge_max)
        if merged:
            image_obj = search_core.build_image_result(
                index,
                ref,
                ok=True,
                transport=merged.get("transport") or last_transport,
                source=merged.get("source"),
                match=merged.get("match"),
                answer=merged.get("answer") or "",
                merged_from=merged.get("merged_from") or [],
                attempts=attempts,
                rounds_used=rounds,
            )
            if int(search.get("cache_ttl_sec", 0)) > 0:
                self._cache.set(cache_key, image_obj)
            return image_obj
        if rounds:
            LOG.info("no match for image %d", index)
        # 被跳过的源从未被请求 → 逐源明细进 notes（F13），避免只把首个原因写进文案
        for attempt in attempts:
            if attempt.get("outcome") == "skipped":
                note = f"skipped:{attempt['id']}:{attempt['error']}"
                if note not in notes:
                    notes.append(note)
        if deadline_exhausted and not attempts:
            # 因总预算耗尽而一个源都没试到 → 用 total_timeout 文案
            error = search_core.TOTAL_TIMEOUT_ERROR
        elif not search_core.has_real_attempt(attempts):
            # F13：没有任何可尝试的实测尝试（全 skipped / 候选为空）→ 聚合为 empty_pool
            error = search_core.NO_ATTEMPTS_ERROR
        else:
            error = search_core.describe_attempts(attempts)
        return search_core.build_image_result(
            index,
            ref,
            transport=last_transport,
            attempts=attempts,
            rounds_used=rounds,
            error=error,
        )

    async def _run_source(
        self,
        cfg: dict,
        source: dict,
        image_input: dict,
        question: str,
        timeout_sec: float,
    ) -> dict:
        """调用单个源，统一返回 `{ok, miss, challenge, match, answer, error}`。"""
        if source["kind"] == "engine":
            return await self._run_engine_source(cfg, source, image_input, timeout_sec)
        return await self._run_vision_source(source, image_input, question, timeout_sec)

    async def _run_engine_source(
        self, cfg: dict, source: dict, image_input: dict, timeout_sec: float
    ) -> dict:
        """B 类源：调用 `search_engines.run_engine(engine, image, cfg, timeout_sec)`。

        `run_engine` 由引擎适配器（`search_engines.py`）提供，契约见 PRD §4.1：
        任何异常都由它内部收敛成统一 dict，本方法再做一次防御性归一。
        """
        engine = str(source.get("engine") or "")
        try:
            from . import search_engines
        except ImportError:
            LOG.warning("engine adapter search_engines.py is unavailable")
            return {
                "ok": False,
                "miss": False,
                "challenge": False,
                "match": None,
                "answer": "",
                "error": "引擎适配器不可用（search_engines.py 缺失）",
            }
        engine_cfg = dict(cfg)
        engine_cfg["_engine_opts"] = {
            engine: {
                "base_url": source.get("base_url", ""),
                "api_key": source.get("api_key", ""),
                "extra_headers": dict(source.get("extra_headers") or {}),
            }
        }
        raw = await search_engines.run_engine(
            engine, image_input, engine_cfg, float(timeout_sec)
        )
        # 入口再验一次脱敏（engine 层已脱敏；此处防适配器未来新增未脱敏出口）
        secrets = provider_reader.collect_secrets(
            source.get("api_key"),
            source.get("extra_headers"),
            (source.get("base_url") or "",),
        )
        if not isinstance(raw, dict):
            return {
                "ok": False,
                "miss": False,
                "challenge": False,
                "match": None,
                "answer": "",
                "error": "引擎返回结构非法",
            }
        return {
            "ok": bool(raw.get("ok")),
            "miss": bool(raw.get("miss")),
            "challenge": bool(raw.get("challenge")),
            "match": raw.get("match") or None,
            "answer": "",
            "error": provider_reader.sanitize_secrets(
                str(raw.get("error") or ""), secrets
            ),
        }

    async def _run_vision_source(
        self, source: dict, image_input: dict, question: str, timeout_sec: float
    ) -> dict:
        """A 类源：解析端点 → OpenAI 兼容调用 → VERDICT 命中判定（PRD §3.3 / §4.7）。"""
        provider = self._resolve_provider(source)
        try:
            endpoint = provider_reader.resolve_source_endpoint(
                source, provider=provider, provider_manager=self._provider_manager()
            )
        except provider_reader.SourceError as exc:
            return self._vision_failure(str(exc))
        system_prompt = prompts.SEARCH_SYSTEM_PROMPT
        if not source.get("web_search", True):
            system_prompt += prompts.SEARCH_WEB_SEARCH_OFF_SUFFIX
        try:
            text = await provider_reader.call_vision_source(
                endpoint,
                system_prompt=system_prompt,
                user_prompt=prompts.build_user_prompt(question),
                data_uri=image_input.get("data_uri") or "",
                timeout_sec=float(timeout_sec),
                extra_headers=source.get("extra_headers") or {},
            )
        except provider_reader.SourceError as exc:
            return self._vision_failure(str(exc))
        verdict = search_core.vision_verdict(text)
        LOG.debug(
            "vision source %s answered (model=%s vision=%s len=%d)",
            source["id"],
            endpoint.get("model"),
            endpoint.get("vision"),
            len(text or ""),
        )
        return {
            "ok": verdict["ok"],
            "miss": verdict["miss"],
            "challenge": False,
            "match": None,
            "answer": verdict["answer"],
            "error": verdict["error"],
        }

    # ------------------------------------------------------------ 运行期辅助

    def _active_cfg(self) -> dict:
        """当前生效配置：优先 `initialize()` 归一化结果，否则即时归一化。"""
        if self._cfg:
            return self._cfg
        self._cfg = search_core.normalize_config(search_core.load_config(self.config))
        return self._cfg

    def _provider_manager(self):
        """AstrBot ProviderManager（不可用时返回 None，由 provider_reader 走回退）。"""
        try:
            return getattr(self.context, "provider_manager", None)
        except Exception:  # noqa: BLE001 -- 上下文缺失不阻断
            return None

    def _resolve_provider(self, source: dict):
        """provider 形态源解析 Provider 实例（PRD §4.7：失败取 id 末段再试一次）。"""
        ref = str(source.get("provider_ref") or "").strip()
        if not ref:
            return None
        context = getattr(self, "context", None)
        getter = getattr(context, "get_provider_by_id", None)
        if not callable(getter):
            return None
        for candidate in (ref, ref.split("/")[-1]):
            try:
                provider = getter(candidate)
            except Exception as exc:  # noqa: BLE001 -- 解析失败按未找到处理
                LOG.debug("provider lookup failed for %s: %s", candidate, exc)
                provider = None
            if provider is not None:
                return provider
        return None

    def _publish_image(self, data: bytes, ext: str) -> str | None:
        """把图片发布到内置图床并返回公网 URL；图床未就绪/发布失败返回 None。

        公网前缀（`_public_base`）只在 `image_url.enabled=true` 且内置服务启动成功时
        由 `initialize()` 预计算，因此它同时代表「图床可用」（PRD §3.3.4）。
        """
        if not self._image_dir or not self._public_base:
            return None
        try:
            filename = image_io.publish_image(data, ext, self._image_dir)
        except (OSError, ValueError) as exc:
            LOG.warning("failed to publish image for engine sources: %s", exc)
            return None
        return image_io.build_public_url(self._public_base, filename)

    def _exceeds_source_limits(
        self, cfg: dict, source: dict, data: bytes, pixels: int
    ) -> bool:
        """该图是否超出该源自身的体积/像素上限（PRD §2.4.6，**仅 vision 源**）。

        PRD §2.4.6 的 P6 冻结澄清：engine 模板没有 `max_size_mb` / `max_pixels`
        字段，engine 源不参与任何源级大小判定（不得出现 `image_too_large_for_source`）。
        """
        if source.get("kind") != "vision":
            return False
        max_size_mb, max_pixels = search_core.source_size_limits(cfg, source)
        if len(data) > int(max_size_mb) * 1024 * 1024:
            return True
        return bool(max_pixels) and pixels > int(max_pixels)

    def _log_challenge(self, source: dict) -> None:
        """挑战页告警（每源每运行期最多 3 条，PRD §5）。"""
        source_id = source.get("id") or "?"
        count = self._challenge_logs.get(source_id, 0)
        self._challenge_logs[source_id] = count + 1
        if count < _MAX_CHALLENGE_LOGS:
            LOG.warning("source %s blocked by challenge (Cloudflare)", source_id)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """距 started 的毫秒数。"""
        return int((time.time() - started) * 1000)

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        """源失败文本：原样复用上层已脱敏的异常文本（不重新拼接 endpoint/headers）。

        `provider_reader` 在异常消息里已把 endpoint key、`extra_headers` 全部 value
        与 URL query 值替换为「前 4 位 + ***」，此处只做空值兜底。
        """
        return str(exc) or type(exc).__name__

    @staticmethod
    def _vision_failure(error: str) -> dict:
        """构造 vision 源失败结果（非 challenge、可继续下一个源）。"""
        return {
            "ok": False,
            "miss": False,
            "challenge": False,
            "match": None,
            "answer": "",
            "error": error or "源调用失败",
        }

    # ------------------------------------------------------------ 钩子

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """请求前钩子：把消息里的图片路径可读化 + 注入 image_search 工具提示。

        受 `tool_hint_enabled` 控制（PRD §2.7）；任何异常都不阻断请求。
        """
        cfg = self._active_cfg()
        if not cfg.get("tool_hint_enabled", True):
            return
        try:
            changed = self._ensure_image_refs_visible(req)
            if changed:
                LOG.debug(
                    "image refs / image_search hint injected (%d images)",
                    len(req.image_urls or []),
                )
        except Exception as exc:  # noqa: BLE001 -- 注入失败不影响请求
            LOG.warning("image hint injection failed (request continues): %s", exc)

    def _ensure_image_refs_visible(self, req: ProviderRequest) -> bool:
        """把 `req.image_urls` 中未出现在文本里的引用追加为 `[Image Attachment: path ...]`。

        同时幂等注入一行 `image_search` 工具提示（含 `[image-search-hint]` 标记）。
        追加项使用 dict 形态内容块（`{"type": "text", "text": ...}`），与 AstrBot
        当前 `extra_user_content_parts` 的消费路径一致。
        """
        urls = [u for u in (req.image_urls or []) if isinstance(u, str) and u.strip()]
        if not urls:
            return False
        parts = list(req.extra_user_content_parts or [])
        # 「已在文本中出现」的判定必须含 req.prompt（用户消息正文），否则当路径本来就
        # 写在用户话里时会被重复追加一份 [Image Attachment: path ...]
        existing = "\n".join(
            [str(getattr(req, "prompt", "") or "")]
            + [
                (part.get("text") or "") if isinstance(part, dict) else ""
                for part in parts
            ]
        )
        changed = False
        for url in urls:
            if url in existing:
                continue
            parts.append({"type": "text", "text": f"[Image Attachment: path {url}]"})
            existing += f"\n[Image Attachment: path {url}]"
            changed = True
        if _TOOL_HINT_MARKER not in existing:
            parts.append({"type": "text", "text": _TOOL_HINT_TEXT})
            changed = True
        if changed:
            req.extra_user_content_parts = parts
        return changed
