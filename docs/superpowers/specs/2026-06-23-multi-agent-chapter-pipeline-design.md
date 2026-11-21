# 子项目 B：多智能体章节质量管线（Multi-Agent Chapter Pipeline）

- 状态：设计已确认，待写实现计划
- 日期：2026-06-23
- 分支：`rescue/2026-05-17-baseline`
- 关联：`/goal` 复合目标的第二个子项目（B 先于 A）。Humanizer-zh 接入已完成（前序工作）。

## 背景与动机

用户对神裔项目 ch1 正文的人工审查暴露了三类**章内逻辑/剧情缺陷**：

1. **空间与动作方向矛盾**：消防通道既写「通向地面层出口」「继续向上跑」，又写「往下跑」「向地下深处倾斜延伸」，方向自相矛盾。
2. **画面大面积重述（草稿叠写残留）**：主角目击五楼骨架的画面在相邻段落被高相似度重描两次，二次目击只新增「蓝色塑料拖鞋」一个细节，却重复了整幅静态走廊描写与「头骨转过来」的动作，削弱逃生紧迫感。
3. **空间跨度突变**：从五楼逃离，前文刚写「才下去半层」，后文直接「右脚踩上三楼平台」，四楼到三楼缺少动态衔接。

阅读现有 critic 链路确认了**根因**：这三类都是**单章内部**的语义级缺陷，而现有质量设施无一覆盖此层：

- `geo_jump` / `time_reversal` / `item_missing`：跨章、Neo4j 支撑，比较的是 chN vs chN-1 的实体状态，不读单章内部空间逻辑。
- `continuity_checker`：基于关键词的时间线，无空间推理。
- `chinese_prose_mechanics_checker` 的 `duplicate_explanation_span_count`：抓的是**解释性**重复（同一压力链换说法），不是**场景画面**重述。
- LLM critic（`critic_hard`/`critic_soft`）：并行、通用，不是专职「读完整章、专找逻辑矛盾」的角色。

因此存在真实能力缺口：**没有任何一个 agent 做整章的逻辑/空间自洽审查**。这正是用户要的「逻辑与剧情核查」角色。

用户的目标流程（来自 `/goal`）：每章创作用 subagent，跑**主写手 + 逻辑与剧情核查 + 用词与格式核查**三轮，最终把 subagent 的终稿 echo 到主流程，**不污染上下文**。

## 关键约束

- **relay 严重限流**：opus-4-6 并发动辄 25s 冷却 / 503 `auth_unavailable`。**并行扇出会直接撞限流墙**（见项目记忆多次实证）。因此管线必须**串行**，每棒独立调用、独立退避重试。
- **不能破坏现有质量门**：`apply_chapter_quality_gate`（Humanizer + QMAI + 机械痕迹门 + persist-on-block）已稳定服务神裔续写，签名与内部行为不得改动。
- **渐进式（子项目 A）将复用 B**：B 的 `run_chapter_pipeline()` 必须是 A「写一章」那一步可直接调用的单元。
- **可一键回退**：出问题能秒退回纯 `quality_gate` 老路径。

## 架构总览

新增串行三角色编排器，取代 `generate_chapter` 中「单模型 inline 改写」的质量环节。`apply_chapter_quality_gate` 退化为管线的**第三棒**，不再是唯一质量环节。

```
主写手 drafter
   └─ 出全章初稿（现有生成逻辑，基本不动）
         ↓ 初稿正文
逻辑与剧情核查 logic_critic   ← 新增核心
   └─ 独立 context：只喂【本章正文 + 本章大纲 + 紧邻前章末尾片段】
   └─ 不喂全书记忆/世界观长文（隔离 = 只盯「这一章内部自洽」）
   └─ 产出结构化 issue 清单（空间方向/画面重述/跨度突变/动作链/道具状态）
         ↓ issue 清单（clean=true → 跳过改写）
   └─ drafter 定向改写（只动 quote 命中处，不重写全章）
         ↓ 修订稿（逻辑回环最多 2 轮，见下）
用词与格式核查 prose_polish   ← 复用现有，零改动
   └─ apply_chapter_quality_gate（Humanizer + QMAI + 机械门 + persist-on-block）
         ↓ 终稿
echo 回主流程：只回传【终稿正文 + 精简报告】
   └─ 各角色中间推理/上下文不进主 SSE、不进主对话上下文
```

