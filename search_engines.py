"""B-class image-search engine adapters: SauceNAO / ascii2d / trace.moe.

Contract source of truth: ``PRD.md`` §4 (frozen). This module is the only place that
talks to the three image-reverse-search engines. It never raises: every branch returns

    {
      "engine": str,        # engine name echo
      "ok": bool,           # hit or not
      "match": None | {"similarity": float | None, "title": str, "author": str,
                       "links": list[str], "extra": str},
      "error": str,         # human readable reason when ok is False
      "challenge": bool,    # blocked by Cloudflare / bot protection
      "miss": bool,         # request succeeded but produced no result
    }

Mutually exclusive semantics (§4.1): ``ok=True`` => ``error=""`` and ``miss=False``;
``challenge=True`` => ``ok=False`` and ``miss=False``; ``miss=True`` => ``ok=False`` and
``challenge=False``.

Parsing is separated from HTTP (§4.5) so every result branch is testable offline with
fixtures: ``parse_trace_moe`` / ``parse_saucenao`` / ``parse_ascii2d`` accept text and
never touch the network.

PRD interpretation notes (deliberate, recorded so reviewers can audit them):

1. §4.1 freezes the return keys above (six keys, ``miss`` included); the same section
   also states that no exception may escape, so an unknown ``engine`` returns a clean
   ``ok=False`` dict instead of raising ``ValueError``.
2. §4.1 describes ``similarity`` as ``None`` "for engines without a similarity notion"
   while §4.3 and the §4.6 fixture table freeze ``similarity = similarity * 100`` for
   trace.moe. The trace.moe row is followed here (its API reports 0~1), which also keeps
   the 0~100 scale identical to SauceNAO; ascii2d is the engine that yields ``None``.
3. §4.3 says a response body "containing an ``error`` key" means failure, but the live
   trace.moe API always ships ``"error": ""`` on success (verified against
   ``api.trace.moe``). Only a non-empty value is treated as an engine error.
4. ``url is None`` uses ``no_public_url`` as the leading token (§4.2) and appends the
   Chinese guidance §5 requires, so the string stays greppable and human-readable.
5. trace.moe ``extra`` keeps the frozen ``"第 <episode> 集 <from→mm:ss>"`` form; the
   end timestamp and filename are appended only when the API actually returns them.
6. SauceNAO ``links`` are the ``data.ext_urls`` entries (§4.3); ``data.pixiv_id`` only
   becomes ``https://www.pixiv.net/artworks/<id>`` when ``ext_urls`` yields nothing.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

# 脱敏实现与 A 类 vision 源**共用**（provider_reader 不 import 本模块，无循环依赖）：
# t9 引入的 provider_reader._collect_secrets / _sanitize_all 在 t12 提升为公开 API
# collect_secrets / sanitize_secrets，engine 层在拼 error 前统一走它。
from .provider_reader import collect_secrets, sanitize_secrets

# Human readable strings for the non-parseable source states.

CHALLENGE_ERROR = (
    "目标站点返回 Cloudflare 人机校验页，未取得结果。"
    "可配置自建反代（engine 源 base_url）或提供 api_key 后重试。"
)

NO_PUBLIC_URL_ERROR = (
    "no_public_url: 图搜图引擎只能使用可被外网抓取的图片 URL，不接受 data URI / base64。"
    "请开启 image_url 图床（image_url.enabled=true）后重试。"
)

UNKNOWN_ENGINE_ERROR = (
    "未知的图搜图引擎：{engine}；可用引擎为 saucenao / ascii2d / trace_moe。"
)

# Browser-like UA: ascii2d answers 403 to bare clients, and the other two are friendlier
# with one as well. Cookies are passed through by the `extra_headers` config field.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_SEC = 30.0
MAX_LINKS = 5
CHALLENGE_STATUSES = (403, 429)
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "cf_chl_opt",
    "attention required! | cloudflare",
    "__cf_chl",
)

# Per-engine HTTP spec (§4.3). `path` is joined onto `base_url` when the config
# overrides the endpoint with a self-hosted reverse proxy.
_ENGINES: dict[str, dict] = {
    "trace_moe": {
        "name": "trace.moe",
        "origin": "https://api.trace.moe",
        "path": "/search",
        "accept": "application/json,text/plain,*/*",
    },
    "saucenao": {
        "name": "SauceNAO",
        "origin": "https://saucenao.com",
        "path": "/search.php",
        "accept": "application/json,text/plain,*/*",
    },
    "ascii2d": {
        "name": "ascii2d",
        "origin": "https://ascii2d.net",
        "path": "/search/uri",
        "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "site_headers": True,
    },
}

# ascii2d labels the two retrieval passes in Japanese; both spellings are accepted and
# the frozen Chinese labels are emitted.
_ASCII2D_LABELS = (
    ("色合检索", ("色合検索", "色合检索")),
    ("特征检索", ("特徴検索", "特征检索")),
)


# --------------------------------------------------------------------------------------
# shared result helpers (§4.1)
# --------------------------------------------------------------------------------------


def _result(
    engine: str,
    *,
    ok: bool = False,
    match: dict | None = None,
    error: str = "",
    challenge: bool = False,
    miss: bool = False,
) -> dict:
    """Build the single frozen return shape for one engine run.

    Args:
        engine: Engine name echo.
        ok: Whether the engine produced a usable match.
        match: Normalized match dict when ``ok`` is True.
        error: Human readable failure reason.
        challenge: Whether bot protection blocked the request.
        miss: Whether the request succeeded without any result.

    Returns:
        The engine result dict with the mutually exclusive flags applied.
    """
    if ok:
        return {
            "engine": engine,
            "ok": True,
            "match": match,
            "error": "",
            "challenge": False,
            "miss": False,
        }
    return {
        "engine": engine,
        "ok": False,
        "match": None,
        "error": str(error or ""),
        "challenge": bool(challenge),
        "miss": bool(miss),
    }


def _match(
    similarity: float | None, title: str, author: str, links: list[str], extra: str
) -> dict:
    """Normalize a match: missing strings become ``""``, missing links ``[]``.

    Args:
        similarity: 0~100 score, or None when the engine has no such notion.
        title: Work / subject name.
        author: Artist or creator.
        links: Candidate links, filtered and deduplicated by ``_clean_links``.
        extra: Human readable supplement.

    Returns:
        The frozen ``match`` dict.
    """
    return {
        "similarity": similarity,
        "title": str(title or ""),
        "author": str(author or ""),
        "links": _clean_links(links),
        "extra": str(extra or ""),
    }


def _clean_links(links) -> list[str]:
    """Keep http(s) links only, de-duplicated, order preserving, at most 5 (§4.3)."""
    cleaned: list[str] = []
    for link in links or []:
        text = str(link or "").strip()
        if text.startswith(("http://", "https://")) and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_LINKS:
            break
    return cleaned


def _first_str(*values) -> str:
    """Return the first non-empty value as a stripped string, else ``""``."""
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def _as_str_list(value) -> list[str]:
    """Coerce a possibly scalar / nested field into a flat list of strings."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    if isinstance(value, str):
        return [value]
    return []


