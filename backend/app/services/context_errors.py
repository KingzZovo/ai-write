"""Failures that must not become apparently valid writing context."""


class RequiredContextUnavailable(RuntimeError):
    """Required chapter tasks, continuity, identity or world rules are unavailable."""
