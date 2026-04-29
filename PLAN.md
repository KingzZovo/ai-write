# 项目计划（post v1.8.0）

本文档记录 ai-write 在 `feature/v1.0-big-bang` 主线上的已完成里程碑、当前活动版本、近期 backlog、长期扩展路线，以及明确冻结不再追的旧思路。它不是 `ITERATION_PLAN.md` 的替身；`ITERATION_PLAN.md` 保留历史执行痕迹，而这里维护的是**当前版本语义下的项目事实与路线**。

## 已完成里程碑

### v1.0.0

v1.0.0 把项目从“若干能跑的原型功能”推到“可作为产品底座继续演化”的状态。后端完成多阶段 Docker 构建、`/api/version`、Prometheus/Grafana/Sentry、usage quota、项目导出（EPUB/PDF/DOCX）、LangGraph generation graph、`chapter_variants` 表与 BVSR 候选稿基础、ConStory v1 checker、多 Agent prompt seed；前端完成 Tailwind v4 design tokens、双语脚手架、移动端与可折叠工作区。它定义了 `feature/v1.0-big-bang` 这条主线，也把“生产就绪”第一次拉进了仓库的硬目标。

### v1.1.0

v1.1.0 的重心不在算法，而在作者工作台的可用性。核心交付是业务 UI 向 design tokens 迁移、真实中英 i18n、移动端 landing 与 workspace 重排、项目侧边栏记忆与 `[` / `]` 快捷键，让前端从“功能存在”提升到“高频可用”。这一步使得后续版本新增的复杂控制面板有了稳定容器，而不是继续堆散页。

### v1.2.0

v1.2.0 是观测和工程化加固版。它把结构化 JSON logging、`X-Request-ID`、更完整的 Prometheus 指标、Sentry 脱敏接入，以及 GitHub Actions CI 固化进主线。换句话说，v1.0 做出了“能部署”的骨架，v1.2 才真正把“出问题时能定位、改动后能回归验证”补齐。

### v1.3.0

v1.3.0 的关键词是**字数预算与容量分配**。这一版通过 `a1001300_v13_target_word_count.py` 把 `target_word_count` 正式下沉到 `projects / volumes / chapters` 三层，同时接上 project 级预算分配、volume 级回填与章节目标字数治理。它解决的是“长篇写作不是单章独立请求，而是一个逐卷逐章消耗预算的连续系统”这个建模问题，也是后续讨论 500 万字容量规划时必须回头看的起点。

### v1.4.0

v1.4.0 交付的是 tier-based LLM routing。`llm_endpoints.tier` 与 `prompt_assets.model_tier` 入库后，路由开始按 `prompt.model_tier ≫ endpoint.tier ≫ standard` 解析实际模型；`critic_service`、`settings_extractor` 和 `context_pack` 也相应拆出新 `task_type` 与可控开关。前端新增 `/llm-routing` 矩阵页、settings/prompts 的 tier 可视化。这一版的价值在于把“系统里有很多模型调用”改造成“系统知道每一种调用为什么应该走哪一档模型”。

### v1.4.1

v1.4.1 没改架构方向，但修掉了 v1.4 在可见性与兼容性上的缺口。`/api/llm-routing/matrix` 字段对齐前端真实需要，端点测试不再只回延迟而是回显请求/响应摘要，NVIDIA embeddings 作为新的 provider type 接入。它让“路由”从后端概念变成前端能看懂、能调试、能验证的真实运维面。

### v1.5.0

v1.5.0 是第一个真正改变章节生成主链路的版本，主线有四条：scene-staged writing、auto-revise、prompt cache 双层防死锁、cascade auto-regenerate。`SceneOrchestrator` 进入热路径，`chapter_evaluations`、`evaluate_tasks`、`cascade_tasks` 形成闭环，`prompt_cache` 则把 `prompt_assets` 的热路径读写彻底隔离。今天仓库里关于 scene_mode、auto_revise、prompt_registry 死锁防线的大部分硬规则，都来自 v1.5.0 的生产事故与修补经验。

