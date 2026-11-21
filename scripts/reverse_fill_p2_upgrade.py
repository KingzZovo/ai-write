#!/usr/bin/env python3
"""PR-FIX-PROJSET-P2: 升维伏笔抽取 / 写法风格 v9 / 剧情架构 v2。

Mode 说明：
  --kind foreshadows     重抽伏笔（按卷分批，target 100+）
  --kind style_v9        升维《江南 综合写法》 style_profile
  --kind structure_v2    升维 龙族 reference_book.metadata_json.plot_structure_v2
  --kind all             三者都跑
运行在后端容器内（复用 prompt_registry 加密 key 与 router）。
"""
from __future__ import annotations
import argparse, asyncio, json, logging, os, re, sys, time, uuid
from typing import Any

sys.path.insert(0, '/app')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('p2')

from sqlalchemy import select, text as _sql_text
from app.db.session import async_session_factory
from app.services.prompt_registry import run_text_prompt
from app.services.style_profile_resolver import get_or_create_book_profile

DEFAULT_PROJECT = 'c5480585-78f0-44cd-b41e-c8b8348934d7'
# PR-BOOK-PROFILE-BIND: --book is now required and --style auto-resolves from it


def _parse_json_loose(txt: str) -> Any:
    if not txt: return None
    s = txt.strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\n', '', s)
        s = re.sub(r'\n```\s*$', '', s)
    try:
        return json.loads(s)
    except Exception:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(s))
        except Exception as e:
            log.warning('json parse failed: %s', e)
            return None



async def _llm(messages):
    """Helper: run a chat-style messages call via prompt_registry, with an own DB session."""
    async with async_session_factory() as db:
        result = await run_text_prompt(
            'generation', '', db, messages=messages
        )
    return result.text if result else ''


# =============================================================================
# 1) Foreshadows v2 — 按卷分批，更密集
# =============================================================================
FORESHADOW_SYSTEM = """你是专业的小说伏笔分析师。从提供的分卷上下文中尽量多地识别『已埋伏但尚未收』的伏笔，输出纯JSON。

requirements:
- 识别该卷出现、但尚未完全展开/解释的伏笔，包括：
  * 人物身份与背景伏笔（身世之谜、隐藏身份、未说出口的过去）
  * 关系伏笔（未被揭示的人物联系、家族谱系、恨意、恋情伏笔）
  * 设定伏笔（世界规则、技术、魔法、处的使用限制与未知后果）
  * 谜团伏笔（奇怪现象、丢失的物品、未解之谜、奇怪对象的来历）
  * 势力伏笔（未出场组织、阶发事件伏笔、未成熟冲突）
  * 道具/物件伏笔（重要物品的来历、未激活的能力）
  * 心理伏笔（未被说出口的动机、压抑的情感、未打开的心结）
- 包括明伏笔（读者能意识到的）与暗伏笔（隐藏依靠后文才能发现）
- 仅记录「当前本卷已埋下、但在本卷范围内尚未被完全揭示/收束」的项。
- 同一伏笔不要重复。描述要独立、具体。

output format:
{
  "foreshadows": [
    {
      "type": "明伏笔|暗伏笔",
      "category": "人物|关系|设定|谜团|势力|道具|心理",
      "description": "具体描述（40-100字）",
      "planted_chapter": 本卷卷内位置估计，是该卷第N章（0-149）,
      "hint": "可能在何处收线的提示（0-50字）",
      "narrative_proximity": 0.0-1.0估值（越接近全书末尾的伏笔越接近 1.0）
    }
  ]
}
请仅输出JSON，不要多余说明。尽量多抽，本轮目标 25-40 条。"""

