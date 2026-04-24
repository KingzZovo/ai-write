"""Prompt Registry — Unified prompt management, versioning, and execution.

All production prompts are registered here. Services use the registry
to retrieve prompts by task_type, ensuring consistent, trackable prompting.

Runner functions:
  - run_text_prompt: Generate free-form text
  - run_structured_prompt: Generate JSON output
  - stream_text_prompt: Stream text via SSE
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt import PromptAsset
from app.services.model_router import get_model_router, GenerationResult

logger = logging.getLogger(__name__)


@dataclass
class RouteSpec:
    """Resolved routing for a task: which endpoint + model + sampling params."""

    prompt_id: uuid.UUID | None
    endpoint_id: uuid.UUID | None
    model: str
    temperature: float
    max_tokens: int
    system_prompt: str
    mode: str

# =========================================================================
# Built-in prompts — auto-seeded on first access if DB is empty
# =========================================================================

BUILTIN_PROMPTS: list[dict[str, Any]] = [
    {
        "task_type": "generation",
        "name": "小说正文生成",
        "name_en": "Chapter Generation",
        "description": "根据大纲和上下文生成章节正文",
        "description_en": "Generate chapter prose from context pack.",
        "category": "Core Writing",
        "order": 10,
        "always_enabled": 1,
        "mode": "text",
        "system_prompt": (
            "你是一位专业的小说内容生成引擎。你的任务是根据提供的设定、大纲和上下文，"
            "生成剧情连贯、逻辑自洽的小说正文。\n\n"
            "要求：\n"
            "- 展示而非讲述，用动作和细节代替抽象描述\n"
            "- 对话要有目的性，推动情节或揭示人物\n"
            "- 保持与前文的一致性（人物状态、场景、时间线）\n"
            "- 每章至少3000字，节奏控制：铺垫→冲突→高潮→钩子\n"
            "- 避免AI痕迹词（璀璨、油然而生、心潮澎湃等）"
        ),
    },
    {
        "task_type": "polishing",
        "name": "文本润色",
        "name_en": "Text Polishing",
        "description": "对初稿进行风格润色，提升文学表现力",
        "description_en": "Polish draft prose while preserving plot and logic.",
        "category": "Core Writing",
        "order": 20,
        "always_enabled": 0,
        "mode": "text",
        "system_prompt": (
            "你是一位专业的文学润色编辑。你的任务是对初稿进行风格润色，"
            "在保持剧情和逻辑不变的前提下，提升文学表现力。\n\n"
            "润色方向：\n"
            "- 替换AI高频词为更自然的表达\n"
            "- 增加五感细节（视觉之外至少两种感官）\n"
            "- 优化长短句交替节奏\n"
            "- 用生理反应代替情绪名词\n"
            "- 关键时刻放慢叙述（子弹时间）"
        ),
    },
    {
        "task_type": "outline_book",
        "name": "全书大纲生成",
        "name_en": "Book Outline",
        "description": "根据创意生成完整的全书大纲",
        "description_en": "Generate a full-book outline from a pitch.",
        "category": "Outline",
        "order": 30,
        "always_enabled": 1,
        "mode": "text",
        "system_prompt": (
            "你是一位经验丰富的小说策划师。根据用户提供的创意和设定，"
            "生成一份完整的全书大纲。\n\n"
            "大纲应包含：\n"
            "1. 故事核心概念和主题\n"
            "2. 主要角色设定（性格、目标、矛盾）\n"
            "3. 世界观设定\n"
            "4. 主线剧情走向（起承转合）\n"
            "5. 分卷规划（每卷核心冲突和解决）\n"
            "6. 核心伏笔布局"
        ),
    },
    {
        "task_type": "outline_volume",
        "name": "分卷大纲生成",
        "name_en": "Volume Outline",
        "description": "根据全书大纲生成单卷详细大纲",
        "description_en": "Generate detailed outline for a single volume.",
        "category": "Outline",
        "order": 40,
        "always_enabled": 1,
        "mode": "text",
        "system_prompt": (
            "你是一位经验丰富的小说策划师。根据全书大纲和指定的卷号，"
            "生成该卷的详细大纲。\n\n"
            "每卷大纲应包含：\n"
            "1. 本卷核心剧情线\n"
            "2. 章节列表（标题+简要内容）\n"
            "3. 本卷新增/活跃角色\n"
            "4. 本卷埋设和消解的伏笔\n"
            "5. 节奏规划（高潮点位置）"
        ),
    },
    {
        "task_type": "outline_chapter",
        "name": "章节大纲生成",
        "name_en": "Chapter Outline",
        "description": "根据卷大纲生成单章详细大纲",
        "description_en": "Generate detailed outline for a single chapter.",
        "category": "Outline",
        "order": 50,
        "always_enabled": 1,
        "mode": "text",
        "system_prompt": (
            "你是一位经验丰富的小说策划师。根据卷大纲和指定的章号，"
            "生成该章的详细大纲。\n\n"
            "章节大纲应包含：\n"
            "1. 场景设定（地点、时间、氛围）\n"
            "2. 出场角色及其状态\n"
            "3. 核心事件和冲突\n"
            "4. 章末钩子设计\n"
            "5. 与前后章的衔接点"
        ),
    },
    {
        "task_type": "evaluation",
        "name": "质量评估",
        "name_en": "Quality Evaluation",
        "description": "评估小说文本的写作质量",
        "description_en": "Evaluate writing quality across five axes.",
        "category": "Quality",
        "order": 60,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": "你是文学评论专家，只输出 JSON。",
        "output_schema": {
            "writing_quality": "float 0-10",
            "plot_coherence": "float 0-10",
            "character_depth": "float 0-10",
            "narrative_technique": "float 0-10",
            "readability": "float 0-10",
            "overall": "float 0-10",
            "verdict": "string",
            "brief_comment": "string",
        },
    },
    {
        "task_type": "extraction",
        "name": "实体提取",
        "name_en": "Entity Extraction",
        "description": "从文本中提取角色、地点、事件等实体",
        "description_en": "Extract characters, locations, events from text.",
        "category": "Extraction",
        "order": 70,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是一个文本分析助手。从小说文本中提取以下实体信息，输出纯 JSON。\n"
            "提取：characters(角色)、locations(地点)、events(事件)、items(物品)、"
            "relationships(关系)、timeline(时间线)。"
        ),
        "output_schema": {
            "characters": [{"name": "string", "description": "string"}],
            "locations": [{"name": "string"}],
            "events": [{"event": "string", "chapter": "int"}],
        },
    },
    {
        "task_type": "summary",
        "name": "章节摘要",
        "name_en": "Chapter Summary",
        "description": "生成章节的结构化摘要",
        "description_en": "Generate structured summary for a chapter.",
        "category": "Extraction",
        "order": 80,
        "always_enabled": 1,
        "mode": "structured",
        "system_prompt": (
            "你是一个小说分析助手。请为以下章节内容生成结构化摘要。\n"
            "输出 JSON 格式：{\"summary\": \"...\", \"key_events\": [...], "
            "\"character_states\": {...}, \"foreshadows\": [...]}"
        ),
    },
    {
        "task_type": "rewrite",
        "name": "文本改写",
        "name_en": "Text Rewrite",
        "description": "根据指令对选中文本进行改写",
        "description_en": "Rewrite selected text per instruction.",
        "category": "Editing",
        "order": 90,
        "always_enabled": 0,
        "mode": "text",
        "system_prompt": (
            "你是一位专业的小说文本编辑。请根据指令对所选文本进行改写。\n"
            "保持原文的核心意思，但按照指令调整风格/语气/细节。\n"
            "只输出改写后的文本，不要解释。"
        ),
    },
    # v0.6 — decompile pipeline
    {
        "task_type": "style_abstraction",
        "name": "风格抽象化",
        "name_en": "Style Abstraction",
        "description": "从参考书片段提取结构化风格指令，不保留原文",
        "description_en": "Distill structured style directives from reference slice without retaining raw text.",
        "category": "Decompile",
        "order": 100,
        "always_enabled": 1,
        "mode": "structured",
        "system_prompt": (
            "你是文本风格分析官。阅读小说片段后，输出结构化风格画像 JSON，"
            "不要引用或复述原文句子。\n"
            "输出字段：\n"
            "- pov：叙事视角（first/third_limited/third_omni）\n"
            "- tense：时态（past/present）\n"
            "- sentence_rhythm：句式节奏描述（长句/短句比例、排比候使用频率等）\n"
            "- dialogue_style：对话特征（描述比例、乡土化/书面化、口头禅等）\n"
            "- sensory_mix：感官调用分布（视/听/崅/触/味相对占比）\n"
            "- pacing：节奏特征（慢镜头/跳写/起起伏伏）\n"
            "- emotional_register：情感温度（冷峻/热烈/疏离/谐谑）\n"
            "- vocab_tone：词汇色彩标签列表（古风/柔软/硬核/市井/奇幻/科技等）\n"
            "- forbidden_tells：该风格应避免的标签或句式\n"
            "- signature_moves：该风格独特的写法（抽象描述，例如“用天气映射人物内心”）\n"
            "硬性约束：绝不出现原文完整句子、人名、专有名词。只输出 JSON。"
        ),
        "output_schema": {
            "pov": "string",
            "tense": "string",
            "sentence_rhythm": "string",
            "dialogue_style": "string",
            "sensory_mix": "string",
            "pacing": "string",
            "emotional_register": "string",
            "vocab_tone": ["string"],
            "forbidden_tells": ["string"],
            "signature_moves": ["string"],
        },
    },
    {
        "task_type": "beat_extraction",
        "name": "情节骨架抽取",
        "name_en": "Beat Sheet Extraction",
        "description": "从参考片段提取去专名的情节骨架（主体、诉求、障碍、转折）",
        "description_en": "Extract entity-redacted plot beats (subject/goal/obstacle/turn) from a reference slice.",
        "category": "Decompile",
        "order": 110,
        "always_enabled": 1,
        "mode": "structured",
        "system_prompt": (
            "你是剧本结构分析官。阅读小说片段后，输出按骨架组织的 beat sheet JSON，"
            "所有具体人名/地名/法宝名/门派名替换为角色标签（A/B/C）或类型词（老者/派系/圣器）。\n"
            "字段：\n"
            "- scene_type：场景类型（开篇/冲突/悔悟/类型转折）\n"
            "- subject：主角标签\n"
            "- goal：主角目标\n"
            "- stakes：起教点\n"
            "- obstacle：阻力\n"
            "- turn：转折/升级\n"
            "- outcome：结果\n"
            "- emotional_arc：情感曲线\n"
            "- foreshadow：伏笔埋下或回收\n"
            "- reusable_pattern：骨架模板一句话（例如“弱者被歺侮后获遗宝反杀”）\n"
            "硬性约束：不得包含原文人名/地名/专有名词。只输出 JSON。"
        ),
        "output_schema": {
            "scene_type": "string",
            "subject": "string",
            "goal": "string",
            "stakes": "string",
            "obstacle": "string",
            "turn": "string",
            "outcome": "string",
            "emotional_arc": "string",
            "foreshadow": "string",
            "reusable_pattern": "string",
        },
    },
    {
        "task_type": "redaction",
        "name": "实体脱敏",
        "name_en": "Entity Redaction",
        "description": "将人名/地名/法宝名/门派名替换为中性标签，保留句式和风格",
        "description_en": "Redact proper nouns from text while preserving sentence structure and style.",
        "category": "Decompile",
        "order": 120,
        "always_enabled": 1,
        "mode": "text",
        "system_prompt": (
            "你是文本脱敏工具。将所有具体人名、地名、法宝名、门派名、功法名替换为占位符，"
            "保留句式、节奏、丰富程度不变。\n"
            "替换规则：\n"
            "- 人名→《角色A》、《角色B》…（按出场顺序）\n"
            "- 地名→《地点1》、《地点1-山岰》（保留类型描述）\n"
            "- 法宝/功法→《法宝1-剑》、《功法1-剑法》\n"
            "- 门派→《势力1-剑派》\n"
            "- 时代/年号→《纪年1》\n"
            "绝不更改描述性词汇、动作词、形容词。只输出脱敏后的纯文本。"
        ),
    },
    {
        "task_type": "critic",
        "name": "一致性审校",
        "name_en": "Consistency Critic",
        "description": "比对初稿与人物/世界观/历史设定，指出冲突问题",
        "description_en": "Audit draft vs characters/world/history for inconsistencies.",
        "category": "Quality",
        "order": 130,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是一致性审校官。比对提供的 draft 与 ContextPack（人物卡/世界观/历史摘要），"
            "找出 draft 中与设定或已有情节冲突的地方，输出 JSON：\n"
            "{issues: [{severity: hard|soft|info, category, desc, location, suggestion}]}\n"
            "severity 划分：\n"
            "- hard: 位置矛盾/实力跨级/关系翻转，必须重写\n"
            "- soft: 风格粽頋、细节含糊、不建议重写但值得记录\n"
            "- info: 下一章需要注意的提醒点\n"
            "如无问题，输出 {issues: []}。不要引用原文长篇，location 用短引可。"
        ),
        "output_schema": {
            "issues": [
                {
                    "severity": "string",
                    "category": "string",
                    "desc": "string",
                    "location": "string",
                    "suggestion": "string",
                }
            ]
        },
    },
    {
        "task_type": "compact",
        "name": "记忆压缩",
        "name_en": "Memory Compaction",
        "description": "将多章摘要合并为一条高密度叙事脚手架",
        "description_en": "Compress N chapter summaries into one dense narrative scaffold.",
        "category": "Quality",
        "order": 140,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是小说记忆压缩器。输入为连续多章的摘要列表，输出一条高信息密度的 JSON：\n"
            "{span: [start_chapter, end_chapter], arc: string, key_events: [string], "
            "character_deltas: {name: string_delta}, world_changes: [string], "
            "open_threads: [string], callback_hooks: [string]}\n"
            "保留下文可能复使用的钩子和影响，删除不影响后续的细节。"
        ),
        "output_schema": {
            "span": ["int"],
            "arc": "string",
            "key_events": ["string"],
            "character_deltas": {},
            "world_changes": ["string"],
            "open_threads": ["string"],
            "callback_hooks": ["string"],
        },
    },
    # v1.4 — tier-aware specialized prompts (fallback to critic/extraction if no row)
    {
        "task_type": "critic_hard",
        "name": "硬性审校（flagship）",
        "name_en": "Hard Consistency Critic",
        "description": "高等级模型做硬性矛盾审校；回落到 critic",
        "description_en": "Flagship-tier hard inconsistency audit; falls back to critic.",
        "category": "Quality",
        "order": 131,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是高级一致性审校官（hard）。重点捕捉位置矛盾、实力跨级、关系翻转等必须重写的问题。\n"
            "输出 JSON：{issues: [{severity: hard, category, desc, location, suggestion}]}。无问题时输出 {issues: []}。"
        ),
    },
    {
        "task_type": "critic_soft",
        "name": "软性审校（standard/small）",
        "name_en": "Soft Consistency Critic",
        "description": "较低度模型做软性提醒/细节建议；回落到 critic",
        "description_en": "Standard/small-tier soft critique; falls back to critic.",
        "category": "Quality",
        "order": 132,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是软性审校官（soft/info）。关注风格粗糙、细节含糊、可优化点。\n"
            "输出 JSON：{issues: [{severity: soft|info, category, desc, location, suggestion}]}。"
        ),
    },
    {
        "task_type": "consistency_llm_check",
        "name": "一致性 LLM 快检",
        "name_en": "Consistency LLM Check",
        "description": "针对单个候选点做二元判定；回落到 critic",
        "description_en": "Binary LLM check for a single candidate issue; falls back to critic.",
        "category": "Quality",
        "order": 133,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是一致性快速检验器。输入一个候选矛盾点，判断是否成立。\n"
            "输出 JSON：{is_issue: bool, severity: hard|soft|info, reason: string}。"
        ),
    },
    {
        "task_type": "rag_query_rewrite",
        "name": "RAG 查询改写",
        "name_en": "RAG Query Rewrite",
        "description": "将用户原始查询改写为检索友好形式；回落到 extraction",
        "description_en": "Rewrite raw query into retrieval-friendly forms; falls back to extraction.",
        "category": "Extraction",
        "order": 71,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是 RAG 查询改写器。输入原始查询，输出 JSON："
            "{queries: [string], keywords: [string]}，覆盖同义词、实体别名、相关上下文。"
        ),
    },
    {
        "task_type": "characters_extraction",
        "name": "角色抽取",
        "name_en": "Characters Extraction",
        "description": "从文本中抽取角色卡；回落到 extraction",
        "description_en": "Extract character cards from text; falls back to extraction.",
        "category": "Extraction",
        "order": 72,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是角色信息抽取器。输出 JSON："
            "{characters: [{name, aliases, role, traits, goals, relationships}]}。"
        ),
    },
    {
        "task_type": "world_rules_extraction",
        "name": "世界规则抽取",
        "name_en": "World Rules Extraction",
        "description": "从文本中抽取世界规则/设定；回落到 extraction",
        "description_en": "Extract world-building rules from text; falls back to extraction.",
        "category": "Extraction",
        "order": 73,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是世界规则抽取器。输出 JSON："
            "{rules: [{scope, statement, evidence}]}。"
        ),
    },
    {
        "task_type": "relationships_extraction",
        "name": "关系抽取",
        "name_en": "Relationships Extraction",
        "description": "从文本中抽取角色间关系；回落到 extraction",
        "description_en": "Extract character relationships; falls back to extraction.",
        "category": "Extraction",
        "order": 74,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是角色关系抽取器。输出 JSON："
            "{relationships: [{source, target, type, sentiment, evidence}]}。"
        ),
    },
]


# =========================================================================
# Registry class
# =========================================================================

# v1.4 — task_type fallback for specialized tier-aware tasks.
# If a specialized task_type has no registered PromptAsset, fall back to the
# general task_type on the right.
_TASK_TYPE_FALLBACK: dict[str, str] = {
    "critic_hard": "critic",
    "critic_soft": "critic",
    "consistency_llm_check": "critic",
    "rag_query_rewrite": "extraction",
    "characters_extraction": "extraction",
    "world_rules_extraction": "extraction",
    "relationships_extraction": "extraction",
}


class PromptRegistry:
    """Central registry for prompt assets. Provides lookup and execution."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def seed_builtins(self) -> int:
        """Seed built-in prompts if the registry is empty. Returns count seeded."""
        from sqlalchemy import func
        count = await self.db.scalar(select(func.count(PromptAsset.id)))
        existing_task_types: set[str] = set()
        if count and count > 0:
            rows = await self.db.execute(select(PromptAsset.task_type))
            existing_task_types = {r for r in rows.scalars().all() if r}

        seeded = 0
        for p in BUILTIN_PROMPTS:
            if p["task_type"] in existing_task_types:
                continue
            asset = PromptAsset(
                task_type=p["task_type"],
                name=p["name"],
                name_en=p.get("name_en", ""),
                description=p.get("description", ""),
                description_en=p.get("description_en", ""),
                mode=p.get("mode", "text"),
                system_prompt=p["system_prompt"],
                user_template=p.get("user_template", ""),
                output_schema=p.get("output_schema"),
                context_policy=p.get("context_policy", "default"),
                category=p.get("category", "Core"),
                order=p.get("order", 0),
                always_enabled=p.get("always_enabled", 0),
            )
            self.db.add(asset)
            seeded += 1

        await self.db.flush()
        return seeded

    async def get(self, task_type: str) -> PromptAsset | None:
        """Get the active prompt for a task type."""
        result = await self.db.execute(
            select(PromptAsset)
            .where(PromptAsset.task_type == task_type, PromptAsset.is_active == 1)
            .order_by(PromptAsset.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> list[PromptAsset]:
        """Get all prompt assets."""
        result = await self.db.execute(
            select(PromptAsset).order_by(PromptAsset.task_type, PromptAsset.version.desc())
        )
        return list(result.scalars().all())

    async def resolve_route(self, task_type: str) -> RouteSpec:
        """Return routing spec for a task. Raises if no prompt or no endpoint."""
        asset = await self.get(task_type)
        # v1.4 — task_type fallback for specialized tier-aware tasks
        if asset is None:
            fallback = _TASK_TYPE_FALLBACK.get(task_type)
            if fallback:
                asset = await self.get(fallback)
        if asset is None:
            raise ValueError(
                f"No active prompt registered for task '{task_type}'. Create one at /prompts."
            )
        if asset.endpoint_id is None:
            raise ValueError(
                f"Prompt '{asset.name}' (task {task_type}) has no endpoint configured. "
                "Assign one at /prompts."
            )
        return RouteSpec(
            prompt_id=asset.id,
            endpoint_id=asset.endpoint_id,
            model=asset.model_name or "",
            temperature=asset.temperature if asset.temperature is not None else 0.7,
            max_tokens=asset.max_tokens if asset.max_tokens is not None else 8192,
            system_prompt=asset.system_prompt,
            mode=asset.mode,
        )

    async def resolve_tier(self, task_type: str) -> str | None:
        """v1.4 — return preferred model_tier for a task_type, with fallback.

        Looks up the PromptAsset for task_type (or its fallback) and returns its
        ``model_tier`` (one of flagship|standard|small|distill|embedding) or
        ``None`` if not set. Does not raise when no prompt is registered.
        """
        asset = await self.get(task_type)
        if asset is None:
            fallback = _TASK_TYPE_FALLBACK.get(task_type)
            if fallback:
                asset = await self.get(fallback)
        if asset is None:
            return None
        return getattr(asset, "model_tier", None)

    async def track_result(self, asset_id: str | UUID, success: bool, score: int = 0) -> None:
        """Track prompt execution result for analytics."""
        if success:
            await self.db.execute(
                update(PromptAsset)
                .where(PromptAsset.id == str(asset_id))
                .values(success_count=PromptAsset.success_count + 1)
            )
        else:
            await self.db.execute(
                update(PromptAsset)
                .where(PromptAsset.id == str(asset_id))
                .values(fail_count=PromptAsset.fail_count + 1)
            )


# =========================================================================
# Unified runners
# =========================================================================

async def run_text_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    project_id: Any = None,
    chapter_id: Any = None,
    rag_hits: list[dict] | None = None,
    messages: list[dict] | None = None,
    **kwargs,
) -> GenerationResult:
    """Run a text prompt from the registry (v0.5: uses RouteSpec + logs calls).

    If `messages` is provided, it is used directly (skip system/user build).
    Otherwise build `[system, user_content]` from the prompt's system + extra_system.
    """
    from app.services.llm_call_logger import log_llm_call

    registry = PromptRegistry(db)
    route = await registry.resolve_route(task_type)

    if messages is None:
        system = route.system_prompt
        if extra_system:
            system = f"{system}\n\n{extra_system}" if system else extra_system
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

    router = get_model_router()
    # Prometheus metric provider / model labels ("unknown" when route has no
    # explicit endpoint yet).
    _provider = getattr(route, "provider", None) or "unknown"
    _model = route.model or "unknown"
    from app.observability.metrics import time_llm_call
    async with log_llm_call(
        db=db,
        task_type=task_type,
        prompt_id=route.prompt_id,
        project_id=project_id,
        chapter_id=chapter_id,
        messages=messages,
        rag_hits=rag_hits or [],
        model=route.model or "",
        endpoint_id=route.endpoint_id,
    ) as ctx:
        with time_llm_call(task_type, _provider, _model) as mbox:
            result = await router.generate_by_route(route, messages, **kwargs)
            ctx.add_chunk(result.text)
            ctx.set_usage(result.usage.input_tokens, result.usage.output_tokens)
            mbox["input_tokens"] = result.usage.input_tokens
            mbox["output_tokens"] = result.usage.output_tokens

    if route.prompt_id:
        await registry.track_result(route.prompt_id, success=bool(result.text))

    return result


async def run_structured_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    project_id: Any = None,
    chapter_id: Any = None,
    **kwargs,
) -> dict:
    """Run a structured prompt (expects JSON output). Returns parsed dict."""
    result = await run_text_prompt(
        task_type,
        user_content,
        db,
        extra_system,
        project_id=project_id,
        chapter_id=chapter_id,
        **kwargs,
    )

    try:
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return {"raw_text": result.text, "parse_error": True}


async def stream_text_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    project_id: Any = None,
    chapter_id: Any = None,
    rag_hits: list[dict] | None = None,
    messages: list[dict] | None = None,
    **kwargs,
) -> AsyncIterator[str]:
    """Stream a text prompt from the registry (v0.5).

    If `messages` is provided, use directly (ContextPack path). Otherwise
    build messages from the prompt's system + user_content.
    """
    from app.services.llm_call_logger import log_llm_call

    registry = PromptRegistry(db)
    route = await registry.resolve_route(task_type)

    if messages is None:
        system = route.system_prompt
        if extra_system:
            system = f"{system}\n\n{extra_system}" if system else extra_system
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

    router = get_model_router()
    async with log_llm_call(
        db=db,
        task_type=task_type,
        prompt_id=route.prompt_id,
        project_id=project_id,
        chapter_id=chapter_id,
        messages=messages,
        rag_hits=rag_hits or [],
        model=route.model or "",
        endpoint_id=route.endpoint_id,
    ) as ctx:
        async for chunk in router.stream_by_route(route, messages, **kwargs):
            ctx.add_chunk(chunk)
            yield chunk
