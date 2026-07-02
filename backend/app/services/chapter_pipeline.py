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


# 定向改写应只动命中处、保持篇幅。drafter 偶尔会「只回被点名片段附近的一小段」
# 而把整章其余正文丢弃（线上 ch2/ch3 真实故障：完整章被压成 ~500 字残片）。
# 残片随后被 persist-on-block 当成终稿存库并标 completed = 静默数据丢失。
# 任何掉到原文这个比例以下的候选都视为「内容被丢弃」而非「定向修订」，拒绝并保留上一稿。
_MIN_REWRITE_LENGTH_RATIO = 0.70


async def apply_targeted_logic_rewrite(
    *,
    text: str,
    issues: list[LogicIssue],
    db: object,
    project_id: object = None,
    chapter_id: object = None,
) -> str | None:
    """调 drafter 做定向改写。失败或残片返回 None（调用方保留上一稿）。"""
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
    if not candidate:
        return None
    # 篇幅守门：定向改写不应明显缩短全文。掉到阈值以下=drafter 丢了正文，
    # 不是修订，拒绝以免残片被当成终稿存库。
    original_len = len(text.strip())
    if original_len > 0 and len(candidate) < int(original_len * _MIN_REWRITE_LENGTH_RATIO):
        logger.warning(
            "targeted logic rewrite collapsed text (%d -> %d chars, < %.0f%%); "
            "keeping prior draft",
            original_len,
            len(candidate),
            _MIN_REWRITE_LENGTH_RATIO * 100,
        )
        return None
    return candidate


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

    current_text = text
    logic_rounds = 0
    logic_available = True
    issues_remaining = 0
    prev_high: int | None = None
    max_rounds = _max_logic_rounds()

    for _round in range(1, max(0, max_rounds) + 1):
        report = await run_logic_critic(
            chapter_text=current_text,
            chapter_outline=chapter_outline,
            prev_chapter_tail=prev_chapter_tail,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        if not report.available:
            logic_available = False
            break
        high = report.high_issues
        issues_remaining = len(high)
        if report.clean or not high:
            break
        cur = len(high)
        if prev_high is not None and cur >= prev_high:
            break  # plateau：无改善，停止空耗 throttle
        rewritten = await apply_targeted_logic_rewrite(
            text=current_text,
            issues=report.locatable_issues,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        if rewritten is None:
            break  # 改写失败：保留上一稿
        current_text = rewritten
        logic_rounds += 1
        prev_high = cur

    qg = await apply_chapter_quality_gate(
        text=current_text,
        db=db,
        project_id=project_id,
        chapter_id=chapter_id,
        target_word_count=target_word_count,
        skip_polish=skip_polish,
    )
    return ChapterPipelineResult(
        final_text=qg.final_text,
        quality_gate_result=qg,
        logic_rounds=logic_rounds,
        logic_issues_remaining=issues_remaining,
        logic_available=logic_available,
    )
