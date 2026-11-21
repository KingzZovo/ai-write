# 神裔 全流程验证报告（2026-06-18）

针对目标"全书大纲→分卷大纲→章节大纲→章节正文，确认不偏离设定、正文人类作家水准、逻辑自洽不瞎编"的端到端验证。验证用 host 内联 `.venv` 跑**已修复**代码（live 容器仍是旧码，未重启），DB 只读，正文产物落 `backend/tmp/shenyi_flow/`（已 gitignore）。

## 流程结果

| 阶段 | 结果 |
|------|------|
| 全书大纲 | ✅ 生成成功（skeleton+characters+world 三段，~11KB） |
| 分卷大纲 | ✅ 第1卷 150 章全部生成（30 批 ×5，标题质检改写 2 个） |
| 章节大纲 | ✅ ch1/ch2 结构完整；ch3 JSON 解析失败（落 raw_text） |
| 章节正文 | ✅ ch1-3 各 ~4.7-5.4K 字 |

## 关键发现与修复（本轮 4 个 commit）

### 1. scene_planner 6/6 fallback —— 已修 (`293dbb2`)
**根因**：`run_structured_prompt` 永远返回 dict（裸数组被包成 `{"items":[...]}`），但 `plan_scenes` 用 `isinstance(parsed_any, list)` 判成功——永假，故每次良好规划都落模板 fallback。修复后按 `items`/`scenes` 键取数组。
**实测**：ch1 `fallback=False`，模型自起场景名（最后一单/箱底工单/502室门前），旧码绝不可能产出。

### 2. 大纲 fallback 偏离设定 —— 已修 (`cbccfb4`)
**根因**：relay 慢（>120s）时 characters/world 段落落本地 fallback，而 fallback 文案是**过时的**神裔设定（沈砚/血印/血税/王朝档案司），与项目固定事实锁（林照/回声塌陷/暗面学院/活体滤波接口/清除署）冲突——导致 run1 全书大纲出现"两个主角两个世界"。修复：fallback 改为 canon-neutral，回显 user_input 不再硬编码人名。
**实测**：run2 全书大纲 0 处 off-canon 泄漏，5/5 canonical anchors 命中；卷标题回到 回声塌陷/管网深处/协议之下/根权限/无名神核。

### 3. 章节/分卷大纲 502 —— 已修 (`2edc7f4`)
**根因**：`generate_chapter_outline`/`generate_volume_outline`(非staged) 用 `task_type="outline"`，但 prompt_assets 只有 `outline_book/outline_volume/outline_chapter`——无 `outline` 路由，落空 env provider→relay 502 "unknown provider"。修复：各调用点改用匹配 `_log_meta` 的具体 task_type。

### 4. call-log 毒化 DB 连接 —— 已修 (`2edc7f4`)
**根因**：上面 502 fallback 把 `endpoint_id="env_openai"`（字符串）写入 UUID FK 列→asyncpg DataError→**整个 session 事务中止、连接关闭**→级联 rollback InterfaceError 杀死整个 run。修复：`_coerce_uuid_or_none` 把非 UUID endpoint id 记为 NULL。

## 正文质量评估（人工抽样）

- **ch1**（4797 字，真规划非 fallback）：人类作家水准。具体感官（刹车皮偏硬入冬刹距长半拍、路灯比市中心晚二十分钟），克制节奏，真伏笔钩子（寄件编号 7-B-04 对应父亲工具箱底维修工单——正是设定里的物理锚点）。在设定内（林照/快递/旧环十二区）。
- **ch2**（5426 字，fallback briefs）：仍属优秀。回声塌陷开场——日光灯精确频率亮灭、消防栓边框每段黑暗偏移几毫米（空间折叠）、邻居王姐端水姿势冻结但脚踝以下非人。物理锚点（工牌/工单/录音带）在场。
- **ch3**（4742 字，fallback briefs）：紧接 ch2 折叠空间，右臂被"空间裁开的截面"割伤、银色神经纹路从伤口析出（canonical 血脉激活征兆）。逻辑连续、不瞎编。

**结论**：即便用 fallback briefs，正文仍达标——因为章节大纲本身扛住了设定。真规划（ch1）质量更高。

## 待办（已记录，本轮未扩展处理）

1. **scene_planner 偶发丢合同字段**：planner 返回的场景偶尔缺 `action_budget`/`inference_ledger` 等 1-2 个字段（安静场景模型判定 N/A 而略过）。`_has_valid_scene_contract` 要求 12 字段全在每个场景→不匹配→fallback。prompt 用的是"高压场景必须"的条件措辞，gate 却按无条件全要求——**gate 比 prompt 自身更严**。建议：要么 gate 改为条件式（仅高压场景要 action_budget），要么 prompt 强化为无条件全填。生产默认 `ALLOW_SCENE_PLANNER_FALLBACK=1` 已优雅兜底，非阻塞。
2. **OUTLINE_FAST_CALL_TIMEOUT_SECONDS=120 偏紧**：characters/world 段落经常 >120s，每轮稳定双超时再 fallback。relay 快时无碍，慢时拉长~4min/书。可调高或改流式。
3. **ch3 章节大纲 JSON 解析失败**：单章 outline 落 raw_text。章节大纲也应像 scene_planner 一样有 json_repair+strict retry 兜底。

## 更新（同轮后续修复 + 复测）

- **待办#1 已修** (`fb74b56`)：把合同字段拆成"始终必填的 10 个连续性字段"+"条件字段 action_budget/inference_ledger"，gate 只校验前者，与 prompt 的条件措辞一致。**复测（run3，gate 修复后）**：ch3 由 fallback→**真规划**（场景名 夹角濒死/二十分钟撤离/安全屋锁定，fallback=False），正文紧接 ch2 折叠空间、右臂银色纹路析出，逻辑连续在设定内。ch1 仍真规划。
- **新发现（待办#1 的更深层根因，未改 live 配置）**：ch2 仍 fallback，但这次缺的是 `mechanism_limits/result_strength/transition_bridge/continuity_ledger` 这些**始终必填**字段——root cause 是 `scene_planner.max_tokens=4096`（DB prompt_assets 配置）有时不够装 3-6 个写满合同字段的场景，末场被截断、json_repair 只救回残缺对象。探针实测：明确要求写满时 3 场≈4852 字符（恰好够），但更长/更多场景会溢出。**建议**：把 DB 里 scene_planner 的 max_tokens 调到 6144（动 live 配置，待确认）。生产 fallback 已兜底，非阻塞。

## 最终结论

全流程（书→卷→章大纲→3 章正文）端到端跑通。本轮修了 5 个真 bug（scene_planner 契约、大纲 fallback 偏离设定、大纲 task 路由 502、call-log 毒化连接、合同 gate 过严），均 TDD + 全套 631 绿 + live relay 复测。正文质量达人类作家水准、在设定内、逻辑自洽不瞎编（ch1/ch3 真规划，ch2 fallback 仍优秀）。剩余 3 项为非阻塞优化（max_tokens 调高、outline 超时、章节大纲 json 兜底），生产 fallback 均已覆盖。
