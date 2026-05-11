# HANDOFF 2026-05-12 — PR-STATE-WIRING-AUDIT

**Branch**: `feat/pr-state-wiring-audit`  
**预计 PR**: PR-STATE-WIRING-AUDIT（接线审计 + running_state 字段吸收 + lct2 数据 wipe）

## 0. 上下文 / 这次为什么开 PR

- 上一次（2026-05-12 早上）做了 lct2 volumes/chapters wipe，发现：
  - lct2 `character_states` 残留 1051 行（chapter_start=0 处 201 行 + 之后每章 2–10 行），但 lct2 已无 volume/chapter（孤儿数据）。
  - chixin `character_states` = 0，但项目说有人物卡，说明 extract-settings/entity 提取链可能从未跑过。
- 用户提出方案变更：**先看「running_state（上一轮我提案的角色运行状态）」相比现有 `character_states.status_json` 体系有什么补盲点 → 能吸收的全部吸收 → 再做接线审计 + 字段补齐 → 然后 wipe 孤儿数据 → 一起推进**。

## 1. 接线审计结果（这次就把图摸清了，别再忘）

```
章末（HookManager.run_post_hooks 触发；见 hook_manager.py:401）
  └─ dispatch_entity_extraction（entity_dispatch.py）
      └─ celery: entity_tasks.extract_chapter_entities
          ├─ EntityTimelineService.extract_and_update → LLM 提取（entity_timeline.py:ENTITY_EXTRACTION_PROMPT）
          │   └─ Neo4j 写入: (:Character)-[:HAS_STATE]->(:CharacterState {status_json})
          │                    (:Character)-[:AT_LOCATION]->(:Location)
          │                    (:Character)-[:RELATES_TO]->(:Character)
          │                    (:Character)-[:MEMBER_OF]->(:Organization)
          └─ _materialize_entities_to_postgres（同文件）→ Postgres 投影
              ├─ characters / character_states (status_json JSONB)
              ├─ character_locations / character_organizations
              ├─ relationships / locations / organizations / world_rules
              └─ items / item_events / foreshadows / time_anchors

章生成（chapter_generator.py / scene_orchestrator.py）
  └─ ContextPackBuilder.build()
      ├─ L1 proximity (recent_summaries)
      ├─ L2 facts: character_cards <- _enrich_from_neo4j()
      │              ↑ 读 Neo4j HAS_STATE.status_json 注入 CharacterCard
      ├─ L3 RAG (chapter_summaries / dialogue_samples / style_samples)
      ├─ foreshadows / time_anchors / strand_tracker
      └─ to_messages() → run_text_prompt / stream_text_prompt
                        ↑ 进入 system_prompt 喂给 LLM

章末 checkers（reader_pull_checker / continuity / consistency / ooc / pacing）
  └─ 通过 ContextPack.character_cards 读人物卡（确实接通了）
```

**关键确认**：
- ✅ ContextPack 已经接进了 chapter_generator + scene_orchestrator（不是孤岛）
- ✅ ContextPack 的 character_cards 是从 Neo4j HAS_STATE.status_json 拉的（不是 Postgres 直接读，但 Neo4j 和 Postgres 是双写镜像）
- ✅ checker 链路全部读 ContextPack.character_cards（一致性/OOC/连续性都消费 character_cards）
- ✅ 章末 entity 提取由 HookManager 触发，写入 Neo4j → 投影回 Postgres 的 13 张表（chars/states/locs/rels/orgs/items/foreshadows/...）

**仍有的缺口**（写进 backlog 防忘）：
- ⚠️ `_extract_entities_from_outline` (context_pack.py:1076) 仅做了名字白名单过滤，没主动把大纲里的角色注入 character_cards（依赖 Neo4j 已有 HAS_STATE，章 1 之前几乎拿不到）
- ⚠️ `time_anchors` 表 lct2 / chixin 都为 0，说明这一支注入路径从未被实测打通
- ⚠️ `character_organizations` / `items` / `item_events` 同样为 0
- ⚠️ `settings_extraction` task_type 在 `llm_call_logs` 中 lct2/chixin 均为 0 → `/outlines/{id}/extract-settings` 路径**从未被调用过**（角色都是章末提取生成的，没有大纲预生成）

## 2. running_state 设计 → 吸收哪些字段（这次都加进 status_json schema）

现有 `character_states.status_json` 是 JSONB，4 个 free-text 字段：`{身份, 状态, 情绪, 能力等级}`。  
running_state（我上一次提案的设计）里值得吸收的字段：

| 新字段 | 类型 | 作用 | 补盲点 |
| --- | --- | --- | --- |
| `位置` | string | 本章末所在地点 | 之前 character_locations 只跟 Location 实体绑，地点没进 prompt 时 LLM 容易瞎写 |
| `持物` | list[string] | 关键道具 | items 表全空 → 道具状态完全失忆，会出现"刚扔了又有" |
| `知识边界` | list[string] | 本章新知晓的事实 | 跨章一致性弱项：角色"还没听说就先知道"的穿帮 |
| `称谓` | dict[str,str] | 对方→怎么称呼 TA | 防止 A 在 B 面前忽然换称呼 |
| `未决项` | list[string] | 本章埋下未消解的承诺/选择/伏笔 | 与 foreshadows 表互补，更细粒度（不到伏笔层级的小线索） |
| `生理` | string | 受伤/疲劳/伪装/中毒 | 与"情绪"解耦，物理状态独立追踪 |

