#!/bin/bash
# 重启 ai-write 的 API 与 celery(main / pipeline-control / cognition / beat)。
#
# 为什么需要这个脚本:
#   1) 改完代码、改了 prompt_assets 的模型路由、或换了 embedding 模型之后,
#      **必须**重启 API 与 worker —— prefork 子进程只在启动时加载代码与 DB 路由;
#   2) 启动所需的 env(DATABASE_URL / SECRET_KEY / NEO4J_* / QDRANT_* / REDIS_URL /
#      QUALITY_EVAL_API_KEY / LLM_EXTRA_BODY_JSON)不进仓库,运行中的进程里有全套,
#      直接从 /proc/<pid>/environ 复制,避免手抄漏项。
#
# 用法:
#   bash backend/scripts/ops/restart_services.sh            # 只重启 celery
#   bash backend/scripts/ops/restart_services.sh --with-api # 连 API 一起重启
#   ENV_FILE=/tmp/aiwrite.env bash ...                      # 复用已提取的 env
#
# 关键坑(踩过):
#   * 不要在**同一个 shell 命令行**里既写含 "celery"/"app.tasks" 字面的代码又做
#     kill —— 那些字面会出现在自己的 cmdline 里,被自己的匹配循环杀掉(exit 143)。
#     kill 逻辑放在本文件内执行,调用方的 cmdline 只有脚本路径,不含关键字。
#   * 从 /proc/<pid>/environ 抄 env 时,含 JSON 的值(LLM_EXTRA_BODY_JSON)必须
#     **重新加单引号**,否则 source 时引号被 shell 剥掉,退化成非法 JSON,
#     worker 会打印 "LLM_EXTRA_BODY_JSON is not valid JSON; ignored" 并静默继续。
set -u

WITH_API=0
[ "${1:-}" = "--with-api" ] && WITH_API=1

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BACKEND_DIR" || exit 1

PY=/root/ai-write/.venv/bin/python
CEL=/root/ai-write/.venv/bin/celery
ENV_FILE="${ENV_FILE:-/tmp/aiwrite.env}"
LOG_DIR="${LOG_DIR:-/tmp}"

# --- 1) 提取 env(仅第一次;已有 ENV_FILE 就直接用) ---
if [ ! -s "$ENV_FILE" ]; then
  "$PY" "$(dirname "${BASH_SOURCE[0]}")/dump_service_env.py" "$ENV_FILE" || {
    echo "无法提取 env;请显式提供 ENV_FILE" >&2
    exit 1
  }
fi

set -a
. "$ENV_FILE" || exit 1
set +a

# --- 2) 停 ---
if [ "$WITH_API" = "1" ]; then
  API_PID="$(ss -ltnp 2>/dev/null | grep ':8020' | grep -oP 'pid=\K[0-9]+' | head -1)"
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null && echo "api $API_PID stopped"
fi

"$PY" - <<'PYEOF'
import os, signal
n = 0
for p in os.listdir('/proc'):
    if not p.isdigit():
        continue
    try:
        cl = open('/proc/%s/cmdline' % p, 'rb').read().decode(errors='replace')
    except Exception:
        continue
    if '/celery' in cl and 'app.tasks' in cl:
        try:
            os.kill(int(p), signal.SIGTERM)
            n += 1
        except Exception:
            pass
print('stopped %d celery processes' % n)
PYEOF
sleep 3

# --- 3) 起 ---
start() {  # start <logfile> <args...>
  local log="$1"; shift
  setsid nohup "$@" > "$LOG_DIR/$log" 2>&1 &
}

if [ "$WITH_API" = "1" ]; then
  start aiwrite_api.log "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8020
fi
start celery_main.log "$CEL" -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -n main@%h
start celery_pipe.log "$CEL" -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -Q pipeline-control -n pipe@%h
start celery_cog.log  "$CEL" -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -Q cognition -n cog@%h
# 恢复/派发任务必须有独立 worker:默认队列单次 pipeline 任务实测占用 1031 秒,
# 期间恢复任务全部排队 → pending 认知记录派发不出去 → 认知就绪守候超时 → 级联失败。
start celery_recovery.log "$CEL" -A app.tasks:celery_app worker --loglevel=info --concurrency=4 -Q recovery -n rec@%h
start celery_beat.log "$CEL" -A app.tasks:celery_app beat --loglevel=info --schedule="$LOG_DIR/aiwrite-celerybeat"

sleep 8

# --- 4) 健康检查 ---
echo '--- health ---'
if [ "$WITH_API" = "1" ]; then
  ss -ltnp 2>/dev/null | grep ':8020' | head -1 || echo 'API NOT LISTENING'
fi
echo "celery processes: $(pgrep -c '[c]elery' 2>/dev/null || echo 0)"
if grep -q 'LLM_EXTRA_BODY_JSON is not valid' "$LOG_DIR/celery_main.log" 2>/dev/null; then
  echo 'WARNING: LLM_EXTRA_BODY_JSON invalid in worker env' >&2
fi
