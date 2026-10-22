# v1.7 → v2.0 迁移指南（astrbot_plugin_vision_router → astrbot_plugin_image_search）

> v2.0.0 是**破坏性重构**：插件从「识图路由」改成了「纯搜图」。
> 配置键、工具名、返回结构都变了，所以**必须迁移一次配置**，不是可选动作。
>
> **ais 识图池已被彻底移除。** 旧配置里的 `ais` 整节（含多 AI 池、标签预路由所需字段、
> 质量换 AI 相关字段）在 v2.0 **没有任何对应能力**，也不会被搬进新配置——它只会被
> 快照进迁移报告供你回查。删掉它是这次重构的目的本身，不是副作用。
> 旧的 `route.routing_timeout_sec`（关键词预路由超时）同理丢弃：v2.0 没有路由 LLM
> 决策环节，这个键没有可映射的语义。

---

## 1. 迁移四步

### 第 0 步：备份旧配置

```bash
cd /root/AstrBot
cp data/config/astrbot_plugin_vision_router_config.json \
   data/config/astrbot_plugin_vision_router_config.json.bak.$(date +%Y%m%d%H%M%S)
```

旧插件目录 `data/plugins/astrbot_plugin_vision_router/` **本轮零改动**：不删除、不修改，
它的内部 `data/cmd_config.json`（v1.x 的 provider 兜底直读副本）v2.0 **不读、不迁移、不动**。

### 第 1 步：跑迁移脚本

先干跑看 diff（**不写任何文件**，密钥已脱敏）：

```bash
cd /root/AstrBot
python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py --dry-run
```

确认无误后正式迁移：

```bash
python data/plugins/astrbot_plugin_image_search/migrate_v1_7.py
```

> ⚠️ **如果新插件已经安装并启动过**（AstrBot 会按 schema 预生成新配置、`search_sources` 是空数组），请先读 **第 2 节「升级已装插件的配置」**——脚本对这种情况有专门的保真规则，不会把旧 `image_url` 值清成默认值。

产物两个：

| 产物 | 默认路径 |
|---|---|
| 新插件配置 | `data/config/astrbot_plugin_image_search_config.json` |
| 迁移报告 | `data/config/astrbot_plugin_image_search_migration_report.json` |

脚本 CLI（PRD §6.4 的 `--config` / `--out` 与任务契约的 `--old` / `--new` 是同一组别名的两种写法，用哪个都行）：

```bash
python migrate_v1_7.py [--old 旧配置路径] [--new 新配置路径] [--report 报告路径] [--dry-run]
# 等价写法（PRD §6.4）：
python migrate_v1_7.py [--config 旧配置路径] [--out 新配置路径] [--dry-run]
```

行为约定（退出码对齐 PRD §6.4 冻结基准）：

| 情况 | 行为 |
|---|---|
| 旧配置文件不存在 | 把**旧路径与指引文案打印到 stderr**，**退出码 1**（PRD §6.4：输入不存在属失败，脚本不改任何东西），不写任何文件。⚠️ 部署从未用过旧插件时这属正常「无需迁移」，但退出码仍是 1 —— 自动化里请按退出码判定，不要靠解析文案 |
| 旧配置 JSON 非法 | 打印 `json` 错误到 **stderr**，**退出码 1**，不写任何文件 |
| 成功 | 写新配置 + 写迁移报告，**退出码 0** |
| 输出文件已存在 | 先备份为 `<新配置名>.bak.<YYYYmmddHHMMSS>` 再写 |
| `--dry-run` | 只打印摘要 + 脱敏 diff，**不写任何文件**，**退出码 0**（输入本身不合法/不存在时仍为 1） |
| 重复执行 | **幂等**：不产生重复源条目（按 `id` 去重）。迁移报告是「这一轮到底追加过什么」的持久记录：只有上一轮确实追加过迁移源时，重跑才跳过追加 —— 所以你手工删掉的那条不会被重新塞回来；反过来，AstrBot 预先按 schema 生成的默认配置（空池、还没有报告）**不会**阻断迁移。已存在的新键与自加的源一律不覆盖，缺失键按 schema 默认值补齐 |

