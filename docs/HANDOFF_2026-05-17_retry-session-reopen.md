# Handoff — 2026-05-17 retry-session-reopen

## 1. 速览

- Repo: `/root/ai-write`
- Branch: `main`
- Base before fix: `5c5fc1c fix(retry): single-flight reference retry waves`
- Book: `0a543b1d-19fe-4e03-986e-42844feb36ee`（《赤心巡天》）
- Trigger: hourly patrol at 2026-05-17 04:00 Asia/Shanghai

## 2. 本批交付物表格

| sha | PR | 主题 | 验证状态 |
|---|---|---|---|
| pending | local hotfix | retry wave fresh-session final recount | local/container py_compile pending; retry re-dispatch pending |

## 3. 接手第一件事的 cmd 序列

```bash
cd /root/ai-write
git status --short
BID=0a543b1d-19fe-4e03-986e-42844feb36ee
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -tAc "SELECT (SELECT count(*) FROM style_profile_cards WHERE book_id='$BID'), (SELECT count(*) FROM beat_sheet_cards WHERE book_id='$BID'), 22941"
docker exec ai-write-celery-worker-1 celery -A app.tasks.celery_app inspect active reserved scheduled
```

## 4. 待做任务 A 详述

确认补发后的 retry wave 没有再次在尾部抛 `connection is closed`：

```bash
docker logs --tail 4000 ai-write-celery-worker-1 2>&1 \
  | grep -E 'retry_reference_book_missing_branches\[|style_filled|beat_filled|connection is closed' \
  | tail -40
```

预期：看到新 retry task active 或完成后自调度；不再出现新的 `connection is closed`。

## 5. 待做任务 B 详述

继续按小时巡检进度，不要因 codex `model_cooldown` / `auth_not_found` 调整 provider 配置。

## 6. 关键 ID / endpoint / schema 速查

- Redis queue: `celery`
- Celery task: `retry_reference_book_missing_branches`
- Redis lock: `decompile_retry:lock:{book_id}`
- Relevant code: `backend/app/services/reference_ingestor.py::retry_missing_branches`

## 7. 已知陷阱 / shell gotcha

- `docker logs --since` 在此机器常超时，用 `--tail`。
- 长命令放后台写 `/tmp/*.log`。
- 不要用 `pkill -f python/curl`，需要杀进程时用明确 PID。

## 8. 历史临时文件 / 脚本清单

- `/root/ai-write/docs/HANDOFF_2026-05-17.md` 是前序交接文档，当前仍未跟踪。

## 9. 本批文件改动点速查

- `backend/app/services/reference_ingestor.py`: retry 波快照后释放 snapshot AsyncSession；最终对账/metadata 更新改用 fresh session。
- `docs/RUNBOOK.md`: 记录 04:00 根因、修复点、验证命令。
- `docs/PROGRESS.md`: 顶部 banner 指向本文。

## 10. 合并 PR 模板（4 段式）

Context: retry task stopped after single-flight because a long-running wave kept the initial AsyncSession idle long enough for Postgres to close it; final `db.refresh(book)` failed and no next wave was scheduled.

Change: close the snapshot session before long branch work and reopen a fresh session for final status recount/metadata update; update runbook/progress/handoff docs.

Verification: `python -m py_compile`; container py_compile; re-dispatch retry task; inspect Celery active/reserved/scheduled and worker logs.

Docs updated: `docs/RUNBOOK.md`, `docs/PROGRESS.md`, `docs/HANDOFF_2026-05-17_retry-session-reopen.md`.

## 11. EOL

End of handoff.
