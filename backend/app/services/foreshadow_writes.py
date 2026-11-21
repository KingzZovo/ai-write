"""Direct author actions with source comparison, explicit commit, and replay proof."""
import asyncio

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.project import Project, Foreshadow, ForeshadowWriteReceipt as Receipt
from app.services.entity_materialization_lock import require_materialization_lock, MaterializationBusy
from app.services.foreshadow_graph_write import apply_graph_write, acknowledge_graph_write
from app.services.foreshadow_projection import project_current_foreshadow
from app.services.foreshadow_provenance import foreshadow_source_hash
from app.services.foreshadow_write_contract import (
    ForeshadowWriteConflict, ForeshadowProjectUnavailable, ForeshadowSyncPending,
    ForeshadowWriteUnconfirmed, receipt_for, require_same_receipt,
)


async def require_project(db, project_id, *, allow_deleted=False):
    query = select(Project.id).where(Project.id == project_id)
    if not allow_deleted:
        query = query.where(Project.deleted_at.is_(None))
    # SHARE prevents purge/soft delete until the mutation is settled. It remains
    # compatible with foreign-key checks on the same project's child rows.
    if (await db.execute(query.with_for_update(read=True)
            .execution_options(autoflush=False))).scalar_one_or_none() is None:
        raise ForeshadowProjectUnavailable('project_unavailable')


async def current_row(db, command, *, lock=False):
    query = select(*Foreshadow.__table__.columns).where(
        Foreshadow.project_id == command.project_id, Foreshadow.id == command.foreshadow_id)
    if lock:
        query = query.with_for_update()
    row = (await db.execute(query.execution_options(autoflush=False))).mappings().one_or_none()
    return dict(row) if row is not None else None


async def record_write_receipt(db, receipt):
    await db.execute(insert(Receipt).values(**receipt).on_conflict_do_nothing(index_elements=['id']))
    stored = (await db.execute(select(*Receipt.__table__.columns).where(
        Receipt.id == receipt['id']).with_for_update()
        .execution_options(autoflush=False))).mappings().one()
    require_same_receipt(stored, receipt)
    if (stored['source'] == receipt['source'] == 'neo4j' and stored['outcome'] == 'pending'
            and receipt['outcome'] in ('applied', 'rejected')):
        await db.execute(update(Receipt).where(Receipt.id == receipt['id'], Receipt.outcome == 'pending')
            .values(outcome=receipt['outcome'], reason=receipt['reason'])
            .execution_options(synchronize_session=False))
        return
    if any(stored[key] != receipt[key] for key in ('source', 'outcome', 'reason')):
        raise ForeshadowWriteConflict('inconsistent_write_receipt')


async def existing_receipt(db, command):
    stored = (await db.execute(select(*Receipt.__table__.columns).where(Receipt.id == command.id)
        .with_for_update().execution_options(autoflush=False))).mappings().one_or_none()
    if stored is not None:
        require_same_receipt(stored, receipt_for(command, stored['source'], stored['outcome']))
        if stored['outcome'] == 'rejected':
            raise ForeshadowWriteConflict(stored['reason'] or 'source_changed')
        if stored['outcome'] != 'applied' and not (stored['source'] == 'neo4j' and stored['outcome'] == 'pending'):
            raise ForeshadowWriteConflict('inconsistent_write_receipt')
    return stored


def result_after_write(command, row):
    if command.operation == 'delete':
        if row is not None:
            raise ForeshadowWriteConflict('changed_after_write')
        return None
    if row is None:
        raise ForeshadowWriteConflict('source_missing')
    return row


async def apply_author_write(db, driver, command):
    """Return only after SQL commit; failed graph projections remain recoverable.

    Graph requests first commit their immutable SQL identity, then the graph
    change and graph receipt commit together. Local changes/final outcomes and
    graph projections/final outcomes share their respective SQL transaction.
    """
    graph_applied = False
    try:
        async with asyncio.timeout(90):
            await require_materialization_lock(db, command.project_id)
            await require_project(db, command.project_id)
            stored = await existing_receipt(db, command)
            if stored is not None and stored['outcome'] == 'applied':
                row = await current_row(db, command)
                await db.commit()
                return result_after_write(command, row)

            # A pending graph request keeps its original owner even if the PG
            # projection was deleted or its provenance changed during recovery.
            use_graph = stored is not None
            if not use_graph:
                row = await current_row(db, command, lock=True)
                foreign_id = False
                if row is None and command.operation == 'create':
                    foreign_id = (await db.execute(select(Foreshadow.id).where(
                        Foreshadow.id == command.foreshadow_id))).scalar_one_or_none() is not None
                if foreign_id:
                    receipt = receipt_for(command, 'unknown', 'rejected', 'already_exists')
                elif row is not None and row['source'] == 'lifecycle':
                    reason = None
                    if command.operation == 'create':
                        reason = 'already_exists'
                    elif foreshadow_source_hash(row) != command.expected_version:
                        reason = 'source_changed'
                    elif command.operation == 'resolve' and row['status'] == 'resolved':
                        reason = 'already_resolved'
                    if reason is None and (command.operation == 'delete' or command.changes):
                        statement = delete(Foreshadow) if command.operation == 'delete' else update(Foreshadow).values(**command.changes)
                        await db.execute(statement.where(Foreshadow.id == command.foreshadow_id,
                            Foreshadow.project_id == command.project_id, Foreshadow.source == 'lifecycle')
                            .execution_options(synchronize_session=False))
                        row = await current_row(db, command)
                    receipt = receipt_for(command, 'lifecycle', 'applied' if reason is None else 'rejected', reason)
                elif row is not None and row['source'] != 'neo4j':
                    receipt = receipt_for(command, 'unknown', 'rejected', 'unknown_source')
                else:
                    # Commit the request binding before any graph mutation.
                    # A lost graph/SQL acknowledgement must not free this ID
                    # for a different operation in either owning store.
                    await record_write_receipt(db, receipt_for(command, 'neo4j', 'pending'))
                    await db.commit()
                    await require_materialization_lock(db, command.project_id)
                    await require_project(db, command.project_id)
                    stored = await existing_receipt(db, command)
                    if stored is None:
                        raise ForeshadowWriteConflict('missing_write_receipt')
                    if stored['outcome'] == 'applied':
                        row = await current_row(db, command)
                        await db.commit()
                        return result_after_write(command, row)
                    use_graph = True
            if use_graph:
                receipt = await apply_graph_write(driver, command)
                if receipt['outcome'] == 'applied':
                    graph_applied = True
                if receipt['outcome'] == 'applied' or receipt['reason'] in ('source_changed', 'source_missing', 'already_resolved', 'already_exists'):
                    row = await project_current_foreshadow(db, driver, command.project_id, command.foreshadow_id)
            await record_write_receipt(db, receipt)
            await db.commit()
    except (ForeshadowWriteConflict, ForeshadowProjectUnavailable, MaterializationBusy):
        await db.rollback()
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        error = ForeshadowSyncPending if graph_applied else ForeshadowWriteUnconfirmed
        raise error(type(exc).__name__) from exc

    if receipt['outcome'] != 'applied':
        raise ForeshadowWriteConflict(receipt['reason'] or 'source_changed')
    if receipt['source'] == 'neo4j':
        try:
            async with asyncio.timeout(10):
                await acknowledge_graph_write(driver, receipt)
        except Exception:
            pass  # SQL is committed; the graph receipt can safely be retried.
    return result_after_write(command, row)
