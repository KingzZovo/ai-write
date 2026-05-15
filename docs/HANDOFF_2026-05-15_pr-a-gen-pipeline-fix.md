# Handoff 2026-05-15 — PR-A-GEN-PIPELINE-FIX 收稿

> 上手者启动顺序：先读本文 → `docs/PROGRESS.md` 末段 → `backend/scripts/_pr_a_verify.py` 注释

## 1. 速览

| 项 | 值 |
|---|---|
| 仓库 | `/root/ai-write` |
| 分支 | `feat/pr-a-gen-pipeline-fix` |
| HEAD | `8583910`（live-LLM verify script）/ 父 `cd77b5c`（实修） |
| 父 main | `2f01744` |
| 是否 push | 是（origin 同名分支） |
| 是否合 main | 否（待用户决定） |
| PR URL（待 open） | https://github.com/KingzZovo/ai-write/pull/new/feat/pr-a-gen-pipeline-fix |
| Backend env | docker compose 11 容器 健康；JWT secret + ADMIN_USERNAMES 已配置 |
| LLM 配置 | `model_router.task_routing['generation'] → ac6eb9cd 大纲 endpoint`，已验证可直接调用 |

## 2. 本批交付物

| sha | 主题 | 验证状态 |
|---|---|---|
| `cd77b5c` | PR-A-GEN-PIPELINE-FIX: real wiring of /api/generate/batch（Bug A/B/C 三连修） | `_pr_a_verify.py` stub 模式 PASS（5s）；`run_text_prompt(generation)` 真 LLM 调用 PASS（返回中文文本） |
| `8583910` | PR-A: add opt-in live-LLM end-to-end verify script | 脚本可启动，跑到 `BatchStatus.RUNNING` 实跑 LLM；agent session 内未等到 batch 完成（2-4 min/章），未抓最终 RESULT 行 |

## 3. 接手第一件事

```bash
cd /root/ai-write
git status                                 # 应 working tree clean
git log --oneline origin/main..HEAD        # 应见 cd77b5c + 8583910
git log --oneline -3 main                  # 应 2f01744 在头

# 验证 stub 回归（5 秒）
docker exec ai-write-backend-1 python /app/scripts/_pr_a_verify.py
# 期望最后一行: RESULT: PASS

# 验证 LLM 路由通畅
docker exec ai-write-backend-1 python -c "
import asyncio
from app.db.session import async_session_factory
from app.services.prompt_registry import run_text_prompt
async def m():
    async with async_session_factory() as db:
        r = await run_text_prompt(task_type='generation',
                                  user_content='测试一句话',
                                  db=db, extra_system='')
    print(type(r).__name__, repr(r.text[:60]))
asyncio.run(m())"
# 期望 GenerationResult + 返回的中文片段
```

## 4. 待做任务 A — 合并 PR-A 到 main（待用户确认）

PR-A 已 push 但未开 PR / 未合并。可选两种走法：

**4a. 直接合（推荐）**
```bash
cd /root/ai-write
git checkout main
git pull --ff-only origin main
git merge --no-ff feat/pr-a-gen-pipeline-fix -F /tmp/msg.txt   # 中文 commit msg 先写文件
git push origin main
```

**4b. 开 GitHub PR 走 review**
打开 https://github.com/KingzZovo/ai-write/pull/new/feat/pr-a-gen-pipeline-fix ，按 4 段式描述：
- Context：`/api/generate/batch` 多个 release 内 100% TypeError，且即便不抛错也未持久化
- Change：见 `cd77b5c` commit message Bug A/B/C 三段修复
- Verification：`docker exec ai-write-backend-1 python /app/scripts/_pr_a_verify.py` → `RESULT: PASS`；可选 live `_pr_a_verify_live_llm.py`
- Docs updated：本 handoff + PROGRESS / HANDOFF_TODO / HANDOFF_EXECUTION / RUNBOOK

## 5. 待做任务 B — schema 孤儿清理（低风险）

本批 audit 发现 `backend/app/schemas/project.py` 里有一组从未被 router import 的 `*Create / *Update` 类，**和 `backend/app/api/<entity>.py` 里被实际使用的同名类是字段集不同**：

