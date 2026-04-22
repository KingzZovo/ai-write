#!/usr/bin/env bash
# v1.0.0 backup script — snapshots postgres, qdrant, redis, neo4j into a tarball.
#
# Usage:
#   scripts/backup.sh                # local only, writes to ./backups/
#   BACKUP_S3_URI=s3://bucket/path ./scripts/backup.sh   # also upload to S3
#
# Env vars (all optional):
#   BACKUP_DIR        default ./backups
#   BACKUP_RETAIN     default 14 (keep last N local tarballs)
#   BACKUP_S3_URI     if set, aws-s3 upload (requires awscli)
#   BACKUP_S3_ENDPOINT_URL   for non-AWS S3 (e.g. MinIO, Cloudflare R2)
#
# Assumes running from repo root with `docker compose` services up.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
BACKUP_RETAIN="${BACKUP_RETAIN:-14}"
STAGE="$(mktemp -d -t ai-write-backup-XXXX)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$BACKUP_DIR"

log() { echo "[backup $(date -u +%H:%M:%SZ)] $*"; }

# ---------- Postgres ----------
log "pg_dump ai_write"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-aiwrite}"
docker compose exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges \
  | gzip -9 > "$STAGE/postgres-$PG_DB.sql.gz"

# ---------- Qdrant ----------
log "qdrant snapshots (per collection)"
mkdir -p "$STAGE/qdrant"
QDRANT_COLLECTIONS=$(curl -sf http://127.0.0.1:6333/collections \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); [print(c["name"]) for c in d["result"]["collections"]]' 2>/dev/null || true)
if [[ -n "$QDRANT_COLLECTIONS" ]]; then
  while IFS= read -r COLL; do
    [[ -z "$COLL" ]] && continue
    log "  snapshot collection=$COLL"
    SNAP_RESP=$(curl -sf -X POST "http://127.0.0.1:6333/collections/$COLL/snapshots" || true)
    SNAP_NAME=$(echo "$SNAP_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["name"])' 2>/dev/null || true)
    if [[ -n "$SNAP_NAME" ]]; then
      curl -sf -o "$STAGE/qdrant/${COLL}_${SNAP_NAME}" \
        "http://127.0.0.1:6333/collections/$COLL/snapshots/$SNAP_NAME" || true
    fi
  done <<< "$QDRANT_COLLECTIONS"
else
  log "  (qdrant not reachable; skipping)"
fi

# ---------- Redis ----------
log "redis bgsave"
docker compose exec -T redis redis-cli BGSAVE > /dev/null 2>&1 || true
# Give redis a moment to flush then copy dump.rdb
sleep 3
docker compose cp redis:/data/dump.rdb "$STAGE/redis-dump.rdb" 2>/dev/null \
  || log "  (redis dump.rdb not found; skipping)"

# ---------- Neo4j ----------
log "neo4j cypher-shell dump (apoc.export)"
if docker compose ps --status running 2>/dev/null | grep -q neo4j; then
  # Try apoc.export.cypher.all first; fall back to noop if APOC not installed.
  docker compose exec -T neo4j bash -lc \
    'cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-neo4jpass}" "CALL apoc.export.cypher.all(null, {stream:true, format:\"cypher-shell\"}) YIELD cypherStatements RETURN cypherStatements" 2>/dev/null' \
    > "$STAGE/neo4j-export.cypher" 2>/dev/null || log "  (neo4j export unavailable; skipping)"
fi

# ---------- Metadata ----------
cat > "$STAGE/MANIFEST.txt" <<MANIFEST
ai-write backup
timestamp: $TS
git_sha: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)
git_tag: $(git describe --tags --abbrev=0 2>/dev/null || echo unknown)
host: $(hostname)
MANIFEST

# ---------- Package ----------
OUT="$BACKUP_DIR/ai-write-backup-$TS.tar.gz"
log "packing -> $OUT"
tar -czf "$OUT" -C "$STAGE" .
SIZE=$(du -h "$OUT" | cut -f1)
log "done: $OUT ($SIZE)"

# ---------- Retention ----------
if [[ "$BACKUP_RETAIN" =~ ^[0-9]+$ ]]; then
  ls -1t "$BACKUP_DIR"/ai-write-backup-*.tar.gz 2>/dev/null \
    | tail -n +$((BACKUP_RETAIN + 1)) \
    | xargs -r rm -f
  log "retention: keeping last $BACKUP_RETAIN"
fi

# ---------- Optional S3 upload ----------
if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  ENDPOINT_ARG=""
  if [[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]]; then
    ENDPOINT_ARG="--endpoint-url $BACKUP_S3_ENDPOINT_URL"
  fi
  log "uploading to $BACKUP_S3_URI"
  aws s3 cp $ENDPOINT_ARG "$OUT" "$BACKUP_S3_URI/" \
    || log "  (S3 upload failed)"
fi

log "backup complete"
