#!/usr/bin/env python3
"""Bulk-generate chapter content (正文) for chapters in a given volume.

Reads pending chapter ids from PG (where status != 'completed' or content_text
short), then POSTs SSE /api/generate/chapter and waits for the saved event.
Designed to be run via nohup outside the MCP 360s task cap.

Usage:
    nohup python3 scripts/bulk_generate_chapters.py \\
        --project c5480585-78f0-44cd-b41e-c8b8348934d7 \\
        --volume-idx 1 \\
        --concurrency 3 \\
        > /tmp/bulk_gen_v1.log 2>&1 &

Idempotent: re-running picks up rows still pending (status != completed).
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import aiohttp
import asyncpg

API_BASE = os.environ.get("AIWRITE_API_BASE", "http://127.0.0.1:8080")


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


async def fetch_pending(project_id, volume_idx):
    conn = await asyncpg.connect(pg_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT c.id::text, c.volume_id::text, c.chapter_idx, c.title,
                   coalesce(c.status,''), octet_length(coalesce(c.content_text,''))
            FROM chapters c
            JOIN volumes v ON v.id = c.volume_id
            WHERE v.project_id = $1::uuid
              AND v.volume_idx = $2
              AND (c.status IS NULL OR c.status != 'completed' OR octet_length(coalesce(c.content_text,'')) < 2000)
            ORDER BY c.chapter_idx
            """,
            project_id, volume_idx,
        )
        return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    finally:
        await conn.close()


async def gen_one(session, sem, project_id, ch_id, vol_id, ch_idx, label, stats):
    url = f"{API_BASE}/api/generate/chapter"
    payload = {
        "project_id": project_id,
        "chapter_id": ch_id,
        "volume_id": vol_id,
        "chapter_idx": ch_idx,
        "max_tokens": 6144,
    }
    async with sem:
        for attempt in (1, 2, 3):
            t0 = time.time()
            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=600),
                ) as resp:
                    body = await resp.text()
                    dt = time.time() - t0
                    if resp.status == 200 and '"status": "saved"' in body:
                        # Extract reported word_count
                        word_count = 0
                        try:
                            for line in body.split('\n'):
                                if line.startswith('data:') and 'saved' in line:
                                    payload_ = json.loads(line[5:].strip())
                                    word_count = payload_.get('word_count', 0)
                                    break
                        except Exception:
                            pass
                        stats["done"] += 1
                        log(f"OK  {label} ({dt:.1f}s, {word_count} chars) [{stats['done']}/{stats['total']}]")
                        return
                    else:
                        log(f"FAIL {label} status={resp.status} attempt={attempt} body_tail={body[-200:]!r}")
            except asyncio.TimeoutError:
                log(f"TOUT {label} attempt={attempt} after 600s")
            except Exception as exc:
                log(f"ERR  {label} attempt={attempt} err={exc!r}")
            await asyncio.sleep(5 * attempt)
        stats["failed"] += 1
        log(f"GIVE-UP {label}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--volume-idx", type=int, required=True)
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    log(f"START project={args.project} volume_idx={args.volume_idx} concurrency={args.concurrency} api={API_BASE}")
    pending = await fetch_pending(args.project, args.volume_idx)
    if not pending:
        log("No pending chapters. Exiting.")
        return 0
    log(f"pending={len(pending)}")
    log(f"first3={[(p[2], p[3], p[4], p[5]) for p in pending[:3]]}")
    log(f"last3={[(p[2], p[3], p[4], p[5]) for p in pending[-3:]]}")

    token = mint_jwt()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sem = asyncio.Semaphore(args.concurrency)
    stats = {"done": 0, "failed": 0, "total": len(pending)}

    t_start = time.time()
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [
            gen_one(session, sem, args.project, ch_id, vol_id, ch_idx,
                    f"v{args.volume_idx}c{ch_idx} {title}", stats)
            for (ch_id, vol_id, ch_idx, title, status, content_len) in pending
        ]
        await asyncio.gather(*tasks)

    dt = time.time() - t_start
    log(f"DONE total={stats['total']} ok={stats['done']} failed={stats['failed']} elapsed={dt:.0f}s")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