def _to_float(value) -> float | None:
    """Convert a number-ish value to ``float``; ``None`` when it is not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_failure(reason: str) -> dict:
    """Build the shared parse-failure payload (never raises, §4.5)."""
    return {"ok": False, "miss": False, "match": None, "error": f"解析失败：{reason}"}


def _mmss(value) -> str:
    """Render a seconds value as ``mm:ss`` (empty string when not numeric)."""
    seconds = _to_float(value)
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------------------
# parse layer (§4.5) — pure text in, match/miss out, no network
# --------------------------------------------------------------------------------------


def parse_trace_moe(text: str) -> dict:
    """Parse a trace.moe ``/search`` JSON body.

    Args:
        text: Raw response body of ``GET https://api.trace.moe/search``.

    Returns:
        ``{"ok": bool, "miss": bool, "match": dict | None, "error": str}``.
        A non-empty ``error`` field in the body maps to ``ok=False`` with the original
        text quoted; an empty ``result`` list maps to ``miss=True``.
    """
    raw = text or ""
    if not raw.strip():
        return _parse_failure("响应为空")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _parse_failure(f"JSON 解析失败：{exc}")
    if not isinstance(payload, dict):
        return _parse_failure("响应不是 JSON 对象")
    engine_error = _first_str(payload.get("error"))
    if engine_error:
        return {
            "ok": False,
            "miss": False,
            "match": None,
            "error": f"trace.moe 返回错误：{engine_error}",
        }
    results = payload.get("result")
    if not isinstance(results, list) or not results:
        return {
            "ok": False,
            "miss": True,
            "match": None,
            "error": "trace.moe 未找到匹配结果。",
        }
    first = results[0] if isinstance(results[0], dict) else {}
    anilist = first.get("anilist") if isinstance(first.get("anilist"), dict) else {}
    titles = anilist.get("title") if isinstance(anilist.get("title"), dict) else {}
    filename = _first_str(first.get("filename"))
    title = _first_str(
        titles.get("native"), titles.get("romaji"), titles.get("english"), filename
    )
    similarity = _to_float(first.get("similarity"))
    if similarity is not None:
        similarity = round(similarity * 100, 2)
    links: list[str] = []
    anilist_id = anilist.get("id")
    if anilist_id not in (None, "", 0, "0"):
        links.append(f"https://anilist.co/anime/{anilist_id}")
    links.append(_first_str(first.get("video")))
    extra_parts: list[str] = []
    episode = first.get("episode")
    if episode not in (None, ""):
        episode_text = (
            "/".join(str(item) for item in episode)
            if isinstance(episode, (list, tuple))
            else str(episode)
        )
        extra_parts.append(f"第 {episode_text} 集")
    start, end = _mmss(first.get("from")), _mmss(first.get("to"))
    if start:
        extra_parts.append(f"{start}-{end}" if end else start)
    if filename:
        extra_parts.append(filename)
    return {
        "ok": True,
        "miss": False,
        "error": "",
        "match": _match(similarity, title, "", links, " ".join(extra_parts)),
    }


def parse_saucenao(text: str) -> dict:
    """Parse a SauceNAO ``output_type=2`` JSON body.

    Args:
        text: Raw response body of ``GET https://saucenao.com/search.php``.

    Returns:
        ``{"ok": bool, "miss": bool, "match": dict | None, "error": str}``.
        ``data.pixiv_id`` is only used as a link fallback when ``data.ext_urls`` is
        empty, so the frozen ``links = ext_urls`` rule stays authoritative.
    """
    raw = text or ""
    if not raw.strip():
        return _parse_failure("响应为空")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _parse_failure(f"JSON 解析失败：{exc}")
    if not isinstance(payload, dict):
        return _parse_failure("响应不是 JSON 对象")
    engine_error = _first_str(payload.get("error"))
    if engine_error:
        return {
            "ok": False,
            "miss": False,
            "match": None,
            "error": f"SauceNAO 返回错误：{engine_error}",
        }
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return {
            "ok": False,
            "miss": True,
            "match": None,
            "error": "SauceNAO 未找到匹配结果。",
        }
    first = results[0] if isinstance(results[0], dict) else {}
    header = first.get("header") if isinstance(first.get("header"), dict) else {}
    data = first.get("data") if isinstance(first.get("data"), dict) else {}
    similarity = _to_float(header.get("similarity"))
    title = _first_str(data.get("title"), data.get("jp_name"), data.get("material"))
    author = _first_str(
        data.get("author_name"), data.get("creator"), data.get("member_name")
    )
    links = _clean_links(_as_str_list(data.get("ext_urls")))
    if not links:
        pixiv_id = _first_str(data.get("pixiv_id"))
        if pixiv_id.isdigit():
            links = _clean_links([f"https://www.pixiv.net/artworks/{pixiv_id}"])
    extra = ""
    if similarity is not None:
        extra = f"相似度 {similarity}%"
        if similarity < 60:
            extra += "，相似度较低，仅供参考"
    return {
        "ok": True,
        "miss": False,
        "error": "",
        "match": _match(similarity, title, author, links, extra),
    }


class _Ascii2dResultParser(HTMLParser):
    """Collect ascii2d result blocks (``div.row``) with titles, links and images.

    ascii2d result pages are a flat list of Bootstrap rows; the parser keeps one entry
    per ``div.row`` so the caller can pick the first block that actually carries a
    result (an ``<h6>`` title or external links plus a thumbnail).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._current: dict | None = None
        self._div_depth = 0
        self._h6_depth = 0
        self._anchor_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "div":
            if self._current is not None:
                self._div_depth += 1
                return
            if "row" not in attributes.get("class", "").split():
                return
            self._current = {"title": [], "links": [], "images": [], "text": []}
            self._div_depth = 0
            return
        if self._current is None:
            return
        if tag == "h6":
            self._h6_depth += 1
        elif tag == "a":
            self._anchor_depth += 1
            href = attributes.get("href", "").strip()
            if href:
                self._current["links"].append({"href": href, "text": ""})
        elif tag == "img":
            src = attributes.get("src", "").strip()
            if src:
                self._current["images"].append(src)

    def handle_data(self, data) -> None:
        if self._current is None:
            return
        text = " ".join(data.split())
        if not text:
            return
        self._current["text"].append(text)
        if self._h6_depth:
            self._current["title"].append(text)
        if self._anchor_depth and self._current["links"]:
            link = self._current["links"][-1]
            if not link["text"]:
                link["text"] = text

    def handle_endtag(self, tag) -> None:
        if tag == "h6" and self._h6_depth:
            self._h6_depth -= 1
        elif tag == "a" and self._anchor_depth:
            self._anchor_depth -= 1
        elif tag == "div" and self._current is not None:
            if self._div_depth:
                self._div_depth -= 1
            else:
                self.blocks.append(self._current)
                self._current = None

    def close(self) -> None:
        super().close()
        if self._current is not None:
            # Tolerate truncated markup: a half open block is still a candidate result.
            self.blocks.append(self._current)
            self._current = None


