#!/bin/bash
# Stage D repair: scan all chapters in $RANGE (default 1-20) and re-run any
# whose DB row is below the success bar:
#   status != 'completed', OR word_count < $MIN_WORDS, OR latest evaluation
#   overall_score < $MIN_SCORE.
# Re-uses stage_d_batch_v2.sh so the same max-time/cooldown rules apply.
#
# Usage: nohup bash stage_d_repair.sh >>/tmp/stage_d_repair.log 2>&1 &
set -u
LOG=/tmp/stage_d_repair.log
VOLUME=ee36b649-ff4d-45ea-a045-f50f01589b5a
RANGE_LO=${RANGE_LO:-1}
RANGE_HI=${RANGE_HI:-20}
MIN_WORDS=${MIN_WORDS:-8000}
MIN_SCORE=${MIN_SCORE:-7.0}
PW=$(grep -E '^POSTGRES_PASSWORD=' /root/ai-write/.env | cut -d= -f2-)

echo "=== repair scan at $(date -Iseconds) range=$RANGE_LO-$RANGE_HI min_words=$MIN_WORDS min_score=$MIN_SCORE ===" >> "$LOG"
NEEDS=()
for IDX in $(seq $RANGE_LO $RANGE_HI); do
  ROW=$(docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -t -A -F'|' -c \
    "SELECT c.id, c.status, c.word_count, COALESCE((SELECT overall FROM chapter_evaluations WHERE chapter_id=c.id ORDER BY created_at DESC LIMIT 1)::text, 'NA') FROM chapters c WHERE c.volume_id='$VOLUME' AND c.chapter_idx=$IDX;")
  if [ -z "$ROW" ]; then continue; fi
  CID=$(echo "$ROW" | cut -d'|' -f1)
  ST=$(echo "$ROW" | cut -d'|' -f2)
  WC=$(echo "$ROW" | cut -d'|' -f3)
  SC=$(echo "$ROW" | cut -d'|' -f4)
  REASON=""
  [ "$ST" != "completed" ] && REASON="$REASON status=$ST"
  [ "${WC:-0}" -lt "$MIN_WORDS" ] && REASON="$REASON wc=$WC<$MIN_WORDS"
  if [ "$SC" != "NA" ] && python3 -c "import sys; sys.exit(0 if float('$SC')<$MIN_SCORE else 1)" 2>/dev/null; then
    REASON="$REASON score=$SC<$MIN_SCORE"
  fi
  if [ -n "$REASON" ]; then
    echo "[ch$IDX] NEEDS_REPAIR reason=$REASON st=$ST wc=$WC sc=$SC" >> "$LOG"
    NEEDS+=($IDX)
    docker exec -e PGPASSWORD="$PW" -i ai-write-postgres-1 psql -U postgres -d aiwrite -c \
      "UPDATE chapters SET status='draft', word_count=0, content_text='', outline_json=NULL, summary=NULL WHERE id='$CID';" >> "$LOG" 2>&1
  else
    echo "[ch$IDX] OK st=$ST wc=$WC sc=$SC" >> "$LOG"
  fi
done
if [ ${#NEEDS[@]} -eq 0 ]; then
  echo "=== repair: nothing to do at $(date -Iseconds) ===" >> "$LOG"
  exit 0
fi
echo "=== repair: re-running ${NEEDS[*]} ===" >> "$LOG"
bash /root/ai-write/scripts/stage_d_batch_v2.sh "${NEEDS[@]}" >>$LOG 2>&1
echo "=== repair done at $(date -Iseconds) ===" >> "$LOG"
