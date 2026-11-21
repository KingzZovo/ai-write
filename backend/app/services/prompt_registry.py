"""Prompt Registry — Unified prompt management, versioning, and execution.

All production prompts are registered here. Services use the registry
to retrieve prompts by task_type, ensuring consistent, trackable prompting.

Runner functions:
  - run_text_prompt: Generate free-form text
  - run_structured_prompt: Generate JSON output
  - stream_text_prompt: Stream text via SSE

Registry entry points:
  - PromptRegistry.seed_builtins: insert built-in task_types that are missing
  - PromptRegistry.upgrade_builtins: version-upgrade untouched built-in rows
    that still carry an older default (operator edits stay, flagged for review)
"""

from __future__ import annotations

from app.services.polish_prompt import POLISH_SYSTEM

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt import PromptAsset
from app.services.model_router import get_model_router_async, GenerationResult

# v1.12 L1: tolerant JSON parser fallback. ``json_repair.loads`` will
# attempt to repair LLM output that is truncated, missing closing braces,
# uses smart quotes, has trailing commas, etc. We import lazily inside
# the parse helper so import failure never breaks the happy path.

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

from app.services.prompts.scene_writing import CHAPTER_WRITER_SYSTEM, SCENE_PLANNER_SYSTEM, SCENE_WRITER_SYSTEM
from app.services.prompts.arc_planning import ARC_META_SYSTEM, ARC_BATCH_SYSTEM

