# 搜图路由 Agent（astrbot_plugin_image_search）v2.1.0

> 一个**纯搜图**插件。主模型遇到「图中角色/场景是谁、出自哪里、什么梗」时，把图片和
> 一句检索线索交给它；它按你配置的**搜图源池**联网检索，把结构化结果回传给主模型，
> 由主模型组织回复。插件自己在调用过程中**不发送任何消息**。

- 唯一工具：`image_search(image_refs, question, intent, search_type)`
- 两类源：**vision**（视觉联网模型）与 **engine**（SauceNAO / ascii2d / trace.moe）
- 逐源失败自动换源、单源超时隔离、多源命中合并去重
- 返回 JSON 字符串；**不抛异常给主模型**、**不含 API Key**

---

## 1. 它做什么，不做什么

| | 说明 |
|---|---|
| **做什么** | 联网查「图中主体是谁 / 出自哪里」。vision 源把原图交给带联网检索能力的多模态模型；engine 源拿图片公网 URL 去图搜图站点反查。 |
| **不做什么** | **不做识图**。看图、转述画面、提取图中文字/表格、描述图表——这些由主模型自己的多模态能力完成，插件不参与，也没有任何「转述图片内容」的入口。 |
| **为什么** | 对话模型看图的短板不是「看见」，而是「训练集里没有的新角色/冷门作品认不出来」。联网搜图补的是这个缺口。 |

`image_search` 是本插件**唯一**注册给主模型的 LLM 工具。

---

## 2. 特性

- **`intent` 检索线索**：主模型可以把「看起来像《明日方舟》的阿米娅」「场景疑似在杭州」
  这类判断交给搜图模型，用来缩小检索范围。线索只进入 vision 提示词与缓存键，
  并明确标注「可能不准确，仅供参考」，不会替代图片本身与联网核实。
- **源池有序**：`auto` 先跑全部 engine 源、再跑 vision 源；源失败只做顺序切换，
  不按「健康度」偷偷重排。
- **轮数只由明确未命中消耗**：报错、超时、Cloudflare 挑战页、因图床未开被跳过，
  一律不计轮、不停止。
- **单源超时隔离**：每个源独立 `wait_for`，一个源挂死不会拖垮其他源或其他图。
- **多源合并去重**：命中后按配置继续尝试后续源，`merged_from` 逐条标注归属。
- **缓存**：键含图片字节哈希 + `search_type` + `question` + `intent`；TTL 之外还有
  256 条 LRU 上限，避免长时间运行内存无界增长。
- **图片安全流水线**：引用归一化 → SSRF 防护（内网/环回/链路本地/保留段拒绝，
  重定向后二次校验）→ 体积/像素压缩 → data URI 或内置只读图床 URL。
- **只读图床按需启动**：只有候选里真的存在 engine 源时才发布图片，纯 vision 配置
  不会往图床目录落无用文件。

---

## 3. 安装

要求 AstrBot `>=4.16,<5`。

**方式 A：从 GitHub 安装（推荐）**

在 AstrBot WebUI 的插件管理里选择「从 GitHub / 链接安装」，填入：

```
https://github.com/cmdst/astrbot_plugin_image_search
```

**方式 B：手动放入插件目录**

```bash
cd /root/AstrBot/data/plugins
git clone https://github.com/cmdst/astrbot_plugin_image_search.git
```

然后在 WebUI 重载插件（或重启 AstrBot）。插件没有额外的 Python 依赖，
使用 AstrBot 运行时自带的 `httpx` 等库。

**最小可用配置**（vision 源，直接复用 AstrBot 里已配好的联网视觉提供商）：

```json
{
  "search_sources": [
    {
      "__template_key": "vision",
      "enabled": true,
      "id": "doubao-search",
      "name": "豆包联网搜索",
      "provider_ref": "doubao/doubao",
      "web_search": true,
      "timeout_sec": 90
    }
  ]
}
```

`provider_ref` 建议在 WebUI 下拉里选，不要手抄。`web_search=true` 只是要求模型联网，
不能给一个没有联网能力的模型凭空加上联网。

---

## 4. 工具契约

### 4.1 签名

