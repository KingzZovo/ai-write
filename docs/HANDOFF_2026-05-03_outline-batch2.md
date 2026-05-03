# Handoff — 2026-05-03 `feat/outline-batch2` 交接

> **新窗口接手优先看这一份**。看完即可独立推进。
>
> 上一窗口（本会话）已把 outline-batch2 的 7 个 PR + 2 个 baseline + 2 个 docs 全部 push 到 `origin/feat/outline-batch2`，但**还没跑行为级 E2E 重测**，也**还没启动 Neo4j 扩展批次**。

---

## 0. 速览

```
仓库：/root/ai-write
分支：feat/outline-batch2
HEAD：38c4413  (本批末点 + 本交接文档本身，已 push 到 origin)
main：1b52952  (PR #22)
本批新增 commit 数：11（2 baseline + 7 PR + 2 docs）
GitHub：https://github.com/KingzZovo/ai-write
Token： /tmp/king_tok   (sub:king, exp:1778335635 — 2026 年未到期)
项目路径：/root/ai-write
Python： backend/ （sqlalchemy + asyncpg + neo4j-driver）
Frontend：frontend/（Next.js + React + Tailwind）
```

---

## 1. 本批交付物（8 commit）

| commit | PR | 主题 | 需重跑验证 |
|---|---|---|---|
| `bf1ae1e` | baseline backend | 抢救 PR-OL1~9 backend 工作区改动（12 文件 +942/-16） | 走 E2E 验证 |
| `de6d623` | baseline frontend | 抢救 fallback 卡片 / cascade UI / i18n（8 文件 +989/-599） | 手动 UI 烟测 |
| `70706c9` | **PR-OL10** | 字数→章数→卷数自动推算（4000 字/章 × 100-200 章/卷）+ prompt 硬约束 | E2E 验证全书大纲说 「3-5 卷」 |
| `e838cd6` | **PR-OL11** | 分卷 chapter_summaries 强化（60-100 字 + main_progress / side_progress / foreshadow_state / key_scene） + `extract_chapter_breakdown()` helper | E2E 检查卷大纲 JSON |
| `4b515ba` | **PR-OL12** | 章节大纲调用层补 `previous_chapter_summary` + 本章预规划注入 | 看 backend log 有没有「本章在分卷大纲里的预规划」 |
| `3d07194` | **PR-OL13** | 章节大纲生成后解析 `title` 回写 `Chapter.title` | DB 查 chapter.title 不是「第 N 章」 |
| `f6fa9e5` | **PR-OL14** | OutlineTree 三层查看入口（全书/分卷/章节大纲） | 前端手动烟测 |
| `919abab` | **PR-AI1** | 命名与词汇硬约束（`FORBIDDEN_HALLUCINATION_TERMS` + `NAMING_DIRECTIVE` + context_pack 注入） | grep 生成文 没有「怎表」「屃门」 |
| `f3e9e55` | **PR-STY1** | style v9 5 条 directives + context_pack 注入 | 人工读一章看节奏 / 外加朱雀检测 |
| `6e0715d` | docs(progress+handoff) | PROGRESS / HANDOFF_TODO 同步 | 无 |

**验证状态**：
- 全部 backend 改动过 `python3 -m py_compile`
- 全部 frontend 改动过 `cd frontend && npx tsc --noEmit -p tsconfig.json` × 0 errors
- **行为级 E2E 未跑**。上轮未修复前的朱雀 baseline：V1 CH2 护股12.04% 人工 / 42.21% 疑似 / 45.75% AI

---

## 2. 上一个窗口在做但未完成的（需新窗口接）

### 任务 A：30 章 E2E 重跑 + 朱雀复测（仅质量验证，未启动）

**背景**：King 下令「7 PR 全做完后重跑」。在于验证 PR-OL10 ​～ PR-STY1 联动下生成质量提升。

**实验设计**：推荐新建项目以隔离变量，避免旧 PID `310c1f9a` 带有 baseline 生成的脑补。如果为省 LLM 调用，也可复用旧 PID 但必须先清数据。