> 退出码速查：**0** = 成功 / dry-run（输入合法）；**1** = 输入不存在 / 输入 JSON 非法 / 写入失败。
> 失败路径的错误与指引文案一律走 **stderr**，stdout 只承载正常流程输出。

### 第 2 步：在 WebUI 核对 `search_sources`

打开 AstrBot WebUI → 插件配置 → **搜图路由 Agent**，重点核这几项：

1. **`search_sources` 里应当有且只有一条迁移来的 vision 源**：`id` = `migrated_route_llm`，
   `provider_ref` = 你 v1.7 配的 `route.llm_provider`。旧 `ais` 池里的其它条目**不会**出现
   ——这是预期行为（见下面第 4 节）。
2. 想补第二个源就直接在 `search_sources` 里加：图搜图引擎建议一并配上（见
   [README.md](README.md) §4.4），注意 **engine 源需要先打开 `image_url.enabled`**，否则它们
   会被逐条跳过（`attempts` 记 `skipped` / `no_public_url`）。
3. 如果旧配置里真正承担联网搜图的是 `ais` 池中的某个 provider 条目、而不是
   `route.llm_provider`，请按 `vision` 模板**手工重建**一条：填 `provider_ref` 即可
   （`api_base` / `api_key` / `model` 留空）。
   被丢弃条目的完整快照（`api_key` 只记前 4 位 + `***`）在迁移报告的 `dropped` 数组里可查。

### 第 3 步：重启生效

```bash
# 重启 AstrBot（或从 WebUI 重新加载插件）
uv run main.py
```

启动日志里应出现一行英文摘要：

```
image_search v2.0.0 activated: vision_sources=[...] engine_sources=[...] search_type_default=auto max_rounds=2 image_url=on/off
```

看到它就说明配置已被正确读取（`vision_sources` / `engine_sources` 应当和你在 WebUI 里配的一致；
`image_url=off` 时 B 类引擎源不工作）。

---

## 2. 升级已装插件的配置

**场景**：新插件目录已经就位、AstrBot 已经启动过一次——此时框架会**按 `_conf_schema.json` 预生成** `data/config/astrbot_plugin_image_search_config.json`，里面每个键都恰好等于 schema 默认值（`image_url.dir=""`、`public_base_url=""`、`cache_ttl_sec=300`…），`search_sources` 是空数组。这是线上升级最常见的状态，**不要**因为「配置文件已经存在」就跳过迁移。

迁移脚本对这种情况有明确的保真规则（**F3**，逐键判定）：

| 目标配置里的键 | 迁移动作 |
|---|---|
| **缺失** | 写入 v1.7 映射值 |
| **存在，但值仍等于 schema 默认值**（即 AstrBot 刚预生成的） | **写入 v1.7 映射值**，报告 `notes` 记 `<键>_restored_from_v1_7` |
| **存在，且用户已改成非默认值** | **保留用户的**，报告 `kept_existing` 记该键 |

因此本机这套配置（旧 v1.7 值：`image_url.dir=/root/AstrBot/data/imgserv/files`、`public_base_url=http://159.75.175.217:8123`）迁移后**不会**被静默清成空串，`search.cache_ttl_sec` 也会取回 v1.7 的 60 而不是 schema 默认的 300。该规则只在**该输出路径还没有迁移报告**时生效；已有报告的重跑一律以目标配置为准，避免把你刻意设回默认值的键又改回去。

### 2.1 `__template_key`：迁移产物必须带，否则面板存不进去

AstrBot 面板的 `template_list` 校验读的是 `item.get("__template_key") or item.get("template")`；两者都空就报 **「缺少模板选择 search_sources[n]: 需要 __template_key」**，在配置页保存会被挡下。因此迁移脚本产出的每条源都带模板标记（**F2**）：

| 源类型 | `__template_key` 取值 |
|---|---|
| A 类视觉联网模型（`provider_ref`，或 `api_base` + `model`） | `"vision"` |
| B 类图搜图引擎（SauceNAO / ascii2d / trace.moe） | `"engine"` |

迁移生成的那条源长这样（可直接在面板编辑并保存）：

