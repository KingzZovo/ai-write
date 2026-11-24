#!/usr/bin/env python3
"""把正在运行的 ai-write API 进程的启动 env 抄成一份可 source 的文件。

为什么需要:
    改完代码 / 换了模型路由 / 换了 embedding 模型之后必须重启 API 与 worker,
    而启动所需的 env(DATABASE_URL / SECRET_KEY / NEO4J_* / QDRANT_* / REDIS_URL /
    QUALITY_EVAL_API_KEY / LLM_EXTRA_BODY_JSON)不进仓库。运行中的进程里就有全套,
    从 /proc/<pid>/environ 复制可以避免手抄漏项。

用法:
    python scripts/ops/dump_service_env.py [输出路径]   # 默认 /tmp/aiwrite.env

坑:
    含引号/花括号的值(LLM_EXTRA_BODY_JSON)必须重新加单引号,否则 source 时
    shell 会把引号剥掉,退化成非法 JSON,worker 静默忽略并继续
    (日志里是 "LLM_EXTRA_BODY_JSON is not valid JSON; ignored")。
"""
from __future__ import annotations

import os
import stat
import sys

KEEP = {
    'DATABASE_URL', 'SECRET_KEY', 'NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD',
    'REDIS_URL', 'QDRANT_HOST', 'QDRANT_PORT', 'QUALITY_EVAL_API_KEY',
    'DISABLE_AUTH', 'APP_ENV', 'LLM_EXTRA_BODY_JSON',
}
# 需要重新加单引号的值(含 shell 元字符)。
QUOTE = {'LLM_EXTRA_BODY_JSON'}


def find_api_pid() -> str | None:
    for p in sorted(os.listdir('/proc'), key=lambda x: (len(x), x)):
        if not p.isdigit():
            continue
        try:
            cl = open('/proc/%s/cmdline' % p, 'rb').read().decode(errors='replace').replace('\0', ' ')
        except Exception:
            continue
        if 'app.main:app' in cl and '--port 8020' in cl:
            return p
    return None


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/aiwrite.env'
    pid = find_api_pid()
    if pid is None:
        print('找不到运行中的 ai-write API 进程(--port 8020)', file=sys.stderr)
        return 1
    lines: list[str] = []
    for raw in open('/proc/%s/environ' % pid, 'rb').read().split(b'\0'):
        if not raw:
            continue
        item = raw.decode(errors='replace')
        key = item.split('=', 1)[0]
        if key not in KEEP:
            continue
        if key in QUOTE:
            k, _, v = item.partition('=')
            item = "%s='%s'" % (k, v)
        lines.append(item)
    with open(out, 'w') as fh:
        fh.write('\n'.join(sorted(lines)) + '\n')
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    print('wrote %d vars to %s (from pid %s)' % (len(lines), out, pid))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
