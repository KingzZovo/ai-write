"""In-memory graph adapter; real Cypher/PG locks need the service suite."""
from copy import deepcopy
from app.services.world_rule_material import rule_from_properties


def install_rule_graph(monkeypatch, project_id, rules=()):
    graph = RuleGraph(project_id, rules)
    async def driver(): return graph
    monkeypatch.setattr('app.services.world_rule_sources.current_rule_driver', driver)
    return graph


class RuleResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def consume(self):
        return None

    async def single(self):
        return self.rows[0] if self.rows else None

    def __aiter__(self):
        async def records():
            for row in self.rows:
                yield row
        return records()


class RuleGraph:
    def __init__(self, project_id, rules=()):
        self.project_id = str(project_id)
        self.rules = deepcopy(list(rules))
        self.calls = []
        self.failure = None
        self.before_run = None

    def session(self):
        graph = self

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def execute_read(self, work): return await work(self)
            async def execute_write(self, work):
                before = deepcopy(graph.rules)
                try:
                    return await work(self)
                except BaseException:
                    graph.rules[:] = before
                    raise
            async def run(self, query, **params):
                graph.calls.append((query, params))
                if graph.failure:
                    raise graph.failure
                if graph.before_run:
                    await graph.before_run(query, params)
                assert 'WorldRule' in query, query
                rows = [r for r in graph.rules if r.get('project_id', graph.project_id) == params['pid']]
                if 'cat' in params:
                    rows = [r for r in rows if (r['category'], r['text']) == (params['cat'], params['txt'])]
                if query.startswith('MERGE') and not rows:
                    row = dict(project_id=params['pid'], category=params['cat'], text=params['txt'])
                    graph.rules.append(row)
                    rows = [row]
                if 'SET w.category=' in query:
                    for row in rows:
                        row.update(category=params['new_cat'], text=params['new_txt'])
                if 'w.origin="author"' in query:
                    for row in rows:
                        row['origin'] = 'author'
                        row['id'] = row.get('id') or params['id']
                if 'DETACH DELETE w' in query:
                    for row in rows: graph.rules.remove(row)
                if 'RETURN properties(w)' in query:
                    return RuleResult([{'properties': deepcopy(row)} for row in rows])
                if 'AS rule_text' in query:
                    return RuleResult([rule_from_properties(row) for row in sorted(rows,
                        key=lambda r: (r['category'], r['text'], r.get('id') or ''))])
                if 'AS deleted' in query:
                    return RuleResult([{'deleted': len(rows)}])
                if 'AS matched' in query:
                    return RuleResult([{'matched': len(rows)}])
                if 'RETURN w.id AS id' in query:
                    return RuleResult([{'id': row.get('id')} for row in rows])
                return RuleResult()

        return Session()
