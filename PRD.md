# 搜图路由 Agent v2.0.0 — 需求基准与接口契约（PRD）

- 文档状态：**冻结（frozen）**，本文件是 t2~t7（实现 / 引擎 / 迁移 / 验证 / 评审 / 收口）的**唯一基准**。
- 基准范围：插件目录 `data/plugins/astrbot_plugin_image_search/`（v2.0.0 全新插件）。
- 读者：core-smith（核心流水线）、engine-smith（引擎适配器）、migrate-docsmith（迁移与文档）、verifier（独立验证）、reviewer（对抗式评审）。
- **本文件不含实现代码**；本文件给出的键名、类型、默认值、返回形状即为可直接编码的契约。文中每条规则都是可判定的硬约定，**不存在措辞含糊的留白条款**；若实现与本文冲突，以本文为准，改动必须先改本文。

---

## 0. 硬边界（用户裁决，不得自行放宽）

| # | 边界 | 落地含义（可核对） |
|---|------|-------------------|
| ① | **整插件收敛为纯搜图插件，彻底删除 analyze 识图池** | 不得保留 `ais` 多 AI 池、关键词预路由、质量不满意换 AI（max_rounds 换 AI 语义）、路由 LLM 兜底直判。新插件不存在任何"转述图片内容"能力入口；`image_search` 是**唯一**的 LLM 工具。 |
| ② | **搜图源池只支持两类源** | A 类视觉联网模型（AstrBot 提供商引用含反代 / 自定义 OpenAI 兼容端点）+ B 类图搜图引擎（SauceNAO / ascii2d / trace.moe）。**关键词纯文本搜索 API 本轮不做**（见 §7）。 |
| ③ | **破坏性重构 + 迁移脚本 + 版本 2.0.0，本地交付** | 插件 `metadata.yaml` 版本 `2.0.0`；提供 v1.7→v2.0 配置迁移脚本（§6）；不 push、不重启服务、不改 WebUI 前端。 |

### 0.1 术语（全文统一）

| 术语 | 含义 |
|---|---|
| **源（source）** | `search_sources` 数组中的一项，属 A 类（`vision`）或 B 类（`engine`）。 |
| **A 类 / vision 源** | 视觉联网模型。两种形态：`provider_ref`（AstrBot 提供商引用，可指向反代提供商）或 `api_base`+`api_key`+`model`（自定义 OpenAI 兼容端点）。 |
| **B 类 / engine 源** | 图搜图引擎：`saucenao` / `ascii2d` / `trace_moe`。只接受**图片公网 URL**。 |
| **传输形态（transport）** | 图片交给源时的形态：`data_uri` 或 `url`。 |
| **命中（hit）** | 源返回了可用结果：engine 有结构化 `match`；vision 有非空回答且判定为 HIT（§3.3）。 |
| **明确未命中（miss）** | 请求成功但确无结果：engine 语法合法但 0 结果；vision 判定为 MISS。 |
| **源失败（failure）** | 未得到有效响应：网络错误、HTTP 5xx/4xx、超时、挑战页、解析异常。 |
| **挑战页（challenge）** | Cloudflare / 机器人风控拦截页。识别规则见 §4.4。 |

---

## 1. 工具契约表

### 1.1 注册签名（冻结）

```python
@filter.llm_tool(name="image_search")
async def image_search(
    self,
    event: AstrMessageEvent,
    image_refs: list[str] | None = None,
    question: str = "",
    search_type: str = "auto",
) -> str:
```

- 工具名（对主模型可见）：`image_search`。
- 参数类型映射到 AstrBot 工具 schema：`list[str]` → JSON Schema `array`（`items: {type: string}`）；`str` → `string`。
- **`image_refs` 必须有默认值 `None`（P1 澄清）**：AstrBot 为 `@filter.llm_tool` 生成的工具 schema **不产 `required`**，框架不会替插件拒绝缺参，主模型漏传时 Python 会直接抛 `TypeError`（违反 §1.6.2「不得抛异常给主模型」）。因此实现侧签名给 `None` 兜底，进入函数后按 §1.2 判「缺失 → `status="no_refs"`」。**对主模型可见的参数名与类型不变（仍 `list[string]`）**，docstring（§1.3）逐字不变。
- 返回值：**`str`（JSON 字符串）**，结构见 §1.4。必须返回 str，不得返回 dict/list/None（否则主模型无法读取结果）。
- 工具内部**不发送任何消息**（不 `yield`、不 `event.send`）；结果只通过返回字符串回传主模型，由主模型组织回复。

### 1.2 参数表（冻结）

| 参数 | 类型 | 必填 | 默认值 | 语义 | 校验与异常行为 |
|---|---|---|---|---|---|
| `image_refs` | `list[string]` | **是**（语义必填；签名侧 `None` 兜底，见 §1.1） | `None` | 待搜图的引用列表，单图也可，支持多图（≤ 8 张，超出截断并记 `notes`）。接受形态见 §1.2.1。 | 缺失 / 非 list / 空 list → 返回 `status="no_refs"`，`ok=false`，`summary` 引导"请把图片路径或链接传给我"。**工具 schema 不产 `required`，框架不会替插件拒绝缺参**，所以缺参由本工具自己判为 `no_refs`（不得让 `TypeError` 冒泡，见 §1.1 与 §1.6.2）。按 `image_refs` 顺序处理，去重（同一归一键只处理一次）。 |
| `question` | `string` | 否 | `""` | 用户关于这些图片的原始问题（如"这是谁""出自哪部番""什么梗"）。会拼进 vision 源提示词；engine 源不使用。空串时用内置默认意图"这是谁 / 出自哪里 / 什么梗"。 | 非 str → 按 `str()` 强转；超 500 字截断到 500 字并在 `notes` 记 `question_truncated`。 |
| `search_type` | `string` | 否 | `"auto"` | 源选择策略，枚举 `auto` / `vision` / `engine`（大小写不敏感，首尾空白去除）。 | 非法值 → **不报错**：按 `auto` 执行，并在 `notes` 追加 `invalid_search_type:<原值>`；返回体中 `search_type` 字段回显**实际生效值**。 |

`search_type` 语义（冻结）：

| 取值 | 候选源顺序 |
|---|---|
| `auto` | 先全部启用 engine 源（按 `search_sources` 数组顺序），再全部启用 vision 源（按数组顺序）。 |
| `engine` | 仅启用 engine 源，按数组顺序。为空 → `status="empty_pool"`，**不回落** vision。 |
| `vision` | 仅启用 vision 源，按数组顺序。为空 → `status="empty_pool"`，**不回落** engine。 |
| `auto` 且两类启用源都为空 | `status="empty_pool"`。 |

#### 1.2.1 `image_refs` 元素可接受形态与归一化（冻结）

| 形态 | 示例 | 归一化规则 |
|---|---|---|
| 本地绝对路径 | `/root/AstrBot/data/temp/x.jpg` | 直接使用；必须存在且为常规文件。 |
| `file://` URI | `file:///root/AstrBot/data/temp/x.jpg` | 去掉 `file://` 前缀后按本地路径处理。 |
| `http(s)` URL | `https://example.com/a.png` | 走 SSRF 校验（§3.1 步骤 2）后下载。 |
| data URI | `data:image/png;base64,iVBOR...` | 直接解出字节。 |
| `base64://` | `base64://iVBOR...` | 解出字节。 |
| 附件占位文本 | `[Image Attachment: path /root/.../x.jpg]` | 正则抽出其中路径再按上表处理（兼容 `on_llm_request` 注入形态）。 |
| 其他 / 空串 | `""`、`"刚才那张图"` | 该图 `ok=false`，`error="bad_ref"`，不阻断其余图。 |

### 1.3 工具 docstring（逐字冻结，实现必须原样粘贴）

```python
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
```

**不调用本工具的场景（必须写进 docstring 之外的工具描述，也可保留在实现注释中）**：用户只是要求识别/转述图片里的文字内容、描述画面、提取表格数据、或者与"图中主体身份/出处"无关的图片理解——**不需要调用本工具**，直接由主模型自身多模态能力回答。

### 1.4 返回结构（JSON 字符串）逐字段冻结

顶层对象：

