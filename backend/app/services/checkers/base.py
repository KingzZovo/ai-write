"""
Base classes for the multi-checker system.

Each checker evaluates a specific dimension of chapter quality and returns
a structured CheckResult with a score and detailed issues.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.services.context_pack import ContextPack


@dataclass
class CheckIssue:
    """A single issue found by a checker.

    Attributes:
        type: Category of the issue (e.g. "world_rule_violation", "ooc", "pacing").
        severity: One of "critical", "high", "medium", "low".
        location: Where in the text the issue was found (paragraph index, sentence, etc.).
        description: Human-readable description of the issue.
        suggestion: Recommended fix or improvement.
    """

    type: str
    severity: str  # critical / high / medium / low
    location: str = ""
    description: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "location": self.location,
            "description": self.description,
            "suggestion": self.suggestion,
        }


@dataclass
class CheckResult:
    """Result from a single checker.

    Attributes:
        checker_name: The name of the checker that produced this result.
        passed: Whether the check passed (True = no critical/high issues).
        score: Quality score from 0-10.
        issues: List of issues found, each as a dict with
            {type, severity, location, description, suggestion}.
    """

    checker_name: str
    passed: bool = True
    score: float = 10.0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add_issue(
        self,
        type: str,
        severity: str,
        location: str = "",
        description: str = "",
        suggestion: str = "",
    ) -> None:
        """Add an issue to the result.

        Automatically updates ``passed`` to False if severity is
        'critical' or 'high'.
        """
        issue = CheckIssue(
            type=type,
            severity=severity,
            location=location,
            description=description,
            suggestion=suggestion,
        )
        self.issues.append(issue.to_dict())
        if severity in ("critical", "high"):
            self.passed = False


class BaseChecker(ABC):
    """Abstract base for all chapter checkers.

    Subclasses must implement :meth:`check` which receives the chapter
    text and the full context pack, and returns a :class:`CheckResult`.
    """

    name: str = "base"

    @abstractmethod
    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        """Run the check and return results.

        Args:
            chapter_text: Full text of the generated chapter.
            context: The context pack used for generation.

        Returns:
            CheckResult with score, pass/fail, and detailed issues.
        """
        ...
