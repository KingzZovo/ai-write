#!/usr/bin/env bash
set -euo pipefail

# Deduplicate Postgres relationships rows.
#
# Dedup key: (project_id, source_id, target_id, rel_type)
# Keep the earliest created_at row; delete the rest.
#
# Usage:
#   PROJECT_ID=<project_id> bash scripts/dedupe_relationships_pg.sh

if [[ -z "${PROJECT_ID:-}" ]]; then
	echo "PROJECT_ID is required" >&2
	exit 2
fi

docker exec -i ai-write-postgres-1 psql -U postgres -d aiwrite <<SQL
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY project_id, source_id, target_id, rel_type
            ORDER BY created_at ASC NULLS FIRST, id ASC
        ) AS rn
    FROM relationships
    WHERE project_id = '${PROJECT_ID}'
), del AS (
    DELETE FROM relationships r
    USING ranked x
    WHERE r.id = x.id
      AND x.rn > 1
    RETURNING r.id
)
SELECT COUNT(*) AS deleted_rows FROM del;

-- sanity check
WITH dup AS (
    SELECT project_id, source_id, target_id, rel_type, COUNT(*) AS n
    FROM relationships
    WHERE project_id='${PROJECT_ID}'
    GROUP BY project_id, source_id, target_id, rel_type
    HAVING COUNT(*) > 1
)
SELECT COUNT(*) AS remaining_dup_groups, COALESCE(SUM(n), 0) AS remaining_dup_rows
FROM dup;
SQL

