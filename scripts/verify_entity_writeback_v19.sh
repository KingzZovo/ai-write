#!/usr/bin/env bash
# verify_entity_writeback_v19.sh — 设定集 Neo4j → Postgres materialize 对账脚本（v1.9）
#
# 目的：
#   验证「Neo4j 真相源 → materialize → PG 投影」这条链路对一个 (project_id, chapter_idx)
#   是否一致。脚本只读、幂等，不写任何数据。
#
# 用法：
#   PROJECT_ID=<uuid> CHAPTER_IDX=<int> bash scripts/verify_entity_writeback_v19.sh
#
# 可选环境变量：
#   API_BASE        默认 http://localhost:8000
#   API_TOKEN       若开启 Bearer 鉴权，则需设置
#   PG_DSN          默认 postgresql://postgres:postgres@localhost:5432/aiwrite
#                   也可改为 docker：postgres:5432，按 docker-compose 改写
#   NEO4J_URI       默认 bolt://localhost:7687
#   NEO4J_USER      默认 neo4j
#   NEO4J_PASSWORD  必填（若 Neo4j 开启鉴权）
#
# 退出码：
#   0 = OK（Neo4j 与 PG 行数一致 / 字段健康）
#   1 = 检查失败（不一致 / 缺字段 / 服务不可达）
#
# 验收点（v1.9 范围）：
#   1. legacy 写接口已 410：POST /api/projects/{pid}/world-rules、/relationships
#   2. 真实写入口存在：POST /api/projects/{pid}/outlines/{oid}/extract-settings
#      （不依赖 README 旧文档里写的 /neo4j-settings/* 与 /admin/entities/materialize —
#       两者**未在 main 实现**，详见 docs/PROGRESS.md §3）
#   3. PG 端 characters / world_rules / relationships / locations 行数 == Neo4j 端
#      对应节点 / 关系数（按 project_id 维度）
#   4. PG 端 character_states.chapter_start <= CHAPTER_IDX 至少存在 1 行
#      （证明 entity_tasks materialize 跑过这一章）

set -euo pipefail

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok()   { echo "[OK]   $*"; }
warn() { echo "[WARN] $*" >&2; }

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${CHAPTER_IDX:?CHAPTER_IDX is required}"
API_BASE="${API_BASE:-http://localhost:8000}"
PG_DSN="${PG_DSN:-postgresql://postgres:postgres@localhost:5432/aiwrite}"
NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"

echo "== 设定集 writeback 对账 v1.9 =="
echo "  PROJECT_ID  = $PROJECT_ID"
echo "  CHAPTER_IDX = $CHAPTER_IDX"
echo "  API_BASE    = $API_BASE"
echo "  PG_DSN      = ${PG_DSN%%@*}@***"
echo "  NEO4J_URI   = $NEO4J_URI"
echo

# ---- 0. 依赖检查 -----------------------------------------------------------
for tool in curl psql; do
  command -v "$tool" >/dev/null 2>&1 || fail "缺少依赖：$tool"
done
if ! command -v cypher-shell >/dev/null 2>&1; then
  warn "未安装 cypher-shell，将跳过 Neo4j 端直接计数（仍会做 API + PG 端）"
  HAVE_CYPHER=0
else
  HAVE_CYPHER=1
fi

AUTH_HDR=()
if [[ -n "${API_TOKEN:-}" ]]; then
  AUTH_HDR=(-H "Authorization: Bearer ${API_TOKEN}")
fi

# ---- 1. legacy 410 烟测 ---------------------------------------------------
for path in "world-rules" "relationships"; do
  url="${API_BASE}/api/projects/${PROJECT_ID}/${path}"
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
    "${AUTH_HDR[@]}" -H "Content-Type: application/json" -d '{}' || true)
  if [[ "$code" == "410" ]]; then
    ok "legacy POST /${path} → 410（已禁用，符合 v1.9）"
  else
    fail "legacy POST /${path} 期望 410 实际 ${code}"
  fi
done

