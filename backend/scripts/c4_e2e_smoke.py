"""v1.5.0 C4-6: cascade auto-regenerate end-to-end live smoke.

Drives the full SSE auto-revise -> cascade trigger pipeline in-process,
then verifies the C4-3 worker picks up the resulting cascade_tasks row
and transitions it to a terminal state. Designed to be re-runnable
locally and on CI.

What it covers (single binary, no LLM cost):

  Round 1 (fresh):
    POST /api/generate/chapter (auto_revise=True, threshold=99.0)
      -> ChapterEvaluator.evaluate is monkey-patched to return overall=3.5
         + 2 plot_coherence issues -> rounds_exhausted=True
         -> for-else cascade trigger fires
      -> SSE emits a `cascade_triggered` event with
         inserted=1, duplicates=0, dispatched=1, candidates_planned=1
      -> Celery worker picks up the row within ~30s and writes
         status='skipped', error_message='outline_handler_not_implemented'
         (handler stub is intentional: see C4 follow-up).

  Round 2 (idempotent):
    Same POST. plan_cascade still produces 1 candidate (planner is pure),
    but enqueue_cascade_candidates' INSERT ... ON CONFLICT DO NOTHING
    matches uq_cascade_tasks_idem (source_chapter_id, target_entity_type,
    target_entity_id, severity) and produces:
      inserted=0, duplicates=1, dispatched=0
    Worker is NOT re-invoked; the existing terminal row is left alone.

Usage:
    docker exec -e PYTHONPATH=/app ai-write-backend-1 \\
        python /app/scripts/c4_e2e_smoke.py [--rounds 2] [--no-cleanup]

The script writes the chapter's revised content as a side effect of
running the SSE pipeline, identical to a real auto_revise call. That is
benign for the smoke chapter (chapter 3, project f147...).

Requires:
    /tmp/.tok in the container (JSON {"token": "<bearer>", ...}).
    Use scripts/issue_token.sh or equivalent to refresh it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from unittest.mock import patch

import httpx
from sqlalchemy import text

PROJECT_ID = "f14712d6-6dc6-4cfb-b05f-e107fa02b63d"
CHAPTER_ID = "0aa149fa-8e64-428a-a31b-597586c28de9"
WORKER_DEADLINE_SEC = 30
WORKER_POLL_INTERVAL_SEC = 1.5


# --- LLM monkeypatches ------------------------------------------------------


async def _fake_orchestrate(self, **kwargs):
    """Yield a tiny canned 'revised' chapter to skip LLM cost."""
    for chunk in ("\u3010C4-6 smoke\u3011\u5360\u4f4d\u5185\u5bb9", "\u8df3\u8fc7 LLM\u3002"):
        yield chunk


def _fake_eval_result(overall: float):
    from app.services.chapter_evaluator import EvaluationResult

    return EvaluationResult(
        overall=overall,
        plot_coherence=overall,
        character_consistency=overall,
        style_adherence=overall,
        narrative_pacing=overall,
        foreshadow_handling=overall,
        issues=[
            {
                "dimension": "plot_coherence",
                "location": 3,
                "description": "C4-6 smoke: \u4eba\u7269\u52a8\u673a\u8df3\u8dc3",
                "suggestion": "\u8865\u5145\u8fc7\u6e21",
            },
            {
                "dimension": "plot_coherence",
                "location": 7,
                "description": "C4-6 smoke: \u573a\u666f\u8854\u63a5\u4e0d\u8db3",
                "suggestion": "\u52a0\u8fc7\u6e21\u53e5",
            },
        ],
    )


async def _fake_evaluate(self, *args, **kwargs):
    return _fake_eval_result(overall=3.5)


# --- DB helpers -------------------------------------------------------------


async def _read_token() -> str:
    with open("/tmp/.tok") as f:
        return json.load(f)["token"]


async def _delete_cascade_rows() -> int:
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        r = await db.execute(
            text("DELETE FROM cascade_tasks WHERE source_chapter_id = :cid"),
            {"cid": CHAPTER_ID},
        )
        await db.commit()
        return r.rowcount or 0


async def _list_cascade_rows() -> list[tuple]:
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        r = await db.execute(
            text(
                "SELECT id, target_entity_type, severity, status, attempt_count, error_message "
                "FROM cascade_tasks WHERE source_chapter_id = :cid "
                "ORDER BY created_at DESC"
            ),
            {"cid": CHAPTER_ID},
        )
        return [tuple(row) for row in r.all()]


async def _wait_for_terminal(initial_count: int) -> tuple | None:
    """Poll cascade_tasks until the most recent row reaches a terminal
    status. Returns (status, attempt_count, error_message) or None if
    the deadline elapses."""
    from app.db.session import async_session_factory

    deadline = asyncio.get_event_loop().time() + WORKER_DEADLINE_SEC
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(WORKER_POLL_INTERVAL_SEC)
        async with async_session_factory() as db:
            r = await db.execute(
                text(
                    "SELECT status, attempt_count, error_message FROM cascade_tasks "
                    "WHERE source_chapter_id = :cid ORDER BY created_at DESC LIMIT 1"
                ),
                {"cid": CHAPTER_ID},
            )
            row = r.first()
        if row and row[0] in ("done", "failed", "skipped"):
            return (row[0], row[1], row[2])
    return None


# --- SSE round runner -------------------------------------------------------


@dataclass
class RoundResult:
    http_status: int
    sse_total_lines: int
    cascade_evt: dict | None


async def _run_one_round(token: str, label: str) -> RoundResult:
    from app.services.scene_orchestrator import SceneOrchestrator
    from app.services.chapter_evaluator import ChapterEvaluator
    from app.main import app

    payload = {
        "project_id": PROJECT_ID,
        "chapter_id": CHAPTER_ID,
        "use_scene_mode": True,
        "auto_revise": True,
        "max_revise_rounds": 1,
        # Impossible threshold -> rounds_exhausted=True regardless of overall.
        "revise_threshold": 99.0,
        "target_words": 200,
    }

    sse_lines: list[str] = []
    cascade_evt: dict | None = None
    http_status = 0

    with patch.object(SceneOrchestrator, "orchestrate_chapter_stream", _fake_orchestrate), \
         patch.object(ChapterEvaluator, "evaluate", _fake_evaluate):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=120.0
        ) as client:
            async with client.stream(
                "POST",
                "/api/generate/chapter",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                http_status = resp.status_code
                async for raw in resp.aiter_lines():
                    if not raw:
                        continue
                    sse_lines.append(raw)
                    if raw.startswith("data: ") and "cascade_triggered" in raw:
                        try:
                            cascade_evt = json.loads(raw[6:])
                        except Exception:
                            pass
                    if raw.startswith("data: [DONE]"):
                        break

    print(f"[{label}] HTTP_STATUS={http_status} SSE_LINES={len(sse_lines)}")
    interesting = [
        l for l in sse_lines
        if any(
            k in l
            for k in (
                "cascade_triggered",
                "rounds_exhausted",
                "saved",
                "revise_skipped",
                "revise_error",
                "error",
            )
        )
    ]
    for l in interesting[:10]:
        print(f"[{label}]  ", l[:240])
    if cascade_evt is None:
        print(f"[{label}] cascade_evt=NONE")
    else:
        print(
            f"[{label}] cascade_evt=",
            json.dumps(cascade_evt, ensure_ascii=False),
        )
    return RoundResult(
        http_status=http_status,
        sse_total_lines=len(sse_lines),
        cascade_evt=cascade_evt,
    )


# --- Assertions -------------------------------------------------------------


def _assert_round1_fresh(r: RoundResult) -> None:
    assert r.http_status == 200, f"round1: bad http {r.http_status}"
    evt = r.cascade_evt
    assert evt is not None, "round1: no cascade_triggered SSE event"
    assert evt.get("candidates_planned") == 1, f"round1: candidates_planned {evt}"
    assert evt.get("tasks_inserted") == 1, f"round1: tasks_inserted {evt}"
    assert evt.get("duplicates") == 0, f"round1: duplicates {evt}"
    assert evt.get("dispatched") == 1, f"round1: dispatched {evt}"
    assert isinstance(evt.get("task_ids"), list) and len(evt["task_ids"]) == 1, evt


def _assert_round2_idempotent(r: RoundResult) -> None:
    assert r.http_status == 200, f"round2: bad http {r.http_status}"
    evt = r.cascade_evt
    assert evt is not None, "round2: no cascade_triggered SSE event"
    # Planner is pure -> still produces the same candidate.
    assert evt.get("candidates_planned") == 1, f"round2: candidates_planned {evt}"
    # ON CONFLICT DO NOTHING -> 0 new rows, 1 dup, 0 newly-dispatched.
    assert evt.get("tasks_inserted") == 0, f"round2: tasks_inserted {evt}"
    assert evt.get("duplicates") == 1, f"round2: duplicates {evt}"
    assert evt.get("dispatched") == 0, f"round2: dispatched {evt}"
    assert evt.get("task_ids") in ([], None), f"round2: task_ids {evt}"


# --- Main -------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=2, choices=(1, 2))
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip the final DELETE; useful when debugging the worker path.",
    )
    args = parser.parse_args()

    n0 = await _delete_cascade_rows()
    print(f"PRECLEAN deleted={n0}")

    token = await _read_token()

    # --- Round 1: fresh insert + worker terminal -----------------------
    r1 = await _run_one_round(token, label="round1")
    _assert_round1_fresh(r1)
    rows_after_r1 = await _list_cascade_rows()
    print(f"ROWS_AFTER_ROUND1 count={len(rows_after_r1)}")
    for row in rows_after_r1:
        print("  ", row)
    assert len(rows_after_r1) == 1, f"round1: expected 1 row, got {rows_after_r1}"

    terminal = await _wait_for_terminal(initial_count=1)
    print(f"WORKER_TERMINAL={terminal}")
    assert terminal is not None, "round1: worker did not reach a terminal status in time"
    assert terminal[0] == "skipped", (
        f"round1: expected 'skipped' (handler stub), got {terminal}"
    )
    # error_message column carries the stub reason; current value
    # 'outline_handler_not_implemented' is informational and may evolve
    # when the real outline regenerator lands.
    assert terminal[2] and "outline" in terminal[2].lower(), (
        f"round1: unexpected error_message {terminal!r}"
    )

    # --- Round 2: idempotent dedup -------------------------------------
    if args.rounds >= 2:
        r2 = await _run_one_round(token, label="round2")
        _assert_round2_idempotent(r2)
        rows_after_r2 = await _list_cascade_rows()
        print(f"ROWS_AFTER_ROUND2 count={len(rows_after_r2)}")
        for row in rows_after_r2:
            print("  ", row)
        assert len(rows_after_r2) == 1, (
            f"round2: expected unchanged row count 1, got {rows_after_r2}"
        )
        # Same row id as r1, status still 'skipped' (worker not re-invoked).
        assert rows_after_r2[0][0] == rows_after_r1[0][0], (
            "round2: cascade_tasks row id changed -- ON CONFLICT DO NOTHING violated"
        )
        assert rows_after_r2[0][3] == "skipped", (
            f"round2: row status mutated unexpectedly: {rows_after_r2[0]}"
        )

    # --- Cleanup -------------------------------------------------------
    if args.no_cleanup:
        print("NO_CLEANUP: cascade_tasks rows preserved for inspection")
    else:
        n_clean = await _delete_cascade_rows()
        print(f"POSTCLEAN deleted={n_clean}")

    print(f"OK_C4_6_E2E_SMOKE rounds={args.rounds}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
