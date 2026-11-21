# HANDOFF · 2026-05-04 · B1 失效点收尾 + PR-OUTLINE-DEEPDIVE

## TL;DR

本会话收尾 B1 批次 + 代付了全部 4 阶段大纲重构，并顺手修了设定集世界规则编辑。
分支 `feat/phase2-fix` · HEAD = `aae0060` · base `2583d9d` · 7 个新 commit（1 个还本会话补 push 后紧跟）。

## 状态总表

| Issue / PR | 状态 | Commit | 备注 |
|---|---|---|---|
| A · TOKEN 用量显示 0 | ✅ | `d215a38` | PR-FIX-TOKEN-DASH-SHAPE |
| B · 全书大纲 `<volume-plan>` 泄露 | ✅ | `0a87168` | PR-FIX-OL15-CELERY-STRIP |
| C · 设定集·世界规则不可编辑 | ✅ | `a147fc7` | PR-FIX-CHAR-SETTINGS-FE |
| D · 第一卷为空 | ✅ | (用户补上) | 用户反馈已自修 |
| E · 右侧三按钮失效 | ❌丢弃 | — | 用户取消，以左侧点击代替 |
| 新 · 大纲中间可编辑 | ✅ | `92f3814` | PR-OUTLINE-CENTER-EDIT |
| 大纲重构 · 设计文档 | ✅ | `b3a8768` | PR-OUTLINE-DEEPDIVE 定文 |
| 大纲重构 · Phase 1 | ✅ | `bce57a3` | chapter_outline_expander.py + API |
| 大纲重构 · Phase 4 | ✅ | `faffc57` | context_pack 本章大纲中文结构化渲染 |
| 大纲重构 · Phase 2 | ✅ | `aae0060` | OutlineEditor AI 扩写按钮 |
| 大纲重构 · Phase 3 | ⏳ | (本文档后续) | repair 脚本·dry-run 烟测通过 |

## 部署提醒（必看）

前端代码修改**必须**：`docker compose build frontend && docker compose up -d frontend`。
容器是 `RUN npm run build`，无 bind mount，`docker restart` 不会拋 BUILD_ID。
后端则只需 `docker compose restart backend`（有 bind mount）。

BUILD_ID 足迹：
- B1 起点：`hhSqzezETm-_vgp8yHNMC`
- A 修代：`0bVsaiM0wMnnsi7xrB183`
- 中间可编辑：`C7QXDNPvHCOV8K-s8g1q2`
- 设定集修代：`ir-gDe0lHEt1hfVkRXoGj`
- 本次最后（Phase 2）：`uCd8CDV3bRaNpouoHfeeo`

## PR-OUTLINE-DEEPDIVE 详情

### 问题陈述
用户反馈「章节大纲只是分卷大纲拄出来」。代码在 `volumes.py:313` 与 `outlines.py:142`
只是 `outline_json = chapter_summaries[i]` 直接拷贝，未走 LLM 扩写。结果章节大纲只有 4 个字段、缺全本记忆跳板资产。

### 设计文档
[`docs/PR-OUTLINE-DEEPDIVE_2026-05-04.md`](./PR-OUTLINE-DEEPDIVE_2026-05-04.md) · commit `b3a8768`。

### 新增 schema（chapter.outline_json）
```
{
  "chapter_idx": int,
  "title": str,
  "summary": str,
  "key_events": list[str],
  // PR-OUTLINE-DEEPDIVE 新增字段
  "prev_chapter_threads": list[str],            // 上章余波接续
  "state_changes": {
    "characters": list[{"name", "change"}],
    "items": list[{"name", "change"}],
    "relationships": list[{"from", "to", "change"}]
  },
  "foreshadows_planted": list[{"description", "resolve_conditions"}],
  "foreshadows_resolved": list[str],
  "next_chapter_hook": str
}
```