async def extract_foreshadows_per_volume(project_id: str):
    async with async_session_factory() as db:
        rows = (await db.execute(_sql_text("""
            SELECT v.id, v.volume_idx, v.title FROM volumes v
            WHERE v.project_id=:pid ORDER BY v.volume_idx
        """), {'pid': project_id})).all()
    log.info('Volumes: %d', len(rows))

    total_inserted = 0
    # 先体面清除之前的 14 条（避免重复）
    async with async_session_factory() as db:
        await db.execute(_sql_text("DELETE FROM foreshadows WHERE project_id=:pid"), {'pid': project_id})
        await db.commit()
    log.info('Cleared existing foreshadows for re-extraction')

    for vid, vidx, vtitle in rows:
        async with async_session_factory() as db:
            chs = (await db.execute(_sql_text("""
                SELECT chapter_idx, title, content_text, summary FROM chapters
                WHERE volume_id=:vid AND content_text IS NOT NULL AND length(content_text) > 200
                ORDER BY chapter_idx
            """), {'vid': str(vid)})).all()
        if not chs:
            log.warning('vol %s no chapters', vidx); continue
        # 集中全卷内容，太长则采样
        full = []
        for cidx, ctitle, ctext, csum in chs:
            full.append(f"【章 {cidx}】{ctitle}\n" + (csum or '')[:600] + "\n\n" + (ctext or '')[:1800])
        material = '\n---\n'.join(full)
        # 控制在 ~80k 字以下
        if len(material) > 80000:
            # 采样 keep first/mid/last
            third = len(material)//3
            material = material[:third] + '\n[...]\n' + material[third:2*third] + '\n[...]\n' + material[2*third:]
            material = material[:80000]

        log.info('Vol %s (%s) %d chs %d chars -> LLM', vidx, vtitle, len(chs), len(material))
        t0 = time.time()
        try:
            res = await _llm(messages=[
                    {'role':'system', 'content': FORESHADOW_SYSTEM},
                    {'role':'user', 'content': f"【本卷：第{vidx}卷 《{vtitle}》 ({len(chs)}章)】\n\n{material}\n\n请抽取 25-40 条本卷伏笔。输出JSON。"}
                ])
            data = _parse_json_loose(res or '')
            arr = (data or {}).get('foreshadows') if isinstance(data, dict) else None
            if not arr:
                log.warning('vol %s no foreshadows extracted', vidx); continue
            # 插入
            inserted = 0
            async with async_session_factory() as db:
                for f in arr:
                    typ = (f.get('type') or '伏笔').strip()[:20]
                    desc = (f.get('description') or '').strip()
                    if not desc: continue
                    cat = (f.get('category') or '').strip()[:30]
                    pch_v = f.get('planted_chapter')
                    # 卷内章转为全书章号（需要个 0-549 全局位置）
                    if isinstance(pch_v, (int,float)):
                        pch_local = max(0, min(int(pch_v), len(chs)-1))
                    else:
                        pch_local = len(chs)//2
                    pch_global = chs[pch_local][0] if pch_local < len(chs) else chs[-1][0]
                    prox = f.get('narrative_proximity')
                    if not isinstance(prox, (int,float)): prox = None
                    rh_obj = {"hint": (f.get('hint') or '')[:300], "category": cat}
                    rb_obj = {}
                    await db.execute(_sql_text("""
                      INSERT INTO foreshadows (id, project_id, type, description, planted_chapter, status,
                                              created_at, resolve_conditions_json, resolution_blueprint_json,
                                              narrative_proximity)
                      VALUES (gen_random_uuid(), :pid, :type, :desc, :pch, 'pending', now(),
                              CAST(:rh AS json), CAST(:rb AS json), :prox)
                    """), {'pid': project_id, 'type': typ, 'desc': desc, 'pch': pch_global,
                            'rh': json.dumps(rh_obj, ensure_ascii=False),
                            'rb': json.dumps(rb_obj),
                            'prox': prox})
                    inserted += 1
                await db.commit()
            log.info('Vol %s inserted %d in %.1fs', vidx, inserted, time.time()-t0)
            total_inserted += inserted
        except Exception as e:
            log.error('Vol %s failed: %s', vidx, e)
            continue
    log.info('=== Foreshadows total inserted: %d ===', total_inserted)
    return total_inserted


# =============================================================================
# 2) Style v9 — 多维调出细粒度 rules + 按场景分类的 sample_passages
# =============================================================================
STYLE_DIM_PROMPT = """你是资深小说文本分析师。从下面的文本里抽取写作风格的「{dim_label}」维度规律（有量化偏好的才抽，没有明显偏好不要强抽）。

output JSON:
{
  "rules": [
    {"rule": "具体描述、30字内」", "weight": 0.5到一个, "category": "{dim_code}", "evidence": "举一句例（<=80字）"}
  ]
}
输出JSON仅。"""

