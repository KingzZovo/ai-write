# HANDOFF 2026-05-04 — feat/phase2-fix（B1 失效点收尾批次启动前）

> **新窗口接手优先看这一份。** PROGRESS / HANDOFF_TODO / HANDOFF_EXECUTION 顶部 banner 已指向本文件。

---

## 1. 速览

- 仓库：`/root/ai-write`（AWS EC2，docker compose 编排）
- 分支：`feat/phase2-fix`（**ahead origin 9 commits，未 push**）
- HEAD：`a5744a3`（`chore(frontend): add .dockerignore`）
- 凭据：本机 root 直连，无需额外凭据；GitHub MCP 已挂在 `connections.mcpServer_github`（如需 push 走 https + 已配 origin）
- 部署入口：`http://127.0.0.1:8080`（nginx → frontend:3000 / backend:8000）
- 前端 Next.js BUILD_ID（最新一次容器内 build）：`hhSqzezETm-_vgp8yHNMC`
- 测试用户 / 项目：`user://346d872b-594c-8174-affd-0002474450a5` / project `20d164ab-232f-4863-8265-452186638d83`（5 卷 × 150 章数据真实存在，DB 已验证）

---

## 2. 本批交付物（9 commits，全部本地 commit、未 push）

| # | sha | 主题 | 验证 |
|---|---|---|---|
| 1 | `4a1c507` | fix(infra): 修复 502 + rebuild frontend 镜像 (PR-FIX-502) | curl 200 |
| 2 | `65bf88d` | fix(workspace): wizard 锁安全纲 - volumes>0 强制 editor (PR-FIX-WIZARD-LOCK) | tsc + 手测 |
| 3 | `8200cd4` | fix(backend): ask-user/vector-store/call-logs 422 + model-config/tasks 404 (PR-FIX-API-422-404) | py_compile + curl |
| 4 | `523158d` | fix(frontend): wizard lock 仅在 0→>0 迁移点生效 (PR-FIX-WIZARD-LOCK-V2) | 手测 |
| 5 | `b3e550f` | fix(frontend): "开始创作" 同时选中第一章 (PR-FIX-START-CREATE) | 手测 |
| 6 | `50bee3c` | fix(chapters): tolerate NULL columns in ChapterResponse (PR-FIX-CHAPTER-422) | curl /api/projects/{pid}/chapters → 200, 750 条 |
| 7 | `fcd08d5` | fix(api): force cache:'no-store' on apiFetch (PR-FIX-NO-STORE) | tsc |
| 8 | `463a1e8` | **fix(workspace): bump LS keys to v2 + 清旧 sidebar/panel collapsed (PR-FIX-LS-V2)** | build + 浏览器人工验证 |
| 9 | `99fd114` | fix(nginx): 停掉 /_next/static/ 365d 强缓存 (PR-FIX-NGINX-CACHE) | nginx -t + reload |
| 10 | `a5744a3` | chore(frontend): .dockerignore | n/a |

**Push 前置**：origin 是 `https://github.com/KingzZovo/ai-write.git`，需要 PAT。如果 push 失败，先和用户对齐 token / 是否走 GitHub MCP `create_pull_request` 路线。

---

## 3. 接手第一件事的 cmd 序列

```bash
cd /root/ai-write
git status -sb && git log --oneline -12
git reflog | head -20
docker compose ps
docker exec ai-write-frontend-1 cat .next/BUILD_ID    # 期望 hhSqzezETm-_vgp8yHNMC
curl -s -o /dev/null -w 'http=%{http_code}\n' http://127.0.0.1:8080/api/health
# Push（如用户授权）
git push origin feat/phase2-fix
```

---

## 4. 待做任务 — B1 失效点队列（按用户原话"按顺序推进、全部修好、不要折中方案"）

用户今天给出的失效点（**已修复的删去线，未修复的展开**）：

- ~~1. 刷新后左侧看不到卷/章节~~ → PR-FIX-LS-V2 + PR-FIX-CHAPTER-422 已修
- ~~2. "开始创作" 退到全书大纲~~ → PR-FIX-START-CREATE 已修
- ~~3. wizard 把用户拽回向导~~ → PR-FIX-WIZARD-LOCK-V2 已修

