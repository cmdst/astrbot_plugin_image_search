# 搜图路由 Agent（astrbot_plugin_image_search）v2.0.0

> 一个**纯搜图**插件。主模型遇到「图中角色/场景是谁、出自哪里、什么梗」时把图交给它，
> 它按你配置的**搜图源池**联网检索，把结构化结果回传给主模型去组织回复。

---

## 1. 定位：它做什么，不做什么

| | 说明 |
|---|---|
| **做什么** | 联网查「图中主体是谁 / 出自哪里」。源池两类：**A 类视觉联网模型**（借模型自己的联网检索能力）与 **B 类图搜图引擎**（SauceNAO / ascii2d / trace.moe 反查）。逐源失败自动换下一个源，多源命中合并去重。 |
| **不做什么** | **不做识图**。看图、转述画面、提取图中文字 / 表格、描述图表——这些一律由主模型自己的多模态能力完成，插件不参与，也不存在任何「转述图片内容」的入口。 |
| **为什么** | 对话模型看图的短板不是「看见」，而是「训练集里没有的新角色 / 冷门作品认不出来」。联网搜图正好补这个缺口，而识图本身它已经会了。 |

`image_search` 是本插件**唯一**注册给主模型的 LLM 工具。

插件自己在工具调用过程中**不发送任何消息**（不 `yield`、不 `event.send`）：结果只通过工具返回的 JSON 字符串回到主模型手里，回复由主模型组织。

### 相对 v1.7 的变化（破坏性重构）

- **删掉整个 analyze 识图池**：`ais` 多 AI 池、关键词预路由、`mode` 双模式、质量不满意换 AI（`max_rounds` 换 AI 语义）、路由 LLM 兜底直判——全部移除，配置里也不再有任何对应开关。
- 工具名从 `image_analyze` 改为 **`image_search`**，参数语义与返回结构全部重写。
- 源池从「识图 AI 池」改为「搜图源池」，并新增 B 类图搜图引擎。
- 配置迁移脚本见 [MIGRATION.md](MIGRATION.md)，字段映射表与「ais 池已彻底移除」声明都在那里。

---

## 2. 工具契约

```
image_search(image_refs: list[string], question: string = "", search_type: string = "auto") -> str
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `image_refs` | 是 | — | 待搜图片引用列表，单图也可；最多 8 张（超出截断）。接受：本地绝对路径、`file://`、`http(s)` URL、`data:` URI、`base64://`、以及 `[Image Attachment: path /root/.../x.jpg]` 占位文本。 |
| `question` | 否 | `""` | 用户原话（「这是谁」「出自哪部番」），会拼进 vision 源提示词；engine 源不使用。超过 500 字截断。 |
| `search_type` | 否 | `"auto"` | `auto` = 先全部 engine 源、再全部 vision 源；`engine` = 只用图搜图引擎；`vision` = 只用视觉联网模型。非法值不报错，按 `auto` 执行并记 `notes`。 |

**返回值**：JSON 字符串（必须能当文本读，所以是 `str` 不是 dict）。顶层字段：

`tool`（恒为 `"image_search"`）、`ok`、`status`、`search_type`（实际生效值）、`question`、
`images`（每张图一个对象）、`summary`（≤800 字人话摘要，主模型可直接改写）、
`hint`（配置/网络排障引导，无问题时为 `""`）、`elapsed_ms`、`cached`、`notes`。

`status` 取值：`ok`（全部命中）/ `partial`（部分命中）/ `failed`（全部未命中）/
`empty_pool`（没有可用源）/ `no_refs`（没传图）/ `bad_refs`（所有引用都读不出来）。

`images[]` 每个元素：`index` / `ref` / `ok` / `transport`（`data_uri` 或 `url`）/ `source` /
`match`（引擎的结构化命中：`similarity` / `title` / `author` / `links` / `extra`）/
`answer`（vision 源回答正文）/ `merged_from`（合并来源逐条归属）/ `attempts`（**逐源尝试明细，
含被跳过的源与原因**）/ `rounds_used` / `cached` / `error`。

`attempts[].outcome` 取值：`hit` / `miss` / `error` / `timeout` / `challenge` / `skipped`。

两个硬保证：工具**不抛异常给主模型**（内部异常都转成 `status="failed"` 的结构化返回），返回体**不含 API Key**。

---

## 3. 配置

配置通过 WebUI 插件配置页呈现（schema 见 [_conf_schema.json](_conf_schema.json)）。顶层键：