def _ascii2d_label(raw: str) -> str:
    """Return the retrieval-pass label of the page (§4.3), ``""`` when absent."""
    best: tuple[int, str] | None = None
    for label, markers in _ASCII2D_LABELS:
        for marker in markers:
            position = raw.find(marker)
            if position >= 0 and (best is None or position < best[0]):
                best = (position, label)
    return best[1] if best else ""


def parse_ascii2d(text: str) -> dict:
    """Parse an ascii2d result page.

    Args:
        text: Raw HTML of the page returned by ``POST https://ascii2d.net/search/uri``.

    Returns:
        ``{"ok": bool, "miss": bool, "match": dict | None, "error": str}``.
        The first block carrying a ``<h6>`` title (or external links plus a thumbnail)
        becomes the match; a page without any such block is a ``miss``.
    """
    raw = text or ""
    if not raw.strip():
        return _parse_failure("响应为空")
    if "<" not in raw:
        return _parse_failure("响应不是 HTML")
    parser = _Ascii2dResultParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as exc:  # malformed HTML must never escape (§4.1)
        return _parse_failure(f"{type(exc).__name__}: {exc}")
    label = _ascii2d_label(raw)
    for block in parser.blocks:
        title = " ".join(block["title"]).strip()
        links = _clean_links([link["href"] for link in block["links"]])
        if not title and not (links and block["images"]):
            continue
        author = ""
        for link in block["links"]:
            text_value = link["text"].strip()
            if not text_value or text_value == title or text_value == label:
                continue
            if link["href"].startswith(("http://", "https://")):
                author = text_value
                break
        return {
            "ok": True,
            "miss": False,
            "error": "",
            "match": _match(None, title, author, links, label),
        }
    return {
        "ok": False,
        "miss": True,
        "match": None,
        "error": "ascii2d 未找到匹配结果。",
    }