### A) TOKEN 用量显示 0（**新窗口第一项**）

现象：右侧顶 bar / 设置页 token 用量栏长期显示 0。  
上次相关修复 `b3fe18e fix(B2): drop nonexistent quota imports in PR-USAGE-SYNC` 只解了 import 报错，写入路径仍未确认通畅。

排查 cmd（按这个顺序）：
```bash
# 1. 看最近一次生成请求是否落库
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c \
  "select id,project_id,prompt_tokens,completion_tokens,total_tokens,created_at from llm_usage order by created_at desc limit 10"
# 2. 看 backend record_usage 是否被调用
docker logs ai-write-backend-1 --since 30m | grep -i 'record_usage\|usage\|tokens' | tail -50
# 3. 看前端拿 usage 的 API
grep -rn 'usage' frontend/src/components | head -20
curl -s http://127.0.0.1:8080/api/projects/20d164ab-232f-4863-8265-452186638d83/usage | head -5
```

预期定位：要么 (a) `record_usage()` 被 strand / outline_generator 漏调，要么 (b) `/api/projects/{pid}/usage` 路由聚合 SQL 错。两边修。

### B) 全书大纲 `<volume-plan>[...]</volume-plan>` 标签泄露到 UI

现象：用户截图里 outline 文本里直接出现 `<volume-plan>[ {...} ]</volume-plan>` 字面量。  
根因猜测：`backend/app/services/outline_generator.py` 把 LLM 原始输出里的 XML-ish 包裹标签拼回了 `outline_text`，前端渲染纯文本时没剥。

排查 cmd：
```bash
grep -n 'volume-plan\|<volume\|</volume' backend/app/services/outline_generator.py
grep -rn 'volume-plan' frontend/src | head
# 看一条真实数据
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c \
  "select substring(outline_text from 1 for 200) from outlines where project_id='20d164ab-232f-4863-8265-452186638d83' order by created_at desc limit 1"
```

方案：在写库前 strip `<volume-plan>...</volume-plan>` / `<chapter-plan>...</chapter-plan>` 等 wrapper（保留 JSON 内容到 `outline_json` 字段，纯文本字段只放人类可读叙述）。

### C) 设定集 → 人物设定 / 世界规则编辑空

现象：从设定集列表点进编辑页，表单字段为空，但 API 返回数据正确。  
上次 `64743cc fix(frontend): PR-WORLDRULES-FE accept world_rules field name from API` 改了世界规则字段名兼容，但**人物设定**没改，且编辑页可能仍写死另一个 key。

排查 cmd：
```bash
grep -rn 'character.*name\|character_name\|characterName' frontend/src/components/settings | head
curl -s http://127.0.0.1:8080/api/projects/20d164ab-232f-4863-8265-452186638d83/neo4j-settings/characters | head -200
```

### D) 第一卷为空 / 显示从第二卷开始

DB 端已验证 `vol1=150 章` 真实存在（用临时脚本 `check_chs.py` 跑过，已删）。  
所以是前端在 `WorkspaceLayout` / `OutlineSidebar` 把 `volume_idx` 当成数组下标用了 1-based vs 0-based 错位。

排查 cmd：
```bash
grep -rn 'volume_idx\|volumeIdx\|volumes\[' frontend/src/components/workspace | head
```

### E) 右侧 "查看全书 / 分卷 / 章节大纲" 三个按钮失效

上次 PR-FIX-WIZARD-LOCK-V2 改了路由锁，但右侧顶 bar 这三个按钮的 onClick handler 也要单独看（可能是 setView 没传 chapterId / volumeId）。

排查 cmd：
```bash
grep -rn '查看全书\|查看分卷\|查看章节\|onClick.*setView' frontend/src/components/workspace | head
```

---

## 5. （预留：本批后接的 B2 / B3）

用户在更早的会话提到 B2 阶段还有 "灰灯重启" 章节正文流的 token 计费、strand 失败回退等收尾，按 B1 全清后再启动。新窗口暂时不动。