### v1.6.0

v1.6.0 延续 v1.5 的调用栈，但把焦点放在 provider 侧 prompt cache 与 scene mode observability。Anthropic/OpenAI 的 prompt cache key/flag 接入、`llm_cache_token_total` 指标、scene 规划 fallback 与 revise round 的指标化，都发生在这一版。它没有像 v1.5 那样改变业务流程，但显著提升了“知道系统到底在消耗什么、回退了几次、缓存是否生效”的能力。

### v1.7.0

v1.7.0 是 carry-forward 清账版本。`knowledge_tasks`/`style_tasks` 统一 `_run_async` 调度；Qdrant 孤立 slice 清理脚本落地，把历史 `style_samples_redacted` 的冗余点清掉；cascade_tasks 的只读 API 与前端独立页面也在这版上线。它的价值不是新算法，而是把 v1.5/v1.6 留下的“系统能跑但维护负债很高”逐项削平。

### v1.7.1

v1.7.1 是两处细但关键的补强。第一，`task_type` 从调用方贯穿到 provider 层，解决了大量 `llm_call_total{task_type="unknown"}` 指标退化；第二，Cascade 任务面板不再是独立路由孤岛，而是嵌进 `/workspace` 桌面工作区，具备详情弹窗。这一版继续沿着“把系统能力从后台埋点变成作者和运维都能直接观察的界面”推进。

### v1.7.2

v1.7.2 的核心是把 `time_llm_call` 从局部包裹补成全路径覆盖。`ModelRouter.generate*` 主要入口的 provider 调用点都开始产出 `llm_call_total / llm_call_duration_seconds / llm_token_total`，观测口径从只覆盖 `prompt_registry` 路径扩展到 evaluator、checker、settings_extractor、outline_generator 等旁路服务。这是“看见少量主流程”到“看见完整模型调用面”的分水岭。

### v1.7.3

v1.7.3 是纯 hotfix，但重要性不低。它修复了 `ModelRouter.stream_by_route` 在 `_log_meta is None` 分支里的 `NameError`，避免裸流式调用一上来就炸。该 bug 是通过 v1.7.2 审计基线发现的，说明观测和代码审计开始真正反哺生产质量，而不是停留在“文档里说过会做”。

### v1.7.4

v1.7.4 是 anti-AI 主线从经验补丁走向系统调校的过渡版。它补齐 book/volume outline 注入、章节后摘要、outline→facts ETL、style chain fallback，以及 generation/polishing prompt v3 三支柱升级；更关键的是修掉了 `prompt_registry.run_text_prompt/stream_text_prompt` 在传入 `messages=` 时 silent bypass `route.system_prompt` 的致命问题。没有这次修复，后续所有 prompt 版本升级都会停留在数据库表面而不进真实运行时。

### v1.8.0

v1.8.0 把 anti-AI 路线从“禁令式 prompt”切换成“dosage-driven 风格画像”。《龙族》全本被抽成 16 维剂量数据，落到 `style_profiles.config_json.dosage_profile`，再由 `ContextPackBuilder._render_style_profile()` 注入 system prompt；同时修掉 `style_samples[:3]` 截断导致剂量数字静默丢失的 bug。结果不是某一条规则变严，而是第一次把朱雀 AI 检测打到 `AI 0%`。这版也暴露出新的问题：单向上限会把句长、段长、比喻和心理描写一起压扁，所以 v1.8.1 不能只加更多禁令，必须改成双向区间。

## 当前活动版本（v1.8.1）

当前活动版本是 **v1.8.1**。它不是另起炉灶的大版本，而是沿着 v1.8.0 的数据画像架构，对“事实写回”和“剂量区间”两个现实缺口做收口。

### 阶段 B：凌祝 bug 单点修

现阶段最明确、最该先收掉的问题是“凌祝 bug”。ch9-ch11 的 LLM 输出已经写出“凌祝”，但 `characters` 表没有对应人物档案，结果导致：

