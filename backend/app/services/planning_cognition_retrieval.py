"""Per-attempt cognition selection, called after the arc read transaction ends."""
from copy import deepcopy

from app.services.cognition_context import history_fits, render_history
from app.services.cognition_retrieval import CognitionPrompt, CognitionSelection, resolve_prompt_selection
from app.services.cognition_validation import CognitionLedger
from app.services.context_errors import RequiredContextUnavailable
from app.services.planning_cognition_material import COGNITION_CHARS, selection_receipt, validate_cognition_source


async def prepare_planning_cognition(source, query, *, db=None, sessions=None, client=None,
                                     space_loader=None, lock_factory=None):
    validate_cognition_source(source)
    if db is not None and db.in_transaction():
        raise RequiredContextUnavailable('character_cognition_transaction_open')
    ledger = CognitionLedger(deepcopy(source['ledger']))
    ledger.snapshot_hash = source['snapshot_hash']
    ledger.snapshot = deepcopy(source['snapshot'])
    # Mandatory records must fit before paying for retrieval. This diagnostic
    # rendering is never the long-history material supplied to the model.
    text = render_history(ledger, COGNITION_CHARS, context_text=query)
    if history_fits(ledger, COGNITION_CHARS):
        choice = CognitionSelection(text, (), None)
    else:
        prompt = CognitionPrompt.create(source['project_id'], ledger, query, (), COGNITION_CHARS,
            before_global_idx=source['before_global_idx'], before_chapter_id=None)
        choice = await resolve_prompt_selection(prompt, db=db, sessions=sessions, client=client,
            space_loader=space_loader, lock_factory=lock_factory)
    return selection_receipt(source, query, choice)
