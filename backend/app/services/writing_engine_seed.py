"""v0.8 — Writing engine seeds (incremental, idempotent).

On startup we ensure a small set of sensible defaults exists so that the
anti-AI scan, ContextPack v3 fourth recall, and Agent Tool Registry are
functional even on a fresh install. Users can edit/disable/delete via the
``/settings/writing-engine`` UI; seeds only insert rows whose unique key is
missing, never overwriting existing edits.

Uniqueness keys used here:
- genre_profiles : code
- tool_specs     : name
- writing_rules  : (genre, category, title)
- beat_patterns  : (genre, stage, title)
- anti_ai_traps  : (locale, pattern_type, pattern)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.writing_engine import (
    AntiAITrap,
    BeatPattern,
    GenreProfile,
    ToolSpec,
    WritingRule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_WRITING_RULES: list[dict[str, Any]] = [
    {
        "genre": "",
        "category": "pacing",
        "title": "每1500字至少一个小钙子",
        "rule_text": "每约 1500 字必须出现一个动作 / 冲突 / 信息揭示，避免长段写景主导的无油煩水。",
        "examples_json": [
            {"good": "他推开门，窗外的光照在地上留下另一个人的影子。"},
            {"bad": "他坐了很久。阳光照着。他又坐了很久。"},
        ],
        "priority": 80,
    },
    {
        "genre": "",
        "category": "dialogue",
        "title": "对话比调不超过7字",
        "rule_text": "尽量以 '谁说 + 动作/表情' 代替 '谁说 + 大段展开'；单行对话比调控制在 7 字以内。",
        "examples_json": [
            {"good": "\"不必。\"他摸了一下剑柄。"},
            {"bad": "他一边慢慢走到窗边，一边回忆着過去三十年的种种，然后轻轻地说：\"不必。\""},
        ],
        "priority": 70,
    },
    {
        "genre": "",
        "category": "hook",
        "title": "章末留钩",
        "rule_text": "每章末尾留下明确的未决问题（新敌对忽见 / 新信息出现 / 角色做出决定），至少三种之一。",
        "examples_json": [
            {"good": "他解开装入信笾的纸，上面只写了三个字。"},
            {"bad": "这一天终于安静地结束了。"},
        ],
        "priority": 90,
    },
    {
        "genre": "",
        "category": "description",
        "title": "阵户不替代人物",
        "rule_text": "设定 / 环境 / 宝物介绍不超过 3 行，人物的选择与反应才是主轴。",
        "examples_json": [
            {"bad": "这是一把剑，剑鋒是黄金制成的，剑穗是杜鹑毛，剑令是...(连续 10 行)"},
            {"good": "他看了一眼那把剑，并未伸手。"},
        ],
        "priority": 60,
    },
]

_BEAT_PATTERNS: list[dict[str, Any]] = [
    {
        "genre": "",
        "stage": "opening",
        "title": "开篇即冲突",
        "description": "前两百字内必须出现角色当前冲突或愿望，避免用大段背景介绍起篇。",
        "trigger_conditions_json": {"chapter_idx": {"lte": 3}},
    },
    {
        "genre": "",
        "stage": "turning",
        "title": "中部反转",
        "description": "卷中段需安排一次认知翻转：原本的盟友/故事/线索被重新定义。",
        "trigger_conditions_json": {"volume_progress": {"gte": 0.4, "lte": 0.6}},
    },
    {
        "genre": "",
        "stage": "climax",
        "title": "高潮三拍",
        "description": "冲突再升级 → 主角付出代价 → 跱式结果，三个心跳节奏一个不少。",
        "trigger_conditions_json": {"volume_progress": {"gte": 0.8}},
    },
    {
        "genre": "",
        "stage": "volume_end",
        "title": "卷末钩",
        "description": "本卷大冲突解决后，立刻给出下一卷的新敌或新谜团，留着读者往下看。",
        "trigger_conditions_json": {"volume_last_chapter": True},
    },
    {
        "genre": "",
        "stage": "closure",
        "title": "收尾闭环",
        "description": "最后一卷需闭合开篇埋下的三大伏笔中的至少两个，给读者回味感。",
        "trigger_conditions_json": {"is_final_volume": True},
    },
]

_ANTI_AI_TRAPS: list[dict[str, Any]] = [
    # Classic LLM Chinese stock phrases.
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "空气凝固", "severity": "hard",
     "replacement_hint": "改写为角色的物理动作或呼吸变化，如 '他挣卡的嘴角无法闭合'。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "霖的一下",      "severity": "hard",  "replacement_hint": "改为具体生理反应，如 '后颈发麻'。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "心情复杂",      "severity": "soft",  "replacement_hint": "用具体动作/语言展现波动，而非用 '复杂' 概括。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "不由得",          "severity": "soft",  "replacement_hint": "删除语气填充词，直接写动作。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "终于从回忆中回过神来", "severity": "hard",
     "replacement_hint": "删掉整句，回忆通过现在的动作打断而结束。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "价值观",          "severity": "soft",  "replacement_hint": "丛书叙事中不要直说 '价值观'，换成角色具体的立场。"},
    {"locale": "zh-CN", "pattern_type": "keyword", "pattern": "内心深处",      "severity": "soft",  "replacement_hint": "避免 '内心深处' 这种抽象说法，直接写心跳 / 呼吸 / 动作。"},
    # Common AI structural ticks.
    {"locale": "zh-CN", "pattern_type": "regex",   "pattern": r"他\s*的\s*眼\s*神\s*[变带]得\s*[复杂深邃难以捉摸]",
     "severity": "soft", "replacement_hint": "用具体的眼神描写替换。"},
    {"locale": "zh-CN", "pattern_type": "regex",   "pattern": r"一时间[，,].{0,8}[心禁心中心裡]",
     "severity": "soft", "replacement_hint": "用角色的真实行为取代规律性过渡词。"},
    {"locale": "zh-CN", "pattern_type": "ngram",   "pattern": "笑容满面|怒火中烧|汗如雨下|汗流浃背|心如止水",
     "severity": "soft", "replacement_hint": "四字成语堆砌感强，拆成具体动作或感受。"},
]

_GENRE_PROFILES: list[dict[str, Any]] = [
    {"code": "xianxia",  "name": "仙侠",   "description": "仙侠 / 修真题材默认配置。"},
    {"code": "urban",    "name": "都市",   "description": "现代都市 / 重生 / 财经题材默认配置。"},
    {"code": "scifi",    "name": "科幻",   "description": "软硬科幻默认配置。"},
    {"code": "mystery",  "name": "悬疑",   "description": "推理 / 悬疑题材默认配置。"},
]

_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_memory",
        "description": "根据查询语从 chapter_summaries + compacted 集合中检索语义最接近的记忆段。",
        "input_schema_json": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "project_id":  {"type": "string"},
                "top_k":       {"type": "integer", "default": 5},
            },
            "required": ["query", "project_id"],
        },
        "output_schema_json": {
            "type": "object",
            "properties": {
                "snippets": {"type": "array", "items": {"type": "string"}},
            },
        },
        "handler": "qdrant",
        "config_json": {"collections": ["chapter_summaries", "compacted"], "score_threshold": 0.4},
    },
    {
        "name": "check_character_fact",
        "description": "查询某角色当前的位置 / 实力等级 / 主要关系。",
        "input_schema_json": {
            "type": "object",
            "properties": {
                "character_name": {"type": "string"},
                "project_id":     {"type": "string"},
            },
            "required": ["character_name", "project_id"],
        },
        "output_schema_json": {
            "type": "object",
            "properties": {
                "location":      {"type": "string"},
                "power_level":   {"type": "string"},
                "relationships": {"type": "object"},
            },
        },
        "handler": "sql",
        "config_json": {"source_table": "characters"},
    },
    {
        "name": "lookup_relation",
        "description": "查询两个角色在指定卷的关系。",
        "input_schema_json": {
            "type": "object",
            "properties": {
                "character_a": {"type": "string"},
                "character_b": {"type": "string"},
                "volume_id":   {"type": "string"},
            },
            "required": ["character_a", "character_b"],
        },
        "output_schema_json": {
            "type": "object",
            "properties": {"rel_type": {"type": "string"}, "notes": {"type": "string"}},
        },
        "handler": "sql",
        "config_json": {"source_table": "relationships"},
    },
    {
        "name": "suggest_beat",
        "description": "根据章节进度建议下一个合适的 beat_pattern。",
        "input_schema_json": {
            "type": "object",
            "properties": {
                "project_id":       {"type": "string"},
                "chapter_progress": {"type": "number", "minimum": 0, "maximum": 1},
                "genre":            {"type": "string"},
            },
            "required": ["chapter_progress"],
        },
        "output_schema_json": {
            "type": "object",
            "properties": {
                "beat_title":       {"type": "string"},
                "beat_description": {"type": "string"},
            },
        },
        "handler": "sql",
        "config_json": {"source_table": "beat_patterns"},
    },
    {
        "name": "classify_rule_violation",
        "description": "给定一段文本，列出它命中的 writing_rules 与 anti_ai_traps。",
        "input_schema_json": {
            "type": "object",
            "properties": {
                "text":  {"type": "string"},
                "genre": {"type": "string"},
            },
            "required": ["text"],
        },
        "output_schema_json": {
            "type": "object",
            "properties": {
                "violated_rules": {"type": "array", "items": {"type": "string"}},
                "anti_ai_hits":   {"type": "array", "items": {"type": "string"}},
            },
        },
        "handler": "python_callable",
        "config_json": {"callable": "app.services.tool_registry.classify_rule_violation"},
    },
]


# ---------------------------------------------------------------------------
# Seed runner
# ---------------------------------------------------------------------------


async def seed_writing_engine(db: AsyncSession) -> dict[str, int]:
    """Incrementally insert missing defaults. Returns counts added per table."""
    added = {"writing_rules": 0, "beat_patterns": 0, "anti_ai_traps": 0, "genre_profiles": 0, "tool_specs": 0}

    # writing_rules: key = (genre, category, title)
    existing = await db.execute(select(WritingRule.genre, WritingRule.category, WritingRule.title))
    wr_keys = {(r[0] or "", r[1] or "", r[2] or "") for r in existing.all()}
    for row in _WRITING_RULES:
        key = (row["genre"], row["category"], row["title"])
        if key in wr_keys:
            continue
        db.add(WritingRule(id=uuid.uuid4(), **row))
        added["writing_rules"] += 1

    # beat_patterns: key = (genre, stage, title)
    existing = await db.execute(select(BeatPattern.genre, BeatPattern.stage, BeatPattern.title))
    bp_keys = {(r[0] or "", r[1] or "", r[2] or "") for r in existing.all()}
    for row in _BEAT_PATTERNS:
        key = (row["genre"], row["stage"], row["title"])
        if key in bp_keys:
            continue
        db.add(BeatPattern(id=uuid.uuid4(), **row))
        added["beat_patterns"] += 1

    # anti_ai_traps: key = (locale, pattern_type, pattern)
    existing = await db.execute(select(AntiAITrap.locale, AntiAITrap.pattern_type, AntiAITrap.pattern))
    trap_keys = {(r[0] or "", r[1] or "", r[2] or "") for r in existing.all()}
    for row in _ANTI_AI_TRAPS:
        key = (row["locale"], row["pattern_type"], row["pattern"])
        if key in trap_keys:
            continue
        db.add(AntiAITrap(id=uuid.uuid4(), **row))
        added["anti_ai_traps"] += 1

    # genre_profiles: key = code
    existing = await db.execute(select(GenreProfile.code))
    gp_codes = {r[0] for r in existing.all()}
    for row in _GENRE_PROFILES:
        if row["code"] in gp_codes:
            continue
        db.add(GenreProfile(id=uuid.uuid4(), **row))
        added["genre_profiles"] += 1

    # tool_specs: key = name
    existing = await db.execute(select(ToolSpec.name))
    ts_names = {r[0] for r in existing.all()}
    for row in _TOOL_SPECS:
        if row["name"] in ts_names:
            continue
        db.add(ToolSpec(id=uuid.uuid4(), **row))
        added["tool_specs"] += 1

    if any(v > 0 for v in added.values()):
        await db.commit()
    return added
