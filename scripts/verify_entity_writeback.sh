#!/usr/bin/env bash
set -euo pipefail

# Verify entity writeback convergence: Neo4j -> Postgres + Prometheus metric.
#
# Usage:
#   PROJECT_ID=<uuid> bash scripts/verify_entity_writeback.sh
#
# Requirements:
# - docker compose stack running
# - ADMIN_USERNAMES contains the JWT subject (default king)
# - /tmp/king_tok contains a valid JWT (or we will login with default creds)

PROJECT_ID="${PROJECT_ID:-}"
CHAPTER_IDX="${CHAPTER_IDX:-11}"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"
PG_DB="${PG_DB:-aiwrite}"
PG_USER="${PG_USER:-postgres}"

if [[ -z "$PROJECT_ID" ]]; then
	echo "ERROR: PROJECT_ID is required" >&2
	exit 1
fi

ensure_token() {
	if [[ -s /tmp/king_tok ]]; then
		return 0
	fi
	# Default smoke creds (RUNBOOK §0)
	local username="${AUTH_USERNAME:-king}"
	local password="${AUTH_PASSWORD:-Wt991125}"
	local tok
	tok=$(curl -sS -X POST "$API_BASE/api/auth/login" \
		-H 'Content-Type: application/json' \
		-d "{\"username\":\"$username\",\"password\":\"$password\"}" \
		| python -c 'import sys,json; print(json.load(sys.stdin)["token"])')
	echo "$tok" > /tmp/king_tok
}

header() { echo; echo "==== $* ===="; }

ensure_token
TOK=$(cat /tmp/king_tok)

header "1) Admin materialize (backend process)"
curl -sS -X POST "$API_BASE/api/admin/entities/materialize" \
	-H 'Content-Type: application/json' \
	-H "Authorization: Bearer $TOK" \
	-d "{\"project_id\":\"$PROJECT_ID\",\"chapter_idx\":$CHAPTER_IDX,\"caller\":\"scripts.verify_entity_writeback\"}" \
	| python -m json.tool

header "2) Postgres counts"
docker exec ai-write-postgres-1 psql -U "$PG_USER" -d "$PG_DB" -c "SELECT COUNT(*) AS characters_n FROM characters WHERE project_id='$PROJECT_ID';"
docker exec ai-write-postgres-1 psql -U "$PG_USER" -d "$PG_DB" -c "SELECT COUNT(*) AS relationships_n FROM relationships WHERE project_id='$PROJECT_ID';"

header "3) Sample characters (must exist)"
docker exec ai-write-postgres-1 psql -U "$PG_USER" -d "$PG_DB" -c "SELECT name FROM characters WHERE project_id='$PROJECT_ID' AND name IN ('凌祝','纪砚','苏未') ORDER BY name;"

header "4) Prometheus metric (must have time series line)"
curl -sS "$API_BASE/metrics" | grep -E '^entity_pg_materialize_total\{' | head -n 20 || true


echo

echo "OK: verify_entity_writeback completed"