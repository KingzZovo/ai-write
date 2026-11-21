#!/usr/bin/env bash
# 100 章最终验证(2026-11-18):用**默认配置**跑满 100 章,并渐进刷新对照页。
#
# 默认配置即最终方案(无需 env):
#   SCENE_TARGET_OVERALLOC=1.08 / CHAPTER_LENGTH_TOPUP_RATIO=1.0
#   CHAPTER_LENGTH_CONTINUATION_MAX=1 / CHAPTER_MIN_TARGET_WORD_RATIO=0.95
#
# 为什么不用 env 显式写一遍:要验证的是"仓库默认值就能产出 ≥2000 字"。
# 目标:
#   1) 篇幅是否长程不衰减(ch1-33 / ch34-66 / ch67-100 三段中位比);
#   2) blocked / truncated / error 比例是否正常;
#   3) 每 20 章刷新一次 _public/length-100-final.html,跑完出终版。
#
# 启动:
#   setsid nohup bash scripts/ops/run_final_100.sh > /tmp/final100.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
EV="$R/docs/evaluation"
OUT="$EV/longrun-100-gemini-2000-final"
BASE="$EV/longrun-100-gemini-2000-20261117"
PUB="$EV/_public"
HTML="$PUB/length-100-final.html"

log() { echo "$(date -Is) $*"; }
mkdir -p "$PUB"
cd "$R/backend" || exit 1

# --- 1) 渐进刷新:每变化 10 章重生成一次对照页 ------------------------------
(
  last=-1
  while true; do
    n=$(ls "$OUT"/ch*.txt 2>/dev/null | wc -l)
    if [ "$n" != "$last" ] && [ "$n" -gt 0 ]; then
      last=$n
      (cd "$R/backend" && "$PY" -m scripts.quality_eval.sbs_report \
         --old "$BASE" --new "$OUT" --out "$HTML" \
         --old-label "gemini · 修复前(超分配关 / 无补足)" \
         --new-label "gemini · 最终方案(超分配 1.08 + 补足到 2000)" \
         > /tmp/final100_sbs.log 2>&1) && log "page refreshed chapters=$n"
    fi
    sleep 300
  done
) &
WATCHER=$!

# --- 2) 跑满 100 章 --------------------------------------------------------
log "starting final 100-chapter run (default config) -> $OUT"
"$PY" -m scripts.quality_eval.continuity_probe \
  --database-url "$DB" --out "$OUT" \
  --chapters 100 --words 2000 --model gemini-3.1-pro --max-tokens 8000 \
  --max-consecutive-errors 10 \
  > /tmp/final100_probe.log 2>&1
RC=$?
log "probe exit=$RC chapters=$(ls "$OUT"/ch*.txt 2>/dev/null | wc -l)"

# --- 3) 曲线 + 终版对照页 --------------------------------------------------
"$PY" -m scripts.quality_eval.longrun --dir "$OUT" > /tmp/final100_longrun.log 2>&1
log "longrun:"; sed -n '1,8p' /tmp/final100_longrun.log | while read -r l; do log "  $l"; done

"$PY" -m scripts.quality_eval.sbs_report \
  --old "$BASE" --new "$OUT" --out "$HTML" \
  --old-label "gemini · 修复前(超分配关 / 无补足)" \
  --new-label "gemini · 最终方案(超分配 1.08 + 补足到 2000)" \
  > /tmp/final100_sbs.log 2>&1
log "final page exit=$?"

kill "$WATCHER" 2>/dev/null || true
log "ALL DONE"