# AI Write v1.9 实体写回（Neo4j → Postgres）一致性设计规格

**日期：** 2026-04-30
**状态：** Draft
**关联问题：** “正文回写 + 角色档案双断裂”中的第二层断裂（实体抽取产物仅落 Neo4j，不回写 PG 主表）。

## 0. TL;DR（结论先行）

当前系统的“实体抽取”已经形成稳定闭环：

- 生成后 dispatch → Celery task → `EntityTimelineService.extract_and_update()` → **写入 Neo4j**（Character / CharacterState / Relationship / Location / ExtractionMarker 等）。

但**读端与主业务表**仍依赖 Postgres 的 `characters / relationships / foreshadows`，导致：

- 新角色（如“凌祝 / 纪砚 / 苏未”）在 Neo4j 已存在，但 PG `characters` 仍停留在最初种子数据；
- 前端/校验/上下文拼装读取 PG 时出现“知识图谱已更新但主表未更新”的结构性脱节。

v1.9 目标：把“Neo4j 实体产物”以幂等方式**物化回 PG**（最小闭环），并定义长期演进方向（是否继续以 Neo4j 为主写）。

## 1. 背景与问题定义

### 1.1 现状：写路径与读路径分裂

**写路径（事实）：**

- `HookManager.run_post_hooks()` 会 enqueue entity extraction。
- `app.tasks.entity_tasks` 相关任务会读取 PG `chapters.content_text`（只读），并调用 `EntityTimelineService(driver)` 将抽取结果写入 Neo4j。
- `EntityTimelineService` 内部是纯 Cypher（大量 `driver.session()`），**不触碰 PG**。

**读路径（事实）：**

- Postgres：
  - `characters`：`(id, project_id, name, profile_json, created_at)`
  - `relationships`：强依赖 `characters.id` 的 FK
  - `foreshadows`：独立系统（hook_manager 里也有相关 post-hook）
- Neo4j：实体/状态/关系/地点等更丰富，但当前并未“回灌” PG。

### 1.2 断裂的具体后果

- 角色一致性、人物列表、关系图等功能在“读 PG”时永远看不到新角色。
- 生成时若 ContextPack 或 validators 依赖 PG `characters`，会持续偏离真实小说正文。
- “实体抽取已经成功”并不等于“产品侧知识资产可用”。

## 2. 设计目标 / 非目标

### 2.1 目标

1) **幂等、可回放**：同一章抽取多次不会造成 PG 重复角色、重复关系。
2) **低侵入闭环**：不重写整个知识系统，先把第二层断裂补上。
3) **可观测**：写回成功/失败可计数、可定位（至少日志 + task 状态 + 可查询对账 SQL）。
4) **一致性语义明确**：PG 侧哪些字段由 Neo4j 物化？冲突如何处理？

### 2.2 非目标（本 RFC 不做）

- 不把“PG 变成完全弃用，只读缓存”一次性做到位（只给演进方向）。
- 不在 v1.9 一次性重构所有读端为 Neo4j（但会列出最小必要的读端改造点）。
- 不引入新外部存储。

## 3. 真实 Schema（作为设计锚点）

### 3.1 Postgres：characters

`public.characters`：
- `id uuid` (PK)
- `project_id uuid` (FK → projects.id)
- `name varchar(200)`
- `profile_json json` (nullable)
- `created_at timestamptz` (nullable)

> 注意：表上没有 (project_id, name) unique 约束；如果要做并发幂等 upsert，需要补约束或引入事务级去重策略。

### 3.2 Postgres：relationships

`public.relationships`：
- `id uuid PK default gen_random_uuid()`
- `project_id uuid` (FK)
- `source_id uuid` (FK → characters.id)
- `target_id uuid` (FK → characters.id)
- `rel_type varchar(50)`
- `label varchar(200) default ''`
- `note text default ''`
- `sentiment varchar(20) default 'neutral'`
- `created_at timestamptz default now()`
- `since_volume_id uuid null` / `until_volume_id uuid null`
- `evolution_json json default '[]'`

> v1.9：新增唯一约束 `uq_relationships_rel_key(project_id, source_id, target_id, rel_type)`，用于保证写回幂等与去重。

### 3.3 Postgres：foreshadows

