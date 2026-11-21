# 项目工作流(按代码实况,2026-11-21)

三层形态:**后端**(FastAPI + PG + Redis + Qdrant + Neo4j + Celery)、**前端**(Next.js 桌面/移动两套视图)、**离线评估工具**(`backend/scripts/quality_eval`,不依赖 FastAPI)。

---

## 1. 一次性准备

| 项 | 说明 |
|---|---|
| 模型端点 | 按 tier 配置(flagship / embedding);密钥加密存储,解不开的端点**只禁用自身**,不拖垮整次配置加载 |
| 提示词资产 | `prompt_assets` 幂等种子(有则更新,不重复插) |
| 向量库 | 集合维度随 `EMBEDDING_DIMENSION`(env > 配置 > 2048),换模型只改一处 |
| 参考文笔(可选) | 采样 `make_style_sample` → 导入蒸馏 `ingest_reference_books` → 剂量画像 `style_dosage_from_sample` → 风格卡规则写回 `StyleProfile.rules_json` |

---

## 2. 全书规划(一次性,可迭代)

1. **项目与立意** —— 题材、目标规模、premise
2. **书纲** `outline_book`(单卷章数上限 200)
3. **卷纲** `outline_volume`
4. **章纲** `outline_chapter` —— 含时间声明、到期义务、伏笔计划
5. **弧规划** `arc_planning` —— 方向、人物材料、世界规则;每步产物**绑定来源**可审阅,坏来源停止而非静默沿用旧值

规划阶段的共同约束:所有写入**先提交再派发**下游;恢复语义严格(attempt/run 可查)。

---

## 3. 逐章生成循环(核心)

**两个入口,同一条流水线**:

| 入口 | 路由 | 特征 |
|---|---|---|
| 桌面 SSE | `POST /generate/chapter` | 实时 yield、心跳、可中断;不经过 Celery |
| 后台任务 | `POST /generate/async` → Celery `tasks.*` | 进度轮询、可取消、瞬时故障保留 `pending` |

两者都走 `scene_orchestrator`:

### 3.1 场景规划 `plan_scenes`
输入:上下文包、本章到期义务、时间声明、伏笔计划 → 输出场景清单(含 `time_delta`)。

### 3.2 逐场景写作 `write_scene_stream`
写手 user content 的拼接顺序(实况):

```
上下文包 ctx_block
人物表 name_roster_block
场景合同 contract_block
认知台账 ledger_block        ← 角色此刻知道什么
数值/金额 numeric_block      ← 金额钉子
时间钉子 time_block          ← 大纲声明的"次日清晨/一周后"
已确立事实 fact_block        ← 欠谁多少 / 期限 / 条件(2026-11-20 新增)
前文 prior_block
作者要求 instr_block
```

系统层另有 background(题材/文风);输出为流。

### 3.3 章级篇幅(写完后)
超分配(送模章目标 ×1.08)→ 章级补足(不足真实目标才补 ≤1 次)→ 验收线 ≥95% 目标。

### 3.4 净化(落库前)
`sanitize_prose`:元叙事泄漏(章节号/大纲标签)+ 服务话术/中转诊断(位置无关、引号内豁免)。
任务路径额外在**质量门之前**净化一次,使含话术的正文拿不到质量分。

### 3.5 落库
`Chapter.content_text` 与 `ChapterVersion`(active)**同一事务**;空稿不可标 `completed`。

### 3.6 章后投影(顺序,实况)
1. 摘要 `summarize_and_save_chapter`
2. 实体/关系/位置抽取 → Neo4j `_dispatch_chapter_entities`
3. 卷桥接 `_backfill_volume_summary_safe`
4. 认知台账(角色知道了什么,带证据定位)
5. 金额台账 + 数值检查
6. 伏笔缺铺垫检查(仅告警)
7. 事实/经济一致性检查(仅告警)

投影失败**不影响正文交付**;每步自开会话、独立成败。

