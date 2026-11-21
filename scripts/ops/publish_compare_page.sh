#!/usr/bin/env bash
# 把两次长跑的逐章对照页持续发布到本地目录(供隧道/静态服务对外提供)。
#
# 行为:每 60s 看一眼新跑目录;章节数变化就重生成一次 HTML;新跑写完
# continuity.json 且章节数 >= 100 时,再生成一次终版并退出。
#
# 用法(建议 setsid 起,免得被会话收尾杀掉):
#   setsid nohup bash scripts/ops/publish_compare_page.sh > /tmp/publish_sbs.log 2>&1 &
set -uo pipefail

REPO="${REPO:-/root/ai-write}"
PY="$REPO/.venv/bin/python"
OLD="${OLD_DIR:-$REPO/docs/evaluation/longrun-100-20261117}"
NEW="${NEW_DIR:-$REPO/docs/evaluation/longrun-100-095-20261117}"
OUT="${OUT_FILE:-$REPO/docs/evaluation/_public/index.html}"
SLEEP_SECONDS="${SLEEP_SECONDS:-60}"

last_count=-1
while true; do
  count=$(ls "$NEW"/ch*.txt 2>/dev/null | wc -l)
  if [ "$count" != "$last_count" ]; then
    (cd "$REPO/backend" && "$PY" -m scripts.quality_eval.sbs_report \
        --old "$OLD" --new "$NEW" --out "$OUT") \
      || echo "$(date -Is) regen failed chapters=$count"
    echo "$(date -Is) regenerated chapters=$count"
    last_count=$count
  fi

  if [ "$count" -ge 100 ] && [ -f "$NEW/continuity.json" ]; then
    (cd "$REPO/backend" && "$PY" -m scripts.quality_eval.sbs_report \
        --old "$OLD" --new "$NEW" --out "$OUT" \
        --new-label "新:0.95 阈值 + 时间钉子(已跑完)") \
      || echo "$(date -Is) final regen failed"
    echo "$(date -Is) DONE chapters=$count"
    break
  fi

  sleep "$SLEEP_SECONDS"
done