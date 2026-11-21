#!/usr/bin/env bash
# 换题材验证(2026-11-19):在**第二个题材**上重跑跨模型对比。
#
# 目的:此前的结论(gemini-3.1-pro vs deepseek-v4-flash,dsv4 被压倒性偏好)
# 建立在单一题材《渡口旧账》(现实生活)上。换到都市悬疑《回声档案》再跑一遍,
# 看结论是否仍然成立 —— 只有跨题材成立,才谈得上"默认换模型"。
#
# 串行执行(任何时刻只有一个真实模型 harness):
#   1) dsv4 29 章(新题材,关 thinking + 流式)
#   2) gemini 29 章(新题材)
#   3) 双盲 A/B:两位中立评审(claude-sonnet-5.5 / claude-opus-4-6)
#   4) 出 _public/model-compare-genre2.html
#
# 启动:
#   setsid nohup bash scripts/ops/run_genre2_compare.sh > /tmp/genre2.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
EV="$R/docs/evaluation"
SEED="$EV/seeds/urban_mystery.json"
DSV="$EV/genre2-dsv4-29"
GEM="$EV/genre2-gemini-29"
AB="$EV/ab-genre2-20261119"
PUB="$EV/_public"
CH=29

log() { echo "$(date -Is) $*"; }
cd "$R/backend" || exit 1
[ -f "$SEED" ] || { log "seed missing: $SEED"; exit 1; }

# --- 1) dsv4(新题材) ------------------------------------------------------
log "dsv4 arm: $CH chapters, genre=都市悬疑"
LLM_EXTRA_BODY_JSON='{"thinking":{"type":"disabled"}}' \
  "$PY" -m scripts.quality_eval.continuity_probe \
    --database-url "$DB" --out "$DSV" --seed-file "$SEED" \
    --chapters "$CH" --words 2000 --model deepseek-v4-flash --max-tokens 16000 \
    --max-consecutive-errors 5 \
  > /tmp/genre2_dsv4.log 2>&1
log "dsv4 exit=$? chapters=$(ls "$DSV"/ch*.txt 2>/dev/null | wc -l)"
"$PY" -m scripts.quality_eval.longrun --dir "$DSV" > /tmp/genre2_dsv4_longrun.log 2>&1
sed -n '1,5p' /tmp/genre2_dsv4_longrun.log | while read -r l; do log "  $l"; done

# --- 2) gemini(新题材) ----------------------------------------------------
log "gemini arm: $CH chapters, genre=都市悬疑"
"$PY" -m scripts.quality_eval.continuity_probe \
  --database-url "$DB" --out "$GEM" --seed-file "$SEED" \
  --chapters "$CH" --words 2000 --model gemini-3.1-pro --max-tokens 8000 \
  --max-consecutive-errors 5 \
  > /tmp/genre2_gemini.log 2>&1
log "gemini exit=$? chapters=$(ls "$GEM"/ch*.txt 2>/dev/null | wc -l)"
"$PY" -m scripts.quality_eval.longrun --dir "$GEM" > /tmp/genre2_gemini_longrun.log 2>&1
sed -n '1,5p' /tmp/genre2_gemini_longrun.log | while read -r l; do log "  $l"; done

# --- 3) 双盲 A/B(中立评审) ------------------------------------------------
log "arm_judge: gemini(A) vs dsv4(B), $CH pairs, neutral judges"
"$PY" -m scripts.quality_eval.arm_judge \
  --a "$GEM" --b "$DSV" --out "$AB" \
  --sample "$CH" --judges claude-sonnet-5.5,claude-opus-4-6 --seed 53 \
  > /tmp/genre2_ab.log 2>&1
log "arm_judge exit=$? -> $(tail -3 /tmp/genre2_ab.log | tr '\n' ' ' | cut -c1-300)"

# --- 4) 对照页 ------------------------------------------------------------
"$PY" -m scripts.quality_eval.sbs_report \
  --old "$GEM" --new "$DSV" --out "$PUB/model-compare-genre2.html" --ab "$AB" \
  --old-label "gemini-3.1-pro(都市悬疑)" \
  --new-label "deepseek-v4-flash(都市悬疑)" \
  > /tmp/genre2_sbs.log 2>&1
log "sbs_report exit=$? -> $PUB/model-compare-genre2.html"
log "ALL DONE"