BUILTIN_PROMPTS: list[dict[str, Any]] = [
    {"task_type": "arc_outline_meta", "name": "当前弧整体规划", "mode": "structured",
     "system_prompt": ARC_META_SYSTEM, "inherits_route_from": "outline_volume"},
    {"task_type": "arc_outline_batch", "name": "当前弧章节展开", "mode": "structured",
     "system_prompt": ARC_BATCH_SYSTEM, "inherits_route_from": "outline_volume"},
    {"task_type": "scene_planner", "name": "中文小说场景规划", "mode": "structured",
     "system_prompt": SCENE_PLANNER_SYSTEM, "inherits_route_from": "outline_chapter"},
    {"task_type": "scene_writer", "name": "中文小说场景续写", "mode": "text",
     "system_prompt": SCENE_WRITER_SYSTEM, "inherits_route_from": "generation"},
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
        "system_prompt": CHAPTER_WRITER_SYSTEM,
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
        "system_prompt": POLISH_SYSTEM,
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
            "4. 按既定剧情收束章末，全书终章不强留悬念\n"
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
            "\"character_states\": {...}, \"foreshadows\": [...]}\n\n"
            "PR-OL6 抽取规则（重要，严格遵守）：\n"
            "1. character_states 仅包含本章「有变化」的角色。未变化的不要重复输出「承前状态」。\n"
            "2. 角色名使用「出租车司机/实习护士/抱孩子的女人/值班学姐/外卖员/交警/保安/院监/路人/孩子」等通用职业名时，必须加场景修饰词区分不同实例：如「桥上出租司机」与「机场出租司机」是不同人。\n"
            "3. 同一个通用名谓在多场景出现且作者未明示是同一人，不要合并为一个角色实体。\n"
            "4. 章号 ≥ 1。不要使用 0 或负数。\n"
            "5. 状态 JSON 各字段（身份/状态/情绪/能力等）需是本章发生的变化，不要重述上一章状态。"
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
            "硬性约束：不得包含原文人名/地名/专有名词。\n"
            "输出格式（v1.12 强约束）：\n"
            "1. 必须是单个合法、闭合的 JSON 对象（所有 { 与 } 配对，所有 [ 与 ] 配对）。\n"
            "2. 不要使用 ``` 代码块包裹，不要添加任何解释或注释，只输出 JSON。\n"
            "3. 字符串值必须使用 ASCII 双引号 (\"); 不要使用中文引号或单引号。\n"
            "4. 输出尽可能紧凑，控制总长度，不要冗长复述原文，只抽取骨架要点。"
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
    # Dossier consolidation layer (book_dossier / worldview_extractor).
    # Full output-format instructions are carried in user_content, so these
    # degrade cleanly to the generic 'extraction' prompt via
    # _TASK_TYPE_FALLBACK until an operator binds a dedicated endpoint.
    {
        "task_type": "style_consolidation",
        "name": "全书风格归纳",
        "name_en": "Book Style Consolidation",
        "description": "将全书风格卡的聚合统计+代表性卡片归纳为全书级风格档案",
        "description_en": "Reduce aggregated style-card stats + representative cards into one book-level style profile.",
        "category": "Decompile",
        "order": 150,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是全书风格总设计师。输入是确定性聚合的风格统计与分层抽取的代表性卡片，"
            "不是原书全文。请归纳全书级风格档案，只输出 JSON。"
            "不得出现书中人名/地名等专有名词；例证每条不超过60字。"
        ),
    },
    {
        "task_type": "plot_consolidation",
        "name": "全书剧情架构归纳",
        "name_en": "Book Plot Consolidation",
        "description": "将节拍卡的按章聚合统计归纳为全书级剧情架构档案",
        "description_en": "Reduce per-chapter beat-card statistics into one book-level plot-architecture profile.",
        "category": "Decompile",
        "order": 151,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是剧情架构分析师。输入是确定性聚合的节拍统计（场景类型分布、高潮间隔、"
            "铺垫→回收距离等）与可复用骨架模板。请归纳全书级剧情架构档案，只输出 JSON。"
            "不得出现书中人名/地名等专有名词。"
        ),
    },
    {
        "task_type": "world_arch_extraction",
        "name": "世界观架构抽取",
        "name_en": "Worldview Architecture Extraction",
        "description": "从抽样片段批量提取力量体系/规则约束/组织架构/地理格局/核心冲突源",
        "description_en": "Extract power-system/rules/organizations/geography/conflict-source candidates from sampled slices.",
        "category": "Decompile",
        "order": 152,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是世界观架构抽取器。从小说片段中提取世界观架构要素，只输出 JSON。"
            "除 proper_nouns 字段外，其余内容一律用「主角」「A势力」等占位符抽象表述。"
        ),
    },
    {
        "task_type": "world_arch_merge",
        "name": "世界观架构合并",
        "name_en": "Worldview Architecture Merge",
        "description": "将分批抽取的世界观要素合并为可复用的系统设计模式档案",
        "description_en": "Merge batched worldview candidates into a reusable system-design-pattern dossier.",
        "category": "Decompile",
        "order": 153,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是世界观系统设计师。将去重后的世界观要素候选合并为「世界观架构档案」，"
            "描述系统设计模式（力量体系如何分层、规则如何制造冲突），只输出 JSON。"
            "不得出现任何原书专有名词，一律抽象为「主角」「A势力」「圣器」等占位符。"
        ),
    },
    {
        "task_type": "author_style_merge",
        "name": "作者级风格合并",
        "name_en": "Author Style Merge",
        "description": "将同一作者多本书的全书级风格档案蒸馏为作者级风格档案（作者惯用 vs 单书特例）",
        "description_en": "Distill one author's per-book style profiles into an author-level profile (habitual vs book-specific).",
        "category": "Decompile",
        "order": 154,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是作者风格总设计师。输入是同一作者多本书的全书级风格档案与确定性跨书对比，"
            "不是原书全文。主字段只写跨书一致的「作者惯用」，单书独有写法放入 book_specific。"
            "只输出 JSON。不得出现真实书名、人名、地名等专有名词。"
        ),
    },
    {
        "task_type": "author_plot_merge",
        "name": "作者级剧情架构合并",
        "name_en": "Author Plot Merge",
        "description": "将同一作者多本书的剧情架构档案蒸馏为作者级架构档案（作者惯用 vs 单书特例）",
        "description_en": "Distill one author's per-book plot-architecture profiles into an author-level profile.",
        "category": "Decompile",
        "order": 155,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是作者剧情架构分析师。输入是同一作者多本书的剧情架构档案与确定性跨书节奏对比。"
            "主字段只写跨书一致的「作者惯用」结构模式，单书独有结构放入 book_specific。"
            "只输出 JSON。不得出现真实书名/人名/地名等专有名词。"
        ),
    },
    {
        "task_type": "author_world_merge",
        "name": "作者级世界观合并",
        "name_en": "Author World Merge",
        "description": "将同一作者多本书的世界观架构档案蒸馏为作者级设计模式档案",
        "description_en": "Distill one author's per-book worldview dossiers into an author-level design-pattern profile.",
        "category": "Decompile",
        "order": 156,
        "always_enabled": 0,
        "mode": "structured",
        "system_prompt": (
            "你是作者世界观系统设计师。输入是同一作者多本书的世界观架构档案与去重后的设计模式并集。"
            "主字段只写跨书复现的「作者惯用」设计模式，单书独有设定放入 book_specific。"
            "只输出 JSON。不得出现真实书名或任何原书专有名词。"
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
            "{summary: string, span: [start_chapter, end_chapter], arc: string, key_events: [string], "
            "character_deltas: {name: string_delta}, world_changes: [string], "
            "open_threads: [string], callback_hooks: [string]}\n"
            "summary 必须为非空连贯摘要，保留关键事件、人物变化、未决状态和后续承接所需事实。"
            "只压缩原文：未知仍未知，计划不算完成，不新增疑点、钩子或因果；不得用暗含确定结论的措辞改变状态。"
        ),
        "output_schema": {
            "summary": "string",
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
    # Dossier consolidation layer: instructions live in user_content, so the
    # generic extraction prompt is a safe degrade target pre-seed.
    "style_consolidation": "extraction",
    "plot_consolidation": "extraction",
    "world_arch_extraction": "extraction",
    "world_arch_merge": "extraction",
    "author_style_merge": "extraction",
    "author_plot_merge": "extraction",
    "author_world_merge": "extraction",
    # Multi-agent chapter pipeline (subproject B): degrade to existing
    # prompts when no dedicated PromptAsset is configured.
    "logic_critic": "critic",
    "drafter": "rewrite",
    # Incremental arc writing loop (subproject A): degrade to existing prompts.
    "arc_outline": "outline_volume",
    "arc_kickoff": "critic",
    "arc_suggest": "critic",
}


# Separate stage identities keep a whole-book prompt from demanding all nine
# sections when the active call owns only characters or volume metadata.
OUTLINE_STAGE_PROMPTS = {
    "outline_book_skeleton": ("outline_book", "全书骨架", "仅生成骨架阶段要求的一、三、七、八、九段及分卷计划。"),
    "outline_book_characters": ("outline_book", "人物与关系规划", "只生成当前请求指定的人物、关系或势力段落，承接已批准骨架。"),
    "outline_book_world": ("outline_book", "世界观规划", "仅生成六、世界观设定集，解释规则、资源、限制和人物行动代价。"),
    "outline_volume_meta": ("outline_volume", "分卷元信息", "仅返回本卷元信息 JSON 和准确章数，不生成章节摘要数组。"),
    "outline_volume_batch": ("outline_volume", "分卷章节规划批次", "仅返回当前指定章节范围的 batch JSON，保持前后因果与章号连续。"),
}
for _task, (_parent, _name, _instruction) in OUTLINE_STAGE_PROMPTS.items():
    BUILTIN_PROMPTS.append({"task_type": _task, "name": _name, "category": "Outline",
                            "system_prompt": "你是小说规划编辑。" + _instruction,
                            "inherits_route_from": _parent})


# Chapter acceptance and reference-text scoring have incompatible schemas.
# Inherit only the initial endpoint, never the generic scorer's instructions.
BUILTIN_PROMPTS.append({
    "task_type": "chapter_evaluation",
    "name": "章节质量验收",
    "name_en": "Chapter Quality Evaluation",
    "description": "核对章节到期事件、动作因果、人物一致性、风格、节奏与伏笔",
    "category": "Quality",
    "order": 61,
    "mode": "structured",
    "system_prompt": "你是小说章节评审。依据本次提供的正文和合同评分，只输出指定五维 JSON。不要使用参考文本学习价值评分格式。",
    "output_schema": {
        dimension: {"score": "number 0-10", "issues": "array of issue objects"}
        for dimension in ("plot_coherence", "character_consistency", "style_adherence",
                          "narrative_pacing", "foreshadow_handling")
    },
    "inherits_route_from": "evaluation",
})


# Chapter graph deltas are incompatible with reference-text entity extraction.
from app.services.chapter_entity_extraction import SYSTEM_PROMPT as _ENTITY_SYSTEM, OUTPUT_SCHEMA as _ENTITY_SCHEMA
BUILTIN_PROMPTS.append({
    "task_type": "chapter_entities", "name": "章节事实提取",
    "name_en": "Chapter Entity Changes", "category": "Extraction", "order": 71,
    "mode": "structured", "system_prompt": _ENTITY_SYSTEM,
    "output_schema": _ENTITY_SCHEMA, "inherits_route_from": "extraction",
})


from app.services.relationship_scope import SYSTEM_PROMPT as _SCOPE_SYSTEM, OUTPUT_SCHEMA as _SCOPE_SCHEMA
BUILTIN_PROMPTS.append({
    "task_type": "relationship_scope", "name": "关系变化范围核查",
    "name_en": "Relationship Change Scope", "category": "Extraction", "order": 74,
    "mode": "structured", "system_prompt": _SCOPE_SYSTEM,
    "output_schema": _SCOPE_SCHEMA, "inherits_route_from": "chapter_entities",
})


from app.services.character_cognition import EXTRACTION_PROMPT as _COGNITION_SYSTEM, COGNITION_KINDS
BUILTIN_PROMPTS.append({
    "task_type": "chapter_cognition", "name": "章节认知变化", "category": "Extraction", "order": 72,
    "mode": "structured", "system_prompt": _COGNITION_SYSTEM,
    "output_schema": {"type": "array", "items": {"oneOf": [
        {"type": "object", "required": ["character", field, "evidence"], "additionalProperties": False,
         "properties": {name: {"type": "string", "minLength": 1} for name in ("character", field, "evidence")}}
        for field in COGNITION_KINDS]}},
    "inherits_route_from": "extraction",
})


from app.services.foreshadow_discovery_prompt import SYSTEM_PROMPT as _DISCOVERY_SYSTEM, OUTPUT_SCHEMA as _DISCOVERY_SCHEMA
BUILTIN_PROMPTS.append({
    "task_type": "chapter_foreshadows", "name": "正文伏笔线索核查", "category": "Extraction", "order": 73,
    "mode": "structured", "system_prompt": _DISCOVERY_SYSTEM,
    "output_schema": _DISCOVERY_SCHEMA, "inherits_route_from": "extraction",
})


# =========================================================================
# Built-in default provenance — non-destructive upgrade policy
# =========================================================================
# ``seed_builtins`` only inserts task_types that are missing entirely, so a
# database seeded before a default changed would keep the stale text forever.
# Deciding whether such an existing row may be replaced needs the *previous*
# default content, and the registry has no column to record provenance in
# (no migration is allowed for this). The content itself is therefore the
# marker:
#
#   * ``default_content_fingerprint(mode, system_prompt)`` identifies one exact
#     default content (sha256 over whitespace-normalised text).
#   * ``BUILTIN_DEFAULT_HISTORY`` lists, per task_type, the fingerprints of the
#     defaults that already shipped in released code — i.e. the only contents
#     an untouched database row can have.
#   * ``BUILTIN_DEFAULTS_REVISION`` is the explicit revision of the defaults
#     shipped by this module right now. Bump it whenever any default below
#     changes and append the superseded fingerprint to the history, otherwise
#     an untouched row is reported as "needs review" instead of upgraded.
#
#
# Judgement per task_type (an existing row is never rewritten in place):
#   fingerprint(row) == fingerprint(current default) -> already current
#   fingerprint(row) in BUILTIN_DEFAULT_HISTORY[task] -> untouched legacy
#       default -> insert a new version with the current text and activate it
#   anything else -> operator edit: keep it active and flag for review
#
# The fingerprint covers mode + system_prompt (the instruction text the runner
# sends), not the route binding or output_schema. A row an upgrade deactivates
# stays in the table as history, so even a misjudged upgrade loses no text.

# Explicit revision of the shipped defaults; see the policy note above.
BUILTIN_DEFAULTS_REVISION = 2

# Fingerprints of superseded defaults, keyed by task_type. Extend, never
# rewrite, this table; each entry records where the old default came from.
BUILTIN_DEFAULT_HISTORY: dict[str, tuple[str, ...]] = {
    # Superseded when the scene prompts gained the "±15% 篇幅浮动" wording
    # (2026-11): the fingerprints are of the constants committed in 854f9b9,
    # obtained with
    #   git show 854f9b9:backend/app/services/prompts/scene_writing.py
    # and default_content_fingerprint(mode, <constant of that revision>).
    "generation": ("ab95e5bc564f36ab49c8f175fc42ae61f105177f7e1270ea16f50dc40dde1102",),
    "scene_planner": ("cff0512b63fe6ad9e895d7b791fcfcdf4b68f67b43267c234edf6e2f3fa503c6",),
    "scene_writer": ("f47d069100a6e815007cdc2615f467fdd2d54d656c6de8292e81f4067a0557d3",),
    # Superseded by the cognition-prompt revision: the old default is the
    # OLD_PROMPT constant of migration a1001948 (still the content of any row
    # seeded before that revision).
    "chapter_cognition": ("e8befb7a4b960ba2e81572f218163c455f98ea569c6b85b44af6c2d5598230da",),
    # Superseded by the polish revision: OLD_PROMPT of migration a1001935.
    "polishing": ("ab4ca819be47dc89b62d502163bdcc2c54bcb0f797b4de88c173d7abf8b0dbaf",),
}

# Route columns a new version inherits from the row it supersedes, so an
# operator-configured endpoint/model/budget survives the upgrade.
_ROUTE_BINDING_FIELDS = ("endpoint_id", "model_name", "temperature", "max_tokens", "model_tier")


def _normalised_default_text(system_prompt: str) -> str:
    """Ignore trailing whitespace and blank-line noise when matching defaults."""
    return "\n".join(line.rstrip() for line in (system_prompt or "").strip().splitlines())


def default_content_fingerprint(mode: str, system_prompt: str) -> str:
    """Fingerprint of one exact default content (mode + instruction text)."""
    payload = f"{(mode or 'text').strip()}\n{_normalised_default_text(system_prompt)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def builtin_prompt(task_type: str) -> dict[str, Any] | None:
    """Return the shipped default entry for ``task_type``, or None if unknown."""
    return next((p for p in BUILTIN_PROMPTS if p["task_type"] == task_type), None)


def builtin_default_fingerprint(task_type: str) -> str | None:
    """Fingerprint of the default content currently shipped for ``task_type``."""
    p = builtin_prompt(task_type)
    if p is None:
        return None
    return default_content_fingerprint(p.get("mode", "text"), p["system_prompt"])


def builtin_default_fingerprints() -> dict[str, str]:
    """Fingerprint of every shipped default, keyed by task_type."""
    return {p["task_type"]: default_content_fingerprint(p.get("mode", "text"), p["system_prompt"])
            for p in BUILTIN_PROMPTS}


def _row_fingerprint(row: PromptAsset) -> str:
    return default_content_fingerprint(row.mode, row.system_prompt)


def _builtin_asset(p: dict[str, Any], version: int | None = None) -> PromptAsset:
    """Build the row ``seed_builtins``/``upgrade_builtins`` inserts for a default."""
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
    if version is not None:
        asset.version = version
    return asset


def _copy_route_binding(source: PromptAsset, target: PromptAsset) -> None:
    for attr in _ROUTE_BINDING_FIELDS:
        setattr(target, attr, getattr(source, attr))


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
            asset = _builtin_asset(p)
            parent_task = p.get("inherits_route_from")
            if parent_task:
                parent = await self.get(parent_task)
                if parent is not None:
                    _copy_route_binding(parent, asset)
            self.db.add(asset)
            seeded += 1

        await self.db.flush()
        return seeded

    async def upgrade_builtins(self) -> dict[str, Any]:
        """Upgrade already-registered built-in prompts without losing author edits.

        Non-destructive rule, one decision per built-in task_type (see the
        provenance note above ``BUILTIN_DEFAULTS_REVISION``):

          * the active row still carries the default this module ships -> no-op;
          * the active row carries a default that was shipped earlier
            (``BUILTIN_DEFAULT_HISTORY``) -> the operator never touched it, so
            insert a new version with the current default, copy the route
            binding, activate it and deactivate the older rows;
          * any other content -> the operator wrote it: keep it active and mark
            the task_type ``needs_review`` ("需人工确认") in the report.

        Rows are added/deactivated only; no existing row is ever rewritten and
        no commit is issued (the caller owns the transaction, exactly like
        ``seed_builtins``). Task types with no row at all belong to
        ``seed_builtins`` and are reported under ``absent``.
        """
        rows = list((await self.db.execute(select(PromptAsset))).scalars().all())
        by_task: dict[str, list[PromptAsset]] = {}
        for row in rows:
            by_task.setdefault(row.task_type, []).append(row)

        report: dict[str, Any] = {
            "revision": BUILTIN_DEFAULTS_REVISION,
            "upgraded": [], "current": [], "needs_review": [], "absent": [],
        }
        handled: set[str] = set()
        for p in BUILTIN_PROMPTS:
            task_type = p["task_type"]
            if task_type in handled:
                continue
            handled.add(task_type)
            versions = by_task.get(task_type) or []
            if not versions:
                report["absent"].append(task_type)
                continue
            active = max((r for r in versions if r.is_active == 1),
                         key=lambda r: r.version or 1, default=None)
            if active is None:
                report["needs_review"].append({
                    "task_type": task_type, "version": None,
                    "reason": "no_active_version",
                })
                continue
            fingerprint = _row_fingerprint(active)
            if fingerprint == default_content_fingerprint(p.get("mode", "text"), p["system_prompt"]):
                report["current"].append(task_type)
                continue
            if fingerprint not in BUILTIN_DEFAULT_HISTORY.get(task_type, ()):
                report["needs_review"].append({
                    "task_type": task_type, "version": active.version,
                    "reason": "operator_modified",
                })
                continue
            new_version = max(r.version or 1 for r in versions) + 1
            upgraded = _builtin_asset(p, version=new_version)
            upgraded.is_active = 1
            _copy_route_binding(active, upgraded)
            for row in versions:
                if row.is_active == 1:
                    row.is_active = 0
            self.db.add(upgraded)
            report["upgraded"].append({
                "task_type": task_type, "from_version": active.version,
                "to_version": new_version,
            })

        await self.db.flush()
        return report

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

# v1.5.0 C3: collapse the per-LLM-call DB cost.
#
# Pre-C3, every runner call did three statements against ``prompt_assets``:
#   1. registry.resolve_route(task_type)  -> SELECT (1 row)
#   2. registry.resolve_tier(task_type)   -> SELECT (same 1 row, redundantly)
#   3. registry.track_result(...)         -> UPDATE success_count+1 / fail_count+1
#
# After C3:
#   - Steps 1 + 2 collapse into a single read-through cache lookup
#     (``prompt_cache.get_snapshot``); 0 DB hits on warm cache.
#   - Step 3 becomes an in-memory ``buffer_track_result`` tick that is
#     flushed by a background task on a fresh session, eliminating the
#     C2-class deadlock where the per-call UPDATE blocked behind the outer
#     request session's idle-in-transaction row lock.
async def _resolve_route_and_tier_cached(
    task_type: str,
    db: AsyncSession,
) -> tuple[RouteSpec, str | None]:
    """Cached resolver used by ``run_text_prompt`` / ``stream_text_prompt``.

    Mirrors ``PromptRegistry.resolve_route`` semantics exactly (including the
    ``_TASK_TYPE_FALLBACK`` lookup and the "no endpoint" / "no prompt" error
    messages) but builds the RouteSpec from a detached snapshot, then returns
    the snapshot's ``model_tier`` alongside it so callers don't need a second
    DB hit.
    """
    from app.services import prompt_cache

    if db is None:
        from app.db.session import async_session_factory
        async with async_session_factory() as owned_db:
            return await _resolve_route_and_tier_cached(task_type, owned_db)
    snap = await prompt_cache.get_snapshot(task_type, db)
    if snap is None:
        fallback = _TASK_TYPE_FALLBACK.get(task_type)
        if fallback:
            snap = await prompt_cache.get_snapshot(fallback, db)
    if snap is None:
        raise ValueError(
            f"No active prompt registered for task '{task_type}'. Create one at /prompts."
        )
    if snap.endpoint_id is None:
        raise ValueError(
            f"Prompt '{snap.name}' (task {task_type}) has no endpoint configured. "
            "Assign one at /prompts."
        )
    route = RouteSpec(
        prompt_id=snap.id,
        endpoint_id=snap.endpoint_id,
        model=snap.model_name or "",
        temperature=snap.temperature,
        max_tokens=snap.max_tokens,
        system_prompt=snap.system_prompt,
        mode=snap.mode,
    )
    return route, snap.model_tier


def _prepend_registered_system(messages: list[dict], system: str, extra: str) -> list[dict]:
    """Keep the registered prompt exactly once when a legacy loader supplied it."""
    present = bool(system) and any(
        m.get("role") == "system" and isinstance(m.get("content"), str)
        and (m["content"] == system or m["content"].startswith(system + "\n"))
        for m in messages
    )
    prefix = extra if present else "\n\n".join(p for p in (system, extra) if p)
    return ([{"role": "system", "content": prefix}] if prefix else []) + list(messages)


async def run_text_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    project_id: Any = None,
    chapter_id: Any = None,
    rag_hits: list[dict] | None = None,
    messages: list[dict] | None = None,
    input_char_limit: int | None = None,
    frozen_prompt: dict | None = None,
    **kwargs,
) -> GenerationResult:
    """Run a text prompt from the registry (v0.5: uses RouteSpec + logs calls).

    If `messages` is provided, it is used directly (skip system/user build).
    Otherwise build `[system, user_content]` from the prompt's system + extra_system.
    """
    from app.services import prompt_cache

    router = await get_model_router_async()
    # v1.5.0 C3: one cached lookup yields both the RouteSpec and the
    # preferred model tier, replacing the two redundant SELECTs.
    if frozen_prompt is None:
        route, preferred_tier = await _resolve_route_and_tier_cached(task_type, db)
    else:
        from app.services.prompt_execution_snapshot import restore_prompt_snapshot
        route, preferred_tier = restore_prompt_snapshot(frozen_prompt, task_type)

    if messages is None:
        system = route.system_prompt
        if extra_system:
            system = f"{system}\n\n{extra_system}" if system else extra_system
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
    else:
        messages = _prepend_registered_system(messages, route.system_prompt, extra_system)

    # Budget the actual registered prompt as well as the complete material.
    # Reject instead of clipping source text or silently dropping instructions.
    if input_char_limit is not None:
        if type(input_char_limit) is not int or input_char_limit <= 0:
            raise ValueError("invalid_prompt_input_budget")
        if any(not isinstance(m.get("content"), str) for m in messages):
            raise ValueError("prompt_input_budget_requires_text")
        if sum(len(m["content"]) for m in messages) > input_char_limit:
            raise ValueError("prompt_input_budget_exceeded")

    # preferred_tier already resolved above (C3 collapses the second SELECT).
    # v1.7.2 Z3: time_llm_call wrap moved into ModelRouter
    # `generate_with_tier_fallback`; the metric box (status/duration/
    # tokens) is filled there from result.usage. Keeping the wrap here
    # would double-count.
    success = False
    try:
        result = await router.generate_with_tier_fallback(
            task_type,
            messages,
            route=route,
            preferred_tier=preferred_tier,
            _log_meta={
                "prompt_id": route.prompt_id,
                "project_id": project_id,
                "chapter_id": chapter_id,
                "rag_hits": rag_hits or [],
            },
            **kwargs,
        )

        success = bool(result.text and result.text.strip())
    finally:
        # Completion metrics include exceptions/cancellation without any
        # UPDATE on the request transaction. This is delivery, not quality.
        if route.prompt_id:
            await prompt_cache.buffer_track_result(route.prompt_id, success=success)

    return result


