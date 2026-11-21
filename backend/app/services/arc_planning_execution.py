"""Resume arc stages on the existing physical project lock and pinned session."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.arc_planning import ArcPlanningRequest
from app.services.arc_materialization import materialize_arc
from app.services.arc_outline import ARC_PHASE_TIMEOUT_SECONDS
from app.services.arc_planning_contract import (
    MAX_EXECUTIONS, ArcPlanningError, author_material, can_resume, validate_record,
)
from app.services.arc_planning_journal import (
    ArcPlanningBudgetExhausted, accepted_chapters, complete_outline, finish_attempt,
    next_attempt_query, next_stage, pending_call, reserve_attempt, validate_stage_output,
)
from app.services.arc_planning_requests import existing_request
from app.services.arc_planning_sources import read_planning_source
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable, lock_chapter_structure
from app.services.entity_sources import digest
from app.services.context_errors import RequiredContextUnavailable
from app.services.generation_execution import GenerationExecutionUnavailable, generation_execution_session
from app.services.production.control import PipelineConflict
from app.services.prompt_execution_snapshot import capture_prompt_snapshot
from app.services.prompt_registry import run_structured_prompt
from app.services.planning_cognition_retrieval import prepare_planning_cognition

logger = logging.getLogger(__name__)
STALE_SECONDS = 300  # Each bounded model phase is at most 240 seconds.


def _now():
    return datetime.now(timezone.utc)


def is_stale(row, now):
    stamp = row.heartbeat_at
    if stamp is None:
        return False
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    return stamp <= now - timedelta(seconds=STALE_SECONDS)


def _touch(row):
    row.revision += 1
    row.updated_at = row.heartbeat_at = _now()


def _checkpoint(row, journal):
    row.journal_json, row.journal_hash = journal, digest(journal)
    row.accepted_chapters = accepted_chapters(journal)
    spec = next_stage(journal)
    row.phase = spec["key"] if spec else "finalize"
    _touch(row)


async def _review(db, row, code, message):
    row.status, row.error_code, row.error_message = "needs_review", code, message
    row.execution_id = None
    _touch(row)
    # Do not repair a damaged journal/hash or overwrite an uncertain receipt.
    await db.commit()


async def _locked_request(db, project_id, request_id, execution_id=None):
    await lock_chapter_structure(db, project_id)
    row = await existing_request(db, request_id, lock=True)
    if row is None or row.project_id != project_id:
        return None
    if execution_id is not None and (row.execution_id != execution_id or row.status != "running"):
        return None
    return row


async def _current_source(db, row):
    try:
        validate_record(row)
        current = await read_planning_source(db, row.project_id, lock=True)
        if current != row.source_json or digest(current) != row.source_hash:
            raise ArcPlanningError("arc_planning_source_changed", "原全书材料、分卷或前弧来源已变化，已有规划保留待审；不能用新来源续接旧请求。")
    except ArcPlanningError as exc:
        await _review(db, row, exc.code, str(exc))
        return False
    return True


async def _model_phase(db, row, call):
    raw_text, output, error = "", None, ""
    try:
        data = await asyncio.wait_for(run_structured_prompt(
            call["task"], call["input"], db, project_id=row.project_id,
            strict_json=True, retry_on_validation_error=False,
            output_validator=lambda value: validate_stage_output(call, value, row.target_chapters),
            frozen_prompt=call["route"], temperature=call["temperature"],
        ), timeout=ARC_PHASE_TIMEOUT_SECONDS)
        if isinstance(data, dict) and (data.get("parse_error") or data.get("_parse_error")):
            raw_text = data.get("raw_text") if isinstance(data.get("raw_text"), str) else ""
            error = str(data.get("validation_error") or "模型输出未通过完整性校验")
        else:
            raw_text = json.dumps(data, ensure_ascii=False, allow_nan=False)
            output = validate_stage_output(call, data, row.target_chapters)
    except asyncio.TimeoutError:
        error = "模型阶段超时"
    except (ValueError, TypeError):
        error = "模型输出未通过完整性校验"
    except asyncio.CancelledError:
        raise  # The committed reservation remains spent; no late failure write.
    except Exception as exc:
        error = "模型阶段调用失败"
        logger.warning("Arc planning model phase failed (%s)", type(exc).__name__)
    return output, raw_text, error


async def execute_arc_request(db, project_id, request_id, *, manual=False, sessions=None):
    """The supplied db must be the live generation_execution_session."""
    async with asyncio.timeout(30):
        row = await _locked_request(db, project_id, request_id)
        if row is None or row.status in {"completed", "cancelled"}:
            return
        if row.status == "running" and not manual and not is_stale(row, _now()):
            return
        if row.status == "needs_review" and (not manual or not can_resume(row)):
            return
        other = await db.scalar(select(ArcPlanningRequest.id).where(
            ArcPlanningRequest.project_id == project_id, ArcPlanningRequest.id != request_id,
            ArcPlanningRequest.status.in_(("pending", "running"))).limit(1))
        if other is not None:
            raise ChapterOrderConflict("本书已有另一份未完成的弧规划，请先核对该请求。")
        if not await _current_source(db, row):
            return
        spec = next_stage(row.journal_json)
        stages = row.journal_json["stages"]
        if spec is not None and stages and stages[-1]["key"] == spec["key"] and len(stages[-1]["attempts"]) >= 2:
            await _review(db, row, "phase_budget_exhausted", "本阶段两次尝试已用完；完整原材料和已有规划保留待审。")
            return
        if spec is not None and row.execution_attempts >= MAX_EXECUTIONS:
            await _review(db, row, "execution_budget_exhausted", "三轮规划执行已用完，原请求与完整已有内容保留待审。")
            return
        # A complete plan needs only a save retry, which spends no new LLM budget.
        execution_id = uuid4()
        row.execution_id, row.status = execution_id, "running"
        row.execution_attempts += int(spec is not None)
        row.execution_history_json = [*deepcopy(row.execution_history_json), {
            "execution_id": str(execution_id), "number": row.execution_attempts,
            "kind": "planning" if spec is not None else "finalize", "started_at": _now().isoformat()}]
        row.error_code = row.error_message = ""
        _touch(row)
        await db.commit()

    while True:
        async with asyncio.timeout(30):
            row = await _locked_request(db, project_id, request_id, execution_id)
            if row is None or not await _current_source(db, row):
                return
            spec = next_stage(row.journal_json)
            if spec is None:
                plan = complete_outline(row.journal_json)
                delivery = await materialize_arc(db, project_id, author_material(row.request_json), row.target_volume_idx, plan)
                # Progress polling must not repeatedly transfer author setup or
                # opening prose. Their originals live in the request/outline.
                delivery["arc"] = {key: delivery["arc"][key] for key in
                                   ("title", "target_chapters", "status", "chapters_written")}
                row.result_json = {"version": 1, "request_id": str(row.id), "project_id": str(row.project_id),
                    "request_hash": row.request_hash, "journal_hash": row.journal_hash,
                    "chapter_count": row.target_chapters, **delivery}
                row.result_hash = digest(row.result_json)
                row.status, row.phase, row.execution_id = "completed", "done", None
                _touch(row)
                validate_record(row)
                # New volume, every chapter, outline and receipt commit together.
                await db.commit()
                return
            stages = row.journal_json["stages"]
            try:
                query = next_attempt_query(row.journal_json)
            except ArcPlanningBudgetExhausted as exc:
                await _review(db, row, "phase_budget_exhausted", str(exc))
                return
            route = None
            if not stages or stages[-1]["key"] != spec["key"]:
                try:
                    route = await capture_prompt_snapshot(spec["task"], db)
                except asyncio.CancelledError:
                    raise
                except GenerationExecutionUnavailable:
                    raise
                except Exception:
                    await _review(db, row, "route_unavailable", "本阶段注册提示或模型路由不可用；已完成规划保留，可修正配置后恢复原请求。")
                    return
            if query is not None:
                cognition_source = deepcopy(row.source_json['cognition_context'])
                original_journal_hash = row.journal_hash
                _touch(row)
                await db.commit()  # Release row locks before any retrieval I/O.
            else:
                journal = reserve_attempt(row.journal_json, route, now=_now().isoformat())
                _checkpoint(row, journal)
                call = pending_call(journal)
                await db.commit()

        if query is not None:
            retrieval_error, choice = None, None
            try:
                choice = await prepare_planning_cognition(cognition_source, query, db=db, sessions=sessions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                budget = isinstance(exc, RequiredContextUnavailable) and str(exc) == 'character_cognition_context_budget_exceeded'
                retrieval_error = ('arc_planning_cognition_budget_exceeded',
                    '本轮必需认知及完整原句超出容量，原材料与已完成规划保留，请先核对创作范围。') if budget else (
                    'arc_planning_cognition_retrieval_unavailable',
                    '本轮人物认知材料暂时无法准备；原材料和已完成规划保留，可恢复原请求。')
                logger.warning('Arc cognition selection unavailable (%s)', type(exc).__name__)
            async with asyncio.timeout(30):
                row = await _locked_request(db, project_id, request_id, execution_id)
                if row is None or not await _current_source(db, row):
                    return
                if row.journal_hash != original_journal_hash or next_attempt_query(row.journal_json) != query:
                    await _review(db, row, 'arc_planning_record_invalid', '认知准备期间原阶段记录发生变化，保留原材料待核对。')
                    return
                if retrieval_error is not None:
                    if (retrieval_error[0] == 'arc_planning_cognition_retrieval_unavailable'
                            and row.execution_attempts >= MAX_EXECUTIONS):
                        retrieval_error = ('execution_budget_exhausted',
                            '三轮规划执行已用完，本轮认知材料仍未就绪；原请求与已有内容保留待审，不能再续用预算。')
                    await _review(db, row, *retrieval_error)
                    return
                journal = reserve_attempt(row.journal_json, route, now=_now().isoformat(), cognition=choice)
                _checkpoint(row, journal)
                validate_record(row)
                call = pending_call(journal)
                await db.commit()  # Actual selection and spent attempt commit before the model call.

        output, raw_text, error = await _model_phase(db, row, call)

        async with asyncio.timeout(30):
            row = await _locked_request(db, project_id, request_id, execution_id)
            if row is None or not await _current_source(db, row):
                return
            journal = finish_attempt(row.journal_json, call["key"], call["number"],
                output=output, raw_text=raw_text, error=error, now=_now().isoformat())
            _checkpoint(row, journal)
            if error and call["number"] == 2:
                await _review(db, row, "phase_budget_exhausted", f"本阶段两次尝试已用完：{error}。原输出与此前规划保留待审。")
                return
            await db.commit()


async def run_arc_request(request_id, *, manual=False, sessions=None):
    if sessions is None:
        from app.db.session import async_session_factory
        sessions = async_session_factory
    identity = UUID(str(request_id))
    async with sessions() as reader:
        project_id = await reader.scalar(select(ArcPlanningRequest.project_id).where(ArcPlanningRequest.id == identity))
    if project_id is None:
        return {"status": "unavailable"}
    try:
        async with sessions() as base:
            async with generation_execution_session(base, project_id) as writer:
                await execute_arc_request(writer, project_id, identity, manual=manual, sessions=sessions)
    except asyncio.CancelledError:
        raise
    except (PipelineConflict, ChapterOrderConflict):
        return {"status": "busy"}
    except ChapterOrderUnavailable:
        return {"status": "unavailable"}
    except Exception as exc:
        # A save/connection may have succeeded before its reply was lost. Never
        # open another session to mark the old actor failed or rebuild a plan.
        logger.warning("Arc planning execution unconfirmed (%s), request %s retained", type(exc).__name__, identity)
        return {"status": "unconfirmed"}
    return {"status": "checked"}
