"""串行三角色章节质量管线编排器（子项目 B）。

drafter（已出初稿）→ logic_critic 隔离核查 → drafter 定向改写（最多 N 轮、
plateau 终止）→ apply_chapter_quality_gate（第三棒，零改动）。任一棒失败降级，
不丢整章。CHAPTER_PIPELINE_ENABLED=0 一键回退纯 quality_gate 老路径。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services.logic_critic import LogicIssue
from app.services.prompt_registry import run_text_prompt

if TYPE_CHECKING:
    from app.services.chapter_quality_gate import ChapterQualityGateResult

logger = logging.getLogger(__name__)


def build_targeted_rewrite_content(text: str, issues: list[LogicIssue]) -> str:
    """构造定向改写指令：只列 locatable issue，要求只动命中处。"""
    locatable = [i for i in issues if i.locatable]
    lines = [
        "下面这段中文小说正文存在若干「章内逻辑/空间」缺陷。",
        "只修复下列明确点名的问题，逐字定位到引用片段附近改写；",
        "不要改写其他段落，不要改变事件顺序、人物关系与核心信息，字数不要明显缩短。",
        "不要输出解释、分析、标题、Markdown 或代码块，只输出修订后的完整正文。",
        "",
        "【待修复问题】",
    ]
    for idx, issue in enumerate(locatable, 1):
        lines.append(
            f"{idx}. [{issue.dimension}] 引用：「{issue.quote}」｜问题：{issue.problem}｜修法：{issue.fix_hint}"
        )
    lines.append("")
    lines.append("【待修订正文】")
    lines.append(text)
    return "\n".join(lines)


async def apply_targeted_logic_rewrite(
    *,
    text: str,
    issues: list[LogicIssue],
    db: object,
    project_id: object = None,
    chapter_id: object = None,
) -> str | None:
    """调 drafter 做定向改写。失败返回 None（调用方保留上一稿）。"""
    if not any(i.locatable for i in issues):
        return None
    user_content = build_targeted_rewrite_content(text, issues)
    try:
        result = await run_text_prompt(
            "drafter",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("targeted logic rewrite failed; keeping prior draft: %s", exc)
        return None
    candidate = (result.text or "").strip()
    return candidate or None


@dataclass
class ChapterPipelineResult:
    final_text: str
    quality_gate_result: "ChapterQualityGateResult | Any"
    logic_rounds: int
    logic_issues_remaining: int
    logic_available: bool

    def to_echo_report(self) -> dict:
        """不污染主流程的精简报告：只回约定字段。"""
        return {
            "logic_rounds": self.logic_rounds,
            "logic_issues_remaining": self.logic_issues_remaining,
            "logic_available": self.logic_available,
            "prose_gate_status": getattr(self.quality_gate_result, "status", "unknown"),
        }


from app.services.chapter_quality_gate import apply_chapter_quality_gate
from app.services.logic_critic import run_logic_critic


def _pipeline_enabled() -> bool:
    return os.getenv("CHAPTER_PIPELINE_ENABLED", "1") != "0"


def _max_logic_rounds() -> int:
    return int(os.getenv("LOGIC_CRITIC_MAX_ROUNDS", "2"))


async def run_chapter_pipeline(
    *,
    text: str,
    db: object,
    project_id: object = None,
    chapter_id: object = None,
    target_word_count: int | None = None,
    chapter_outline: dict | None = None,
    prev_chapter_tail: str = "",
    skip_polish: bool = False,
) -> ChapterPipelineResult:
    """串行三角色管线。开关关闭时等价于直调 apply_chapter_quality_gate。

    完整逻辑回环在 Task 9 实现；此处先落开关旁路 + 最小直通路径。
    """
    if not _pipeline_enabled():
        qg = await apply_chapter_quality_gate(
            text=text,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
            target_word_count=target_word_count,
            skip_polish=skip_polish,
        )
        return ChapterPipelineResult(
            final_text=qg.final_text,
            quality_gate_result=qg,
            logic_rounds=0,
            logic_issues_remaining=0,
            logic_available=False,
        )

    # Task 9 会在此插入 logic 回环；当前先直通第三棒。
    qg = await apply_chapter_quality_gate(
        text=text,
        db=db,
        project_id=project_id,
        chapter_id=chapter_id,
        target_word_count=target_word_count,
        skip_polish=skip_polish,
    )
    return ChapterPipelineResult(
        final_text=qg.final_text,
        quality_gate_result=qg,
        logic_rounds=0,
        logic_issues_remaining=0,
        logic_available=False,
    )