STYLE_DIMENSIONS = [
    ('rhythm',          '句子节奏 与 长短句偏好'),
    ('vocab',           '词汇选用 与 雅俗偏好'),
    ('pov',             '视角 与 叙述人称'),
    ('emotion',         '情感表达 与 心理描写'),
    ('metaphor',        '比喻与象征密度'),
    ('dialogue',        '对话节奏 与 口语化程度'),
    ('scene',           '场景描写 与 五感处理'),
    ('transition',      '场景转换 与 时空接续'),
    ('paragraph',       '段落韵律 与 信息密度'),
    ('punctuation',     '标点偏好 与 表意习惯'),
    ('humor',           '幽默 与 挑衅/恨意表达'),
    ('action',          '动作/打斗场面节奏'),
    ('worldview',       '世界规则提示方式'),
    ('subtext',         '潜台词 与 未言明之意'),
]

SAMPLE_CLASSIFY_SYSTEM = """你是场景分类专家。阅读下面的文本片段，从以下场景类型中选一个最主要的：
- action（动作战斗）
- dialogue（对话为主）
- emotion（心理情感描写）
- scene（环境场景描写）
- transition（场景转换过渡）
- exposition（背景与设定介绍）
- climax（高潮）
输出：{"scene_type": "..."}  仅JSON，不要多余。"""