---

## 6. 关键 ID / endpoint / schema

- 测试 project_id：`20d164ab-232f-4863-8265-452186638d83`（5 卷，每卷 150 章）
- 主要表：`projects` / `volumes` / `chapters` / `outlines` / `llm_usage` / `settings_*`
- Neo4j 设定集端点：`POST /api/projects/{pid}/neo4j-settings/{characters|world_rules|relationships|locations|character_states|organizations|foreshadows}`
- materialize：`POST /api/admin/entities/materialize`
- 章节列表：`GET /api/projects/{pid}/chapters`（PR-FIX-CHAPTER-422 后已 200）
- BUILD_ID 检查：`docker exec ai-write-frontend-1 cat .next/BUILD_ID`

---

## 7. 已知陷阱 / shell gotcha

1. **MCP 单次工具调用 240s 超时** → 任何 build / 长 curl 必须 `setsid nohup ... > /tmp/log 2>&1 < /dev/null &` 然后轮询。本批 `npm run build` 走的就是这条。
2. **kill 后台用 PID** → `pkill -f curl` 会误杀本会话其它 curl，启动时一律记 `/tmp/<task>.pid`。
3. **中文 commit message** → 全部 `git commit -F /tmp/msg.txt`，绝不 `-m "中文..."` 会乱码。
4. **localStorage 残留是顽固病灶** → PR-FIX-LS-V2 已加 `migrateOldKeys()`，未来再加 LS key 直接走 v3 / v4 后缀，不要复用 v2。
5. **nginx /_next/static/ 不再 365d** → 改 chunk 看到效果不必再 `docker restart nginx`，但 `nginx.conf` 改完要 `nginx -t && nginx -s reload`。
6. **frontend 改完只有 docker cp 进容器是不够的** → 必须 `npm run build` 拿到新 BUILD_ID + `docker restart ai-write-frontend-1` 才走新 chunk。
7. **章节生成 API 历史脏数据** → `chapters` 表里 ChapterResponse 旧记录有 NULL 列，PR-FIX-CHAPTER-422 已用 `model_validate` 容错，新代码别再回退到严格 schema。

---

## 8. 历史临时文件 / 脚本

- `/tmp/fe_build_v5.log` / `/tmp/fe_build_v5.pid` — 本批最后一次前端 build 日志
- `/tmp/msg_*.txt` — 各 PR 的 commit message 草稿（可清）
- `backend/check_chs.py` — 已删（11 行排查脚本，验证 vol1 真实存在 150 章后清理）

---

## 9. 本批文件改动点速查

```
nginx/nginx.conf                                         (PR-FIX-NGINX-CACHE)
frontend/.dockerignore                                   (chore)
frontend/src/components/workspace/WorkspaceLayout.tsx    (PR-FIX-LS-V2 + PR-FIX-WIZARD-LOCK-V2 + PR-FIX-START-CREATE)
frontend/src/lib/apiFetch.ts                             (PR-FIX-NO-STORE)
backend/app/api/v1/chapters.py                           (PR-FIX-CHAPTER-422)
backend/app/api/v1/{ask_user,vector_store,call_logs,
  model_config,tasks}.py                                 (PR-FIX-API-422-404)
```

---

## 10. 合并 PR 模板（4 段式）

```
## Context
说明用户报障原文 + 复现路径 + 你定位的 root cause（一段话）

## Change
- 文件 A: 改了什么
- 文件 B: 改了什么
（每条不超过 1 行，必要时再加一段说明）

## Verification
- 命令 1 → 预期输出
- 命令 2 → 预期输出
- E2E（如适用）：步骤 + 截图/日志位置

## Docs updated
- docs/HANDOFF_<date>_<branch>.md
- docs/PROGRESS.md（如更新里程碑）
- docs/RUNBOOK.md（如改了运维步骤）
```

---

## 11. EOL

本批就此封箱。新窗口接手按 §3 cmd 跑一遍验证，然后从 §4 A) TOKEN 用量开始一项一项推。  
用户原话：**"按顺序推进、全部修好、不要折中方案"**。