### 3.7 评审 `ChapterEvaluator`
- 五维评分:情节 / 人物 / 文风 / 节奏 / 伏笔
- **自洽性判据**(2026-11-20 新增):`continuity_fact_violation`(与已确立事实冲突)、`causal_chain_violation`(因果断链)、`dangling_setup_violation`(铺垫无回收)
- 每条 issue 要求 `quote` **逐字定位**;无定位不升格为硬失败

---

## 4. 验收与修订

| 结果 | 条件 | 后果 |
|---|---|---|
| `completed` | 评分 ≥ 阈值(默认 8.2)且无 hard issue | 可继续下一章 |
| `needs_review` | 有逐字定位的因果/事实类 hard issue | 桌面提示,可人工改 |
| `draft` | 拒答/截断/质量门拦截/空稿 | 保留可读正文,不算完成 |

修文链:`auto_revise` → 质量门对比(**候选比原稿更糟即拒**,critical/high 分层)→ 修订终止条件(保留原候选与原因)。

---

## 5. 导出与读者侧

EPUB / PDF / DOCX / TXT;中文标题走 RFC5987。**读者侧判断由用户负责**,系统只保证事实自洽与机制不退化。

---

## 6. 离线评估工具链(`scripts/quality_eval`)

| 工具 | 用途 |
|---|---|
| `cases` / `detectors` | 逐条检测:POV 越界、指令泄漏、事实违规、比喻堆叠… |
| `judge` / `cross_judge` | 双盲 A/B(逐章逐评审哈希随机化) |
| `continuity_probe` | 真实长跑;`--style-profile` 绑文笔卡(找不到即失败,不静默退化) |
| `longrun` / `sbs_report` | 篇幅曲线 / 逐章左右并排 |
| `build_gallery` | 自包含总览页(按题材/版本/模型筛选) |
| `arm_judge` | 双盲评审两臂 |
| `audit_partition` / `audit_coverage` | 全仓审计分区与覆盖率核验 |

---

## 7. 守卫清单:哪一层挡什么

| 层 | 机制 | 触发后 |
|---|---|---|
| 入口 | 端点/路由解密失败隔离 | 只禁用该端点 |
| 写手输入 | 时间钉子、数值钉子、已确立事实 | 预防(不阻断) |
| 生成中 | 流式重试(首块前失败重试)、连续错误上限 | 重试 / 中止该章 |
| 落库前 | `sanitize_prose` 净化、空稿判定 | 剥污染 / 判 draft |
| 落库 | 正文与版本同事务、作者改稿基线+行锁 | 不覆盖作者文本 |
| 章后 | 伏笔缺铺垫、事实一致性、认知/金额台账 | 告警 + 落 metadata |
| 评审 | 五维评分 + 三类自洽性判据 | `needs_review` |
| 修文 | 新增严重问题即拒、终止条件保留候选 | 拒绝更差的候选 |
| 每章预算 | token 闸门(默认关) | 超限停 |
| 成本 | 用量落库(含缓存命中与估算标记) | `/usage` 可查 |

---

## 8. 一条命令跑通一条链(实例)

```bash
# 真实长跑 20 章(内置题材),带文笔卡
python -m scripts.quality_eval.continuity_probe \
  --database-url "$DB" --out docs/evaluation/run-x \
  --style-profile "龙族+天之炽 综合文笔样本 综合写法" \
  --chapters 20 --words 2000 --model gemini-3.1-pro

# 篇幅曲线 / 逐章对照 / 总览页
python -m scripts.quality_eval.longrun --dir docs/evaluation/run-x
python -m scripts.quality_eval.sbs_report --old A --new B --out _public/x.html \
  --old-label 旧 --new-label 新
python -m scripts.quality_eval.build_gallery --out _public/gallery.html
```

后端全目录验证:`pytest --offline -q -p no:cacheprovider tests`