async def run_structured_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    project_id: Any = None,
    chapter_id: Any = None,
    output_validator: Callable[[dict], None] | None = None,
    retry_on_validation_error: bool = True,
    strict_json: bool = False,
    **kwargs,
) -> dict:
    """Parse and optionally validate JSON; retry once without shrinking scope.

    Validators raise ValueError/TypeError for missing or invalid task fields.
    JSON repair alone does not prove that a truncated outline is complete.
    The route/caller token budget is preserved on retry, including when the
    budget is inherited from the registered route rather than passed here.
    Callers owning task-specific repairs can disable this inner retry. Strict
    JSON disables local completion/repair and rejects duplicate/non-finite data.
    Explicitly incomplete provider endings are never accepted as valid JSON.
    """
    validation_error = ""
    validation_stage = ""

    def accepted(result):
        nonlocal validation_error, validation_stage
        from app.services.provider_response_guard import require_normal_finish
        try:
            require_normal_finish(getattr(result, "finish_reason", None))
        except ValueError:
            validation_error = "provider_incomplete_response"
            validation_stage = "provider"
            return None
        parsed = _try_parse_structured(result.text, strict_json=strict_json)
        if parsed is None:
            validation_error = "输出不是合法 JSON"
            validation_stage = "json"
            return None
        normalized = _normalize_structured(parsed)
        if normalized.get("parse_error") or normalized.get("_parse_error"):
            validation_error = "输出不是符合要求的 JSON 对象或数组"
            validation_stage = "schema"
            return None
        if output_validator is not None:
            try:
                output_validator(normalized)
            except (ValueError, TypeError) as exc:
                validation_error = str(exc)[:500]
                validation_stage = "schema"
                return None
        return normalized

    def failed(result):
        return {"raw_text": result.text, "parse_error": True,
                "validation_error": validation_error, "validation_stage": validation_stage}

    result = await run_text_prompt(
        task_type, user_content, db, extra_system,
        project_id=project_id, chapter_id=chapter_id, **kwargs,
    )
    parsed = accepted(result)
    if parsed is not None:
        return parsed
    if not retry_on_validation_error:
        return failed(result)

    logger.warning("structured prompt %s: validation failed, retrying once", task_type)
    strict_suffix = (
        "\n\n[结构修复] 上一次输出未通过校验。请按原任务重新输出单个完整 JSON，"
        "闭合全部对象、数组和字符串，不要 markdown 或解释文字。"
        "保留原任务要求的全部字段、条目数量、剧情过程、因果关系与细节；"
        "不要为了修复格式而缩写成摘要或减少章节/场景。只修复结构和缺失字段。"
        f"\n校验问题：{validation_error}"
    )
    retry_kwargs = dict(kwargs)
    retry_kwargs["temperature"] = 0.0
    try:
        result2 = await run_text_prompt(
            task_type, user_content, db, (extra_system or "") + strict_suffix,
            project_id=project_id, chapter_id=chapter_id, **retry_kwargs,
        )
    except Exception as exc:
        logger.warning("structured prompt %s: strict retry failed: %s", task_type, exc)
        return failed(result)
    parsed2 = accepted(result2)
    if parsed2 is not None:
        return parsed2
    return failed(result2)