| 键 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `search_sources` | template_list | `[]` | 搜图源池，双模板 `vision` / `engine` |
| `search` | object | — | 检索通用参数（轮数、超时、合并、缓存、体积像素上限） |
| `image_url` | object | — | 内置只读图床，**只为 engine 源**提供公网 URL |
| `download_timeout_sec` | int | `20` | 下载 `http(s)` 引用图片的超时 |
| `tool_hint_enabled` | bool | `true` | 是否注入图片路径与 `image_search` 工具提示 |
| `log_level` | string | `"info"` | `debug` / `info` / `warning` / `error` |

### 3.1 源池怎么排

`search_sources` 是**有序数组**，顺序就是尝试顺序；插件**不会**按「健康度」偷偷重排。
`auto` 策略下先跑完全部 `engine` 源，再跑 `vision` 源：

- 图搜图引擎对二次元角色 / 插画 / 动画截图的命中率高、返回可核对链接、且免费；
- 视觉联网模型负责补引擎查不到的实景、真人、冷门或非二次元主体。

**想省钱**：只留 engine 源。**想稳**：engine 打头 + 一个 vision 兜底。
`search.merge_enabled=true`（默认）时首个命中后还会继续试后续源把结果合并，想更快更省就设 `false`（首个命中即停）。

### 3.2 vision 模板字段

`enabled` / `id` / `name` / `provider_ref` / `api_base` / `api_key` / `model` / `web_search` /
`timeout_sec` / `max_size_mb` / `max_pixels` / `extra_headers`。

两种形态二选一：

- **提供商引用**：填 `provider_ref`（下拉选 AstrBot 里已配好的提供商 ID，可以指向自建反代提供商）。
  `api_base` / `api_key` / `model` **不需要填**，填了也会被忽略（`provider_ref` 非空时优先走引用形态）。
- **自定义端点**：`api_base` 与 `model` 必须同时非空（`api_key` 可留空，本机反代常见）。
  只填一半 → 该源判为非法，运行期跳过并在 `attempts` 记 `skipped` / `invalid_source`。

> `provider_ref` 形态下模型名留空即可，自动用该提供商的默认模型；填了也会被忽略（以 PRD §4.7 为准），被忽略的源会在启动 WARNING 与返回体 notes `provider_model_ignored:<id>` 里可见。手工填错 model 还容易触发 `400 Unknown model`。

> **面板辅助键 `__template_key`**：`search_sources` 的每个条目都必须带 `__template_key`（vision 源为 `"vision"`、engine 源为 `"engine"`）。它不在上面的 schema 字段表里，但 AstrBot 面板的 `template_list` 校验会检查它——缺失时报「缺少模板选择，需要 __template_key」并拒绝保存。在面板里新增条目会自动带上，手工编辑 JSON 时要自己写。

### 3.3 engine 模板字段

`enabled` / `id` / `name` / `engine`（必填，枚举 `saucenao` / `ascii2d` / `trace_moe`）/
`api_key`（**仅 SauceNAO** 使用）/ `base_url`（自建反代覆盖端点）/ `timeout_sec` / `extra_headers`。

⚠️ engine 源只吃**图片公网 URL**：必须打开 `image_url.enabled`，否则所有 engine 源会被逐条跳过
（`attempts` 记 `skipped` / `no_public_url`）；纯 `engine` 请求在**没有任何实际尝试**的情况下归 `status="empty_pool"`（不是 `failed`），`hint` 会提示你打开 `image_url.enabled`。

### 3.4 `search` 小节

| 键 | 默认 | 说明 |
|---|---|---|
| `max_rounds` | `2` | 每图轮数上限（0~10）。**只有明确未命中（miss）计轮**；报错/超时/挑战页/被跳过都不计轮、不停止 |
| `source_timeout_sec` | `30` | 单源默认超时（5~300），被源自身 `timeout_sec` 覆盖 |
| `total_timeout_sec` | `180` | 单次调用总预算（30~900）。用完后停止尝试剩余源，**已有结果照常返回** |
| `merge_enabled` | `true` | 命中后是否继续试后续源做多源合并 |
| `merge_max_sources` | `3` | 每图最多合并源数（1~5，含首个命中源） |
| `cache_ttl_sec` | `300` | 结果缓存 TTL（0~3600，0=关闭）。缓存键 = 图片字节 sha256 + search_type + question |
| `health_stats` | `true` | 累计各源成功/耗时统计，卸载时输出一行摘要（**不影响**源顺序） |
| `max_size_mb` | `20` | 图片体积上限（1~100），超限先压缩；压完仍超 → 该图失败 |
| `max_pixels` | `16777216` | 图片像素上限（默认约 4096×4096），超限等比缩放 |

