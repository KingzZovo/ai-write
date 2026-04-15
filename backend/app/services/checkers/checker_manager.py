"""
Checker Manager

Runs all checkers in parallel, aggregates results, and provides
an overall pass/fail assessment with combined scoring.
"""

from __future__ import annotations

import asyncio
import logging
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

    passed: bool = True
    overall_score: float = 10.0
    checker_results: list[CheckResult] = field(default_factory=list)
    total_issues: int = 0
    critical_issues: list[dict[str, Any]] = field(default_factory=list)
    high_issues: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "overall_score": round(self.overall_score, 2),
            "total_issues": self.total_issues,
            "critical_issues": self.critical_issues,
            "high_issues": self.high_issues,
            "duration_ms": round(self.duration_ms, 1),
            "checkers": [
                {
                    "name": cr.checker_name,
                    "passed": cr.passed,
                    "score": round(cr.score, 2),
                    "issue_count": len(cr.issues),
                    "issues": cr.issues,
                }
                for cr in self.checker_results
            ],
        }

    def summary(self) -> str:
        """Human-readable summary of the check results."""
        lines = [
            f"Overall: {'PASS' if self.passed else 'FAIL'} "
            f"(Score: {self.overall_score:.1f}/10, "
            f"{self.total_issues} issues, "
            f"{self.duration_ms:.0f}ms)",
        ]
        for cr in self.checker_results:
            status = "OK" if cr.passed else "FAIL"
            lines.append(
                f"  [{status}] {cr.checker_name}: "
                f"{cr.score:.1f}/10 ({len(cr.issues)} issues)"
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

        total_weighted_score = 0.0
        total_weight = 0.0

        for cr in results:
            weight = self.weights.get(cr.checker_name, 1.0)
            total_weighted_score += cr.score * weight
            total_weight += weight

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

        aggregated.overall_score = (
            total_weighted_score / total_weight if total_weight > 0 else 0.0
        )

        elapsed = (time.monotonic() - start_time) * 1000
        aggregated.duration_ms = elapsed

        logger.info(
            "All checks completed: %s (score=%.1f, issues=%d, duration=%.0fms)",
            "PASS" if aggregated.passed else "FAIL",
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
        if not selected:
            logger.warning(
                "No matching checkers found for: %s", checker_names
            )
            return AggregatedResult()

        manager = CheckerManager(checkers=selected, weights=self.weights)
        return await manager.run_all(chapter_text, context_pack)

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
            return await checker.check(chapter_text, context_pack)
        except Exception as e:
            logger.exception("Checker '%s' failed: %s", checker.name, e)
            error_result = CheckResult(
                checker_name=checker.name,
                passed=True,  # Don't fail overall on checker errors
                score=5.0,    # Neutral score
            )
            error_result.add_issue(
                type="checker_error",
                severity="low",
                description=f"Checker '{checker.name}' encountered an error: {e}",
                suggestion="This checker could not complete. Manual review recommended.",
            )
            return error_result
