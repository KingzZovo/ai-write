from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.services.character_roster import count_appearances, load_historical_roster


def test_shared_alias_is_not_given_to_first_sorted_owner():
    aliases = {'陈川': ['老陈'], '陈青': ['老陈']}
    for mapping in (aliases, dict(reversed(list(aliases.items())))):
        assert count_appearances('老陈来了。老陈离开。陈川在屋内。', set(aliases), mapping) == {'陈川': 1}


def test_ambiguous_alias_masks_embedded_names_without_losing_separate_mentions():
    aliases = {'林青': ['小林青'], '沈青': ['小林青']}
    assert count_appearances('小林青来了。林青在门外。', set(aliases), aliases) == {'林青': 1}


def test_duplicate_alias_for_same_owner_is_still_unique():
    assert count_appearances('老陈来了。', {'陈川'}, {'陈川': ['老陈', '老陈']}) == {'陈川': 1}


def test_alias_cannot_create_an_untracked_canonical_owner():
    aliases = {'甲乙': ['丙丁'], '丙丁': ['小丁']}
    assert count_appearances('小丁来了。丙丁走了。', {'甲乙'}, aliases) == {'甲乙': 1}


@pytest.mark.asyncio
async def test_ambiguous_mentions_do_not_move_a_characters_historical_last_seen():
    profiles = [('陈川', {'aliases': ['老陈']}), ('陈青', {'aliases': ['老陈']})]
    class Rows:
        def __init__(self): self.closed = False
        def __aiter__(self): return self.read()
        async def read(self):
            yield 1, '陈川在屋内。'
            yield 2, '陈青在门外。'
            yield 30, '老陈来了。'
        async def close(self): self.closed = True
    rows = Rows()
    db = NS(execute=AsyncMock(return_value=NS(all=lambda: profiles)), stream=AsyncMock(return_value=rows))
    result = await load_historical_roster(db, 'p', before_global_idx=31)
    by_name = {r['character_name']: r for r in result}
    assert by_name['陈川']['last_seen_chapter'] == 1
    assert by_name['陈青']['last_seen_chapter'] == 2
    assert all(r['appearance_count'] == 1 for r in result)
    assert rows.closed