| 字段 | 类型 | 必出现 | 说明 |
|---|---|---|---|
| `tool` | `string` | 是 | 恒为 `"image_search"`。 |
| `ok` | `bool` | 是 | **全部**请求图片都 `ok=true` 时为 `true`，否则 `false`。 |
| `status` | `string` | 是 | 枚举：`ok`（全部命中）/ `partial`（部分命中）/ `failed`（全部未命中）/ `empty_pool`（无可用源）/ `no_refs`（未传图）/ `bad_refs`（所有引用都无法读取）。`ok` 为真当且仅当 `status=="ok"`。 |
| `search_type` | `string` | 是 | 实际生效值：`auto` / `vision` / `engine`。 |
| `question` | `string` | 是 | 回显（截断后）的 `question`。 |
| `images` | `list[object]` | 是 | 每张图一个对象，顺序与去重后的 `image_refs` 一致；结构见下。 |
| `summary` | `string` | 是 | 面向主模型的可读摘要（中文，≤ 800 字）：命中时逐图列出"主体/出处/来源链接"，未命中时给出人话原因与下一步建议。 |
| `hint` | `string` | 是 | 运维/配置引导文案；无问题时为 `""`。`empty_pool` 时给"请在插件配置 search_sources 添加搜图源"；**挑战引导（P8 统一口径，与 §5「源返回挑战页」行一致）**：当该图**存在 `challenge` 尝试，且没有任何 `ok` / `error` / `timeout`** 时给"目标站点被 Cloudflare 拦截，可配置自建反代 base_url 或提供 api_key"；纯 engine 池因图床未开而全 `skipped(no_public_url)` 且无命中时，改给"开启 image_url.enabled"的引导；**仅因超出源级上限而全被跳过（`image_too_large_for_source`）时，给"调大该源 `max_size_mb` / `max_pixels`（或全局值）后重试"的引导（P13-a）**。**P13-b：`summary` 与 `hint` 分工冻结**——同一 `status` 只允许**一个** `summary` 字面（`empty_pool` 恒为 `"当前没有可用的搜图源。"`），各情形的差异**只体现在 `hint`**，禁止用另一句 summary 表达同一 status。 |
| `elapsed_ms` | `int` | 是 | 本次工具调用总耗时（毫秒）。 |
| `cached` | `bool` | 是 | 是否整单命中缓存（缓存键含图片哈希 + search_type + question）。 |
| `notes` | `list[string]` | 是 | 补充说明，如 `invalid_search_type:xx`、`question_truncated`、`refs_truncated:8`、`migrated_config_hint`。无内容时为 `[]`。 |

`images[]` 元素：

| 字段 | 类型 | 必出现 | 说明 |
|---|---|---|---|
| `index` | `int` | 是 | 0 起算的图片序号。 |
| `ref` | `string` | 是 | 归一化后的引用（本地路径或 URL），超 200 字符截断。 |
| `ok` | `bool` | 是 | 该图是否命中。 |
| `transport` | `string` | 是 | 实际使用的传输形态：`data_uri` / `url` / `""`（未走到传输阶段）。 |
| `source` | `object \| null` | 是 | 最终采纳的源：`{"kind": "engine"\|"vision", "name": str, "id": str, "engine": str}`；未命中为 `null`。`engine` 仅在 kind=engine 时为引擎名，否则 `""`。 |
| `match` | `object \| null` | 是 | engine 命中的结构化结果，形状与 §4.1 的 `match` 完全一致：`{"similarity": float\|null, "title": str, "author": str, "links": list[string], "extra": str}`。vision 命中时为 `null`。 |
| `answer` | `string` | 是 | vision 源的原始回答正文（已剥掉 `VERDICT:` 首行）；engine 命中或未命中时为 `""`。 |
| `merged_from` | `list[object]` | 是 | 合并来源明细，元素 `{"kind": str, "name": str, "id": str, "similarity": float\|null, "title": str}`；未合并时为 `[]`。 |
| `attempts` | `list[object]` | 是 | 逐源尝试明细（含被跳过的源），元素见下。顺序即实际尝试顺序。 |
| `rounds_used` | `int` | 是 | 该图消耗的轮数（语义见 §3.2）。 |
| `cached` | `bool` | 是 | 该图是否命中缓存。 |
| `error` | `string` | 是 | 失败原因的人话描述；`ok=true` 时为 `""`。 |

`attempts[]` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `string` | `engine` / `vision`。 |
| `name` | `string` | 源显示名。 |
| `id` | `string` | 源 `id`。 |
| `engine` | `string` | engine 名（vision 为 `""`）。 |
| `outcome` | `string` | 枚举：`hit` / `miss` / `error` / `timeout` / `challenge` / `skipped`。 |
| `counted_round` | `bool` | 该次尝试是否计入 `rounds_used`（仅 `miss` 为 `true`）。 |
| `error` | `string` | 失败/跳过原因（人话）；成功时为 `""`。 |
| `challenge` | `bool` | 是否挑战页。 |
| `elapsed_ms` | `int` | 该源耗时。 |

**P2（冻结澄清）：`images[]` 元素上不存在 `challenge` 字段。** §3.1 伪代码早期版本在同一处同时写了 14 个字段 + `challenge=`，与本节字段表不一致；终态口径为**不输出** `images[].challenge`——挑战语义由 `attempts[].challenge`（逐源）与顶层 `hint`（汇总引导）承载，`status` 仍是 §1.4 的六值枚举，不新增值。任何实现或测试不得据「§3.1 出现 challenge=」断言该键存在。

### 1.5 返回示例（冻结形状，实现须能产出等价物）

```json
{
  "tool": "image_search",
  "ok": true,
  "status": "partial",
  "search_type": "auto",
  "question": "这是谁",
  "images": [
    {
      "index": 0,
      "ref": "/root/AstrBot/data/temp/a.jpg",
      "ok": true,
      "transport": "url",
      "source": {"kind": "engine", "name": "SauceNAO", "id": "saucenao-1", "engine": "saucenao"},
      "match": {"similarity": 92.41, "title": "作品名", "author": "作者", "links": ["https://..."], "extra": ""},
      "answer": "",
      "merged_from": [
        {"kind": "engine", "name": "SauceNAO", "id": "saucenao-1", "similarity": 92.41, "title": "作品名"},
        {"kind": "vision", "name": "豆包联网搜索", "id": "doubao-search", "similarity": null, "title": "角色名"}
      ],
      "attempts": [
        {"kind": "engine", "name": "SauceNAO", "id": "saucenao-1", "engine": "saucenao", "outcome": "hit", "counted_round": false, "error": "", "challenge": false, "elapsed_ms": 1204}
      ],
      "rounds_used": 0,
      "cached": false,
      "error": ""
    }
  ],
  "summary": "图 1：疑似出自《作品名》（相似度 92.41%），作者：作者。",
  "hint": "",
  "elapsed_ms": 1502,
  "cached": false,
  "notes": []
}
```

### 1.6 关键约束（冻结）

1. 工具必须容忍**部分图失败**：单图失败不影响其他图，最终 `status="partial"`。
2. 工具**不得抛异常给主模型**：内部所有异常必须被捕获并转成 `status="failed"` 的结构化返回（含人话 `summary`/`hint`）。
3. `summary` 必须是主模型可直接改写为群回复的自然语言，禁止把原始 JSON 塞进 `summary`。
4. 工具返回**不得包含 API Key**（任何字段；如需提及只允许前 4 位 + `***`）。

---

## 2. 配置 schema 表（file: `_conf_schema.json`）

AstrBot 支持的类型集合（冻结，取自 `astrbot/core/config/default.py: DEFAULT_VALUE_MAP`）：`int` / `float` / `bool` / `string` / `text` / `list` / `file` / `object` / `template_list` / `dict`。本节所有 `type` 必须在此集合内。

### 2.1 顶层键总览

| 键 | type | default | 必填 | 说明 |
|---|---|---|---|---|
| `search_sources` | `template_list` | `[]` | 否 | 搜图源池，双模板 `vision` / `engine`（§2.2、§2.3）。 |
| `search` | `object` | `{}` | 否 | 检索通用参数（§2.5）。**P7**：`object` 键的 `default` 为 `{}`，实际生效值由 schema `items` 逐键默认值填充（AstrBot `_parse_schema` 对 `object` 走 `items` 递归，不读该 `default`）；此处给 `{}` 只为满足 §2.8「每个字段都必须给出 `default`」。 |
| `image_url` | `object` | `{}` | 否 | 内置只读图床（§2.6），只为 B 类引擎提供公网 URL。**P7**：同 `search`，`default` 为 `{}`，生效值由 `items` 递归补齐。 |
| `download_timeout_sec` | `int` | `20` | 否 | 远程图片下载超时（秒），范围 5~120。 |
| `tool_hint_enabled` | `bool` | `true` | 否 | 是否在 LLM 请求前注入「图片路径 + image_search 工具提示」（§2.7）。 |
| `log_level` | `string` | `"info"` | 否 | 插件日志级别，枚举 `debug` / `info` / `warning` / `error`。 |

### 2.2 `search_sources` 模板 `vision`（A 类：视觉联网模型）

AstrBot `template_list` 的 `templates.vision`，字段如下（`enabled` 置于首位）：