| 实际被使用（router 用） | 孤儿（schemas/project.py，无人 import） |
|---|---|
| `backend/app/api/chapters.py:16 ChapterCreate` `:23 ChapterUpdate` | `schemas/project.py:88,97` |
| `backend/app/api/outlines.py:23 OutlineCreate` `:29 OutlineUpdate` | `schemas/project.py:129,137` |
| `backend/app/api/volumes.py:17 VolumeCreate` `:23 VolumeUpdate` | `schemas/project.py:58,65` |
| `backend/app/api/styles.py:29 StyleProfileCreate` `:38 StyleProfileUpdate` | `schemas/project.py:210,216` |

**字段集差异关键例**（不是无害重复）：
- `OutlineUpdate` schemas 版有 `level / parent_id / version` 字段，api 版只接 `content_json / is_confirmed`
- `VolumeUpdate` schemas 版多 `target_word_count`，api 版没有
- `StyleProfileUpdate` 两版完全不同字段集（schemas 版只有 name/source_book/config_json；api 版有 description/rules_json/anti_ai_rules/tone_keywords/sample_passages/is_active）

**风险**：如果有人误从 `schemas.project` import 这些类绑到 router endpoint，会得到一个不同的请求 body schema → 400 / 字段被丢弃。

**清理建议**：
```bash
# 验证零依赖（应只列出类定义本身）
cd /root/ai-write
for n in OutlineUpdate OutlineCreate VolumeUpdate VolumeCreate StyleProfileUpdate StyleProfileCreate ChapterUpdate ChapterCreate CharacterCreate CharacterUpdate WorldRuleCreate WorldRuleUpdate ForeshadowCreate ForeshadowUpdate VolumeSummaryCreate VolumeSummaryUpdate RelationshipCreate RelationshipUpdate; do
  echo "=== $n ==="
  grep -rn "\\b$n\\b" backend/app/ --include=*.py | grep -v -E "class $n|tests/|schemas/project.py:"
done
# 若全空 → 安全删除 schemas/project.py 里这些类块
```

## 6. 待做任务 C — Live LLM 端到端 RESULT 行（可选）

`backend/scripts/_pr_a_verify_live_llm.py` 已 push 但 agent 自己跑没等到结束。两种验法：

**6a. 容器内手跑（前台 2-4 min/章，看进度）**
```bash
docker exec -it ai-write-backend-1 python -u /app/scripts/_pr_a_verify_live_llm.py
```

**6b. 后台跑 + 轮询日志**
```bash
docker exec -d ai-write-backend-1 sh -c 'python -u /app/scripts/_pr_a_verify_live_llm.py > /tmp/llm_e2e.log 2>&1'
# 5 分钟后
docker exec ai-write-backend-1 tail -20 /tmp/llm_e2e.log
# 期望最后一行 RESULT: PASS, chars=NNNN, word_count=NNN
```

脚本结束时会自动清理它自己创建的 project / volume / chapter row。

## 7. 关键 ID / endpoint / 速查

### LLM 路由（DB 实状）
- `llm_endpoints`：3 条 — `ac6eb9cd 大纲 standard`（generation 默认）/ `dfd26325 本地Qwen standard` / `88e25ef0 英伟达emb embedding`
- `model_configs`：仅 `extraction` 一条；其它 task 通过 `prompt_assets.endpoint_id` 装配进 `task_routing`
- `prompt_assets active`：`generation → ac6eb9cd tier=flagship`（小说正文生成 prompt） / `polishing → dfd26325` / `extraction → dfd26325`
- 26 个 task_routing key 见 `model_router.task_routing`（含 generation / scene_writer / scene_planner / outline_* / critic_* / polishing / extraction / characters_extraction / ... 全套）

### API endpoints（本批触及）
- `POST /api/generate/batch` — SSE 批量生成（Bug A/B 修复点）
- `PUT /api/projects/{pid}/chapters/{cid}` — 加了 protect guard（empty / shrink>60%），逃生 `body.force=true`（Bug C 修复点）

### 函数签名（修复后真相）
- `ChapterGenerator.generate(*, project_id, volume_id, chapter_idx, db, chapter_id=None, user_instruction="") -> str`（keyword-only）
- `BatchGenerator.generate_batch(project_id, chapter_configs, *, db=None, style_instruction="", on_progress=None) -> BatchJobStatus`
  - **`on_progress` 必须是 sync 函数**；async def 会 RuntimeWarning + payload 不消费