**完整走位详细步骤**：看下面第 4 节 「物料与句柄」。

### 任务 B：Neo4j 状态机扩展批次 PR-NEO1~NEO4（未启动）

**背景**：King 确认现有 Neo4j schema 只实现了「地点」一维，「阵营」只有静态隶属不含事件，「道具/时间」都未实现。同意开新分支 `feat/neo4j-batch1` 走。

**4 个 PR 设计**：看下面第 5 节 「Neo4j 批次设计」。

---

## 3. 快速交接：新窗口第一件事

```bash
cd /root/ai-write
git fetch --all
git checkout feat/outline-batch2
git pull
git log --oneline 1b52952..HEAD     # 看本批 10 个 commit

# 验证环境还能跑
source /tmp/king_tok 2>/dev/null || true   # 如果 token 过期要重新开 JWT
curl -sS http://127.0.0.1:8000/api/health  # 后端心跳
```

选路：
- 要走任务 A→ 跳第 4 节
- 要走任务 B→ 跳第 5 节
- 两个都要走→ 先 A 验证本批改动质量，再 B。

---

## 4. 任务 A 详述：30 章 E2E 重跑 + 朱雀复测

### 4.1 选择 PID

**选项 1（推荐）**：新建项目，隔离变量

```bash
TOKEN=$(cat /tmp/king_tok)
curl -sS -X POST http://127.0.0.1:8000/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "狩人账2号（outline-batch2 验证）",
    "genre": "东方奇幻",
    "premise": "...同 baseline 的 premise查 PID 310c1f9a project_json...",
    "target_word_count": 2000000,
    "settings_json": {
      "target_chapter_words": 4000,
      "chapters_per_volume_min": 100,
      "chapters_per_volume_max": 200
    }
  }' | tee /tmp/e2e_v2_pid.json
NEW_PID=$(jq -r .id /tmp/e2e_v2_pid.json)
echo $NEW_PID > /tmp/e2e_v2_pid.txt
```

> **重要 quirk：**历史上 target_word_count POST 2000000 返回可能被改为 3000000。验证返回体 `target_word_count` 字段，必要时 PUT 修正。

**选项 2**：复用旧 PID `310c1f9a-3cb9-4516-b9e0-c0233cc2a648`（《狩人账》），必须先清数据：

```sql
-- 在后端 PG 连接上跑
DELETE FROM chapter_evaluations  WHERE chapter_id IN (SELECT id FROM chapters WHERE volume_id IN (SELECT id FROM volumes WHERE project_id='310c1f9a-3cb9-4516-b9e0-c0233cc2a648'));
DELETE FROM chapter_versions     WHERE chapter_id IN (SELECT id FROM chapters WHERE volume_id IN (SELECT id FROM volumes WHERE project_id='310c1f9a-3cb9-4516-b9e0-c0233cc2a648'));
DELETE FROM chapters             WHERE volume_id  IN (SELECT id FROM volumes WHERE project_id='310c1f9a-3cb9-4516-b9e0-c0233cc2a648');
DELETE FROM outlines             WHERE project_id='310c1f9a-3cb9-4516-b9e0-c0233cc2a648' AND level IN ('chapter','volume');  -- 保留 book outline
-- （可选）如果要从头走连 book outline 也一起删
DELETE FROM volumes              WHERE project_id='310c1f9a-3cb9-4516-b9e0-c0233cc2a648';
```

### 4.2 走 outline 三层