| 字段 | type | default | 必填 | hint 要点 |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | 否 | 关掉即跳过该源，不参与任何 `search_type` 候选。 |
| `id` | `string` | `""` | 否 | 源唯一标识；留空自动生成 `vision_<数组下标>`。用于日志/返回体 `source.id`。 |
| `name` | `string` | `""` | 否 | 显示名；留空取 `id`。 |
| `provider_ref` | `string` | `""` | 否* | AstrBot 提供商引用 ID（WebUI 中提供商的 ID，如 `豆包/doubao`，可指向反代提供商）。**非空时优先走该形态**；此时忽略 `api_base`/`api_key`。 |
| `api_base` | `string` | `""` | 否* | 自定义 OpenAI 兼容端点基础地址（如 `https://host/v1`，可带或不带 `/chat/completions`，不带时自动补）。 |
| `api_key` | `string` | `""` | 否* | 自定义端点密钥，`Authorization: Bearer <key>`。 |
| `model` | `string` | `""` | 否* | 模型名（自定义端点形态必填）。**P4（以 §4.7 为准）**：provider 引用形态下本字段留空即用提供商默认模型；填了也会被忽略（以 §4.7 为准），被忽略的源会在启动 WARNING 与 notes `provider_model_ignored:<id>` 中可见。 |
| `web_search` | `bool` | `true` | 否 | 是否要求该模型联网检索（写入提示词）。关掉表示该模型本身自带联网能力，不需要额外提示。 |
| `timeout_sec` | `int` | `0` | 否 | 该源超时（秒）；`0` = 沿用 `search.source_timeout_sec`。 |
| `max_size_mb` | `int` | `0` | 否 | 该源接受的图片体积上限；`0` = 沿用 `search.max_size_mb`。 |
| `max_pixels` | `int` | `0` | 否 | 该源接受的像素上限；`0` = 沿用 `search.max_pixels`。 |
| `extra_headers` | `object` | `{}` | 否 | 附加请求头（反代鉴权、`api-key` 等）。 |

\* 合法性规则（冻结，见 §2.4）：`provider_ref` 非空 **或** （`api_base` 与 `model` 同时非空）即为合法源；两者都不满足 → 该源标记 `invalid`，运行期跳过并在 `attempts` 记 `outcome="skipped"`。

### 2.3 `search_sources` 模板 `engine`（B 类：图搜图引擎）

| 字段 | type | default | 必填 | hint 要点 |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | 否 | 关掉即跳过。 |
| `id` | `string` | `""` | 否 | 唯一标识；留空自动 `engine_<数组下标>`。 |
| `name` | `string` | `""` | 否 | 显示名；留空取引擎默认名（`SauceNAO` / `ascii2d` / `trace.moe`）。 |
| `engine` | `string` | `""` | **是** | 引擎名，枚举 `saucenao` / `ascii2d` / `trace_moe`。非法值或空 → 该源 `invalid`，运行期跳过并记日志。 |
| `api_key` | `string` | `""` | 否 | 仅 SauceNAO 使用（作为查询参数 `api_key`）。ascii2d / trace.moe 忽略。 |
| `base_url` | `string` | `""` | 否 | 覆盖默认端点（自建反代）；留空用 §4.3 内置默认端点。 |
| `timeout_sec` | `int` | `0` | 否 | 该源超时（秒）；`0` = 沿用 `search.source_timeout_sec`。 |
| `extra_headers` | `object` | `{}` | 否 | 附加请求头。 |

### 2.4 源解析规则（冻结，实现必须完全一致）

1. `id` 为空 → `f"{kind}_{下标}"`（`kind` 为 `vision`/`engine`）；仍冲突 → 追加 `_2`、`_3`（按出现次数）。
2. `name` 为空 → vision 取 `id`；engine 取引擎默认名映射 `{"saucenao": "SauceNAO", "ascii2d": "ascii2d", "trace_moe": "trace.moe"}`。
3. vision 形态判定：`provider_ref.strip()` 非空 → `form="provider"`；否则 `api_base.strip()` 与 `model.strip()` 都非空 → `form="custom"`；否则 `invalid`。
4. engine 形态判定：`engine.strip()` 属枚举 → 合法；否则 `invalid`。
5. 生效超时（秒）：`int(src.get("timeout_sec") or 0) or int(search.source_timeout_sec)`；命中值为 0 或负数时回落 `search.source_timeout_sec`。
6. 生效图片上限：`src.max_size_mb or search.max_size_mb`、`src.max_pixels or search.max_pixels`；但**压缩目标**始终由全局 `search.max_size_mb` / `search.max_pixels` 决定（先按全局压缩，再按源上限校验；超过源上限且无法再压缩 → 该源 `outcome="skipped"`，`error="image_too_large_for_source"`）。**P6（冻结澄清）：本条只对 vision 源生效** —— engine 模板不提供 `max_size_mb` / `max_pixels`（§2.3 只有 8 个字段），engine 源不参与任何源级大小/像素判定，只用全局压缩后的结果；engine 源因尺寸被跳过的情况**不存在**（不得据本条给 engine 源判 `image_too_large_for_source`，也不得在 engine 模板补这两个字段）。
7. 候选顺序 = `search_sources` 数组顺序，先按 `search_type` 选择类别集合（§1.2），不排序、不按健康度重排（健康统计仅供日志与摘要，不改变顺序）。

### 2.5 `search` 小节

| 键 | type | default | 必填 | 语义（冻结） |
|---|---|---|---|---|
| `max_rounds` | `int` | `2` | 否 | 每图允许的**轮数上限**，语义见 §3.2。范围 0~10；0 = 不因"空结果"继续换源（第一个 miss 即停）。 |
| `source_timeout_sec` | `int` | `30` | 否 | 单个源的默认超时（秒），范围 5~300；被源自身 `timeout_sec` 覆盖。 |
| `total_timeout_sec` | `int` | `180` | 否 | 单次工具调用总预算（秒），范围 30~900。超预算后停止尝试剩余源，已得结果照常返回，`notes` 追加 `total_timeout`。 |
| `merge_enabled` | `bool` | `true` | 否 | 命中后是否继续尝试后续源以合并多源结果（§3.4）。`false` = 首个命中即停。 |
| `merge_max_sources` | `int` | `3` | 否 | 每图最多合并的源数量（含首个命中源），范围 1~5。达到即停止继续尝试。 |
| `cache_ttl_sec` | `int` | `300` | 否 | 结果缓存 TTL（秒），范围 0~3600；0 = 关闭缓存。缓存键 = `sha256(图片字节) + search_type + question`。 |
| `health_stats` | `bool` | `true` | 否 | 是否累计各源成功率/耗时统计，并在 `terminate()` 与 `log_level>=info` 时输出摘要（不影响路由顺序）。 |
| `max_size_mb` | `int` | `20` | 否 | 图片体积上限（MB），范围 1~100；超过则压缩，压缩后仍超 → 该图 `ok=false`、`error="image_too_large"`。 |
| `max_pixels` | `int` | `16777216` | 否 | 图片像素上限（宽×高），范围 1048576~67108864；默认 4096×4096。超过则等比缩放。 |

**P11 补充（物化读入上限，冻结）**：`max_size_mb` 是**压缩目标**，不是读取闸门。图片在「读进内存」这一步另有一道**不随配置缩放的绝对上限 `100 MiB`（`100 * 2**20` 字节）**，只为挡病态大文件；实现**不得**把闸门写成 `max_size_mb × 常数`（否则本键调小时压缩路径会先被闸门拦掉，`image_too_large` 与「先压缩再判定」的语义全部失效）。读取时先探测**前 32 字节**魔数，非图片直接拒（不整体读入）；本地文件先按 `stat` 体积预检。绝对上限之外的超限判定仍走 §3.1 第 3 条的压缩流程。

### 2.6 `image_url` 小节（内置只读图床）

| 键 | type | default | 必填 | 语义（冻结） |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | 否 | 插件加载时启动内置只读 HTTP 图床；**仅 B 类引擎需要公网 URL**（vision 源一律走 data URI，不依赖本开关）。 |
| `dir` | `string` | `""` | 否 | 图片落盘目录；留空运行时解析为 `{AstrBot data}/plugin_data/astrbot_plugin_image_search/imgserv`（`get_astrbot_plugin_data_path()` 派生）；目录不存在则创建，不可写时告警并降级（url 传输不可用）。 |
| `port` | `int` | `8123` | 否 | 监听端口，范围 1~65535；被占用 → 端口 +1 重试（最多 10 次），成功后日志记实际 `bound_port`。 |
| `public_base_url` | `string` | `""` | 否 | 对外访问前缀；留空 → `http://<自动探测公网 IP>:<bound_port>`。末尾斜杠自动去除。 |

服务安全约束（冻结）：仅 `GET`/`HEAD`；文件名白名单（`^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp|bmp)$`，即 `sha256` 前 32 位 + 扩展名）；URL 解码后必须仍在 `dir` 内（防穿越）；无目录列表；绑定 `0.0.0.0`。

### 2.7 `download_timeout_sec` / `tool_hint_enabled` / `log_level`

| 键 | type | default | 语义（冻结） |
|---|---|---|---|
| `download_timeout_sec` | `int` | `20` | 下载 `http(s)` 引用图片的超时（秒）。下载失败（超时/非 200/非图片魔数）→ 该图 `ok=false`、`error="download_failed"`。 |
| `tool_hint_enabled` | `bool` | `true` | `true` 时在 `@filter.on_llm_request()` 里：把 `req.image_urls` 中未在文本出现的引用以 `[Image Attachment: path <ref>]` 追加进 `extra_user_content_parts`，并追加一行 `image_search` 工具提示（含 `[image-search-hint]` 标记，幂等去重）。`false` → 完全不注入。注入异常不得阻断请求。 |
| `log_level` | `string` | `"info"` | 枚举 `debug`/`info`/`warning`/`error`；非法值回落 `info` 并告警。日志一律英文（仓库规范）。 |

