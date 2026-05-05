# HANDOFF 2026-05-05 - PR-BOOK-PROFILE-BIND (未开始)

> 上一个窗口在 PR-NO-RAW-INJECT push 后起手赤心全流程验证，发现“style_profile 写错位置”是设计缺陷。用户要求先完成 PR-BOOK-PROFILE-BIND 再跑全流程验证。收尾阶段 MCP 频繁超时，DB migration 未落地，所以这个 PR 完全未开始。

## 1. 速览

- 仓库：/root/ai-write，origin = https://github.com/KingzZovo/ai-write.git
- 分支：main (与 feat/phase2-fix 同步)
- HEAD：`a136960` PR-NO-RAW-INJECT 已 push
- 部署：http://127.0.0.1:8080 (king / Wt991125)
- 凭据文件：/root/ai-write/.env (POSTGRES_PASSWORD)
- 本窗口未完成任务：PR-BOOK-PROFILE-BIND 全部

## 2. 本批交付物 (上个窗口 + 本窗口)

| sha | PR | 主题 | 验证状态 |
|---|---|---|---|
| a136960 | PR-NO-RAW-INJECT | 移除 ContextPack 三处原文注入 | 源码变更 + py_compile OK + commit已推 |
| be1d414 | PR-NVIDIA-MULTIKEY | NVIDIA endpoint 第二密钥 + round-robin 并发 | 2 keys 轮询实测，并发 8x embed 0.82s |
| f7f6915 | PR-VECTORIZE-PASSAGES | (被后续 PR 部分回滚，scene_samples 路径以及备) | 原状 |

本窗口还跑了赤心全流验证的 stage A2/A3/A4（全部后台 nohup）：
- A2 style_v9 完成：赤心 profile (`b76da43a-a2fa-4fd3-8c54-3912acee6bb0`) rules=73 / samples=24
- A3 structure_v2 完成：赤心 reference_book.metadata_json.plot_structure_v2（4 卷 + 10 顶级 keys）
- A4 chapter_naming 完成：赤心 profile config_json.chapter_naming_style = 18 examples / 11 patterns / 7 条 principles

## 3. 接手第一件事 cmd 序列

```bash
cd /root/ai-write
git fetch --all --prune
git log --oneline -5
git status -s
git reflog | head -5

PW=$(grep -E "^POSTGRES_PASSWORD=" /root/ai-write/.env | cut -d= -f2-); export PGPASSWORD=$PW

# 验证上个窗口完成进度
psql -h 127.0.0.1 -U postgres -d aiwrite -c "SELECT id, name, source_book FROM style_profiles ORDER BY created_at"
psql -h 127.0.0.1 -U postgres -d aiwrite -c "SELECT column_name FROM information_schema.columns WHERE table_name='style_profiles' AND column_name LIKE 'source%'"
# 预期：只有 source_book 列。如果出现 source_book_id，说明上个窗口后期补完了，跳到 step 4。

# 验证赤心 reprocess 后台是否还在跑
psql -h 127.0.0.1 -U postgres -d aiwrite -c "SELECT (SELECT count(*) FROM reference_book_slices WHERE book_id='0a543b1d-19fe-4e03-986e-42844feb36ee') AS slices, (SELECT count(*) FROM style_profile_cards WHERE book_id='0a543b1d-19fe-4e03-986e-42844feb36ee') AS spcards"
# 上个窗口看到 ≈1000 / 998，增长则 celery worker 还在跑。

# 验证服务全部在线
docker ps --format "table .Names	.Status" | head
curl -s -o /dev/null -w "backend %{http_code}\n" http://127.0.0.1:8080/api/healthz
```

## 4. PR-BOOK-PROFILE-BIND 完整 cmd 序列 (主任务)

