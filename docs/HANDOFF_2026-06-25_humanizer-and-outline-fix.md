# HANDOFF 2026-06-25 — Humanizer-zh 接入 + 大纲生成致命 bug 修复 + 子项目 B 设计/计划

分支：`rescue/2026-05-17-baseline`（事实主线）

本轮三件事：① 接入 Humanizer-zh 结构性去 AI 味规则；② 定位并修复「大纲生成回退到 prompt」致命 bug；③ 为「多智能体章节质量管线」（子项目 B）产出设计 spec + 实现计划（尚未实现）。

---

## 1. Humanizer-zh 结构性去 AI 味规则（已交付，commit `2a4cdc1`）

**动机**：项目已有两层去 AI 味设施——QMAI 词表（`prompts/anti_ai_rules_zh`，管网文腔陈词滥调/机械小动作/情绪直陈）与 `AntiAIChecker`（管密度统计：AI 词密度、的字、四字成语、句式单调）。Humanizer-zh（源自 Wikipedia「Signs of AI writing」/ op7418 中文化）补的是**第三层：句法骨架级 AI 指纹**——单看每个词都正常，组合成固定结构才露马脚，前两层查不到。

**新增模块** `backend/app/services/prompts/humanizer_zh_rules.py`，7 条结构性规则：

| rule_id | 中文 | 可正则检测 | 说明 |
|---------|------|-----------|------|
| negative_parallelism | 否定式排比 | ✓ | 「不是X，而是Y」「与其说…不如说…」 |
| shallow_significance | 句尾强行升华 | ✓ | 「象征着/标志着/预示着…」收尾贴意义标签 |
| synonym_cycling | 同义词循环/同画面重述 | prompt only | 同一对象换称呼轮换；同画面重述（正对神裔 ch1 草稿叠写痕迹） |
| rule_of_three | 三段式法则 | prompt only | 硬凑三项并列显全面 |
| copula_avoidance | 系动词回避 | ✓ | 「作为…的存在」「堪称」替代简单「是/有」 |
| over_qualification | 过度限定 | ✓ | 叠加「可能/也许/大概/似乎」 |
| false_range | 虚假范围 | ✓ | 「从X到Y，从A到B」空泛扫描 |

**接法**（沿用 QMAI 既有模式，零新机制）：
- `render_humanizer_prompt_block()` 追加进 `prose_quality_rules.render_prose_quality_prompt()` 末尾——**单一注入点**，同时进生成与润色 prompt。
- `scan_humanizer_structural()` 做确定性正则检测，`AntiAIChecker._check_humanizer_structural` 产 `humanizer_*` issue。**只标 low/medium，不做硬门**——诊断用，避免过度返工（与神裔已苦战的 rewrite 触底循环正交）。

**测试**：`tests/services/test_humanizer_zh_rules.py`（17 例），全套 **642 pytest 绿**（含修掉的一个 pre-existing 测试隔离 bug，见下）。

**顺带修的 pre-existing bug**：`chapter_quality_gate._DEFAULT_MAX_REWRITE_ROUNDS` 在 import 时读 `CHAPTER_MAX_REWRITE_ROUNDS`，`.env` 是 2 但全套跑时模块先于 `.env` 加载、默认成 5，导致两个 gate 用例时挂时过。已在 `conftest.py` 用 `setdefault` 钉死为 2。

---

## 2. 「大纲生成回退到 prompt」致命 bug（已修复+验证，commit `39ca573`）

### 症状
点击大纲生成 → 不出正文，前端显示 prompt/错误文本。

### 根因（已复现、已定位到源头）
- `OutlineGenerator.__init__`（`outline_generator.py:560`）调用**同步** `get_model_router()`。
- 在 SSE 请求的 async 事件循环里，该函数命中 `model_router.py:1368` 的 `loop.is_running()` 分支 → `pass`，**不加载 DB 配置**，只回退到 `_load_from_env()`。
- 本部署是**纯 DB 配置**（3 个 endpoint 在库），**容器内所有 env LLM key 都空**。于是 env 回退得 `providers={}` → `_get_route` 抛 `No model configured for 'outline_book'`，被 SSE 当 `{"error": ...}` 发出 → 前端渲染成「回退到 prompt」。
- 对照：章节生成走 `run_text_prompt` → `get_model_router_async()`（`:1336`，强制 `await load_from_db()`），**不受影响**。outline 是唯一用同步 router 的生成路径。

### 修复
在 `generate_outline` 的 SSE 生成器里，实例化 `OutlineGenerator` 前 `await get_model_router_async()`，治愈共享单例 `_router`（两个 getter 同一 global）。容器内已证：即便 `_router=None`（失败前置条件），补丁路径也恢复 2 个 provider + `outline_book` 路由。

### 测试
`tests/test_outline_router_preload.py`（2 例：源码断言 preload 存在 + 证明 async-load 治愈 sync getter）。全套 **644 pytest 绿**，零回归。

### ✅ 部署完成
已 `docker compose up -d --build backend`（celery 未动），live 端到端验证 outline 流恢复正常正文输出，不再回退 prompt。