### 2.8 配置规范化（冻结）

- 插件 `initialize()` 时对配置做一次规范化：逐键校验类型与范围，非法值 → 回落默认值 + `LOG.warning`（英文，含键名与原值），**不阻断加载**。
- 缺失键 → 由 AstrBot `AstrBotConfig` 依据 schema 补齐默认值（依赖 schema 的 `default`，因此 §2 每个字段都必须给出 `default`）。
- `search_sources` **接受三种形态（P5 放宽）**：① `list`（正常形态）；② **JSON 字符串**（WebUI 或旧配置偶尔把整个数组写成字符串）；③ **keyed dict**（面板/框架回写时按 key 索引的映射）。三者都按语义解析为「源条目序列」。**无法解析为上述任一形态 → 视为 `[]` 并告警**；元素非 dict → 跳过该元素并告警（含下标）。
- 启动期日志必须一行摘要：`image_search v2.0.0 activated: vision_sources=[...] engine_sources=[...] search_type_default=auto max_rounds=2 image_url=on/off`。

---

## 3. 搜图流水线（伪代码）

### 3.1 主流程（冻结）

```
async def image_search(event, image_refs, question="", search_type="auto") -> str:
    t0 = now()
    cfg = normalized_config()
    notes = []
    stype = normalize_search_type(search_type, notes)      # 非法 → "auto" + note
    refs  = normalize_refs(image_refs, notes)              # 去重、去空、≤8 张、保序
    if not refs:
        return json_str(status="no_refs", ok=False,
                        summary="请把图片路径或图片链接一起发给我。", hint="...")

    candidates = select_candidates(cfg, stype)             # §1.2 顺序；每项 {kind,id,name,engine,form,timeout,...}
    if not candidates:
        return json_str(status="empty_pool", ok=False,
                        summary="当前没有可用的搜图源。",
                        hint="请在插件配置 search_sources 里添加 vision 或 engine 源。")

    results = []
    for idx, ref in enumerate(refs):                       # 逐图独立，互不影响
        # ---- 1) 归一键 + 缓存
        raw = materialize_ref(ref)                         # 本地路径 / file:// / http(s) / data URI / base64://
        if raw is None:
            results.append(image_obj(idx, ref, ok=False, error="bad_ref")); continue
        data, ext = raw                                    # bytes, 扩展名（魔数嗅探优先）
        ckey = sha256(data) + stype + question
        if cfg.search.cache_ttl_sec > 0 and (hit := cache.get(ckey)):
            results.append(hit | {"cached": True}); continue

        # ---- 2) 预处理：SSRF 校验（下载前）/ 体积像素校验 / 压缩
        if ref_is_url(ref) and is_blocked_host(host_of(ref)):   # 非 http(s)、内网/环回/链路本地/保留段 → 拒绝
            results.append(image_obj(idx, ref, ok=False, error="ssrf_blocked")); continue
        data, ext, changed = preprocess(data, ext,
                                        cfg.search.max_size_mb, cfg.search.max_pixels)
        if len(data) > cfg.search.max_size_mb * 2**20:
            results.append(image_obj(idx, ref, ok=False, error="image_too_large")); continue

        # ---- 3) 传输形态选择（每图只算一次）
        data_uri = "data:{mime};base64,{b64}"              # vision 用
        public_url = publish_to_imgserv(data, ext) if cfg.image_url.enabled else None
        #   引擎需要 URL；public_url 为 None → 所有 engine 源跳过(no_public_url)

        # ---- 4) 全程总预算
        deadline = t0 + cfg.search.total_timeout_sec

        # ---- 5) 按候选顺序尝试（轮数隔离 + 单源超时隔离）
        attempts, merged, rounds, hits, stop = [], [], 0, [], False
        for src in candidates:
            if now() >= deadline: notes.append("total_timeout"); break
            if len(hits) >= cfg.search.merge_max_sources: break
            if cfg.search.merge_enabled is False and hits: break
            transport = "url" if src.kind == "engine" else "data_uri"
            if src.kind == "engine" and not public_url:
                attempts.append(src, outcome="skipped", error="no_public_url"); continue
            if len(data) > src.max_size_mb*2**20 or pixels > src.max_pixels:
                attempts.append(src, outcome="skipped", error="image_too_large_for_source"); continue

            try:
                r = await asyncio.wait_for(
                        run_source(src, image={"data_uri": data_uri, "url": public_url},
                                   cfg=cfg, timeout_sec=src.timeout),
                        timeout=src.timeout + 5)           # 外层硬闸，避免内部挂死
            except asyncio.TimeoutError:
                attempts.append(src, outcome="timeout", counted_round=False); continue
            except Exception as e:
                attempts.append(src, outcome="error", error=str(e), counted_round=False); continue

            if r["ok"]:                                    # 命中
                hits.append((src, r))
                merged = merge_results(hits, cfg.search.merge_max_sources)
                attempts.append(src, outcome="hit", counted_round=False)
                if not cfg.search.merge_enabled or len(hits) >= cfg.search.merge_max_sources:
                    break
                continue                                   # 已命中，继续找第二源做合并
            if r.get("challenge"):
                attempts.append(src, outcome="challenge", counted_round=False); continue
            if r.get("miss"):                              # 明确未命中
                attempts.append(src, outcome="miss", counted_round=True, error=r["error"])
                rounds += 1
                if rounds >= cfg.search.max_rounds:
                    stop = True; break
                continue
            attempts.append(src, outcome="error", counted_round=False, error=r["error"])
            # 源失败不计轮数、不停止 → 继续下一源

        # ---- 6) 汇总该图
        if merged:
            results.append(image_obj(idx, ref, ok=True, transport=…, source=merged.head,
                                     match=merged.match, answer=merged.answer,
                                     merged_from=merged.from, attempts=attempts,
                                     rounds_used=rounds))
        else:
            results.append(image_obj(idx, ref, ok=False, transport=…, attempts=attempts,
                                     rounds_used=rounds, error=describe(attempts)))
            # 不写 images[].challenge（P2）：挑战语义只由 attempts[].challenge + hint 承载

        # ---- 7) 写缓存（命中才写；TTL=search.cache_ttl_sec）
        if merged and cfg.search.cache_ttl_sec > 0: cache.set(ckey, results[-1])

    # ---- 8) 输出：结构化 JSON + 可读摘要（不走任何消息发送）
    if cfg.search.health_stats: health.record(attempts of all images)
    return json_str(tool="image_search", images=results, status=aggregate(results),
                    summary=render_summary(results, question),
                    hint=render_hint(results), elapsed_ms=…, cached=all_cached(results),
                    notes=notes)
```

伪代码中的契约要点（均为硬要求）：

1. **归一化 refs 在前**：到达任何源之前完成，去重保序，最多 8 张。
2. **SSRF 防护**：仅允许 `http` / `https`；拒绝内网（`10/8`、`172.16/12`、`192.168/16`、`127/8`、`169.254/16`、`::1`、`fc00::/7`、`fe80::/10`）与保留段、`localhost` 及 `.local` 域名；**重定向后的最终 URL 需二次校验**。
3. **压缩**：超 `max_size_mb` 或超 `max_pixels` → 等比缩放（最长边 ≤ 4096）并重编码为 JPEG（quality 85），保留原扩展名映射；压缩失败 → 用原图但按上限校验。
4. **传输形态**：vision 源一律 `data_uri`；engine 源一律 `url`（图床 URL）。图床关闭 → engine 源全部 `skipped(no_public_url)`，不报错。
5. **单源超时隔离**：每源 `asyncio.wait_for`，超时不污染其他源、不污染其他图。
6. **切换规则**：源失败（`error`/`timeout`/`challenge`）→ **不计轮数**、继续下一源；只有 `miss` 计轮数。
7. **合并去重**：命中后若 `merge_enabled=true` 且未达 `merge_max_sources` → 继续尝试后续源，全部命中合并（§3.4）。
8. **输出**：结构化 JSON（§1.4） + `summary` 可读摘要；不发送消息、不 `yield`。
9. **物化读入上限（P11，冻结）**：图片在「读进内存」这一步有一道**不随配置缩放的绝对上限 `100 MiB`（`100 * 2**20` 字节）**。`search.max_size_mb` 是**压缩目标**（压缩后仍超才判 `image_too_large`），绝不是读取闸门——绝对上限的存在只为挡住病态大文件，**不得**把上限乘在 `max_size_mb` 上（否则 `max_size_mb=1` 时天花板会塌到几 MB，压缩路径根本跑不到）。读取阶段先**嗅探前 32 字节**：魔数不是图片就立刻拒绝（`bad_ref` / `download_failed`，视来源而定），**不整体读入**；本地文件先按 `stat` 体积预检。

### 3.2 `search.max_rounds` 语义（冻结，逐字生效）