def _try_parse_structured(text: str | None, *, strict_json: bool = False) -> Any:
    """v1.12 L1 parser: try json.loads, then json_repair as fallback.

    Returns the parsed dict/list on success, or None when both attempts
    fail. Strips a leading markdown code fence if present.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    if s.startswith("```"):
        if strict_json and not s.endswith("```"):
            return None
        try:
            s = s.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        except (IndexError, ValueError):
            pass
    try:
        if strict_json:
            import math

            def unique_object(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("duplicate_json_key")
                    obj[key] = value
                return obj

            def invalid_constant(value):
                raise ValueError("non_finite_json_number")

            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("non_finite_json_number")
                return number

            return json.loads(s, object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=finite_float)
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        if strict_json:
            return None
    try:
        from json_repair import loads as _repair_loads
        repaired = _repair_loads(s)
        # json_repair returns "" or {} for unrecoverable inputs; treat
        # truly empty results as failure so we don't fabricate data.
        if repaired in ("", None):
            return None
        return repaired
    except Exception:
        return None


def _normalize_structured(parsed: Any) -> dict:
    """Normalize parsed JSON to a dict.

    LLMs may return a top-level array under structured prompts. Wrap
    arrays in {"items": ...} so all callers can use .get() without
    crashing with "'list' object has no attribute 'get'".
    """
    if isinstance(parsed, list):
        return {"items": parsed} if parsed else {}
    if isinstance(parsed, dict):
        return parsed
    return {"raw_text": str(parsed), "parse_error": True}


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

    router = await get_model_router_async()
    # v1.5.0 C3: cached resolver (collapses the two SELECT prompt_assets).
    route, preferred_tier = await _resolve_route_and_tier_cached(task_type, db)

    if messages is None:
        system = route.system_prompt
        if extra_system:
            system = f"{system}\n\n{extra_system}" if system else extra_system
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
    else:
        messages = _prepend_registered_system(messages, route.system_prompt, extra_system)

    # preferred_tier already resolved above (C3 collapses the second SELECT).
    from contextlib import aclosing
    from app.services import prompt_cache

    complete = False
    has_text = False
    stream = router.stream_with_tier_fallback(
        task_type, messages, route=route, preferred_tier=preferred_tier,
        _log_meta={"prompt_id": route.prompt_id, "project_id": project_id,
                   "chapter_id": chapter_id, "rag_hits": rag_hits or []}, **kwargs,
    )
    try:
        async with aclosing(stream):
            async for chunk in stream:
                has_text = has_text or bool(chunk and chunk.strip())
                yield chunk
            complete = True
    finally:
        if route.prompt_id:
            await prompt_cache.buffer_track_result(route.prompt_id, success=complete and has_text)