### 3.5 `image_url` 小节（内置只读图床）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 打开后插件加载时启动图床；**关闭时 engine 源全部不可用** |
| `dir` | `""` | 图片落盘目录；留空 → `{AstrBot data}/plugin_data/astrbot_plugin_image_search/imgserv` |
| `port` | `8123` | 监听端口；被占用自动 +1 重试（最多 10 次），实际端口看启动日志 |
| `public_base_url` | `""` | 对外访问前缀；留空 → `http://<自动探测公网 IP>:<实际端口>` |

⚠️ 云主机上自动探测常拿到**内网 IP**，图搜图站点拉不到图。生产使用请**显式填** `public_base_url`
（域名反代或公网 `IP:端口`），并确保端口在安全组/防火墙放行。

服务本身是只读的：仅 `GET` / `HEAD`，文件名白名单（sha256 前 32 位 + 图片扩展名），
URL 解码后校验仍在 `dir` 内（防穿越），无目录列表。

---

## 4. 配置示例

### 4.1 vision 源示例 A：Gemini 反代（provider 引用）

先在 AstrBot 里把反代配成一个提供商（例如 `gemini-web2api` 转发的 Gemini），再引用它：

```json
{
  "search_sources": [
    {
      "__template_key": "vision",
      "enabled": true,
      "id": "gemini_proxy",
      "name": "Gemini 反代（联网）",
      "provider_ref": "gemini-web2api/gemini-3.7-flash",
      "web_search": true,
      "timeout_sec": 90
    }
  ]
}
```

要点：`provider_ref` 填的是 AstrBot 里的提供商 ID；`api_base` / `api_key` / `model` 留空。
反代上游是否真联网取决于反代本身，插件只能按提示词要求它联网、无法强制。
联网检索比普通对话慢，`timeout_sec` 可给到 90~120。

### 4.2 vision 源示例 B：豆包联网搜索（provider 引用）

豆包图片搜索 API 自带联网能力（v1.7 的 `mode="search"` 走的就是它），因此 `web_search` 可以留默认：

```json
{
  "search_sources": [
    {
      "__template_key": "vision",
      "enabled": true,
      "id": "doubao_search",
      "name": "豆包联网搜索",
      "provider_ref": "豆包/doubao",
      "web_search": true,
      "timeout_sec": 90
    }
  ]
}
```

国内知识（国漫、国内梗、中文互联网出处）它明显强于海外模型。

### 4.3 vision 源示例 C：自定义 OpenAI 兼容端点

自建/第三方网关，不经过 AstrBot 提供商体系时：

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

`api_base` 带不带 `/chat/completions` 都行（不带时自动补）。`extra_headers` 用于网关鉴权。
⚠️ 日志与工具返回**不会**输出明文 key。

### 4.4 engine 源示例：三个引擎一起用

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

顺序即尝试顺序：SauceNAO 综合命中率最高放第一，ascii2d 补画师/推特出处，trace.moe 收动画截图。

---

## 5. 三个图搜图引擎：各自适用场景与限制

| 引擎 | 适合查什么 | 限制 |
|---|---|---|
| **`saucenao`**（SauceNAO） | 综合动漫 / 插画 / 同人图 / pixiv 与推特出处，淡出度最高，返回**相似度百分比** + 作品名 + 作者 + 可核对链接 | 免费额度**限速很严**，且常被 **Cloudflare 拦**（本环境实测：`saucenao.com` 返回 `403` + `Just a moment...` 人机校验页）。需配 `api_key` 显著降低被拦概率，或自建反代填 `base_url`。相似度 <60 时结果仅供参考 |
| **`ascii2d`** | 日系插画 / 画师推特出处，**色合检索 + 特征检索**双通道，对小众画师图有时比 SauceNAO 更灵 | 同样受 **Cloudflare 拦截**（本环境实测 `ascii2d.net` 返回 `403` + `Just a moment...`）；**没有相似度数值**（`similarity` 恒为 `null`），只有标题/作者/链接与检索方式标签 |
| **`trace_moe`**（trace.moe） | **动画截图专用**：返回作品名 + 第几集 + 时间点，并附 AniList 链接与视频片段 | **只认动画帧**：插画、真人照片、CG 会返回 `{"error": ...}`（插件判为源失败并换源，**不是**未命中）；没有相似度以外的语义，`author` 恒为空 |

共同限制：

- 三者都**只接受图片公网 URL**（不接 base64），所以必须开 `image_url.enabled`；
- 被 Cloudflare 拦时插件返回 `outcome="challenge"`、`challenge=true`，**不会**从挑战页里抠标题当结果，而是切下一个源；
- 全部源都 challenge 时 `hint` 会提示「配置自建反代 base_url 或提供 api_key」。