- **计轮**：仅当某源返回**结果为空或明确未命中**（`outcome="miss"`）时，`rounds += 1`。
- **不计轮**：源失败（网络错误 / HTTP 4xx、5xx / 解析失败 / 挑战页 / 超时 / 因尺寸被跳过 / 因缺 URL 被跳过）**一律不计轮数**，且不停止尝试，按顺序继续下一个源。
- **停止条件**（任一满足即停止该图的换源）：`rounds >= max_rounds`；候选源耗尽；`total_timeout_sec` 预算耗尽；`merge_max_sources` 已达；已命中且 `merge_enabled=false`。
- `max_rounds=0` → 第一个 `miss` 立即停止。
- **禁止**任何形式的"质量不满意换 AI""报错换 AI 直到成功""兜底直判"逻辑：源失败只做顺序切换，不做重试同一源、不做模型兜底判断。

### 3.3 命中判定（冻结）

| 源类型 | 命中（`ok=true`） | 明确未命中（`miss`） | 失败（`error`） |
|---|---|---|---|
| engine | 解析出 ≥1 条可用结果（`match` 非空，`title` 或 `links` 至少一项非空） | HTTP 200 且解析成功但 0 条结果 | 网络/HTTP/解析异常、超时、挑战页 |
| vision | 回答首个非空行匹配 `^\s*VERDICT\s*[:：]\s*HIT\b`（大小写不敏感），或未给 VERDICT 但回答非空、长度 ≥ 4 且不含 MISS 标记词 | 首个非空行 `VERDICT: MISS`，或回答去空白后长度 < 4，或回答包含冻结标记词之一 | HTTP/网络异常、超时 |

冻结 MISS 标记词表（实现必须逐字使用，可单测）：

```
["无法识别", "无法判断", "无法确定", "看不出", "识别不出", "不确定这是什么",
 "我无法回答", "cannot identify", "can't identify", "unable to identify", "not able to identify"]
```

vision 源提示词（冻结，实现逐字使用）：

```
system: 你是搜图路由 Agent 的联网搜图源。你只做一件事：识别图片主体的身份与来历并联网核实。
        禁止转述图片文字内容。

user:   请联网检索并回答这张图片中主体的身份与来历。用户问题：{question 或 "（未提供，按“这是谁 / 出自哪里”处理）"}
        输出要求（严格遵守）：
        第一行必须是 VERDICT: HIT 或 VERDICT: MISS。HIT = 联网检索后给出了确定的身份/来源/出处；MISS = 无法确定。
        第二行起用中文简述：主体名称、作品/出处、角色或场景说明、可核对的来源链接（如有）。
        不要复述图片中的文字，不要输出与主体身份无关的内容。
```

### 3.4 合并与去重（冻结）

1. 合并顺序 = 命中发生的先后顺序；`source` 字段取**第一个**命中源。
2. 去重键：engine 结果取 `(title.strip().lower(), author.strip().lower())`；vision 结果取 `answer` 去空白后的前 80 字符 `sha1`。
3. `merged_from` 逐源列出（含首个源），最多 `merge_max_sources` 条；`match` 取**相似度最高**的 engine 结果（`similarity` 为 `null` 时视为 0）。
4. `answer` 取第一个非空 vision 回答；`summary` 中每个来源都要有归属标注（如"图搜图引擎 SauceNAO / 视觉联网模型 豆包联网搜索"）。

### 3.5 缓存与健康统计（冻结）

- 缓存键：`sha256(图片字节) + "|" + search_type + "|" + question`；值为该图最终 `images[]` 对象（含 attempts）深拷贝。
- 命中缓存：直接返回，`cached=true`（顶层 `cached` 为全部图都命中缓存）；**不发起任何网络请求**。
- 健康统计（`health_stats=true`）：每源累计 `attempts`/`hits`/`misses`/`errors`/`challenges`/平均耗时；在 `terminate()` 输出一行英文摘要；不得用于改变候选顺序。

---

## 4. 引擎适配契约（冻结接口，core-smith 与 engine-smith 按此对接）

> 双方**不得各自发明形状**：core 只按下述签名调用，engine 只按下述结构返回。

### 4.1 统一签名与返回（冻结）

```python
async def run_engine(engine: str, image: dict, cfg: dict, timeout_sec: float) -> dict
```

- `engine`：`"saucenao"` / `"ascii2d"` / `"trace_moe"`（其余值 → 抛 `ValueError`，core 侧不会传非法值）。
- `cfg`：插件规范化后的完整配置 dict（只读），engine 允许读取 `cfg["search_sources"]` 中该源的 `base_url` / `api_key` / `extra_headers`（通过 `cfg["_engine_opts"][engine]` 只读注入，键为 `base_url`/`api_key`/`extra_headers`，缺失视为空）。
- `timeout_sec`：**float**，该源生效超时；engine 内部必须用该值作为 httpx 超时（不得使用硬编码超时）。
- **返回**（所有分支统一为这一种 dict，任何异常都不得外抛）：

```python
{
  "engine": str,        # 引擎名回显
  "ok": bool,           # 是否命中
  "match": None | {
      "similarity": float | None,   # 0~100；无相似度概念（trace.moe）→ None
      "title": str,                 # 作品/主体名；未知 → ""
      "author": str,                # 作者/制作方；未知 → ""
      "links": list[str],           # 可核对链接（http/https），无 → []
      "extra": str                  # 人话补充（如「第 12 集 04:31」「相似度低，仅供参考」）
  },
  "error": str,         # ok=False 时的原因（人话，中文）；ok=True 时为 ""
  "challenge": bool,    # 是否被 Cloudflare/机器人挑战拦截
  "miss": bool           # 是否「请求成功但明确未命中」（HTTP 200 + 解析成功 + 0 结果）
}
```

返回三元组语义（互斥，冻结）：`ok=True` ⇒ `error=""`、`miss=False`；`challenge=True` ⇒ `ok=False`、`miss=False`；`miss=True` ⇒ `ok=False`、`challenge=False`；其余 ⇒ `ok=False`、`miss=False`、`challenge=False`、`error=人话原因`。

### 4.2 `image` 入参（冻结）

```python
image = {"data_uri": str, "url": str | None}
```

- `data_uri`：必存在，`data:<mime>;base64,<payload>`。
- `url`：可访问的公网图片 URL；图床未启用时为 `None`。
- **三个引擎都只使用 `url`**；`url is None` 时 engine 必须返回 `ok=False`、`miss=False`、`challenge=False`、`error="no_public_url"`（不抛异常、不发请求）。

### 4.3 逐引擎 HTTP 契约

| 引擎 | 方法与端点（默认） | 请求要点 | 超时 | `match` 映射 |
|---|---|---|---|---|
| `trace_moe` | `GET https://api.trace.moe/search?anilistInfo=true&url=<图片URL>` | URL 作为查询参数（需 URL-encode）；`base_url` 非空时替换为 `{base_url}/search`（保留 `anilistInfo=true` 与 `url`） | `timeout_sec` | 取 `result[0]`：`similarity = similarity * 100`（float，4 舍 5 入到 2 位）；`title` = `anilist.title.native or anilist.title.romaji or anilist.title.english or filename`；`author` = `""`；`links` = `["https://anilist.co/anime/<anilist.id>"]`（`anilist.id` 存在时，否则 `[]`），另追加 `video` URL（存在时）；`extra` = `"第 <episode> 集 <from 秒→mm:ss>"`（`episode`/`from` 存在时，否则 `""`） |
| `saucenao` | `GET https://saucenao.com/search.php?output_type=2&numres=3&url=<图片URL>` | `api_key` 非参数时追加 `&api_key=<key>`；`base_url` 非空时替换 host 与 path（保留三个查询参数） | `timeout_sec` | JSON：`results[0]` → `similarity = float(headers.similarity)`（已是 0~100）；`title` = `data.title` 或 `data.jp_name` 或 `data.material`；`author` = `data.author_name` 或 `data.creator` 或 `data.member_name`；`links` = `data.ext_urls` 中所有 `http(s)` 项；`extra` = `"相似度 <sim>%"`（<60 时追加 `"，相似度较低，仅供参考"`） |
| `ascii2d` | `POST https://ascii2d.net/search/uri` | 表单 `uri=<图片URL>`（`Content-Type: application/x-www-form-urlencoded`）；`base_url` 非空时替换为 `{base_url}/search/uri` | `timeout_sec` | HTML：取第一个结果块 → `similarity = None`；`title` = 结果标题文本（`<h6>`）；`author` = 结果作者/画师链接文本（存在时）；`links` = 结果块内全部 `http(s)` 链接；`extra` = 匹配方式标签（`"色合检索"` / `"特征检索"`，从结果页文本判定，缺失为 `""`） |

结果为空 / 元素缺失时的统一处理（冻结）：

- 三者 `match` 中任何字符串字段缺失一律 `""`，列表缺失一律 `[]`，`similarity` 缺失一律 `None`；**不得**用 `None` 表示空字符串。
- `links` 只保留 `http(s)` 且去重、保序、最多 5 条。
- trace.moe：返回体是 JSON 且**含 `"error"` 键** → `ok=False`、`error = "<原文 error 值>"`（原文照抄，可加中文前缀 `trace.moe 返回错误：`），**不得抛异常**、不得当作 miss；`result` 为 `[]` 或缺失 → `miss=True`。

### 4.4 挑战 / 风控识别（冻结）

