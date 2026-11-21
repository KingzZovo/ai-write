# Handoff — 2026-05-17 retry-singleflight

## 1. 速览

- Repo: `/root/ai-write`
- Branch: `main`
- Base before fix: `6dda3c9 feat(retry): LLM transient-failure auto-retry + wave-batching`
- Credentials: local docker stack; Postgres `postgres/postgres`, DB `aiwrite`
- Trigger: hourly patrol at 2026-05-17 01:00 Asia/Shanghai

## 2. 本批交付物表格

| sha | PR | 主题 | 验证状态 |
|---|---|---|---|
| pending | local hotfix | retry task single-flight + smaller wave batch | py_compile pass; container deploy pass; health ok; active retry reduced to 1 |

## 3. 接手第一件事的 cmd 序列

```bash
cd /root/ai-write
git status --short
git diff --stat
docker exec ai-write-celery-worker-1 celery -A app.tasks.celery_app inspect active reserved scheduled
```

## 4. 待做任务 A 详述

确认《赤心巡天》补全是否只剩 0-1 个 retry wave 活跃：

```bash
docker exec ai-write-celery-worker-1 celery -A app.tasks.celery_app inspect active reserved scheduled
```

预期：同一 book_id `0a543b1d-19fe-4e03-986e-42844feb36ee` 不再出现 4 个同时 active 的 `retry_reference_book_missing_branches`。

## 5. 待做任务 B 详述

继续按小时巡检进度，不要因 codex `model_cooldown` / `auth_not_found` 调整 provider 配置。

```bash
BID=0a543b1d-19fe-4e03-986e-42844feb36ee
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -tAc "SELECT (SELECT count(*) FROM style_profile_cards WHERE book_id='$BID'), (SELECT count(*) FROM beat_sheet_cards WHERE book_id='$BID'), 22941"
```

## 6. 关键 ID / endpoint / schema 速查

- Book: `0a543b1d-19fe-4e03-986e-42844feb36ee`（《赤心巡天》）
- Redis lock: `decompile_retry:lock:{book_id}`
- Env: `DECOMPILE_RETRY_WAVE_BATCH` default `50`; `DECOMPILE_RETRY_LOCK_TTL` default `10800`

## 7. 已知陷阱 / shell gotcha

- `docker logs --since` 在此机器常超时，用 `--tail`。
- 长命令放后台写 `/tmp/*.log`。
- 不要用 `pkill -f python/curl`，需要杀进程时用明确 PID。

## 8. 历史临时文件 / 脚本清单

- `/tmp/aiwrite_patrol_20260517_0100.log`
- `/tmp/aiwrite_patrol_recheck_20260517_0100.log`
- `/tmp/aiwrite_patrol_active_20260517_0100.log`
- `/tmp/fix_retry_singleflight.py`

## 9. 本批文件改动点速查

- `backend/app/services/reference_ingestor.py`: default retry wave batch `250 -> 50` with cooldown rationale.
- `backend/app/tasks/__init__.py`: Redis single-flight lock around `retry_reference_book_missing_branches`.
- `docs/RUNBOOK.md`: added §0.1 ops notes, verification commands, reconciliation SQL.
- `docs/PROGRESS.md`: top banner pointing here.

## 10. 合并 PR 模板（4 段式）

Context: retry waves for the same reference book were redelivered and ran concurrently after exceeding Redis broker visibility timeout under provider cooldown.

Change: add Redis single-flight lock per book and lower default retry wave batch size to 50; update runbook/progress/handoff docs.

Verification: `python -m py_compile` for touched backend files; deploy to backend/celery containers; inspect active Celery tasks and DB progress.

Docs updated: `docs/RUNBOOK.md`, `docs/PROGRESS.md`, `docs/HANDOFF_2026-05-17_retry-singleflight.md`.

## 11. EOL

2026-05-17 01:00 patrol result after deploy: backend health ok, touched files py_compile ok, Celery active retry tasks reduced from 4 to 1 for the Chixin book.

End of handoff.
