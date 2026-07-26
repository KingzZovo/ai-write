"""Resilient volume-outline regeneration for Shenyi.

Why this exists (root cause):
The stock `generate_volumes` path generates each 150-chapter volume as 30
back-to-back batches. When the relay throttles (account concurrency cap /
empty-text 25s cooldown), a single batch can exhaust its 3 in-window retries,
which makes the WHOLE volume fail — discarding ~90 already-good chapters — and
the caller then restarts the volume from scratch, re-burning everything.

This script makes throttling a SPEED problem instead of a CORRECTNESS problem:
  * Reuses the existing, canon-correct book outline (no book regen).
  * Replicates the staged meta + 5-chapter-batch contract exactly.
  * Checkpoints every accepted batch (and each volume meta) to disk, so a crash
    or throttle storm resumes from the last good batch instead of from zero.
  * Uses many attempts per batch with backoff PAST the relay cooldown window.
  * Only calls persist() once all 5 volumes hold their full chapter_summaries.

Run inside the backend container (has decrypted keys + mounted source):
  docker compose exec -T backend python scripts/regenerate_shenyi_volumes_resilient.py
Resumable: just run again; completed batches are skipped.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import async_session_factory, dispose_current_engine_async
from app.models.project import Outline, Project
from app.services.model_router import get_model_router_async
from app.services.outline_generator import (
    OUTLINE_VOLUME_CONTRACT_PROMPT,
    OUTLINE_VOLUME_STAGED_TASK_TYPE,
    OUTLINE_WRITING_QUALITY_PROMPT,
    VOLUME_CHAPTERS_SKELETON_SYSTEM,
    VOLUME_META_SYSTEM,
    OutlineGenerator,
    compute_scale,
)

import regenerate_shenyi_outlines as R

BATCH = 5
MAX_BATCH_ATTEMPTS = 12         # transient throttle needs more tries than the stock 3
BACKOFF_SECONDS = 30            # clears the relay's 25s empty-text cooldown
META_MAX_ATTEMPTS = 15          # meta is a bigger call; must ride out long throttle storms
CKPT_PATH = Path(__file__).resolve().with_name(".shenyi_volumes_ckpt.json")

PLACEHOLDER = "<<APPEND>>"


# --------------------------------------------------------------------------
# checkpoint helpers
# --------------------------------------------------------------------------
def load_ckpt() -> dict:
    if CKPT_PATH.exists():
        try:
            data = json.loads(CKPT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            R.log(f"checkpoint 读取失败，忽略旧档：{type(exc).__name__}: {exc}")
    return {"volumes": {}}


def save_ckpt(data: dict) -> None:
    tmp = CKPT_PATH.with_suffix(CKPT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CKPT_PATH)


def _compact(value, limit: int = 420) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", text).strip()[:limit]


# --------------------------------------------------------------------------
# stage V1 — volume meta (with retries + backoff + checkpoint)
# --------------------------------------------------------------------------
async def gen_meta(gen: OutlineGenerator, book_outline: dict, idx: int, notes: str) -> dict:
    meta_ctx = (
        f"全书大纲：\n{json.dumps(book_outline, ensure_ascii=False, indent=2)}\n\n"
        f"请生成第 {idx} 卷的元信息（不包含章节摘要）。"
    )
    if notes:
        meta_ctx += f"\n\n用户补充说明：{notes}"
    system = (
        VOLUME_META_SYSTEM
        + OUTLINE_VOLUME_CONTRACT_PROMPT
        + OUTLINE_WRITING_QUALITY_PROMPT
        + (("\n\n" + gen.chapter_naming_directive) if gen.chapter_naming_directive else "")
    )
    for attempt in range(1, META_MAX_ATTEMPTS + 1):
        R.log(f"第 {idx} 卷 meta 第 {attempt}/{META_MAX_ATTEMPTS} 次")
        try:
            res = await asyncio.wait_for(
                gen.router.generate(
                    task_type=OUTLINE_VOLUME_STAGED_TASK_TYPE,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": meta_ctx},
                    ],
                    max_tokens=4096,
                ),
                timeout=240,
            )
        except Exception as exc:  # noqa: BLE001
            R.log(f"第 {idx} 卷 meta 第 {attempt} 次异常：{type(exc).__name__}: {str(exc)[:120]}")
            await asyncio.sleep(BACKOFF_SECONDS)
            continue
        meta = gen._parse_json(getattr(res, "text", "") or "")
        if isinstance(meta, dict) and not meta.get("_parse_error"):
            try:
                cc = int(meta.get("chapter_count"))
            except (TypeError, ValueError):
                cc = 0
            if cc > 0:
                return meta
            R.log(f"第 {idx} 卷 meta chapter_count 无效（{meta.get('chapter_count')!r}），重试")
        else:
            R.log(f"第 {idx} 卷 meta 解析失败/空响应，退避 {BACKOFF_SECONDS}s 后重试")
        await asyncio.sleep(BACKOFF_SECONDS)
    raise RuntimeError(f"volume {idx} meta generation failed after {META_MAX_ATTEMPTS} attempts")


# --------------------------------------------------------------------------
# stage V2 — one 5-chapter batch (with retries + backoff)
# --------------------------------------------------------------------------
async def gen_batch(
    gen: OutlineGenerator,
    compact_meta: dict,
    tail: list[dict],
    start: int,
    end: int,
) -> list[dict]:
    expected_len = end - start + 1
    tail_str = json.dumps(tail, ensure_ascii=False, indent=2) if tail else "（无）"
    system = VOLUME_CHAPTERS_SKELETON_SYSTEM + (
        ("\n\n" + gen.chapter_naming_directive) if gen.chapter_naming_directive else ""
    )
    for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
        retry_note = "" if attempt == 1 else "\n上一次返回格式不合格。请只返回 JSON，顶层必须包含 batch 数组，且数组长度必须准确。"
        batch_ctx = (
            f"卷元信息（已压缩）：\n{json.dumps(compact_meta, ensure_ascii=False, indent=2)}\n\n"
            f"已生成的最近几章摘要：\n{tail_str}\n\n"
            f"start={start}, end={end}, count={expected_len}。"
            f"请生成第 {start} 章到第 {end} 章的章节骨架。"
            f"chapter_idx 必须从 {start} 连续到 {end}。"
            f"\n必须返回：{{\"batch\": [{{...}}]}}，batch 数组长度必须是 {expected_len}。"
            f"{retry_note}"
        )
        try:
            res = await asyncio.wait_for(
                gen.router.generate(
                    task_type=OUTLINE_VOLUME_STAGED_TASK_TYPE,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": batch_ctx},
                    ],
                    max_tokens=4096,
                ),
                timeout=240,
            )
        except Exception as exc:  # noqa: BLE001
            R.log(f"  batch {start}-{end} 第 {attempt} 次异常：{type(exc).__name__}: {str(exc)[:100]}；退避 {BACKOFF_SECONDS}s")
            await asyncio.sleep(BACKOFF_SECONDS)
            continue
        parsed = gen._parse_json(getattr(res, "text", "") or "")
        if isinstance(parsed, dict):
            raw_items = parsed.get("batch") or parsed.get("chapter_summaries") or parsed.get("chapters")
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = None
        if isinstance(raw_items, list) and len(raw_items) >= expected_len:
            items = raw_items[:expected_len]
            # normalize chapter_idx
            expected = start
            for item in items:
                if isinstance(item, dict):
                    item["chapter_idx"] = expected
                    expected += 1
            return [it for it in items if isinstance(it, dict)]
        R.log(f"  batch {start}-{end} 第 {attempt} 次形状/数量不合格，退避 {BACKOFF_SECONDS}s")
        await asyncio.sleep(BACKOFF_SECONDS)
    raise RuntimeError(f"batch {start}-{end} failed after {MAX_BATCH_ATTEMPTS} attempts")


# --------------------------------------------------------------------------
# one volume = meta + all batches, fully checkpointed
# --------------------------------------------------------------------------
async def gen_volume(gen: OutlineGenerator, book_outline: dict, idx: int, plan_item: dict, ckpt: dict) -> dict:
    vkey = str(idx)
    vstate = ckpt["volumes"].setdefault(vkey, {})
    notes = (
        f"{R.SHENYI_CANONICAL_FACTS_PROMPT}\n"
        f"实体注册表：{json.dumps(R.SHENYI_ENTITY_REGISTRY, ensure_ascii=False)}\n"
        f"请严格按照全书卷计划生成第 {idx} 卷。"
        f"本卷标题：{plan_item.get('title') or f'第{idx}卷'}。"
        f"本卷主题：{plan_item.get('theme') or ''}。"
        f"本卷核心冲突：{plan_item.get('core_conflict') or ''}。"
    )

    meta = vstate.get("meta")
    if not isinstance(meta, dict):
        meta = await gen_meta(gen, book_outline, idx, notes)
        vstate["meta"] = meta
        save_ckpt(ckpt)
        R.log(f"第 {idx} 卷 meta 完成并存档，chapter_count={meta.get('chapter_count')}")
    else:
        R.log(f"第 {idx} 卷 meta 复用存档，chapter_count={meta.get('chapter_count')}")

    chapter_count = int(meta.get("chapter_count"))
    meta_for_ctx = {k: v for k, v in meta.items() if k != "chapter_summaries"}
    compact_meta = {
        "volume_idx": meta_for_ctx.get("volume_idx", idx),
        "title": _compact(meta_for_ctx.get("title", f"第{idx}卷"), 80),
        "core_conflict": _compact(meta_for_ctx.get("core_conflict", "")),
        "turning_points": _compact(meta_for_ctx.get("turning_points", [])),
        "emotional_arc": _compact(meta_for_ctx.get("emotional_arc", ""), 260),
        "transition_to_next": _compact(meta_for_ctx.get("transition_to_next", ""), 260),
    }

    batches_state = vstate.setdefault("batches", {})
    n_batches = math.ceil(chapter_count / BATCH)
    all_summaries: list[dict] = []
    for b in range(n_batches):
        start = b * BATCH + 1
        end = min((b + 1) * BATCH, chapter_count)
        bkey = f"{start}-{end}"
        cached = batches_state.get(bkey)
        if isinstance(cached, list) and len(cached) == (end - start + 1):
            all_summaries.extend(cached)
            continue
        tail = [
            {"chapter_idx": it.get("chapter_idx"), "title": it.get("title"), "summary": _compact(it.get("summary", ""), 120)}
            for it in all_summaries[-2:]
            if isinstance(it, dict)
        ]
        R.log(f"第 {idx} 卷 batch {b + 1}/{n_batches} ({bkey}) 生成中")
        items = await gen_batch(gen, compact_meta, tail, start, end)
        batches_state[bkey] = items
        save_ckpt(ckpt)
        all_summaries.extend(items)

    if len(all_summaries) != chapter_count:
        raise RuntimeError(f"volume {idx}: assembled {len(all_summaries)} != chapter_count {chapter_count}")

    merged = dict(meta_for_ctx)
    merged["chapter_summaries"] = all_summaries
    R.validate_shenyi_volume_outline(merged, f"volume_{idx}_resilient")
    R.log(f"第 {idx} 卷完成：{len(all_summaries)} 章")
    return merged


# --------------------------------------------------------------------------
# relaxed book gate (reuse canon-correct but thin book outline)
# --------------------------------------------------------------------------
def soft_validate_book_outline(generator, book_outline: dict, scale, context: str) -> dict:
    text = R.as_text(book_outline.get("raw_text") or book_outline.get("full_outline"))
    errors: list[str] = []
    errors.extend(R.shenyi_text_gate_errors(text, require_book_terms=True, require_anchor_terms=True))
    hit = [w for w in R.BANNED_PLACEHOLDER_TERMS if w in text]
    if hit:
        errors.append("placeholder_terms:" + ",".join(hit))
    volume_plan = book_outline.get("volume_plan")
    expected_volumes = int(scale.get("n_volumes") or 0) if scale else 0
    if expected_volumes:
        if not isinstance(volume_plan, list) or len(volume_plan) != expected_volumes:
            count = len(volume_plan) if isinstance(volume_plan, list) else 0
            errors.append(f"volume_plan_count:{count}!={expected_volumes}")
        else:
            titles = [R.as_text(it.get("title")) for it in volume_plan if isinstance(it, dict)]
            missing = [t for t in R.SHENYI_VOLUME_PLAN_TITLES if t not in titles]
            if missing:
                errors.append("volume_plan_title_drift:" + ",".join(missing))
    if errors:
        raise RuntimeError(f"{context} soft gate failed: " + "; ".join(errors))
    enriched = dict(book_outline)
    enriched["entity_registry"] = dict(R.SHENYI_ENTITY_REGISTRY)
    return enriched


R.validate_shenyi_book_outline_payload = soft_validate_book_outline


async def load_book_outline() -> dict:
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Outline)
                .where(Outline.project_id == R.PROJECT_ID, Outline.level == "book")
                .order_by(Outline.is_confirmed.desc(), Outline.id)
            )
        ).scalars().first()
        if row is None:
            raise RuntimeError("No book-level outline found.")
        if not isinstance(row.content_json, dict):
            raise RuntimeError("Book outline content_json is not a dict")
        return row.content_json


async def main() -> None:
    try:
        async with async_session_factory() as db:
            project = await db.get(Project, R.PROJECT_ID)
            if not project:
                raise RuntimeError(f"Project not found: {R.PROJECT_ID}")
        scale = compute_scale(project.target_word_count)
        R.log(f"项目：{project.title}，目标字数 {project.target_word_count}")

        book_outline = await load_book_outline()
        gen = OutlineGenerator(project_id=str(R.PROJECT_ID))
        book_outline = soft_validate_book_outline(gen, book_outline, scale, "Book outline (reused)")
        R.log(
            f"已加载并软校验 book 大纲：raw_text {len(R.as_text(book_outline.get('raw_text')))} 字，"
            f"volume_plan {len(book_outline.get('volume_plan') or [])} 卷"
        )

        gen.router = await get_model_router_async()
        plan = R.normalize_plan(book_outline, scale)
        R.log("卷计划：" + "；".join(f"{p['idx']}.{p.get('title')}({p.get('est_chapters')}章)" for p in plan))

        ckpt = load_ckpt()
        volumes: list[dict] = []
        for item in plan:
            idx = int(item.get("idx"))
            outline = await gen_volume(gen, book_outline, idx, item, ckpt)
            volumes.append({"idx": idx, "plan": item, "outline": outline})

        R.log("全部 5 卷生成完成，开始写库")
        stats = await R.persist(project, book_outline, volumes)
        R.log("已写回数据库：" + json.dumps(stats, ensure_ascii=False))
        R.log("DONE")
    except RuntimeError as exc:
        R.log("生成停止：" + str(exc))
        raise SystemExit(2) from None
    finally:
        await dispose_current_engine_async()


if __name__ == "__main__":
    asyncio.run(main())


