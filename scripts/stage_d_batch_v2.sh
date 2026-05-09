#!/bin/bash
# Stage D batch driver v2 (PR-CHIXIN-REGEN-V2):
#   - --max-time 5400 (90min) covers initial + 2 revise rounds + 3 eval rounds
#   - 90s sleep between chapters lets backend revise loop drain (avoid LLM
#     resource contention which inflated v1 ch3 from ~18min to ~37min)
#   - DB truth (status='completed' AND word_count>=8000) is the success gate;
#     curl rc=28 (SSE timeout) is no longer treated as failure as long as
#     the backend committed a valid revised version
#
# Usage: nohup bash stage_d_batch_v2.sh 5 6 7 ... 20 >>/tmp/stage_d_run.log 2>&1 &
set -u
LOG=/tmp/stage_d_run.log
PROJECT=df6f523e-f903-4644-bcce-636f5ed89c68
VOLUME=ee36b649-ff4d-45ea-a045-f50f01589b5a
MIN_WORDS=8000
TOKEN=$(python3 -c 'import json; print(json.load(open("/tmp/login.json"))["token"])')
PW=$(grep -E '^POSTGRES_PASSWORD=' /root/ai-write/.env | cut -d= -f2-)

echo "=== Stage D v2 batch start at $(date -Iseconds) chapters=$* ===" >> "$LOG"
for IDX in "$@"; do
  CID=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -c \
    "SELECT id FROM chapters WHERE volume_id='$VOLUME' AND chapter_idx=$IDX;")
  if [ -z "$CID" ]; then
    echo "[ch$IDX] NO CHAPTER ROW, skip" >> "$LOG"
    continue
  fi
  echo "=== ch$IDX cid=$CID start=$(date -Iseconds) ===" >> "$LOG"

  T1=$(date +%s)
  EX_HTTP=$(curl -sS -X POST "http://127.0.0.1:8000/api/projects/$PROJECT/chapters/$CID/outline/expand" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -o "/tmp/sd_ch${IDX}_expand.json" -w '%{http_code}' --max-time 300)
  T2=$(date +%s)
  echo "[ch$IDX] expand HTTP=$EX_HTTP elapsed=$((T2-T1))s" >> "$LOG"
  if [ "$EX_HTTP" != "200" ]; then
    echo "[ch$IDX] EXPAND FAILED, skip content gen" >> "$LOG"
    head -c 400 "/tmp/sd_ch${IDX}_expand.json" >> "$LOG"; echo >> "$LOG"
    continue
  fi

  cat > "/tmp/sd_ch${IDX}_payload.json" <<JSON
{"project_id":"$PROJECT","chapter_id":"$CID","scene_mode":true,"auto_revise":true,"target_words":14000,"max_tokens":8000}
JSON
  T3=$(date +%s)
  curl -sS -N -X POST "http://127.0.0.1:8000/api/generate/chapter" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data-binary "@/tmp/sd_ch${IDX}_payload.json" \
    --max-time 5400 > "/tmp/sd_ch${IDX}.sse" 2>>"$LOG"
  GEN_RC=$?
  T4=$(date +%s)

  # DB truth, not SSE markers
  DB_ROW=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -F'|' -c \
    "SELECT status, word_count FROM chapters WHERE id='$CID';")
  DB_STATUS=$(echo "$DB_ROW" | cut -d'|' -f1)
  DB_WC=$(echo "$DB_ROW" | cut -d'|' -f2)
  EVAL_LATEST=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -c \
    "SELECT overall FROM chapter_evaluations WHERE chapter_id='$CID' ORDER BY created_at DESC LIMIT 1;")
  if [ "$DB_STATUS" = "completed" ] && [ "${DB_WC:-0}" -ge "$MIN_WORDS" ]; then
    OK=PASS
  else
    OK=FAIL
  fi
  echo "[ch$IDX] gen rc=$GEN_RC elapsed=$((T4-T3))s db_status=$DB_STATUS db_wc=${DB_WC:-0} latest_overall=${EVAL_LATEST:-NA} verdict=$OK" >> "$LOG"

  # let backend revise loop drain so the next chapter doesn't fight for LLM
  echo "[ch$IDX] cooling 90s" >> "$LOG"
  sleep 90
done
echo "=== Stage D v2 batch done at $(date -Iseconds) ===" >> "$LOG"
