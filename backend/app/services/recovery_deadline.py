"""Cooperative wall-clock budget for durable periodic recovery passes."""
import asyncio
from functools import wraps


# Keep the existing 120-second per-project allowance for large snapshots.
# An entire pass must not multiply that allowance by every selected project.
RECOVERY_BATCH_SECONDS = 300


def bounded_recovery(function):
    @wraps(function)
    async def run(*args, **kwargs):
        # TimeoutError intentionally reaches the task runner: a partial batch
        # must not be reported as successful. External cancellation propagates
        # unchanged, and unacknowledged receipts remain the source of truth.
        async with asyncio.timeout(RECOVERY_BATCH_SECONDS):
            return await function(*args, **kwargs)
    return run