`public.foreshadows`：
- `id uuid` (PK)
- `project_id uuid` (FK)
- `type varchar(20)`
- `description text`
- `planted_chapter int`
- `resolve_conditions_json json null`
- `resolution_blueprint_json json null`
- `narrative_proximity double precision null`
- `status varchar(20) null`
- `resolved_chapter int null`
- `created_at timestamptz null`

> 注意：此前口径中的 `foreshadowings` 表不存在，真实表名为 `foreshadows`。

## 4. 关键事实：batch / 单章两条入口的“写回契约”

- 单章生成路径：生成保存成功后会 dispatch entity extraction。
- batch 路径：`BatchGenerator.generate_batch()` 每章完成后调用 `HookManager.run_post_hooks()`，其 `_update_entities()` 同样 dispatch entity extraction。

=> 结论：实体抽取“触发入口”已经足够统一；缺的是“抽取产物物化回 PG”的最后一步。

## 5. 方案选型（D-1）

这里给出 3 种可行策略，并明确 v1.9 选择。

### 5.1 方案 A：Neo4j 主写 + PG 物化（本 RFC 推荐）

- 事实上的写路径已经是 Neo4j 主写。
- v1.9 在 Celery entity task 成功后追加一个 **PG 物化步骤**：
  1) upsert characters（按 project_id + name）
  2) upsert relationships（依赖 source/target 的 character_id）

## 8. rel_type 治理（写回长期可用性）

### 8.1 问题

当前抽取的 `relationships.rel_type` 存在大量“长句 + 解释”的类型值（例如包含括号解释、斜杠组合），会导致：

- 数据库侧：`rel_type varchar(50)` 容易被截断或形成过多低频类型；
- 逻辑侧：下游一致性校验（例如 OOC checker）依赖 `rel_type` 的关键字匹配（如“敌对/朋友”），长句会显著降低命中与可维护性；
- 产品侧：关系图/筛选等功能难以形成稳定维度。

### 8.2 约束与原则

- `rel_type` 作为“粗粒度关系类型”，应保持**短、稳定、可枚举**（建议 2~6 个字）。
- 更细粒度的叙事解释应进入 `label/note/evolution_json`。

建议 canonical 词表（可扩展）：

- 盟友 / 朋友 / 恋人 / 师徒 / 兄弟
- 敌对 / 对立
- 上下级 / 监管
- 同伴 / 同舍
- 其他

### 8.3 v1.9 最小落地

在写入 PG 前，对抽取到的 `rel_type` 做轻量归一化（包括：写入 PG 的 outlines/extract 路径、以及 Neo4j → PG materialize 路径）：

- 去掉括号解释（`（...）` 或 `(...)` 之后的内容）
- 斜杠组合（`A/B`）取第一个 token（`A`）
- 按关键字归一到 canonical token（例如 敌对/对立/监管/审讯/师生/上下级/同舍/同伴/失联）
- 最终截断到 50 字符（遵循 DB schema）
  3) （可选）把本章抽取出的“新伏笔”也映射到 `foreshadows`（但当前系统伏笔已有独立 manager，应慎重）

优点：
- 改动最小：不动抽取逻辑，只补写回。
- 立刻消除“Neo4j 有、PG 没有”的读端断裂。

风险：
- 必须解决幂等键与并发去重（尤其 characters 表没有唯一约束）。

### 5.2 方案 B：PG 主写 + Neo4j 由 PG 构建

- 抽取写回 PG，再由 PG 导入/同步到 Neo4j。

缺点：
- 与现有实现逆向，改动大；并且 Neo4j 的状态/时间线模型更丰富，反向映射难。

### 5.3 方案 C：读端全面迁移 Neo4j（不做，但作为长期方向）

- 直接让前端/validators/ContextPack 读 Neo4j。

缺点：
- 需要系统性梳理全部读路径，v1.9 不适合“一步到位”。

### 5.4 v1.9 选择

- **选择方案 A**：Neo4j 主写 + PG 物化。

## 6. 设计细节（落地契约）

### 6.1 幂等键与一致性规则

#### characters

- 逻辑唯一键：`(project_id, name)`。
- v1.9 建议增加 DB 约束：
  - `UNIQUE (project_id, name)`

如果短期不加约束（不推荐），则写回步骤必须：
- 在同一事务中 `SELECT ... FOR UPDATE` / 或用 `SERIALIZABLE` 防并发插入重复。

