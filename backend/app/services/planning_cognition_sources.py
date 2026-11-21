"""Read every completed prior chapter's current cognition at the real arc edge."""
from copy import deepcopy

from sqlalchemy.exc import SQLAlchemyError

from app.services.character_cognition import load_ledger
from app.services.context_errors import RequiredContextUnavailable
from app.services.planning_cognition_material import (
    PlanningCognitionMaterialError, PlanningCognitionUnavailable, validate_cognition_source,
)


async def read_planning_cognition_source(db, project_id, character_context):
    boundary = character_context['before_global_idx']
    try:
        ledger = await load_ledger(db, project_id, before_global_idx=boundary)
        source = dict(version=1, project_id=str(project_id), before_global_idx=boundary,
            snapshot=deepcopy(ledger.snapshot), snapshot_hash=ledger.snapshot_hash, ledger=deepcopy(dict(ledger)))
        return validate_cognition_source(source, project_id, character_context)
    except PlanningCognitionMaterialError:
        raise
    except (SQLAlchemyError, RequiredContextUnavailable):
        raise PlanningCognitionUnavailable('此前完成章节的认知记录尚未就绪或暂时无法核对，请检查正文来源与认知提取/校订后重试。') from None