```bash
PID=$(cat /tmp/e2e_v2_pid.txt)
TOKEN=$(cat /tmp/king_tok)

# 1) book 大纲（staged_stream=1，SSE，需 setsid nohup）
setsid nohup curl -sN --max-time 900 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @<(jq -c -n --arg pid $PID '{level:"book",project_id:$pid,user_input:"走 PR-OL10 只说「200 万字」的默认 scale"}') \
  "http://127.0.0.1:8000/api/generate/outline?staged_stream=1" \
  > /tmp/e2e_v2_book.sse 2>&1 < /dev/null & disown

# 等完后
awk '/^data: /{sub("^data: ","");print}' /tmp/e2e_v2_book.sse | tail -20
# 拿 outline_id
BOOK_OL=$(jq -r 'select(.status=="saved")|.outline_id' /tmp/e2e_v2_book.sse)
echo $BOOK_OL > /tmp/e2e_v2_book_oid.txt

# **验证 PR-OL10**：下载该 outline，查「七、分卷规划」该说「输出 3-5 卷」不是「2-8 卷」
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/projects/$PID/outlines/$BOOK_OL" | jq -r '.content_json.raw_text' | head -200

# 2) 生成 N 个卷的 volume outline（例：调 compute_scale 后是 3 卷就 1..3）
# 如果 PR-OL11 起作用会看到 chapter_summaries.main_progress 等字段
for V in 1 2 3; do
  setsid nohup curl -sN --max-time 900 \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data-binary @<(jq -c -n --arg pid $PID --arg pol $BOOK_OL --argjson v $V '{level:"volume",project_id:$pid,parent_outline_id:$pol,volume_idx:$v}') \
    "http://127.0.0.1:8000/api/generate/outline?staged_stream=1" \
    > /tmp/e2e_v2_vol${V}.sse 2>&1 < /dev/null & disown
done
wait
# confirm 每个卷并建 Chapter （历史 quirk：PR-OL2 confirm 路径似乎不建 Volume，手动建一下）
# 看 /tmp/e2e_volumes_map.json /tmp/build_20_vols.py 的历史脚本作参考

# 3) 生成每章的 chapter outline（PR-OL12 会该传 prev_summary + 本章预规划）
# 可参考历史 /tmp/run_30_chapter_outlines.sh

# 4) **验证 PR-OL13**：查 chapter.title 不是 「第 N 章」
PGPASSWORD=... psql -h ... -U ... -d aiwrite -c "SELECT chapter_idx, title FROM chapters WHERE volume_id IN (SELECT id FROM volumes WHERE project_id='$PID') ORDER BY chapter_idx LIMIT 30;"
```

### 4.3 生成正文 30 章

续用历史脚本模型（位置：/tmp/run_30_chapter_text.sh）或重写为新 PID：

```bash
# 4-worker 并发 · 27 min 完 30 章是上轮 baseline 数据
for C in $(seq 1 30); do
  # 查 chapter_id，调 /api/generate/chapter SSE，看 /tmp/build_book_body.py 里的调用体
  ...
done
```

### 4.4 朱雀复测

- 贴 V1 CH2 的正文到 https://matrix.tencent.com/ai-detect
- 记录 人工%/疑似%/AI% 三段比例
- 与 baseline `12.04% / 42.21% / 45.75%` 对比
- 期望：**AI% 明显下降**（PR-AI1 防幻觉词 + PR-STY1 节奏留白阶決升人工%）

### 4.5 验证检查单

- [ ] 全书大纲 「七、分卷规划」 含「必须输出 N 卷」硬约束句 → PR-OL10 起效
- [ ] volume_outline.chapter_summaries[i] 含 main_progress / side_progress / foreshadow_state / key_scene → PR-OL11 起效
- [ ] backend log 看到 「本章在分卷大纲里的预规划」 hint 进 prompt → PR-OL12 起效
- [ ] DB chapters.title 不是 「第 N 章」纯占位 → PR-OL13 起效
- [ ] 前端 OutlineTree 顶部 「全书大纲」 可展开 · 章节行 「▶大纲」 可展开 → PR-OL14 起效
- [ ] grep 生成正文 无 「怎表」「屃门」「黄铜怎表」 → PR-AI1 起效
- [ ] 人读一章 节奏交错、段落长短不均、200-300 字推一个 beat → PR-STY1 起效
- [ ] 朱雀 AI% < 45.75% baseline → 本批联动质量提升证据

---