---

## 6. 失败与降级行为

| 场景 | 你会看到什么 |
|---|---|
| 单源报错 / 超时 | 切下一个源，`attempts` 记 `error` / `timeout`；**不计轮数**，不影响其他源与其他图 |
| 被 Cloudflare 拦（challenge） | 切下一个源，`attempts` 记 `challenge`；不解析挑战页、不产生结果 |
| 源配置非法（如 vision 只填了 `api_base`） | 该源 `skipped` / `invalid_source`，其余源照常跑 |
| 明确未命中（miss） | 计 1 轮（受 `max_rounds` 约束），`status="failed"` + `summary` 说明「没有查到这张图的来源」，**区别于「源全失败」** |
| 全部源失败（有实际尝试） | `ok=false`、`status="failed"`，`summary` 给出最先失败的原因，`hint` 给排障建议；此前被跳过的源逐条记进 `notes`（`skipped:<id>:<error>`） |
| 无源可试（没有任何实际尝试） | `status="empty_pool"`，**不发起任何请求**；`summary` 恒为同一句「当前没有可用的搜图源。」，具体原因只说在 `hint` 里：候选为空 → 提示去配 `search_sources`；仅因图床未开而全被跳过 → 提示开 `image_url.enabled`；仅因超出某源的体积/像素上限而全被跳过 → 提示调大该源的 `max_size_mb` / `max_pixels`（或全局值） |
| 图床没开 / 发布失败 | engine 源全部 `skipped` / `no_public_url`；`auto` 下 vision 源照常命中；纯 `engine` 请求 → `status="empty_pool"`（无实际尝试）并提示开 `image_url.enabled` |
| 图片读不出来 | 该图 `ok=false`、`error` ∈ `bad_ref` / `download_failed` / `image_too_large` / `ssrf_blocked`，其他图照常 |
| 总预算耗尽 | 停止剩余源、返回已有结果，`notes` 追加 `total_timeout` |
| 单图失败 | 不影响其他图，最终 `status="partial"` |
| 内部未预期异常 | 捕获后返回 `status="failed"`，日志里留完整 traceback（只进日志、不进返回体） |

**单源超时是真隔离**：每源独立 `wait_for`，一个源挂死不会拖垮其他源，也不会拖垮其他图。

---

## 7. 从 v1.7 迁移

```bash
# 0) 备份旧配置
cp data/config/astrbot_plugin_vision_router_config.json /tmp/vision_router_config.bak.json

# 1) 先干跑看 diff（不写任何文件）
python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py --dry-run

# 2) 正式迁移
python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py
```

脚本会把旧 `route.llm_provider` 变成一条 vision 源，保留 `image_url` / `download_timeout_sec` /
`tool_hint_enabled` / `log_level`，并把旧 `ais` 识图池整节丢弃、快照写进迁移报告。
重复执行不会产生重复源条目，也不会覆盖你在 WebUI 里改过的值。

完整步骤、字段映射表与「ais 池已彻底移除」声明见 **[MIGRATION.md](MIGRATION.md)**。

---

## 8. 本轮明确不做的项

1. **关键词纯文本搜索**（Tavily / 博查 / Bing 文本搜索之类）——不做，只有图搜图 + 视觉联网模型两条路。
2. **Google Lens / TinEye 适配**——不做。
3. **analyze 识图能力**——不做；看图、转述、OCR、图表提取一律交主模型自身多模态能力。
4. **WebUI 前端改动**——不做；配置只通过 `_conf_schema.json` 呈现。
5. **旧插件目录的删除/归档**——不做，`data/plugins/astrbot_plugin_vision_router/` 保持零改动。

---

## 9. 文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口：注册 `image_search` 工具、`on_llm_request` 提示注入、配置规范化 |
| `search_core.py` | 搜图流水线：引用归一化 / SSRF / 压缩 / 候选顺序 / 换源 / 合并 / 摘要 |
| `search_engines.py` | 三个图搜图引擎的 HTTP 与解析适配（`run_engine` 统一签名） |
| `provider_reader.py` | 从 AstrBot 提供商体系读取 `api_base` / `api_key` / `model` |
| `image_io.py` | 图片读取、魔数嗅探与压缩 |
| `prompts.py` | vision 源提示词与 `VERDICT: HIT/MISS` 判定标记词 |
| `migrate_v1_7.py` | v1.7 → v2.0 配置迁移脚本（独立可执行） |
| `_conf_schema.json` | 配置 schema（WebUI 表单来源） |
| `PRD.md` | 需求基准与接口契约（冻结） |
| `MIGRATION.md` | 迁移步骤与字段映射表 |
