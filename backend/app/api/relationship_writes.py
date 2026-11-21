"""Versioned author relationship operations and current source inspection."""
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from neo4j import AsyncDriver
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from app.services.entity_materialization_lock import MaterializationBusy
from app.services.relationship_graph_write import read_relationship_snapshot
from app.services.relationship_writes import apply_author_relationship_write, require_relationship_project
from app.services.relationship_write_contract import (
    make_relationship_command, RelationshipWriteConflict, RelationshipProjectUnavailable,
    RelationshipWriteUnconfirmed, RelationshipSyncPending,
)

router = APIRouter()


class RelationshipIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    rel_type: str = Field(..., min_length=1, max_length=100)

    @field_validator('source', 'target', 'rel_type', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class Neo4jRelationshipCreateRequest(RelationshipIdentity):
    chapter_start: int = Field(default=0, ge=0, strict=True)
    label: str = Field(default='', max_length=200)
    note: str = ''
    sentiment: str = Field(default='neutral', max_length=20)


class Neo4jRelationshipUpdateRequest(RelationshipIdentity):
    new_rel_type: str | None = Field(None, min_length=1, max_length=100)
    label: str | None = Field(None, max_length=200)
    note: str | None = None
    sentiment: str | None = Field(None, max_length=20)


class Neo4jRelationshipDeleteRequest(RelationshipIdentity):
    pass


def public_error(error):
    if isinstance(error, RelationshipProjectUnavailable):
        return HTTPException(404, detail={'code': 'project_unavailable', 'message': '作品不存在或已删除。'})
    if isinstance(error, RelationshipWriteConflict):
        messages = {
            'source_missing': '关系来源已不存在，请刷新核对；原输入已保留。',
            'source_changed': '关系记录已被修改，请刷新核对；原输入已保留。',
            'already_exists': '这段关系已经存在，请打开已有记录修改。',
            'target_exists': '目标关系类型已有记录，请分别核对，未合并或覆盖。',
            'request_id_reused': '此请求编号已用于另一项修改，请核对原请求。',
        }
        code = str(error) if str(error) in messages else 'relationship_conflict'
        return HTTPException(409, detail={'code': code, 'message': messages.get(code, '关系资料存在冲突，请刷新核对；原输入已保留。')})
    if isinstance(error, MaterializationBusy):
        return HTTPException(409, detail={'code': 'sync_busy', 'message': '资料正在同步，请稍后重试此请求。'})
    if isinstance(error, RelationshipSyncPending):
        return HTTPException(503, detail={'code': 'sync_pending', 'message': '关系修改已记录，显示资料尚待同步。请保留原请求并重试确认。'})
    if isinstance(error, ValueError):
        return HTTPException(422, detail={'code': 'invalid_relationship_request', 'message': '请提供有效的关系内容、原版本和固定请求编号。'})
    return HTTPException(503, detail={'code': 'write_unconfirmed', 'message': '尚未确认关系操作结果，请保留原请求并重试确认。'})


async def perform_write(project_id, body, operation, request_id, version, db, neo4j):
    try:
        values = body.model_dump(exclude_unset=True)
        for key in ('source', 'target', 'rel_type'):
            values.pop(key, None)
        command = make_relationship_command(request_id, project_id, body.source, body.target,
            body.rel_type, operation, version, values)
        return await apply_author_relationship_write(db, neo4j, command)
    except (RelationshipWriteConflict, RelationshipProjectUnavailable, RelationshipWriteUnconfirmed,
            RelationshipSyncPending, MaterializationBusy, ValueError) as exc:
        raise public_error(exc) from exc


@router.get('/relationships/record')
async def relationship_record(project_id: UUID, source: str = Query(..., min_length=1, max_length=200),
        target: str = Query(..., min_length=1, max_length=200), rel_type: str = Query(..., min_length=1, max_length=100),
        db: AsyncSession = Depends(get_db), neo4j: AsyncDriver = Depends(get_neo4j)):
    try:
        identity = RelationshipIdentity(source=source, target=target, rel_type=rel_type)
        async with asyncio.timeout(30):
            await require_relationship_project(db, project_id)
            return await read_relationship_snapshot(neo4j, project_id, identity.source, identity.target, identity.rel_type)
    except RelationshipProjectUnavailable as exc:
        raise public_error(exc) from exc
    except (ValueError, RelationshipWriteConflict) as exc:
        raise public_error(exc) from exc
    except Exception as exc:
        raise HTTPException(503, detail={'code': 'relationship_read_unavailable', 'message': '未能完整读取关系来源，请稍后重试。'}) from exc


@router.post('/relationships', status_code=202)
async def create_relationship(project_id: UUID, body: Neo4jRelationshipCreateRequest,
        request_id: UUID = Header(..., alias='Idempotency-Key'),
        db: AsyncSession = Depends(get_db), neo4j: AsyncDriver = Depends(get_neo4j)):
    return await perform_write(project_id, body, 'create', request_id, None, db, neo4j)


@router.put('/relationships', status_code=202)
async def update_relationship(project_id: UUID, body: Neo4jRelationshipUpdateRequest,
        request_id: UUID = Header(..., alias='Idempotency-Key'), version: str = Header(..., alias='If-Match'),
        db: AsyncSession = Depends(get_db), neo4j: AsyncDriver = Depends(get_neo4j)):
    return await perform_write(project_id, body, 'update', request_id, version, db, neo4j)


@router.delete('/relationships', status_code=202)
async def delete_relationship(project_id: UUID, source: str | None = None, target: str | None = None,
        rel_type: str | None = None, body: Neo4jRelationshipDeleteRequest | None = None,
        request_id: UUID = Header(..., alias='Idempotency-Key'), version: str = Header(..., alias='If-Match'),
        db: AsyncSession = Depends(get_db), neo4j: AsyncDriver = Depends(get_neo4j)):
    try:
        identity = body or Neo4jRelationshipDeleteRequest(source=source or '', target=target or '', rel_type=rel_type or '')
    except ValueError as exc:
        raise public_error(exc) from exc
    return await perform_write(project_id, identity, 'delete', request_id, version, db, neo4j)