### 组件与职责

| 组件 | 文件 | 职责 | 依赖 |
|------|------|------|------|
| 编排器 | `services/chapter_pipeline.py`（新） | 串联三棒、逻辑回环、降级、组装 echo 报告 | logic_critic、quality_gate、run_text/structured_prompt |
| 逻辑核查 | `services/logic_critic.py`（新） | 构造隔离 context、调 LLM、解析结构化 issue | run_structured_prompt、prose_quality 既有 JSON 修复 |
| 用词核查 | `services/chapter_quality_gate.py`（既有，不动） | 第三棒 | 原样 |
| 角色路由 | PromptAsset / `task_type` | `drafter`/`logic_critic`/`prose_polish` 各自配 endpoint/model/prompt | 现有 LLM 路由机制 |

### 命名

三角色作为 `task_type` 走现有 PromptAsset 路由（与 `critic_hard`/`rewrite` 同机制）：`drafter` / `logic_critic` / `prose_polish`。方便在 DB 里独立配 endpoint/model/prompt。

## 逻辑核查 agent：检测维度与 issue 结构

LLM 角色为主、轻量正则为辅（空间方向矛盾需语义理解，纯正则做不到——这正是现有 checker 漏掉的原因）。检测清单写进 logic_critic 的 prompt：

1. **空间方向一致性** ← ch1 消防通道上/下矛盾。同一移动段落里方向词（上/下/进/出）与目标是否自洽。
2. **画面重述 / 草稿叠写残留** ← ch1 骨架二次描写。同一对象/场景在相邻段落被高相似度重复描写；二次出现应只保留增量信息。段落级语义判重（与 Humanizer 的 `synonym_cycling` 同源，但那是句法层，这里是段落语义层）。
3. **空间/时间跨度突变** ← ch1 五楼→才下去半层→三楼。位置/楼层/时间无过渡跳变。
4. **动作因果链断裂**（通用扩展）。动作的前置条件在文中未出现就直接发生（`action_causality_count` 的语义版）。
5. **道具/状态连续性**（章内）。同一道具/身体状态章内前后矛盾。

### issue 结构（结构化输出）

```json
{
  "issues": [
    {
      "dimension": "spatial_direction",
      "severity": "high",
      "quote": "原文命中片段（精确，供定位）",
      "problem": "通道既写通向地面又写向地下延伸，方向矛盾",
      "fix_hint": "统一为逃向地面，删去'往下跑''向地下深处延伸'"
    }
  ],
  "clean": false
}
```

- `dimension` ∈ 上述 5 类。
- `quote` 必须是原文精确片段 → 定向改写只动命中处，不重写全章（省 token、防越改越偏、避免空耗循环）。
- `severity` 仅用于排序与是否值得再跑一轮，**不做硬门**（诊断 + 定向修，不是 pass/fail 闸）。
- `clean: true` 是常态快速路径：多数章无硬伤，核查一次说 clean 就直接进用词棒，省一次改写调用（限流友好）。

## 改写回环：轮次与终止

```
初稿 → logic_critic 第1次核查
  ├─ clean=true  → 直接进用词棒（0 次改写，最省）
  └─ clean=false → drafter 定向改写（只动 quote 命中处）
                     → logic_critic 第2次核查（复核上次 issue 是否消除）
                        ├─ 消除/降级 → 进用词棒
                        └─ 仍有 high  → 最多再 1 轮，然后强制进用词棒
```

**终止条件**（任一触发即停止逻辑回环）：

1. `clean=true` 或无 high-severity issue；
2. 达到 `LOGIC_CRITIC_MAX_ROUNDS`（默认 **2**，env 可调，沿用 `CHAPTER_MAX_REWRITE_ROUNDS` 范式）；
3. **无改善 plateau**：本轮 issue 数 ≥ 上轮（借鉴 quality_gate 现有 `no_improvement` 逻辑，防空耗 throttle）。

终止后**无论是否还剩 issue 都进用词棒**——逻辑核查是「尽力修」不是阻断闸。剩余 issue 写进 echo 报告，前端可见「还有 N 处逻辑存疑」，作者自行定夺。

