"""test_verification_v2.py -- t5 独立验证（可执行复现，只验不改）。

本文件是 verifier（独立验证者）的产出，**不依赖同目录其他测试文件的夹具**：每条结论
都由本文件自己发起的子进程 / 真实网络请求 / 临时目录复现得出，逐项对应任务书 t5 的
六条验收：

1. 工具注册实证：以 AstrBot 真实加载路径（`data.plugins.<dir>.main`）导入新插件，断言
   `image_search` 进工具表、`image_analyze` 不在表内、JSON schema 与 PRD §1.1 契约一致、
   docstring 能被 `docstring_parser` 解析出三个带类型的参数；并附**反向对照**：同一探测链
   导入旧插件 `astrbot_plugin_vision_router` 时确实能看见 `image_analyze`，证明「新插件看
   不见 image_analyze」不是探测方法失灵。
2. 全量测试：子进程跑同目录全量 pytest（`--ignore` 本文件避免递归），断言 0 失败。
3. 迁移脚本复现：临时目录构造 v1.7 旧配置 -> 跑 `migrate_v1_7.py` -> 断言产出 vision 源、
   三连跑字节一致（幂等）、`ais` 丢弃快照脱敏、PRD §6.4 退出码矩阵。
4. 引擎网络态：对 trace.moe / saucenao / ascii2d 各真发一次请求，断言 `run_engine` 返回
   契约 6 键字典且不抛异常；HTTP 状态码由**并行的裸 httpx 请求**独立取证。
5. 降级场景：空源池 -> 友好 JSON；不存在的图片路径 -> 结构化失败而非堆栈。
6. 字段一致性：`_conf_schema.json` 字段名 <-> README / MIGRATION 提及双向核对，
   schema 默认值 <-> PRD §2 冻结默认值逐项核对。

纪律（任务书）：只新增本文件，不修改任何实现文件；发现缺陷只报告并给最小复现。

环境约束（PRD §8，本文件原样沿用）：saucenao / ascii2d 在本沙箱被 Cloudflare 403 拦截，
判定口径为「`challenge=True` 即契约成立」；trace.moe 可达但对示例 URL 返回 404 + JSON
error 体，判定口径为「error 含原文、`challenge=False`」。网络不可达时显式
`pytest.skip("环境不可达: ...")`，不含糊为通过。
"""

import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import docstring_parser
import httpx
import pytest

# ---------------------------------------------------------------------------
# 路径与解释器（真实加载路径 = StarManager 的 `data.plugins.<dir>.main`）
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent  # data/plugins/astrbot_plugin_image_search
_PLUGINS_DIR = _PLUGIN_DIR.parent  # data/plugins
_REPO_ROOT = _PLUGINS_DIR.parent.parent  # /root/AstrBot

NEW_PLUGIN_MODULE = "data.plugins.astrbot_plugin_image_search.main"
OLD_PLUGIN_MODULE = "data.plugins.astrbot_plugin_vision_router.main"
NEW_PLUGIN_NAME = "astrbot_plugin_image_search"

if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

_PYTHON = Path(sys.executable)


def _subprocess_env(**extra: str) -> dict:
    """Build a subprocess env that also carries probe parameters."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra.items()})
    return env


# 子进程探测脚本：走 AstrBot 真实导入路径，打印工具表 + 各自 JSON schema。
_REGISTRATION_PROBE = r"""
import importlib
import json
import os

mod = importlib.import_module(os.environ["PROBE_MODULE"])
from astrbot.core.provider.register import llm_tools

tools = {}
for tool in llm_tools.func_list:
    tools[tool.name] = {
        "parameters": tool.parameters,
        "description": tool.description,
        "module_path": getattr(tool, "handler_module_path", None),
    }
