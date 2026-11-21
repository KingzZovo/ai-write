"""Decide whether a graph profile may replace an existing character card."""
from copy import deepcopy


AUTHOR_EDIT = 'author_edit'


def projected_profile(existing, incoming, origin, *, preserve_existing=False):
    """None means preserve; an empty dict is a valid explicit author edit.

    Historical graph profiles have no reliable author provenance. Existing
    empty cards may also represent an author's deliberate removal of content.
    New cards are initialized separately by the materializer.
    """
    if preserve_existing or not isinstance(incoming, dict):
        return None
    if origin == AUTHOR_EDIT:
        return deepcopy(incoming)
    return None
