#!/usr/bin/env bash
# 参考文笔对比(2026-11-20):用刚蒸馏出的风格卡跑 10 章,与"模型默认风格"逐章并排。
#
# 前提:ingest_reference_books 已跑完并输出 style_profile_id。
# 对照臂用已有的默认风格 10 章(longrun-10-gemini-2000-topup100),无需重跑。
#
# 用法:
#   setsid nohup bash scripts/ops/run_style_compare.sh <style_profile_id> > /tmp/stylecmp.log 2>&1 &
set -uo pipefail

R=/root/ai-write
PY="$R/.venv/bin/python"
DB="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval"
EV="$R/docs/evaluation"
SEED="$EV/seeds/urban_mystery.json"
STYLE_ID="${1:-}"
NEW="$EV/style-ref-10"
BASE="$EV/longrun-10-gemini-2000-topup100"
PUB="$EV/_public"

log() { echo "$(date -Is) $*"; }
cd "$R/backend" || exit 1

if [ -z "$STYLE_ID" ]; then
  log "用法: run_style_compare.sh <style_profile_id>"; exit 2
fi

# --- 1) 带风格卡跑 10 章 ----------------------------------------------------
# 必须与对照臂**同题材**:对照臂 longrun-10-gemini-2000-topup100 用的是内置题材
# 《渡口旧账》。所以这里**不传 --seed-file** —— 否则并排页面里的差异会混进题材差异,
# 唯一变量必须只有"有没有绑文笔卡"。
log "style arm: 10 chapters with style_profile=$STYLE_ID (built-in genre, same as control)"
"$PY" -m scripts.quality_eval.continuity_probe \
  --database-url "$DB" --out "$NEW" \
  --style-profile "$STYLE_ID" \
  --chapters 10 --words 2000 --model gemini-3.1-pro --max-tokens 8000 \
  --max-consecutive-errors 3 \
  > /tmp/stylecmp_probe.log 2>&1
log "probe exit=$? chapters=$(ls "$NEW"/ch*.txt 2>/dev/null | wc -l)"

# --- 2) 曲线 + 并排 -------------------------------------------------------
"$PY" -m scripts.quality_eval.longrun --dir "$NEW" > /tmp/stylecmp_longrun.log 2>&1
sed -n '1,5p' /tmp/stylecmp_longrun.log | while read -r l; do log "  $l"; done

"$PY" -m scripts.quality_eval.sbs_report \
  --old "$BASE" --new "$NEW" --out "$PUB/style-compare.html" \
  --old-label "模型默认风格(无参考文笔)" \
  --new-label "参考文笔:龙族+天之炽(综合文笔卡)" \
  > /tmp/stylecmp_sbs.log 2>&1
log "sbs_report exit=$? -> $PUB/style-compare.html"

# --- 3) 刷新总览页(把这一版也纳进去) ------------------------------------
"$PY" -m scripts.quality_eval.build_gallery --out "$PUB/gallery.html" > /tmp/stylecmp_gallery.log 2>&1
log "gallery refreshed"
log "ALL DONE"