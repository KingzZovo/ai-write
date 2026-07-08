"""Layer 3: RAG — Qdrant retrieval, dialogue samples, style samples."""

from __future__ import annotations

import logging
import os
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character, Project

logger = logging.getLogger(__name__)


async def build_rag_layer(
    pack,
    project_id: str | UUID,
    chapter_idx: int,
    db: AsyncSession,
) -> None:
    """Build Layer 3: RAG retrieval, dialogue samples, style samples."""
    pid = str(project_id)

    # Extract key entities from outline for CoKe-pattern retrieval
    entities = extract_entities_from_outline(pack.current_outline)

    # Search Qdrant for relevant snippets
    if entities:
        await search_qdrant_snippets(pack, entities, db)

    # Get dialogue samples from PostgreSQL characters
    try:
        char_result = await db.execute(
            select(Character)
            .where(Character.project_id == pid)
        )
        for char in char_result.scalars().all():
            profile = char.profile_json or {}
            samples = profile.get("dialogue_samples", [])
            if isinstance(samples, list) and samples:
                pack.dialogue_samples[char.name] = samples[:3]
    except Exception as e:
        logger.warning("Failed to load dialogue samples [project=%s]: %s", pid, e)

    # Style samples from Qdrant
    await load_style_samples(pack, pid, db)


def extract_entities_from_outline(outline: dict) -> list[str]:
    """Extract key entity names from the chapter outline for Qdrant search.

    Implements CoKe (Context-based Keyword Extraction) pattern:
    extracts character names, locations, items, and key concepts.
    """
    entities: list[str] = []
    if not outline:
        return entities

    text_fields = ["summary", "main_plot", "key_points", "events", "description"]
    combined_text = ""
    for field_name in text_fields:
        val = outline.get(field_name, "")
        if isinstance(val, str):
            combined_text += " " + val
        elif isinstance(val, list):
            combined_text += " " + " ".join(str(v) for v in val)

    chars = outline.get("characters", [])
    if isinstance(chars, list):
        for c in chars:
            if isinstance(c, str):
                entities.append(c)
            elif isinstance(c, dict):
                name = c.get("name", "")
                if name:
                    entities.append(name)

    locations = outline.get("locations", outline.get("setting", []))
    if isinstance(locations, str):
        entities.append(locations)
    elif isinstance(locations, list):
        entities.extend(str(loc) for loc in locations)

    items = outline.get("items", outline.get("props", []))
    if isinstance(items, list):
        entities.extend(str(item) for item in items)

    if combined_text:
        quoted = re.findall(r'["""](.*?)["""]', combined_text)
        entities.extend(quoted)
        book_marks = re.findall(r'[《](.*?)[》]', combined_text)
        entities.extend(book_marks)

    seen: set[str] = set()
    unique: list[str] = []
    for e in entities:
        e = e.strip()
        if e and e not in seen:
            seen.add(e)
            unique.append(e)

    return unique[:20]


async def search_qdrant_snippets(
    pack,
    entities: list[str],
    db: AsyncSession,
) -> None:
    """Search Qdrant for relevant content based on extracted entities."""
    try:
        from app.services.feature_extractor import generate_embedding

        query_text = " ".join(entities[:10])
        query_text = await maybe_rewrite_query(query_text, db)
        embedding = await generate_embedding(query_text)
        if not embedding:
            return

        from qdrant_client import AsyncQdrantClient
        from app.config import settings

        client = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )

        try:
            results = await client.search(
                collection_name="chapter_summaries",
                query_vector=embedding,
                limit=5,
                score_threshold=0.4,
            )
            for hit in results:
                payload = hit.payload or {}
                summary = payload.get("summary", payload.get("text", ""))
                if summary:
                    pack.rag_snippets.append(summary)

            if os.getenv("CONTEXT_PACK_V2_ENABLED", "false").lower() in ("1", "true", "yes"):
                try:
                    await v2_three_way_recall(pack, embedding, client, db)
                except Exception as v2_exc:
                    logger.debug("v2 three-way recall skipped: %s", v2_exc)
        except Exception:
            logger.debug("Qdrant search failed, collection may not exist")
        finally:
            await client.close()

    except (ImportError, Exception) as e:
        logger.debug("Qdrant RAG retrieval skipped: %s", e)


