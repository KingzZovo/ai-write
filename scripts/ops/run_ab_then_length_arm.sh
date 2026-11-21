#!/usr/bin/env bash
# 串行实验链(2026-11-18):思考臂已完成 20/20,现在按"先短后长"跑完两件待办。
#
#   1) 双盲 A/B:dsv4-flash 关思考(OFF 16 章)vs 开思考(ON 20 章)→ _public/thinking.html
#   2) 长度臂:gemini + SCENE_TARGET_OVERALLOC=1.08,10 章 → 验证"单章 2000 字"是否达成
#   3) 与 gemini@2000(超分配关闭)前 10 章并排 → _public/length.html
#
# 全程**串行**,任何时刻只有一个真实模型 harness 在跑(之前并发打崩过一次网关)。
# 启动:
#   setsid nohup bash scripts/ops/run_ab_then_length_arm.sh > /tmp/chain2.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
EV="$R/docs/evaluation"
OFF="$EV/longrun-100-dsv4flash-2000-20261117"
ON="$EV/arm-dsv4-thinking-20"
AB="$EV/ab-thinking-20261117"
BASE="$EV/longrun-100-gemini-2000-20261117"
LEN="$EV/longrun-10-gemini-2000-overalloc"
PUB="$EV/_public"

log() { echo "$(date -Is) $*"; }
mkdir -p "$PUB"

cd "$R/backend" || exit 1

# --- 1) 双盲 A/B(关思考 vs 开思考) ----------------------------------------
log "arm_judge: OFF=$OFF ON=$ON"
"$PY" -m scripts.quality_eval.arm_judge \
  --a "$OFF" --b "$ON" --out "$AB" \
  --sample 16 --judges deepseek-chat,claude-sonnet-4-6 --seed 23 \
  > /tmp/ab_thinking.log 2>&1
log "arm_judge exit=$? -> $(tail -3 /tmp/ab_thinking.log | tr '\n' ' ' | cut -c1-300)"

log "sbs_report -> thinking.html"
"$PY" -m scripts.quality_eval.sbs_report \
  --old "$OFF" --new "$ON" --out "$PUB/thinking.html" --ab "$AB" \
  --old-label "deepseek-v4-flash · 关思考 · 流式 · 目标 2000 字" \
  --new-label "deepseek-v4-flash · 开思考 · 非流式 · 目标 2000 字" \
  > /tmp/sbs_thinking.log 2>&1
log "sbs_report(thinking) exit=$?"

# --- 2) 长度臂:超分配 1.08 + gemini,10 章 ---------------------------------
log "length arm: gemini @2000 with SCENE_TARGET_OVERALLOC=1.08 (10 chapters)"
SCENE_TARGET_OVERALLOC=1.08 \
  "$PY" -m scripts.quality_eval.continuity_probe \
    --database-url "$DB" --out "$LEN" \
    --chapters 10 --words 2000 --model gemini-3.1-pro --max-tokens 8000 \
    --max-consecutive-errors 3 \
  > /tmp/arm_length.log 2>&1
log "length arm exit=$? chapters=$(ls "$LEN"/ch*.txt 2>/dev/null | wc -l)"

# --- 3) 曲线 + 并排对照 ----------------------------------------------------
log "longrun(new arm)"
"$PY" -m scripts.quality_eval.longrun --dir "$LEN" > /tmp/longrun_len.log 2>&1
tail -6 /tmp/longrun_len.log | while read -r line; do log "  $line"; done

log "sbs_report -> length.html"
"$PY" -m scripts.quality_eval.sbs_report \
  --old "$BASE" --new "$LEN" --out "$PUB/length.html" \
  --old-label "gemini · 超分配关闭(1.0)· 目标 2000 字" \
  --new-label "gemini · 超分配 1.08 · 目标 2000 字" \
  > /tmp/sbs_length.log 2>&1
log "sbs_report(length) exit=$?"

log "ALL DONE"