async def upgrade_style_v9(book_id: str, style_profile_id: str):
    """从 reference_book_slices 采样，从 14 个维度各抽一批规律，同时收集 60+ 个 sample_passages并分类。"""
    # 采样代表性强的片段（3 批 x 6 片段 = 18 个训练金）
    async with async_session_factory() as db:
        all_slices = (await db.execute(_sql_text("""
            SELECT id, chapter_idx, raw_text FROM reference_book_slices
            WHERE book_id=:bid AND char_length(raw_text) BETWEEN 250 AND 600
            ORDER BY chapter_idx, sequence_id
        """), {'bid': book_id})).all()
    log.info('Style v9: book has %d candidate slices', len(all_slices))
    # 过滤黑名单
    blacklist = ['ISBN','出版许可','电子邮箱','http','www.','cb@','版权','BookDNA','云阅读']
    clean = []
    for sid, cidx, txt in all_slices:
        if any(b in txt for b in blacklist): continue
        ch = len(re.findall(r'[\u4e00-\u9fff]', txt))
        if ch / len(txt) < 0.6: continue
        clean.append((sid, cidx, txt))
    log.info('Style v9: %d clean slices', len(clean))

    # 抽 24 个片段作为 sample_passages（3 本作品会补充，这里主体）
    n_samples = min(24, len(clean))
    step = len(clean) / max(1, n_samples)
    sample_picks = [clean[int(step * i)] for i in range(n_samples)]

    # 并发分类场景
    sem = asyncio.Semaphore(8)
    async def classify(sid, cidx, txt):
        async with sem:
            try:
                res = await _llm(messages=[
                        {'role':'system', 'content': SAMPLE_CLASSIFY_SYSTEM},
                        {'role':'user', 'content': f"片段：\n{txt}\n\n输出JSON。"}
                    ])
                d = _parse_json_loose(res)
                stype = (d or {}).get('scene_type', 'scene') if isinstance(d, dict) else 'scene'
                return {'text': txt, 'source_book_slice_id': str(sid), 'chapter_idx': cidx, 'scene_type': stype}
            except Exception as e:
                log.warning('classify failed: %s', e)
                return {'text': txt, 'source_book_slice_id': str(sid), 'chapter_idx': cidx, 'scene_type': 'scene'}
    classified = await asyncio.gather(*[classify(*p) for p in sample_picks])
    by_type = {}
    for c in classified:
        by_type.setdefault(c['scene_type'], 0)
        by_type[c['scene_type']] += 1
    log.info('Sample by scene type: %s', by_type)

    # 按维度抽 rules。每个维度用 6 个片段作证据
    async def extract_dim(dim_code, dim_label):
        # 抽 6 片段
        step = len(clean) / 6
        picks = [clean[int(step * (i+0.5))][2] for i in range(6)]
        material = '\n---\n'.join(picks)
        try:
            res = await _llm(messages=[
                    {'role':'system', 'content': STYLE_DIM_PROMPT.replace('{dim_label}', dim_label).replace('{dim_code}', dim_code)},
                    {'role':'user', 'content': f"文本集：\n{material}\n\n请抽取 3-6 条「{dim_label}」维度规律。JSON输出。"}
                ])
            d = _parse_json_loose(res)
            arr = (d or {}).get('rules') if isinstance(d, dict) else None
            return arr or []
        except Exception as e:
            log.warning('dim %s failed: %s', dim_code, e)
            return []

    log.info('Extracting %d style dimensions in parallel...', len(STYLE_DIMENSIONS))
    sem2 = asyncio.Semaphore(6)
    async def wrap(d):
        async with sem2:
            return await extract_dim(d[0], d[1])
    results = await asyncio.gather(*[wrap(d) for d in STYLE_DIMENSIONS])
    all_rules = []
    for dim, rs in zip(STYLE_DIMENSIONS, results):
        log.info('  %s: %d rules', dim[0], len(rs))
        for r in rs:
            if not isinstance(r, dict): continue
            r['category'] = dim[0]
            if 'weight' not in r: r['weight'] = 0.6
            all_rules.append(r)
    log.info('Style v9 total rules: %d', len(all_rules))

    # 读取现有 sample_passages，合并护贝
    async with async_session_factory() as db:
        prof = (await db.execute(_sql_text("SELECT sample_passages, anti_ai_rules, tone_keywords FROM style_profiles WHERE id=:i"),
                                {'i': style_profile_id})).first()
    existing = list(prof[0] or [])
    # 合并：现有 + classified，去重
    seen = set()
    merged = []
    for s in existing + classified:
        if isinstance(s, dict):
            sig = (s.get('text') or '')[:60]
        else:
            sig = str(s)[:60]
        if sig in seen: continue
        seen.add(sig)
        merged.append(s if isinstance(s, dict) else {'text': str(s), 'scene_type': 'scene'})

    log.info('Final sample_passages: %d (existing %d + new %d)', len(merged), len(existing), len(classified))

    async with async_session_factory() as db:
        await db.execute(_sql_text("""
            UPDATE style_profiles SET
              rules_json=CAST(:rules AS jsonb),
              sample_passages=CAST(:samples AS jsonb),
              updated_at=now()
            WHERE id=:i
        """), {
            'rules': json.dumps(all_rules, ensure_ascii=False),
            'samples': json.dumps(merged, ensure_ascii=False),
            'i': style_profile_id
        })
        await db.commit()
    log.info('=== Style v9 saved: rules=%d, samples=%d ===', len(all_rules), len(merged))
    return len(all_rules), len(merged)


# =============================================================================
# 3) Plot Structure v2 — 全书 + 分卷 + 人物弧线 + 伏笔密度曲线
# =============================================================================
STRUCTURE_V2_SYSTEM = """你是小说架构分析专家。对提供的小说样本（按卷拼接）进行多维架构分析。

输出 JSON 格式：
{
  "global": {
    "arc_pattern": "...", "opening_style": "...", "pacing_curve": "...",
    "conflict_escalation": "...", "climax_frequency": "...", "ending_pattern": "...",
    "structure_summary": "一句话总结。"
  },
  "per_volume": [
    {
      "volume_idx": 1,
      "arc": "本卷弧线定位（30-50字）",
      "main_conflict": "本卷主冲突",
      "climax_position": "高潮位置（卷末/卷中/几个小高潮）",
      "hook_to_next": "末尾钙子原型"
    }
  ],
  "character_arc_pattern": "人物弧线选择（隶属型/双主角/群像/逆袭式/成长线/下降线） + 说明",
  "foreshadow_density_curve": "伏笔密度随卷变化模式（如递增/递减/中间最密/前重后轻等） + 说明",
  "subplot_pattern": "副线交织方式（并行/交替/收束式等）",
  "info_disclosure_pattern": "信息揭示节奏（期期震惊/慢烬/后期集中等）",
  "viewpoint_strategy": "视角调度策略（纯单主视角/多视角轮转/奇数章财友视角等）",
  "action_to_introspection_ratio": "动作与内心描写的比例特征",
  "chapter_length_pattern": "章节长短偏好",
  "signature_techniques": ["招牌手法1", "招牌手法2", "..."]
}
仅JSON。重点要有针对性，不要空谈。"""

