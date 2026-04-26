"""Per-task-type prompt recommendations.

Single source of truth for "should this prompt use a chat (thinking) model
or an embedding model, and if chat, at what tier?" rendered by the /prompts
UI next to each prompt so the operator knows which endpoint to bind.

Design notes:
- kind: "chat" means a text-generation / reasoning LLM endpoint. "embedding"
  means a vector embedding endpoint (e.g. NVIDIA NV-Embed / bge).
- tier: only meaningful when kind == "chat". Values match the LLM tier enum
  used by ModelRouter/endpoints: flagship | standard | small | distill.
  For kind == "embedding" we echo tier = "embedding" for symmetric display.
- reason: short Chinese explanation shown in the UI tooltip.

When you add a new task_type anywhere in prompt_registry.py, add a matching
row here. get_recommendation() falls back to a safe default if unknown.
"""

from __future__ import annotations

from typing import TypedDict


class Recommendation(TypedDict):
    kind: str  # "chat" | "embedding"
    tier: str  # "flagship" | "standard" | "small" | "distill" | "embedding"
    reason: str


# Mapping of task_type -> Recommendation.
# Keep tiers conservative: prefer small/standard unless the task truly needs
# deep reasoning or long-form generation.
TASK_TYPE_RECOMMENDATIONS: dict[str, Recommendation] = {
    # ---------- Core long-form generation / deep reasoning (flagship) ----------
    "outline_book": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "全书大纲需要长上下文与深度规划，建议旗舰思考模型",
    },
    "outline_volume": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "分卷大纲需整合全书脉络推演，建议旗舰",
    },
    "outline_chapter": {
        "kind": "chat",
        "tier": "standard",
        "reason": "章节大纲体量中等，standard 足够",
    },
    "generation": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "小说正文是质量核心，建议用旗舰模型",
    },
    "scene_writer": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "逐场景流式写作是质量核心，建议旗舰",
    },

    # ---------- Mid-weight text ops (standard) ----------
    "polishing": {
        "kind": "chat",
        "tier": "standard",
        "reason": "润色任务以文字表达为主，standard 平衡质量与成本",
    },
    "rewrite": {
        "kind": "chat",
        "tier": "standard",
        "reason": "改写以保持原意为主，standard 即可",
    },
    "evaluation": {
        "kind": "chat",
        "tier": "standard",
        "reason": "质量评估是综合型任务，standard 即可",
    },
    "beat_extraction": {
        "kind": "chat",
        "tier": "standard",
        "reason": "情节骨架抽取需要一定理解力",
    },
    "critic": {
        "kind": "chat",
        "tier": "standard",
        "reason": "综合审校用 standard 性价比最高",
    },
    "critic_soft": {
        "kind": "chat",
        "tier": "standard",
        "reason": "软指标批评，standard 即可",
    },
    "team_writer": {
        "kind": "chat",
        "tier": "standard",
        "reason": "写作型 agent，standard 平衡",
    },
    "team_editor": {
        "kind": "chat",
        "tier": "standard",
        "reason": "编辑型 agent，standard 平衡",
    },
    "scene_planner": {
        "kind": "chat",
        "tier": "standard",
        "reason": "场景拆分输出结构化 JSON，standard 足够",
    },

    # ---------- Deep reasoning / hard checks (flagship) ----------
    "critic_hard": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "硬伤审查要最严格的逻辑判断，建议旗舰",
    },
    "consistency_llm_check": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "一致性判官需深度推理",
    },
    "team_planner": {
        "kind": "chat",
        "tier": "flagship",
        "reason": "规划型 agent 需推理深度",
    },

    # ---------- Lightweight structured ops (small) ----------
    "summary": {
        "kind": "chat",
        "tier": "small",
        "reason": "摘要输出短，small 足够，成本/延迟优先",
    },
    "extraction": {
        "kind": "chat",
        "tier": "small",
        "reason": "实体抽取结构化简单",
    },
    "characters_extraction": {
        "kind": "chat",
        "tier": "small",
        "reason": "人物名单抽取",
    },
    "world_rules_extraction": {
        "kind": "chat",
        "tier": "small",
        "reason": "设定条目抽取",
    },
    "relationships_extraction": {
        "kind": "chat",
        "tier": "small",
        "reason": "关系边抽取",
    },
    "style_abstraction": {
        "kind": "chat",
        "tier": "small",
        "reason": "风格特征抽象",
    },
    "redaction": {
        "kind": "chat",
        "tier": "small",
        "reason": "脱敏替换任务，small 即可",
    },
    "compact": {
        "kind": "chat",
        "tier": "small",
        "reason": "记忆压缩，速度优先",
    },
    "rag_query_rewrite": {
        "kind": "chat",
        "tier": "small",
        "reason": "检索查询改写，轻量低延迟",
    },

    # ---------- Vector embedding ----------
    "embedding": {
        "kind": "embedding",
        "tier": "embedding",
        "reason": "向量检索专用，必须使用 embedding 端点",
    },
}


_DEFAULT: Recommendation = {
    "kind": "chat",
    "tier": "standard",
    "reason": "未给出建议的 task_type，默认用 standard 思考模型",
}


def get_recommendation(task_type: str) -> Recommendation:
    """Return the recommended endpoint kind/tier for a given task_type.

    Falls back to a safe standard-tier chat recommendation for unknown
    task_types so the UI never shows nothing.
    """
    rec = TASK_TYPE_RECOMMENDATIONS.get(task_type)
    if rec is None:
        return dict(_DEFAULT)  # type: ignore[return-value]
    return dict(rec)  # type: ignore[return-value]