## 5. 任务 B 详述：Neo4j 批次设计 PR-NEO1~NEO4

### 5.1 背景

现状未实现的 3 维（道具 / 阵营事件 / 时间）都要同时动下面几处：

- `backend/app/services/entity_timeline.py` — ENTITY_EXTRACTION_PROMPT + initialize_graph + 新增 _set_X_event() 记录函数
- `backend/app/tasks/entity_tasks.py` — 抽取流程启动后的 PG 同步（添加 items / faction_events / time_events 表同步）
- `backend/app/models/project.py` — 新增 Item / FactionEvent / TimeEvent SQLAlchemy model
- `backend/migrations/` — alembic 迁移
- `backend/app/services/context_pack.py` — 新增「当前道具持有」「阵营态势」「时间轴」三小节到 to_prompt
- `backend/app/services/critic_service.py` — 下一章 critic 查道具不一致
- `backend/app/services/checkers/item_missing.py` · `time_reversal.py` · `geo_jump.py` — 现在粉饰，可以接入真数据

### 5.2 PR-NEO1 道具

**Schema**：
- `Item { project_id, name, kind, owner_history }` 节点
- `(:Character)-[:HAS_ITEM { chapter_start, chapter_end? }]->(:Item)` 关系
- `(:Character)-[:USES_ITEM { chapter, scene_id? }]->(:Item)` 关系
- `(:Character)-[:TRANSFER_ITEM { chapter, to_character }]->(:Item)` 关系
- PG 表 `items` 镜像（供前端读取 item card）

**Prompt 改造**（`entity_timeline.py` ENTITY_EXTRACTION_PROMPT）增加：

```json
"items": [
  {"name": "道具名", "kind": "法器/兵器/信物/医药", "first_owner": "首次持有者"}
],
"item_transfers": [
  {"item": "道具名", "from": "原持有者", "to": "新持有者", "reason": "赠送/夺取/遗失..."}
]
```

**验证**：生成 1 章有道具交接的场景后，neo4j 查 `MATCH (c:Character)-[r:HAS_ITEM]->(i:Item) RETURN c.name, i.name, r.chapter_start`。

### 5.3 PR-NEO2 阵营事件

**Schema**：
- `FactionEvent { project_id, kind ∈ {alliance, conflict, dissolve, treaty}, chapter, summary }` 节点
- `(:Organization)-[:INVOLVED_IN]->(:FactionEvent)` 关系
- `(:Organization)-[:OPPOSED_BY { chapter_start, chapter_end? }]->(:Organization)` 关系

**Prompt 改造**：增加 `faction_events` 字段，抽取每章中发生的阵营状态变化（结盟/破盟/开战/休战）。

### 5.4 PR-NEO3 时间

**Schema**：
- `Time { project_id, label, kind ∈ {era, festival, anniversary, day_offset}, abs_value? }` 节点
- `(:Chapter)-[:OCCURS_AT { precision }]->(:Time)` 关系

**Prompt 改造**：增加 `time_events` 字段：

```json
"time_events": [
  {"label": "上元节", "kind": "festival"},
  {"label": "三日后", "kind": "day_offset", "offset": 3, "anchor": "上一事件"}
]
```

### 5.5 PR-NEO4 context_pack / critic 消费

- ContextPack 新增 `current_items: list[ItemCard]` · `faction_state: dict` · `timeline_anchors_v2: list[TimeAnchor]`
- to_prompt 新增『当前场景信息』块描述人物手中道具、阵营关系、时间坐标
- critic_service 加「道具不一致」「阵营状态回退」「时间倒退」三个检查

### 5.6 依赖 / 顺序

NEO1 / NEO2 / NEO3 可以并行。NEO4 必须在后。建议各占 1 个 commit，送一起 PR。

### 5.7 schema 迁移谨慎

Neo4j 是 IF NOT EXISTS 的约束 / 索引，安全。PG 迁移需一个 alembic revision + downgrade，参考现有 `backend/migrations/versions/` 样本。

---

