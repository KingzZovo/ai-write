"""In-memory read adapter, never evidence that production Cypher was run."""
from copy import deepcopy

from tests.support.world_rules import RuleResult


class CharacterGraph:
    def __init__(self, project_id):
        self.project_id = str(project_id)
        self.profiles, self.states, self.relationships, self.locations = [], [], [], []
        self.calls = []
        self.failure = None

    def session(self):
        graph = self

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def execute_read(self, work): return await work(self)
            async def run(self, query, **params):
                graph.calls.append((query, params))
                if graph.failure: raise graph.failure
                if 'AS profile_json' in query:
                    kind, candidates = 'profile', graph.profiles
                elif 'HAS_STATE' in query:
                    kind, candidates = 'state', graph.states
                elif 'RELATES_TO' in query:
                    kind, candidates = 'relationship', graph.relationships
                elif 'AT_LOCATION' in query:
                    kind, candidates = 'location', graph.locations
                else:
                    raise AssertionError(query)
                rows = [deepcopy(row) for row in candidates
                        if row.get('project_id', graph.project_id) == params['pid']]
                if kind != 'profile':
                    rows = [row for row in rows if kind == 'relationship' and row.get('origin') == 'author'
                            or row.get('source_chapter_id') and row.get('global_idx', 0) < params['idx']]
                    if kind == 'state':
                        rows = [row for row in rows if row.get('source_mode', 'chapter_delta') == 'chapter_delta']
                    if 'e.chapter_end >= $idx-1' in query:
                        rows = [row for row in rows if row.get('chapter_end') is None or row['chapter_end'] >= params['idx'] - 1]
                    rows.sort(key=lambda row: (row.get('name', ''), -(row.get('global_idx') or 0)))
                if 'LIMIT 2000' in query: rows = rows[:2000]
                return RuleResult([{key: value for key, value in row.items() if key not in {'project_id', 'source_mode'}} for row in rows])

        return Session()
