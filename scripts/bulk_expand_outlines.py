#!/usr/bin/env python3
"""Bulk-expand chapter outlines to the new process-narrative schema.

Reads pending chapter ids from PG (octet_length(outline_json->>summary) < 800),
then POSTs /api/projects/{project_id}/chapters/{chapter_id}/outline/expand at
concurrency=5. Run via nohup outside the MCP 360s task cap.

Usage:
    nohup python3 scripts/bulk_expand_outlines.py \\
        --project c5480585-78f0-44cd-b41e-c8b8348934d7 \\
        --concurrency 5 \\
        > /tmp/bulk_expand.log 2>&1 &

Idempotent: re-running picks up rows still pending.
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime

import aiohttp
import asyncpg

API_BASE = os.environ.get("AIWRITE_API_BASE", "http://127.0.0.1:8080")
PENDING_THRESHOLD = 800


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}", flush=True)


def mint_jwt():
    return subprocess.check_output(
        ["docker", "exec", "ai-write-backend-1", "python", "-c",
         "from app.api.auth import _create_token; print(_create_token(os.environ.get('AUTH_USERNAME', 'admin')))"],
        text=True,
    ).strip()


def pg_dsn():
    pwd = subprocess.check_output(
        "grep -E '^POSTGRES_PASSWORD=' /root/ai-write/.env | cut -d= -f2-",
        shell=True, text=True,
    ).strip()
    return f"postgresql://postgres:{pwd}@127.0.0.1:5432/aiwrite"


async def fetch_pending(project_id):
    conn = await asyncpg.connect(pg_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT c.id::text, v.volume_idx, c.chapter_idx, c.title
            FROM chapters c
            JOIN volumes v ON v.id = c.volume_id
            WHERE v.project_id = $1::uuid
              AND octet_length(coalesce(c.outline_json->>'summary','')) < $2
            ORDER BY v.volume_idx, c.chapter_idx
            """,
            project_id, PENDING_THRESHOLD,
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    finally:
        await conn.close()


async def expand_one(session, sem, project_id, chapter_id, label, stats):
    url = f"{API_BASE}/api/projects/{project_id}/chapters/{chapter_id}/outline/expand"
    async with sem:
        t0 = time.time()
        for attempt in (1, 2, 3):
            try:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    body_text = await resp.text()
                    dt = time.time() - t0
                    if resp.status == 200:
                        ok = '"summary"' in body_text and len(body_text) > 800
                        stats["done"] += 1
                        marker = "OK " if ok else "OK?"
                        log(f"{marker} {label} ({dt:.1f}s) [{stats['done']}/{stats['total']}]")
                        return
                    else:
                        log(f"FAIL {label} status={resp.status} attempt={attempt} body={body_text[:200]}")
            except Exception as exc:
                log(f"ERR  {label} attempt={attempt} err={exc!r}")
            await asyncio.sleep(2 * attempt)
        stats["failed"] += 1
        log(f"GIVE-UP {label}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    log(f"START project={args.project} concurrency={args.concurrency} api={API_BASE}")
    pending = await fetch_pending(args.project)
    if not pending:
        log("No pending chapters.")
        return 0
    log(f"pending={len(pending)} first3={pending[:3]} last3={pending[-3:]}")

    token = mint_jwt()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sem = asyncio.Semaphore(args.concurrency)
    stats = {"done": 0, "failed": 0, "total": len(pending)}

    t_start = time.time()
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [
            expand_one(session, sem, args.project, ch_id,
                       f"v{v}c{c} {title}", stats)
            for (ch_id, v, c, title) in pending
        ]
        await asyncio.gather(*tasks)

    dt = time.time() - t_start
    log(f"DONE total={stats['total']} ok={stats['done']} failed={stats['failed']} elapsed={dt:.0f}s")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