```json
{
  "__template_key": "vision",
  "enabled": true,
  "id": "migrated_route_llm",
  "name": "迁移：v1.7 路由提供商",
  "provider_ref": "豆包/doubao",
  "web_search": true,
  "timeout_sec": 0,
  "max_size_mb": 0,
  "max_pixels": 0,
  "extra_headers": {}
}
```

手工往配置 JSON 里加源时也要照抄 `__template_key`——它不在 schema 字段表里（是面板用的辅助键），但**没有它这条目不会被面板接受**。

升级完请按第 5 节的自检清单核对一遍，再按第 1 节第 3 步重启。

---

## 3. 字段映射表

### 3.1 保留（语义 1:1 对齐）

| 旧键（v1.7） | 新键（v2.0） | 说明 |
|---|---|---|
| `route.llm_provider` | `search_sources[0]`（`__template_key="vision"`） | 生成 `{"__template_key": "vision", "enabled": true, "id": "migrated_route_llm", "name": "迁移：v1.7 路由提供商", "provider_ref": <原值>}`（**F2**：`__template_key` 必带，否则面板存不进去，见 §2.1）。v1.7 的 `mode="search"` 正是把原图交给该提供商联网搜图，与新插件 vision 源 1:1 对齐 |
| `route.max_rounds` | `search.max_rounds` | 数值直搬；越界（>10）截断为 10 并在报告记 `clamped` |
| `route.max_size_mb` | `search.max_size_mb` | 数值直搬 |
| `route.max_pixels` | `search.max_pixels` | 数值直搬 |
| `route.cache_ttl_sec` | `search.cache_ttl_sec` | 数值直搬 |
| `route.health_stats` | `search.health_stats` | 布尔直搬 |
| `image_url.enabled` | `image_url.enabled` | 同名同义直搬（**F3**：目标配置里若仍是 schema 默认 `false`，按 v1.7 值写入） |
| `image_url.dir` | `image_url.dir` | 同名直搬（**F3**：AstrBot 预生成的空串会被 v1.7 的实际目录替换；用户在 WebUI 改过的非默认值保留）。⚠️ 旧值可能指向旧插件目录，建议核对后在 WebUI 清空 |
| `image_url.port` | `image_url.port` | 数值直搬 |
| `image_url.public_base_url` | `image_url.public_base_url` | 字符串直搬，末尾斜杠去除（**F3**：预生成的空串会被 v1.7 的公网前缀替换） |
| `download_timeout_sec` | `download_timeout_sec` | 数值直搬 |
| `tool_hint_enabled` | `tool_hint_enabled` | 布尔直搬（提示文字从 `image_analyze` 改为 `image_search`） |
| `log_level` | `log_level` | 字符串直搬；非法值回落 `info` |

### 3.2 丢弃（含理由）

| 旧键（v1.7） | 动作 | 理由 |
|---|---|---|
| LLM 工具名 `image_analyze` | **改为** `image_search` | 工具语义完全变了（识图 → 搜图），参数与返回结构一并重写。主模型的提示词/人设里如果写过 `image_analyze`，记得同步改名 |
| `route.routing_timeout_sec` | **丢弃** | 服务于 analyze 的**关键词预路由**，v2.0 已删除预路由，无路由 LLM 决策环节，无可映射语义 |
| `ais`（整节） | **丢弃** | 见第 4 节 |
| `route.mode`（`search` / `analyze`）等 analyze 专属开关 | **丢弃** | v2.0 只有一个模式：搜图 |

### 3.3 新增（v1.7 无对应键，取 schema 默认值）

| 新键 | 默认 | 说明 |
|---|---|---|
| `search.source_timeout_sec` | `30` | 单源默认超时（5~300），被逐源 `timeout_sec` 覆盖 |
| `search.total_timeout_sec` | `180` | 单次调用总预算（30~900） |
| `search.merge_enabled` | `true` | v1.7 的 search 模式为单模型直判，没有「多源合并」概念 |
| `search.merge_max_sources` | `3` | 每图最多合并源数（1~5） |

另外，vision / engine 源模板里的 `web_search` / `max_size_mb` / `max_pixels` / `extra_headers`
等字段在迁移生成的那条源上取 schema 默认值（`web_search=true`、上限 `0` 表示沿用全局）。