## 与现有 quality_gate 的接缝

- `apply_chapter_quality_gate` **签名和内部完全不动**，作为管线第三棒，照常跑 Humanizer+QMAI+机械门+persist-on-block。
- 新编排器 `run_chapter_pipeline()` 包在外面：`drafter → logic loop → apply_chapter_quality_gate`。
- `generate_chapter` 的调用点（generate.py:~446）从「直接调 quality_gate」改为「调 run_chapter_pipeline」，**SSE 事件名保持兼容**（前端不改），新增一个 `logic_critic_done` 事件携带逻辑报告。
- **开关** `CHAPTER_PIPELINE_ENABLED`（默认开，可一键回退到纯 quality_gate 老路径）。

## Echo 契约（不污染主流程）

- 回主 SSE 流的只有：**终稿正文 + 精简 JSON 报告** `{logic_rounds, logic_issues_remaining, prose_gate_status}`。
- logic_critic 的完整推理、中间稿、它读的上下文——全部留在编排器内部，**不进**主 SSE、不进主对话上下文。

## 降级策略（限流容错）

每棒独立 try/except + 退避重试：

- logic_critic 调用失败/解析失败 → **跳过逻辑回环**，初稿直接进用词棒（不阻断整章）。
- drafter 定向改写失败 → 保留上一稿，进用词棒。
- 用词核查失败 → persist-on-block（沿用现有兜底）。
- 绝不因某一棒挂了丢整章。

## 错误处理与边界

- logic_critic 返回非法 JSON：复用项目既有 `json_repair` + strict retry 路径；仍失败则视为「核查不可用」走降级。
- `quote` 在正文中找不到（模型臆造片段）：该 issue 标记为 `unlocatable`，不参与定向改写，但仍计入 echo 报告。
- 空初稿 / 超短稿：跳过逻辑核查（无意义），直接进用词棒（其已有最短长度门）。
- `CHAPTER_PIPELINE_ENABLED=0`：`run_chapter_pipeline` 直接转调 `apply_chapter_quality_gate`，行为与今日完全一致。

## 测试策略

全部 TDD，打 dev 真库的测试需带清理（沿用项目惯例）。

1. **logic_critic 单元**（mock LLM）：
   - 喂 ch1 三类问题的最小复现文本，断言对应 `dimension` 的 issue 被解析出来。
   - clean 文本 → `clean=true`、空 issues。
   - 非法 JSON → 走 repair → 仍失败 → 返回「不可用」哨兵。
   - 臆造 quote → 标 `unlocatable`。
2. **编排器单元**（mock 三棒）：
   - clean 初稿 → 0 次改写，直达用词棒。
   - 有 high issue → 定向改写 → 复核消除 → 进用词棒，`logic_rounds==1`。
   - plateau（issue 数不降）→ 第 2 轮后强制进用词棒。
   - `LOGIC_CRITIC_MAX_ROUNDS` 封顶。
   - logic_critic 抛异常 → 降级，初稿进用词棒，管线不崩。
   - `CHAPTER_PIPELINE_ENABLED=0` → 等价于直调 quality_gate（断言调用路径）。
3. **echo 契约**：断言回传报告只含约定字段，不含中间稿/角色推理。
4. **回归**：现有 642 pytest 全绿；`apply_chapter_quality_gate` 既有测试零改动通过。
5. **前端兼容**：现有 SSE 事件消费不受影响（新增事件可选消费）。

## 非目标（YAGNI）

- 不做真并行扇出（限流约束）。
- 不改 `apply_chapter_quality_gate` 内部。
- 不在本子项目做弧式增量循环（那是子项目 A）。
- 不引入新的跨章一致性检查（geo_jump 等已存在，与本章内核查正交）。
- 不做前端大改（仅可选消费新事件）。

## 验证与交付口径（项目惯例）

- `cd /root/ai-write/backend && .venv/bin/python -m pytest`（注意 cwd 与 `.env` 加载顺序；`CHAPTER_MAX_REWRITE_ROUNDS` 已在 conftest 钉住）。
- 前端 `npx tsc --noEmit` 零输出；eslint 仅要求零新增。
- live 容器跑旧码，生效需 `docker compose up -d --build backend`（用户授权后再动；celery 勿擅动）。
