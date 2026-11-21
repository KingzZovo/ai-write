# 与 AI-Novel-Writing-Assistant 的两边对比(2026-11-17)

对比对象:[`ExplosiveCoderflome/AI-Novel-Writing-Assistant`](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant)(下称"他们",产品名 Biz Novel Studio)与本仓库 `/root/ai-write`(下称"我们")。

**证据边界**

- 他们:读完 README 全文、GitHub API 元数据、递归文件树(4094 个 blob)、`LICENSE`、3 个关键源文件(`state/stateConflictDetection.ts`、`novel/runtime/proseQuality/ProseQualityDetector.ts`、`payoff/payoffLedgerShared.ts`)、1 篇开发 wiki。**没有克隆、没有跑他们的代码、没有读过他们的 prompt**。凡超出这个范围的判断都标注为"未核实"。
- 我们:本仓库代码 + 这两天的实测数据(离线回归、交叉盲评、100 章长跑、上下文 trace)。

---

## 0. 客观规模与形态(实测于 2026-11-17)

| 项 | 他们 | 我们 |
|---|---|---|
| 语言 / 仓库体量 | TypeScript,59.8 MB,4,094 个文件 | Python + TS,`backend/app` 83,220 行 / 378 个模块 |
| 测试 | 690 个测试文件(server 623 + client 62 + 其他) | 413 个测试文件、3,437 个 test 函数、72,880 行 |
| 迁移 | Prisma `server/src/prisma` 下 **236 个 `.sql`** | Alembic **82** 个,单 head |
| Star / Fork / Issues | **3,486 / 658 / 125 open** | 未发布 |
| 活跃度 | 创建 2025-02,今天(2026-11-17)仍在推送,v0.8.7 | 本地工作树 |
| 分发形态 | **五平台桌面安装包**(Electron)+ 官网 biznovelstudio.com + 文档站 | `docker-compose` + Next.js 16.2.3 前端(201 个源文件),**无桌面、无安装包、无官网** |
| 技术栈 | React 19+Vite / Express 5+Prisma+Zod / LangChain+LangGraph / SQLite(默认)+PG / Qdrant(可选) / Electron / Plate | FastAPI / Next.js 16 / PG+Redis+Qdrant+**Neo4j** / Celery |
| 起步依赖 | **SQLite 默认 + `RAG_ENABLED=false` 可先跑通创作** | PG + Redis + Qdrant + Neo4j 四件套 + 一个 embedding 服务 |
| 许可 | **双授权**:AGPL-3.0-only(默认)+ 服务式商用需单独商业授权 + CLA(GitHub API 的 `spdx` 显示 `NOASSERTION`,以 `LICENSE` 正文为准) | **无 LICENSE 文件** |

规模上一个有意思的对照:他们 4,094 个文件里 server 2,372 / client 887 / docs 375,`server/src/services/novel` 一个目录就 456 个文件、`novel/director` 131 个文件 1.32 MB。**他们不是"壳",工程厚度和我们同一量级**,只是厚度分布在产品层(界面、桌面、路由、合约测试)而不是后端机制层。

---

## 1. 逐维度对比