- `run_text_prompt(task_type, user_content, db, extra_system='', project_id=None, chapter_id=None, rag_hits=None, messages=None) -> GenerationResult`
  - 关键字是 `user_content`，不是 `user_prompt`

### 凭据 / Auth
```bash
docker exec ai-write-backend-1 python3 -c "import jwt;from datetime import datetime,timedelta,timezone;from app.config import settings;print(jwt.encode({'sub':'king','exp':datetime.now(timezone.utc)+timedelta(days=1)},settings.SECRET_KEY,algorithm='HS256'))"
```

## 8. 已知陷阱（本批新增 / 反复踩）

1. **`docker exec -d` 后 `&&` 链截断** — 前置命令 exit non-zero 时 chain 终止；改用 `;` 或 `bash -c 'cmd1; cmd2'`
2. **容器内无 `ps` / `pgrep`** — 改用 `ls /proc | grep '^[0-9]\\+$'` + `cat /proc/$PID/cmdline | tr '\\0' ' '`
3. **MCP runTool 默认 60s 超时** — 长任务必须 `docker exec -d` 或后台 `setsid nohup ... &`，再轮询；不要直接前台 wait
4. **sh 不支持 `<(...)` 进程替换** — 套 `bash -c '...'`
5. **中文 commit message + `git commit -m`** — 必乱码，用 `git commit -F /tmp/msg.txt`
6. **`on_progress` 不能 async def**（见 §7）
7. **没真实 stack trace 不写「会撞 XXX」**（错误 Z 教训）— 上轮误判 "No model configured for 'generation'" 全因没真跑 `run_text_prompt`；现已自证根本不存在该 raise
8. **`agent` 类型的 page parent 不能用于 createPage** — agent_url 只能从 loadPage 看到，写不了

## 9. 历史临时文件 / 脚本清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `backend/scripts/_pr_a_verify.py` | ✅ 已 commit `cd77b5c` | stub 回归（5 s），可反复 CI 跑 |
| `backend/scripts/_pr_a_verify_live_llm.py` | ✅ 已 commit `8583910` | live LLM e2e（2-4 min/章），手跑 |
| `backend/scripts/_probe_generation.py` | ❌ 已删除 | 一次性 router probe，证据已抓 |
| `/tmp/e2e_smoke.py` | 在容器内 | 上轮误判事件遗留，可删 |

**DB 残留待清**（之前 kill 进程时 chain SQL 被 exit 137 截断未执行）：
```bash
docker exec ai-write-backend-1 sh -c '
PGPASSWORD=postgres psql -h postgres -U postgres -d aiwrite <<SQL
DELETE FROM chapters WHERE id = '\''0e530fa5-65ff-4da0-b96c-d42736cbe091'\'';
DELETE FROM volumes  WHERE id = '\''99c32ff1-b24a-48ae-9d04-a0d2b8d5bfc5'\'';
DELETE FROM projects WHERE id = '\''f01298f0-c50c-44e1-90a0-ad0ab5d23b27'\'';
SQL
'
```

## 10. 本批文件改动点速查

| 文件 | LOC | 关键点 |
|---|---|---|
| `backend/app/api/chapters.py` | +43 / -3 | `ChapterUpdate.force` 字段 + empty/shrink protect guard |
| `backend/app/api/rewrite.py` | +50 / -17 | `batch_generate` 拥有自己的 DB session；asyncio.Queue 推 SSE 进度事件 |
| `backend/app/services/batch_generator.py` | +123 / -42 | 接 AsyncSession；调用 ChapterGenerator 新签名；持久化到 chapters 表；post-hook 保底；保留 `_MIN_CHAPTER_BYTES` |
| `backend/scripts/_pr_a_verify.py` | +242 / 0 | stub 模式回归（不依赖 LLM 配置） |
| `backend/scripts/_pr_a_verify_live_llm.py` | +122 / 0 | live LLM e2e（可选） |

## 11. 合并 PR 模板（4 段式）— 已嵌入到本 handoff §4

EOL.
