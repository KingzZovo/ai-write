> ⚠️ **2026-05-02 后状态** — 本文档原为 PR #6 后续决策辅助文档，记载 F1、F2、F3 三项决策。F1=A 与 F2 均已落地，F3 仅剩真实环境对账未跑。
>
> | follow-up | 决策 | 状态 | 证据 |
> |---|---|---|---|
> | F1 foreshadows | A（纳入 Neo4j） | ✅ 已落地 | PR #18 commits `02a5f19 / cd8ed7a / e16c492 / a86738c / 49776e0` |
> | F2 v1.10 路由族 | 提前落地 | ✅ 已落地 | PR #18 入仓 `neo4j_settings.py / admin_entities.py / admin_usage.py` |
> | F3 真实环境对账 | 依赖环境 | ⏳ 待跑 | 需真实 PROJECT_ID + 后端启动后跑 `verify_entity_writeback_v19.sh` |
>
> 新窗口接手请优先看 `docs/PROGRESS.md`。以下原文作为历史上下文保留。

---

# Follow-up Plan（PR #6 后续决策辅助文档）

> 目的：把 PR #6 Follow-up 段里点名的三项待决策事项写成**具体可执行的 diff 计划**，让用户可以一眼对比选项。本文档**不**代表架构决策，仅为决策辅助。

## 0. 背景快照

- PR #6 (`chore/runbook-and-handoff-sync`, HEAD `e38fa32`) 已交付：RUNBOOK + verify_v19 脚本 + P4 README/settings 410 message 修正 + 4 份 docs 同步。
- main HEAD = `17fb371`（PR #5 合并后）。
- 三项 follow-up：
  - **F1**：foreshadows P2 路线决策（选项 A vs 选项 B）
  - **F2**：v1.10 路由族重新引入（cherry-pick `dc98363` + `08b0494`）
  - **F3**：P3 part 3 真实环境对账脚本跑出业务结果

## 1. F1：foreshadows P2 路线（选项 A vs 选项 B）

### 1.1 当前 main 上的 PG 直写点（3 处）

```
backend/app/api/foreshadows.py:111
  POST /api/projects/{pid}/foreshadows
  foreshadow = Foreshadow(...) ; db.add(foreshadow)

backend/app/api/foreshadows.py:179
  DELETE /api/projects/{pid}/foreshadows/{id}
  await db.delete(foreshadow)

backend/app/services/foreshadow_manager.py:84
  ForeshadowManager.create(...)
  foreshadow = Foreshadow(...) ; db.add(foreshadow)
  被章节生成 / 大纲提取链路调用
```

这是在「`world_rules` / `relationships` / `locations` / `character_states` 都已全部走 Neo4j-真相源 + materialize 投影」后，仓库里唯一还直接 PG 写的设定集实体。

### 1.2 选项 A：纳入 Neo4j 真相源链路

**依赖**：必须先完成 F2（v1.10 重新引入 `/neo4j-settings/*` 路由族）。

**代码变动范围**：

1. **新增 Neo4j 节点与关系模型**（`backend/app/db/neo4j.py` 或新 schema 文件）：
   - `(:Foreshadow {id, project_id, type, description, planted_chapter, status, narrative_proximity, resolve_conditions_json, resolution_blueprint_json, created_at})` 节点
   - `(:Foreshadow)-[:PLANTED_AT]->(:Chapter)` / `(:Foreshadow)-[:RESOLVES_AT]->(:Chapter)` 可选
2. **`backend/app/api/neo4j_settings.py`**（v1.10 cherry-pick 后）新增子路由：
   - `POST /api/projects/{pid}/neo4j-settings/foreshadows` —— upsert Foreshadow 节点后调 `_materialize_entities_to_postgres` 投影回 PG `foreshadows` 表
   - `DELETE .../foreshadows/{id}` —— 删 Neo4j 节点 + 同步删 PG 行