# Frozen parse-function dispatch (§4.5): text in, match/miss out, no network.
_PARSERS = {
    "trace_moe": parse_trace_moe,
    "saucenao": parse_saucenao,
    "ascii2d": parse_ascii2d,
}


# --------------------------------------------------------------------------------------
# challenge detection (§4.4) — shared by all three adapters
# --------------------------------------------------------------------------------------


def is_challenge(text: str, status: int, content_type: str) -> bool:
    """Detect a Cloudflare / bot-protection interstitial (§4.4).

    Rules (any hit wins):

    1. Body starts with ``<!DOCTYPE html`` (first 200 stripped chars) and contains a
       Cloudflare challenge marker.
    2. Status 403/429 with a ``text/html`` content type.
    3. Status 403/429 with an empty body, or a short body that is not JSON.

    Args:
        text: Raw response body.
        status: HTTP status code.
        content_type: Response ``Content-Type`` header value.

    Returns:
        True when the response must be treated as a challenge page.
    """
    body = text or ""
    lowered = body.lower()
    head = lowered.strip()[:200]
    if head.startswith("<!doctype html") and any(
        marker in lowered for marker in CHALLENGE_MARKERS
    ):
        return True
    if status in CHALLENGE_STATUSES:
        if "text/html" in (content_type or "").lower():
            return True
        stripped = body.strip()
        if not stripped:
            return True
        if len(stripped) < 2048:
            try:
                json.loads(stripped)
            except ValueError:
                return True
    return False


