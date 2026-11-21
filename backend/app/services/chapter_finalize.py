"""章后统一收尾:三个入口(SSE / 队列 / 评估探针)共用**同一条链**。

## 为什么单独做这一个服务(2026-11-21)

此前三个入口各写一套章后逻辑:

* 桌面 SSE —— `app/api/generate.py`
* 后台队列 —— `app/tasks/generation_tasks.py`
* 评估探针 —— `backend/scripts/quality_eval/continuity_probe.py`

三处互相抄,字段名、成败处理、执行顺序都略有出入;更糟的是**因为这个近路存在,
正路一直没人走**,导致"评估工具能跑通"长期被当成"产品能用"。

对照 AI-Novel-Writing-Assistant 的"唯一执行链"原则(手动单章 / 批量 / 自动导演三个入口
必须落到同一个 runner,任何入口不得再维护第二套正文执行实现),这里把**章后收尾**
抽成唯一实现,三入口改为调用它。

## 顺序(与既有实况一致)

1. 摘要 —— `summarize_and_save_chapter`
2. 实体/关系/位置 —— `dispatch_for_chapter`
3. 卷桥接 —— `backfill_prev_volume_summary`(下一卷第 1~3 章要读的 L2 桥)
4. 伏笔缺铺垫 —— `check_chapter_foreshadow_setup`
5. 事实/经济一致性 —— `build_established_facts` + `check_chapter_against_facts`

**每一步独立成败**:任何一步失败只记 warning,绝不影响正文交付 —— 既有语义,保持不变。
返回结构化结果,供 SSE 发事件、队列记 params、探针写 records。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _summarize(db: Any, chapter_id: Any) -> bool:
    from app.services.chapter_summarizer import summarize_and_save_chapter

    await summarize_and_save_chapter(chapter_id=chapter_id, db=db, overwrite=True)
    return True


async def _dispatch_entities(chapter: Any, db: Any, project_id: Any, caller: str) -> bool:
    from app.services.entity_dispatch import dispatch_for_chapter

    await dispatch_for_chapter(chapter, db, caller=caller, project_id_hint=project_id)
    return True


async def _backfill_volume(volume_id: Any) -> bool:
    from app.services.memory import backfill_prev_volume_summary

    await backfill_prev_volume_summary(str(volume_id))
    return True


async def _extract_inline(
    project_id: Any, chapter_id: Any, chapter_idx: Any, *, attempts: int = 3
) -> dict:
    """同步执行实体 + 认知抽取(评估探针/无 Celery 环境用)。

    逻辑与派发路径一致,只是**执行模式**不同:生产把抽取交给 Celery(可重试、可回灌),
    探针没有 worker,只能同步跑 —— 但走的是同一套抽取函数,不再各写一份。
    """
    from app.db.neo4j import init_neo4j
    from app.services.cognition_reader import resolve_cognition_source
    from app.tasks.entity_tasks import _extract_chapter_async, _extract_cognition_async

    result: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        try:
            entity = await _extract_chapter_async(
                project_id, chapter_idx, caller="inline_finalize", chapter_id=chapter_id)
            result["entity"] = {"status": entity.get("status"), "reason": entity.get("reason"),
                                "attempts": attempt}
            break
        except Exception as exc:  # noqa: BLE001
            result["entity_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            try:
                await init_neo4j()
            except Exception:  # noqa: BLE001 - 驱动重连失败不改变结果语义
                pass
    try:
        await init_neo4j()
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(1, max(1, attempts) + 1):
        try:
            from app.db.session import async_session_factory

            async with async_session_factory() as db:
                source = await resolve_cognition_source(db, project_id, chapter_id)
                await db.commit()
            result["cognition"] = {"source": bool(source), "attempts": attempt}
            break
        except Exception as exc:  # noqa: BLE001
            result["cognition_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            try:
                await init_neo4j()
            except Exception:  # noqa: BLE001
                pass
    result["chapter_idx"] = chapter_idx
    return result


async def _foreshadow(db: Any, project_id: Any, chapter_id: Any) -> dict | None:
    from app.services.foreshadow_setup_check import (
        check_chapter_foreshadow_setup,
        setup_event_payload,
    )

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id, chapter_id=chapter_id)
    return setup_event_payload(report)


async def _facts(db: Any, project_id: Any, chapter_id: Any, full_text: str) -> list[dict]:
    from app.services.established_facts import (
        build_established_facts,
        check_chapter_against_facts,
    )

    facts = await build_established_facts(db, project_id, chapter_id)
    if not facts or not full_text:
        return []
    return check_chapter_against_facts(full_text, facts.lines)


async def finalize_chapter(
    *,
    db: Any,
    chapter: Any,
    project_id: Any,
    chapter_id: Any = None,
    volume_id: Any = None,
    full_text: str = "",
    caller: str = "chapter_finalize",
    # 执行模式:dispatch = 生产(交给 Celery);inline = 评估探针/无 worker 环境(同步跑)。
    # 逻辑唯一,只有执行方式不同 —— 这就是三入口共用同一套收尾的意思。
    mode: str = "dispatch",
    run_summary: bool = True,
    run_entities: bool = True,
    run_volume_bridge: bool = True,
    run_foreshadow: bool = True,
    run_facts: bool = True,
) -> dict[str, Any]:
    """章后收尾的唯一实现。任何一步失败都只记 warning,绝不影响正文交付。

    返回::

        {"caller": ..., "steps": {"summary": bool, "entities": bool, "volume_bridge": bool,
                                  "foreshadow": payload|None, "facts": [...]},
         "warnings": ["summary: TypeError: ...", ...]}
    """
    target_chapter_id = chapter_id or getattr(chapter, "id", None)
    target_project_id = project_id or getattr(chapter, "project_id", None)
    steps: dict[str, Any] = {}
    warnings: list[str] = []

    if run_summary and target_chapter_id is not None:
        try:
            steps["summary"] = await _summarize(db, target_chapter_id)
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响交付
            warnings.append(f"summary: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("finalize summary failed caller=%s chapter=%s err=%s",
                           caller, target_chapter_id, exc)

    if run_entities and chapter is not None:
        try:
            if mode == "inline":
                steps["entities"] = await _extract_inline(
                    target_project_id, target_chapter_id,
                    getattr(chapter, "chapter_idx", None))
            else:
                steps["entities"] = await _dispatch_entities(
                    chapter, db, target_project_id, caller)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"entities: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("finalize entity dispatch failed caller=%s chapter=%s err=%s",
                           caller, target_chapter_id, exc)

    if run_volume_bridge and volume_id is not None:
        try:
            steps["volume_bridge"] = await _backfill_volume(volume_id)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"volume_bridge: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("finalize volume bridge failed caller=%s volume=%s err=%s",
                           caller, volume_id, exc)

    if run_foreshadow and target_chapter_id is not None:
        try:
            payload = await _foreshadow(db, target_project_id, target_chapter_id)
            steps["foreshadow"] = payload
            if payload:
                logger.warning("foreshadow_setup_missing caller=%s chapter=%s planned=%s",
                               caller, target_chapter_id, payload.get("planned"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"foreshadow: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("finalize foreshadow check failed caller=%s chapter=%s err=%s",
                           caller, target_chapter_id, exc)

    if run_facts and full_text:
        try:
            findings = await _facts(db, target_project_id, target_chapter_id, full_text)
            steps["facts"] = findings
            if findings:
                logger.warning(
                    "fact_consistency_warning caller=%s chapter=%s kinds=%s",
                    caller, target_chapter_id,
                    sorted({f.get("kind") for f in findings if isinstance(f, dict)}))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"facts: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("finalize fact check failed caller=%s chapter=%s err=%s",
                           caller, target_chapter_id, exc)

    return {"caller": caller, "steps": steps, "warnings": warnings}
