"""prompts.py -- 搜图路由 Agent 提示词常量（v2.0.0）。

本模块只放常量，不含业务逻辑。

v2.0.0 收敛：**只保留搜图所需常量**。与 analyze 识图时代相关的常量已全部删除，
删除依据（PRD §0 边界①「彻底删除 analyze 识图池」+ §7.3「analyze 识图能力不做」）：

- `DEFAULT_IMAGE_PROMPT`（图像转述指令）：识图/转述一律交主模型多模态能力，
  插件内不得留任何转述提示词；
- `ROUTING_SYSTEM_PROMPT` / `ROUTING_USER_PROMPT`（关键词预路由决策 JSON）：
  预路由已被删除，新插件无路由 LLM 决策环节；
- `LOOP_SYSTEM_PROMPT`（Agent loop 换 AI 决策）：质量换 AI / 报错换 AI 已删除；
- `RESTATE_SYSTEM_PROMPT` / `RESTATE_USER_PROMPT`（用户问题转述）：转述服务于
  识图指令生成，新插件只做搜图，用户原话直传视觉源；
- `ROUTER_DIRECT_SYSTEM_PROMPT` / `DIRECT_FALLBACK_MARKER`（兜底直判）：
  兜底直判是硬边界① 明令删除的能力入口。

保留并冻结的搜图提示词（PRD §3.3「vision 源提示词（冻结，实现逐字使用）」）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# vision（A 类）源提示词（PRD §3.3 冻结，逐字使用）
# ---------------------------------------------------------------------------

SEARCH_SYSTEM_PROMPT: str = (
    "你是搜图路由 Agent 的联网搜图源。你只做一件事：识别图片主体的身份与来历并联网核实。\n"
    "禁止转述图片文字内容。"
)

# `question` 为空时写入 user 提示词的内置默认意图（PRD §3.3 逐字给定）
NO_QUESTION_PLACEHOLDER: str = "（未提供，按“这是谁 / 出自哪里”处理）"

SEARCH_USER_PROMPT: str = (
    "请联网检索并回答这张图片中主体的身份与来历。用户问题：{question}\n"
    "输出要求（严格遵守）：\n"
    "第一行必须是 VERDICT: HIT 或 VERDICT: MISS。"
    "HIT = 联网检索后给出了确定的身份/来源/出处；MISS = 无法确定。\n"
    "第二行起用中文简述：主体名称、作品/出处、角色或场景说明、可核对的来源链接（如有）。\n"
    "不要复述图片中的文字，不要输出与主体身份无关的内容。"
)

# question 为空时的等价完整提示词（供调用方直接使用，避免再次 format）
SEARCH_NO_QUESTION_PROMPT: str = SEARCH_USER_PROMPT.format(
    question=NO_QUESTION_PLACEHOLDER
)

# 源配置 `web_search=false`（该模型自带联网能力，不需要额外要求联网检索）时
# 追加的一行说明；`web_search=true` 时不追加，system 提示词保持 §3.3 原文。
SEARCH_WEB_SEARCH_OFF_SUFFIX: str = (
    "\n（本模型自带联网检索能力，请直接使用内置联网能力核实，无需额外提示。）"
)


def build_user_prompt(question: str) -> str:
    """按 PRD §3.3 组装 vision 源的 user 提示词（空问题走内置默认意图）。

    Args:
        question: 用户关于图片的原话；空串时使用内置默认意图。

    Returns:
        完整的 user 提示词文本（含 VERDICT 输出要求）。
    """
    return SEARCH_USER_PROMPT.format(
        question=(question or "").strip() or NO_QUESTION_PLACEHOLDER
    )
