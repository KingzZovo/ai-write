"""
Multi-checker system for AI-generated chapter quality assurance.

Checkers:
- ConsistencyChecker: world rules, power system violations
- ContinuityChecker: timeline, character position, event sequence
- OOCChecker: out-of-character detection
- PacingChecker: rhythm, tension, information density
- ReaderPullChecker: hook effectiveness, engagement
- AntiAIChecker: AI writing trace detection
"""

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.checkers.checker_manager import AggregatedResult, CheckerManager

__all__ = [
    "BaseChecker",
    "CheckResult",
    "CheckerManager",
    "AggregatedResult",
]