- Neo4j 或章节正文里已经出现该角色，PostgreSQL 主事实表却没有；
- `ContextPackBuilder._build_facts()` 主要仍从 `characters.profile_json` 读取人物卡，缺档角色无法稳定进入后续章节上下文；
- 人工验收时会看到“文本里有这个人，数据库里没有这个人”的撕裂状态。

v1.8.1 阶段 B 的执行目标很具体：

1. 往 `characters` 表补齐凌祝的档案事实，而不是只在 prompt 里绕过去。
2. 确认 `ContextPackBuilder` 在当前项目/卷/章节路径上能稳定加载这份档案，并与 Neo4j 的活跃角色集合并。
3. 用 ch10/ch11 回归，验证“钟塔线”和相关人名不再互相矛盾。

### 当前验收候选

当前最适合作为验收样章的是 **ch11《钟塔不只是钟塔》**。理由不是标题好看，而是它同时踩中了角色一致性、跨章事实延续、上下文召回和 dosage 风格控制四条线：如果这章不再出现“角色缺档但正文继续写”的撕裂，说明 v1.8.1 的修补真正打到了结构问题。

## 近期 backlog（v1.9 – v2.0）

以下 backlog 不是“有空再看”的想法池，而是从现有代码、现有表结构、现有事故里直接推出来的主线工作。复杂度按 `S/M/L/XL` 记录。

| 条目 | 描述 | 依赖 | 影响表 | 复杂度 |
|---|---|---|---|---|
| 章末实体写回管线 | 在章节生成完成后追加一轮结构化抽取/比对/落库，把新增或变化的人物、关系、伏笔正式回写到 Postgres，而不是只写 `chapters.content_text` 与 Neo4j。至少覆盖 `characters`、`relationships.evolution_json`、`foreshadows`。 | 稳定的章末抽取 prompt、幂等策略、人工确认策略 | `characters`, `relationships`, `foreshadows`, `chapters`, 可能新增写回审计表 | XL |
| `chapter_versions` Git-like 启用 | 让每次保存/改写/自动修订都能形成真正的版本节点，支持 parent、branch、diff、切换 active，而不是让表常年 0 行。 | 明确写入时机、diff 生成、UI 浏览器 | `chapter_versions`, `chapters` | L |
| `chapter_variants` 比稿启用 | 把 BVSR 留下的表和 API 真正接进生成链路，允许同章生成多候选、评分、选优和回看。 | 生成任务拆分、评分口径统一、前端 variants 视图 | `generation_runs`, `chapter_variants`, `critic_reports` | L |
| 项目章节切块 + Qdrant 向量化 | 参考 `text_chunks` 的模式新增 `chapter_text_chunks`，为项目自身章节建立 chunk 级向量索引，避免长期只靠 `chapters.summary` 这个单粒度记忆层。 | chunker 设计、embedding 路由、Qdrant collection 策略 | 新表 `chapter_text_chunks`，以及 Qdrant 新集合 | XL |
| Neo4j 状态机扩展 | 把当前以 `EntityTimelineService` 为主的图状态写入继续扩展到更完整的地点、时间、道具、阵营、关系事件，让 checker 与 context pack 读的是时间点状态而不只是静态人物卡。 | 图 schema 稳定、章末抽取可信度、回放接口 | Neo4j 图模型，旁及 `characters`/`relationships` | XL |
| 章末抽取 LLM pass | 在保存章节后跑专门的结构化抽取 pass，不与正文生成混用 prompt；把它作为章末写回的上游阶段，为实体更新、伏笔登记、关系演进提供统一输入。 | 独立 task_type、限流、失败重试策略 | `generation_tasks` 或新增抽取任务表，最终影响 `characters` / `relationships` / `foreshadows` | L |

## 长期路线（500 万字规模瓶颈预判）

系统整体目标是单部作品 **500 万字+**。下面这些不是“等真有 500 万字项目再看”的远期幻想，而是现在就该按下限预判的瓶颈。

### 1. Qdrant collection 切分策略

