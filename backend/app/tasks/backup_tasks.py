"""Backup-related Celery tasks (v1.0.0 chunk 6).

Kept separate so the import surface stays small and the task registers even
when imported lazily.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from app.tasks import celery_app

logger = logging.getLogger(__name__)

_BACKUP_SCRIPT = "/app/scripts/backup.sh"  # mounted in dev; baked in prod image


@celery_app.task(name="tasks.run_daily_backup", bind=True, max_retries=2)
def run_daily_backup(self) -> dict:
    """Invoke scripts/backup.sh from the Celery worker.

    Returns a small status dict; relies on `docker compose` being reachable
    from the host running this worker (dev case) OR the backup script being
    adapted to read services via localhost (prod case).
    """
    script = _BACKUP_SCRIPT if Path(_BACKUP_SCRIPT).exists() else str(
        Path(__file__).resolve().parents[3] / "scripts" / "backup.sh"
    )
    if not Path(script).exists():
        logger.warning("backup script not found at %s; skipping", script)
        return {"ok": False, "reason": "script missing", "path": script}

    env = os.environ.copy()
    env.setdefault("BACKUP_RETAIN", "14")

    try:
        result = subprocess.run(
            ["bash", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        logger.exception("backup script timed out")
        return {"ok": False, "reason": "timeout"}

    ok = result.returncode == 0
    if not ok:
        logger.error("backup script failed rc=%s stderr=%s", result.returncode, result.stderr[-2000:])
    else:
        logger.info("backup script ok")
    return {
        "ok": ok,
        "rc": result.returncode,
        "tail": (result.stdout or "").splitlines()[-5:],
    }
