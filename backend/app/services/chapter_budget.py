"""每章 token 预算护栏。

背景(2026-11-17):项目此前只有"全局月度额度"(``usage_service``),没有
"单章累计消耗"的护栏。一章写坏之后反复重试,每一次重试都会重新消耗
规划/正文/检查/修文的全部 token,而作者在界面上看不到这一章的累计用量。
同类产品(AI-Novel-Writing-Assistant)按章设 100k 上限并允许在上限处
"查看原因 → 按建议或自定额度继续",本模块提供同样的判定与事件。

设计约束:

- **只拦已存在的章**:新章(调用方没有 chapter_id)还没有任何用量,不该被拦。
- **不阻断已有正文**:超限只拒绝"再生成一次",已保存的正文与状态不变。
- **默认关闭**:``CHAPTER_TOKEN_BUDGET_DEFAULT = 0`` 表示不启用,行为与改动前一致。
- 复核口径:``total_tokens = input_tokens + output_tokens``,包含失败调用
  (失败请求同样烧 token),也不受天数窗口限制。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.services.usage_summary import fetch_chapter_usage_budget

logger = logging.getLogger(__name__)

ENV_LIMIT = "CHAPTER_TOKEN_BUDGET_TOKENS"


@dataclass(frozen=True)
class ChapterBudgetVerdict:
    """One chapter's accumulated usage against its budget."""

    chapter_id: str
    used_tokens: int
    limit_tokens: int
    exceeded: bool
    calls: int = 0
    by_step: tuple[dict[str, Any], ...] = field(default=())

    @property
    def remaining_tokens(self) -> int:
        if self.limit_tokens <= 0:
            return 0
        return max(0, self.limit_tokens - self.used_tokens)

    def as_event(self) -> dict[str, Any]:
        """SSE payload for the budget stop (client shows used/limit/steps)."""
        return {
            "event": "chapter_budget_exceeded",
            "status": "blocked",
            "reason": "chapter_token_budget_exceeded",
            "chapter_id": self.chapter_id,
            "used_tokens": self.used_tokens,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": self.remaining_tokens,
            "calls": self.calls,
            "by_step": [dict(step) for step in self.by_step],
        }


def resolve_limit_tokens(limit_tokens: int | None = None) -> int:
    """Explicit value → env override → settings default. ``0`` means disabled.

    Env override exists so an operator can cap one environment without a
    redeploy; it wins over the settings default but never over an explicit
    per-call value (the API passes the client's requested cap).
    """
    if limit_tokens is not None:
        try:
            return max(0, int(limit_tokens))
        except (TypeError, ValueError):
            return 0

    raw = os.environ.get(ENV_LIMIT)
    if raw not in (None, ""):
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            logger.warning("ignoring non-integer %s=%r", ENV_LIMIT, raw)

    try:
        from app.config import settings

        return max(0, int(getattr(settings, "CHAPTER_TOKEN_BUDGET_DEFAULT", 0) or 0))
    except Exception:  # pragma: no cover - config import guard
        return 0


async def chapter_budget_verdict(
    db: Any,
    chapter_id: Any,
    *,
    limit_tokens: int | None = None,
) -> ChapterBudgetVerdict:
    """Read this chapter's accumulated usage and decide whether to stop.

    Never raises on a missing/odd chapter id: an unknown chapter simply has no
    usage rows, which yields ``used_tokens=0`` and ``exceeded=False``.
    """
    limit = resolve_limit_tokens(limit_tokens)
    cid = "" if chapter_id is None else str(chapter_id)
    if limit <= 0 or not cid:
        return ChapterBudgetVerdict(
            chapter_id=cid, used_tokens=0, limit_tokens=limit, exceeded=False
        )

    budget = await fetch_chapter_usage_budget(db, chapter_id=chapter_id, limit_tokens=limit)
    return ChapterBudgetVerdict(
        chapter_id=str(budget.get("chapter_id") or cid),
        used_tokens=int(budget.get("used_tokens") or 0),
        limit_tokens=int(budget.get("limit_tokens") or limit),
        exceeded=bool(budget.get("exceeded")),
        calls=int(budget.get("calls") or 0),
        by_step=tuple(budget.get("by_step") or ()),
    )