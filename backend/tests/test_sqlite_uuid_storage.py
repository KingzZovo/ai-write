"""SQLite substitutes must preserve PostgreSQL UUID values exactly."""
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects import postgresql


@pytest.mark.parametrize('raw', [
    '29305212-9008-4958-8742-82983617e457',  # The full-suite failure's random ID.
    '11111111-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111112',
])
def test_uuid_identity_survives_sqlite_storage(raw):
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    table = sa.Table('uuid_identity', metadata, sa.Column('id', PG_UUID(as_uuid=True), primary_key=True))
    with engine.begin() as conn:
        metadata.create_all(conn)
        value = UUID(raw)
        conn.execute(table.insert().values(id=value))
        assert conn.scalar(sa.select(table.c.id)) == value
    engine.dispose()


def test_nearby_numeric_uuids_are_distinct_in_sqlite():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    table = sa.Table('uuid_identity', metadata, sa.Column('id', PG_UUID(as_uuid=True), primary_key=True))
    values = [UUID(f'11111111-1111-4111-8111-11111111111{i}') for i in (1, 2)]
    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(table.insert(), [{'id': v} for v in values])
        assert set(conn.scalars(sa.select(table.c.id))) == set(values)
    engine.dispose()


def test_test_adapter_does_not_change_postgresql_uuid_ddl():
    table = sa.Table('uuid_identity', sa.MetaData(), sa.Column('id', PG_UUID(as_uuid=True), primary_key=True))
    ddl = str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
    assert 'id UUID NOT NULL' in ddl
    assert 'CHAR(32)' not in ddl
