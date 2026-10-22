"""Offline + network contract tests for the B-class engine adapters.

Fixture strategy: ``PRD.md`` §4.6 lists nine fixture files. The task contract for
engine-smith scopes changes to exactly two paths
(``search_engines.py`` and this file), so the fixture payloads live in ``FIXTURES``
below under their frozen file names instead of being written to
``tests/fixtures/``. Every branch of §4.3 / §4.4 / §4.5 is exercised from these
payloads: the parsing layer is pure text-in / result-out, and the HTTP layer is driven
through a fake ``httpx.AsyncClient``.

Network tests follow the frozen verification stance (PRD §8): only trace.moe may be
asserted against a real response, while SauceNAO / ascii2d are Cloudflare-blocked in
this sandbox and are considered contract-satisfying when ``challenge=True``.
"""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

# Import the plugin module as a package member, from any working directory.
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

se = importlib.import_module("astrbot_plugin_image_search.search_engines")

RESULT_KEYS = {"engine", "ok", "match", "error", "challenge", "miss"}
MATCH_KEYS = {"similarity", "title", "author", "links", "extra"}
ENGINES = ("saucenao", "ascii2d", "trace_moe")

DATA_URI = "data:image/png;base64,iVBORw0KGgo="
PUBLIC_IMAGE = {"data_uri": DATA_URI, "url": "https://img.example.com/abc123.png"}
LOCAL_ONLY_IMAGE = {"data_uri": DATA_URI, "url": None}

LIVE_IMAGE = {"data_uri": DATA_URI, "url": "https://example.com/x.jpg"}

CF_CHALLENGE_HTML = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
    '<meta name="robots" content="noindex,nofollow">'
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=abc123">'
    '</script></head><body><div class="main-wrapper" role="main">'
    '<div class="main-content"><noscript><div id="challenge-error-text">'
    "Enable JavaScript and cookies to continue</div></noscript>"
    '<h1 class="zone-name-title h1">example.com</h1>'
    '<p id="challenge-running">Verifying you are human. This may take a few seconds.</p>'
    '<div id="cf-chl-widget-widget"></div></div></div></body></html>'
)

ASCII2D_OK_HTML = """<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"><title>ascii2d - 検索結果</title></head>
<body>
<div class="container">
  <div class="row">
    <div class="col-md-12 col-sm-12 col-xs-12"><h5>色合検索</h5></div>
  </div>
  <div class="row">
    <div class="col-md-2 col-sm-2 col-xs-4">
      <div class="thumbnail">
        <img class="img-responsive" src="/system/item/583dba4fbf35b8b9e0dd8bc0fceb62e6.jpg" alt="">
      </div>
    </div>
    <div class="col-md-10 col-sm-10 col-xs-8">
      <h6><a href="https://www.pixiv.net/artworks/98765432">サンプル作品名</a></h6>
      <p class="text-muted"><a href="https://twitter.com/sample_artist">サンプル絵師</a></p>
      <div class="well well-sm"><small>色合検索</small><small>pixiv</small></div>
    </div>
  </div>
  <div class="row">
    <div class="col-md-2 col-sm-2 col-xs-4"><img src="/system/item/second.jpg" alt=""></div>
    <div class="col-md-10 col-sm-10 col-xs-8">
      <h6><a href="https://example.com/second-result">第二結果</a></h6>
    </div>
  </div>
</div>
</body>
</html>
"""

ASCII2D_EMPTY_HTML = """<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"><title>ascii2d</title></head>
<body>
<div class="container">
  <div class="row">
    <div class="col-md-12 col-sm-12 col-xs-12"><h5>色合検索</h5></div>
  </div>
  <div class="row">
    <div class="col-md-12">
      <div class="alert alert-warning">該当する結果が見つかりませんでした</div>
    </div>
  </div>
</div>
</body>
</html>
"""