## 6. 关键文件 / ID / endpoint 参考

### 项目 / outline

```
旧 PID： 310c1f9a-3cb9-4516-b9e0-c0233cc2a648  《狩人账》
  book outline:   182823a7-ccbb-445d-b0d1-0744b1d4bc8f
  vol1:  vol_id 0a23f8e2-...   outline_id a508c11a-...
  vol2:  vol_id b2991897-...   outline_id 25593b7e-...
  vol20: vol_id dab90caf-...   outline_id d7969db2-...
  V1 CH1​~9 已生成（8550~10572 字）

废弃 PIDs：
  0eaeff87-...   彼本 30ch
  5b944136-...   3 卷 30 章版本
```

### LLM endpoints / 风格 ID

```
Qwen extraction:  dfd26325-...
outlines/generation: ac6eb9cd-...
OpenAI 兼容 base: http://141.148.185.96:8317/v1

风格龙族 v8: 36fa0610-...
风格江南:    d39058bb-...
参考书龙族:  24498b6b-...   2.06M 字
参考书天之炽: 67fe33f9-... 718k 字
```

### Endpoints 快表

```
POST /api/projects                                 创建项目
POST /api/generate/outline?staged_stream=1         outline SSE，level=book/volume/chapter
POST /api/generate/chapter                         正文 SSE
GET  /api/projects/{pid}/outlines                  列三层大纲
GET  /api/projects/{pid}/outlines/{oid}            单一大纲
GET  /api/projects/{pid}/chapters?lightweight=true 列章节不拉正文
POST /api/projects/{pid}/outlines/{oid}/confirm    确认 outline + 建表卷
POST /api/projects/{pid}/outlines/{oid}/extract-settings  抽设定到项目
```

### Postgres 核心表

```
projects   { id, title, target_word_count, settings_json, ... }
volumes    { id, project_id, volume_idx, title, target_word_count }
chapters   { id, volume_id, chapter_idx, title, outline_json, content_text, word_count, status, summary }
outlines   { id, project_id, level∈(book|volume|chapter), parent_id, content_json, is_confirmed }
characters { id, project_id, name, profile_json }
locations  { id, project_id, name }
world_rules{ id, project_id, category, text }
```

### Neo4j 现有节点 / 关系

```
节点：Character, Location, Organization, WorldRule, Foreshadow, CharacterState
关系：RELATES_TO, AT_LOCATION, MEMBER_OF, HAS_STATE
约束：(c:Character) PK (project_id,name)
        (l:Location)  PK (project_id,name)
        (o:Organization) PK (project_id,name)
        ()-[r:RELATES_TO]-() PK (project_id, source_name, target_name, type, chapter_start)
        ()-[r:AT_LOCATION]-() PK (project_id, character_name, chapter_start)
        ()-[r:MEMBER_OF]-()   PK (project_id, character_name, org_name, chapter_start)
```

---

## 7. 已知陷阱 / shell gotchas

- **中文 commit message** ⚠️ 不要 `git commit -m ".."` 会乱码，一律 `git commit -F /tmp/msg.txt`。
- **中文在 shell 参数** ⚠️ 先 `write_file` 存 JSON 到 /tmp，再 `curl --data-binary @file`。
- **MCP timeout 240s**：超过拆多个指令调用；SSE 长任务用 `setsid nohup curl -sN --max-time 900 ... > /tmp/log 2>&1 < /dev/null & disown`。
- **避免 `pkill -f curl`** 会误杀则使用后台运行的 curl。杀任务用 PID 单独 `kill -9 <pid>`。
- **staged_stream query param** 用 `1` / `0`，不用 `true` / `false`（后端记 0/1）。
- **target_word_count quirk**：POST 2000000 有时返 3000000。返回后验证 + PUT 修正。
- **chapter word_count** 以 DB 为准，SSE 中途累加不准。
- **f-string 双花括号 bug**：`f"x"` 会变 `{x}`，要输出字面别用 f-string 拼。
- **PR-OL2 confirm 不建 Volume**：historical bug，`/confirm` 调用后 `/volumes` 还是 total=0，需手动 POST `/api/projects/{pid}/volumes` 建 Volume 后再指定 parent_outline_id 生成各卷 outline。历史脚本参考：/tmp/build_20_vols.py。
- **服务启动**：请核实 backend 服务运行（8000 端口）。启动脚本看仓库 README + scripts/。

