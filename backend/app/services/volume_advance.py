"""整书任务自动续卷:一卷跑完,自动起下一卷。

## 为什么需要

项目约束是「**同一作品同时只能有一个进行中的整书任务**」,所以百万字长篇必须
逐卷串行。此前续卷靠外部调度器每小时检查 —— 但那是进程外的东西:实测
2026-11-27 该任务 `lastRunAt` 停在 11-21、`nextRunAt` 一直往后滚,**从未真正触发**。
后果:卷 2 跑完后链条就停在那里,卷 3–5 一个字没写,而日志里没有任何错误。

把续卷放进项目自己的 beat(与其它 recovery 任务同走 `recovery` 队列),链条就不再
依赖任何外部进程 —— 只要 worker 在跑,它就会自己往下走。

## 开关

项目的 `settings_json` 里 `auto_advance_volumes: true` 才生效。自动续卷会持续花钱
调模型,必须是作者的显式选择,不能默认打开。

## 边界

- 只在**本作品没有 ACTIVE_STATES 的整书任务**时才起新卷;
- 只挑「正文未完成」的**最小 volume_idx**,且该卷必须有章节行;
- 起新卷失败(`PipelineConflict`:章节正在写入 / 已有在跑的任务)只跳过,下一 tick 再来;
- 所有卷正文都齐了 → 什么都不做(不重复起轮)。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import case, func, select

logger = logging.getLogger(__name__)

AUTO_ADVANCE_SETTING = 'auto_advance_volumes'


def _enabled(settings: Any) -> bool:
    if not isinstance(settings, dict):
        return False
    value = settings.get(AUTO_ADVANCE_SETTING)
    return value is True or str(value).strip().lower() == 'true'


def first_unfinished_volume(rows) -> Any | None:
    """挑出正文未完成的最小卷;全齐返回 None。

    `rows` 每项需带 `volume_idx / total / done`,已按 volume_idx 升序。
    """
    for row in rows:
        if row.total and row.done < row.total:
            return row
    return None


async def advance_project_volumes(sessions, *, dispatch=None, limit=20) -> dict[str, Any]:
    """给所有开了开关的作品各推进一步。返回本轮做了什么,便于观测。"""
    from app.db.session import async_session_factory
    from app.models.pipeline import PipelineRun
    from app.models.project import Chapter, Project, Volume
    from app.services.pipeline_service import create_pipeline
    from app.services.production.control import ACTIVE_STATES, PipelineConflict, request_start

    sessions = sessions or async_session_factory
    result: dict[str, Any] = {'examined': 0, 'started': [], 'skipped': []}

    async with sessions() as db:
        candidates = (await db.execute(select(Project.id, Project.settings_json)
            .where(Project.deleted_at.is_(None)))).all()
    enabled_ids = [row.id for row in candidates if _enabled(row.settings_json)][:max(1, limit)]
    result['examined'] = len(enabled_ids)

    for project_id in enabled_ids:
        async with sessions() as db:
            active = await db.scalar(select(PipelineRun.id).where(
                PipelineRun.project_id == project_id,
                PipelineRun.state.in_(ACTIVE_STATES),
            ).limit(1))
            if active is not None:
                result['skipped'].append({'project_id': str(project_id), 'reason': 'active_pipeline'})
                continue
            rows = (await db.execute(
                select(
                    Volume.id.label('volume_id'),
                    Volume.volume_idx.label('volume_idx'),
                    func.count(Chapter.id).label('total'),
                    func.count(case((Chapter.word_count > 0, Chapter.id))).label('done'),
                )
                .join(Chapter, Chapter.volume_id == Volume.id)
                .where(Volume.project_id == project_id)
                .group_by(Volume.id, Volume.volume_idx)
                .order_by(Volume.volume_idx)
            )).all()
            target = first_unfinished_volume(rows)
            if target is None:
                result['skipped'].append({'project_id': str(project_id), 'reason': 'all_volumes_done'})
                continue
            try:
                pipeline = await create_pipeline(db, project_id, target.volume_id)
                await db.flush()
                await request_start(db, pipeline.id)
                await db.commit()
                pipeline_id = str(pipeline.id)
            except PipelineConflict as exc:
                await db.rollback()
                result['skipped'].append({'project_id': str(project_id), 'reason': str(exc)})
                continue
            except ValueError as exc:
                await db.rollback()
                logger.warning('volume advance refused project=%s volume_idx=%s err=%s',
                               project_id, target.volume_idx, exc)
                result['skipped'].append({'project_id': str(project_id), 'reason': str(exc)})
                continue
            logger.info('volume advance started project=%s volume_idx=%s pipeline=%s',
                        project_id, target.volume_idx, pipeline_id)
            result['started'].append({'project_id': str(project_id),
                                      'volume_idx': target.volume_idx,
                                      'pipeline_id': pipeline_id})

        # 派发放在会话之外:即使这里失败,queued 行本身就是持久 outbox,
        # beat 的 pipeline-command-recovery 会把它捡回来。
        if dispatch is None:
            from app.services.production.dispatch import dispatch_queued_commands
            dispatch = dispatch_queued_commands
        try:
            await dispatch(pipeline_id)
        except Exception as exc:  # noqa: BLE001 —— 已落库的 queued 行会被 beat 重试
            logger.warning('volume advance dispatch deferred pipeline=%s err=%s', pipeline_id, exc)

    return result