# PRD §4.6 payloads, keyed by their frozen file names.
TRAEMOE_OK = {
    "frameCount": 128,
    "error": "",
    "result": [
        {
            "anilist": {
                "id": 12345,
                "title": {"native": "作品名", "romaji": "Sakuhin"},
            },
            "episode": 12,
            "from": 271.5,
            "similarity": 0.9123,
            "video": "https://media.trace.moe/video/12345/ep12.mp4",
        }
    ],
}
TRAEMOE_ERROR = {"error": "Failed to fetch image https://example.com/x.jpg"}

SAUCENAO_OK = {
    "results": [
        {
            "header": {"similarity": "92.41", "index_id": 5},
            "data": {
                "title": "作品名",
                "author_name": "作者",
                "ext_urls": [
                    "https://www.pixiv.net/artworks/123456",
                    "https://www.pixiv.net/artworks/123456",
                ],
            },
        }
    ]
}

FIXTURES = {
    "tracemoe_ok.json": json.dumps(TRAEMOE_OK, ensure_ascii=False),
    "tracemoe_empty.json": json.dumps({"result": []}, ensure_ascii=False),
    "tracemoe_error.json": json.dumps(TRAEMOE_ERROR, ensure_ascii=False),
    "saucenao_ok.json": json.dumps(SAUCENAO_OK, ensure_ascii=False),
    "saucenao_empty.json": json.dumps({"results": []}, ensure_ascii=False),
    "saucenao_challenge.html": CF_CHALLENGE_HTML,
    "ascii2d_ok.html": ASCII2D_OK_HTML,
    "ascii2d_empty.html": ASCII2D_EMPTY_HTML,
    "ascii2d_challenge.html": CF_CHALLENGE_HTML,
}

