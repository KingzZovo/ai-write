"""Bound entity projection statements without splitting their owning transaction."""
from sqlalchemy import JSON, and_, cast, or_
from sqlalchemy.dialects.postgresql import insert, JSONB


async def upsert_projection_rows(db, model, rows, *, constraint=None, update_columns=(), update_condition=None):
    """Each statement stays below asyncpg's 32767-argument ceiling.

    The caller owns commit/rollback. A later batch error must roll back earlier
    batches together, rather than publishing an incomplete read projection.
    """
    count = 0
    size = min(500, max(1, 30000 // len(model.__table__.columns)))
    for start in range(0, len(rows), size):
        statement = insert(model).values(rows[start:start+size])
        target = {'constraint': constraint} if constraint else {'index_elements': list(model.__table__.primary_key.columns)}
        if update_columns:
            changes = []
            for column in update_columns:
                current, incoming = model.__table__.c[column], statement.excluded[column]
                # PostgreSQL JSON has no equality operator; JSONB comparison also
                # avoids rewrites caused only by object key order.
                if isinstance(current.type, JSON):
                    current, incoming = cast(current, JSONB), cast(incoming, JSONB)
                changes.append(current.is_distinct_from(incoming))
            changed = or_(*changes)
            if update_condition is not None:
                changed = and_(update_condition, changed)
            statement = statement.on_conflict_do_update(**target,
                set_={column: statement.excluded[column] for column in update_columns},
                where=changed)
        else:
            statement = statement.on_conflict_do_nothing(**target)
        result = await db.execute(statement)
        count += int(getattr(result, 'rowcount', 0) or 0)
    return count
