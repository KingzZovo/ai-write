#!/bin/bash
# Stage D batch driver: for each chapter_idx in $@:
#   1. POST /api/projects/{pid}/chapters/{cid}/outline/expand   (~80s)
#   2. POST /api/generate/chapter with scene_mode+auto_revise   (~7-9min)
# Logs everything to /tmp/stage_d_run.log; one failed chapter does not stop the rest.
#
# Usage: nohup bash stage_d_batch.sh 12 13 14 15 16 17 18 19 20 >>/tmp/stage_d_run.log 2>&1 &

set -u

LOG=/tmp/stage_d_run.log
PROJECT=df6f523e-f903-4644-bcce-636f5ed89c68
VOLUME=ee36b649-ff4d-45ea-a045-f50f01589b5a
TOKEN=$(python3 -c 'import json; print(json.load(open("/tmp/login.json"))["token"])')
PW=$(grep -E '^POSTGRES_PASSWORD=' /root/ai-write/.env | cut -d= -f2-)

echo "=== Stage D batch start at $(date -Iseconds) chapters=$* ===" >> "$LOG"

for IDX in "$@"; do
  CID=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -c \
    "SELECT id FROM chapters WHERE volume_id='$VOLUME' AND chapter_idx=$IDX;")
  if [ -z "$CID" ]; then
    echo "[ch$IDX] NO CHAPTER ROW found, skip" >> "$LOG"
    continue
  fi
  echo "=== ch$IDX cid=$CID start=$(date -Iseconds) ===" >> "$LOG"

  # 1. expand outline
  T1=$(date +%s)
  EX_HTTP=$(curl -sS -X POST "http://127.0.0.1:8000/api/projects/$PROJECT/chapters/$CID/outline/expand" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -o "/tmp/sd_ch${IDX}_expand.json" -w '%{http_code}' --max-time 240)
  T2=$(date +%s)
  echo "[ch$IDX] expand HTTP=$EX_HTTP elapsed=$((T2-T1))s" >> "$LOG"
  if [ "$EX_HTTP" != "200" ]; then
    echo "[ch$IDX] EXPAND FAILED, skip content gen" >> "$LOG"
    head -c 400 "/tmp/sd_ch${IDX}_expand.json" >> "$LOG"; echo >> "$LOG"
    continue
  fi

  # 2. generate content (SSE)
  cat > "/tmp/sd_ch${IDX}_payload.json" <<JSON
{"project_id":"$PROJECT","chapter_id":"$CID","scene_mode":true,"auto_revise":true,"target_words":14000,"max_tokens":8000}
JSON
  T3=$(date +%s)
  curl -sS -N -X POST "http://127.0.0.1:8000/api/generate/chapter" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data-binary "@/tmp/sd_ch${IDX}_payload.json" \
    --max-time 2400 > "/tmp/sd_ch${IDX}.sse" 2>>"$LOG"
  GEN_RC=$?
  T4=$(date +%s)

  # parse SSE markers (status saved + event scored + completed)
  WC=$(grep -oE '"word_count": [0-9]+' "/tmp/sd_ch${IDX}.sse" | head -1 | grep -oE '[0-9]+')
  SCORE=$(grep -oE '"overall": [0-9.]+' "/tmp/sd_ch${IDX}.sse" | head -1 | grep -oE '[0-9.]+')
  ISSUES=$(grep -oE '"issues": [0-9]+' "/tmp/sd_ch${IDX}.sse" | head -1 | grep -oE '[0-9]+')
  COMPLETED=$(grep -c '"status": "completed"' "/tmp/sd_ch${IDX}.sse")
  REVISE=$(grep -oE '"event": "revise_skipped"|"event": "revising"|"event": "revise_error"' "/tmp/sd_ch${IDX}.sse" | head -1)
  echo "[ch$IDX] gen rc=$GEN_RC elapsed=$((T4-T3))s word_count=${WC:-0} score=${SCORE:-NA} issues=${ISSUES:-NA} revise=${REVISE:-none} completed=$COMPLETED" >> "$LOG"
done

echo "=== Stage D batch done at $(date -Iseconds) ===" >> "$LOG"
