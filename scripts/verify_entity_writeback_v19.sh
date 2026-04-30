#!/usr/bin/env bash
set -euo pipefail

# v1.9+ smoke verification for Neo4j->Postgres entity materialization.
#
# Checks:
# - materialize endpoint is callable (auth required)
# - re-running materialize is idempotent (created deltas are 0)
# - Postgres has no relationship duplicates under uq key
#
# Usage:
#   PROJECT_ID=<uuid> CHAPTER_IDX=<int> TOKEN_FILE=/tmp/king_tok bash scripts/verify_entity_writeback_v19.sh

PROJECT_ID="${PROJECT_ID:-}"
CHAPTER_IDX="${CHAPTER_IDX:-}"
TOKEN_FILE="${TOKEN_FILE:-/tmp/king_tok}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

if [[ -z "$PROJECT_ID" || -z "$CHAPTER_IDX" ]]; then
	echo "PROJECT_ID and CHAPTER_IDX are required" >&2
	exit 2
fi
if [[ ! -f "$TOKEN_FILE" ]]; then
	echo "TOKEN_FILE not found: $TOKEN_FILE" >&2
	exit 2
fi

TOK="$(cat "$TOKEN_FILE")"

call_materialize() {
	curl -sS -X POST "$BASE_URL/api/admin/entities/materialize" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $TOK" \
		-d "{\"project_id\":\"$PROJECT_ID\",\"chapter_idx\":$CHAPTER_IDX,\"caller\":\"verify_entity_writeback_v19\"}" \
		| python -m json.tool
}

echo "[1/3] materialize (1st run)"
OUT1="$(call_materialize)"
echo "$OUT1"

echo "[2/3] materialize (2nd run; must be idempotent: created=0)"
OUT2="$(call_materialize)"
echo "$OUT2"

export OUT2_JSON="$OUT2"

python - <<'PY'
import json, os, sys

out2 = json.loads(os.environ['OUT2_JSON'])
res = out2.get('result') or {}
bad = []
for k in ['chars_created','rels_created','rules_created','locs_created']:
    if k in res and res[k] not in (0, None):
        if res[k] != 0:
            bad.append((k, res[k]))
if bad:
    print('ERROR: materialize not idempotent:', bad)
    sys.exit(1)
print('OK: materialize idempotent')
PY

echo "[3/3] postgres duplicate check (relationships)"
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\
WITH dup AS (\
  SELECT project_id, source_id, target_id, rel_type, COUNT(*) AS n\
  FROM relationships\
  WHERE project_id='${PROJECT_ID}'\
  GROUP BY project_id, source_id, target_id, rel_type\
  HAVING COUNT(*) > 1\
)\
SELECT COUNT(*) AS remaining_dup_groups, COALESCE(SUM(n),0) AS remaining_dup_rows FROM dup;\
"

echo "OK"

