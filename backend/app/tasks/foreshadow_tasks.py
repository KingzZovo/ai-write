"""Background payoff reviews; author confirmation remains a separate action."""
from app.tasks import celery_app


@celery_app.task(name='foreshadows.review_chapter', soft_time_limit=360, time_limit=420)
def review_chapter(job_id):
    from app.tasks import _run_async_safe
    from app.services.foreshadow_review_worker import run_review_job
    return _run_async_safe(run_review_job(job_id))


@celery_app.task(name='foreshadows.replay_reviews')
def replay_reviews():
    from app.tasks import _run_async_safe
    from app.services.foreshadow_review_worker import replay_review_jobs
    return _run_async_safe(replay_review_jobs())


@celery_app.task(name='foreshadows.confirm_review', soft_time_limit=120, time_limit=150)
def confirm_review(job_id):
    from app.tasks import _run_async_safe
    async def run():
        from neo4j import AsyncGraphDatabase
        from app.config import settings
        from app.services.foreshadow_confirmation_worker import run_confirmation
        async with AsyncGraphDatabase.driver(settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)) as driver:
            return await run_confirmation(job_id, driver=driver)
    return _run_async_safe(run())


@celery_app.task(name='foreshadows.replay_confirmations')
def replay_confirmations():
    from app.tasks import _run_async_safe
    from app.services.foreshadow_confirmation_worker import replay_confirmations as recover
    return _run_async_safe(recover())


@celery_app.task(name='foreshadows.replay_writes', soft_time_limit=330, time_limit=360)
def replay_writes():
    from app.tasks import _run_async_safe
    async def run():
        from neo4j import AsyncGraphDatabase
        from app.config import settings
        from app.services.foreshadow_write_recovery import replay_foreshadow_writes
        async with AsyncGraphDatabase.driver(settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)) as driver:
            return await replay_foreshadow_writes(driver)
    return _run_async_safe(run())