当前 collection 主要围绕参考书和章节摘要组织，尚未面对“大型项目自身文本”这个量级。等项目章节向量化真正启用后，collection 至少要支持按 `project_id` 或 `volume_id` 分片，不然单集合 scroll、delete、rebuild、snapshot 和热数据 locality 都会恶化。这里不能再走“先全塞一个 collection 里，后面再迁”的偷懒路线。

### 2. PostgreSQL 表分区

`chapters` 和未来的 `chapter_text_chunks` 都是天然的大表候选。对于 500 万字项目，按 `project_id` 做 declarative partition，至少可以控制索引体积、VACUUM 影响面和热项目/冷项目分离。只靠单表加索引不是长期解法，尤其 `chapters.content_text` 还是全文 `text` 字段。

### 3. 上下文窗口压缩

`ContextPackBuilder` 现在已经有 4 层 budget 和 `recent_summaries`/outline/RAG/风格样本多槽位拼装，但本质还是“把更多东西压进固定窗口”。500 万字规模下，必须把增量摘要、角色态、关键事件、长线伏笔做分级压缩，而不是继续单纯拼接章节摘要。滑窗摘要、事件级抽取、可回放 fact timeline 都要成为正式能力。

### 4. `recent_summaries` 链断裂与摘要污染风险

现有相邻章连续性高度依赖 `chapters.summary`。这条链一旦某章摘要质量差、章末没更新、或者被错误内容污染，后续几十章都可能在错误事实上继续滚雪球。项目字数越长，这个问题越不是“偶发摘要失真”，而是核心事实层的污染传播。因此后续需要引入摘要版本、关键事实校验、回溯重算或多级摘要并存。

### 5. 多 LLM 路由 + tier 降级

今天 tier routing 已经存在，但默认端点仍然很少，降级逻辑也主要解决“调用失败时换一个能跑的模型”。在 500 万字级别上，真正的问题会变成：哪些任务必须旗舰、哪些可以标准档、哪些必须便宜可批量、哪些要保证 embeddings 稳定可重建。换句话说，路由不能只看 prompt 配置，而要看任务类型、项目阶段、token 压力、重试策略和批处理窗口。

## 冻结的 backlog（不再追）

以下思路已经被后续演化证明不值得继续投资，除非未来出现新的强约束，否则不再把它们放回活动 backlog：

- `v0.7` 时期那种“把关键生成决策继续压在单轮 commit/critic 上”的方案已经过时。scene-staged、auto-revise、cascade 和 dosage profile 都说明主链路必须显式分层，回不到单轮 prompt 魔法。
- 新建 `entity_records` 表这条路冻结。现有事实主表已经是 `characters`，补洞方向应该是章末写回与档案规范化，而不是再平行造一套实体主表让同步问题翻倍。
- “先用 pgvector 起步，Qdrant 以后再说”这条路线已经失效。当前仓库真实向量底座就是 Qdrant，相关脚本、API、重建与运维经验也都围绕它沉淀，倒回 pgvector 只会产生第二套半成品。
- “先只做 200 万字测试项目，系统容量以后再谈”的说法冻结。`验收测试-玄幻全本200万` 是样本，不是系统上限；任何以它为理由弱化分区、切块、向量分片或上下文压缩的提案都不再进入主 backlog。

## 文档维护规则

- 任何 schema 变化、目录结构变化、核心调用链变化、版本发布变化，必须同步更新 `PROJECT_STRUCTURE.md`、`PLAN.md`、`AGENTS.md` 三件套，而不是只改其中一份。
- 任何 backlog 条目一旦完成，立即从“近期 backlog”或“长期路线”迁移到“已完成里程碑”，并写明落在哪个版本、解决了什么结构问题。
- 任何新发现的仓库级硬规则，如果会影响编码、调试、验收或运维，优先固化到 `AGENTS.md`；如果会改变系统边界、目录职责或数据流，再同步回 `PROJECT_STRUCTURE.md`。
- 任何版本发布后，如果 CHANGELOG / RELEASE_NOTES 已形成事实，`PLAN.md` 不得继续把该工作写成未来式。
