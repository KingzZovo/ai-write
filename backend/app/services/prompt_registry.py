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
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt import PromptAsset
from app.services.model_router import get_model_router, GenerationResult

logger = logging.getLogger(__name__)

# =========================================================================
# Built-in prompts — auto-seeded on first access if DB is empty
# =========================================================================

BUILTIN_PROMPTS: list[dict[str, Any]] = [
    {
        "task_type": "generation",
        "name": "小说正文生成",
        "description": "根据大纲和上下文生成章节正文",
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
        "description": "对初稿进行风格润色，提升文学表现力",
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
        "description": "根据创意生成完整的全书大纲",
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
        "description": "根据全书大纲生成单卷详细大纲",
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
        "description": "根据卷大纲生成单章详细大纲",
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
        "description": "评估小说文本的写作质量",
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
        "description": "从文本中提取角色、地点、事件等实体",
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
        "description": "生成章节的结构化摘要",
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
        "description": "根据指令对选中文本进行改写",
        "mode": "text",
        "system_prompt": (
            "你是一位专业的小说文本编辑。请根据指令对所选文本进行改写。\n"
            "保持原文的核心意思，但按照指令调整风格/语气/细节。\n"
            "只输出改写后的文本，不要解释。"
        ),
    },
]


# =========================================================================
# Registry class
# =========================================================================

class PromptRegistry:
    """Central registry for prompt assets. Provides lookup and execution."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def seed_builtins(self) -> int:
        """Seed built-in prompts if the registry is empty. Returns count seeded."""
        from sqlalchemy import func
        count = await self.db.scalar(select(func.count(PromptAsset.id)))
        if count and count > 0:
            return 0

        seeded = 0
        for p in BUILTIN_PROMPTS:
            asset = PromptAsset(
                task_type=p["task_type"],
                name=p["name"],
                description=p.get("description", ""),
                mode=p.get("mode", "text"),
                system_prompt=p["system_prompt"],
                user_template=p.get("user_template", ""),
                output_schema=p.get("output_schema"),
                context_policy=p.get("context_policy", "default"),
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
    **kwargs,
) -> GenerationResult:
    """Run a text prompt from the registry. Falls back to model router defaults."""
    registry = PromptRegistry(db)
    asset = await registry.get(task_type)

    router = get_model_router()
    system = asset.system_prompt if asset else ""
    if extra_system:
        system = f"{system}\n\n{extra_system}" if system else extra_system

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    result = await router.generate(task_type=task_type, messages=messages, **kwargs)

    if asset:
        await registry.track_result(asset.id, success=bool(result.text))

    return result


async def run_structured_prompt(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    extra_system: str = "",
    **kwargs,
) -> dict:
    """Run a structured prompt (expects JSON output). Returns parsed dict."""
    result = await run_text_prompt(task_type, user_content, db, extra_system, **kwargs)

    try:
        # Try to extract JSON from the response
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
    **kwargs,
) -> AsyncIterator[str]:
    """Stream a text prompt from the registry."""
    registry = PromptRegistry(db)
    asset = await registry.get(task_type)

    router = get_model_router()
    system = asset.system_prompt if asset else ""
    if extra_system:
        system = f"{system}\n\n{extra_system}" if system else extra_system

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    async for chunk in router.generate_stream(task_type=task_type, messages=messages, **kwargs):
        yield chunk
