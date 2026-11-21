"""Acceptance is distinct from whether another automatic rewrite is worthwhile."""
from dataclasses import dataclass
import hashlib
import math
from typing import Any

from app.services.chapter_quality_gate import chapter_persist_status
from app.services.narrative_quality_gates import issue_violation_type
from app.services.quality_debt import classify_terminal_action

# Literary preferences alone must not become hard acceptance failures.
_CAUSAL_TYPES = frozenset({
    'chapter_completion_violation', 'time_rule_violation', 'space_rule_violation',
    'information_rule_violation', 'mechanism_rule_violation', 'power_resource_violation',
    'result_strength_violation', 'cognition_violation', 'mundane_plausibility_violation',
    # 2026-11-20:评审新增的两类自洽性判据。有逐字 quote 定位时才当硬失败
    # (has_located_causal_issue 已要求 quote_verified),避免模型凭感觉扣分。
    'continuity_fact_violation', 'causal_chain_violation',
})


def _field(result: Any, name: str, default=None):
    return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)


def has_located_causal_issue(result: Any) -> bool:
    """A reported factual defect with an exact source anchor needs resolution.

    Quote verification proves location, not the truth of the model's diagnosis.
    This is a revision/review trigger, never an independent literary verdict.
    """
    issues = _field(result, 'issues', [])
    if not isinstance(issues, list):
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        tag = issue_violation_type(issue)
        quote = issue.get('quote')
        description = issue.get('description')
        if (tag in _CAUSAL_TYPES and issue.get('quote_verified') is True
                and isinstance(quote, str) and quote.strip()
                and isinstance(description, str)
                and description.strip().removeprefix(f'[{tag}]').strip()):
            return True
    return False


def evaluation_passes(result: Any, threshold: float = 8.2) -> bool:
    if result is None or _field(result, 'parse_failed', False):
        return False
    score = _field(result, 'overall')
    if type(score) not in (int, float) or not math.isfinite(score) or not threshold <= score <= 10:
        return False
    issues = _field(result, 'issues', [])
    if not isinstance(issues, list):
        return False
    if has_located_causal_issue(result):
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        if (issue_violation_type(issue) in _CAUSAL_TYPES
                and str(issue.get('severity', '')).strip().lower() in {'high', 'critical'}):
            return False
    return True


@dataclass
class ChapterAcceptance:
    required: bool
    threshold: float = 8.2
    text_hash: str | None = None
    result: Any = None

    def record(self, text: str, result: Any) -> None:
        self.text_hash = hashlib.sha256(text.encode()).hexdigest()
        self.result = result

    def passed(self, text: str) -> bool:
        return (self.text_hash == hashlib.sha256(text.encode()).hexdigest()
                and evaluation_passes(self.result, self.threshold))

    def status(self, text: str, quality_meta: dict | None = None) -> str:
        mechanical_status = chapter_persist_status(text, quality_meta)
        if mechanical_status != 'completed':
            return mechanical_status
        if self.required and not self.passed(text):
            return 'needs_review'
        return 'completed'

    def terminal_action(self, text: str, quality_meta: dict | None = None) -> dict:
        """Structured terminal semantics for this chapter (see quality_debt).

        ``mechanical_status`` comes from the existing ``status()`` chain, so an
        unusable body (empty / truncated / refusal / block) still reports as
        ``draft`` and stops; the evaluation signals decide whether a usable body
        is patched locally, deferred as quality debt, or truly replanned.
        ``quality_meta`` may carry ``plan_misaligned`` and ``missing_obligations``
        when the caller knows the plan state; they default to False / [].
        """
        meta = quality_meta or {}
        return classify_terminal_action(
            mechanical_status=self.status(text, quality_meta),
            evaluation=self.result,
            located_causal_issue=has_located_causal_issue(self.result),
            missing_obligations=list(meta.get("missing_obligations") or []),
            plan_misaligned=bool(meta.get("plan_misaligned")),
        )

    def persisted_status(self, text: str, quality_meta: dict | None = None) -> str:
        """Status to persist, letting usable-but-imperfect chapters move on.

        Only ``stop_for_replan`` (plan mismatch / unmet obligation) becomes
        ``needs_review``; ``defer_and_continue`` and ``local_patch_plan`` persist
        as ``completed`` (the former carrying quality debt). A mechanically
        unusable body keeps its existing ``status()`` verdict -- empty text is
        never promoted to ``completed`` by quality debt.
        """
        base = self.status(text, quality_meta)
        if base == "draft":
            return base
        action = self.terminal_action(text, quality_meta).get("action")
        if action in ("defer_and_continue", "local_patch_plan"):
            return "completed"
        if action == "stop_for_replan":
            return "needs_review"
        return base
