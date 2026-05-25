# 生成系统进展与待办（自动维护）

> 目标：必须先产出章节正文全文，再评分；若 scene_planner JSON list 不稳定，先修复；修不掉则走直出（fallback）保证正文不被卡死。

## 当前阻塞
- scene-mode 下 `scene_planner` 结构化输出（JSON list）不稳定，导致 chapter 正文任务频繁 `needs_repair`。

## 已完成
- 扩大 scene_planner 失败时的可观测性：在 `scene_orchestrator.py` 把 `unparseable_output` 的 `raw_text_snippet` 拼进异常字符串，便于定位模型输出形态问题。
- 扩大 scene-mode gate 持久化错误信息长度：`knowledge_tasks.py` 把 `task.error_message` 持久化截断从 220 扩大到 1200，避免 snippet 被截掉。
- 触发并验证 chapter 任务会因 `scene_planner_failed: unparseable_output` 进入 `needs_repair`（证明根因在 planner 结构化输出，而非 writer）。

## 进行中（必须完成正文才算完成）
1. 将 chapter 生成流程改为：planner 失败时不再卡死 gate，而是走 fallback briefs 继续写正文（直出保证正文全文）。
2. 生成完成后执行评分（auto_revise + threshold 8.2）并保存评分报告，用于后续优化。

## 待办（按优先级）
- [ ] 修复 scene_planner 输出契约：让输出稳定为 JSON array（必要时增加容错：接受 `{items:[...]}` / 代码块包裹 / 前后解释文字）。
- [ ] 兜底直出：planner 连续失败时必须 fallback 继续 scene_writer，禁止 0 字卡死。
- [ ] 分层流程验收：outline_book → outline_volume → outline_chapter → chapter，每层都要覆盖 背景/设定/人物/道具技能/主线支线/伏笔/衔接（维度只能多不能少）。

## 最近任务 ID（用于排查）
- 0064d952-ce27-4c0a-950f-163844c642b6（needs_repair：unparseable_output）
- a8f06e19-35a3-4cc8-8e04-b60aa49e933e（running：重启后新触发，用于验证新兜底逻辑）