| 维度 | 他们 | 我们 | 判断 |
|---|---|---|---|
| **分发与上手** | 装包双击即用,官网/文档站/QQ 群/更新说明齐备 | 源码 + compose,得先起四个基础设施 | **他们明显强** |
| **新手路径** | 灵感 → 候选方向 → 开书;热门题材雷达;拆书开书 | 得先理解 arc/卷/章纲/场景/run | **他们明显强** |
| **内容形态** | 长篇 + **短篇 3k–30k 独立全流程** + 漫画分镜 + 短剧素材 + Creative Hub 对话引导 | 只有长篇正文一条线 | **他们明显强** |
| **授权范围表达** | "提交第 1–3 章,完成后不会擅自写第 4 章";全自动 / 按阶段确认;状态显示在书名旁;暂停原因可见 | run / batch / attempt / 提交确认,恢复语义更严,但产品语言更工程 | 他们更**易懂**,我们更**严格** |
| **状态回流** | `ChapterArtifactDeltaService`(48 KB,章级产物增量)、`ChapterArtifactBackgroundSyncService`、`ChapterTimelineFinalizationService` | 章后投影:认知/实体/关系/位置/金额,绑定来源章 | **打平**(都做) |
| **冲突检测** | `stateConflictDetection.ts` 5 类:`character_goal_shift` / `relation_jump` / `information_regression` / `foreshadow_regression` / `foreshadow_missing_setup`;另有 `OpenConflictService` 管未闭合冲突 | 认知"已知→未知"回退、证据定位校验、关系方向校验、章序校验;**未核实**是否有等价的 5 类命名 | **他们更成体系**(冲突是显式产品对象) |
| **伏笔** | `payoff/` 8 文件 50 KB:`PayoffLedgerSyncService` + 来源生命周期 + 章引用 | 发现 → 候选 → 作者确认 → 图谱确认 → 删除留存,全生命周期带正文引文 | **我们更深**(证据粒度) |
| **证据粒度** | 冲突判定输入是**归一化字段快照做字符串比较**(`normalizeText` + `valuesDiffer`),不做原句回位 | 每个认知/伏笔/关系条目带**逐字引文**并回正文定位(标点归一化) | **我们更深** |
| **场景/章节契约** | 有运行包(runtime package)与章节产物契约,**未核实**是否有逐场 20 字段级契约 | 13 个必填 + 7 个条件字段,规划失败走有界修复 | **我们更细**(但也就更重) |
| **正文质量检测** | `ProseQualityDetector` 9 个 code:`negative_flip` / `dash_or_ellipsis` / `period_stutter` / `long_paragraph` / `verbatim_repeat` / `truncation` / `ai_self_reference` / `placeholder_leak` / `engineering_term_leak`,带 severity + fixSuggestion,上限 8/类 40/次 | `quality_eval/detectors.py` 7 类:`pov_breach` / `instruction_leak` / `rule_recitation` / `metaphor_stack` / `fact_violation` / `outcome_unfulfilled` / `unauthorized_new_character` + 指标(比喻密度/复句比/AI 短语) | **互补**:他们抓"输出泄漏与手感",我们抓"设定违约与越界" |
| **修文** | `repair/chapterRepairRuntime` + **`detectNewSevereProseFindings`:修文不得引入新的 high/critical 问题** | `chapter_quality_gate` 只做**结构清理**(正文行变了就拒 `prose_changed`),风格重写走 `auto_revise.py`;候选接受判据是 penalty 变好 + 长度比 | **他们那条硬判据更好用**(见 §4) |
| **坏输出防线** | 有 `prose_truncation` 检测与输出额度策略;**未核实**是否有中转诊断词识别 | 8+ 诊断词(`terminal_empty_incomplete`/`quota_exhausted`/`model_not_supported`/`account_needs_verification`/`[错误]` 前缀…)+ 流开头即拒 + 落库判 draft | **我们更深** |
| **评测与可复现** | `docs/evaluations/` 只有 2 篇(beta-integration、style-risk-review)+ 1 个 `rag-retrieval-eval.cjs` | 固定样本集 + 检测器 + 独立 LLM 评审 + **第二模型交叉盲评**(28/28 一致)+ 连续章探针 + 100 章长跑曲线 + 逐块上下文 trace | **我们明显强** |
| **成本可见性** | **AI 用量页**(按章/步骤/厂商/模型/状态筛选)、总 token 口径明确、缓存命中不计二次、**单章 100k token 上限 + 达上限可自定额度续跑**、`novelTokenUsageSummary` | `llm_call_logs` + 缓存计数 + 全局计费点,**无聚合、无面板、无每章上限** | **他们明显强** |
| **检索** | Qdrant **可选**,知识库/向量设置/导入索引/独立检索评测脚本 | Qdrant 必装 + 认知召回深度接入;本轮补了 embedding 不可用的降级兜底与索引回填,并踩过"换嵌入模型即召回归零" | **他们更稳**(可选),我们更深 |
| **运维门槛** | Node 一套语言 + SQLite 默认;Prisma 迁移 236 个 | Python+TS 双栈 + 四件套 + Celery;82 迁移,刚修过双头 | **他们明显轻** |
| **恢复语义** | 任务/检查点/恢复 + 失败卡一键恢复(关深度思考/提高上限原地重试)+ 导演接管(`novelDirectorTakeover`) | run/attempt/批次 + 固定请求 UUID + 预算不重置 + 来源证明 + 物理连接绑定 | **打平**(各有实测) |
| **社区与生态** | 3.5k star、658 fork、趋势榜、配套 Ani Book Skill(Codex 侧) | 无 | **他们明显强** |
| **许可与可复用** | 双授权,代码有传染性,商用需授权;有 CLA | **无 LICENSE**,既不能对外授权也不能合规接收贡献 | **都有坑**,方向相反 |

---

## 2. 推翻我早前的两个判断(自我修正)

我之前只看 README 时写下过两句,读了代码后**是错的**:

1. "他们没看到证据化状态与冲突检测这一层" → **错**。有 `stateConflictDetection`(5 类冲突)+ `OpenConflictService` + 章级产物增量 + 时间线收尾 + 伏笔台账。他们的状态回流是**显式产品对象**,不是隐含在 prompt 里。
2. "他们 AGPL-3.0" → **不完整**。是**双授权**:AGPL-3.0-only + 服务式商用单独授权 + CLA。这意味着他们的贡献者代码可以被纳入商业授权,而我们的 fork 一旦合入就永远无法闭源商业化——**这比"AGPL 传染"更严格**。