识别规则（任一成立即判为挑战）：

1. 响应体前 200 字符 strip 后以 `<!DOCTYPE html` 开头（大小写不敏感），**且**正文含下列标记之一：`Just a moment`、`cf-challenge`、`challenge-platform`、`cf_chl_opt`、`Attention Required! | Cloudflare`、`__cf_chl`。
2. HTTP 状态码 ∈ {403, 429} **且** 响应 `Content-Type` 含 `text/html`。
3. HTTP 状态码 ∈ {403, 429} **且** 响应体为空或长度 < 2048 且非目标格式（SauceNAO 期望 JSON、ascii2d 期望 HTML 结果页）。

挑战分支返回（冻结）：

```python
{"engine": <engine>, "ok": False, "match": None,
 "error": "目标站点返回 Cloudflare 人机校验页，未取得结果。可配置自建反代（engine 源 base_url）或提供 api_key 后重试。",
 "challenge": True, "miss": False}
```

**禁止**把挑战页当结果解析（例如从 HTML 里正则抠出标题/链接）。挑战页必须走 challenge 分支，不产生任何 `match`。

### 4.5 解析与 HTTP 分层（冻结，离线可测）

解析函数必须接受**文本**、返回 §4.1 的 `match` / miss 判定，**不发起任何网络请求**：

| 引擎 | 解析函数（冻结签名） | 输入 |
|---|---|---|
| `saucenao` | `parse_saucenao(text: str) -> dict` → `{"ok": bool, "miss": bool, "match": dict\|None, "error": str}` | `output_type=2` 的 JSON 文本 |
| `ascii2d` | `parse_ascii2d(text: str) -> dict` → 同上形状 | ascii2d 结果页 HTML 文本 |
| `trace_moe` | `parse_trace_moe(text: str) -> dict` → 同上形状 | trace.moe JSON 文本 |
| 通用 | `is_challenge(text: str, status: int, content_type: str) -> bool` | 原始文本 + 状态码 + 头 |
| 通用 | `run_engine(engine, image, cfg, timeout_sec) -> dict` | 组合：HTTP → is_challenge → parse_* |

解析函数在入参非法 JSON / 空文本时不得抛异常：返回 `{"ok": False, "miss": False, "match": None, "error": "解析失败：<原因>"}`。

### 4.6 离线 fixture 清单（engine-smith 必须提供，verifier 据此单测）

| 文件 | 内容要求 | 期望 |
|---|---|---|
| `tests/fixtures/tracemoe_ok.json` | `{"result": [{"similarity": 0.9123, "episode": 12, "from": 271.5, "anilist": {"id": 12345, "title": {"native": "作品名", "romaji": "Sakuhin"}}, "video": "https://..."}]}` | `ok=True`，`similarity≈91.23`，`title="作品名"` |
| `tests/fixtures/tracemoe_empty.json` | `{"result": []}` | `miss=True` |
| `tests/fixtures/tracemoe_error.json` | `{"error": "Failed to fetch image https://example.com/x.jpg"}` | `ok=False`、`miss=False`、`error` 含原文 |
| `tests/fixtures/saucenao_ok.json` | `{"results": [{"header": {"similarity": "92.41"}, "data": {"title": "作品名", "author_name": "作者", "ext_urls": ["https://..."]}}]}` | `ok=True`，`similarity=92.41` |
| `tests/fixtures/saucenao_empty.json` | `{"results": []}` | `miss=True` |
| `tests/fixtures/saucenao_challenge.html` | 以 `<!DOCTYPE html` 开头且含 `Just a moment...` 与 `challenge-platform` | `is_challenge=True` |
| `tests/fixtures/ascii2d_ok.html` | 含结果块（`<h6>` 标题、画师链接、`色合检索` 标签） | `ok=True`，`links` 非空 |
| `tests/fixtures/ascii2d_empty.html` | 结果页但无结果块 | `miss=True` |
| `tests/fixtures/ascii2d_challenge.html` | 同 CF 挑战页 | `is_challenge=True` |

### 4.7 A 类（vision）源调用契约（冻结）

| 形态 | 解析规则 | 调用方式 | 取文本 |
|---|---|---|---|
| `form="provider"` | `provider_ref` → `self.context.get_provider_by_id(provider_ref)`；失败则取 `provider_ref.split("/")[-1]` 再试一次；仍失败 → 该源 `ok=False`、`error="provider_not_found: <ref>"` | 从 ProviderManager 读该提供商的 `api_base`（`provider.provider_config["api_base"]` 或 `provider_reader` 等价路径）/ `api_key` / `model`，统一按 `POST {api_base[-/chat/completions]}/chat/completions` 发起（**与 custom 形态同一 HTTP 路径**，保证行为一致、可离线单测） | `choices[0].message.content`；为空回退 `choices[0].message.reasoning_content` |
| `form="custom"` | `api_base` 不以 `/chat/completions` 结尾则自动拼接 | `POST`，头 `Authorization: Bearer <api_key>`、`Content-Type: application/json`；体 `{"model": <model>, "messages": [{"role":"system","content": SEARCH_SYSTEM_PROMPT}, {"role":"user","content":[{"type":"text","text": SEARCH_USER_PROMPT}, {"type":"image_url","image_url":{"url": <data_uri>}}]}], "stream": false}` | 同上 |

统一约束：请求体只用 `data_uri` 传图（不依赖图床）；超时 = 该源生效超时；HTTP 非 2xx → `ok=False`、`error` 取响应体前 200 字（脱敏 key）；解析不出文本 → `miss` 判定走 §3.3。`provider` 形态解析出的 `api_key` 在日志中只允许前 4 位 + `***`。

---

## 5. 失败与降级矩阵（冻结）

