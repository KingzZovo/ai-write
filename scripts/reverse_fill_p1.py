#!/usr/bin/env python3
"""P1 reverse-fill: characters / world_rules / foreshadows.

Designed to run INSIDE the backend container so it can resolve the
encrypted llm_endpoints.api_key via the existing prompt_registry +
model_router code path.

Usage (host):
    docker exec ai-write-backend-1 python -m scripts.reverse_fill_p1 \\
        --project c5480585-78f0-44cd-b41e-c8b8348934d7 \\
        --kind characters \\
        --concurrency 6 \\
        --limit 0

Idempotent: skips entities that already have non-empty profile_json /
already-populated world_rules / already-populated foreshadows.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from uuid import UUID

# Mounted at /app inside the container
sys.path.insert(0, '/app')

from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import Project
from app.services.prompt_registry import run_text_prompt

# Avoid pulling characters/world_rules/foreshadows ORM if we just need raw SQL

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('p1')


def _parse_json_loose(text: str) -> dict | None:
    """Strip markdown fences + repair JSON if needed."""
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        try:
            from json_repair import loads as repair_loads
            return repair_loads(text)
        except Exception:
            return None


async def fetch_book_outline_text(db, project_id: str) -> str:
    rows = (await db.execute(
        sql_text("SELECT content_json FROM outlines WHERE project_id=:pid AND level='book' LIMIT 1"),
        {'pid': project_id},
    )).fetchall()
    if not rows:
        return ''
    cj = rows[0][0] or {}
    if isinstance(cj, str):
        cj = json.loads(cj)
    parts = []
    for k, v in cj.items() if isinstance(cj, dict) else []:
        parts.append(f'## {k}\n{v}')
    return '\n\n'.join(parts)[:10000]


# =========================================================================
# CHARACTER PROFILE REVERSE-FILL
# =========================================================================

CHAR_SYSTEM = (
    "你是一位小说人物设定提取专家。给定人物名称和在文中出现的多个上下文片段，"
    "请提取该人物的完整设定。输出严格的 JSON，仅包含以下项，未提及的项目返回空字符串：\n"
    '{"identity":"身份或身份名/职业/位阶",'
    '"personality":"性格特征与主要性格标签",'
    '"appearance":"外貌特征",'
    '"abilities":"身手/能力/专长",'
    '"biography":"关键生平/背景事件",'
    '"current_status":"当前处境",'
    '"background":"出身/家世/阶层",'
    '"motivation":"动机/驱动力",'
    '"goal":"人物目标",'
    '"emotion":"主要情感底色"}\n'
    "不要输出任何不在文本中体现的信息，不肨撑。若某项信息不足以论断，返回空字符串。"
)


async def fetch_char_contexts(db, project_id: str, name: str, k: int = 4, ctx_chars: int = 350) -> list[str]:
    """Find up to k occurrences of `name` in chapter content_text, return surrounding contexts."""
    rows = (await db.execute(
        sql_text("""
          SELECT c.content_text
          FROM chapters c JOIN volumes v ON v.id=c.volume_id
          WHERE v.project_id=:pid AND c.content_text LIKE :pat
          LIMIT 8
        """),
        {'pid': project_id, 'pat': f'%{name}%'},
    )).fetchall()
    contexts = []
    seen = set()
    for (text,) in rows:
        if not text:
            continue
        idx = 0
        while True:
            i = text.find(name, idx)
            if i < 0 or len(contexts) >= k:
                break
            start = max(0, i - ctx_chars // 2)
            end = min(len(text), i + len(name) + ctx_chars // 2)
            snip = text[start:end].strip()
            sig = snip[:60]
            if sig not in seen and len(snip) > 60:
                seen.add(sig)
                contexts.append(snip)
            idx = i + len(name)
        if len(contexts) >= k:
            break
    return contexts


async def extract_one_character(sem, project_id: str, char_id: str, name: str):
    async with sem:
        async with async_session_factory() as db:
            try:
                contexts = await fetch_char_contexts(db, project_id, name, k=4, ctx_chars=350)
                if not contexts:
                    log.info(f'  [{name}] no occurrences -> skip')
                    return ('skip_no_match', name)
                user = (
                    f'人物名称：{name}\n\n'
                    f'以下是该人物在正文中的 {len(contexts)} 个出现片段：\n\n' +
                    '\n\n---\n\n'.join(f'[片段{i+1}]\n{c}' for i, c in enumerate(contexts)) +
                    '\n\n请输出该人物设定的 JSON。'
                )
                msgs = [
                    {'role': 'system', 'content': CHAR_SYSTEM},
                    {'role': 'user', 'content': user},
                ]
                result = await run_text_prompt(
                    task_type='generation',
                    user_content='',
                    db=db,
                    project_id=project_id,
                    messages=msgs,
                    max_tokens=600,
                    temperature=0.2,
                )
                profile = _parse_json_loose(result.text)
                if not profile or not isinstance(profile, dict):
                    log.warning(f'  [{name}] parse fail: {result.text[:120]!r}')
                    return ('parse_fail', name)
                # Strip out keys with empty values to keep JSON clean
                profile = {k: v for k, v in profile.items() if v and isinstance(v, str)}
                if not profile:
                    log.info(f'  [{name}] empty profile after strip')
                    return ('empty', name)
                await db.execute(
                    sql_text('UPDATE characters SET profile_json=:pj WHERE id=:cid'),
                    {'pj': json.dumps(profile, ensure_ascii=False), 'cid': char_id},
                )
                await db.commit()
                log.info(f'  [{name}] OK ({len(contexts)} ctxs, {len(profile)} fields)')
                return ('ok', name)
            except Exception as e:
                log.exception(f'  [{name}] error: {e}')
                return ('error', name)


async def run_characters(project_id: str, concurrency: int, limit: int):
    async with async_session_factory() as db:
        rows = (await db.execute(
            sql_text("""
              SELECT id, name FROM characters
              WHERE project_id=:pid AND (profile_json::text='{}' OR profile_json IS NULL)
              ORDER BY name
              LIMIT :lim
            """),
            {'pid': project_id, 'lim': (limit or 1000)},
        )).fetchall()
    log.info(f'characters to fill: {len(rows)}')
    sem = asyncio.Semaphore(concurrency)
    tasks = [extract_one_character(sem, project_id, str(r[0]), r[1]) for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    counts = {}
    for r in results:
        if isinstance(r, tuple):
            counts[r[0]] = counts.get(r[0], 0) + 1
    log.info(f'characters done: {counts}')


# =========================================================================
# WORLD RULES EXTRACTION (from book outline + first 5 volume outlines)
# =========================================================================

WORLD_SYSTEM = (
    "你是世界观设定提取专家。阅读提供的全书大纲与分卷大纲，提取 8–20 条世界规则。\n"
    "每条输出 JSON对象: {\"category\": \"分类\", \"rule_text\": \"具体规则描述\"}\n"
    "分类可选: 足践设定/能力体系/魔法体系/势力架构/空间设定/种族设定/组织机构/历史背景/资源设定/什部規则/其它\n"
    "输出严格以 {\"rules\": [...]} 包裹数组。不要添加未在文中明示的规则。"
)


async def run_world_rules(project_id: str):
    async with async_session_factory() as db:
        # Skip if already populated
        cnt = (await db.execute(
            sql_text('SELECT COUNT(*) FROM world_rules WHERE project_id=:pid'),
            {'pid': project_id},
        )).scalar() or 0
        if cnt > 0:
            log.info(f'world_rules already has {cnt} rows; skipping')
            return
        # Build user content from book outline + first 5 volume outlines
        book_text = await fetch_book_outline_text(db, project_id)
        vol_rows = (await db.execute(
            sql_text("""
              SELECT content_json FROM outlines
              WHERE project_id=:pid AND level='volume'
              ORDER BY id LIMIT 5
            """),
            {'pid': project_id},
        )).fetchall()
        vol_parts = []
        for (cj,) in vol_rows:
            if isinstance(cj, str):
                cj = json.loads(cj)
            if isinstance(cj, dict):
                vol_parts.append('\n'.join(f'{k}: {v}' for k, v in cj.items())[:2500])
        user = f'## 全书大纲\n{book_text}\n\n## 代表性分卷大纲\n' + '\n---\n'.join(vol_parts)
        user = user[:18000]
        if not user.strip():
            log.info('no outline content; skipping world_rules'); return
        msgs = [
            {'role': 'system', 'content': WORLD_SYSTEM},
            {'role': 'user', 'content': user},
        ]
        result = await run_text_prompt(
            task_type='generation', user_content='', db=db,
            project_id=project_id, messages=msgs,
            max_tokens=2200, temperature=0.2,
        )
        parsed = _parse_json_loose(result.text)
        rules = []
        if isinstance(parsed, dict):
            rules = parsed.get('rules', [])
        elif isinstance(parsed, list):
            rules = parsed
        log.info(f'extracted {len(rules)} world rules')
        for r in rules:
            if not isinstance(r, dict): continue
            cat = (r.get('category') or '其它')[:100]
            txt = (r.get('rule_text') or '').strip()
            if not txt: continue
            try:
                await db.execute(sql_text("""
                  INSERT INTO world_rules (id, project_id, category, rule_text, created_at, metadata_json)
                  VALUES (gen_random_uuid(), :pid, :cat, :txt, now(), '{}'::json)
                  ON CONFLICT DO NOTHING
                """), {'pid': project_id, 'cat': cat, 'txt': txt})
            except Exception as e:
                log.warning(f'  insert rule failed: {e}')
        await db.commit()
        new_cnt = (await db.execute(sql_text('SELECT COUNT(*) FROM world_rules WHERE project_id=:pid'), {'pid': project_id})).scalar()
        log.info(f'world_rules total now: {new_cnt}')


# =========================================================================
# FORESHADOWS EXTRACTION (from book outline + volume outlines)
# =========================================================================

FOR_SYSTEM = (
    "你是小说伏笔与线索提取专家。阅读全书+分卷大纲，识别 10–25 条明伏笔/暗伏笔/锻造。输出严格 JSON:\n"
    '{"foreshadows": [{"type": "明伏笔|暗伏笔|锻造", "description": "伏笔描述", "planted_chapter": <int>, "resolve_hint": "预计回收点/提示"}, ...]}\n'
    'planted_chapter 为伏笔在哪一章埋下（估计数字，全书 0–549 范围）。不要虐造伏笔，只取大纲中明示的。'
)


async def run_foreshadows(project_id: str):
    async with async_session_factory() as db:
        cnt = (await db.execute(
            sql_text('SELECT COUNT(*) FROM foreshadows WHERE project_id=:pid'),
            {'pid': project_id},
        )).scalar() or 0
        if cnt > 0:
            log.info(f'foreshadows already has {cnt} rows; skipping')
            return
        book_text = await fetch_book_outline_text(db, project_id)
        vol_rows = (await db.execute(
            sql_text("""
              SELECT content_json FROM outlines WHERE project_id=:pid AND level='volume'
              ORDER BY id LIMIT 5
            """),
            {'pid': project_id},
        )).fetchall()
        vol_parts = []
        for (cj,) in vol_rows:
            if isinstance(cj, str): cj = json.loads(cj)
            if isinstance(cj, dict):
                vol_parts.append('\n'.join(f'{k}: {v}' for k, v in cj.items())[:2500])
        user = f'## 全书大纲\n{book_text}\n\n## 代表性分卷大纲\n' + '\n---\n'.join(vol_parts)
        user = user[:18000]
        if not user.strip():
            log.info('no outline content; skipping foreshadows'); return
        msgs = [
            {'role': 'system', 'content': FOR_SYSTEM},
            {'role': 'user', 'content': user},
        ]
        result = await run_text_prompt(
            task_type='generation', user_content='', db=db,
            project_id=project_id, messages=msgs,
            max_tokens=3000, temperature=0.2,
        )
        parsed = _parse_json_loose(result.text)
        items = []
        if isinstance(parsed, dict):
            items = parsed.get('foreshadows', [])
        elif isinstance(parsed, list):
            items = parsed
        log.info(f'extracted {len(items)} foreshadows')
        type_map = {'明伏笔': '明伏笔', '暗伏笔': '暗伏笔', '锻造': '锻造'}
        for it in items:
            if not isinstance(it, dict): continue
            t = type_map.get(it.get('type', '').strip(), '明伏笔')
            desc = (it.get('description') or '').strip()
            try:
                pc = int(it.get('planted_chapter') or 0)
            except Exception:
                pc = 0
            pc = max(0, min(pc, 549))
            if not desc: continue
            try:
                await db.execute(sql_text("""
                  INSERT INTO foreshadows (id, project_id, type, description, planted_chapter, status, created_at, resolve_conditions_json, resolution_blueprint_json)
                  VALUES (gen_random_uuid(), :pid, :t, :d, :pc, 'pending', now(), CAST(:rh AS json), '{}'::json)
                """), {'pid': project_id, 't': t, 'd': desc, 'pc': pc,
                       'rh': json.dumps({'hint': it.get('resolve_hint','')}, ensure_ascii=False)})
            except Exception as e:
                log.warning(f'  insert foreshadow failed: {e}')
        await db.commit()
        new_cnt = (await db.execute(sql_text('SELECT COUNT(*) FROM foreshadows WHERE project_id=:pid'), {'pid': project_id})).scalar()
        log.info(f'foreshadows total now: {new_cnt}')


# =========================================================================
# main
# =========================================================================

async def main():
    p = argparse.ArgumentParser()
    p.add_argument('--project', required=True)
    p.add_argument('--kind', required=True, choices=['characters', 'world_rules', 'foreshadows', 'all'])
    p.add_argument('--concurrency', type=int, default=6)
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()

    t0 = time.time()
    if args.kind in ('characters', 'all'):
        await run_characters(args.project, args.concurrency, args.limit)
    if args.kind in ('world_rules', 'all'):
        await run_world_rules(args.project)
    if args.kind in ('foreshadows', 'all'):
        await run_foreshadows(args.project)
    log.info(f'done in {time.time()-t0:.1f}s')


if __name__ == '__main__':
    asyncio.run(main())