---

## 3. 「点击项目弹回列表」bug —— 非后端缺陷
- 后端对所有有效项目 + 有效 token 返回 200（实测神裔/「123」/新建项目全 200；create→立即 GET→200，无 phantom id）。
- 弹回是前端 `DesktopWorkspace.tsx:332` `.catch(() => router.replace('/'))`：项目加载任何失败都踢回列表。`apiFetch` 对 401 是跳 `/login`（不是列表），所以弹列表是非 401 失败。
- 日志里被打 32× 404 的 `9ac37da5` 项目在库里**根本不存在**（无任何残留行）= 前端缓存的死项目 id。
- **现象已自行恢复**（用户反馈「现在又能点进去了」），符合陈旧 token/缓存诊断：重登/刷新即清。无需后端改动。

---

## 4. 子项目 B：多智能体章节质量管线（**已实现 + 已部署 live**）

- **设计 spec**：`docs/superpowers/specs/2026-06-23-multi-agent-chapter-pipeline-design.md`
- **实现计划**：`docs/superpowers/plans/2026-06-23-multi-agent-chapter-pipeline.md`（12 个 TDD 任务，全部完成并勾选）

**要点**：串行三角色 `drafter → logic_critic → prose_polish`，新增「逻辑与剧情核查」角色专查神裔 ch1 暴露的章内缺陷（空间方向矛盾/画面重述/跨度突变/动作因果/道具状态）——这些是现有 checker（geo_jump/continuity 跨章、mechanics 抓解释重复）的真实盲区。串行因 relay 限流（并行扇出撞墙）。`apply_chapter_quality_gate` 零改动作第三棒。`CHAPTER_PIPELINE_ENABLED=0` 一键回退。echo 只回 `{logic_rounds, logic_issues_remaining, logic_available, prose_gate_status}` 不污染主流程。

**落地文件**：
- `backend/app/services/logic_critic.py`：`LogicIssue`/`LogicCriticReport`、隔离 context 构造、结构化输出解析（unlocatable quote 标记）、`run_logic_critic`（短稿跳过 + 任何失败降级 available=False）。
- `backend/app/services/chapter_pipeline.py`：`ChapterPipelineResult.to_echo_report()`、`build_targeted_rewrite_content`（只列 locatable）、`apply_targeted_logic_rewrite`（失败返 None）、`run_chapter_pipeline`（clean 快路径/定向改写/plateau/`LOGIC_CRITIC_MAX_ROUNDS` 封顶/降级/开关旁路）。
- `prompt_registry._TASK_TYPE_FALLBACK`：`logic_critic→critic`、`drafter→rewrite`（未配 PromptAsset 时开箱即用）。
- `generate.py:445`：质量环节从直调 gate 改为 `run_chapter_pipeline`，新增 `logic_critic_done` SSE 事件；下游 persist-on-block 逻辑零改动（`quality_gate_result = pipeline_result.quality_gate_result`）。
- `conftest.py`：钉 `LOGIC_CRITIC_MAX_ROUNDS=2` / `CHAPTER_PIPELINE_ENABLED=1`。

**测试**：`tests/services/test_logic_critic.py`（10 例）+ `tests/services/test_chapter_pipeline.py`（12 例）。全套 **666 pytest 绿**。`test_generate_sse_fallback` 因 gate 下沉一层，patch 点同步加 `chapter_pipeline.apply_chapter_quality_gate`（架构反映性更新，非回归）。

**实现顺序**：B（质量管线）已先于 A（弧式增量循环）落地——A 的「写一章」步骤将直接复用 B 的 `run_chapter_pipeline()`。

---

## 5. 调研：两个外部仓库的可借鉴点
- **worldwonderer/oh-story-claudecode**：文件系统即记忆（对话只创作不记忆）；`guard-outline-before-prose.sh` 钩子——无章节大纲禁止写正文（强制大纲先行，正合子项目 A 哲学）；7 agent 按模型分层（architect→Opus、checker→Haiku）印证子项目 B。
- **dama-cyber/magic-distillation**：七步单任务 prompt 拆分（一 prompt 一职责）；**可量化原创性闸**（5-gram 重叠<5%、最长公共子串<8字）；逐段字数预算 ±5%；分级黑名单（硬/软）。⚠️ 其核心目标「反 AI 检测」不可取，仅借鉴节奏/原创度量化与 prompt 拆分工程。

---

## 下一步（建议优先级）
1. ~~部署 outline 修复~~ ✅ 已 `docker compose up -d --build backend` 部署并端到端验证。
2. ~~执行子项目 B 计划~~ ✅ 12 个 TDD 任务全部完成、666 pytest 绿、已部署 live。
3. **子项目 A**（弧式增量循环）：B 已落地，可开新 spec→plan→实现循环。A 的「写一章」步骤直接复用 `run_chapter_pipeline()`。
4. **配 logic_critic / drafter 专属 PromptAsset**（可选）：当前走 fallback（→critic/→rewrite），可在 `/prompts` 为这两个 task_type 绑定专用 prompt + endpoint 以进一步调优逻辑核查质量。