### Step 1. DB schema migration + 回填
```bash
PW=$(grep -E "^POSTGRES_PASSWORD=" /root/ai-write/.env | cut -d= -f2-); export PGPASSWORD=$PW
# 1.1 加列 + FK + 索引
psql -h 127.0.0.1 -U postgres -d aiwrite -c "ALTER TABLE style_profiles ADD COLUMN IF NOT EXISTS source_book_id UUID REFERENCES reference_books(id) ON DELETE SET NULL;"
psql -h 127.0.0.1 -U postgres -d aiwrite -c "CREATE INDEX IF NOT EXISTS idx_style_profiles_source_book_id ON style_profiles(source_book_id);"
# 1.2 回填现有 3 个 profile (江南多书聚合保留 NULL)
psql -h 127.0.0.1 -U postgres -d aiwrite -c "UPDATE style_profiles SET source_book_id='24498b6b-2698-4900-b44b-b42806964e1b' WHERE id='36fa0610-6df7-4e9a-aea9-6ea9ad1c9345'"
psql -h 127.0.0.1 -U postgres -d aiwrite -c "UPDATE style_profiles SET source_book_id='0a543b1d-19fe-4e03-986e-42844feb36ee' WHERE id='b76da43a-a2fa-4fd3-8c54-3912acee6bb0'"
# 1.3 为剩余 reference_books (天之炽×2) 创空白 profile
psql -h 127.0.0.1 -U postgres -d aiwrite -c "INSERT INTO style_profiles (id, name, source_book, source_book_id, rules_json, anti_ai_rules, tone_keywords, sample_passages, config_json, bind_level, is_active, created_at, updated_at) SELECT gen_random_uuid(), rb.title || ' 综合写法', rb.title, rb.id, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, 'book', 1, now(), now() FROM reference_books rb WHERE NOT EXISTS (SELECT 1 FROM style_profiles sp WHERE sp.source_book_id = rb.id)"
# 验证
psql -h 127.0.0.1 -U postgres -d aiwrite -c "SELECT sp.id, sp.name, COALESCE(rb.title, '(多书聚合)') AS book FROM style_profiles sp LEFT JOIN reference_books rb ON rb.id=sp.source_book_id ORDER BY sp.created_at"
```

### Step 2. Model: 加 source_book_id 列到 StyleProfile
文件 `backend/app/models/project.py` 第 286 行开始是 `class StyleProfile`，第 294 行 `source_book = Column(String(500))` 后面加：
```python
    source_book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="SET NULL"),
        nullable=True,
    )  # PR-BOOK-PROFILE-BIND: 1:1 binding to a reference book
```
如果顶部 import 里没 ForeignKey，加上 `from sqlalchemy import ..., ForeignKey`。

### Step 3. Service helper
新建 `backend/app/services/style_profile_resolver.py`:
```python
"""Resolve a reference book’s bound StyleProfile (PR-BOOK-PROFILE-BIND)."""
from __future__ import annotations
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.project import ReferenceBook, StyleProfile

async def get_or_create_book_profile(db: AsyncSession, book_id: str) -> StyleProfile:
    """Return the StyleProfile bound to a reference_book, creating an empty one if missing.
    Raises ValueError if the book itself doesn’t exist."""
    book = await db.get(ReferenceBook, UUID(str(book_id)))
    if book is None:
        raise ValueError(f"reference_book {book_id} not found")
    rs = await db.execute(
        select(StyleProfile).where(StyleProfile.source_book_id == book.id).limit(1)
    )
    sp = rs.scalar_one_or_none()
    if sp is not None:
        return sp
    sp = StyleProfile(
        name=f"{book.title} 综合写法",
        source_book=book.title,
        source_book_id=book.id,
        rules_json=[], anti_ai_rules=[], tone_keywords=[], sample_passages=[],
        config_json={}, bind_level="book", is_active=1,
    )
    db.add(sp)
    await db.commit()
    await db.refresh(sp)
    return sp
```

### Step 4. Hook 到上传与重处理
文件 `backend/app/tasks/knowledge_tasks.py` `_process_uploaded_book_async()` 末尾 `book.status = "ready"` 那一块后，`await db.commit()` 前/后加：
```python
            try:
                from app.services.style_profile_resolver import get_or_create_book_profile
                await get_or_create_book_profile(db, str(book.id))
            except Exception as e:
                logger.warning("auto profile binding failed for %s: %s", book.title, e)
```
同样补加到 `backend/app/services/reference_ingestor.py` 的 `reprocess_reference_book()` 末尾。