3. **改 `backend/app/tasks/entity_tasks.py:_materialize_entities_to_postgres`**：增加从 Neo4j 读 `Foreshadow` 节点 → upsert `foreshadows` 表的分支。
4. **`backend/app/api/foreshadows.py`**：把 3 个写端改为 410 + 引导 `/neo4j-settings/foreshadows`；GET 保留（读 PG 投影仍有效）。
5. **`backend/app/services/foreshadow_manager.py:create`**：改为调内部 Neo4j upsert 函数（与实体生成接口复用同一个底层写函数），不再调 `db.add`。
6. **补 alembic 迁移**（如果 PG `foreshadows` 表 schema 需动）。
7. **补回归测试**：在 `backend/tests/` 增加「写 `/neo4j-settings/foreshadows` 后 PG `foreshadows` 上出现同一行」 + 「旧 `/foreshadows` POST 返 410 」两个测试。
8. **补 RUNBOOK §2.x 写路径**、**§3 anti-pattern**、**§4.4 验收**、**`scripts/verify_entity_writeback_v19.sh`** 多补一个 `foreshadows` 路径分支。
9. **同步改 README §设定集数据源约定**：把 `foreshadows` 加进列表（不再是例外）。

**估算变更量**：新文件 1～2 个，修改 5～7 个，迁移 0～1 个，测试 +2 个。中等以上。

**优点**：与 `world_rules / relationships / locations` 架构一致，PG drift 风险为 0；RUNBOOK §3 可以去掉「foreshadows 是例外」说明。

**缺点**：需先推 v1.10；设定集生成链路要一起改；训练 / 集成测试都要同步调整。

### 1.3 选项 B：显式声明不入 Neo4j 真相源链路

**依赖**：无。可独立合并。

**代码变动范围**：

1. **`docs/RUNBOOK.md` §3**：从「待决策」改为「明确例外」，补上决策理由（伏笔是动态收敛实体，与 world\_rules 等静态设定语义不同，resolve\_conditions\_json / status 变迁频繁，放 PG 单表更适合「边写边读」场景）。
2. **`backend/app/api/foreshadows.py` 顶部 docstring**：补一句「本模块是设定集体系中唯一不过 Neo4j 真相源的实体」。
3. **`backend/app/services/foreshadow_manager.py:create`**：同样补注释，并明确调用点为「intentional, see RUNBOOK §3」。
4. **新增回归测试** `backend/tests/test_foreshadows_pg_only.py`：
   - 创建伏笔 → PG `foreshadows` 上有行 → Neo4j 查不到 `(:Foreshadow)` 节点（反向验证不误入 Neo4j）
   - 删除伏笔 → PG 列不到、Neo4j 仍查不到
5. **`scripts/verify_entity_writeback_v19.sh`**：额外补一个 `OK: foreshadows 仅 PG，Neo4j 为空` 的反向烟测段。
6. **`README.md` §设定集数据源约定**：加一句「例外：foreshadows 仍为 PG 单表」。

**估算变更量**：修改 4～5 个文件，测试 +1 个。轻量。

**优点**：收发简单，不依赖 v1.10；明确「有意设计」防止后来人误以为是遗漏。

**缺点**：架构说明中仍有一个例外项；未来要跨实体联合查询（如「某角色的伏笔」）只能靠 PG。

### 1.4 推荐顺序

- 如果未来 6 个月内要上 v1.10 路由族：先选 B 是中性实用的，后续 v1.10 落地后再独立评估是否改 A。
- 如果 v1.10 本来就要看 A：A / B 二选一看呢对「伏笔查询跨实体联合」的需求强不强。

## 2. F2：v1.10 路由族重新引入

### 2.1 上下文

- README v1.9+ 文档化了 `/api/projects/{pid}/neo4j-settings/*` 与 `/api/admin/entities/materialize`。
- 两个路由族只在不在 main 的中间提交里出现过，后被 `feature/v1.0-big-bang` 重构删除。
- PR #6 已把它们明确标为「v1.10 计划」，本项是实现双方。

### 2.2 cherry-pick 范围

```
dc98363 feat(v1.9): add neo4j settings write API + materialize projection
  backend/app/api/neo4j_settings.py | 164 ++  (新)
  backend/app/main.py                |   3 +- (注册 router)

08b0494 feat(v1.9): add admin materialize endpoint (backend-visible metric)
  backend/app/api/admin_entities.py | 66 ++   (新)
  backend/app/main.py                |   3 +- (注册 router)
  docs/RUNBOOK.md                    | 37 ++  (与本 PR §1/§3 可能冲突)
```