`profile_json` 合并规则：
- v1.9 先采取“保守 merge”：
  - 若 PG 为空 → 写入 Neo4j 侧抽取的 profile
  - 若 PG 非空 → 以 PG 为主，只把 Neo4j 中明确新增字段 merge 进去（具体字段白名单在实现时定义）

> 理由：避免抽取噪声覆盖用户手工编辑的 profile。

#### relationships

- 关系的幂等键建议：`(project_id, source_id, target_id, rel_type)`
- 若允许多条 label/note 演化，则：
  - 固定一条主记录，历史写入 `evolution_json` append。

### 6.2 写回触发点（建议）

在 entity extraction Celery task **成功完成**后执行：

1) 从 Neo4j 拉取本章增量结果（或直接使用 task 内已有的抽取结果结构体，避免二次查询）。
2) PG 写回：
   - characters upsert
   - relationships upsert
3) 写回完成后记录一个“物化 marker”（可用 PG 新表或复用 Neo4j ExtractionMarker 增加字段）。

### 6.3 可观测性

- 新增 Prometheus counter：
  - `entity_pg_materialize_total{outcome, reason}`
- 日志：
  - 成功：写回数量（new_chars / updated_chars / new_rels / updated_rels）
  - 失败：异常类 + project_id + chapter_idx

### 6.4 对账 SQL（验收必跑）

每次验证至少跑：

- PG 侧：
  - `SELECT COUNT(*) FROM characters WHERE project_id = ...;`
  - `SELECT COUNT(*) FROM relationships WHERE project_id = ...;`
- Neo4j 侧（通过 python driver）：
  - Character 节点数（按 project_id filter）
  - ExtractionMarker 状态
- 差异：
  - 抽样 10 个 Neo4j 新角色名，在 PG 中必须能查到（按 name）。

## 7. 实施计划（映射到 D-2）

### 7.1 最小补丁范围

- 增加 unique 约束：`characters(project_id, name)`（若允许迁移）
- 在 entity task 成功路径追加 PG 写回函数：
  - `materialize_entities_to_pg(project_id, chapter_idx, extracted_entities)`

### 7.2 回滚策略

- 写回逻辑以 feature flag 控制（默认开或默认关在计划中明确）。
- 回滚时只需关闭 flag，不影响 Neo4j 主写链路。

### 7.3 v1.9 实际落地（已实现）

以下实现已在仓库落地（以 `feature/v1.0-big-bang` 为准）：

- 迁移：新增 `a1001901`，为 `characters(project_id, name)` 增加唯一约束，保证并发幂等。
- 写回：在 `app.tasks.entity_tasks` 的 `entities.extract_chapter` 任务内，抽取成功后追加 **Neo4j → Postgres** 物化：
	- `characters`：按 `(project_id, name)` 查缺补漏；`profile_json` 初始写 `{}`（不覆盖人工编辑字段）。
	- `relationships`：按 `(project_id, source_id, target_id, rel_type)` 去重插入。
- 注意：物化读取 Neo4j 时不使用“状态快照”（`get_world_snapshot`）作为数据源；而是直接扫描 `(:Character {project_id})` 节点与 `[:RELATES_TO]` 边。
	- 原因：真实数据里可能存在“只有 Character 节点但尚无 HAS_STATE”的新角色；若只读状态快照会导致 PG 写回静默为 0。
- 可观测：新增 Prometheus Counter：`entity_pg_materialize_total{outcome,reason}`。

#### 7.3.1 验收对账（必跑）

以 project_id 为粒度，至少跑以下 SQL：

```sql
SELECT COUNT(*) FROM characters WHERE project_id = '<project_id>';
SELECT COUNT(*) FROM relationships WHERE project_id = '<project_id>';
```

并抽样检查 Neo4j 新角色名在 PG 中可查到。

## 8. 开放问题（本 RFC v0 明确待定）

1) 角色名是否需要规范化（去空格/全角半角/别名）作为幂等键的一部分？
2) profile_json 的字段级 merge 白名单如何定义？是否需要“用户锁定字段”机制？
3) relationships 的 rel_type 枚举是否已有统一规范？不同抽取版本的 rel_type 归一策略？
4) 是否要把 foreshadows 也纳入从抽取结果回写？还是保持 foreshadow_manager 独立（倾向独立）。
