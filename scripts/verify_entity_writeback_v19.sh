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

require_pg_constraints() {
	local missing=0
	for cname in \
		uq_relationships_rel_key \
		uq_world_rules_key \
		uq_locations_project_name \
		uq_character_locations_key \
		uq_character_states_key
	do
		if ! docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -Atqc \
			"SELECT 1 FROM pg_constraint WHERE conname='${cname}' LIMIT 1;" \
			| grep -q '^1$'; then
			echo "ERROR: missing Postgres constraint: ${cname}" >&2
			missing=1
		fi
	done

	if [[ "$missing" -ne 0 ]]; then
		echo "HINT: run alembic upgrade head and verify migrations a1001902..a1001905 were applied." >&2
		exit 1
	fi
}

echo "[0/6] verify required Postgres uniqueness constraints exist"
require_pg_constraints

neo4j_count() {
	local cypher="$1"
	# Use container's configured auth (see NEO4J_AUTH).
	local neo4j_pass
	neo4j_pass="$(docker exec ai-write-neo4j-1 bash -lc 'echo "${NEO4J_AUTH:-neo4j/neo4j}" | cut -d/ -f2')"
	# Execute cypher as a positional arg to avoid here-string quoting issues.
	docker exec ai-write-neo4j-1 bash -lc \
		"/var/lib/neo4j/bin/cypher-shell --non-interactive --format plain -a bolt://127.0.0.1:7687 -u neo4j -p \"${neo4j_pass}\" \"${cypher}\"" \
		| tail -n 1 \
		| tr -d '\r' \
		| tr -d ' '
}

pg_count() {
	local sql="$1"
	docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -Atqc "$sql" \
		| head -n 1 \
		| tr -d '\r' \
		| tr -d ' '
}

reconcile_counts() {
	local pid="$1"

	echo "[0.5/6] reconcile Neo4j vs Postgres counts (project_id=$pid)"

	local n_chars n_rels n_rules n_locs n_atlocs n_cstates n_foreshadows
	local p_chars p_rels p_rules p_locs p_atlocs p_cstates p_foreshadows

	n_chars="$(neo4j_count "MATCH (c:Character {project_id: '$pid'}) RETURN count(DISTINCT c.name)")"
	n_rels="$(neo4j_count "MATCH (a:Character {project_id: '$pid'})-[r:RELATES_TO]->(b:Character {project_id: '$pid'}) RETURN count(DISTINCT a.name + '|' + b.name + '|' + r.type)")"
	# WorldRule node stores rule text under `text` (not `rule_text`).
	n_rules="$(neo4j_count "MATCH (w:WorldRule {project_id: '$pid'}) RETURN count(DISTINCT w.category + '|' + w.text)")"
	n_locs="$(neo4j_count "MATCH (l:Location {project_id: '$pid'}) RETURN count(l)")"
	n_foreshadows="$(neo4j_count "MATCH (f:Foreshadow {project_id: '$pid'}) RETURN count(f)")"
	n_atlocs="$(neo4j_count "MATCH (:Character {project_id: '$pid'})-[r:AT_LOCATION]->(:Location {project_id: '$pid'}) RETURN count(r)")"
	n_cstates="$(neo4j_count "MATCH (c:Character {project_id: '$pid'})-[:HAS_STATE]->(s:CharacterState) RETURN count(DISTINCT c.name + '|' + toString(s.chapter_start))")"

	p_chars="$(pg_count "SELECT count(DISTINCT name) FROM characters WHERE project_id='$pid';")"
	p_rels="$(pg_count "SELECT count(DISTINCT (source_id::text || '|' || target_id::text || '|' || rel_type)) FROM relationships WHERE project_id='$pid';")"
	p_rules="$(pg_count "SELECT count(DISTINCT (category || '|' || rule_text)) FROM world_rules WHERE project_id='$pid';")"
	p_locs="$(pg_count "SELECT count(*) FROM locations WHERE project_id='$pid';")"
	p_foreshadows="$(pg_count "SELECT count(*) FROM foreshadows WHERE project_id='$pid';")"
	p_atlocs="$(pg_count "SELECT count(*) FROM character_locations WHERE project_id='$pid';")"
	p_cstates="$(pg_count "SELECT count(DISTINCT (character_id::text || '|' || chapter_start::text)) FROM character_states WHERE project_id='$pid';")"

	echo "Neo4j counts: characters=$n_chars relationships=$n_rels world_rules=$n_rules locations=$n_locs foreshadows=$n_foreshadows at_locations=$n_atlocs character_states=$n_cstates"
	echo "Postgres counts: characters=$p_chars relationships=$p_rels world_rules=$p_rules locations=$p_locs foreshadows=$p_foreshadows character_locations=$p_atlocs character_states=$p_cstates"

	# This is a coarse reconciliation signal. We expect PG to eventually match Neo4j
	# after materialize. Any mismatch may indicate partial materialize or schema drift.
	local bad=0
	# NOTE: We only hard-reconcile projections that are written exclusively via
	# Neo4j materialize (locations / character_locations / character_states).
	# characters / relationships / world_rules can be edited/seeded via PG admin
	# settings UI and may legitimately diverge from Neo4j.
	for pair in \
		"locations:$n_locs:$p_locs" \
		"foreshadows:$n_foreshadows:$p_foreshadows" \
		"at_location_edges:$n_atlocs:$p_atlocs" \
		"character_states:$n_cstates:$p_cstates"
	do
		IFS=':' read -r label n p <<<"$pair"
		if [[ "$n" != "$p" ]]; then
			echo "WARN: count mismatch: $label neo4j=$n pg=$p" >&2
			bad=1
		fi
	done

	if [[ "$bad" -ne 0 ]]; then
		echo "HINT: rerun materialize; if still mismatched, check Neo4j queries vs PG filters and whether PG has soft-deletes or dedupe behavior." >&2
	fi
}

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

