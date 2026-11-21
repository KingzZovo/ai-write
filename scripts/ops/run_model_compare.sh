#!/usr/bin/env bash
# 跨模型对比(2026-11-18):gemini-3.1-pro vs deepseek-v4-flash,同一套默认配置、同一批章。
#
# 前置:gemini 的 100 章最终验证(longrun-100-gemini-2000-final)跑完。本脚本挂在那之后,
# 串行执行(任何时刻只有一个真实模型 harness —— 并发打崩过网关)。
#
#   1) 等 gemini final 跑满 100 章;
#   2) dsv4 跑同样的前 30 章(默认配置;关 thinking + 流式,否则首正文 26s 撞网关超时);
#   3) 双盲 A/B:gemini vs dsv4(30 对、两位评审);
#   4) 出 _public/model-compare.html。
#
# 规模为什么取 30 而不是 100:dsv4 关思考也要 2~3 分钟/章,100 章要 4~5 小时;
# 30 章已足够看出篇幅/连续性/评审偏好差异,并且能与 A/B 一起在 2 小时内出结果。
# 需要 100 章镜像时把 CH 改成 100 重跑即可。
#
# 启动:
#   setsid nohup bash scripts/ops/run_model_compare.sh > /tmp/modelcmp.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
EV="$R/docs/evaluation"
GEM="$EV/longrun-100-gemini-2000-final"
DSV="$EV/longrun-30-dsv4flash-2000-final"
AB="$EV/ab-model-20261118"
PUB="$EV/_public"
CH=30

log() { echo "$(date -Is) $*"; }
cd "$R/backend" || exit 1

# --- 1) 等 gemini final 跑满 ------------------------------------------------
log "waiting for gemini final run (100 chapters)"
for _ in $(seq 1 360); do
  n=$(ls "$GEM"/ch*.txt 2>/dev/null | wc -l)
  if [ "$n" -ge 100 ]; then log "gemini done ($n)"; break; fi
  sleep 120
done
n=$(ls "$GEM"/ch*.txt 2>/dev/null | wc -l)
[ "$n" -ge 100 ] || log "WARN gemini only $n chapters; continuing anyway"

# --- 2) dsv4 镜像臂 -------------------------------------------------------
log "starting dsv4 arm: $CH chapters (default config, thinking disabled, streaming)"
LLM_EXTRA_BODY_JSON='{"thinking":{"type":"disabled"}}' \
  "$PY" -m scripts.quality_eval.continuity_probe \
    --database-url "$DB" --out "$DSV" \
    --chapters "$CH" --words 2000 --model deepseek-v4-flash --max-tokens 16000 \
    --max-consecutive-errors 5 \
  > /tmp/arm_dsv4_final.log 2>&1
log "dsv4 exit=$? chapters=$(ls "$DSV"/ch*.txt 2>/dev/null | wc -l)"

"$PY" -m scripts.quality_eval.longrun --dir "$DSV" > /tmp/longrun_dsv4_final.log 2>&1
log "dsv4 longrun:"; sed -n '1,6p' /tmp/longrun_dsv4_final.log | while read -r l; do log "  $l"; done

# --- 3) 双盲 A/B(跨模型) -------------------------------------------------
log "arm_judge: gemini vs dsv4 ($CH pairs)"
"$PY" -m scripts.quality_eval.arm_judge \
  --a "$GEM" --b "$DSV" --out "$AB" \
  --sample "$CH" --judges deepseek-chat,claude-sonnet-4-6 --seed 31 \
  > /tmp/ab_model.log 2>&1
log "arm_judge exit=$? -> $(tail -3 /tmp/ab_model.log | tr '\n' ' ' | cut -c1-300)"

# --- 4) 对照页 ------------------------------------------------------------
"$PY" -m scripts.quality_eval.sbs_report \
  --old "$GEM" --new "$DSV" --out "$PUB/model-compare.html" --ab "$AB" \
  --old-label "gemini-3.1-pro · 目标 2000 字" \
  --new-label "deepseek-v4-flash · 关思考 · 目标 2000 字" \
  > /tmp/sbs_model.log 2>&1
log "sbs_report exit=$? -> $PUB/model-compare.html"

log "ALL DONE"