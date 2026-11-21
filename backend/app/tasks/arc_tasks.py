"""Durable arc planning is distinct from the five independent generation tasks."""
from app.tasks import celery_app
from app.tasks.common import _run_async


@celery_app.task(name="tasks.run_arc_planning")
def run_arc_planning(request_id, manual=False):
    from app.services.arc_planning_execution import run_arc_request
    return _run_async(run_arc_request(request_id, manual=manual))


@celery_app.task(name="tasks.dispatch_arc_planning")
def dispatch_arc_planning():
    from app.services.arc_planning_dispatch import dispatch_pending_arcs
    return _run_async(dispatch_pending_arcs())
