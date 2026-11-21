#!/bin/bash
# PR-GEN-REVISE-DEDUP: regen low-score chapters after scene_orchestrator dedup fix.
# Skips outline/expand (outlines already exist) -- only calls /api/generate/chapter.
# Usage: nohup bash stage_d_dedup_rerun.sh 2 8 10 15 >>/tmp/dedup_rerun.log 2>&1 &
set -u
LOG=/tmp/dedup_rerun.log
PROJECT=df6f523e-f903-4644-bcce-636f5ed89c68
VOLUME=ee36b649-ff4d-45ea-a045-f50f01589b5a
MIN_WORDS=8000
TOKEN=$(python3 -c 'import json; print(json.load(open("/tmp/login.json"))["token"])')
PW=$(grep -E '^POSTGRES_PASSWORD=' /root/ai-write/.env | cut -d= -f2-)
echo "=== dedup rerun start at $(date -Iseconds) chapters=$* ===" >> "$LOG"
for IDX in "$@"; do
  CID=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -c "SELECT id FROM chapters WHERE volume_id='$VOLUME' AND chapter_idx=$IDX;")
  echo "=== ch$IDX cid=$CID start=$(date -Iseconds) ===" >> "$LOG"
  cat > "/tmp/dedup_ch${IDX}_payload.json" <<JSON
{"project_id":"$PROJECT","chapter_id":"$CID","scene_mode":true,"auto_revise":true,"target_words":14000,"max_tokens":8000}
JSON
  T1=$(date +%s)
  curl -sS -N -X POST "http://127.0.0.1:8000/api/generate/chapter" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data-binary "@/tmp/dedup_ch${IDX}_payload.json" \
    --max-time 5400 > "/tmp/dedup_ch${IDX}.sse" 2>>"$LOG"
  RC=$?
  T2=$(date +%s)
  DB_ROW=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -F'|' -c "SELECT status, word_count FROM chapters WHERE id='$CID';")
  EVAL_LATEST=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -c "SELECT overall FROM chapter_evaluations WHERE chapter_id='$CID' ORDER BY created_at DESC LIMIT 1;")
  echo "[ch$IDX] rc=$RC elapsed=$((T2-T1))s db=$DB_ROW overall=${EVAL_LATEST:-NA}" >> "$LOG"
  echo "[ch$IDX] cooling 90s" >> "$LOG"
  sleep 90
done
echo "=== dedup rerun done at $(date -Iseconds) ===" >> "$LOG"