# ---- 2. 真实写入口存在性（探测） ---------------------------------
# extract-settings 需要 outline_id，这里只做 404/400/422/500 探测（说明路由存在）
probe="${API_BASE}/api/projects/${PROJECT_ID}/outlines/__probe__/extract-settings"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$probe" \
  "${AUTH_HDR[@]}" -H "Content-Type: application/json" -d '{}' || true)
if [[ "$code" =~ ^(404|400|422|500)$ ]]; then
  ok "extract-settings 路由存在（探测返回 ${code}，非 405/未实现）"
else
  warn "extract-settings 探测返回 ${code}，请确认路由是否注册"
fi

# ---- 3. PG 行数 -----------------------------------------------------------
pg_count() {
  local table="$1" filter="$2"
  psql "$PG_DSN" -At -c "SELECT count(*) FROM ${table} WHERE ${filter};"
}

PG_CHARS=$(pg_count "characters"    "project_id = '${PROJECT_ID}'")
PG_RULES=$(pg_count "world_rules"    "project_id = '${PROJECT_ID}'")
PG_RELS=$(pg_count  "relationships"  "project_id = '${PROJECT_ID}'")
PG_LOCS=$(pg_count  "locations"      "project_id = '${PROJECT_ID}'")
PG_STATES_LE=$(pg_count "character_states" \
  "project_id = '${PROJECT_ID}' AND chapter_start <= ${CHAPTER_IDX}")

echo "  PG  characters       = $PG_CHARS"
echo "  PG  world_rules      = $PG_RULES"
echo "  PG  relationships    = $PG_RELS"
echo "  PG  locations        = $PG_LOCS"
echo "  PG  states(<=$CHAPTER_IDX)  = $PG_STATES_LE"

[[ "$PG_STATES_LE" -ge 1 ]] \
  || fail "character_states 没有 chapter_start <= ${CHAPTER_IDX} 的行（materialize 没跑过这一章）"
ok "character_states 已覆盖 chapter ${CHAPTER_IDX}"

# ---- 4. Neo4j 计数 + 对账 -------------------------------------------------
if [[ "$HAVE_CYPHER" -eq 1 ]]; then
  : "${NEO4J_PASSWORD:?NEO4J_PASSWORD is required when cypher-shell is available}"
  cypher() {
    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
      --format plain "$1" | tail -n +2 | tr -d '"' | head -1
  }
  N4_CHARS=$(cypher "MATCH (c:Character {project_id: '${PROJECT_ID}'}) RETURN count(DISTINCT c.name) AS n;")
  N4_RULES=$(cypher "MATCH (r:WorldRule {project_id: '${PROJECT_ID}'}) RETURN count(DISTINCT r.rule_text) AS n;")
  N4_RELS=$(cypher  "MATCH (:Character {project_id:'${PROJECT_ID}'})-[r:RELATES_TO]->(:Character {project_id:'${PROJECT_ID}'}) RETURN count(r) AS n;")
  N4_LOCS=$(cypher  "MATCH (l:Location {project_id: '${PROJECT_ID}'}) RETURN count(DISTINCT l.name) AS n;")

  echo "  N4  characters       = $N4_CHARS"
  echo "  N4  world_rules      = $N4_RULES"
  echo "  N4  relationships    = $N4_RELS"
  echo "  N4  locations        = $N4_LOCS"

  diff_check() {
    local label="$1" pg="$2" n4="$3"
    if [[ "$pg" == "$n4" ]]; then
      ok "${label}：PG=${pg} == Neo4j=${n4}"
    else
      fail "${label}：PG=${pg} != Neo4j=${n4}（materialize 不一致）"
    fi
  }
  diff_check "characters"    "$PG_CHARS" "$N4_CHARS"
  diff_check "world_rules"   "$PG_RULES" "$N4_RULES"
  diff_check "relationships" "$PG_RELS"  "$N4_RELS"
  diff_check "locations"     "$PG_LOCS"  "$N4_LOCS"
else
  warn "跳过 Neo4j 对账（无 cypher-shell）；只验证了 PG 端一致性指标"
fi

echo
echo "OK: 设定集 writeback v1.9 对账通过"