| 场景 | 触发条件 | 用户可见输出（工具返回） | 日志行为 |
|---|---|---|---|
| **源不可用（配置非法）** | vision 源 `provider_ref`/`api_base`+`model` 均空；engine 源 `engine` 非法 | 该源仅出现在 `attempts`，`outcome="skipped"`、`error="invalid_source"`；其余源照常执行 | 启动期 `WARNING`（英文，含 id 与原因）；运行期 `DEBUG` |
| **源不可用（运行时失败）** | 网络错误 / HTTP 4xx、5xx / 解析异常 / `provider_not_found` | 切下一个源；`attempts` 记 `outcome="error"` + `error` 人话；**不计轮数**。有其它源命中 → `status` 正常 | `WARNING`：`source <id> failed: <原因>` |
| **全部源失败** | **至少有一次实际尝试发生**，且全部尝试 `outcome ∈ {error, timeout, challenge}`（可同时存在被跳过的源，见 P10 第 3 条） | `ok=false`、`status="failed"`；`summary="图 1：所有搜图源都失败了（<最先失败原因>）。"`；`hint` 给出配置/网络排查建议 | `WARNING` 汇总每源原因；`health_stats=true` 时计入统计 |
| **全部源未命中（miss）** | 至少一个源 `miss`，无 `ok` | `ok=false`、`status="failed"`；`summary="图 1：没有查到这张图的来源。"`（明确区别于"失败"） | `INFO`：`no match for image <index>` |
| **空池 / 无源可试** | **该图没有任何实际尝试发生**：候选集合为空，或候选在真正发起前就被全部跳过（三种跳过原因：`invalid_source` 配置非法 / `no_public_url` 图床未开 / **`image_too_large_for_source` 超出该源体积或像素上限**，见 P13-a） | `ok=false`、`status="empty_pool"`；**`summary` 恒为唯一字面 `"当前没有可用的搜图源。"`**（P13-b：同一 status 只允许一个 summary 字面，差异一律只体现在 `hint`）；`hint` 按跳过原因给（可并列多句）：① 配置里确实没有可用源 → `"请在插件配置 search_sources 添加 vision 或 engine 源。"`；② 仅因图床未开导致 engine 源全被跳过 → `"请开启 image_url.enabled（图搜图引擎需要图片公网 URL）。"`；③ **仅因超出源级上限被跳过 → 提示「该图超出某源的体积/像素上限，请调大该源的 `max_size_mb` / `max_pixels`（或全局值）后重试」**；**不发起任何请求** | `WARNING` 一次（英文），提示 `search_sources` / `image_url.enabled` / 源级上限 |
| **图片不可读** | 引用无法解析/文件不存在/下载失败/魔数非图片/超体积且无法压缩/**SSRF 被拒** | 该图 `ok=false`、`error ∈ {bad_ref, download_failed, image_too_large, ssrf_blocked}`（**P9**：与 §3.1 实际取值一致）；其余图照常；全部图不可读 → `status="bad_refs"` | `WARNING`：`image <index> unreadable: <原因>` |
| **图片无法用于引擎** | 图床未启用 / 发布失败（`url` 为 `None`） | engine 源全部 `attempts.outcome="skipped"`、`error="no_public_url"`；若仍有 vision 源可用（`auto`）→ 正常命中；**纯 `engine` 请求 → 归上面的「空池 / 无源可试」行**（`status="empty_pool"`，无实际尝试，`hint` 提示开启 `image_url.enabled`），**不是 `status="failed"`** | `INFO`（图床关闭只提示一次） |
| **源返回挑战页** | 命中 §4.4 任一规则 | 该源 `attempts.outcome="challenge"`、`challenge=true`；切下一个源；**当该图存在 challenge 尝试、且没有任何 `ok` / `error` / `timeout`** → `status="failed"`，`summary` 取「全部源失败」行的文案，`hint="目标站点需人机校验，请配置自建反代 base_url 或提供 api_key。"`（**P8**：与 §1.4 `hint` 行同一口径，覆盖 challenge 与 challenge+skipped 混合）；**不解析挑战页** | `WARNING`：`source <id> blocked by challenge (Cloudflare)`，每源每运行期最多 3 条，避免刷屏 |
| **超时（单源）** | 单源超过生效超时 | 该源 `attempts.outcome="timeout"`；**不计轮数**；切下一个源 | `WARNING`：`source <id> timeout after <n>s` |
| **超时（总预算）** | 超过 `total_timeout_sec` | 停止剩余源，返回已有结果；`notes` 追加 `total_timeout`；若无结果 → `status="failed"`，**`summary` 必须与人话区分于空池**（形如「图 1：搜图在总预算 <n>s 内未完成，已尝试 <k> 个源但都没拿到结果。」），**不得**输出「没有可用的搜图源」那句空池文案 | `WARNING`：`total timeout budget exceeded` |
| **缓存命中** | 缓存键命中且未过期 | 直接返回缓存对象（`cached=true`，逐图 `cached` 也为 `true`），**零网络请求** | `DEBUG`：`cache hit for image <index>` |
| **工具参数缺失** | `image_refs` 缺失 / 空 | `ok=false`、`status="no_refs"`；`summary="请把图片路径或图片链接一起发给我。"` | `INFO` 一行 |
| **内部未预期异常** | 任意未捕获异常 | 捕获后返回 `ok=false`、`status="failed"`、`summary="搜图过程中出现内部错误。"`、`hint` 含异常类型（不含堆栈与密钥） | `ERROR`（含 `traceback.format_exc()`，只进日志） |

**P3 + P10 兜底归类（冻结终态，按「有无实际尝试」切分，取代此前的重叠描述）**：某图的所有候选都没拿到结果时，先看**有没有真的发起过尝试**，再决定归哪一行——「没有可用的搜图源」这句人话**只允许出现在真的没得试的时候**：

1. **没有任何实际尝试发生**（候选为空，或候选在发起前就全被 `invalid_source` / `no_public_url` / `image_too_large_for_source` 跳过）→ 归「**空池 / 无源可试**」行：`status="empty_pool"`，无任何网络请求；`summary` 恒为 `"当前没有可用的搜图源。"`，具体原因只放进 `hint`（P13-a/P13-b）。
2. **有实际尝试且全失败**（`error` / `timeout` / `challenge` 至少其一）→ 归「**全部源失败**」行：`status="failed"`，`summary="图 1：所有搜图源都失败了（<最先失败原因>）。"`。若此前还有被跳过的源，**逐源跳过明细追加进 `notes`**，格式 `skipped:<id>:<error>`。
3. **混合**（既有 `skipped` 又有真实失败）→ 按第 2 条归「全部源失败」（`skipped` 明细同样进 `notes`），**不得**因为存在 skipped 就判成 `empty_pool`。

注：总预算耗尽不参与上述三分类，它走「超时（总预算）」行自己的 `notes=total_timeout` + 专用 summary（**不得**回落到第 1 条的空池文案）。

---

## 6. v1.7 → v2.0 迁移映射表

### 6.1 文件与路径（冻结）

| 类别 | 路径 | 说明 |
|---|---|---|
| **旧配置（迁移输入）** | `data/config/astrbot_plugin_vision_router_config.json` | AstrBot 按 `{插件目录名}_config.json` 落盘的旧插件配置。**可能带 UTF-8 BOM，必须用 `encoding="utf-8-sig"` 读取。** |
| **旧插件内部历史路径（不迁移）** | `data/plugins/astrbot_plugin_vision_router/data/cmd_config.json` | v1.x provider 兜底直读常量路径（内容为主配置副本，带 BOM）。v2.0 **不读取、不迁移、不改动**。 |
| **新配置（迁移输出）** | `data/config/astrbot_plugin_image_search_config.json` | 新插件配置，schema 见 §2。 |
| **迁移报告（产物）** | `data/config/astrbot_plugin_image_search_migration_report.json` | 结构化迁移说明（保留字段/丢弃字段/丢弃理由/密钥脱敏）。 |
| **迁移脚本** | `data/plugins/astrbot_plugin_image_search/migrate_v1_7.py` | CLI 见 §6.4。 |
| **新插件目录** | `data/plugins/astrbot_plugin_image_search/` | `main.py` / `search_core.py` / `search_engines.py` / `_conf_schema.json` / `metadata.yaml` / `README.md` / `PRD.md`（本文件）。 |

### 6.2 字段映射（冻结）

| 旧键（v1.7） | 新键（v2.0） | 动作 | 说明 |
|---|---|---|---|
| `route.llm_provider` | `search_sources[0]`（`__template_key="vision"`） | **保留** | 生成一条 vision 源：`__template_key="vision"`、`enabled=true`、`id="migrated_route_llm"`、`name="迁移：v1.7 路由提供商"`、`provider_ref=<原值>`、`web_search=true`、`timeout_sec=0`。理由：v1.7 的 `mode="search"` 正是把原图交给该提供商联网搜图，语义与新插件 vision 源 1:1 对齐。**F2：`__template_key` 必须写入**（engine 源取 `"engine"`）——面板校验读 `item.get("__template_key") or item.get("template")`，缺失即报「缺少模板选择，需要 __template_key」（`astrbot/dashboard/services/config_service.py`），迁移产物将无法在面板保存。 |
| `route.max_rounds` | `search.max_rounds` | 保留 | 数值直搬；越界（>10）截断为 10 并记 `notes`。 |
| `route.max_size_mb` | `search.max_size_mb` | 保留 | 数值直搬。 |
| `route.max_pixels` | `search.max_pixels` | 保留 | 数值直搬。 |
| `route.cache_ttl_sec` | `search.cache_ttl_sec` | 保留 | 数值直搬。 |
| `route.health_stats` | `search.health_stats` | 保留 | 布尔直搬。 |
| `route.routing_timeout_sec` | — | **丢弃** | 该键服务于 analyze 的**关键词预路由**（§0 边界① 已删除预路由），新插件无路由 LLM 决策环节，无可映射语义。 |
| `ais`（整节） | — | **丢弃** | 见 §6.3 分类策略。 |
| `image_url.enabled` | `image_url.enabled` | 保留 | 同名同义直搬。 |
| `image_url.dir` | `image_url.dir` | 保留 | 同名直搬；空值保持空（运行期解析为新插件 plugin_data 目录）。 |
| `image_url.port` | `image_url.port` | 保留 | 数值直搬。 |
| `image_url.public_base_url` | `image_url.public_base_url` | 保留 | 字符串直搬；末尾斜杠去除。 |
| `download_timeout_sec` | `download_timeout_sec` | 保留 | 数值直搬。 |
| `tool_hint_enabled` | `tool_hint_enabled` | 保留 | 布尔直搬；提示语文字从 `image_analyze` 改为 `image_search`。 |
| `log_level` | `log_level` | 保留 | 字符串直搬；非法值回落 `info`。 |
| — | `search.source_timeout_sec` | **新增** | 默认 `30`（v1.7 无对应键；旧语义的逐 AI `timeout_sec` 随 `ais` 一并丢弃）。 |
| — | `search.total_timeout_sec` | **新增** | 默认 `180`。 |
| — | `search.merge_enabled` | **新增** | 默认 `true`（v1.7 的 search 模式为单模型直判，无合并概念）。 |
| — | `search.merge_max_sources` | **新增** | 默认 `3`。 |

**F3 配置保真（冻结，线上升级路径必须遵守）**：目标配置（`data/config/astrbot_plugin_image_search_config.json`）**可能已被 AstrBot 按 schema 预生成**（每个键的值恰好等于 schema 默认值），此时**不得**用这些默认值覆盖 v1.7 的映射值——否则用户的图床目录/公网前缀会被静默打成空串。逐键判定原则（只对 v2.0 已有键生效）：

- **目标键缺失**，或**其现值仍等于该键的 schema 默认值** → **写入 v1.7 映射值**；
- **用户已改成非默认值** → **保留用户的**，绝不回退。

适用时机：**首次迁移**（该输出路径还没有迁移报告）适用上面两条；**已有迁移报告的重跑**一律保留目标现值（避免把用户刻意设成默认值的键又改回去）。被取回的键在迁移报告 `notes` 里以 `<键>_restored_from_v1_7` 记录，被保留的用户值记在 `kept_existing`。

以本机现实配置为例（旧 v1.7 值：`image_url.enabled=false`、`dir=/root/AstrBot/data/imgserv/files`、`port=8123`、`public_base_url=http://159.75.175.217:8123`；预生成的新配置里 `dir` 与 `public_base_url` 均为 `""`）：迁移后必须是 `image_url.dir=/root/AstrBot/data/imgserv/files`、`image_url.public_base_url=http://159.75.175.217:8123`。

### 6.3 `ais` 池条目迁移策略（冻结）

**总策略：`ais` 整节丢弃，不生成任何 vision 源**（唯一例外是 §6.2 中由 `route.llm_provider` 生成的那一条）。逐类策略：

| 旧条目类别 | 策略 | 理由（必须写进迁移报告） |
|---|---|---|
| `__template_key == "provider"`（AstrBot 提供商引用条目，可能多条） | **丢弃**（快照进迁移报告） | ① 其字段 `desc` / `tags` / `vision_mode` / `reasoning_effort` / `max_retry` / `thumbnail` 全部服务于 analyze 的「按标签预路由 + 换 AI」语义，新插件不存在这些字段的目标位置，强行映射等于把 analyze 语义夹带进来（违反边界①）；② 池内多数条目（如 Gemini flash、sensenova、mimo）并不具备联网搜图能力，自动搬进 vision 池会让 `image_search` 用非联网模型冒充搜图源，属静默降质；③ 其中真正承担联网搜图的提供商已由 `route.llm_provider` 单条覆盖，不重复产出（若某条目 `provider_ref` 恰等于 `route.llm_provider`，只保留 §6.2 那一条，迁移报告记 `dedup_with_route_llm_provider`）。 |
| `__template_key == "custom"`（自定义 OpenAI 兼容端点条目） | **丢弃**（快照进迁移报告，`api_key` 只记前 4 位 + `***`） | ① 字段语义是「直连端点做识图」，无 `web_search` 能力标识，迁移后无法判断该端点是否能联网检索；② 该端点的 `api_base`/`api_key` 属于旧池的成本与配额，vision 源会被**真实调用并产生费用**，静默迁移属越权变更用户成本预期；③ 需要它的用户按 `vision` 模板手工重建（`api_base` + `api_key` + `model` + `web_search` 四填即可），迁移报告已给出快照与操作指引。 |
| `ais` 缺失 / 非 list / 含非 dict 元素 | 丢弃并告警 | 配置本身非法。 |

迁移报告必须为每个被丢弃条目记录：`{"index": n, "template_key": "provider"|"custom", "id": str, "enabled": bool, "reason": "<上表理由编号+一句说明>", "snapshot": {...脱敏后的原字段...}}`。

### 6.4 迁移脚本 CLI 与产物（冻结）

```
python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py \
    [--config <旧配置路径，默认 data/config/astrbot_plugin_vision_router_config.json>] \
    [--out <新配置路径，默认 data/config/astrbot_plugin_image_search_config.json>] \
    [--dry-run]
```

| 行为 | 约定 |
|---|---|
| 输入不存在 | 打印目标路径到 stderr，**退出码 1**，不写任何文件。 |
| 输入 JSON 非法 | 打印 `json` 错误到 stderr，退出码 1，不写任何文件。 |
| 输出已存在 | 先备份为 `<out>.bak.<YYYYmmddHHMMSS>`，再写新文件。 |
| `--dry-run` | 只打印将生成的配置（JSON，密钥脱敏）与迁移报告摘要，**不写文件**，退出码 0。 |
| 成功 | 写新配置 + 写迁移报告，打印摘要（保留 N 条 / 丢弃 M 条 / 新增 K 键），退出码 0。 |
| 幂等性 | 对同一输入重复执行产出**字节一致**的新配置（键序固定，不写时间戳进配置本体；时间戳只进备份文件名与报告）。 |
| 自动迁移 | **不做**。插件 `initialize()` 检测到新配置缺失且旧配置存在时，只打一条 `WARNING`（英文）提示运行本脚本，并在返回体 `notes` 场景不触发（仅日志）。 |

### 6.5 版本与文档收口（由 migrate-docsmith 执行，本 PRD 冻结要求）

- `data/plugins/astrbot_plugin_image_search/metadata.yaml`：`version: 2.0.0`、`name`/`desc` 改为搜图定位（desc 不得再出现"识图/analyze"）。
- `README.md`：首节写 v2.0.0 变更（删除 analyze 池、只保留搜图）、配置示例（vision/engine 各一条）、迁移命令、SauceNAO/ascii2d 的 Cloudflare 限制与 `base_url` 反代方案。
- 旧插件目录 `data/plugins/astrbot_plugin_vision_router/`：**零改动**（不在本轮交付范围；不删除、不修改）。

---

## 7. 本轮不做项（明确排除，测试与评审不得据此判失败）

1. **关键词纯文本搜索 API**（如 Tavily/博查/Bing 文本搜索）：不做，新插件只有图搜图 + 视觉联网模型两条路。
2. **Google Lens / TinEye 适配**：不做（本沙箱 `google.com`、`lens.google.com` 网络不可达，且用户裁决排除）。
3. **analyze 识图能力**：不做（识图/转述/OCR/图表提取一律交主模型；插件内不得留任何识图提示词或代码路径）。
4. **WebUI 前端改动**：不做（不改 `dashboard/`，不新增前端组件；配置只通过 `_conf_schema.json` 呈现）。
5. **git 推送与服务重启**：不做（只本地实现与验证，不 `git push`，不重启 AstrBot 服务，不做 git tag/commit 之外的发布动作）。
6. 旧插件目录的删除/归档：不做（`astrbot_plugin_vision_router/` 保持零改动）。

---

## 8. 环境约束（实测事实，成员按此设计，不当意外）

以下为 2026-10-06 在本沙箱（`/root/AstrBot`，`curl --max-time 10`）实测结果，**不是推测**：

| 目标 | 实测命令 | 实测结果 | 设计含义 |
|---|---|---|---|
| **trace.moe 可达** | `curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://api.trace.moe/search?anilistInfo=true&url=https%3A%2F%2Fexample.com%2Fx.jpg"` | `404`（HTTP 层可达），响应体 `{"error":"Failed to fetch image https://example.com/x.jpg"}` | trace.moe 可做**网络态**测试；且该响应正是 §4.3「含 `error` 键 → `ok=False` 带原文错误、不抛异常」的天然样例。 |
| **saucenao.com 被 Cloudflare 拦** | `curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://saucenao.com/search.php?output_type=2&numres=1&url=..."` | `403`，响应体为 `<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>...` | SauceNAO **不做网络态结果断言**；网络态测试允许「`challenge=True` 即视为契约成立」；解析逻辑用离线 fixture（§4.6）验证。 |
| **ascii2d.net 被 Cloudflare 拦** | `curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST -d "uri=https://example.com/x.jpg" https://ascii2d.net/search/uri` | `403`，响应体同为 `Just a moment...` 挑战页 | 同 SauceNAO：网络态只断言 `challenge=True` + 人话错误 + 不解析挑战页；解析用离线 fixture。 |
| **google.com 不可达** | `curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://www.google.com/` | `000`（连接失败） | Google Lens 相关适配本轮不做（§7.2），且不得写任何依赖 google 的用例。 |
| **lens.google.com 不可达** | `curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://lens.google.com/` | `000`（连接失败） | 同上。 |

由此冻结的验证口径：

- **网络态验收**：仅 trace.moe 允许断言真实响应；SauceNAO / ascii2d 允许断言 `challenge=True`（视为契约成立）。
- **离线验收**：三个引擎的解析、挑战识别、错误映射必须能在**无网络**下用 §4.6 fixture 全绿。
- 任何测试**不得**依赖 google / lens.google 网络可达。

---

## 9. 验收自检（本 PRD 自身与后续任务的核对清单）

本文件应满足：

```bash
test -s data/plugins/astrbot_plugin_image_search/PRD.md
grep -c 'image_search' data/plugins/astrbot_plugin_image_search/PRD.md      # ≥1
grep -c 'run_engine'   data/plugins/astrbot_plugin_image_search/PRD.md      # ≥1
grep -c 'challenge'    data/plugins/astrbot_plugin_image_search/PRD.md      # ≥1
grep -c '迁移'          data/plugins/astrbot_plugin_image_search/PRD.md      # ≥1
git status --porcelain data/plugins/astrbot_plugin_vision_router/ | wc -l   # 0（旧目录零改动）
```

后续任务对本文件的核对点：

| 任务 | 核对点 |
|---|---|
| core-smith | §1 工具形状、§2 schema 全部键与默认值、§3 流水线与 `max_rounds` 语义、§5 矩阵逐行可触发 |
| engine-smith | §4.1 返回 dict 逐键、§4.3 三引擎端点与映射、§4.4 挑战规则、§4.6 fixture 齐全、离线单测全绿 |
| migrate-docsmith | §6.1 文件名、§6.2 字段映射、§6.3 丢弃理由、§6.4 CLI 与退出码、§6.5 版本 `2.0.0` |
| verifier | §8 验证口径（trace.moe 网络态；SauceNAO/ascii2d 仅 `challenge=True`；解析用 fixture） |
| reviewer | 无含糊留白条款（键名/类型/默认值处处给定）；三条硬边界未被放宽；§7 排除项未被偷偷实现 |

**文档结束（v2.0.0 frozen）。**