同样要修正"我们唯一的护城河是一致性机制"这个说法:**一致性机制两边都有**,差别在证据粒度(逐字回原句 vs 归一化快照比较)和冲突的显式程度(我们散在各校验器,他们是独立冲突对象 + 未闭合冲突处理)。

---

## 3. 我们真正独有的(逐条有代码依据)

1. **逐字证据 + 来源证明复用**:认知/伏笔/关系条目回正文定位,1006 章规模实测冷读 6 批、热读 0 正文。
2. **场景合同 13+7 字段** + 规划层金额收敛(`money_plan.reconcile_money_totals`)+ 章内金额台账(同名目跨章比对)。
3. **上游坏输出防线**:中转/配额/账号/模型不支持/中文错误前缀 8+ 诊断词,流开头就拒,且落库状态区分 `draft`。
4. **可复现评测制度**:固定样本集、交叉盲评(一致率 1.0)、连续章探针、100 章长跑曲线、上下文逐块 trace(budget/omitted)、token 换算实测校准(1.43–1.48 汉字/token,更正了旧的 4.00)。
5. **迁移与恢复的工程纪律**:单 head、幂等 DDL、downgrade 去重、`alembic_sync` 五态判定永不执行 DDL、预算不重置、固定请求 UUID。

---

## 4. 值得向他们借的三条(按性价比排序)

**① 用量与预算可见性(性价比最高)**
他们有"单章 100k token 上限 + 达上限按建议或自定额度续跑 + 用量页按章/步骤/厂商筛"。我们的成本只活在 `llm_call_logs` 里,作者看不见,我们自己也看不见聚合。
落点:后端加聚合查询(按项目/章/任务类型/模型)、前端加用量页、生成链路加"本章累计超阈即停并可续"。数据已有,缺的只是聚合与面板。

**② "修文不得引入新的严重问题"这条硬判据**
`detectNewSevereProseFindings(original, candidate)` —— 只比较**新增的 high/critical** 问题;这比"总分变好就接受"精确得多:总分变好但新增一个"AI 自指"是净损失。
落点:[chapter_quality_gate.py](/root/ai-write/backend/app/services/chapter_quality_gate.py) 的 `_candidate_is_acceptable`:现在只看 `_quality_penalty` 是否变小 + 长度比,**没有"新增严重问题即拒"**。改造成本低,收益直接。

**③ 冲突类型的显式化(尤其 `foreshadow_missing_setup`)**
他们把"该埋伏笔的章没铺垫"列为一种冲突。我们有伏笔计划/登记/确认,但**没有"计划了却没在章里落地"的判据**(待核实,若无则是低成本补齐)。
落点:伏笔模块 + 章纲契约,复用现有引文定位。

**附加(产品侧,非质量侧)**:短篇 3k–30k 独立流程。它解决的是"新用户第一小时看不到成品"——我们现在最快也要等 arc+卷+章规划完。

---

## 5. 不建议抄的三条

- **双导演 V1/V2 并存**:两套编排、两套恢复、两套版本隔离规则(他们自己写了 `director-version-isolation.md`)。那是历史包袱,我们对齐的是单一管线 + 可恢复 run。
- **Electron 桌面壳**:那是分发问题不是质量问题。除非要卖给不装环境的人,否则收益全在打包与更新链,不在文本质量。
- **代码本身**:双授权 + CLA,合入即放弃闭源商业化路径。**架构、交互、判据可以借;实现不能复制**。

---

## 6. 双方共同短板

- **都没有读者侧质量证据**。他们 3.5k star ≠ 文本受欢迎;我们有模型评审与机械指标,也不是读者。这条只能靠真人数据。
- **都依赖第三方模型**;我们额外依赖 embedding(本轮才做成可降级),他们 RAG 可选——这点他们稳。
- **我们的评测覆盖不了"好不好看"**:检测器抓违约、评审抓套路,抓不到"读者会不会追更"。

---

## 7. 结论

- **他们是更好的产品**:能装、能上手、能看见花了多少钱、能把小说延展成别的形态、有人用。
- **我们是更深的引擎**:证据回原句、场景契约、坏输出防线、评测制度、恢复纪律。
- **但"引擎更深"不是壁垒**——他们的机制覆盖面已经和我们高度重叠,差距主要在证据粒度与评测制度,而这两点对外部用户不可见。
- 真正该补的:**把已有机制暴露成作者能看懂的东西**(用量、预算、冲突、确认),再加**许可**。

## 8. 如果只能做一件事

做**用量与预算可见性**:作者信任 + 我们自己的成本失控风险,一次解决,90% 数据已在 `llm_call_logs`。