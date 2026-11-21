"""
Checker Manager

Runs all checkers in parallel, aggregates results, and provides
an overall pass/fail assessment with combined scoring.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)


@dataclass
class AggregatedResult:
    """Aggregated result from all checkers.

    Attributes:
        passed: Overall pass/fail (True if all critical checks pass).
        overall_score: Weighted average of all checker scores (0-10).
        checker_results: Individual results from each checker.
        total_issues: Total count of issues across all checkers.
        critical_issues: Issues with severity "critical".
        high_issues: Issues with severity "high".
        duration_ms: Time taken to run all checks in milliseconds.
    """

    passed: bool = False
    overall_score: float | None = None
    completed: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)
    checker_results: list[CheckResult] = field(default_factory=list)
    total_issues: int = 0
    critical_issues: list[dict[str, Any]] = field(default_factory=list)
    high_issues: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "review_status": 'complete' if self.completed else 'incomplete',
            "editorial_status": 'needs_author_review',
            "review_scope": 'configured_checks_on_supplied_context',
            "overall_score": round(self.overall_score, 2) if self.overall_score is not None else None,
            "total_issues": self.total_issues,
            "critical_issues": self.critical_issues,
            "high_issues": self.high_issues,
            "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors,
            "checkers": [cr.to_dict() for cr in self.checker_results],
        }

    def summary(self) -> str:
        """Human-readable summary of the check results."""
        status = 'INCOMPLETE' if not self.completed else ('PASS' if self.passed else 'FINDINGS')
        score = f'{self.overall_score:.1f}/10' if self.overall_score is not None else 'unavailable'
        lines = [
            f"Overall: {status} "
            f"(Score: {score}, "
            f"{self.total_issues} issues, "
            f"{self.duration_ms:.0f}ms)",
        ]
        for cr in self.checker_results:
            status = 'INCOMPLETE' if not cr.completed else ('OK' if cr.passed else 'FINDINGS')
            score = f'{cr.score:.1f}/10' if cr.completed and cr.score is not None else 'unavailable'
            lines.append(
                f"  [{status}] {cr.checker_name}: "
                f"{score} ({len(cr.issues)} issues)"
            )
        return "\n".join(lines)


# Default weight for each checker (can be overridden)
DEFAULT_WEIGHTS: dict[str, float] = {
    "consistency": 1.5,     # World consistency is very important
    "continuity": 1.3,      # Timeline continuity
    "ooc": 1.2,             # Character consistency
    "pacing": 1.0,          # Rhythm and pacing
    "reader_pull": 1.0,     # Engagement
    "anti_ai": 1.0,         # AI trace detection
}


class CheckerManager:
    """Orchestrates all checkers and aggregates results.

    Runs all registered checkers concurrently using asyncio.gather
    for maximum efficiency. Provides weighted scoring and aggregation.
    """

    def __init__(
        self,
        checkers: list[BaseChecker] | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        if checkers is not None:
            self.checkers = checkers
        else:
            self.checkers = self._create_default_checkers()
        self.weights = weights or DEFAULT_WEIGHTS

    @staticmethod
    def _create_default_checkers() -> list[BaseChecker]:
        """Create the default set of all checkers."""
        from app.services.checkers.anti_ai_checker import AntiAIChecker
        from app.services.checkers.consistency_checker import ConsistencyChecker
        from app.services.checkers.continuity_checker import ContinuityChecker
        from app.services.checkers.ooc_checker import OOCChecker
        from app.services.checkers.pacing_checker import PacingChecker
        from app.services.checkers.reader_pull_checker import ReaderPullChecker

        return [
            ConsistencyChecker(),
            ContinuityChecker(),
            OOCChecker(),
            PacingChecker(),
            ReaderPullChecker(),
            AntiAIChecker(),
        ]

    async def run_all(
        self,
        chapter_text: str,
        context_pack: ContextPack,
    ) -> AggregatedResult:
        """Run all checkers concurrently and aggregate results.

        Args:
            chapter_text: The generated chapter text to check.
            context_pack: The context pack used for generation.

        Returns:
            AggregatedResult with overall score and individual checker results.
        """
        start_time = time.monotonic()

        # Run all checkers concurrently
        tasks = [
            self._run_single_checker(checker, chapter_text, context_pack)
            for checker in self.checkers
        ]
        results: list[CheckResult] = await asyncio.gather(*tasks)

        # Aggregate
        aggregated = AggregatedResult()
        aggregated.checker_results = results
        aggregated.completed = bool(results) and all(cr.completed for cr in results)
        aggregated.passed = aggregated.completed and all(cr.passed for cr in results)
        if not results:
            aggregated.errors.append({'stage': 'checkers', 'code': 'no_checkers'})

        total_weighted_score = 0.0
        total_weight = 0.0

        for cr in results:
            weight = self.weights.get(cr.checker_name, 1.0)
            if cr.completed and cr.score is not None:
                total_weighted_score += cr.score * weight
                total_weight += weight
            aggregated.errors.extend(cr.errors)

            aggregated.total_issues += len(cr.issues)

            # Collect critical and high issues
            for issue in cr.issues:
                issue_with_checker = {**issue, "checker": cr.checker_name}
                if issue.get("severity") == "critical":
                    aggregated.critical_issues.append(issue_with_checker)
                elif issue.get("severity") == "high":
                    aggregated.high_issues.append(issue_with_checker)

            # If any checker fails, overall fails
            if not cr.passed:
                aggregated.passed = False

        aggregated.overall_score = total_weighted_score / total_weight if aggregated.completed and total_weight > 0 else None

        elapsed = (time.monotonic() - start_time) * 1000
        aggregated.duration_ms = elapsed

        logger.info(
            "Checks ended: %s (score=%s, issues=%d, duration=%.0fms)",
            'INCOMPLETE' if not aggregated.completed else ('PASS' if aggregated.passed else 'FINDINGS'),
            aggregated.overall_score,
            aggregated.total_issues,
            elapsed,
        )

        return aggregated

    async def run_selected(
        self,
        chapter_text: str,
        context_pack: ContextPack,
        checker_names: list[str],
    ) -> AggregatedResult:
        """Run only the specified checkers.

        Args:
            chapter_text: The chapter text to check.
            context_pack: The context pack used for generation.
            checker_names: List of checker names to run.

        Returns:
            AggregatedResult from the selected checkers only.
        """
        selected = [
            c for c in self.checkers if c.name in checker_names
        ]
        manager = CheckerManager(checkers=selected, weights=self.weights)
        result = await manager.run_all(chapter_text, context_pack)
        missing = set(checker_names) - {c.name for c in selected}
        if missing:
            result.completed = False
            result.passed = False
            result.overall_score = None
            result.errors.append({'stage': 'checkers', 'code': 'unknown_checker'})
        return result

    async def _run_single_checker(
        self,
        checker: BaseChecker,
        chapter_text: str,
        context_pack: ContextPack,
    ) -> CheckResult:
        """Run a single checker with error handling.

        If a checker fails, returns a result with a system error issue
        rather than propagating the exception.
        """
        try:
            if not isinstance(chapter_text, str) or not chapter_text.strip():
                raise ValueError('empty_draft')
            if context_pack is None:
                raise ValueError('review_context_missing')
            result = await checker.check(chapter_text, context_pack)
            if not isinstance(result, CheckResult) or result.checker_name != checker.name:
                raise ValueError('invalid_checker_result')
            if type(result.completed) is not bool or type(result.passed) is not bool or not isinstance(result.issues, list):
                raise ValueError('invalid_checker_result')
            if any(not isinstance(i, dict) or i.get('severity') not in {'critical', 'high', 'medium', 'low'} for i in result.issues):
                raise ValueError('invalid_checker_issues')
            if any(i['severity'] in {'critical', 'high'} for i in result.issues):
                result.passed = False
            if not isinstance(result.errors, list) or any(not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ('stage', 'code')) for e in result.errors):
                raise ValueError('invalid_checker_errors')
            if result.completed and (type(result.score) not in (int, float) or not math.isfinite(result.score) or not 0 <= result.score <= 10):
                raise ValueError('invalid_checker_score')
            if not result.completed or result.errors:
                result.completed = False
                result.passed = False
                result.score = None
            return result
        except Exception as e:
            logger.warning("Checker '%s' unavailable (%s)", checker.name, type(e).__name__)
            error_result = CheckResult(checker_name=checker.name)
            error_result.mark_unavailable('invalid_result' if isinstance(e, (ValueError, TypeError)) else 'check_failed')
            return error_result
