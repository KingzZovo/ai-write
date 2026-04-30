#!/usr/bin/env bash
set -euo pipefail

# Normalize existing Postgres relationships.rel_type values to keep them short/stable.
# This is a one-off maintenance script (safe to re-run).
#
# Usage:
#   PROJECT_ID=<project_id> bash scripts/normalize_relationship_rel_type_pg.sh

if [[ -z "${PROJECT_ID:-}" ]]; then
	echo "PROJECT_ID is required" >&2
	exit 2
fi

docker exec -i ai-write-postgres-1 psql -U postgres -d aiwrite <<SQL
-- 1) Remove fullwidth parentheses explanation: "xxx（..." -> "xxx"
UPDATE relationships
SET rel_type = btrim(split_part(rel_type, '（', 1))
WHERE project_id = '${PROJECT_ID}'
  AND rel_type LIKE '%（%';

-- 2) Remove ASCII parentheses explanation: "xxx(..." -> "xxx"
UPDATE relationships
SET rel_type = btrim(split_part(rel_type, '(', 1))
WHERE project_id = '${PROJECT_ID}'
  AND rel_type LIKE '%(%';

-- 3) Slash combos: "A/B" -> "A"
UPDATE relationships
SET rel_type = btrim(split_part(rel_type, '/', 1))
WHERE project_id = '${PROJECT_ID}'
  AND rel_type LIKE '%/%';

-- 4) Enforce varchar(50) bound (defensive)
UPDATE relationships
SET rel_type = left(rel_type, 50)
WHERE project_id = '${PROJECT_ID}'
  AND length(rel_type) > 50;

-- Report remaining non-canonical patterns
SELECT rel_type, COUNT(*) AS n
FROM relationships
WHERE project_id='${PROJECT_ID}'
  AND (rel_type LIKE '%（%' OR rel_type LIKE '%(%' OR rel_type LIKE '%/%')
GROUP BY rel_type
ORDER BY n DESC, rel_type;
SQL

