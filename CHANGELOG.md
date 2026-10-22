# Changelog

本库自 v2.0.0 基线建库（2026-10-23 从生产安装副本逐字节取件，无更早的 git 历史；
v1.x 代码保留在生产树 `data/plugins/astrbot_plugin_vision_router/` 冻结副本中，未入库）。

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
- 配置热更新；`migrate_v1_7.py` 提供 v1.7 → v2.0 迁移（旧 `route.llm_provider`
  → 一条 vision 源；旧 ais 识图池整节丢弃并快照进迁移报告），步骤见 MIGRATION.md。