---

## 4. 为什么 `ais` 池被整个丢弃

`ais` 池专门服务于 analyze 语义（按 `tags` 预路由选 AI、质量不满意换 AI、报错换 AI 直到成功），
它的每个字段都在那个机制里有位置、在 v2.0 里没有：

1. **字段没有落点**：`desc` / `tags` / `vision_mode` / `reasoning_effort` / `max_retry` / `thumbnail`
   全部是为「预路由 + 换 AI」服务的。新插件不存在这些字段的目标位置，硬映射等于把 analyze
   语义夹带进 v2.0——这正是本次重构要删掉的东西。
2. **会静默降质**：池内多数条目并不具备联网搜图能力。把它们自动搬进 vision 池，会让
   `image_search` 用「认不出训练集外角色」的模型冒充搜图源，用户问「这是谁」只会得到一段
   看似合理的猜测，而没有任何联网证据。
3. **会越权改成本**：`custom` 端点条目带着 `api_base` / `api_key`，属于旧池的成本与配额。
   搬进 vision 源后它会被**真实调用并产生费用**——不经用户确认就改变成本预期，不能做。

所以迁移策略是：**整节丢弃 + 快照留存 + 操作指引**。真正承担联网搜图的那一条提供商已由
`route.llm_provider` 覆盖，不重复产出；如果你确信某个被丢弃的条目仍然需要，按 `vision` 模板
手工重建（提供商引用填 `provider_ref`；自定义端点填 `api_base` + `api_key` + `model` + `web_search`），
重建所需的原始字段值都能在迁移报告的 `dropped[].snapshot` 里找到。

迁移报告结构（`data/config/astrbot_plugin_image_search_migration_report.json`）：

```jsonc
{
  "schema_version": 1,
  "from_version": "1.7",
  "to_version": "2.0.0",
  "kept": ["route.max_rounds -> search.max_rounds", "..."],
  "kept_existing": ["... 保留的既有 v2.0 键（幂等重跑时出现）..."],
  "dropped": [
    {"index": 0, "template_key": "provider", "id": "cc/xiaomi/mimo-v2.5", "enabled": false,
     "reason": "...丢弃理由...", "snapshot": {"...脱敏后的原字段...": "..."}}
  ],
  "dropped_keys": ["ais", "route.routing_timeout_sec"],
  "migrated_new_top_level_keys": ["search_sources", "search", "..."],
  "source_added": true,
  "source_count": 1,
  "notes": []
}
```

---

## 5. 迁移后的自检清单

```bash
cd /root/AstrBot

# 1) 新配置能被解析，且关键键都在
python -c "import json; d=json.load(open('data/config/astrbot_plugin_image_search_config.json',encoding='utf-8')); print(sorted(d.keys()))"
# 期望: ['download_timeout_sec', 'image_url', 'log_level', 'search', 'search_sources', 'tool_hint_enabled']

# 2) 迁移来的 vision 源在，且 id 唯一（重复执行不应变多）
python -c "import json; d=json.load(open('data/config/astrbot_plugin_image_search_config.json',encoding='utf-8')); print([s.get('id') for s in d['search_sources']])"

# 3) 旧配置里不该再被 v2.0 读取到 analyze 键
python -c "import json; d=json.load(open('data/config/astrbot_plugin_image_search_config.json',encoding='utf-8')); print('ais' in d, 'route' in d)"
# 期望: False False
```

然后按第 1 节第 3 步重启，并确认启动日志里那行 `image_search v2.0.0 activated: ...`。

---

## 6. 回滚

v1.7 配置**没有被修改**（脚本只读它），所以回滚很直接：

```bash
# 停掉 AstrBot，删掉（或改名）新插件配置与报告，即可回到旧插件的配置状态
mv data/config/astrbot_plugin_image_search_config.json{,.rolledback}
```

旧插件目录 `data/plugins/astrbot_plugin_vision_router/` 与旧配置
`data/config/astrbot_plugin_vision_router_config.json` 全程零改动，回滚不需要额外恢复动作。