```python
image_search(
    image_refs: list[str],
    question: str = "",
    intent: str = "",
    search_type: str = "auto",
) -> str
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `image_refs` | 是 | — | 图片引用列表，最多 8 张（超出截断）。接受本地绝对路径、`file://`、`http(s)` URL、`data:` URI、`base64://`、`[Image Attachment: path ...]`。 |
| `question` | 否 | `""` | 用户原话（「这是谁」「出自哪部番」）。超过 500 字截断。 |
| `intent` | 否 | `""` | 主模型给出的检索线索（角色 / 作品 / 场景等）。超过 300 字截断，记 `intent_truncated`。只影响 vision 源的提示词与缓存键。 |
| `search_type` | 否 | `"auto"` | `auto` = 先全部 engine、再全部 vision；`engine` = 只用图搜图引擎；`vision` = 只用视觉联网模型。非法值按 `auto` 执行并记 `invalid_search_type:<原值>`。 |

### 4.2 返回

返回 **JSON 字符串**（不是 dict）。顶层字段：

`tool`、`ok`、`status`、`search_type`、`question`、`intent`、`images`、`summary`、
`hint`、`elapsed_ms`、`cached`、`notes`。

`status`：`ok` / `partial` / `failed` / `empty_pool` / `no_refs` / `bad_refs`。
`ok` 为真当且仅当 `status == "ok"`。

`images[]`：`index` / `ref` / `ok` / `transport` / `source` / `match` / `answer` /
`merged_from` / `attempts` / `rounds_used` / `cached` / `error`。

`attempts[].outcome`：`hit` / `miss` / `error` / `timeout` / `challenge` / `skipped`。
只有 `miss` 的 `counted_round` 为 `true`。

两个硬保证：**工具不把异常抛给主模型**（内部异常转成 `status="failed"` 的结构化返回），
**返回体不含 API Key**（需要提及时只显示前 4 位 + `***`）。

---

## 5. 配置

配置通过 WebUI 插件配置页呈现，schema 见 [`_conf_schema.json`](_conf_schema.json)。

### 5.1 顶层键

| 键 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `search_sources` | template_list | `[]` | 搜图源池，双模板 `vision` / `engine` |
| `search` | object | — | 检索通用参数 |
| `image_url` | object | — | 内置只读图床，**只为 engine 源**提供公网 URL |
| `download_timeout_sec` | int | `20` | 下载 `http(s)` 图片的超时 |
| `tool_hint_enabled` | bool | `true` | 是否注入图片路径与工具提示 |
| `log_level` | string | `"info"` | `debug` / `info` / `warning` / `error` |

### 5.2 vision 模板

字段：`enabled` / `id` / `name` / `provider_ref` / `api_base` / `api_key` / `model` /
`web_search` / `timeout_sec` / `max_size_mb` / `max_pixels` / `extra_headers`。

两种形态二选一：

- **提供商引用**：填 `provider_ref`（WebUI 下拉选 AstrBot 里已配好的提供商，可指向
  自建反代）。`api_base` / `api_key` / `model` 不需要填；`provider_ref` 非空时
  手填的 `model` 会被忽略，并在启动 WARNING 与返回体 notes 里可见。
- **自定义端点**：`api_base` 与 `model` 必须同时非空（`api_key` 可留空）。
  只填一半 → 该源判为非法，运行期跳过。

`provider_ref` 形态下模型名留空即可，自动用提供商的默认模型。

### 5.3 engine 模板

字段：`enabled` / `id` / `name` / `engine`（`saucenao` / `ascii2d` / `trace_moe`，
必填）/ `api_key`（仅 SauceNAO）/ `base_url` / `timeout_sec` / `extra_headers`。

⚠️ engine 源只吃**图片公网 URL**：必须打开 `image_url.enabled`，否则所有 engine 源
会被逐条跳过（`attempts` 记 `skipped` / `no_public_url`）。纯 `engine` 请求在
**没有任何实际尝试**时归 `status="empty_pool"`（不是 `failed`），`hint` 会提示你打开图床。

### 5.4 `search` 小节