# --------------------------------------------------------------------------------------
# HTTP layer (§4.3) + unified entry point (§4.1)
# --------------------------------------------------------------------------------------


def _build_endpoint(origin: str, path: str, base_url) -> str:
    """Join a configured ``base_url`` (self-hosted proxy) with the engine path.

    A ``base_url`` that already ends with the engine path (or a leading part of it) is
    respected instead of being duplicated, so both ``https://proxy.tld`` and
    ``https://proxy.tld/search.php`` work.

    Args:
        origin: Built-in endpoint origin.
        path: Built-in endpoint path.
        base_url: Configured override, may be empty.

    Returns:
        The absolute request URL.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return origin + path
    path_parts = path.strip("/").split("/")
    base_parts = base.split("/")
    overlap = 0
    for size in range(1, len(path_parts) + 1):
        if base_parts[-size:] == path_parts[:size]:
            overlap = size
    return "/".join(base_parts[: len(base_parts) - overlap] + path_parts)


def _engine_opts(cfg: dict, engine: str) -> dict:
    """Read the per-engine options injected by core (§4.1) with a list fallback.

    Args:
        cfg: Normalized plugin config (read only).
        engine: Engine name, e.g. ``saucenao``.

    Returns:
        Dict with any of ``base_url`` / ``api_key`` / ``extra_headers`` / ``cookie``.
    """
    options: dict = {}
    config = cfg if isinstance(cfg, dict) else {}
    injected = config.get("_engine_opts")
    if isinstance(injected, dict) and isinstance(injected.get(engine), dict):
        options.update(
            {
                key: value
                for key, value in injected[engine].items()
                if value not in (None, "")
            }
        )
    sources = config.get("search_sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            if str(source.get("engine") or "").strip().lower() != engine:
                continue
            if source.get("enabled") is False:
                continue
            for key in ("base_url", "api_key", "extra_headers", "cookie"):
                if key not in options and source.get(key):
                    options[key] = source[key]
            break
    return options


def _headers(spec: dict, options: dict, endpoint: str) -> dict:
    """Build request headers: browser UA, engine defaults, then configured extras.

    Args:
        spec: Engine HTTP spec.
        options: Per-engine options; ``extra_headers`` may carry a ``Cookie`` value.
        endpoint: Final request URL, used to derive ascii2d site headers.

    Returns:
        Header dict for the request.
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": spec["accept"]}
    if spec.get("site_headers"):
        try:
            parts = urlsplit(endpoint)
        except ValueError:
            # A malformed base_url must not abort the run: site headers are optional.
            parts = None
        if parts is not None and parts.scheme and parts.netloc:
            origin = f"{parts.scheme}://{parts.netloc}"
            headers["Referer"] = origin + "/"
            headers["Origin"] = origin
            headers["Accept-Language"] = "ja,en-US;q=0.8,zh-CN;q=0.6"
    extra = options.get("extra_headers")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, (str, int, float)):
                headers[key] = str(value)
    if "Cookie" not in headers and options.get("cookie"):
        headers["Cookie"] = str(options["cookie"])
    return headers


async def _send(
    client: httpx.AsyncClient, spec_name: str, endpoint: str, url: str, options: dict
) -> httpx.Response:
    """Issue the engine specific request (§4.3).

    Args:
        client: Open httpx client with the effective timeout already configured.
        spec_name: Engine key, one of ``trace_moe`` / ``saucenao`` / ``ascii2d``.
        endpoint: Absolute request URL.
        url: Public image URL handed to the engine.
        options: Per-engine options (SauceNAO ``api_key``).

    Returns:
        The HTTP response.
    """
    if spec_name == "trace_moe":
        return await client.get(endpoint, params={"anilistInfo": "true", "url": url})
    if spec_name == "saucenao":
        params = {"output_type": "2", "numres": "3", "url": url}
        api_key = str(options.get("api_key") or "").strip()
        if api_key:
            params["api_key"] = api_key
        return await client.get(endpoint, params=params)
    return await client.post(endpoint, data={"uri": url})