### Step 5. 脚本默认值修正
`scripts/reverse_fill_p2_upgrade.py`:
```python
# 删除 LONGZU_BOOK_ID / JIANGNAN_STYLE_ID 常量默认
import argparse
# main():
    ap.add_argument("--book", required=True)  # 仅传 book_id
    ap.add_argument("--style", default=None)  # 默认 None -> 自动查找绑定 profile
# main_async() 里 style_v9 分支使用前：
    if args.kind in ("style_v9", "all"):
        sty = args.style
        if not sty:
            from app.services.style_profile_resolver import get_or_create_book_profile
            async with async_session_factory() as db:
                sp = await get_or_create_book_profile(db, args.book)
                sty = str(sp.id)
        await upgrade_style_v9(args.book, sty)
```

`scripts/extract_chapter_naming_style.py`:
```python
# 删除 BOOK_IDS 硬编码
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--book", required=True)
ap.add_argument("--target-profile", default=None)
args = ap.parse_args()
# extract_chapter_samples(): book_ids = [args.book]
# main(): 如果 args.target_profile is None -> get_or_create_book_profile(args.book).id
```

### Step 6. (可选) API endpoint
`backend/app/api/decompile.py` 或新建 `backend/app/api/style_profiles.py` 加：
```
POST /api/reference-books/{book_id}/extract?kind=style|structure|naming|all
```
触发三个脚本作为 celery task。本轮可跳过。

### Step 7. 验证及烟测
```bash
cd /root/ai-write
python3 -c "import ast; ast.parse(open('backend/app/models/project.py').read()); ast.parse(open('backend/app/services/style_profile_resolver.py').read()); print('syntax OK')"
docker compose restart backend
sleep 6
curl -s -o /dev/null -w "backend %{http_code}\n" http://127.0.0.1:8080/api/healthz
# 烟测脚本：重跑赤心 chapter_naming 不传 --target-profile、看是否自动走赤心 profile
docker cp scripts/extract_chapter_naming_style.py ai-write-backend-1:/app/_chap_test.py
docker exec -d ai-write-backend-1 sh -c "cd /app && nohup python /app/_chap_test.py --book 0a543b1d-19fe-4e03-986e-42844feb36ee > /tmp/chap_test.log 2>&1 &"
sleep 60
docker exec ai-write-backend-1 tail -10 /tmp/chap_test.log
```

### Step 8. Commit + push
```bash
git add backend/app/models/project.py backend/app/services/style_profile_resolver.py backend/app/tasks/knowledge_tasks.py backend/app/services/reference_ingestor.py scripts/reverse_fill_p2_upgrade.py scripts/extract_chapter_naming_style.py
cat > /tmp/msg.txt <<'MSG'
PR-BOOK-PROFILE-BIND: 1:1 binding between reference_book and style_profile

- DB: style_profiles add source_book_id UUID FK to reference_books, with index
- Backfill existing rows by name match; create empty profile for any reference_book without one
- Model: StyleProfile.source_book_id new column
- Service: style_profile_resolver.get_or_create_book_profile(db, book_id)
- Hook: process_uploaded_book + reprocess_reference_book auto-create binding profile
- Scripts: reverse_fill_p2_upgrade.py / extract_chapter_naming_style.py default --style auto-resolve from --book; no more hardcoded JIANGNAN_STYLE_ID / LONGZU_BOOK_ID / multi-book BOOK_IDS

Verification: see docs/HANDOFF_2026-05-05_pr-book-profile-bind.md step 7

Docs updated: docs/HANDOFF_2026-05-05_pr-book-profile-bind.md, docs/PROGRESS.md, docs/HANDOFF_TODO.md, docs/HANDOFF_EXECUTION.md
MSG
git -c user.email=ai@local -c user.name=ai-write-agent commit -F /tmp/msg.txt
git push origin HEAD
git push origin HEAD:feat/phase2-fix
```

## 5. Stage B/C/D/E (PR-BOOK-PROFILE-BIND 后跑)

