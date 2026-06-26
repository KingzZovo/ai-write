"""逻辑与剧情核查角色（章内语义级自洽审查）。

读完整章正文 + 本章大纲 + 紧邻前章末尾（隔离 context，不喂全书记忆），
专查现有 checker 漏掉的章内缺陷：空间方向矛盾、画面重述、跨度突变、
动作因果断裂、道具状态连续性。产出结构化 issue 清单供定向改写。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)

_MIN_LOGIC_CHARS = 200  # 短于此跳过核查（无意义，省一次限流调用）

# 五个检测维度（与 spec 一一对应）。
LOGIC_DIMENSIONS: tuple[str, ...] = (
    "spatial_direction",     # 空间方向一致性
    "scene_redescription",   # 画面重述/草稿叠写残留
    "span_jump",             # 空间/时间跨度突变
    "action_causality",      # 动作因果链断裂
    "prop_state",            # 道具/状态连续性
)


@dataclass(frozen=True)
class LogicIssue:
    dimension: str
    severity: str  # high|medium|low
    quote: str
    problem: str
    fix_hint: str
    locatable: bool = True


@dataclass
class LogicCriticReport:
    available: bool          # False = 核查不可用（LLM/解析失败）→ 降级
    clean: bool              # True = 无 issue
    issues: list[LogicIssue] = field(default_factory=list)

    @property
    def high_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.severity == "high"]

    @property
    def locatable_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.locatable]

    @property
    def issue_count(self) -> int:
        return len(self.issues)


_DIMENSION_GUIDE = """\
逐项核查以下五类「章内」缺陷（只看本章是否自洽，不评价剧情好坏）：
1. spatial_direction 空间方向一致性：同一段移动里方向词（上/下/进/出/前/后）与目标是否自洽。
   例：既写「通向地面层出口」「继续向上跑」，又写「往下跑」「向地下深处延伸」=矛盾。
2. scene_redescription 画面重述/草稿叠写残留：同一对象或场景在相邻段落被高相似度重复描写；
   二次出现应只保留新增信息，不该重描整幅静态画面与同一动作。
3. span_jump 空间/时间跨度突变：位置/楼层/时间出现无过渡跳变（A 点直接到 C 点，缺 B 衔接）。
   例：前文「才下去半层」，后文直接「踩上三楼平台」，中间缺衔接。
4. action_causality 动作因果链断裂：某动作的前置条件在文中未出现就直接发生。
5. prop_state 道具/状态连续性：同一道具或身体状态在本章前后矛盾。
"""

_OUTPUT_CONTRACT = """\
只输出一个 JSON 对象，不要任何解释、Markdown、代码块围栏：
{
  "issues": [
    {
      "dimension": "spatial_direction|scene_redescription|span_jump|action_causality|prop_state",
      "severity": "high|medium|low",
      "quote": "原文中的精确片段（必须能在正文里逐字找到，用于定位）",
      "problem": "一句话说明矛盾",
      "fix_hint": "一句话给出修法"
    }
  ],
  "clean": true 或 false
}
没有任何缺陷时返回 {"issues": [], "clean": true}。
quote 必须从正文原样摘录，不得改写或臆造。"""


def build_logic_critic_user_content(
    *,
    chapter_text: str,
    chapter_outline: dict | None,
    prev_chapter_tail: str,
) -> str:
    """构造逻辑核查的隔离 user content：本章正文 + 本章大纲 + 紧邻前章末尾。"""
    outline_block = ""
    if chapter_outline:
        try:
            outline_block = json.dumps(chapter_outline, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            outline_block = str(chapter_outline)
    parts = [_DIMENSION_GUIDE]
    if outline_block:
        parts.append(f"【本章大纲】\n{outline_block}")
    if prev_chapter_tail and prev_chapter_tail.strip():
        parts.append(f"【紧邻前章末尾（仅供衔接判断）】\n{prev_chapter_tail.strip()}")
    parts.append(f"【本章正文（核查对象）】\n{chapter_text}")
    parts.append(_OUTPUT_CONTRACT)
    return "\n\n".join(parts)


_VALID_SEVERITY = {"high", "medium", "low"}


def parse_logic_critic_output(parsed: object, *, chapter_text: str) -> LogicCriticReport:
    """把 run_structured_prompt 的 dict 解析为 LogicCriticReport。

    - 非 dict 或缺 issues 键 → available=False（核查不可用，触发降级）。
    - issues 为空 → clean=True（不论模型给的 clean 字段）。
    - 每个 issue 的 quote 若不在正文中 → locatable=False（臆造，不参与定向改写）。
    """
    if not isinstance(parsed, dict) or "issues" not in parsed:
        return LogicCriticReport(available=False, clean=False, issues=[])

    raw_issues = parsed.get("issues")
    if not isinstance(raw_issues, list):
        return LogicCriticReport(available=False, clean=False, issues=[])

    issues: list[LogicIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        dimension = str(raw.get("dimension") or "").strip()
        if dimension not in LOGIC_DIMENSIONS:
            dimension = "action_causality"  # 兜底归一，避免丢弃可用诊断
        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in _VALID_SEVERITY:
            severity = "medium"
        quote = str(raw.get("quote") or "").strip()
        locatable = bool(quote) and quote in chapter_text
        issues.append(
            LogicIssue(
                dimension=dimension,
                severity=severity,
                quote=quote,
                problem=str(raw.get("problem") or "").strip(),
                fix_hint=str(raw.get("fix_hint") or "").strip(),
                locatable=locatable,
            )
        )

    clean = len(issues) == 0
    return LogicCriticReport(available=True, clean=clean, issues=issues)


_MIN_LOGIC_CHARS = 200  # 短于此跳过核查（无意义，省一次限流调用）


async def run_logic_critic(
    *,
    chapter_text: str,
    chapter_outline: dict | None,
    prev_chapter_tail: str,
    db: object,
    project_id: object = None,
    chapter_id: object = None,
) -> LogicCriticReport:
    """跑一次逻辑核查。任何失败都降级为 available=False，绝不抛出。"""
    if not chapter_text or len(chapter_text.strip()) < _MIN_LOGIC_CHARS:
        # 超短稿无核查意义，视作干净（不消耗 LLM 调用）。
        return LogicCriticReport(available=True, clean=True, issues=[])

    user_content = build_logic_critic_user_content(
        chapter_text=chapter_text,
        chapter_outline=chapter_outline,
        prev_chapter_tail=prev_chapter_tail,
    )
    try:
        parsed = await run_structured_prompt(
            "logic_critic",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:  # noqa: BLE001 — 任何 relay/路由/解析失败都降级
        logger.warning("logic_critic LLM call failed; degrading: %s", exc)
        return LogicCriticReport(available=False, clean=False, issues=[])

    return parse_logic_critic_output(parsed, chapter_text=chapter_text)