def _engine_error(spec: dict, body: str) -> str:
    """Extract an engine level ``{"error": ...}`` message from a non-2xx body."""
    try:
        payload = json.loads(body or "")
    except ValueError:
        return ""
    if isinstance(payload, dict):
        message = _first_str(payload.get("error"))
        if message:
            return f"{spec['name']} 返回错误：{message}"
    return ""


async def run_engine(engine: str, image: dict, cfg: dict, timeout_sec: float) -> dict:
    """Run one B-class image-search engine and never raise (§4.1).

    Args:
        engine: ``"saucenao"`` / ``"ascii2d"`` / ``"trace_moe"``; any other value returns
            a clean ``ok=False`` result with a human readable error.
        image: ``{"data_uri": str, "url": str | None}``; engines only use ``url`` (§4.2).
        cfg: Normalized plugin config (read only). Per-engine ``base_url`` / ``api_key``
            / ``extra_headers`` are read from ``cfg["_engine_opts"][engine]`` first and
            from the matching ``search_sources`` entry as a fallback.
        timeout_sec: Effective timeout for this source, applied to httpx as is.

    Returns:
        ``{"engine", "ok", "match", "error", "challenge", "miss"}``; ``ok`` is False
        with an explanatory ``error`` for unknown engines, missing public URL, network
        failures, HTTP errors, challenge pages and parse failures.
    """
    name = str(engine or "").strip().lower()
    spec = _ENGINES.get(name)
    if spec is None:
        return _result(
            str(engine or ""), error=UNKNOWN_ENGINE_ERROR.format(engine=repr(engine))
        )
    url = str((image if isinstance(image, dict) else {}).get("url") or "").strip()
    if not url:
        return _result(name, error=NO_PUBLIC_URL_ERROR)
    options = _engine_opts(cfg, name)
    timeout = _to_float(timeout_sec) or DEFAULT_TIMEOUT_SEC
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SEC
    endpoint = _build_endpoint(spec["origin"], spec["path"], options.get("base_url"))
    headers = _headers(spec, options, endpoint)
    # 密文清单：options["api_key"]（SauceNAO 走查询参数）+ extra_headers 全部 value +
    # endpoint / base_url 的 query 值（反代常把 key 写进 URL，httpx 异常文本会回显整条 URL）
    secrets = collect_secrets(
        options.get("api_key"),
        options.get("extra_headers") or options.get("headers"),
        (
            endpoint,
            options.get("base_url") or "",
            spec.get("origin") or "",
        ),
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=True, headers=headers
        ) as client:
            response = await _send(client, name, endpoint, url, options)
    except httpx.TimeoutException:
        return _result(
            name,
            error=sanitize_secrets(
                f"{spec['name']} 请求超时（{timeout:g} 秒），已跳过该源。", secrets
            ),
        )
    except httpx.HTTPError as exc:
        return _result(
            name,
            error=sanitize_secrets(
                f"{spec['name']} 网络请求失败：{type(exc).__name__}: {exc}", secrets
            ),
        )
    except Exception as exc:  # adapter bugs must surface as a clean source failure
        return _result(
            name,
            error=sanitize_secrets(
                f"{spec['name']} 适配器内部错误：{type(exc).__name__}: {exc}", secrets
            ),
        )
    body = response.text or ""
    status = int(getattr(response, "status_code", 0) or 0)
    content_type = str(response.headers.get("content-type", ""))
    if is_challenge(body, status, content_type):
        return _result(name, error=CHALLENGE_ERROR, challenge=True)
    if status not in (200, 201):
        error = _engine_error(spec, body)
        if error:
            # trace.moe answers HTTP 404 with {"error": "<原文>"} (PRD §8).
            return _result(name, error=sanitize_secrets(error, secrets))
        snippet = " ".join(body.split())[:200]
        return _result(
            name,
            error=sanitize_secrets(
                f"{spec['name']} 返回 HTTP {status}：{snippet or '（空响应）'}", secrets
            ),
        )
    parsed = _PARSERS[name](body)
    return _result(
        name,
        ok=bool(parsed.get("ok")),
        match=parsed.get("match"),
        error=sanitize_secrets(str(parsed.get("error") or ""), secrets),
        miss=bool(parsed.get("miss")),
    )
