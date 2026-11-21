"""写后账目核对:本章写出的金额与"计划已定的数"不一致时,记一条提示。

**只记账,不阻断,不自动改写。**

- 不阻断:任何失败(读不到计划、模型报错、返回空正文、解析失败)都只记 warning 日志并返回空清单,
  绝不改章节状态,绝不抛给调用方。
- 不改写:判定错就自动改稿,风险大于收益。作者需要的是"这里可能对不上",不是机器替他改数。
- 有成本边界:只有**本章确实有金额钉子**时才调用模型(计划本身没定数就没有比对对象),
  否则一次调用都不发生。

判定口径见 :mod:`app.services.numeric_ledger`:只比"总额",不比单价;按**同名目**与
"按名目的金额台账"比对 —— 只有"同名目此前已登记过、且本次数额不同"才算冲突
(首次出现的名目、与台账一致的都不报),这样既保住真阳性,也消掉"前文写过但大纲没记"的误报。
台账落在已有的落库通道:``ChapterQualityGateResult.to_safe_metadata()`` →
``ChapterVersion.metadata_json["quality_gate"]["numeric_ledger"]``(``app/models/project.py``
的 ``ChapterVersion.metadata_json``,表 ``chapter_versions``)。读写都用注入式
(``ledger_reader`` / ``ledger_writer``),任何失败只记 warning,退化为"没有可比对象"。
"""
from __future__ import annotations

import logging

from app.services.numeric_ledger import (
    ledger_conflicts, ledger_entries_from_rows, normalize_item, parse_ledger_entries,
    planned_amounts_from_text,
)

logger = logging.getLogger(__name__)

# 还原"计划文本"时向后看多少章。与写作阶段 CONTEXT_FUTURE_OUTLINE_CHAPTERS 同量级:
# 金额常在**后一章**的大纲里才第一次写明,只看本章大纲会漏掉漂移。
MAX_PLAN_CHAPTERS = 20

# 核对时一并纳入"前文已写明的数"的最近章节数。
MAX_PRIOR_SUMMARIES = 8
# 核对用的钉子行上限。提示侧是有意的短清单(12 行);核对要覆盖面,给得宽一些。
MAX_CHECK_PIN_LINES = 40
# 台账读取:往前扫多少个 ``ChapterVersion`` 行(每章可能多行,只取最近的)。台账只是比对
# 依据,不必扫全库;超过这个窗口的老金额当作"没有登记过"。
MAX_LEDGER_VERSION_ROWS = 60