# Extra payloads for the §4.3 fallback chains (not part of the §4.6 list).
TRAEMOE_FALLBACK_JSON = json.dumps(
    {
        "result": [
            {
                "anilist": {
                    "id": 999,
                    "title": {"romaji": "Romaji Only", "english": "EN"},
                },
                "episode": 3,
                "from": 61.0,
                "to": 66.25,
                "similarity": 0.5,
                "filename": "file_ep03.mkv",
            }
        ]
    },
    ensure_ascii=False,
)
TRAEMOE_BARE_JSON = json.dumps(
    {"result": [{"similarity": 0.4, "filename": "sample.mp4"}]}, ensure_ascii=False
)
SAUCENAO_FALLBACK_JSON = json.dumps(
    {
        "results": [
            {
                "header": {"similarity": "45.6"},
                "data": {
                    "jp_name": "低相似作品",
                    "creator": "低相似作者",
                    "pixiv_id": 654321,
                    "ext_urls": [],
                },
            }
        ]
    },
    ensure_ascii=False,
)


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` (status / headers / text)."""

    def __init__(self, status_code=200, text="", content_type="application/json"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type} if content_type else {}


def _install_fake_http(monkeypatch, response, calls):
    """Replace ``httpx.AsyncClient`` with a recorder that replays one response."""

    class _FakeClient:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, params=None, headers=None):
            calls.append(
                {"method": "GET", "url": url, "params": params, "headers": headers}
            )
            return response

        async def post(self, url, data=None, headers=None):
            calls.append(
                {"method": "POST", "url": url, "data": data, "headers": headers}
            )
            return response

    monkeypatch.setattr(se.httpx, "AsyncClient", _FakeClient)


def _install_failing_http(monkeypatch, exc):
    """Replace ``httpx.AsyncClient`` with a stub whose requests raise ``exc``."""

    class _FailingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, *args, **kwargs):
            raise exc

        async def post(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr(se.httpx, "AsyncClient", _FailingClient)


def _run_engine(
    monkeypatch,
    engine,
    response,
    image=None,
    cfg=None,
    timeout_sec=15.0,
):
    """Run ``run_engine`` against a fake HTTP response; return (result, recorded calls)."""
    calls = []
    _install_fake_http(monkeypatch, response, calls)
    result = asyncio.run(
        se.run_engine(engine, image or PUBLIC_IMAGE, cfg or {}, timeout_sec)
    )
    return result, calls


def _assert_contract_shape(result, engine_name=None):
    """Assert the frozen six-key shape and the mutually exclusive flag semantics."""
    assert set(result) == RESULT_KEYS
    if engine_name is not None:
        assert result["engine"] == engine_name
    assert isinstance(result["ok"], bool)
    assert isinstance(result["challenge"], bool)
    assert isinstance(result["miss"], bool)
    assert isinstance(result["error"], str)
    if result["ok"]:
        assert result["error"] == ""
        assert result["miss"] is False
        assert result["challenge"] is False
        assert result["match"] is not None
        assert set(result["match"]) == MATCH_KEYS
        match = result["match"]
        assert isinstance(match["title"], str)
        assert isinstance(match["author"], str)
        assert isinstance(match["extra"], str)
        assert isinstance(match["links"], list)
        assert len(match["links"]) <= se.MAX_LINKS
        for link in match["links"]:
            assert link.startswith(("http://", "https://"))
        assert match["similarity"] is None or isinstance(match["similarity"], float)
    else:
        assert result["match"] is None
        assert result["error"] != ""
        if result["challenge"] or result["miss"]:
            assert not (result["challenge"] and result["miss"])


# --------------------------------------------------------------------------------------
# fixtures and parsing layer (§4.5, §4.6)
# --------------------------------------------------------------------------------------


def test_fixture_inventory_matches_prd_section_4_6():
    expected = {
        "tracemoe_ok.json",
        "tracemoe_empty.json",
        "tracemoe_error.json",
        "saucenao_ok.json",
        "saucenao_empty.json",
        "saucenao_challenge.html",
        "ascii2d_ok.html",
        "ascii2d_empty.html",
        "ascii2d_challenge.html",
    }
    assert expected <= set(FIXTURES)
    for name, payload in FIXTURES.items():
        assert isinstance(payload, str) and payload.strip(), name


def test_parse_trace_moe_ok():
    parsed = se.parse_trace_moe(FIXTURES["tracemoe_ok.json"])
    assert parsed["ok"] is True
    assert parsed["miss"] is False
    assert parsed["error"] == ""
    match = parsed["match"]
    assert match["similarity"] == pytest.approx(91.23)
    assert isinstance(match["similarity"], float)
    assert match["title"] == "作品名"
    assert match["author"] == ""
    assert match["links"] == [
        "https://anilist.co/anime/12345",
        "https://media.trace.moe/video/12345/ep12.mp4",
    ]
    assert match["extra"].startswith("第 12 集 04:31")


def test_parse_trace_moe_empty_is_miss():
    parsed = se.parse_trace_moe(FIXTURES["tracemoe_empty.json"])
    assert parsed["ok"] is False
    assert parsed["miss"] is True
    assert parsed["match"] is None
    assert parsed["error"]


def test_parse_trace_moe_error_keeps_original_text():
    parsed = se.parse_trace_moe(FIXTURES["tracemoe_error.json"])
    assert parsed["ok"] is False
    assert parsed["miss"] is False
    assert parsed["match"] is None
    assert "Failed to fetch image https://example.com/x.jpg" in parsed["error"]


def test_parse_trace_moe_fallbacks_and_bad_input():
    fallback = se.parse_trace_moe(TRAEMOE_FALLBACK_JSON)["match"]
    assert fallback["title"] == "Romaji Only"
    assert fallback["similarity"] == pytest.approx(50.0)
    assert fallback["extra"] == "第 3 集 01:01-01:06 file_ep03.mkv"
    bare = se.parse_trace_moe(TRAEMOE_BARE_JSON)["match"]
    assert bare["title"] == "sample.mp4"
    assert bare["links"] == []
    assert bare["extra"] == "sample.mp4"
    for bad in ("", "   ", "not json", "[1, 2, 3]", None):
        parsed = se.parse_trace_moe(bad)
        assert parsed["ok"] is False
        assert parsed["miss"] is False
        assert parsed["match"] is None
        assert parsed["error"]


def test_parse_saucenao_ok():
    parsed = se.parse_saucenao(FIXTURES["saucenao_ok.json"])
    assert parsed["ok"] is True
    assert parsed["error"] == ""
    match = parsed["match"]
    assert match["similarity"] == pytest.approx(92.41)
    assert match["title"] == "作品名"
    assert match["author"] == "作者"
    # ext_urls are de-duplicated, order preserving (§4.3).
    assert match["links"] == ["https://www.pixiv.net/artworks/123456"]
    assert match["extra"] == "相似度 92.41%"


def test_parse_saucenao_empty_is_miss():
    parsed = se.parse_saucenao(FIXTURES["saucenao_empty.json"])
    assert parsed["ok"] is False
    assert parsed["miss"] is True
    assert parsed["match"] is None
    assert parsed["error"]


def test_parse_saucenao_fallbacks_and_bad_input():
    match = se.parse_saucenao(SAUCENAO_FALLBACK_JSON)["match"]
    assert match["similarity"] == pytest.approx(45.6)
    assert match["title"] == "低相似作品"
    assert match["author"] == "低相似作者"
    # pixiv_id only becomes a link when ext_urls yields nothing (§4.3 note).
    assert match["links"] == ["https://www.pixiv.net/artworks/654321"]
    assert match["extra"] == "相似度 45.6%，相似度较低，仅供参考"
    for bad in ("", "  ", "<html>nope</html>", "[]"):
        parsed = se.parse_saucenao(bad)
        assert parsed["ok"] is False
        assert parsed["miss"] is False
        assert parsed["error"]
    assert "boom" in se.parse_saucenao('{"error": "boom"}')["error"]


def test_parse_ascii2d_ok():
    parsed = se.parse_ascii2d(FIXTURES["ascii2d_ok.html"])
    assert parsed["ok"] is True
    assert parsed["error"] == ""
    match = parsed["match"]
    assert match["similarity"] is None  # ascii2d has no similarity notion (§4.3)
    assert match["title"] == "サンプル作品名"
    assert match["author"] == "サンプル絵師"
    assert match["links"] == [
        "https://www.pixiv.net/artworks/98765432",
        "https://twitter.com/sample_artist",
    ]
    assert match["extra"] == "色合检索"


def test_parse_ascii2d_empty_is_miss():
    parsed = se.parse_ascii2d(FIXTURES["ascii2d_empty.html"])
    assert parsed["ok"] is False
    assert parsed["miss"] is True
    assert parsed["match"] is None
    assert parsed["error"]


def test_parse_ascii2d_invalid_input():
    for bad in ("", "   ", "plain text without markup"):
        parsed = se.parse_ascii2d(bad)
        assert parsed["ok"] is False
        assert parsed["miss"] is False
        assert parsed["match"] is None
        assert parsed["error"]


def test_ascii2d_label_detection_accepts_both_spellings():
    assert se._ascii2d_label("<h5>色合検索</h5>") == "色合检索"
    assert se._ascii2d_label("<h5>色合检索</h5>") == "色合检索"
    assert se._ascii2d_label("<h5>特徴検索</h5>") == "特征检索"
    assert se._ascii2d_label("<h5>特征检索</h5>") == "特征检索"
    assert se._ascii2d_label("<html></html>") == ""


# --------------------------------------------------------------------------------------
# challenge detection (§4.4)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "status", "content_type", "expected"),
    [
        (CF_CHALLENGE_HTML, 403, "text/html; charset=UTF-8", True),
        (CF_CHALLENGE_HTML, 200, "text/html; charset=UTF-8", True),
        (CF_CHALLENGE_HTML, 429, "text/html", True),
        (
            "<!DOCTYPE html> <html><head><title>Attention Required! | Cloudflare</title>",
            503,
            "text/html",
            True,
        ),
        ("", 403, "text/plain", True),
        ("blocked", 403, "text/plain", True),
        ('{"error": "nope"}', 403, "application/json", False),
        ("", 500, "text/html", False),
        (FIXTURES["saucenao_ok.json"], 200, "application/json", False),
        (FIXTURES["ascii2d_ok.html"], 200, "text/html", False),
        (
            FIXTURES["tracemoe_error.json"],
            404,
            "application/json; charset=utf-8",
            False,
        ),
        ("short body", 200, "text/plain", False),
    ],
)
def test_is_challenge_rules(text, status, content_type, expected):
    assert se.is_challenge(text, status, content_type) is expected


# --------------------------------------------------------------------------------------
# run_engine: routing, URL requirement and unknown engines (§4.1, §4.2)
# --------------------------------------------------------------------------------------


def test_unknown_engine_returns_human_error_without_raising(monkeypatch):
    result, calls = _run_engine(
        monkeypatch, "google_lens", _FakeResponse(text="{}"), image=PUBLIC_IMAGE
    )
    _assert_contract_shape(result)
    assert result["ok"] is False
    assert result["challenge"] is False
    assert result["miss"] is False
    assert "saucenao" in result["error"] and "trace_moe" in result["error"]
    assert calls == []  # never touches the network


@pytest.mark.parametrize("engine", ENGINES)
def test_missing_public_url_is_skipped_with_hint(monkeypatch, engine):
    result, calls = _run_engine(
        monkeypatch, engine, _FakeResponse(text="{}"), LOCAL_ONLY_IMAGE
    )
    _assert_contract_shape(result, engine)
    assert result["ok"] is False
    assert result["challenge"] is False
    assert result["miss"] is False
    assert result["error"].startswith("no_public_url")
    assert "image_url" in result["error"] and "图床" in result["error"]
    assert calls == []  # no request is issued without a public URL


@pytest.mark.parametrize("engine", ("SauceNAO", "TRACE_MOE", "ascii2d"))
def test_engine_name_is_case_insensitive(monkeypatch, engine):
    response = _FakeResponse(text=FIXTURES["tracemoe_ok.json"])
    result, _ = _run_engine(monkeypatch, engine, response)
    _assert_contract_shape(result, engine.lower())


# --------------------------------------------------------------------------------------
# run_engine: hit / miss / error paths per engine
# --------------------------------------------------------------------------------------


def test_run_engine_trace_moe_hit(monkeypatch):
    result, calls = _run_engine(
        monkeypatch, "trace_moe", _FakeResponse(text=FIXTURES["tracemoe_ok.json"])
    )
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is True
    assert result["match"]["similarity"] == pytest.approx(91.23)
    assert result["match"]["title"] == "作品名"
    assert result["match"]["extra"]
    assert result["match"]["links"] == [
        "https://anilist.co/anime/12345",
        "https://media.trace.moe/video/12345/ep12.mp4",
    ]
    request = calls[1]
    assert request["method"] == "GET"
    assert request["url"] == "https://api.trace.moe/search"
    assert request["params"] == {
        "anilistInfo": "true",
        "url": PUBLIC_IMAGE["url"],
    }


def test_run_engine_trace_moe_error_body_is_not_miss(monkeypatch):
    result, _ = _run_engine(
        monkeypatch,
        "trace_moe",
        _FakeResponse(
            status_code=404,
            text=FIXTURES["tracemoe_error.json"],
            content_type="application/json; charset=utf-8",
        ),
    )
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is False
    assert result["miss"] is False
    assert result["challenge"] is False
    assert "Failed to fetch image https://example.com/x.jpg" in result["error"]


def test_run_engine_trace_moe_empty_is_miss(monkeypatch):
    result, _ = _run_engine(
        monkeypatch, "trace_moe", _FakeResponse(text=FIXTURES["tracemoe_empty.json"])
    )
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is False
    assert result["miss"] is True
    assert result["challenge"] is False


def test_run_engine_saucenao_hit(monkeypatch):
    result, calls = _run_engine(
        monkeypatch, "saucenao", _FakeResponse(text=FIXTURES["saucenao_ok.json"])
    )
    _assert_contract_shape(result, "saucenao")
    assert result["ok"] is True
    assert result["match"]["similarity"] == pytest.approx(92.41)
    assert result["match"]["author"] == "作者"
    request = calls[1]
    assert request["url"] == "https://saucenao.com/search.php"
    assert request["params"] == {
        "output_type": "2",
        "numres": "3",
        "url": PUBLIC_IMAGE["url"],
    }


def test_run_engine_saucenao_empty_is_miss(monkeypatch):
    result, _ = _run_engine(
        monkeypatch, "saucenao", _FakeResponse(text=FIXTURES["saucenao_empty.json"])
    )
    _assert_contract_shape(result, "saucenao")
    assert result["ok"] is False
    assert result["miss"] is True
    assert result["challenge"] is False


def test_run_engine_ascii2d_hit(monkeypatch):
    result, calls = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(text=FIXTURES["ascii2d_ok.html"], content_type="text/html"),
    )
    _assert_contract_shape(result, "ascii2d")
    assert result["ok"] is True
    assert result["match"]["title"] == "サンプル作品名"
    assert result["match"]["similarity"] is None
    request = calls[1]
    assert request["method"] == "POST"
    assert request["url"] == "https://ascii2d.net/search/uri"
    assert request["data"] == {"uri": PUBLIC_IMAGE["url"]}


def test_run_engine_ascii2d_empty_is_miss(monkeypatch):
    result, _ = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(text=FIXTURES["ascii2d_empty.html"], content_type="text/html"),
    )
    _assert_contract_shape(result, "ascii2d")
    assert result["ok"] is False
    assert result["miss"] is True
    assert result["challenge"] is False


# --------------------------------------------------------------------------------------
# run_engine: challenge and HTTP error handling (§4.4)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_challenge_page_is_never_parsed(monkeypatch, engine):
    """A CF page served with HTTP 200 must still take the challenge branch."""
    result, _ = _run_engine(
        monkeypatch,
        engine,
        _FakeResponse(
            status_code=200, text=CF_CHALLENGE_HTML, content_type="text/html"
        ),
    )
    _assert_contract_shape(result, engine)
    assert result["ok"] is False
    assert result["challenge"] is True
    assert result["miss"] is False
    assert result["match"] is None
    assert "人机校验" in result["error"]
    assert "反代" in result["error"] and "api_key" in result["error"]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("status", (403, 429))
def test_http_403_429_are_challenges(monkeypatch, engine, status):
    result, _ = _run_engine(
        monkeypatch,
        engine,
        _FakeResponse(
            status_code=status, text=CF_CHALLENGE_HTML, content_type="text/html"
        ),
    )
    _assert_contract_shape(result, engine)
    assert result["ok"] is False
    assert result["challenge"] is True
    assert result["match"] is None


def test_http_403_with_short_html_body_is_challenge(monkeypatch):
    result, _ = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(
            status_code=403, text="<html>Blocked</html>", content_type="text/html"
        ),
    )
    _assert_contract_shape(result, "ascii2d")
    assert result["challenge"] is True


@pytest.mark.parametrize("engine", ENGINES)
def test_non_challenge_http_error_is_plain_error(monkeypatch, engine):
    result, _ = _run_engine(
        monkeypatch,
        engine,
        _FakeResponse(
            status_code=500, text="<html>boom</html>", content_type="text/html"
        ),
    )
    _assert_contract_shape(result, engine)
    assert result["ok"] is False
    assert result["challenge"] is False
    assert result["miss"] is False
    assert "HTTP 500" in result["error"]


def test_engine_level_json_error_on_http_error_status(monkeypatch):
    result, _ = _run_engine(
        monkeypatch,
        "saucenao",
        _FakeResponse(status_code=502, text='{"error": "bad gateway"}'),
    )
    _assert_contract_shape(result, "saucenao")
    assert result["challenge"] is False
    assert "bad gateway" in result["error"]


# --------------------------------------------------------------------------------------
# run_engine: no branch may raise (§4.1)
# --------------------------------------------------------------------------------------


def test_network_exception_becomes_a_clean_error(monkeypatch):
    _install_failing_http(monkeypatch, httpx.ConnectError("connection refused"))
    result = asyncio.run(se.run_engine("trace_moe", PUBLIC_IMAGE, {}, 10.0))
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is False
    assert result["challenge"] is False
    assert result["miss"] is False
    assert "网络请求失败" in result["error"]


def test_timeout_exception_becomes_a_clean_error(monkeypatch):
    _install_failing_http(monkeypatch, httpx.ReadTimeout("timed out"))
    result = asyncio.run(se.run_engine("saucenao", PUBLIC_IMAGE, {}, 5.0))
    _assert_contract_shape(result, "saucenao")
    assert result["challenge"] is False
    assert "请求超时" in result["error"]


def test_unexpected_exception_becomes_a_clean_error(monkeypatch):
    _install_failing_http(monkeypatch, RuntimeError("adapter boom"))
    result = asyncio.run(se.run_engine("ascii2d", PUBLIC_IMAGE, {}, 5.0))
    _assert_contract_shape(result, "ascii2d")
    assert result["challenge"] is False
    assert "适配器内部错误" in result["error"]


@pytest.mark.parametrize("bad_image", ["string-image", None, [], 42])
def test_malformed_image_argument_never_raises(monkeypatch, bad_image):
    calls = []
    _install_fake_http(monkeypatch, _FakeResponse(text="{}"), calls)
    result = asyncio.run(se.run_engine("trace_moe", bad_image, {}, 5.0))
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is False
    assert result["error"].startswith("no_public_url")
    assert calls == []


@pytest.mark.parametrize("bad_timeout", [None, "abc", 0, -3])
def test_invalid_timeout_falls_back_without_raising(monkeypatch, bad_timeout):
    result, calls = _run_engine(
        monkeypatch,
        "trace_moe",
        _FakeResponse(text=FIXTURES["tracemoe_ok.json"]),
        timeout_sec=bad_timeout,
    )
    _assert_contract_shape(result, "trace_moe")
    assert result["ok"] is True
    assert calls[0]["init"]["timeout"].read == pytest.approx(se.DEFAULT_TIMEOUT_SEC)


@pytest.mark.parametrize("bad_base_url", ["http://[::1", "not a url", "///"])
def test_malformed_base_url_never_raises(monkeypatch, bad_base_url):
    cfg = {"_engine_opts": {"ascii2d": {"base_url": bad_base_url}}}
    result, _ = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(text=FIXTURES["ascii2d_ok.html"], content_type="text/html"),
        cfg=cfg,
    )
    _assert_contract_shape(result, "ascii2d")


# --------------------------------------------------------------------------------------
# run_engine: transport configuration (timeout, base_url, api_key, headers)
# --------------------------------------------------------------------------------------


def test_timeout_sec_controls_the_httpx_timeout(monkeypatch):
    _, calls = _run_engine(
        monkeypatch,
        "trace_moe",
        _FakeResponse(text=FIXTURES["tracemoe_ok.json"]),
        timeout_sec=7.5,
    )
    timeout = calls[0]["init"]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == pytest.approx(7.5)
    assert calls[0]["init"]["follow_redirects"] is True


def test_base_url_api_key_and_headers_from_engine_opts(monkeypatch):
    cfg = {
        "_engine_opts": {
            "saucenao": {
                "base_url": "https://proxy.example.com",
                "api_key": "KEY123",
                "extra_headers": {"X-Auth": "tok", "Cookie": "session=abc"},
            }
        }
    }
    _, calls = _run_engine(
        monkeypatch,
        "saucenao",
        _FakeResponse(text=FIXTURES["saucenao_ok.json"]),
        cfg=cfg,
    )
    assert calls[0]["init"]["headers"]["X-Auth"] == "tok"
    assert calls[0]["init"]["headers"]["Cookie"] == "session=abc"
    assert calls[0]["init"]["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert calls[1]["url"] == "https://proxy.example.com/search.php"
    assert calls[1]["params"]["api_key"] == "KEY123"
    assert calls[1]["params"]["output_type"] == "2"


def test_ascii2d_cookie_and_site_headers(monkeypatch):
    cfg = {"_engine_opts": {"ascii2d": {"extra_headers": {"Cookie": "a2d=xyz"}}}}
    _, calls = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(text=FIXTURES["ascii2d_ok.html"], content_type="text/html"),
        cfg=cfg,
    )
    headers = calls[0]["init"]["headers"]
    assert headers["Cookie"] == "a2d=xyz"
    assert headers["Referer"] == "https://ascii2d.net/"
    assert headers["User-Agent"].startswith("Mozilla/5.0")


def test_search_sources_fallback_when_engine_opts_absent(monkeypatch):
    cfg = {
        "search_sources": [
            {
                "enabled": False,
                "engine": "trace_moe",
                "base_url": "https://ignored.tld",
            },
            {
                "enabled": True,
                "engine": "trace_moe",
                "base_url": "https://mirror.example.com",
                "extra_headers": {"X-From-Pool": "1"},
            },
        ]
    }
    _, calls = _run_engine(
        monkeypatch,
        "trace_moe",
        _FakeResponse(text=FIXTURES["tracemoe_ok.json"]),
        cfg=cfg,
    )
    assert calls[0]["init"]["headers"]["X-From-Pool"] == "1"
    assert calls[1]["url"] == "https://mirror.example.com/search"


def test_base_url_can_already_contain_the_engine_path(monkeypatch):
    cfg = {
        "_engine_opts": {"ascii2d": {"base_url": "https://proxy.example.com/search"}}
    }
    _, calls = _run_engine(
        monkeypatch,
        "ascii2d",
        _FakeResponse(text=FIXTURES["ascii2d_ok.html"], content_type="text/html"),
        cfg=cfg,
    )
    assert calls[1]["url"] == "https://proxy.example.com/search/uri"


# --------------------------------------------------------------------------------------
# network-mode contract tests (PRD §8 verification stance)
# --------------------------------------------------------------------------------------


def _live_run(engine, image=LIVE_IMAGE, cfg=None, timeout_sec=20.0):
    return asyncio.run(se.run_engine(engine, image, cfg or {}, timeout_sec))


def test_live_trace_moe_contract():
    result = _live_run("trace_moe")
    _assert_contract_shape(result, "trace_moe")
    if result["ok"]:
        assert result["match"]["title"] or result["match"]["extra"]
        return
    # 404 + {"error": ...} is the documented sandbox response (PRD §8).
    if "trace.moe 返回错误" in result["error"]:
        assert "Failed to fetch image" in result["error"]
        assert result["miss"] is False
        return
    # Otherwise the sandbox simply had no egress: still a clean source failure.
    assert result["error"]
    assert result["challenge"] in (True, False)


@pytest.mark.parametrize("engine", ("saucenao", "ascii2d"))
def test_live_saucenao_and_ascii2d_accept_challenge(engine):
    """Cloudflare is expected here: challenge=True counts as contract satisfied."""
    result = _live_run(engine)
    _assert_contract_shape(result, engine)
    if result["challenge"]:
        assert result["ok"] is False
        assert result["miss"] is False
        assert "人机校验" in result["error"]
        return
    if result["ok"]:
        assert result["match"] is not None
        return
    # Miss or a clean transport error are both acceptable outcomes without egress.
    assert result["error"]
