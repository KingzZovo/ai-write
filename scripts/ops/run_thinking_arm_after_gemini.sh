#!/usr/bin/env bash
# 串行实验链(2026-11-17):
#   1) 等 gemini@2000 跑满 100 章;
#   2) 起 20 章"思考开 + 非流式"臂(deepseek-v4-flash);
#   3) 双盲 A/B:dsv4 关思考 vs 开思考(同一批章);
#   4) 出第三张对照页 _public/thinking.html;
#   5) 写 DONE 标记。
#
# 一条链跑 5 小时左右,用 setsid 起,别让会话收尾把它带走:
#   setsid nohup bash scripts/ops/run_thinking_arm_after_gemini.sh > /tmp/chain.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
GEM="$R/docs/evaluation/longrun-100-gemini-2000-20261117"
OFF="$R/docs/evaluation/longrun-100-dsv4flash-2000-20261117"
ON="$R/docs/evaluation/arm-dsv4-thinking-20"
AB="$R/docs/evaluation/ab-thinking-20261117"
OUT="$R/docs/evaluation/_public/thinking.html"

log() { echo "$(date -Is) $*"; }

# --- 1) 等 gemini@2000 跑满 -------------------------------------------------
log "waiting gemini@2000 to finish (100 chapters + continuity.json)"
for _ in $(seq 1 240); do
  n=$(ls "$GEM"/ch*.txt 2>/dev/null | wc -l)
  if [ "$n" -ge 100 ] && [ -f "$GEM/continuity.json" ]; then
    log "gemini@2000 done ($n chapters)"
    break
  fi
  sleep 120
done
n=$(ls "$GEM"/ch*.txt 2>/dev/null | wc -l)
[ "$n" -ge 100 ] || log "WARN gemini@2000 only $n chapters; continuing anyway"

# --- 2) 思考开 + 非流式 臂 -------------------------------------------------
log "starting thinking arm (20 chapters, non-stream, thinking ON)"
cd "$R/backend"
LLM_NONSTREAM_TASKS='*' LLM_NONSTREAM_TIMEOUT_SECONDS=240 \
  "$PY" -m scripts.quality_eval.continuity_probe \
    --database-url "$DB" --out "$ON" \
    --chapters 20 --words 2000 \
    --model deepseek-v4-flash --max-tokens 16000 --max-consecutive-errors 5 \
  > /tmp/arm_thinking.log 2>&1
log "thinking arm exit=$? chapters=$(ls "$ON"/ch*.txt 2>/dev/null | wc -l)"

# --- 3) 双盲 A/B:关思考 vs 开思考 ------------------------------------------
log "running arm_judge (off vs on)"
"$PY" -m scripts.quality_eval.arm_judge \
  --a "$OFF" --b "$ON" --out "$AB" \
  --sample 20 --judges deepseek-chat,claude-sonnet-4-6 --seed 23 \
  > /tmp/ab_thinking.log 2>&1
log "arm_judge exit=$? $(tail -3 /tmp/ab_thinking.log | tr '\n' ' ')"

# --- 4) 第三张对照页 -------------------------------------------------------
(cd "$R/backend" && "$PY" -m scripts.quality_eval.sbs_report \
  --old "$OFF" --new "$ON" --out "$OUT" --ab "$AB" \
  --old-label "deepseek-v4-flash · 关思考 · 流式 · 目标 2000 字" \
  --new-label "deepseek-v4-flash · 开思考 · 非流式 · 目标 2000 字")
log "page written: $OUT"

# --- 5) 终版 2000 对照页(两边都 2000 字) ---------------------------------
(cd "$R/backend" && "$PY" -m scripts.quality_eval.sbs_report \
  --old "$GEM" --new "$OFF" --out "$R/docs/evaluation/_public/2000.html" \
  --old-label "gemini-3.1-pro · 0.95 · 时间钉子 · 目标 2000 字" \
  --new-label "deepseek-v4-flash · 0.95 · 时间钉子 · 关思考 · 目标 2000 字")
log DONE