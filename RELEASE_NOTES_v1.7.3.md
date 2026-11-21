# v1.7.3 — `stream_by_route` `NameError` hotfix + audit baseline

Release date: 2026-04-28
Git tag: `v1.7.3`
Branch: `feature/v1.0-big-bang`

## TL;DR

Patches a P0 runtime bug introduced during v1.7.1's `task_type` propagation
refactor: `ModelRouter.stream_by_route` referenced an undefined `task_type`
identifier whenever its `_log_meta is None` branch ran (i.e. any caller that
streamed without supplying a logging meta dict). The streaming path therefore
raised `NameError: name 'task_type' is not defined` immediately on first chunk
for the bare-call use case.

Also publishes the v1.7.2 audit baseline (`docs/AUDIT_BASELINE_v1.7.2.md`)
which is what surfaced the bug.

## What changed

### Backend (P0 hotfix)

- `backend/app/services/model_router.py` — `stream_by_route` method
  signature now includes `task_type: str = "by_route_stream"` (symmetric to
  `generate_by_route`). The `_log_meta` branch falls back to that parameter
  via `meta.pop("task_type", task_type)` instead of the previous
  literal default that was erased mid-refactor.
- `backend/tests/test_v173_stream_by_route_nameerror.py` — two regression
  tests using the established `_CountingProvider`/`_StreamProvider` pattern:
  - `test_stream_by_route_no_log_meta_no_nameerror` — calls without
    `_log_meta`, asserts chunks emit and `task_type="by_route_stream"`
    propagates to the provider.
  - `test_stream_by_route_no_log_meta_explicit_task_type_propagates` —
    asserts an explicitly passed `task_type` arrives at the provider.

### Docs

- `docs/AUDIT_BASELINE_v1.7.2.md` — full v1.7.2 audit baseline:
  ruff (72 findings), mypy (550 errors / 56 files), pytest --cov
  (252 passed / overall 34 %), pip-audit (4 build-tool vulns), npm audit
  (4 moderate), prioritised P0–P3 fix list, and a roadmap that motivates
  this hotfix and the upcoming P0 unit-test gap fills.

## Verification

- `ruff check backend/app/services/model_router.py --select F821` → all clean.
- `pytest backend/tests/test_v173_stream_by_route_nameerror.py -v` → **2 passed**.
- `pytest backend/tests/ -q --ignore=tests/integration` → **254 passed** in 5.30 s
  (252 → 254, +2 hotfix tests; no regressions).
- `/api/health` → 200 after `docker cp` + `docker restart` of
  `ai-write-backend-1` and `ai-write-celery-worker-1`.

## Migration / impact

- No schema or API contract changes.
- The default `task_type` for unannotated streaming callers stays
  `"by_route_stream"`, identical to the pre-bug intent — Prometheus
  `llm_call_total{task_type="by_route_stream"}` series resumes producing
  samples instead of dying on `NameError` and dropping the call entirely.
- Callers that pass `task_type=` explicitly (or supply it via
  `_log_meta`) continue to take precedence in their respective branches.

## Known follow-ups (deferred to v1.8)

- `stream_by_route` and `stream_with_tier_fallback`'s `_log_meta is None`
  branches still lack the symmetric `time_llm_call` wrap that v1.7.2 Z3 added
  to the non-stream path. Tracked for v1.8.
- 60 % overall test-coverage P0 gap-fill batch (`outline_generator`,
  `chapter_generator`, `style_abstractor`, `feature_extractor`,
  `beat_extractor`) per the v1.7.2 audit baseline.
- Ruff `F` / `E` CI gate enforcement so the next `task_type`-style
  identifier slip lands red in CI rather than blue in production.
