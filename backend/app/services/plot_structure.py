"""Plot Structure Extraction — extract narrative architecture from reference books.

Separate from StyleProfile (which is writing style / language level).
PlotStructure captures:
- Volume/arc organization patterns
- Conflict escalation structure
- Character introduction rhythm
- Pacing patterns (slow start? fast hook?)
- Foreshadow density and resolution timing

This is OPTIONAL when generating — user chooses whether to follow
a reference book's structural pattern or create freely.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

LLM_STRUCTURE_PROMPT = """分析以下小说文本的剧情架构特征。注意：只分析结构和节奏，不分析文笔。

分析以下维度：
1. arc_pattern：全书叙事弧线模式（如"三幕式""英雄之旅""多线交织""逐层揭秘"）
2. volume_pattern：分卷方式（如"按地图推进""按势力推进""按时间线""按主角成长阶段"）
3. opening_style：开局方式（如"悬念开场""日常切入""倒叙""战斗开场"）
4. pacing_curve：节奏曲线（如"慢热型""快节奏开场""前紧后松""波浪式"）
5. conflict_escalation：冲突升级模式（如"层层递进""螺旋上升""间歇爆发"）
6. character_intro_rhythm：角色引入节奏（如"一次性全出""逐卷引入""随事件引入"）
7. foreshadow_style：伏笔风格（如"密集埋线""关键节点埋""自然融入对话"）
8. climax_frequency：高潮频率（如"每卷一个大高潮""前期密集后期收束"）
9. ending_pattern：结局模式（如"开放式""大团圆""半开放""悲剧"）
10. structure_summary：一句话总结这本书的架构特色

输出纯JSON。

文本：
"""


async def extract_plot_structure(text: str) -> dict:
    """Extract plot structure features using LLM analysis."""
    from app.services.model_router import get_model_router

    router = get_model_router()
    sample = text[:5000]

    try:
        result = await router.generate(
            task_type="extraction",
            messages=[
                {"role": "system", "content": "你是小说结构分析专家，只输出JSON。"},
                {"role": "user", "content": LLM_STRUCTURE_PROMPT + sample},
            ],
        )
        cleaned = result.text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(cleaned)
    except Exception as e:
        logger.warning("Plot structure extraction failed: %s", e)
        return {"error": str(e)}


def compile_structure_prompt(structure: dict) -> str:
    """Compile extracted plot structure into a prompt instruction."""
    if not structure or "error" in structure:
        return ""

    parts = []
    parts.append("参考以下剧情架构模式（可选，仅供参考）：")

    field_labels = {
        "arc_pattern": "叙事弧线",
        "volume_pattern": "分卷方式",
        "opening_style": "开局方式",
        "pacing_curve": "节奏曲线",
        "conflict_escalation": "冲突升级",
        "character_intro_rhythm": "角色引入",
        "foreshadow_style": "伏笔风格",
        "climax_frequency": "高潮频率",
        "ending_pattern": "结局模式",
    }

    for key, label in field_labels.items():
        val = structure.get(key, "")
        if val and isinstance(val, str) and val not in ("无", "None"):
            parts.append(f"- {label}：{val[:80]}")

    summary = structure.get("structure_summary", "")
    if summary:
        parts.append(f"\n架构特色：{summary}")

    return "\n".join(parts)
