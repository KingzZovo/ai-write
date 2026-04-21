# v0.7.0 — 状态机 + Critic + 记忆压缩 + 全局大纲

**目标：** 把单次生成升级为受控流水线，有断点恢复、有审查复写、有记忆管理。

## 状态机

```
planning → drafting → critic → [rewrite] → finalize → compact?
```

### `generation_runs` 表

- id, project_id, chapter_id, phase, status, checkpoint_data(JSONB), last_error, retry_count, rewrite_count, max_rewrite_count
- 每 phase 完成落 checkpoint，幂等重试
- SSE 接口改为订阅 run 状态流而非计算流

### checkpoint_data 结构

- `planning.pack`：ContextPack snapshot
- `drafting.text`：draft
- `critic.report`：Critic 问题列表
- `finalize.final_text`：最终文本

## Critic 节点

### 输入
- draft 文本
- 当前 ContextPack（含 Character / WorldRule / Chapter summaries）

### 逻辑
1. **规则校验**：draft 中抽实体 → 对照 Character 表
   - 位置不匹配 → hard
   - 实力跨级 → hard
   - 关系矛盾 → hard
2. **LLM 审查**：`task_type="critic"`
   - system："你是小说一致性审校官，指出 draft 与事实不符之处，JSON 输出"
   - output：`{issues: [{severity: hard|soft|info, category, desc, location}]}`
3. **分级处理**
   - hard → rewrite（最多 N 轮，默认 2）
   - soft → 记录不重写
   - info → 念入 Prompt 的 “下次注意”

### `critic_reports` 表

id, run_id, round, issues_json, created_at

## 记忆压缩

### 触发
- 定时：Celery beat 每天一次
- 阈值：`chapter_summaries` points > 100 且未压缩的 > 50
- 手动：`POST /api/projects/{id}/compact-memory`

### 流程
1. 取前 80% 最老 summaries
2. 按 5 章一组调 `task_type="compact"`→ 二次摘要
3. 新 summary 写入 `chapter_summaries_compacted` 集合
4. 原 points 标记 `compacted=true`，召回时过滤
5. 最近 20% 不动

## 全局大纲（基于参考书层级摘要）

### 新端点
- `POST /api/outlines/from-reference` — 输入 `reference_book_id` + 向导参数

### 流程
1. 参考书 → 三级层级摘要（全书/卷/章）
2. 三级摘要 + 用户向导 → `outline_generator` 现有 system prompt
3. 输出新大纲→走现有 settings_extractor 抽角色/世界观/关系

## 验收标准

- [ ] 生成一章中途 kill 后端重启 → 从 checkpoint 继续
- [ ] 故意注入设定冲突 → Critic 捕获 → 触发 rewrite
- [ ] compact 后集合体积下降，召回命中率未大幅下降
- [ ] 从参考书生成新大纲，不泄露专名

## 工作量

约 7-8d（状态机 2-3d / Critic 2d / 压缩 1d / 全局大纲 1d / 测试 + 前端 1d）
