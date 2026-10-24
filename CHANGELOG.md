# Changelog

本库自 v2.0.0 基线建库（2026-10-23 从生产安装副本逐字节取件，无更早的 git 历史）。
v2.1.0 起在库内继续演进。

## 2.1.0 (2026-10-25)

### Added

- 工具新增可选参数 `intent`：主模型可把「怀疑的角色 / 作品 / 场景」交给 vision 源，
  用于缩小检索范围；超过 300 字截断并记 `intent_truncated`。返回体回显 `intent`。
- 缓存键纳入 `intent`（图片哈希 + JSON `[search_type, question, intent]`），并为缓存
  增加 256 条 LRU 上限，避免长时间运行内存无界增长。
- 仅在候选里存在 engine 源时才发布图片到图床，纯 vision 配置不再产生无用落盘文件。

### Removed

- 删除 v1.7 → v2.0 配置迁移脚本 `migrate_v1_7.py`、迁移文档 `MIGRATION.md`、
  启动期迁移提示与旧配置路径探测。
- 删除 v1.7 遗留的源字段 `desc` / `description` 透传；现在按未知键丢弃并告警。
- 删除过时的 v2.0.0 验证/迁移测试脚本，补充 schema、intent 与注册守卫。

### Changed

- README / PRD 重写到 v2.1.0，移除迁移章节与 v1.7 叙事；配置 schema 的旧版本提示同步清理。
- 版本号 2.0.0 → 2.1.0；激活日志版本号同步更新。

## 2.0.0 (2026-10-06)

破坏性重构（相对 v1.7）：

- **唯一 LLM 工具改为 `image_search(image_refs[], question, search_type)`**；
  彻底移除 analyze 识图池——ais 多 AI 池、关键词预路由、`mode` 双模式、
  质量不满意换 AI、路由 LLM 兜底直判全部删除，看图/转述/OCR 一律交主模型自身
  多模态能力。
- **源池改为「搜图源池」两类**：A 类视觉联网模型（AstrBot provider 引用，含自建
  反代，或自定义 OpenAI 兼容端点 `api_base + api_key + model`）+ B 类图搜图引擎
  （SauceNAO / ascii2d / trace.moe，只接图片公网 URL，需开启内置只读图床
  `image_url.enabled`）。`search_type=auto` 先跑全部 engine 再跑 vision。
- 逐源失败自动换源、单源超时隔离（error / timeout / challenge / skipped 均不计
  轮数），只有明确 `miss` 计轮（`search.max_rounds`，默认 2）；命中后按
  `merge_enabled` / `merge_max_sources` 多源合并去重（`merged_from` 逐源标注）。
- 图片流水线：引用归一化（本地路径 / file:// / http(s) / data URI / base64:// /
  `[Image Attachment: path ...]`）→ SSRF 防护（内网/环回/链路本地/保留段拒绝，
  重定向后二次校验）→ 体积/像素校验与等比压缩 → data URI 或图床 URL → 逐源尝试
  → 结构化 JSON 返回。两个硬保证：不抛异常给主模型、返回体不含 API Key。
- 配置热更新；v1.7 → v2.0 迁移脚本见 v2.0.0 历史（提交 `23dacda`）。
