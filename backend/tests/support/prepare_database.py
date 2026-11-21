"""Initialize an explicitly named disposable database for full API tests.

Usage: DATABASE_URL=postgresql+asyncpg://.../aiwrite_pipeline_test PYTHONPATH=. \
python -m tests.support.prepare_database
Never loads or mutates a production database; no DROP or migration shortcuts.
"""
import asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import settings


async def prepare():
    url = make_url(settings.DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or url.database != 'aiwrite_pipeline_test':
        raise RuntimeError('Refusing to initialize a non-disposable test database')
    from app.models import Base
    import app.main  # register all application models before creating tables
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    print(f'Prepared disposable test database: {len(Base.metadata.tables)} tables')


if __name__ == '__main__':
    asyncio.run(prepare())