| 键 | 默认 | 说明 |
|---|---|---|
| `max_rounds` | `2` | 每图轮数上限（0~10）。只有明确 `miss` 计轮 |
| `source_timeout_sec` | `30` | 单源默认超时（5~300），被源自身 `timeout_sec` 覆盖 |
| `total_timeout_sec` | `180` | 单次调用总预算（30~900）；用完后停止剩余源，已有结果照常返回 |
| `merge_enabled` | `true` | 命中后是否继续试后续源做多源合并 |
| `merge_max_sources` | `3` | 每图最多合并源数（1~5，含首个命中源） |
| `cache_ttl_sec` | `300` | 结果缓存 TTL（0~3600，0=关闭） |
| `health_stats` | `true` | 卸载时输出一行各源健康统计（不影响源顺序） |
| `max_size_mb` | `20` | 图片体积上限（1~100），超限先压缩 |
| `max_pixels` | `16777216` | 图片像素上限（默认约 4096×4096） |

### 5.5 `image_url` 小节

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 打开后插件加载时启动只读图床；关闭时 engine 源全部不可用 |
| `dir` | `""` | 落盘目录；留空 → `{AstrBot data}/plugin_data/astrbot_plugin_image_search/imgserv` |
| `port` | `8123` | 监听端口；被占用自动 +1 重试，实际端口看启动日志 |
| `public_base_url` | `""` | 对外访问前缀；留空 → `http://<自动探测公网 IP>:<实际端口>` |

⚠️ 云主机上自动探测常拿到**内网 IP**，图搜图站点拉不到图。生产使用请**显式填**
`public_base_url`（域名反代或公网 `IP:端口`），并确保端口在安全组/防火墙放行。

### 5.6 配置示例

**A. 豆包联网搜索（provider 引用）**

```json
{
  "search_sources": [
    {
      "__template_key": "vision",
      "enabled": true,
      "id": "doubao_search",
      "name": "豆包联网搜索",
      "provider_ref": "doubao/doubao",
      "web_search": true,
      "timeout_sec": 90
    }
  ]
}
```

**B. 自建 OpenAI 兼容端点**

```json
{
  "search_sources": [
    {
      "__template_key": "vision",
      "enabled": true,
      "id": "my_gateway",
      "name": "自建网关",
      "api_base": "https://your-gateway.example.com/v1",
      "api_key": "sk-xxxxxxxx",
      "model": "gemini-3.7-flash",
      "web_search": true,
      "extra_headers": {"X-Auth": "your-token"},
      "timeout_sec": 120
    }
  ]
}
```

**C. 三个图搜图引擎**

```json
{
  "image_url": {
    "enabled": true,
    "dir": "",
    "port": 8123,
    "public_base_url": "https://img.example.com"
  },
  "search_sources": [
    {"__template_key": "engine", "enabled": true, "id": "saucenao-1", "name": "SauceNAO", "engine": "saucenao", "api_key": "your-saucenao-key", "timeout_sec": 45},
    {"__template_key": "engine", "enabled": true, "id": "ascii2d-1", "name": "ascii2d", "engine": "ascii2d", "timeout_sec": 45},
    {"__template_key": "engine", "enabled": true, "id": "tracemoe-1", "name": "trace.moe", "engine": "trace_moe", "timeout_sec": 30}
  ]
}
```

顺序即尝试顺序。想省钱只留 engine；想稳就用 engine 打头 + 一个 vision 兜底。

> **面板辅助键 `__template_key`**：每个条目都必须带（vision 源为 `"vision"`、
> engine 源为 `"engine"`）。WebUI 新增条目会自动带上；手工编辑 JSON 时要自己写，
> 否则面板会报「缺少模板选择，需要 __template_key」并拒绝保存。

---

## 6. 工作方式

单次调用：

1. 归一化 `image_refs`（去重、≤8 张）、`question`（≤500 字）、`intent`（≤300 字）。
2. 按 `search_type` 选候选源；`auto` 先 engine 后 vision。
3. 每张图独立执行：读取图片 → SSRF 校验 → 体积/像素压缩 → 生成 data URI
   （有 engine 候选时才发布图床 URL）→ 查缓存。
4. 按顺序尝试源；命中后视 `merge_enabled` 继续合并；明确 `miss` 计轮。
5. 汇总为 `images[]` + 人话 `summary` + `hint` + `notes`，以 JSON 字符串返回。

**缓存键**：`sha256(图片字节) + "|" + JSON([search_type, question, intent])`。
`intent` 不同不会命中同一份缓存；缓存 TTL 内最多保留 256 条，超出淘汰最旧条目。

**失败与降级**：