### Stage B - 创建赤心中篇玩玩项目 (~20 万字)
```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/auth/login -H 'Content-Type: application/json' -d '{"username":"king","password":"Wt991125"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
# create project bound to chixin profile + chixin reference_book
cat > /tmp/proj.json <<EOF
{"name":"赤心巡天仿写验证","genre":"玩玩","target_words":200000,"settings_json":{"style_reference":{"profile_id":"b76da43a-a2fa-4fd3-8c54-3912acee6bb0","reference_book_id":"0a543b1d-19fe-4e03-986e-42844feb36ee"}}}
EOF
curl -s -X POST http://127.0.0.1:8080/api/projects -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --data-binary @/tmp/proj.json | python3 -m json.tool
```
拿到 project_id 存下来。

### Stage C - 生成全书/分卷/章节大纲
调 `/api/outline/full` `/api/outline/volumes` `/api/outline/chapters/batch` 几个接口（具体路径查 backend/app/api/outline*.py）。验证点：
- chapter_naming_style 是否被注入 (看生成的章名是否出现赤心风格的“评语式整句/宣告裁决式/诗化意象句/典籍摘句化用”）
- plot_structure_v2 (底层逆袭+地图递开) 是否反映在全书大纲弧上
- foreshadows / character_relations / world_setting 三个结构化设定集是否被生成出来并被后续章节大纲调用

### Stage D - bulk_generate 10 章正文
调 `/api/chapters/bulk-generate` (看 backend/app/api/chapters.py)。验证点：
- 七个写作指南模块 (问号快照 / 二画面联动 / ...) 是否生效
- 73 条赤心 rules_json 是否被加载到 system prompt
- 不能出现任何赤心原文 passage 被注入 (PR-NO-RAW-INJECT 验证)
- chapter_naming_style 生效 — 看章名实际品质

### Stage E - 验证报告
MD 写到 `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`，按上面三个 stage 的验证点逐一打勾/错/部分。

## 6. 关键 ID / endpoint / schema 速查

- 赤心巡天 reference_book: `0a543b1d-19fe-4e03-986e-42844feb36ee`
- 赤心 style_profile: `b76da43a-a2fa-4fd3-8c54-3912acee6bb0` (rules=73 / chapter_naming_style 已入 / plot_structure_v2 在 reference_book.metadata_json)
- 江南 综合写法 style_profile (多书聚合): `d39058bb-a22c-4511-80f6-3649df8eca12`
- 龙族 v8 剂量画像 style_profile: `36fa0610-6df7-4e9a-aea9-6ea9ad1c9345`
- 龙族 reference_book: `24498b6b-2698-4900-b44b-b42806964e1b`
- 天之炽①: `67fe33f9-60e8-459e-9720-8546c828eab7`
- 天之炽②女武神: `c33c2f19-03b4-46a1-ab0a-ede66175a7fe`
- NVIDIA endpoint: `88e25ef0-7132-4008-86fc-c8e595b9e340` (api_key + api_key_2 都已填)
- decompile router prefix: `/api/reference-books` (不是 /api/decompile)
- model_config router prefix: `/api/model-config`, endpoints `/endpoints` GET/POST, `/endpoints/{id}` PUT/DELETE
- DB 表不存在：`style_samples_redacted` (Qdrant collection 存在，PG 表不存在 - 要查 redacted 请 Qdrant)
- text_chunks 没 status 列；reference_books 没 raw_text 列（在 text_chunks.content）

## 7. 已知陷阱 / shell gotcha

1. **MCP 单次工具调用 240s 超时**，本窗口尾段 MCP 连接频繁失败/超时，多次调用都未达。遇到十几次 timeout 继续调用才会召回。长任务必须 nohup 并轮询日志。
2. **中文 / Unicode 禁用 shell 内联**: git commit 一律 `-F /tmp/msg.txt`; curl --data 含中文一律先写文件。
3. **psql -c 中不能包含 `\d`** (psql meta-command)—会报语法错并回滚整个事务。如需 \d 拆为单独 psql -c 调用。
4. **psql -tA 输出会含 INSERT/UPDATE 后面的 row count 行**，使用变量接收时会多拿一行需手动取首行。
5. **/app/scripts 不在 backend container** - scripts/ 没 mount 到容器，所以脚本要 docker cp 进入 /app/_xxx.py 后运行。
6. **MCP run_command 超时 = task 可能已下发**：有时返 timeout 但 ALTER TABLE 已生效。要 information_schema 反查。
7. **PR-NVIDIA-MULTIKEY 去重逻辑**: 两个 key 如果填了同样的会被 dedupe 成单 key。要填真正不同的第二 key。
8. **flag_modified(prof, "config_json") 必须加**：JSONB 字段在 Python 侧 mutate 不会被 SQLAlchemy 检测到需要改。
9. **qdrant-client >=1.10**: `query_points(query=...)` ，不是 `search(query_vector=...)`。
10. **StyleProfile.sample_passages 字段名是 text、不是 passage**。

## 8. 历史临时文件 / 脚本清单

backend container 内 (必要时重新 docker cp):
- `/app/_reverse_fill_p2_upgrade.py` (本次使用过)
- `/app/_extract_chapter_naming_style.py` (本次使用过)
- `/app/_chap_chixin.py` (赤心专属，BOOK_IDS 只赤心)
后台 nohup 进程日志：
- `/tmp/chixin_style_v9.log` (完成)
- `/tmp/chixin_structure_v2.log` (完成)
- `/tmp/chap_chixin.log` (完成)
- `/tmp/fbuild.log` (上个窗口 frontend build 日志)

host 临时文件：
- `/tmp/chap_chixin_only.py` (已 docker cp 进容器)

上个窗口未提交可舍弃，本窗口后期已在 git checkout / rm 清理。

## 9. 本批文件改动点速查

本批仅 commit 了 PR-NO-RAW-INJECT (a136960):
- backend/app/services/context_pack.py〉3处原文注入路径被移除 (lines 1269-1276 / 1278-1323 / 1431-1441)
- Qdrant 集合 `style_samples_by_scene` 被删 (给 PR-VECTORIZE-PASSAGES 重构)

上一 commit be1d414 (PR-NVIDIA-MULTIKEY):
- backend/app/models/project.py LLMEndpoint 加 api_key_2
- backend/app/api/model_config.py EndpointCreate/Update/Response 都加 api_key_2
- backend/app/services/model_router.py NvidiaEmbeddingProvider 支持 api_keys list + round-robin
- frontend/src/app/settings/page.tsx 其 nvidia provider 多加 api_key_2 输入

## 10. 本 PR (PR-BOOK-PROFILE-BIND) 提交 4 段模板

### Context
为什么改：现有 extract_chapter_naming_style.py / reverse_fill_p2_upgrade.py 都把 TARGET_PROFILE_ID 硬编码为江南 ID。多本书联合学习后会全部写进江南 profile，赤心 有自己的 profile 也会被覆盖。style_profiles 表本身与 reference_books 没有 FK 绑定，所以脚本默认值一错不会被任何约束拦住。

### Change
- DB schema: style_profiles.source_book_id UUID FK -> reference_books(id) ON DELETE SET NULL
- 回填 3 个现有 profile；为剩余 reference_book 创空白 profile
- Model: StyleProfile.source_book_id
- Service: style_profile_resolver.get_or_create_book_profile
- Hook: process_uploaded_book / reprocess_reference_book 末尾 auto-bind
- Scripts: --book 驱动 ：默认 --style/--target-profile auto-resolve

### Verification
- syntax: `python3 -c "import ast; ast.parse(open('backend/app/models/project.py').read())"`
- DB: `SELECT sp.id, sp.name, COALESCE(rb.title, '(多书)') FROM style_profiles sp LEFT JOIN reference_books rb ON rb.id=sp.source_book_id`
- 烟测: 重跑 chapter_naming 不传 --target-profile、看是否自动写赤心 profile
- E2E: 合进 stage B/C/D/E 赤心验证

### Docs updated
- docs/HANDOFF_2026-05-05_pr-book-profile-bind.md (本文件)
- docs/PROGRESS.md (顶部 banner)
- docs/HANDOFF_TODO.md (顶部 banner)
- docs/HANDOFF_EXECUTION.md (顶部 banner)

## 11. EOL