async def maybe_rewrite_query(query_text: str, db: AsyncSession) -> str:
    """Optional LLM rewrite of the Qdrant query (gated on RAG_QUERY_REWRITE_ENABLED)."""
    raw = os.getenv("RAG_QUERY_REWRITE_ENABLED", "0").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return query_text
    if not query_text.strip():
        return query_text
    try:
        from app.services.prompt_registry import run_structured_prompt

        out = await run_structured_prompt(
            "rag_query_rewrite",
            f"<query>\n{query_text}\n</query>\n\n请返回用于向量检索的改写查询字符串。",
            db,
        )
    except Exception as exc:
        logger.debug("rag_query_rewrite skipped: %s", exc)
        return query_text
    if isinstance(out, dict):
        rewrote = out.get("query") or out.get("rewrite") or out.get("text")
        if isinstance(rewrote, str) and rewrote.strip():
            return rewrote.strip()
    if isinstance(out, str) and out.strip():
        return out.strip()
    return query_text


async def v2_three_way_recall(pack, embedding: list, client, db: AsyncSession) -> None:
    """ContextPack v2: pull style_profiles + beat_sheets from reference book."""
    from app.services.qdrant_store import QdrantStore

    project_id = getattr(pack, "project_id", None) or getattr(pack.meta, "project_id", None)
    ref_book_id: str | None = None
    if project_id:
        try:
            project = await db.get(Project, project_id)
            if project:
                ref = (project.settings_json or {}).get("style_reference", {})
                ref_book_id = ref.get("reference_book_id") or ref.get("book_id")
        except Exception:
            ref_book_id = None

    store = QdrantStore(client)
    style_hits = await store.search_style_profiles(embedding, book_id=ref_book_id, top_k=3)
    for h in style_hits:
        prof = (h.get("payload") or {}).get("profile") or {}
        if prof:
            line = (
                f"[风格] pov={prof.get('pov','?')} 节奏={prof.get('sentence_rhythm','?')} "
                f"情感={prof.get('emotional_register','?')} "
                f"词汇={','.join(prof.get('vocab_tone') or [])}"
            )
            pack.rag_snippets.append(line)
    beat_hits = await store.search_beat_sheets(embedding, book_id=ref_book_id, top_k=2)
    for h in beat_hits:
        beat = (h.get("payload") or {}).get("beat") or {}
        if beat:
            line = (
                f"[骨架] {beat.get('scene_type','?')}: {beat.get('reusable_pattern','?')} "
                f"→ {beat.get('outcome','?')}"
            )
            pack.rag_snippets.append(line)


async def load_style_samples(
    pack,
    project_id: str,
    db: AsyncSession,
) -> None:
    """Load style samples from a StyleProfile or StyleProfileCard fallback."""
    try:
        project = await db.get(Project, project_id)
        if not project:
            return

        settings_json = project.settings_json or {}
        style_ref = settings_json.get("style_reference", {}) or {}

        # Path A: aggregated StyleProfile
        style_profile_id = (
            style_ref.get("profile_id")
            or settings_json.get("default_style_profile_id")
        )
        if style_profile_id:
            from app.models.project import StyleProfile
            try:
                profile = await db.get(StyleProfile, style_profile_id)
            except Exception:
                profile = None
            if profile is not None:
                rendered = _render_style_profile(profile)
                if rendered:
                    pack.style_samples.extend(rendered)
                    return

        # Path B: StyleProfileCard fallback
        ref_book_id = (
            style_ref.get("reference_book_id")
            or style_ref.get("book_id")
            or settings_json.get("reference_book_id")
        )
        if ref_book_id:
            rendered = await _aggregate_style_cards(db, str(ref_book_id), top_k=12)
            if rendered:
                pack.style_samples.extend(rendered)

    except Exception as e:
        logger.debug("Style sample loading skipped: %s", e)