async def resolve_plan_text(db, *, project_id, chapter_id) -> str:
    """读出"计划里已写明的金额"清单(与写作阶段注入的是同一来源)。

    来源与 ``ContextPack._numeric_pin_lines`` 一致:本卷分章梗概 + 后续若干章的大纲。
    会话约定与质量门内其它读取一致:调用方给了 ``db`` 就用它,**没给就自己开一个短会话**
    —— ``app/api/generate.py`` 的流式路径传的就是 ``db=None``,不自己开会话这里永远不会生效。
    任一步出错都返回空串(调用方据此跳过核对),不抛。
    """
    if not project_id or not chapter_id:
        return ""
    try:
        from app.services.generation_context import generation_context_session

        async with generation_context_session(db) as read_db:
            return await _resolve_plan_text(
                read_db, project_id=project_id, chapter_id=chapter_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: plan text unavailable (%s)", exc)
        return ""


async def _resolve_plan_text(db, *, project_id, chapter_id) -> str:
    try:
        from sqlalchemy import select, tuple_
        from sqlalchemy.orm import load_only

        from app.models.project import Chapter, Outline, Volume
        from app.services.numeric_pins import (
            chapter_outline_line, outline_pin_blocks, pin_block,
        )
        from app.services.outline_authority import outline_for_generation
        from app.services.outline_order import volume_condition
        from app.services.outline_order import volume_condition
        from app.services.summary_provenance import current_summary_sql
        pid = str(project_id)
        head = (await db.execute(select(
            Chapter.volume_id, Chapter.chapter_idx, Chapter.id, Chapter.outline_json
        ).where(Chapter.id == chapter_id).limit(1))).one_or_none()
        if head is None:
            return ""
        volume_id, chapter_idx, cid = head.volume_id, head.chapter_idx, head.id
        volume = await db.scalar(select(Volume).where(
            Volume.id == volume_id, Volume.project_id == pid).limit(1))
        if volume is None:
            return ""

        volume_outline: dict = {}
        selected_volume = await db.scalar(select(Outline).where(
            Outline.project_id == pid, Outline.level == "volume",
            volume_condition(str(volume_id), volume.volume_idx)
        ).order_by(Outline.is_confirmed.desc(), Outline.version.desc(),
                   Outline.created_at.desc(), Outline.id.desc()).limit(1))
        if selected_volume is not None:
            volume_outline = outline_for_generation(selected_volume.content_json or {})

        position = tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        cursor = (volume.volume_idx, volume_id, chapter_idx, cid)
        rows = (await db.execute(
            select(Chapter, Volume.volume_idx).join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == pid, position > cursor)
            .order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
            .options(load_only(Chapter.id, Chapter.chapter_idx, Chapter.title, Chapter.outline_json))
            .limit(MAX_PLAN_CHAPTERS))).all()


        lines = []
        for ch, vindex in rows:
            oj = outline_for_generation(ch.outline_json)
            summary = (oj.get("summary") or oj.get("main_plot") or "") if isinstance(oj, dict) else (oj or "")
            lines.append(chapter_outline_line(
                volume_idx=vindex, chapter_idx=ch.chapter_idx, chapter_id=ch.id,
                title=ch.title or "", summary=summary))
        # "计划"不只等于大纲:同一笔钱常在更早的章节里已经写定,只看大纲会把
        # "与前文一致"误报成冲突(实测 ch18 写的 3000 早在 ch9 摘要里写明)。
        # 这里把最近若干章**核实过的摘要**也算进核对范围 —— 只影响核对,不改进写作提示。
        prior = (await db.execute(
            select(Chapter.chapter_idx, current_summary_sql().label("verified_summary"))
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == pid, position < cursor)
            .order_by(Volume.volume_idx.desc(), Volume.id.desc(),
                      Chapter.chapter_idx.desc(), Chapter.id.desc())
            .limit(MAX_PRIOR_SUMMARIES))).all()
        extra = [(f"CH-{row.chapter_idx}", row.verified_summary)
                 for row in prior if row.verified_summary]
        # 本章自己的大纲也必须算进核对范围:金额常写在**本章**大纲里(实测 ch08
        # 大纲写"周芸收下八百二"),漏掉它会把"照大纲写的"误报成冲突。
        import json as _json
        current_text = _json.dumps(outline_for_generation(head.outline_json or {}),
                                   ensure_ascii=False)
        if current_text.strip(" {}[]\""):
            extra.insert(0, ("本章", current_text))
        # 核对要的是覆盖面,不是简短:提示侧的 12 行上限在这里会把前文挤掉。
        return pin_block(outline_pin_blocks(volume_outline, lines, extra=extra),
                         limit=MAX_CHECK_PIN_LINES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: plan text unavailable (%s)", exc)
        return ""


async def _default_extractor(text: str) -> list[dict] | None:
    """用配置里的模型抽金额;空正文与解析失败都要重试(实测返回空是常态)。"""
    from app.config import settings
    from app.services.model_router import get_model_router
    from app.services.numeric_ledger import AMOUNT_EXTRACTION_SYSTEM, parse_amounts

    max_tokens = int(getattr(settings, "NUMERIC_CHECK_MAX_TOKENS", 3000))
    retries = int(getattr(settings, "NUMERIC_CHECK_RETRIES", 1))
    router = get_model_router()
    for _ in range(max(1, retries + 1)):
        result = await router.generate(
            task_type="evaluation",
            messages=[{"role": "system", "content": AMOUNT_EXTRACTION_SYSTEM},
                      {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        rows = parse_amounts((getattr(result, "text", "") or "").strip())
        if rows is not None:
            return rows
    return None


def _as_uuid(value):
    """把 id 转成 UUID;转不了返回 None(调用方据此跳过,绝不猜)。"""
    import uuid as _uuid
    try:
        return _uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _ledger_payload(metadata: object) -> object:
    """从 ``ChapterVersion.metadata_json`` 里取台账那一格;结构不对返回 None。"""
    if not isinstance(metadata, dict):
        return None
    gate = metadata.get("quality_gate")
    if not isinstance(gate, dict):
        return None
    return gate.get("numeric_ledger")


async def read_chapter_ledger(db=None, *, project_id=None, chapter_id=None) -> list[dict]:
    """读"按名目的金额台账":本章之前各章登记过的 ``{"item", "amount"}``。

    来源就是本批接入的落库通道(每个 ``ChapterVersion`` 的
    ``metadata_json["quality_gate"]["numeric_ledger"]``),按章节位置**只取本章之前的**:
    本章自己的历史版本不算"此前已登记",否则重写同一章会自证一致、报不出漂移。
    会话约定与 ``resolve_plan_text`` 一致:给了 ``db`` 就用它,**没给就自己开一个短会话**。
    任一步出错都返回 ``[]``(等于"没有可比对象"),不抛。
    """
    if not project_id or not chapter_id:
        return []
    try:
        from app.services.generation_context import generation_context_session

        async with generation_context_session(db) as read_db:
            return await _read_chapter_ledger(
                read_db, project_id=project_id, chapter_id=chapter_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: ledger unavailable (%s)", exc)
        return []


async def _read_chapter_ledger(db, *, project_id, chapter_id) -> list[dict]:
    """台账查询本体(与 ``_resolve_plan_text`` 同一套位置口径与元数据来源)。"""
    try:
        from sqlalchemy import select, tuple_

        from app.models.project import Chapter, ChapterVersion, Volume
        pid = str(project_id)
        head = (await db.execute(select(
            Chapter.volume_id, Chapter.chapter_idx, Chapter.id
        ).where(Chapter.id == chapter_id).limit(1))).one_or_none()
        if head is None:
            return []
        volume_idx = await db.scalar(select(Volume.volume_idx).where(
            Volume.id == head.volume_id, Volume.project_id == pid).limit(1))
        if volume_idx is None:
            return []
        position = tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        # 只取本章**之前**的位置,最近的优先(同章多版本时最新版本先看)。
        cursor = (volume_idx, head.volume_id, head.chapter_idx, head.id)
        rows = (await db.execute(
            select(Chapter.chapter_idx, ChapterVersion.metadata_json)
            .join(Volume, Chapter.volume_id == Volume.id)
            .join(ChapterVersion, ChapterVersion.chapter_id == Chapter.id)
            .where(Volume.project_id == pid, position < cursor)
            .order_by(Volume.volume_idx.desc(), Volume.id.desc(), Chapter.chapter_idx.desc(),
                      Chapter.id.desc(), ChapterVersion.created_at.desc(),
                      ChapterVersion.id.desc())
            .limit(MAX_LEDGER_VERSION_ROWS))).all()
        entries: list[dict] = []
        seen: set[tuple] = set()
        for row in rows:
            for entry in parse_ledger_entries(_ledger_payload(row.metadata_json)):
                # 同一章同一名目只留最新版本登记的那一条。
                key = (row.chapter_idx, normalize_item(entry["item"]))
                if key in seen:
                    continue
                seen.add(key)
                if entry.get("chapter_idx") is None:
                    entry["chapter_idx"] = row.chapter_idx
                entries.append(entry)
        return entries
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: ledger unavailable (%s)", exc)
        return []


async def record_chapter_ledger(db=None, *, project_id=None, chapter_id=None,
                                entries=None) -> None:
    """把本章写出的总额登记进台账(通道见 :func:`read_chapter_ledger`)。

    位置说明(读代码确认的落库顺序):``to_safe_metadata()`` 的结果是在**质量门之后**
    才被写进 ``ChapterVersion`` 的,所以核对时本章的新版本行还不存在。这里登记到该章
    **最新已有的**版本行上(最近一次生成/修订留下的那一行);该章一个版本行都没有时
    无处可写,只记 warning —— 非阻断,下次本章再生成时就会登记。

    写用**独立短会话**:质量门可能跑在调用方的事务里(``generation_tasks`` 走的就是这种),
    绝不能替调用方 commit。任何失败都只记 warning,不抛。
    """
    if not chapter_id or not entries:
        return
    cid = _as_uuid(chapter_id)
    if cid is None:
        logger.warning("amount check: ledger write skipped (bad chapter id)")
        return
    try:
        from sqlalchemy import select

        from app.db.session import async_session_factory
        from app.models.project import ChapterVersion

        async with async_session_factory() as write_db:
            # A3 (2026-11-17): 行锁 —— metadata_json 是整列读改写,没有行级 CAS;
            # 不加锁时并发的作者保存/评审回执/另一次生成会把对方刚写的键覆盖掉。
            version = await write_db.scalar(
                select(ChapterVersion)
                .where(ChapterVersion.chapter_id == cid)
                .order_by(ChapterVersion.created_at.desc(), ChapterVersion.id.desc())
                .limit(1)
                .with_for_update())
            if version is None:
                logger.warning(
                    "amount check: no chapter version yet; ledger not recorded (%s)", cid)
                return
            metadata = dict(version.metadata_json) if isinstance(
                version.metadata_json, dict) else {}
            gate = metadata.get("quality_gate")
            gate = dict(gate) if isinstance(gate, dict) else {}
            gate["numeric_ledger"] = list(entries)
            metadata["quality_gate"] = gate
            # 整体赋值:JSON 列没有原地变更跟踪。
            version.metadata_json = metadata
            await write_db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: ledger write skipped (%s)", exc)


async def _extract_and_check(text: str, *, planned_text: str, extractor=None,
                             ledger=None) -> tuple[list[dict], list[dict] | None]:
    """抽一次金额并判定;返回 ``(冲突清单, 抽取结果或 None)``,任何失败返回 ``([], None)``。

    没有金额钉子 → 不调用模型(零成本边界;这里挡一次,因为"抽取结果"同时决定
    要不要写台账)。
    """
    if not text or not text.strip():
        return [], None
    planned = planned_amounts_from_text(planned_text or "")
    if not planned:
        return [], None
    try:
        rows = await (extractor or _default_extractor)(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: extraction failed (%s)", exc)
        return [], None
    if rows is None:
        logger.warning("amount check: extraction returned nothing; skipping (empty != no amounts)")
        return [], None
    found = ledger_conflicts(rows, ledger or [], planned)
    if found:
        logger.info("amount check: %d amount(s) disagree with the ledger: %s", len(found),
                    [f"{row['item']}={row['written']}" for row in found])
    return found, rows


async def check_chapter_amounts(text: str, *, planned_text: str,
                                extractor=None, ledger=None) -> list[dict]:
    """返回"与台账同名目已登记的数对不上"的清单;任何失败返回 ``[]``。

    计划文本为空 → 不调用模型、直接返回 ``[]``(零成本)。
    ``ledger`` 是此前各章登记过的条目(``[{"item", "amount"}]``);读不出来时传 ``[]``:
    此时只剩旧的计划口径,不会因为"没有可比对象"就凭空报警。
    """
    found, _rows = await _extract_and_check(
        text, planned_text=planned_text, extractor=extractor, ledger=ledger)
    return found


async def _read_ledger_safely(reader, *, db, project_id, chapter_id) -> list[dict]:
    """读台账:注入的 reader 优先;失败只记 warning 并当空台账(退化为没有可比对象)。"""
    try:
        rows = await (reader or read_chapter_ledger)(
            db=db, project_id=project_id, chapter_id=chapter_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: ledger unavailable (%s)", exc)
        return []
    return list(rows or [])


async def _write_ledger_safely(writer, *, db, project_id, chapter_id, rows) -> None:
    """登记台账:注入的 writer 优先;失败只记 warning(严格非阻断)。"""
    try:
        entries = ledger_entries_from_rows(rows)
        if not entries:
            return
        await (writer or record_chapter_ledger)(
            db=db, project_id=project_id, chapter_id=chapter_id, entries=entries)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: ledger write skipped (%s)", exc)


async def attach_numeric_conflicts(result, *, db=None, project_id=None, chapter_id=None,
                                   planned_text: str | None = None, extractor=None,
                                   ledger_reader=None, ledger_writer=None) -> list[dict]:
    """把清单写进质量门结果(``numeric_conflicts``),并把本章总额登记进金额台账。

    任何失败都静默跳过(只记 warning):不改 ``status``、不改正文、不抛。
    台账读写都是注入式(``ledger_reader`` / ``ledger_writer``),测试不需要真实库。
    """
    try:
        from app.config import settings
        if not bool(getattr(settings, "NUMERIC_CHECK_ENABLED", True)):
            return []
        text = getattr(result, "final_text", "") or ""
        if planned_text is None:
            planned_text = await resolve_plan_text(db, project_id=project_id, chapter_id=chapter_id)
        if not planned_text:
            # 零成本边界:本章没有金额钉子 → 读台账、抽金额、写台账全都不发生。
            return []
        ledger = await _read_ledger_safely(
            ledger_reader, db=db, project_id=project_id, chapter_id=chapter_id)
        found, rows = await _extract_and_check(
            text, planned_text=planned_text, extractor=extractor, ledger=ledger)
        if found:
            result.numeric_conflicts = found
        if rows:
            entries = ledger_entries_from_rows(rows)
            # 首选通道:随本次 quality_gate 元数据一起落库。核对发生在新版本行创建**之前**,
            # 所以只写"已有版本行"会让本章首次生成无处登记(后续章节就没有可比对象)。
            if entries and hasattr(result, "numeric_ledger"):
                result.numeric_ledger = entries
            await _write_ledger_safely(
                ledger_writer, db=db, project_id=project_id, chapter_id=chapter_id, rows=rows)
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check: attach failed (%s)", exc)
        return []