| 场景 | 行为 |
|---|---|
| 单源报错 / 超时 | 切下一个源，`attempts` 记 `error` / `timeout`，不计轮 |
| Cloudflare 挑战页 | 切下一个源，记 `challenge`，不解析挑战页 |
| 明确未命中 | 计 1 轮（受 `max_rounds` 约束） |
| 源配置非法 | 记 `skipped` / `invalid_source`，其余源照常 |
| 图床未开 / 发布失败 | engine 源记 `skipped` / `no_public_url`；`auto` 下 vision 照常 |
| 没有任何实际尝试 | `status="empty_pool"`，hint 说明是缺源、缺图床还是超尺寸 |
| 图片读不出来 | 该图失败，其他图照常；全部不可读 → `bad_refs` |
| 总预算耗尽 | 停止剩余源，返回已有结果，notes 追加 `total_timeout` |
| 内部未预期异常 | 转成 `status="failed"`，堆栈只进日志 |

---

## 7. 引擎与限制

| 引擎 | 适合查什么 | 限制 |
|---|---|---|
| **SauceNAO** | 动漫 / 插画 / 同人图 / pixiv 与推特出处，返回相似度 + 作品名 + 作者 + 链接 | 免费额度限速严，常被 Cloudflare 拦；建议配 `api_key` 或自建反代 |
| **ascii2d** | 日系插画 / 画师推特出处（色合 + 特征双通道） | 同样受 Cloudflare 拦截；没有相似度数值 |
| **trace.moe** | 动画截图：作品名 + 集数 + 时间点 + AniList 链接 | 只认动画帧；插画/真人/ CG 会返回 error（不是 miss） |

三者都只接受公网图片 URL（不接 base64），所以必须开 `image_url.enabled`。
被 Cloudflare 拦时插件返回 `outcome="challenge"`，不会从挑战页里抠标题当结果。

---

## 8. 版本与升级

- **2.1.0**：新增 `intent` 参数；缓存键纳入 `intent` 并加 256 条 LRU 上限；
  移除 v1.7 配置迁移脚本 / `MIGRATION.md` / v1.7 遗留的 `desc` 字段；README/PRD 重写。
- **2.0.0**：破坏性重构，源池改为 vision + engine，唯一工具改为 `image_search`。
- **v1.7 → v2.0 的迁移脚本只存在于 v2.0.0 的历史里**（本仓库 `23dacda` 提交）。
  需要时可在旧提交中取出 `migrate_v1_7.py`；v2.1.0 本身不再提供迁移入口。
- **2.0.0 → 2.1.0 配置兼容**：配置键没有变化，直接替换插件目录即可；
  已有的 v2.0.0 配置可以原样使用。

---

## 9. 开发与测试

```bash
cd /root/workspace/projects/astrbot_plugin_image_search
PYTHONDONTWRITEBYTECODE=1 \
PYTEST_ADDOPTS='-p no:cacheprovider' \
PYTHONPATH=/root/AstrBot \
/root/AstrBot/.venv/bin/python -m pytest tests/ -q
```

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口：注册 `image_search`、`on_llm_request` 提示注入、逐图/逐源调度 |
| `search_core.py` | 配置归一化、源池、缓存、结果装配、摘要/提示文案 |
| `search_engines.py` | SauceNAO / ascii2d / trace.moe 的 HTTP 与解析适配 |
| `provider_reader.py` | 从 AstrBot 提供商体系读取端点与密钥，统一 vision 调用 |
| `image_io.py` | 图片读取、魔数嗅探、压缩、图床文件发布 |
| `image_server.py` | 内置只读 HTTP 图床 |
| `prompts.py` | vision 系统/用户提示词（含 `intent` 段落） |
| `_conf_schema.json` | 配置 schema（WebUI 表单来源） |
| `PRD.md` | 当前版本的接口与行为契约 |
| `tests/` | 离线单测（网络态用例不可达时显式 skip） |

---

## 10. 已知限制

1. **不做纯文本关键词搜索**（Tavily / Bing 文本搜索等）——只有图搜图 + 视觉联网模型。
2. **不做 Google Lens / TinEye 适配**。
3. **不做识图 / 转述 / OCR**——这些交主模型自身多模态能力。
4. 反代上游是否真的联网取决于反代本身，`web_search=true` 只能提示、不能强制。
5. 图床需要公网可达；云主机请显式配置 `public_base_url`。

---

## 11. License

MIT © cmdst。见 [LICENSE](LICENSE)。