def render_style_profile(profile) -> list[str]:
    """Render a StyleProfile ORM row into Layer-3 style_samples text blocks."""
    parts: list[str] = []
    book_label = (
        getattr(profile, "source_book", None)
        or getattr(profile, "name", None)
        or "未命名风格"
    )

    rules = getattr(profile, "rules_json", None) or []
    rule_lines: list[str] = []
    for r in rules[:10]:
        if isinstance(r, dict):
            txt = r.get("rule") or r.get("text")
            if txt:
                rule_lines.append(f"- {txt}")
        elif isinstance(r, str) and r.strip():
            rule_lines.append(f"- {r}")
    if rule_lines:
        parts.append("【风格规则 — " + str(book_label) + "】\n" + "\n".join(rule_lines))

    anti = getattr(profile, "anti_ai_rules", None) or []
    anti_lines: list[str] = []
    for a in anti[:10]:
        if isinstance(a, dict):
            pat = a.get("pattern") or a.get("rule")
            if pat:
                anti_lines.append(f"- 禁用: {pat}")
        elif isinstance(a, str) and a.strip():
            anti_lines.append(f"- 禁用: {a}")
    if anti_lines:
        parts.append("【反 AI 硬约束】\n" + "\n".join(anti_lines))

    tone = getattr(profile, "tone_keywords", None) or []
    if tone:
        tone_str = " / ".join(str(t) for t in tone[:12] if t)
        if tone_str:
            parts.append("【语气词汇】" + tone_str)

    config = getattr(profile, "config_json", None) or {}
    if isinstance(config, dict) and isinstance(config.get("dosage_profile"), dict):
        d = config["dosage_profile"]
        try:
            dlg = d.get("dialogue", {}) or {}
            met = d.get("metaphor", {}) or {}
            psy = d.get("psychology", {}) or {}
            snt = d.get("sentence", {}) or {}
            par_ = d.get("paragraph", {}) or {}
            col = d.get("colloquial", {}) or {}
            dr = float(dlg.get("ratio", 0) or 0)
            mt = float(met.get("total_per_kchar", 0) or 0)
            ms = float(met.get("sentence_end_per_kchar", 0) or 0)
            py = float(psy.get("pattern_total_per_kchar", 0) or 0)
            pyc = float(psy.get("pattern_per_chapter_7k", 0) or 0)
            pyn = float(psy.get("neutral_words_per_kchar", 0) or 0)
            slm = float(snt.get("mean_chars", 0) or 0)
            plm = float(par_.get("mean_chars", 0) or 0)
            cl = float(col.get("particles_per_kchar", 0) or 0)
            src_name = d.get("source", "参考书")
            dosage_lines = [
                "【剂量画像 — 仿写参考密度（按一章 7000 字换算）】",
                f"· 对话占比 ≈ {dr*100:.0f}%，对话轮均长约 27 字（自然为主，不为凑量而造对话）。",
                f"· 比喻总量 ≈ {mt:.1f}/千字（一章约 {mt*7:.0f} 次），其中句尾比喻 ≈ {ms:.1f}/千字（约 {ms*7:.0f} 次）。句尾比喻是江南特色，多用但每个都要独特、不重复。",
                f"· 心理戏套语（心里一沉/眼皮一跳/喉咙发紧/头皮发麻/握紧拳等 13 类）总量 ≈ {py:.3f}/千字，一章约 {pyc:.1f} 次。硬上限：每章不得超过 2 次，同一套语不得重复。",
                f"· 心理中性词（想 / 觉得 / 感到 / 猛然 / 突然 / 仿佛）≈ {pyn:.1f}/千字（一章约 {pyn*7:.0f} 次）。这是「正常心理描写」，不是黑名单。",
                f"· 句长均 {slm:.0f} 字 / 段长均 {plm:.0f} 字。短长结合，不打碎句、不堆长句。",
                f"· 口语助词（呀 / 哦 / 哈 / 嘴 / 老子 / 个屁）≈ {cl:.2f}/千字（一章约 {cl*7:.0f} 次）。吝槽口吻误论年轻人主语，能用口语骂人就别用「面色凝重」。",
                "· prompt 自指语严禁出现：「以下是 / 根据您 / 以上便是 / prompt / 黑名单 / 护城词 / 伏笔 / 钩子」等。",
                f"提示：以上重点为《{src_name}》原作采样基线，仿写不必精确达标，但严禁批量超标——尤其是心理戏套语和句尾比喻不得重复。",
            ]
            parts.append("\n".join(dosage_lines))
        except Exception as e:
            logger.warning("render dosage_profile failed: %s", e)

    return parts