### Phase 1 · LLM 扩写服务（`bce57a3`）
- `backend/app/services/chapter_outline_expander.py`
- `POST /api/projects/{pid}/chapters/{cid}/outline/expand`
- 拉 全书大纲 + 本卷大纲 + 上章 outline + 上章近文 1500 字作上下文
- 复用 `model_router.generate(task_type="outline_chapter")`
- 烟测：测试项目卷 1 章 1 · 49.5s · 5 条伏笔、三类状态变化、明确钩子 · PG 中 9 个键齐备

### Phase 4 · 生成依赖注入（`faffc57`）
- `backend/app/services/context_pack.py` · +`_render_chapter_outline_block()`
- 本章大纲从 JSON dump 改为中文分段（棗概 / 事件 / 余波 / 状态 / 伏笔 / 钩子）
- 向后兼容老 4 字段格式、全缺字段时 fallback 到 json.dumps

### Phase 2 · 前端 AI 扩写按钮（`aae0060`）
- `frontend/src/components/outline/OutlineEditor.tsx`
- `target.type === 'chapter'` 时额外渲染「AI 扩写本章大纲」按钮
- 调 expand 接口后同步到 textarea + onSaved 向上送

### Phase 3 · 历史 raw_text 修复脚本（待提交）
- `backend/scripts/repair_volume_outline_raw_text.py`
- 扫描所有 volume outline，该干的说干开面、不干可用 `--apply` 重生 raw_text
- 烟测：测试项目·5 卷·clean=5 · 未发现损坏。完整保留以供后续项目/CI 复查。

## 设定集 · Issue C（`a147fc7`）
- DB 诊断：PG `characters` 80 行·profile_json={} · Neo4j Character 52 节点·仅名字
- FE 编辑「世界规则」只有删除/新增、无 inline edit
- 修：`SettingsPanel.tsx` 加 inline 编辑 + 删除确认
- 说明：**人物 profile_json 稀疏是 entity extractor 质量问题，不在本 PR 范围**。后续可考虑为 Character/Setting 加一个「AI 拽充设定」按钮。

## 后续优先级类 TODO
1. **批量扩写**：按卷/按全书一键扩写所有未扩写章节大纲（能 celery 后台跑）
2. **结构化表单**：OutlineEditor 在检测到 7 字段时提供逐字段可编辑表单而不是原 raw_text
3. **中文 prompt 强制**：生成仅依赖上下文余波 / 钩子、不能空、话术调优
4. **设定集 AI 拽充**：FE 人物/世界上加「AI 补全」按钮调用 entity_extractor 在 chapter 上重跑
5. **autofill agent**：如需为 75+ 行人物 profile_json 刷后变量字段可用 autofill agent

## 验收路径（用户可手试）
1. 进测试项目 `20d164ab-232f-4863-8265-452186638d83`
2. 左侧点任一章节大纲（比如《卷 1 章 2 袖扣的习惯》）
3. 中间区头部点「AI 扩写本章大纲」
4. 30~60s 后该章 outline_json 被覆写为 7 字段完备版本
5. 后续调「生成本章」，prompt 中 【本章大纲】下会出现中文分段结构，含伏笔/状态/钩子。

## 错误 & 教训
- read_text 不接受 end_line · 用 `run_command sed -n 'A,Bp'`
- run_command / wait_task 不接受 timeout_seconds
- 超 60s 的 LLM 调用要走 run_command_stream + wait_task
- shell 没有 `time` 可执行，别加
- apply_patch 遭遇 JSX `style={{}}` 予占位时：改用 Tailwind class 避免交互
- apply_patch 「hunk 只有 context」错误代表补不到补丁 · 需不包含原有行、仅增加行时仍需 + 标记
- backend 有 bind mount、frontend 没有 · 报表在 顶 1 部署提醒。

## 下一会话入口
- HEAD = `aae0060`、并未 push。本会话末尾会 push Phase 3 + 本 HANDOFF。
- `feat/phase2-fix` 已领先 base 7 个 commit · 有需要合并请走 PR / 快进