---

## 8. 历史临时文件 / 脚本参考

```
/tmp/king_tok                          JWT
/tmp/e2e_pid_new   /tmp/e2e_book2_oid
/tmp/e2e_volumes_map.json              卷映射 (vol_idx → vol_id, outline_id)
/tmp/e2e_vol_outline_map.json
/tmp/e2e_chapters_map.json   /tmp/e2e_chapter_seq.txt
/tmp/build_20_vols.py        /tmp/build_30_chapters.py   /tmp/build_book_body.py
/tmp/run_20_vol_outlines.sh  /tmp/run_30_chapter_outlines.sh   /tmp/run_30_chapter_text.sh
/tmp/e2e_*_master.log
/tmp/apply_pr_ol10.py / apply_pr_ol11.py / apply_pr_ol12.py / apply_pr_ol13.py
/tmp/apply_pr_ol14.py / fix_pr_ol14.py
/tmp/apply_pr_ai1.py / apply_pr_sty1.py
/tmp/commit_msg_ol10.txt ... commit_msg_sty1.txt
```

---

## 9. 本批 PR 变动点快查

### Backend

- `backend/app/services/outline_generator.py`：PR-OL10 `compute_scale()` + `_format_scale_directive()` + `OutlineGenerator._apply_scale_to_prompt()`；PR-OL11 VOLUME_META_SYSTEM / VOLUME_CHAPTERS_SYSTEM 强化 + `extract_chapter_breakdown()` helper。
- `backend/app/api/generate.py`：PR-OL10 开头 `compute_scale` 调用 + book outline 调用传 scale；PR-OL12 chapter pre-fetch 补 `previous_chapter_summary` + `chapter_breakdown_entry`，chapter 调用传二者；PR-OL13 auto-save 后解析 `title` 并回写 Chapter.title。
- `backend/app/services/checkers/anti_ai_checker.py`：PR-AI1 `FORBIDDEN_HALLUCINATION_TERMS` + `SUSPICIOUS_COMPOUND_PATTERN` + `NAMING_DIRECTIVE`；PR-STY1 `STYLE_V9_DIRECTIVES`。
- `backend/app/services/context_pack.py`：PR-AI1 注入 NAMING_DIRECTIVE；PR-STY1 注入 STYLE_V9_DIRECTIVES。

### Frontend

- `frontend/src/components/outline/OutlineTree.tsx`：PR-OL14 顶部「全书大纲」 toggle + 章节「▶大纲」 toggle + chapter row React.Fragment。
- `frontend/src/components/workspace/DesktopWorkspace.tsx`：PR-OL14 `bookOutlineData` state + 传递给 OutlineTree。

### Docs

- `docs/PROGRESS.md` / `docs/HANDOFF_TODO.md`：6e0715d 同步本批变更。
- **本文件**：`docs/HANDOFF_2026-05-03_outline-batch2.md`

---

## 10. 合并路径建议

本分支 `feat/outline-batch2` 建议在任务 A 验证质量合格后开 PR 合 main：

```
PR 标题：feat: outline-batch2 — PR-OL10..STY1 七项联动 + baseline 抢救
PR 描述：4 段式（Context / Change / Verification / Docs updated）
Verification 附：朱雀复测三段比例截图 + V1 CH2 样例 800 字
Docs updated：docs/PROGRESS.md · docs/HANDOFF_TODO.md · docs/HANDOFF_2026-05-03_outline-batch2.md
```

---

## 11. EOL

本文件为上一窗口完整交接。新窗口读完可独立推进任务 A 或 B。遇到重大决策点才问用户。
