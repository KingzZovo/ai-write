"""Layer 2: Facts — world rules, character cards, foreshadows, timeline."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    Chapter,
    Character,
    CharacterLocation,
    Foreshadow,
    Location,
    Volume,
    WorldRule,
)
from app.services.context_pack import CFPGTriplet, CharacterCard, TimeAnchor
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT

logger = logging.getLogger(__name__)


async def build_facts_layer(
    pack,
    project_id: str | UUID,
    chapter_idx: int,
    db: AsyncSession,
    global_chapter_idx: int | None = None,
) -> None:
    """Build Layer 2: world rules, character cards, foreshadows, timeline.

    ``chapter_idx`` is volume-local (DB ``Chapter.chapter_idx``).
    ``global_chapter_idx`` is the book-global equivalent used for foreshadow debt.
    """
    pid = str(project_id)

    # World rules from PostgreSQL
    try:
        pack.world_rules.append(WORLD_LOGIC_CONTRACT)
        rules_result = await db.execute(
            select(WorldRule.category, WorldRule.rule_text)
            .where(WorldRule.project_id == pid)
            .order_by(WorldRule.category)
        )
        for row in rules_result.all():
            pack.world_rules.append(f"[{row.category}] {row.rule_text}")
    except Exception as e:
        logger.warning("Failed to load world rules [project=%s]: %s", pid, e)

    # Character cards from PostgreSQL characters + Neo4j state
    try:
        loc_by_char_id: dict[UUID, str] = {}
        try:
            loc_rows = await db.execute(
                select(
                    CharacterLocation.character_id,
                    Location.name,
                )
                .join(Location, Location.id == CharacterLocation.location_id)
                .where(
                    CharacterLocation.project_id == pid,
                    CharacterLocation.chapter_start <= chapter_idx,
                )
                .order_by(
                    CharacterLocation.character_id.asc(),
                    CharacterLocation.chapter_start.desc(),
                )
            )
            for character_id, loc_name in loc_rows.all():
                if character_id not in loc_by_char_id:
                    loc_by_char_id[character_id] = loc_name
        except Exception as e:
            logger.debug("Failed to load character_locations projection [project=%s]: %s", pid, e)

        char_result = await db.execute(
            select(Character)
            .where(Character.project_id == pid)
        )
        characters = char_result.scalars().all()

        for char in characters:
            profile = char.profile_json or {}
            card = CharacterCard(
                name=char.name,
                location=(loc_by_char_id.get(char.id) or profile.get("location", "")),
                power_level=profile.get("power_level", ""),
                mental_state=profile.get("mental_state", ""),
            )

            rels = profile.get("relationships", {})
            if isinstance(rels, dict):
                card.relationships = rels
            elif isinstance(rels, list):
                for r in rels:
                    if isinstance(r, dict):
                        target = r.get("target", r.get("name", ""))
                        rel_type = r.get("type", r.get("relation", ""))
                        if target and rel_type:
                            card.relationships[target] = rel_type

            actions = profile.get("recent_actions", [])
            if isinstance(actions, list):
                card.recent_actions = actions[-5:]

            pack.character_cards.append(card)

        # Enrich from Neo4j if available
        await _enrich_characters_from_neo4j(pack, pid, chapter_idx)
    except Exception as e:
        logger.warning("Failed to load character cards [project=%s]: %s", pid, e)

    # Foreshadow triplets
    try:
        fs_result = await db.execute(
            select(Foreshadow)
            .where(
                Foreshadow.project_id == pid,
                Foreshadow.status.in_(("planted", "ripening", "ready")),
            )
            .order_by(Foreshadow.narrative_proximity.desc())
        )
        fs_rows = list(fs_result.scalars().all())
        for fs in fs_rows:
            conditions = fs.resolve_conditions_json or []
            blueprint = fs.resolution_blueprint_json or {}
            triplet = CFPGTriplet(
                cause=f"第{fs.planted_chapter}章: {fs.description}",
                foreshadow=fs.description,
                payoff_goal=blueprint.get("goal", "") or (
                    conditions[0] if conditions else "待定"
                ),
                proximity=fs.narrative_proximity or 0.0,
            )
            pack.foreshadow_triplets.append(triplet)

        # Q4: foreshadow debt gate
        try:
            from app.services.foreshadow_manager import (
                compute_debt_score,
                render_debt_warning,
            )

            debt_idx = (
                global_chapter_idx
                if global_chapter_idx is not None
                else chapter_idx
            )
            debt = compute_debt_score(fs_rows, int(debt_idx))
            pack.foreshadow_debt_warning = render_debt_warning(debt)
        except Exception as e:
            logger.warning("Failed to compute foreshadow debt [project=%s]: %s", pid, e)
    except Exception as e:
        logger.warning("Failed to load foreshadow triplets [project=%s]: %s", pid, e)

    # Timeline anchors from chapter summaries with key events
    try:
        timeline_result = await db.execute(
            select(Chapter.chapter_idx, Chapter.summary)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(
                Volume.project_id == pid,
                Chapter.summary.isnot(None),
                Chapter.summary != "",
                Chapter.chapter_idx <= chapter_idx,
            )
            .order_by(Chapter.chapter_idx.asc())
        )
        for row in timeline_result.all():
            if row.summary and len(row.summary) > 10:
                anchor = TimeAnchor(
                    chapter_idx=row.chapter_idx,
                    event=row.summary[:100],
                )
                pack.timeline_anchors.append(anchor)

        # Keep only the most important anchors to save tokens
        if len(pack.timeline_anchors) > 15:
            first = pack.timeline_anchors[:3]
            last = pack.timeline_anchors[-5:]
            middle = pack.timeline_anchors[3:-5]
            step = max(1, len(middle) // 7)
            sampled = middle[::step][:7]
            pack.timeline_anchors = first + sampled + last
    except Exception as e:
        logger.warning("Failed to load timeline anchors [project=%s]: %s", pid, e)

    # Q3 v1.9.1: character cognition ledger
    try:
        from app.services import character_cognition as _cognition

        ledger = await _cognition.load_ledger(db, pid)
        pack.cognition_boundaries = _cognition.serialize_for_prompt(
            ledger, max_chars=1200
        )
    except Exception as e:
        logger.warning("Failed to load character cognition ledger [project=%s]: %s", pid, e)

    # C2/F1 v1.9.2: whole-book style-tic mirror
    try:
        from app.models.project import StyleStat
        from app.services.style_stat import render_style_mirror_block

        row = (
            await db.execute(
                select(StyleStat.stats_json).where(StyleStat.project_id == pid)
            )
        ).first()
        if row and row[0]:
            pack.style_tic_mirror = render_style_mirror_block(row[0], max_chars=800)
    except Exception as e:
        logger.warning("Failed to load style stats [project=%s]: %s", pid, e)

    # C3/F4: deterministic related-chapter recall + secondary-cast roster
    try:
        from app.services.character_roster import render_roster_block
        from app.services.related_chapters import (
            find_related_chapters,
            render_recall_block,
        )

        related = await find_related_chapters(
            db, pid, None, chapter_idx, pack.current_outline or {}
        )
        recall = render_recall_block(related, max_chars=600)

        roster_block = ""
        try:
            from app.models.project import CharacterAppearance

            cast_rows = (
                await db.execute(
                    select(
                        CharacterAppearance.character_name,
                        CharacterAppearance.last_seen_chapter,
                    ).where(CharacterAppearance.project_id == pid)
                )
            ).all()
            cast = [
                {"character_name": n, "last_seen_chapter": ls}
                for n, ls in cast_rows
            ]
            roster_block = render_roster_block(
                cast, (global_chapter_idx or chapter_idx), max_chars=600
            )
        except Exception as e:
            logger.warning("Failed to render cast roster [project=%s]: %s", pid, e)

        pack.related_chapter_recall = "\n\n".join(
            b for b in (recall, roster_block) if b
        )
    except Exception as e:
        logger.warning("Failed to build related-chapter recall [project=%s]: %s", pid, e)

    # C4/F3: narrative compass direction anchor
    try:
        from app.services.compass_service import load_compass, render_compass_anchor

        compass = await load_compass(db, pid)
        pack.compass_anchor = render_compass_anchor(
            compass, (global_chapter_idx or chapter_idx), max_chars=400
        )
    except Exception as e:
        logger.warning("Failed to load narrative compass [project=%s]: %s", pid, e)

    # Build strand tracker from recent chapters
    await _build_strand_tracker(pack, pid, chapter_idx, db)


async def _enrich_characters_from_neo4j(
    pack,
    project_id: str,
    chapter_idx: int,
) -> None:
    """Enrich character cards with state from Neo4j knowledge graph."""
    try:
        from app.db.neo4j import get_neo4j

        driver = None
        async for d in get_neo4j():
            driver = d
            break

        if driver is None:
            return

        from app.services.entity_timeline import EntityTimelineService

        ets = EntityTimelineService(driver)
        snapshots = await ets.get_active_characters_at(project_id, chapter_idx)
        relationships = await ets.get_relationships_at(project_id, chapter_idx)

        rel_lookup: dict[str, dict[str, str]] = {}
        for rel in relationships:
            rel_lookup.setdefault(rel.source, {})[rel.target] = rel.rel_type

        card_by_name = {c.name: c for c in pack.character_cards}

        for snap in snapshots:
            status = snap.status or {}
            if snap.name in card_by_name:
                card = card_by_name[snap.name]
                if not card.location:
                    card.location = status.get("location", status.get("位置", ""))
                if not card.power_level:
                    card.power_level = status.get(
                        "power_level",
                        status.get("能力等级", status.get("实力", "")),
                    )
                if not card.mental_state:
                    card.mental_state = status.get(
                        "mental_state",
                        status.get("情绪", status.get("状态", "")),
                    )
                if snap.name in rel_lookup:
                    for target, rtype in rel_lookup[snap.name].items():
                        if target not in card.relationships:
                            card.relationships[target] = rtype
            else:
                from app.services.context_pack import CharacterCard as _CC

                card = _CC(
                    name=snap.name,
                    location=status.get("location", status.get("位置", "")),
                    power_level=status.get(
                        "power_level",
                        status.get("能力等级", status.get("实力", "")),
                    ),
                    mental_state=status.get(
                        "mental_state",
                        status.get("情绪", status.get("状态", "")),
                    ),
                    relationships=rel_lookup.get(snap.name, {}),
                )
                pack.character_cards.append(card)

    except (RuntimeError, ImportError):
        logger.debug("Neo4j not available, skipping character enrichment [project=%s]", project_id)
    except Exception as e:
        logger.warning("Failed to enrich characters from Neo4j [project=%s]: %s", project_id, e)


async def _build_strand_tracker(
    pack,
    project_id: str,
    chapter_idx: int,
    db: AsyncSession,
) -> None:
    """Analyze recent chapters to track Quest/Fire/Constellation strands."""
    try:
        from app.services.strand_tracker import StrandTrackerService

        tracker_svc = StrandTrackerService(db=db)
        tracker = await tracker_svc.analyze_strands(project_id, chapter_idx)
        pack.strand_tracker = tracker
    except (ImportError, Exception) as e:
        logger.debug("Strand tracker not available [project=%s]: %s", project_id, e)