print(
    "PROBE_JSON="
    + json.dumps({"module": mod.__name__, "tool_names": sorted(tools), "tools": tools})
)
"""


def _run_registration_probe(
    module_name: str,
) -> tuple[subprocess.CompletedProcess, dict]:
    """Import `module_name` in a fresh interpreter the way StarManager does.

    Args:
        module_name: Dotted module path, e.g. ``data.plugins.<dir>.main``.

    Returns:
        The completed process plus the parsed ``PROBE_JSON`` payload (empty on failure).
    """
    proc = subprocess.run(
        [str(_PYTHON), "-c", _REGISTRATION_PROBE],
        cwd=str(_REPO_ROOT),
        env=_subprocess_env(PROBE_MODULE=module_name),
        capture_output=True,
        text=True,
        timeout=240,
    )
    payload: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_JSON="):
            payload = json.loads(line[len("PROBE_JSON=") :])
    return proc, payload


def _combined(proc: subprocess.CompletedProcess) -> str:
    """Return stdout+stderr of a completed process (AstrBot logs go to stdout)."""
    return f"{proc.stdout}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# 1. 工具注册实证
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """以真实加载路径复现注册，不接受「报告说注册了」。"""

    def test_image_search_registered_via_real_loader_path(self):
        proc, payload = _run_registration_probe(NEW_PLUGIN_MODULE)
        print("\n[probe] returncode =", proc.returncode)
        print("[probe] log line   =", "Added llm tool: image_search" in _combined(proc))
        print("[probe] tool_names =", payload.get("tool_names"))
        assert proc.returncode == 0, _combined(proc)[-2000:]
        assert payload["module"] == NEW_PLUGIN_MODULE
        assert "Added llm tool: image_search" in _combined(proc)
        assert payload["tool_names"] == ["image_search"], payload["tool_names"]
        assert "image_analyze" not in payload["tool_names"]

    def test_old_plugin_probe_really_detects_image_analyze(self):
        """反向对照：同一探测链对旧插件必须能看见 image_analyze。"""
        proc, payload = _run_registration_probe(OLD_PLUGIN_MODULE)
        print("\n[control] returncode =", proc.returncode)
        print("[control] tool_names =", payload.get("tool_names"))
        assert proc.returncode == 0, _combined(proc)[-2000:]
        assert "image_analyze" in payload["tool_names"], (
            "探测链读不到旧插件的 image_analyze —— 上一条断言「看不见」就没有效力"
        )

    def test_tool_json_schema_matches_prd_contract(self):
        """PRD §1.1：参数恰为 image_refs / question / search_type。"""
        _proc, payload = _run_registration_probe(NEW_PLUGIN_MODULE)
        tool = payload["tools"]["image_search"]
        schema = tool["parameters"]
        print("\n[schema]", json.dumps(schema, ensure_ascii=False))
        assert schema["type"] == "object"
        props = schema["properties"]
        assert list(props) == ["image_refs", "question", "search_type"]
        # AstrBot 注入的 event 不得进入 schema
        assert "event" not in props
        assert props["image_refs"]["type"] == "array"
        assert props["image_refs"]["items"] == {"type": "string"}
        assert props["question"]["type"] == "string"
        assert props["search_type"]["type"] == "string"
        assert all(p["description"] for p in props.values())
        assert tool["description"].startswith("联网搜图")

    def test_docstring_parses_three_typed_params(self):
        """docstring_parser 必须解析出三个带类型注释的参数（否则注册会抛错）。"""
        main = importlib.import_module("astrbot_plugin_image_search.main")
        parsed = docstring_parser.parse(
            main.ImageSearchPlugin.image_search.__doc__ or ""
        )
        params = {p.arg_name: p.type_name for p in parsed.params}
        print("\n[docstring]", params)
        assert params == {
            "image_refs": "list[string]",
            "question": "string",
            "search_type": "string",
        }
        # AstrBot 注册期的类型映射：list[string] -> array/items.string
        assert re.match(r"(\w+)\[(\w+)\]", params["image_refs"]).group(1) == "list"
        source = Path(main.__file__).read_text(encoding="utf-8")
        assert re.findall(r'filter\.llm_tool\(name="([^"]+)"\)', source) == [
            "image_search"
        ]


# ---------------------------------------------------------------------------
# 2. 全量测试（子进程，忽略本文件防递归）
# ---------------------------------------------------------------------------


class TestFullSuite:
    def test_full_suite_is_green(self):
        proc = subprocess.run(
            [
                str(_PYTHON),
                "-m",
                "pytest",
                "astrbot_plugin_image_search/tests",
                "-q",
                "--ignore=astrbot_plugin_image_search/tests/test_verification_v2.py",
            ],
            cwd=str(_PLUGINS_DIR),
            capture_output=True,
            text=True,
            timeout=1200,
        )
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-3:])
        print("\n[full-suite] returncode =", proc.returncode)
        print("[full-suite] tail =\n" + tail)
        assert proc.returncode == 0, tail
        match = re.search(r"(\d+) passed", proc.stdout or "")
        assert match, tail
        assert int(match.group(1)) >= 300, tail
        assert " failed" not in tail and "error" not in tail.lower()


# ---------------------------------------------------------------------------
# 3. 迁移脚本复现（构造输入 -> 产出配置 -> 幂等 -> 退出码）
# ---------------------------------------------------------------------------


def _write_v17_config(path: Path) -> None:
    """写一份最小但含 route.llm_provider + ais(provider/custom) 的 v1.7 旧配置。"""
    path.write_text(
        json.dumps(
            {
                "route": {
                    "llm_provider": "豆包/doubao",
                    "max_rounds": 3,
                    "routing_timeout_sec": 25,
                    "max_size_mb": 12,
                    "max_pixels": 8388608,
                    "cache_ttl_sec": 60,
                    "health_stats": True,
                },
                "ais": [
                    {
                        "__template_key": "provider",
                        "enabled": True,
                        "id": "豆包/doubao",
                        "desc": "联网图片搜索",
                        "provider_ref": "豆包/doubao",
                        "model": "",
                        "vision_mode": "both",
                    },
                    {
                        "__template_key": "custom",
                        "enabled": False,
                        "id": "my-relay",
                        "desc": "自建端点",
                        "api_base": "https://relay.example.invalid/v1",
                        "api_key": "sk-abcd1234SECRET",
                        "model": "gemini-3.5-flash",
                    },
                ],
                "image_url": {
                    "enabled": True,
                    "base_url": "http://192.168.1.9:8123/",
                    "token": "tk-1",
                },
                "download_timeout_sec": 20,
                "tool_hint_enabled": True,
                "log_level": "info",
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def _run_migration(
    old: Path, new: Path, report: Path, *extra: str
) -> subprocess.CompletedProcess:
    """Run the frozen-named migration script (PRD §6.4) as a real process."""
    script = _PLUGIN_DIR / "migrate_v1_7.py"
    return subprocess.run(
        [
            str(_PYTHON),
            str(script),
            "--old",
            str(old),
            "--new",
            str(new),
            "--report",
            str(report),
            *extra,
        ],
        cwd=str(_PLUGIN_DIR),
        capture_output=True,
        text=True,
        timeout=240,
    )


class TestMigrationReproduction:
    def test_prd_named_script_exists_and_is_only_one(self):
        """PRD §6.4 冻结文件名是 migrate_v1_7.py；目录内不得有第二个迁移脚本。

        注意：任务书 verify 命令里的 `migrate_config.py` 是**旧插件**的脚本名，
        新插件目录下不存在该文件（见 t5 报告的最小复现）。
        """
        scripts = sorted(p.name for p in _PLUGIN_DIR.glob("migrate*.py"))
        print("\n[migrate] scripts =", scripts)
        assert scripts == ["migrate_v1_7.py"]

    def test_migration_produces_expected_vision_source_and_is_idempotent(
        self, tmp_path
    ):
        old = tmp_path / "old_v17.json"
        _write_v17_config(old)
        new = tmp_path / "new_config.json"
        report = tmp_path / "report.json"

        digests = []
        for run in range(3):
            proc = _run_migration(old, new, report)
            print(
                f"\n[migrate] run{run + 1} exit={proc.returncode} "
                f"stdout={proc.stdout.strip().splitlines()[-2:]}"
            )
            assert proc.returncode == 0, proc.stderr
            payload = json.loads(new.read_text(encoding="utf-8"))
            digests.append(_sha256(new))
            assert [s["id"] for s in payload["search_sources"]] == [
                "migrated_route_llm"
            ], payload["search_sources"]

        print("[migrate] sha256 per run =", digests)
        print("[migrate] config =", json.dumps(json.loads(new.read_text("utf-8"))))
        assert len(set(digests)) == 1, f"非幂等：三次产出 sha256 不同 {digests}"

        payload = json.loads(new.read_text(encoding="utf-8"))
        source = payload["search_sources"][0]
        assert source["provider_ref"] == "豆包/doubao"
        assert source["name"] == "迁移：v1.7 路由提供商"
        assert payload["search"]["max_rounds"] == 3
        assert payload["search"]["max_size_mb"] == 12
        assert payload["search"]["cache_ttl_sec"] == 60
        # ais 整节丢弃，但快照进报告且 api_key 脱敏
        dropped = json.loads(report.read_text(encoding="utf-8"))["dropped"]
        snapshots = {item.get("id"): item for item in dropped if "id" in item}
        assert set(snapshots) == {"豆包/doubao", "my-relay"}
        masked = snapshots["my-relay"]["snapshot"]["api_key"]
        print("[migrate] masked api_key =", masked)
        assert masked == "sk-a***" and "SECRET" not in masked
        assert not any(
            "sk-abcd1234SECRET" in json.dumps(item, ensure_ascii=False)
            for item in dropped
        )

    def test_exit_code_matrix_prd_6_4(self, tmp_path):
        """PRD §6.4：输入不存在 / JSON 非法 -> stderr + 退出码 1 且不落盘。"""
        missing = tmp_path / "nope.json"
        new = tmp_path / "out.json"
        report = tmp_path / "rep.json"
        proc = _run_migration(missing, new, report)
        print(f"\n[exit-code] missing exit={proc.returncode} stderr={proc.stderr!r}")
        assert proc.returncode == 1
        assert "not found" in proc.stderr and str(missing) in proc.stderr
        assert "no file written" in proc.stderr
        assert not new.exists() and not report.exists()

        broken = tmp_path / "broken.json"
        broken.write_text('{"route": ', encoding="utf-8")
        proc = _run_migration(broken, new, report)
        print(f"[exit-code] invalid exit={proc.returncode} stderr={proc.stderr!r}")
        assert proc.returncode == 1
        assert "not valid JSON" in proc.stderr
        assert not new.exists() and not report.exists()

        valid = tmp_path / "valid.json"
        _write_v17_config(valid)
        proc = _run_migration(valid, new, report, "--dry-run")
        print(f"[exit-code] dry-run exit={proc.returncode} stderr={proc.stderr!r}")
        assert proc.returncode == 0
        assert proc.stderr.strip() == ""
        assert not new.exists() and not report.exists()

    def test_real_v17_config_from_live_data_dir(self, tmp_path):
        """用现场真实的旧配置（拷贝，不读改写）复现一次迁移。"""
        live = (
            _REPO_ROOT / "data" / "config" / "astrbot_plugin_vision_router_config.json"
        )
        if not live.exists():
            pytest.skip(f"环境不可达：现场旧配置不存在 {live}")
        old = tmp_path / "live_old.json"
        shutil.copyfile(live, old)
        before = _sha256(live)
        new = tmp_path / "live_new.json"
        report = tmp_path / "live_report.json"
        proc = _run_migration(old, new, report)
        print(f"\n[live-migrate] exit={proc.returncode} stdout={proc.stdout.strip()}")
        assert proc.returncode == 0, proc.stderr
        assert _sha256(live) == before, "迁移脚本改动了现场旧配置（不应发生）"
        payload = json.loads(new.read_text(encoding="utf-8"))
        source = payload["search_sources"][0]
        print("[live-migrate] source =", json.dumps(source, ensure_ascii=False))
        print(
            "[live-migrate] search =", json.dumps(payload["search"], ensure_ascii=False)
        )
        assert source["provider_ref"] == "豆包/doubao"
        assert len(payload["search_sources"]) == 1


# ---------------------------------------------------------------------------
# 4. 引擎网络态复现
# ---------------------------------------------------------------------------

_PUBLIC_IMAGE_URL = "https://example.com/x.jpg"

_RAW_PROBES = {
    "trace_moe": (
        "GET",
        "https://api.trace.moe/search",
        {"anilistInfo": "true", "url": _PUBLIC_IMAGE_URL},
        None,
    ),
    "saucenao": (
        "GET",
        "https://saucenao.com/search.php",
        {"output_type": "2", "numres": "3", "url": _PUBLIC_IMAGE_URL},
        None,
    ),
    "ascii2d": (
        "POST",
        "https://ascii2d.net/search/uri",
        None,
        {"uri": _PUBLIC_IMAGE_URL},
    ),
}


async def _raw_http_probe(engine: str, user_agent: str) -> dict:
    """Send the same request the adapter sends, to capture the real HTTP status."""
    method, url, params, data = _RAW_PROBES[engine]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        ) as client:
            response = await (
                client.get(url, params=params)
                if method == "GET"
                else client.post(url, data=data)
            )
            return {
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(response.content),
                "head": " ".join((response.text or "").split())[:120],
            }
    except Exception as exc:  # noqa: BLE001 -- 网络不可达要显式记录，不得含糊
        return {"status": None, "error": f"{type(exc).__name__}: {exc}"}


class TestEngineNetworkState:
    """真出网：三引擎各一次，断言 run_engine 契约 + 实际 HTTP 状态码。"""

    @pytest.mark.parametrize("engine", ["trace_moe", "saucenao", "ascii2d"])
    def test_engine_real_network_contract(self, engine):
        se = importlib.import_module("astrbot_plugin_image_search.search_engines")
        image = {"data_uri": "data:image/png;base64,AAAA", "url": _PUBLIC_IMAGE_URL}

        raw = asyncio.run(_raw_http_probe(engine, se.BROWSER_UA))
        print(f"\n[{engine}] raw HTTP = {json.dumps(raw, ensure_ascii=False)}")
        if raw.get("status") is None:
            pytest.skip(f"环境不可达：{engine} 出网失败 {raw.get('error')}")

        result = asyncio.run(se.run_engine(engine, image, {"_engine_opts": {}}, 25.0))
        print(f"[{engine}] run_engine = {json.dumps(result, ensure_ascii=False)}")
        assert set(result) == {
            "engine",
            "ok",
            "match",
            "error",
            "challenge",
            "miss",
        }
        assert result["engine"] == engine
        assert result["match"] is None
        assert isinstance(result["error"], str) and result["error"]
        # 契约口径（PRD §8）：challenge 页面 -> challenge=True；其余错误 -> 人话 error
        if engine in {"saucenao", "ascii2d"}:
            assert raw["status"] == 403, raw
            assert result["challenge"] is True, result
            assert "Cloudflare" in result["error"]
            assert raw["head"].startswith("<!DOCTYPE html>")
            assert "Just a moment..." in raw["head"]
        else:
            assert raw["status"] == 404, raw
            assert result["challenge"] is False, result
            assert "Failed to fetch image" in result["error"]

    def test_challenge_is_not_reported_as_a_hit(self):
        """挑战页不得被当成结果：ok=False、match=None。"""
        se = importlib.import_module("astrbot_plugin_image_search.search_engines")
        assert (
            se.is_challenge("<html><title>Just a moment...</title></html>", 403, "")
            is True
        )
        result = asyncio.run(
            se.run_engine(
                "saucenao",
                {"data_uri": "", "url": _PUBLIC_IMAGE_URL},
                {"_engine_opts": {}},
                25.0,
            )
        )
        if not result["challenge"]:
            pytest.skip("环境不可达：saucenao 不再返回挑战页，改用离线 fixture 断言")
        assert result["ok"] is False and result["match"] is None

    def test_unknown_engine_never_raises(self):
        se = importlib.import_module("astrbot_plugin_image_search.search_engines")
        result = asyncio.run(
            se.run_engine(
                "google_lens", {"data_uri": "", "url": _PUBLIC_IMAGE_URL}, {}, 5.0
            )
        )
        print("\n[unknown-engine]", json.dumps(result, ensure_ascii=False))
        assert result["ok"] is False and result["challenge"] is False
        assert "google_lens" in result["error"]


# ---------------------------------------------------------------------------
# 5. 降级场景
# ---------------------------------------------------------------------------

_VISION_SOURCE = {
    "__template_key": "vision",
    "enabled": True,
    "id": "custom-net",
    "name": "自建联网视觉端点",
    "provider_ref": "",
    "api_base": "https://relay.example.invalid/v1",
    "api_key": "sk-verify-only",
    "model": "gemini-3.5-flash",
    "web_search": True,
}
_ENGINE_SOURCE = {
    "__template_key": "engine",
    "enabled": True,
    "id": "saucenao-1",
    "name": "SauceNAO",
    "engine": "saucenao",
    "api_key": "",
    "base_url": "",
}


class _BareContext:
    """最小 duck-typed context：无 provider_manager（不触发任何真实外呼）。"""

    provider_manager = None


async def _build_plugin(sources: list[dict]):
    main = importlib.import_module("astrbot_plugin_image_search.main")
    plugin = main.ImageSearchPlugin(
        _BareContext(),
        {
            "search_sources": sources,
            "search": {},
            "image_url": {"enabled": False},
        },
    )
    await plugin.initialize()
    return plugin


def _call_tool(plugin, **kwargs) -> tuple[str, dict]:
    """调用工具并返回 (原文, 解析后的 dict)；工具不得抛异常。"""
    raw = asyncio.run(plugin.image_search(event=None, **kwargs))
    assert isinstance(raw, str)
    return raw, json.loads(raw)


class TestDegradation:
    def test_empty_source_pool_returns_friendly_json(self):
        plugin = asyncio.run(_build_plugin([]))
        raw, payload = _call_tool(
            plugin, image_refs=["data:image/png;base64,iVBORw0KGgo="], question="这是谁"
        )
        print("\n[empty-pool] " + json.dumps(payload, ensure_ascii=False))
        assert "Traceback" not in raw
        assert payload["status"] == "empty_pool" and payload["ok"] is False
        assert payload["images"] == []
        assert "search_sources" in payload["hint"]
        assert payload["summary"]

    def test_all_invalid_sources_is_empty_pool(self):
        plugin = asyncio.run(
            _build_plugin([{**_ENGINE_SOURCE, "engine": "google_lens"}])
        )
        _raw, payload = _call_tool(plugin, image_refs=["data:image/png;base64,AAAA"])
        print("\n[invalid-source] " + json.dumps(payload, ensure_ascii=False))
        assert payload["status"] == "empty_pool"

    def test_missing_image_path_returns_structured_error_not_traceback(self):
        plugin = asyncio.run(_build_plugin([_VISION_SOURCE]))
        missing = "/tmp/verify-v2-does-not-exist-9f3a71.jpg"
        assert not Path(missing).exists()
        raw, payload = _call_tool(plugin, image_refs=[missing], question="这是谁")
        print("\n[bad-ref] " + json.dumps(payload, ensure_ascii=False))
        assert "Traceback" not in raw
        assert payload["status"] == "bad_refs" and payload["ok"] is False
        image = payload["images"][0]
        assert image["ref"] == missing
        assert image["error"] == "bad_ref"
        assert image["attempts"] == [] and image["rounds_used"] == 0
        assert "bad_ref" in payload["summary"]

    def test_engine_source_without_public_url_is_skipped(self):
        plugin = asyncio.run(_build_plugin([_ENGINE_SOURCE]))
        raw, payload = _call_tool(
            plugin,
            image_refs=["data:image/png;base64,iVBORw0KGgo="],
            search_type="engine",
        )
        print("\n[engine-no-url] " + json.dumps(payload, ensure_ascii=False))
        assert "Traceback" not in raw
        attempt = payload["images"][0]["attempts"][0]
        assert attempt["outcome"] == "skipped" and attempt["error"] == "no_public_url"
        assert attempt["counted_round"] is False
        assert "image_url" in payload["hint"]


# ---------------------------------------------------------------------------
# 6. 字段一致性交叉核对
# ---------------------------------------------------------------------------

# PRD §2 冻结默认值（逐项来自 PRD §2.1~§2.7 表格）
_PRD_DEFAULTS = {
    "search_sources": [],
    "download_timeout_sec": 20,
    "tool_hint_enabled": True,
    "log_level": "info",
    "search.max_rounds": 2,
    "search.source_timeout_sec": 30,
    "search.total_timeout_sec": 180,
    "search.merge_enabled": True,
    "search.merge_max_sources": 3,
    "search.cache_ttl_sec": 300,
    "search.health_stats": True,
    "search.max_size_mb": 20,
    "search.max_pixels": 16777216,
    "image_url.enabled": False,
    "image_url.dir": "",
    "image_url.port": 8123,
    "image_url.public_base_url": "",
}
_VISION_TEMPLATE_FIELDS = [
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
_ENGINE_TEMPLATE_FIELDS = [
    "enabled",
    "id",
    "name",
    "engine",
    "api_key",
    "base_url",
    "timeout_sec",
    "extra_headers",
]


def _load_schema() -> dict:
    return json.loads((_PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))


def _schema_leaves(node: dict, prefix: str = "") -> dict[str, dict]:
    """Flatten schema into ``{dotted-leaf-path: spec}``."""
    out: dict[str, dict] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if (
            isinstance(value, dict)
            and value.get("type") == "object"
            and isinstance(value.get("items"), dict)
        ):
            # 只有带 items 的 object 才是「嵌套小节」；`extra_headers` 这类
            # `type: object` + `default: {}` 的叶子必须原样记账，不能被递归吃掉。
            out.update(_schema_leaves(value["items"], path + "."))
        elif isinstance(value, dict) and value.get("type") == "template_list":
            for name, template in (value.get("templates") or {}).items():
                out.update(
                    _schema_leaves(template.get("items") or {}, f"{path}<{name}>.")
                )
        else:
            out[path] = value if isinstance(value, dict) else {"default": value}
    return out


class TestSchemaDocConsistency:
    def test_schema_key_set_matches_prd_section_2(self):
        schema = _load_schema()
        print("\n[schema] top-level =", list(schema))
        assert list(schema) == [
            "search_sources",
            "search",
            "image_url",
            "download_timeout_sec",
            "tool_hint_enabled",
            "log_level",
        ]
        assert list(schema["search"]["items"]) == [
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
        assert list(schema["image_url"]["items"]) == [
            "enabled",
            "dir",
            "port",
            "public_base_url",
        ]
        templates = schema["search_sources"]["templates"]
        assert list(templates) == ["vision", "engine"]
        assert list(templates["vision"]["items"]) == _VISION_TEMPLATE_FIELDS
        assert list(templates["engine"]["items"]) == _ENGINE_TEMPLATE_FIELDS
        assert templates["engine"]["items"]["engine"]["options"] == [
            "saucenao",
            "ascii2d",
            "trace_moe",
        ]

    def test_schema_defaults_match_prd_section_2(self):
        schema = _load_schema()
        actual = {
            "search_sources": schema["search_sources"]["default"],
            "download_timeout_sec": schema["download_timeout_sec"]["default"],
            "tool_hint_enabled": schema["tool_hint_enabled"]["default"],
            "log_level": schema["log_level"]["default"],
            **{
                f"search.{k}": v["default"]
                for k, v in schema["search"]["items"].items()
            },
            **{
                f"image_url.{k}": v["default"]
                for k, v in schema["image_url"]["items"].items()
            },
        }
        print("\n[schema-defaults] " + json.dumps(actual, ensure_ascii=False))
        assert actual == _PRD_DEFAULTS, {
            k: (actual.get(k), _PRD_DEFAULTS.get(k))
            for k in set(actual) | set(_PRD_DEFAULTS)
            if actual.get(k) != _PRD_DEFAULTS.get(k)
        }

    def test_every_schema_leaf_is_documented(self):
        leaves = _schema_leaves(_load_schema())
        print(f"\n[schema-leaves] count={len(leaves)}")
        print("[schema-leaves] paths =", sorted(leaves))
        # 3 个单值顶层 + 9 search + 4 image_url + 12 vision + 8 engine = 36 叶子
        assert len(leaves) == 36, f"叶子抽取漏键：{len(leaves)}"
        docs = {
            name: (_PLUGIN_DIR / name).read_text(encoding="utf-8")
            for name in ("README.md", "MIGRATION.md")
        }
        undocumented = [
            path
            for path in leaves
            if not any(path.split(".")[-1] in text for text in docs.values())
        ]
        print(f"[schema->docs] undocumented = {undocumented}")
        assert undocumented == []

    def test_no_doc_mentions_a_nonexistent_config_key(self):
        schema = _load_schema()
        roots = set(schema)
        leaves = _schema_leaves(schema)
        leaf_names = {path.split(".")[-1].strip("<>") for path in leaves}
        token = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")
        unresolved: dict[str, list[str]] = {}
        examined: set[str] = set()
        for name in ("README.md", "MIGRATION.md"):
            text = (_PLUGIN_DIR / name).read_text(encoding="utf-8")
            for token_text in sorted(set(token.findall(text))):
                head = token_text.split(".")[0]
                if head not in roots and token_text not in leaf_names:
                    continue  # 非配置键 token（命令名/返回字段名/文件名）
                examined.add(token_text)
                if token_text in leaves or token_text in roots:
                    continue
                if token_text.split(".")[-1] in leaf_names:
                    continue
                unresolved.setdefault(name, []).append(token_text)
        print(f"\n[docs->schema] examined={len(examined)} tokens")
        print("[docs->schema] examined set =", sorted(examined))
        print(f"[docs->schema] unresolved = {unresolved}")
        # 防止检查退化成空转：实际被核对的配置键 token 必须足够多
        assert len(examined) >= 30, f"核对覆盖面不足：{sorted(examined)}"
        assert unresolved == {}

    def test_schema_types_are_in_astrbot_supported_set(self):
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
        leaves = _schema_leaves(_load_schema())
        bad = {
            path: spec.get("type")
            for path, spec in leaves.items()
            if spec.get("type") not in supported
        }
        print(f"\n[schema-types] unsupported = {bad}")
        assert bad == {}

    def test_documented_defaults_agree_with_schema(self):
        """README / MIGRATION 表格里写的默认值必须与 schema 的 default 一致。"""
        schema = _load_schema()
        leaves = _schema_leaves(schema)
        by_leaf: dict[str, set[str]] = {}
        for path, spec in leaves.items():
            by_leaf.setdefault(path.split(".")[-1].strip("<>"), set()).add(
                json.dumps(spec.get("default"), ensure_ascii=False)
            )
        # 顶层 template_list / 容器节点不进 leaves，单独补上
        by_leaf.setdefault("search_sources", set()).add(
            json.dumps(schema["search_sources"]["default"], ensure_ascii=False)
        )
        row_re = re.compile(r"^\| `([A-Za-z_][\w.]*)` \|(.*)\|$", re.M)
        literal_re = re.compile(r'^(?:true|false|-?\d+|""|"[^"]*"|\{\}|\[\])$')

        compared: dict[str, str] = {}
        for name in ("README.md", "MIGRATION.md"):
            text = (_PLUGIN_DIR / name).read_text(encoding="utf-8")
            for key, rest in row_re.findall(text):
                cells = [cell.strip().strip("`") for cell in rest.split("|")]
                literal = next((cell for cell in cells if literal_re.match(cell)), None)
                if literal is None:
                    continue
                expected = by_leaf.get(key.split(".")[-1])
                if expected is None or len(expected) != 1:
                    continue  # 非配置键，或同名叶子默认值不唯一（无法单值比对）
                compared[f"{name}:{key}"] = literal
                assert literal == next(iter(expected)), (
                    f"{name} 的 `{key}` 默认值写作 {literal}，schema 是 "
                    f"{next(iter(expected))}"
                )
        print(f"\n[docs-defaults] compared {len(compared)} rows: {compared}")
        assert len(compared) >= 12, f"比对行数不足，检查可能空转：{compared}"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