async def upgrade_structure_v2(book_id: str):
    async with async_session_factory() as db:
        slices = (await db.execute(_sql_text("""
            SELECT chapter_idx, raw_text FROM reference_book_slices
            WHERE book_id=:bid ORDER BY chapter_idx, sequence_id
        """), {'bid': book_id})).all()
    log.info('Structure v2: %d slices', len(slices))
    if not slices: return None
    # 采样跨全书的中间段 ~30 段
    n_picks = 30
    step = len(slices) / n_picks
    picks = [slices[int(step * (i+0.5))] for i in range(n_picks)]
    material = '\n---\n'.join(f"【ch{cidx}】{txt[:1200]}" for cidx, txt in picks)
    if len(material) > 60000:
        material = material[:60000]
    log.info('Structure v2 material: %d chars', len(material))

    res = await _llm(messages=[
            {'role':'system', 'content': STRUCTURE_V2_SYSTEM},
            {'role':'user', 'content': f"全书采样：\n{material}\n\n输出多维架构分析JSON。"}
        ])
    data = _parse_json_loose(res)
    if not isinstance(data, dict):
        log.error('Structure v2 parse failed'); return None
    log.info('Structure v2 keys: %s', list(data.keys()))

    # 存为 metadata_json.plot_structure_v2，同时保留原 plot_structure
    async with async_session_factory() as db:
        cur = (await db.execute(_sql_text("SELECT metadata_json FROM reference_books WHERE id=:i"),
                              {'i': book_id})).scalar()
        meta = dict(cur or {})
        meta['plot_structure_v2'] = data
        # global 平听入 plot_structure 并保留原 arc_pattern
        if isinstance(data.get('global'), dict):
            ps_old = meta.get('plot_structure', {}) or {}
            ps_old.update({k: v for k, v in data['global'].items() if v})
            meta['plot_structure'] = ps_old
        await db.execute(_sql_text("UPDATE reference_books SET metadata_json=CAST(:m AS json), updated_at=now() WHERE id=:i"),
                        {'m': json.dumps(meta, ensure_ascii=False), 'i': book_id})
        await db.commit()
    log.info('=== Structure v2 saved (per_volume %d entries) ===',
             len(data.get('per_volume', [])) if isinstance(data.get('per_volume'), list) else 0)
    return data


async def main_async(args):
    if args.kind in ('foreshadows','all'):
        log.info('### MODE: foreshadows ###')
        await extract_foreshadows_per_volume(args.project)
    if args.kind in ('style_v9','all'):
        log.info('### MODE: style_v9 ###')
        style_id = args.style
        if not style_id:
            async with async_session_factory() as db:
                sp = await get_or_create_book_profile(db, args.book)
                style_id = str(sp.id)
            log.info('auto-resolved style_profile=%s for book=%s', style_id, args.book)
        await upgrade_style_v9(args.book, style_id)
    if args.kind in ('structure_v2','all'):
        log.info('### MODE: structure_v2 ###')
        await upgrade_structure_v2(args.book)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kind', choices=['foreshadows','style_v9','structure_v2','all'], required=True)
    ap.add_argument('--project', default=DEFAULT_PROJECT)
    ap.add_argument('--book', required=True,
                    help='reference_books.id (UUID); required by PR-BOOK-PROFILE-BIND')
    ap.add_argument('--style', default=None,
                    help='style_profiles.id; if omitted, auto-resolved from --book')
    args = ap.parse_args()
    asyncio.run(main_async(args))

if __name__ == '__main__':
    main()