**预期冲突**：`docs/RUNBOOK.md` 肯定冲突。需要手工合并 §1.x / §3 段。`backend/app/main.py` 重复注册路由只要不在同一行上一般不会冲突；可能需补一下 import 顺序。

### 2.3 预计步骤

1. 在 main 起新分支 `feature/v1.10-neo4j-settings`。
2. `git cherry-pick dc98363 08b0494`，解决 RUNBOOK 冲突。
3. 验证 `backend/app/main.py` 里 `app.include_router(neo4j_settings.router)` 与 `app.include_router(admin_entities.router)` 都在。
4. 跑 `python3 -m compileall -q backend/app` + 拉起 `localhost:8000` 看 OpenAPI 有没有出现 `/api/projects/{project_id}/neo4j-settings/*` 与 `/api/admin/entities/materialize`。
5. 补测试：至少 `tests/test_neo4j_settings_smoke.py` + `tests/test_admin_entities_smoke.py`（200 / 202 烟测，mock Neo4j driver）。
6. 改 `scripts/verify_entity_writeback_v19.sh`：加一个 `/admin/entities/materialize` POST 路径（需 admin token），加一个 `/neo4j-settings/world-rules` 烟测。
7. 同步改 `README.md` §设定集数据源约定：把「v1.10 计划」那三行提升为「当前可用」。
8. 同步改 `backend/app/api/settings.py` 410 message + RUNBOOK §2.x：增加 `/neo4j-settings/*` 作为推荐写入口。
9. 依赖 F1 选项 A 的话：在本分支叠加 F1-A 的变动 → 独立 commit。
10. 开 PR base=main，head=feature/v1.10-neo4j-settings。

### 2.4 风险与门控

- `admin_entities.py` 需要 `ADMIN_USERNAMES` 环境变量 + `_require_admin` 函数存在于 main。需验证 `app.api.admin_usage:_require_admin` 在 main 上可用。
- `neo4j_settings.py` 依赖 `app.db.neo4j.get_neo4j` + `app.tasks.entity_tasks._materialize_entities_to_postgres`。需验证这两个模块与保持 v1.9 同款签名（本地 grep `app/tasks/entity_tasks.py:47 _materialize_entities_to_postgres` 表示同款签名仍在）。

## 3. F3：P3 part 3 真实环境对账

### 3.1 需要环境

- 外部可访问的 `localhost:8000` （FastAPI 后端，含 PR #6 那 7 个 commits）。
- `psql` CLI + PG 连接信息（`DATABASE_URL` 或逆向 `--host`/`--user` 参数）。
- 可选：`cypher-shell` + Neo4j 连接信息。
- 真实项目 `PROJECT_ID` + 章节索引。

### 3.2 跑法

```bash
export BASE_URL=http://localhost:8000
export TOKEN=<JWT>
export PROJECT_ID=<uuid>
export CHAPTER_IDX=<int>
export DATABASE_URL=postgresql://...
export NEO4J_URL=bolt://...        # 可选
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=...

bash scripts/verify_entity_writeback_v19.sh
```

预期输出：`OK: 设定集 writeback v1.9 对账通过`，后跟 4 段验收结果。

### 3.3 输出回填

- 拉一个 follow-up PR（仅 docs），把脚本输出贴进 `docs/PROGRESS.md` §2 + §5 或新建 `docs/VERIFY_LOG_2026-05-XX.md`。
- 同步勾选 `docs/HANDOFF_TODO.md` 的 P3 part 3 / `docs/PROGRESS.md` 的 P3 part 3。

## 4. 建议优先级

| 项 | 优先级 | 依赖 |
|----|--------|------|
| 合并 PR #6 | P0 | 无 |
| F3 P3 part 3 对账 | P1 | PR #6 合并后环境起 |
| F1 选 A 或 选 B | P2 | 选 A 需先上 F2 |
| F2 v1.10 cherry-pick | P2 | PR #6 合并 |

## 5. 本文档不包含

- 代码修改。本 PR 仅为 docs。
- 架构决策本身。选项 A vs B / F2 是否上、何时上、谁负责都留给用户决。
