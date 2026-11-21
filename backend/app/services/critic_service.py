"""Source-bound model review with explicit delivery status.

Continuity material comes from the prepared ContextPack. Duplicate location
and death queries were lossy and mistook omission for a contradiction.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.anti_ai_scanner import scan_anti_ai
from app.services.prompt_registry import run_text_prompt
from app.services.review_contract import (
    COMPLETE, INCOMPLETE, make_critic_report, parse_review_json, validate_critic_issues,
)

logger = logging.getLogger(__name__)

_EDITORIAL_CONTRACT = (
    '结合完整原文审读。人物误信、怀疑、隐瞒、不可靠叙述、否定、回忆、留白和合理省略'
    '不能直接当作作者错误；较早的未知不能推翻后来的获知，没重复地点或没写赶路不是矛盾。'
    '先看人物选择、关键场面和转折后果，再看语言；不要求固定对白比例、微动作、比喻或悬念。'
    '只报告能说明具体阅读问题的发现，不以模型分数宣布文学合格。'
    '输出一个JSON对象：{"issues":[{"severity":"hard",'
    '"category":"类别","desc":"具体问题及依据","location":"本稿逐字原句",'
    '"suggestion":"保留作者设定的最小修改建议"}]}。'
    'severity可选hard/soft/info；hard仅用于原文和已知事实能核实的严重矛盾，soft用于需作者审读的场面/人物/语言问题，'
    'info用于提醒。hard/soft必须在location附本稿确切短引，不能仅写人名或段落号代替证据。'
    '没有发现时返回{"issues":[]}。材料不足或无法完成时返回'
    '{"review_status":"incomplete","issues":[],"reason":"缺少的材料或未完成项"}，不要声称无问题。'
)


def _critic_split_enabled() -> bool:
    return os.getenv('CRITIC_SPLIT_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off', '')


def _critic_consistency_llm_enabled() -> bool:
    return os.getenv('CRITIC_CONSISTENCY_LLM_ENABLED', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def _has_consistency_hit(issues: list[dict[str, Any]]) -> bool:
    categories = {'consistency', 'time_reversal', 'geo_jump', 'item_missing', 'location',
                  'resource_continuity', 'consistency_time_reversal', 'consistency_geo_jump',
                  'consistency_item_missing'}
    return any(i.get('category') in categories for i in issues)


async def _run_llm_critic(
    user_content: str, db: AsyncSession, *, project_id: str, chapter_id: str | None, draft: str,
) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    stages: list[dict] = []
    tasks = ('critic_hard', 'critic_soft') if _critic_split_enabled() else ('critic',)
    # Cold prompt-cache lookups use this session; AsyncSession cannot be shared
    # by concurrent calls. Keep successful streams inspectable when one fails.
    for task in tasks:
        try:
            result = await run_text_prompt(
                task, user_content, db, project_id=project_id, chapter_id=chapter_id,
                extra_system=_EDITORIAL_CONTRACT,
            )
            parsed = parse_review_json(result)
            if (not isinstance(parsed, dict) or parsed.get('parse_error') or parsed.get('_parse_error')
                    or parsed.get('review_status', COMPLETE) != COMPLETE):
                raise ValueError('review_object_required')
            items = validate_critic_issues(parsed.get('issues'), draft=draft)
            issues.extend({**i, 'source': 'llm', 'critic_stream': task} for i in items)
            stages.append({'name': task, 'status': COMPLETE})
        except Exception as exc:
            logger.warning('critic stream %s unavailable (%s)', task, type(exc).__name__)
            stages.append({'name': task, 'status': INCOMPLETE,
                           'code': 'invalid_response' if isinstance(exc, (ValueError, TypeError)) else 'call_failed'})
    return issues, stages


async def _run_consistency_llm_check(
    user_content: str, db: AsyncSession, *, project_id: str, chapter_id: str | None,
    issues: list[dict],
) -> tuple[list[dict], list[dict]]:
    """The registered quick-check returns a binary verdict for ONE finding.

    Rejected findings stay in the stage audit record, outside issue counts.
    Failure keeps the initial finding visible and makes the review incomplete.
    """
    retained: list[dict] = []
    stages: list[dict] = []
    for index, issue in enumerate(issues):
        if issue.get('severity') != 'hard' or not _has_consistency_hit([issue]):
            retained.append(issue)
            continue
        stage = {'name': f'consistency_llm_check:{index}', 'finding': issue}
        try:
            result = await run_text_prompt(
                'consistency_llm_check',
                user_content + '\n\n候选问题：\n' + json.dumps(issue, ensure_ascii=False), db,
                project_id=project_id, chapter_id=chapter_id,
                extra_system=('核查单个候选问题；误信、否定、回忆与合理省略不等于矛盾。'
                              '输出结构为{"is_issue":true,"severity":"hard",'
                              '"reason":"结合原句的判断依据"}。is_issue必须是布尔值，severity可选hard/soft/info。'),
            )
            verdict = parse_review_json(result)
            if (not isinstance(verdict, dict) or type(verdict.get('is_issue')) is not bool
                    or verdict.get('severity') not in {'hard', 'soft', 'info'}
                    or not isinstance(verdict.get('reason'), str) or not verdict['reason'].strip()):
                raise ValueError('invalid_consistency_verdict')
            stage.update(status=COMPLETE, verdict=verdict)
            if verdict['is_issue']:
                retained.append({**issue, 'severity': verdict['severity']})
        except Exception as exc:
            logger.warning('critic consistency verdict unavailable (%s)', type(exc).__name__)
            stage.update(status=INCOMPLETE, code='verdict_unavailable')
            retained.append(issue)
        stages.append(stage)
    return retained, stages


async def run_critic(
    *, draft: str, project_id: str, chapter_id: str | None, db: AsyncSession,
    pack_summary: str = '', chapter_idx: int | None = None, item_names: list[str] | None = None,
) -> dict[str, Any]:
    observations: list[dict] = []
    stages: list[dict] = []
    if not isinstance(draft, str) or not draft.strip():
        return make_critic_report(draft or '', issues=[], stages=[
            {'name': 'draft', 'status': INCOMPLETE, 'code': 'empty_draft'},
        ])
    # Pattern matches are observations, never reasons to force a rewrite.
    try:
        observations = await scan_anti_ai(draft, db)
        stages.append({'name': 'anti_ai_observations', 'status': COMPLETE})
    except Exception as exc:
        logger.warning('critic observation scan unavailable (%s)', type(exc).__name__)
        stages.append({'name': 'anti_ai_observations', 'status': INCOMPLETE, 'code': 'scan_failed'})

    user_content = (f'<上下文摘要>\n{pack_summary}\n</上下文摘要>\n\n'
                    f'<draft>\n{draft}\n</draft>\n\n请按输出结构逐项审读。')
    issues, llm_stages = await _run_llm_critic(
        user_content, db, project_id=project_id, chapter_id=chapter_id, draft=draft,
    )
    stages.extend(llm_stages)
    if _critic_consistency_llm_enabled():
        issues, verdict_stages = await _run_consistency_llm_check(
            user_content, db, project_id=project_id, chapter_id=chapter_id, issues=issues,
        )
        stages.extend(verdict_stages)
    return make_critic_report(draft, issues=issues, stages=stages, observations=observations)