async def aggregate_style_cards(
    db: AsyncSession,
    book_id: str,
    top_k: int = 12,
) -> list[str]:
    """Aggregate top-K StyleProfileCard.profile_json entries from a reference book."""
    try:
        from app.models.decompile import StyleProfileCard

        stmt = (
            select(StyleProfileCard)
            .where(StyleProfileCard.book_id == str(book_id))
            .order_by(StyleProfileCard.created_at.asc())
            .limit(top_k)
        )
        result = await db.execute(stmt)
        cards = list(result.scalars().all())
        if not cards:
            return []

        povs: list[str] = []
        tenses: list[str] = []
        rhythms: list[str] = []
        dialogues: list[str] = []
        sensory_sums: dict[str, float] = {}
        sensory_n = 0
        pacings: list[str] = []
        emotions: list[str] = []
        vocab_set: list[str] = []
        forbidden_set: list[str] = []
        signature_set: list[str] = []

        def _add_unique(lst: list[str], item: str) -> None:
            if item and item not in lst:
                lst.append(item)

        for c in cards:
            pj = c.profile_json or {}
            if not isinstance(pj, dict):
                continue
            if pj.get("pov"):
                povs.append(str(pj["pov"]))
            if pj.get("tense"):
                tenses.append(str(pj["tense"]))
            if pj.get("sentence_rhythm"):
                rhythms.append(str(pj["sentence_rhythm"]))
            if pj.get("dialogue_style"):
                dialogues.append(str(pj["dialogue_style"]))
            sm = pj.get("sensory_mix") or {}
            if isinstance(sm, dict) and sm:
                for k, v in sm.items():
                    try:
                        sensory_sums[k] = sensory_sums.get(k, 0.0) + float(v)
                    except (TypeError, ValueError):
                        continue
                sensory_n += 1
            if pj.get("pacing"):
                pacings.append(str(pj["pacing"]))
            if pj.get("emotional_register"):
                emotions.append(str(pj["emotional_register"]))
            for k in (pj.get("vocab_tone") or []):
                if isinstance(k, str):
                    _add_unique(vocab_set, k)
            for k in (pj.get("forbidden_tells") or []):
                if isinstance(k, str):
                    _add_unique(forbidden_set, k)
            for k in (pj.get("signature_moves") or []):
                if isinstance(k, str):
                    _add_unique(signature_set, k)

        def _pick_longest(lst: list[str]) -> str:
            return max(lst, key=len) if lst else ""

        def _vote(lst: list[str]) -> str:
            return max(set(lst), key=lst.count) if lst else ""

        pov_str = _vote(povs)
        tense_str = _vote(tenses)
        rhythm_str = _pick_longest(rhythms)
        dialogue_str = _pick_longest(dialogues)
        pacing_str = _pick_longest(pacings)
        emotion_str = _pick_longest(emotions)

        sensory_str = ""
        if sensory_n > 0 and sensory_sums:
            avg = {k: v / sensory_n for k, v in sensory_sums.items()}
            top_sens = sorted(avg.items(), key=lambda x: -x[1])[:5]
            sensory_str = " / ".join(
                f"{k} {round(v * 100)}%" for k, v in top_sens if v > 0.001
            )

        parts: list[str] = []

        block1_lines: list[str] = []
        pov_tense = " ".join(s for s in [pov_str, tense_str] if s).strip()
        if pov_tense:
            block1_lines.append(f"- 视角/时态: {pov_tense}")
        if rhythm_str:
            block1_lines.append(f"- 句式节奏: {rhythm_str}")
        if dialogue_str:
            block1_lines.append(f"- 对话风格: {dialogue_str}")
        if sensory_str:
            block1_lines.append(f"- 感官分布: {sensory_str}")
        if pacing_str:
            block1_lines.append(f"- 节奏: {pacing_str}")
        if emotion_str:
            block1_lines.append(f"- 情绪基调: {emotion_str}")
        if vocab_set:
            block1_lines.append("- 词汇调性: " + " / ".join(vocab_set[:10]))
        if block1_lines:
            parts.append("【参考风格档案 (基于参考书切片聚合)】\n" + "\n".join(block1_lines))

        block2_lines: list[str] = []
        for t in forbidden_set[:8]:
            block2_lines.append(f"- 禁忌: {t}")
        for s in signature_set[:6]:
            block2_lines.append(f"- 招牌: {s}")
        if block2_lines:
            parts.append("【风格禁忌与招牌】\n" + "\n".join(block2_lines))

        return parts
    except Exception as e:
        logger.debug("Style cards aggregation failed: %s", e)
        return []
