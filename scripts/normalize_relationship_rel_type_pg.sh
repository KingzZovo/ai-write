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

-- 3.5) Canonicalize by keywords (keep rel_type short/stable)
-- NOTE: if label is empty, preserve the original rel_type into label.
UPDATE relationships
SET label = CASE
    WHEN coalesce(label, '') = '' THEN rel_type
    ELSE label
END,
    rel_type = CASE
    WHEN rel_type ~ '(敌对|仇敌|死敌)' THEN '敌对'
    WHEN rel_type ~ '(对立|不信任|对手)' THEN '对立'
    WHEN rel_type ~ '(监管|押解|押送|看押|管辖|盘查|监控|审查|取证|查档|查档对照)' THEN '监管'
    WHEN rel_type ~ '(审讯|逼问)' THEN '审讯'
    WHEN rel_type ~ '(师生|师徒)' THEN '师生'
    WHEN rel_type ~ '(上下级|上位|下属)' THEN '上下级'
    WHEN rel_type ~ '(同舍|同寝)' THEN '同舍'
    WHEN rel_type ~ '(同伴|同学|同行|协作)' THEN '同伴'
    WHEN rel_type ~ '(失联|寻找)' THEN '失联'
    ELSE rel_type
END
WHERE project_id = '${PROJECT_ID}'
  AND (length(rel_type) > 6 OR rel_type IN ('查档对照'));

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