reconcile_counts "$PROJECT_ID"

echo "[2/3] materialize (2nd run; must be idempotent: created=0)"
OUT2="$(call_materialize)"
echo "$OUT2"

export OUT2_JSON="$OUT2"

python - <<'PY'
import json, os, sys

out2 = json.loads(os.environ['OUT2_JSON'])
res = out2.get('result') or {}
bad = []
for k in ['chars_created','rels_created','rules_created','locs_created','atlocs_created','cstates_created']:
    if k in res and res[k] not in (0, None):
        if res[k] != 0:
            bad.append((k, res[k]))
if bad:
    print('ERROR: materialize not idempotent:', bad)
    sys.exit(1)
print('OK: materialize idempotent')
PY

echo "[3/6] postgres duplicate check (relationships)"
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

echo "[4/6] postgres duplicate check (world_rules)"
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\
WITH dup AS (\
  SELECT project_id, category, rule_text, COUNT(*) AS n\
  FROM world_rules\
  WHERE project_id='${PROJECT_ID}'\
  GROUP BY project_id, category, rule_text\
  HAVING COUNT(*) > 1\
)\
SELECT COUNT(*) AS remaining_dup_groups, COALESCE(SUM(n),0) AS remaining_dup_rows FROM dup;\
"

echo "[5/6] postgres duplicate check (locations)"
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\
WITH dup AS (\
  SELECT project_id, name, COUNT(*) AS n\
  FROM locations\
  WHERE project_id='${PROJECT_ID}'\
  GROUP BY project_id, name\
  HAVING COUNT(*) > 1\
)\
SELECT COUNT(*) AS remaining_dup_groups, COALESCE(SUM(n),0) AS remaining_dup_rows FROM dup;\
"

echo "[6/6] postgres duplicate check (character_locations)"
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\
WITH dup AS (\
  SELECT project_id, character_id, location_id, chapter_start, COUNT(*) AS n\
  FROM character_locations\
  WHERE project_id='${PROJECT_ID}'\
  GROUP BY project_id, character_id, location_id, chapter_start\
  HAVING COUNT(*) > 1\
)\
SELECT COUNT(*) AS remaining_dup_groups, COALESCE(SUM(n),0) AS remaining_dup_rows FROM dup;\
"

echo "OK"

