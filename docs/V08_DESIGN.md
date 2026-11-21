# v0.8.0 — 写法引擎资产化 + 去 AI 味 + Agent Tool Registry v1

**目标：** 把“怎么写得像网文”从散落在 system prompt 里的隐性知识，变成可在 UI 编辑、可被 Critic 检查、可被 Agent 通过工具调用查询的显性资产。

## 核心概念

- **WritingRule**：写法条目，如“冲突要在段首；对话不超 3 轮换场”。
- **BeatPattern**：桥段套路，如“开篇装逼被打脸”“第一卷末揭身份”。
- **AntiAITrap**：AI 味陷阱词/句式，如“他深吸了一口气”“空气仿佛凝固”。
- **GenreProfile**：题材画像，如“仙侠 · 洪荒”“都市 · 龙王赘婿”。
- **ToolSpec**：Agent 可调用的工具契约，带 JSON Schema。

## 数据层

### PG 表

1. `writing_rules`
   - id, genre, category(`pacing`|`dialogue`|`hook`|`description`), title, rule_text, examples_json, priority, is_active
2. `beat_patterns`
   - id, genre, stage(`opening`|`volume_end`|`climax`|...), title, description, trigger_conditions_json, reusable
3. `anti_ai_traps`
   - id, locale(`zh-CN`), pattern_type(`keyword`|`regex`|`ngram`), pattern, severity(`hard`|`soft`), replacement_hint
4. `genre_profiles`
   - id, code(`xianxia`|`urban`|`scifi`|...), name, description, default_beat_pattern_ids(JSONB), default_writing_rule_ids(JSONB)
5. `tool_specs`
   - id, name, description, input_schema_json, output_schema_json, handler(`python_callable`|`sql`|`qdrant`|`llm`), config_json, is_active

### 迁移

`a0800000_v08_writing_engine` 建 5 张表 + 必要索引。种子数据由 `seed_writing_engine()` 启动时增量注入（和 `seed_builtins()` 一致的模式）。

## ContextPack v3（第四路召回）

在 v2 三路（style_profiles / beat_sheets / style_samples_redacted）基础上加入：

- **writing_rules 路**：按当前 `GenreProfile` 选中活跃规则（top-K，按 priority 降序）。
- 规则不向量化，直接 SQL 查。

`context_pack.py` 新增 `writing_rules: list[WritingRule]` 字段；prompt 渲染插入 “写作规则（必须遵守）” 段落。

## Anti-AI 扫描（Critic 的第三层）

在 `critic_service.py` 的规则层和 LLM 层之间加一层：

1. 加载活跃 `anti_ai_traps`
2. `keyword`/`regex`/`ngram` 三种匹配器对 draft 扫描
3. `severity=hard` 命中 → 生成 issue 并触发 rewrite
4. `severity=soft` 命中 → 写入 `info` 下次提醒

## Agent Tool Registry v1

### 目的

让生成 agent 在 drafting 过程中可主动查资料，而不是一次性把所有上下文塞进 prompt。

### 首批工具（5 个）

| name | 描述 | handler |
|---|---|---|
| `search_memory` | 在 chapter_summaries + compacted 里按语义检索 | qdrant |
| `check_character_fact` | 查某角色最新 location / 等级 / 关系 | sql |
| `lookup_relation` | 查两个角色在指定卷的关系 | sql |
| `suggest_beat` | 按当前章节进度给出下一拍套路 | sql + llm |
| `classify_rule_violation` | 给定一段文本，返回触发的 writing_rules 条目 | regex + llm |

### 调用方式

- `generation_runner.drafting` 新增可选 `tool_loop` 模式（env flag `AGENT_TOOL_LOOP_ENABLED`，默认 off）
- 走 OpenAI `tools` / `tool_choice` 协议，最多 3 轮
- 每次工具调用落一行到 `llm_call_logs`

## 前端

- `/settings/writing-engine` 新页（Tab：WritingRule / BeatPattern / AntiAITrap / GenreProfile / ToolSpec）
- 每 Tab 一个表格 + 新建/编辑侧拉
- 项目设置页新增 “题材画像” 选择器（下拉 `genre_profiles`）

## API

```
GET    /api/writing-rules?genre=&category=&active=
POST   /api/writing-rules
PATCH  /api/writing-rules/{id}
DELETE /api/writing-rules/{id}
```

`beat-patterns` / `anti-ai-traps` / `genre-profiles` / `tool-specs` 同形。

## 验收标准

- [ ] 新建一条 AntiAITrap（keyword "空气凝固"），生成时触发 rewrite 并在 critic_reports 留痕
- [ ] GenreProfile 切换 `xianxia` → ContextPack 第四路 top-K 变化可验证
- [ ] `AGENT_TOOL_LOOP_ENABLED=true` 时，drafting 至少调用 1 次 `check_character_fact`
- [ ] WritingRule UI 支持 CRUD、启用/禁用即时生效
- [ ] 不开 flag 时行为与 v0.7 一致（回归测试通过）

## 工作量

约 6-8d（数据层 + 迁移 1d / Critic anti-AI 1d / ContextPack v3 1d / Tool Registry + tool_loop 2-3d / 前端 5 Tab 2d / 测试 1d）