**不吸收**：
- 单独 `running_state` 表/列 → 重复造轮（已有 `character_states` 时序表）
- `spatial_neighbors` → 已由 `character_locations` join 覆盖
- decision_locks → 暂用 `未决项` 表达，等真实痛点出现再细分

## 3. 字段补齐 PR 内容（已落代码，分支 feat/pr-state-wiring-audit）

**Diff stat**：3 files, +83 / -4

### 3.1 `backend/app/services/entity_timeline.py` (+19 / -3)

ENTITY_EXTRACTION_PROMPT 输出 schema 从 4 字段扩展到 10 字段：
- `new_characters[*].state` 加 `位置 / 持物 / 知识边界 / 称谓 / 未决项 / 生理`
- `state_changes[*].changes` 同步加这 6 个 key
- 规则块加 4 条：未变化的字段一律省略 / 新增维度只在确实变化时填 / "知识边界"用于防穿帮 / "未决项"用于伏笔兜底

### 3.2 `backend/app/services/prompt_registry.py` (+2 / -1)

chapter summary prompt 第 5 条规则改为列出全 10 字段；新增第 6 条解释"知识边界 / 未决项"语义。

### 3.3 `backend/app/services/context_pack.py` (+62 / -0)

- `CharacterCard` 数据类加 5 个字段：`inventory / knows_recent / appellations / pending_items / physical_state`
- `CharacterCard.to_prompt()` 加 5 段渲染（位置/实力/关系/心理/生理/持物/新知/称谓/未决/近期）
- `_enrich_from_neo4j` 的「已存在卡补字段」分支 + 「新建卡」分支都补读 `status.get("持物")/("持物")/("知识边界")/("称谓")/("未决项")/("生理")`（带 EN 别名）

**向后兼容**：旧 status_json（4 字段格式）不受影响，新字段全部 default empty。

## 4. 数据 wipe 记录（lct2 孤儿数据）

**Project**: `c5480585-78f0-44cd-b41e-c8b8348934d7` (lct2)  
**Backup**: `/tmp/lct2_settings_wipe_backup_20260511_172532.sql` (24 MB, 14498 lines, 含 16 张表 data-only)

| 表 | 清前 | 清后 |
| --- | --- | --- |
| character_states | 1051 | 0 |
| character_locations | 431 | 0 |
| relationships | 401 | 0 |
| locations | 339 | 0 |
| characters | 208 | 0 |
| foreshadows | 195 | 0 |
| world_rules | 17 | 0 |
| character_organizations / items / item_events / organizations / settings_change_log / time_anchors | 0 | 0 |

清除总量：**2642 行**。chapter_time_anchors / volume_summaries / beat_sheet_cards 不在范围（前两者已 0；beat_sheet_cards 按 reference_books 关联，与项目无关）。

## 5. 已知风险 / 跟进项

- **Neo4j 残留**：本次只清了 Postgres 投影。Neo4j 里 lct2 项目的 `Character / CharacterState / RELATES_TO / Location` 节点未清。下次 entity 提取触发时，Neo4j 的旧节点可能反向投影回 Postgres → 需要单独写 Neo4j wipe cypher 或确认 lct2 不会再触发 entity 提取。
- **Qdrant 残留**：lct2 章节摘要向量也可能还在 RAG 集合里，与已删除章节失联（无害但占空间）。
- **新 schema 第一批生效条件**：必须重启 worker 让 prompt_registry 重新载入 system_prompt（Celery worker、API 进程都要重启）。
- **回归检查**：跑一章 lct2 提取，看 `character_states.status_json` 是否真的出现 `位置/持物/...` 这些 key。如未出现，可能是 LLM 不遵守 prompt（需观察 1–2 章后再加 critic）。
- **PR-A（critic 闭环）未做**：本 PR 只做字段补齐，没加 schema validator / 自动重提取。等新 schema 跑出真实数据后再决定 critic 形态。
- **章末反向更新**：当前 entity 提取是 "章→提取→更新 status"，没有 "prompt 中显式让 LLM 写章末状态变化" 的环节。考虑后续加 generation prompt 收尾段 "### 本章人物状态变化（JSON）"，让作者模型自己声明。（backlog，不本 PR）

## 6. 提交清单

- [x] 备份 lct2 16 张表（24M dump）
- [x] DELETE 13 张表（2642 行清除）
- [x] entity_timeline.py 提取 schema 扩展
- [x] prompt_registry.py summary 字段集同步
- [x] context_pack.py CharacterCard 扩展 + enrichment + to_prompt
- [x] 三文件 AST 通过
- [x] HANDOFF 文档（本文件）
- [ ] git commit + push（紧随其后）
- [ ] worker / api 重启验证（下次会话再做）

— 写于 2026-05-12 01:25（Asia/Shanghai）by Codex (King)
