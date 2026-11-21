# 质量验收批次报告(2026-11-14)

基线 commit:`854f9b9`(`feat(context): character location tracking + entity timeline context`),
工作区带未提交改动(本批全部改动未提交,按要求不 commit)。

## 这一批交付了什么

1. **一键可复现的验收入口** `backend/scripts/quality_eval/`
   (固定样本集 / 提示词拼装 / 真实模型调用 / 确定性检测 / 前后对照 / 契约快照重放 / 连续章探针)。
2. **固定样本集** `cases/sample_set_v1.json`:3 题材 × 2 场景,每个场景自带必须保持的事实、
   不得新增的元素、必须完成的结果、字数区间。
3. **六类确定性检测器** + 连续章检查器(字数对目标、重复开场、同名目金额冲突、
   章内余数冲突、书级数字冲突、道具缺失、未授权人物)。
4. **真实写作链连续 10 章跑通**(含实体提取与认知提取两步前置)。
5. **100 章规模演练**(桩模型):强杀 + 恢复 + 无缺号。
6. **修掉三个真缺陷**(两个是阻断级),都在生产代码里,各带回归测试。

## 逐条对照验收标准

| # | 标准 | 状态 | 证据 |
|---|---|---|---|
| 1 | 一键入口 + 产物齐全 + 两次运行结论一致 | **达成** | `quality_eval run/detect/rescore/compare/snapshot`;`detect` 重跑返回 `consistent_with_previous: true` |
| 2 | 固定样本集(3 题材 × 2 场景,含 must_keep/forbidden/required) | **达成** | `cases/sample_set_v1.json`;`load_case_set` 强制校验题材数与字段 |
| 3 | 六类检测器 + 正反向用例 | **达成** | [detectors.py](/root/ai-write/backend/scripts/quality_eval/detectors.py);[33 项用例](/root/ai-write/backend/tests/test_quality_eval_detectors.py) |
| 4 | 有基线的改进证据(前后同模型同温度、逐类数字) | **达成** | [abi REVIEW](/root/ai-write/docs/evaluation/quality-eval-20261114-abi/REVIEW.md)(n=3/格,契约快照哈希验证可重放) |
| 5 | 单章质量显著下降且无类别上升 | **达成(一条子项因基线已在 0 而无从体现)** | n=8 两臂各 48 次成功调用。确定性检测:fact_violation 100→73、rule_recitation 19→6、pov_breach 11→7、metaphor_stack 2→1,instruction_leak 0→0;**结果交付**改用独立评审(deepseek-chat,盲评,要求逐字依据):220 条里 214 交付,两臂**逐位相同**(未交付 6 : 6)。"需重写比例下降"这一子项无从体现——基线本身就是 0/6 需重写。见 [ABI REVIEW](/root/ai-write/docs/evaluation/quality-eval-20261114-abi/REVIEW.md) |
| 6 | 连续 ≥10 章真实链 | **达成(24/24)** | 大纲扩到两幕 24 章:三臂(基线 / pins / 修复)章节 `status: ok` 均 24/24、认知 completed 24/24;**实体投影 ok 22 · 23 · 24**,`AT_LOCATION` 报错 2 · 0 · 0 次。10 章版见 [outline10](/root/ai-write/docs/evaluation/continuity-20261114-outline10/REVIEW.md),24 章版见 [baseline](/root/ai-write/docs/evaluation/continuity-20261114-long24-baseline/REVIEW.md) / [pins](/root/ai-write/docs/evaluation/continuity-20261114-long24-pins/REVIEW.md) / [fixed](/root/ai-write/docs/evaluation/continuity-20261114-long24-fixed/REVIEW.md)。字数中位比三臂 0.97 / 1.11 / 0.79,单 seed 不可归因 |
| 7 | 100+ 章规模稳定性 | **达成(桩模型)** | 100 章:强杀时已完成 33 章,`--resume` 补齐至 100;`ok` 67 / `resumed` 33 / `error` 0,无缺号,实体与认知 67/67 终态。桩文本不是文学,内容检查无意义;真实模型百章表现**未跑**。见 [scale100 REVIEW](/root/ai-write/docs/evaluation/continuity-20261114-scale100/REVIEW.md) |
| 8 | 回归不退化 | **达成** | 离线全量 **0 failed / 4780 passed / 762 skipped**(338.09s)。HEAD `854f9b9` 自带的那 4 项失败已全部转绿(**改的是产品代码,不是测试**)。本批累计新增 82 项离线回归(2026-11-16:窗口校准 7 / 金额抽取 36 / 流式与中转守卫 12;2026-11-17:写后核对 7 / 计划定数 11 / 抽取与漂移 9)。报告初版的 "4 failed / 4674 passed" 已作废 |
| 9 | 每批结论可追溯 | **达成** | 各目录 `RUN.md` / `results.json` / `detections.*.json` / `chapters.json` / `REVIEW.md` 齐备,含命令、模型、prompt 哈希、正文哈希 |

## 本批修掉的七个真缺陷

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `entity_context.py` 用了没导入的 `UUID`(HEAD `854f9b9` 引入) | 只要项目里有角色卡,`character_states_context_unavailable` → **整章写不出来**(不是降级) | 补 `from uuid import UUID`;[3 项回归](/root/ai-write/backend/tests/test_entity_context_character_states.py) |
| NVIDIA 批量 embedding 被拒 | `modality must be a list of the same length as input`;认知索引按 32 条一批,批量全挂 → 语义召回不可用 → **第 4 章起阻断**;单条调用正常所以只在有上下文积累后暴露 | `nvidia_embedding_payload` 按输入条数展开 modality;[5 项回归](/root/ai-write/backend/tests/test_nvidia_embedding_payload.py) |
| 认知提取逐字依据失败后没有定向修复 | 模型把 evidence 改写成自己的话(实测 5 条里 3 条在正文中找不到),连报 3 次即中止,整条链停住 | 提示明确"直接复制连续片段,不改写/不加省略号/不拼接",失败时把**具体哪几条找不到**回给模型重试;[5 项回归](/root/ai-write/backend/tests/test_cognition_evidence_locator.py)。修后 10/10 章一次通过 |
| `context_pack._render_chapter_outline_block` 把非 schema 大纲静默截断成一行标题 | 章节大纲若用 `brief`/`pov`/`location`/`time_cue` 等非内置字段名,**整章大纲在提示里只剩 `《[CH-n] 标题》`**,生成侧等于没有本章大纲(第 2 章起整章脱纲)。降级分支条件是 `if not parts:`,标题行已经让 `parts` 非空,所以既不渲染也不 dump | 记录标题行数,标题之外一个字段都没渲染到时把剩余字段补出来(跳过已渲染字段与顺序元数据);[2 项回归](/root/ai-write/backend/tests/test_outline_prompt_integrity.py) |

| `AT_LOCATION` 同章第二地点建边违反唯一约束 | ch11 周芸先在医生办公室、后回病房 → 第二次 `MERGE` 匹配不到已有边就新建同键边 → Neo4j `ConstraintValidationFailed` → **整章实体投影回滚**。基线 24 章里 2 章命中(ch11、ch24) | 同章已有地点就记进 `alternate_locations`,不再建边(`entity_graph_projection`、`entity_timeline` 两处,后者还顺手修掉"同章第二次调用会把本章边的 `chapter_end` 设成 idx-1");[结构守卫 + 真实 Neo4j 用例](/root/ai-write/backend/tests/test_location_projection_guard.py) |
| 证据定位要求逐字相等,全角/半角标点差异即判死 | 第 6 章认知提取连报 3 次"逐字定位"失败 → 该章认知 `skipped` → 第 7 章起 `character_cognition_not_ready` → **整本书停摆**(保留在 [aborted 臂](/root/ai-write/docs/evaluation/continuity-20261114-long24-aborted/REVIEW.md)) | `evidence_locatable()`:先逐字判,不中再把**标点形式**一对一归一化判一次;不删字符、不忽略换行、不放行省略号与拼接(那正是改写信号);`cognition_reader` 读侧同步改用同一函数;[回归](/root/ai-write/backend/tests/test_cognition_evidence_locator.py) |
| 探针漏种 `relationship_scope` prompt 资产 | `RelationshipScopeError: relationship_scope_prompt_not_configured` → 该章实体投影失败(第 18 章)。该任务不走 tier 回退,要求 `snapshot.endpoint_id` 非空 | `_PROMPT_TASKS` 补上该任务,并把真实 endpoint id 传给 `_seed_prompts` |
**一处对边界的有意偏离**:`tests/test_cognition_revision_migration.py` 强制"内置认知提示 == 迁移 `a1001948` 的 `NEW_PROMPT`"。
改了内置提示就必须同步迁移里的常量,否则该迁移会把**过时的默认提示**装进库——正是这个测试要防的事。
该迁移从未在任何真实库应用(`a1001948` 未执行),同步只改常量文本,升/降级行为由同一组 3 项测试在 SQLite 上验证通过。

另有两个**工具侧**缺陷(探针自身)已修,写进 scale100 报告:`--resume` 曾按"文件存在"判断完成,
在"写完文件、未提交"的窗口留洞;`--chapters N>10` 实际只建 10 章。

## 独立评审(第 5 条的测量器)

`quality_eval judge` 用**另一个模型**(`deepseek-chat`)盲评"本场必须完成的结果是否交付",要求给出正文原句;原句按"逐字 / 去标点 / 最长公共子串 ≥12 字 / 找不到"四级定位。这一步把第 5 条从"两种指标互相矛盾"变成有结论:针对的类别全部下降,结果交付两臂相同。
命令与配对统计见 [ABI REVIEW](/root/ai-write/docs/evaluation/quality-eval-20261114-abi/REVIEW.md)。

## 篇幅目标(根因已定位,并已修正归因)

连续章探针实测(当时):目标 1200 字的章节,9/10 章低于目标的 85%,中位比值 0.62。

**根因不是写手不愿意写。** 诊断轮先查规划侧:每章 `plan.scenes[].target_words` 合计
恰好等于 `chapter_target`(1200),规划侧没有欠配。真正的原因是同一轮暴露的渲染器缺陷——
章节大纲被截断成一行标题,**写手手里没有本章大纲可以展开**,字数只能随题而不随目标。

修好大纲投影后重跑 10 章:中位比值 **0.94**,达到目标 80% 的章数 **9/10**,只剩 1 章
(0.80)低于 85%。详见 [outline10 REVIEW](/root/ai-write/docs/evaluation/continuity-20261114-outline10/REVIEW.md)。

写手侧提示里的篇幅约束(`prompts/scene_writing.py`、`scene_plan_contract`)保留,
但在装置修复之后**无法与它分离归因**,不再单独声称增益。

同时查清了它为什么不会自愈:质量门在"正文合格但篇幅不足"时返回 `blocked` 并**不进入改写**,
只把稿子留成 draft(既有设计:不自动补字)。所以篇幅只能在生成侧解决。

## 24 章真实链(第二幕)与三个只修掉一个的缺陷

把连续链从 10 章扩到 24 章(新增第二幕:母亲复查 / 渡船停航 / 铺子涨租 / 结账),
并把检查器三项(钉死数额、时间两问、未点名人物)加上,然后跑了一轮基线 + 一轮修复。

三臂结果(同大纲、同模型):基线 / pins 臂 / 修复臂。实体投影 ok 分别是
**22/24 · 23/24 · 24/24**;`AT_LOCATION` 唯一约束报错 **2 · 0 · 0** 次。
修复臂 24/24 是"产品缺陷确实修掉了"的端到端证据(
[fixed REVIEW](/root/ai-write/docs/evaluation/continuity-20261114-long24-fixed/REVIEW.md))。

三臂字数中位比 0.967 / 1.105 / 0.786 —— 跨度 0.32,**单 seed 无法区分改动与方差**,
列为未解决(检查过注入块没有挤掉上下文,但真实运行没打点)。

| 缺陷 | 结果 |
|---|---|
| 同章两个地点 → 整章实体投影回滚 | **已修并验证**:基线 2/24 章失败(含 ch11),修复臂 0 次 `AT_LOCATION` 报错;旧写法必抛已用真实 Neo4j 复现 |
| 第 7 章写"工钱 840"、第 8 章按大纲写"820" | **未修掉**。基线两个不同 seed 都复现(840/820、880/820) |
| 同一个配角两个名字(ch02"钱大叔" / ch16"老刘") | **产品侧无覆盖**。`fact_contract` 只管 主角/导师/父亲/母亲 四个角色族,且要求显式命名句式;`房东` 不在其中 |

第二条试过"把大纲已定的金额写进写作上下文",**没有用**:重放运行时的
`ContextPack` 确认 ch07 的提示里确实有"第 8 章 收下八百二",模型照样自己编了
"每套二十八块 × 三十套 = 840"。提示里给数字 ≠ 约束住数字。

要真解决需要产品级数字台账(写完后按名目抽取已写金额 → 与大纲钉死的数比对 →
冲突时重写产生冲突的那一章)。本批**没有做**:第一步的名目识别就是被删掉的那个
误报率接近 100% 的检查,这不是调正则能补上的。详见
[pins REVIEW 第 3 节](/root/ai-write/docs/evaluation/continuity-20261114-long24-pins/REVIEW.md)。

### 检查器这一轮被删/被收窄的地方

上一版检查器在 24 章基线上报 26 条,人工核对后**大部分是误报**:
`pinned_number_conflict` 5 条全错(把历史值、增量、"姐姐递来的一千元"当成矛盾)、
`remainder_conflict` 2 条全错("剩下的两"是两个人)、`time_missing` 2/7 错
(正文写了"夕阳""清晨",不在我的词表里)、`number_conflict`/`number_variance` 3 条全错
(名目被抽成"呢""的铺租""周芸")。

处置:前者逐条加了守卫;中者拆成"时段可定位 / 时间推进可看出"两问;
后两个**直接删掉**——留一个必出误报的检测器会污染结论,通用跨章数字一致性列为**未覆盖**。

## 诚实说明

- **第 5 条那条子项无从体现**:基线本身是 0/6 需重写,比例不可能再降;能测的是各类计数,而这些在独立评审校正后**没有上升**。不把"无从体现"当成"显著下降"。
- **`outcome_unfulfilled` 的历史数字作废**:n=3 与 n=8 两轮里它都上升,但逐条复核 + 独立评审证明是测量器误报(两臂交付率 97.27% : 97.27%,未交付 6 : 6)。作废的是这个指标,不是那段结论。
- **篇幅只有方向证据**:6 章、方差大,不能宣称已解决。

## 其他诚实说明

- **第 7 条是桩模型**:它证明的是编排(不丢章、能恢复、有终态),不是文学质量,
  也不能替代真实模型百章的费用/时延/失败率。
- **第 7 条是桩模型**:它证明的是编排(不丢章、能恢复、有终态),不是文学质量,
  也不能替代真实模型百章的费用/时延/失败率。
- 报告里的"命中"不等于"缺陷":`unauthorized_new_character` 是 soft 线索,要人读原句裁决;
  `brother_boards` 一类命中经编辑判断属于"限知视角的合理省略"。
- 没有真人盲评;所有"可交付 / 需局部修订"都是编辑式审读。
- 跨章检查**不覆盖**时间推进的单调性、人物位置、知情边界;章内数字只覆盖"剩下/还欠"一族。
  本批新查出**跨章数字矛盾**一例:ch07 写"四十套,每套二十二块,加起来是八百八十块",
  ch08 写"总共是八百二十元。这笔钱是四十套校服的工钱"。产品侧 `fact_contract` 只校验称谓,
  没有跨章金额/数量台账,这类矛盾**目前没有任何机制会拦**(探针的 `number_conflict` 也拦不住,
  因为名目不同)。这是下一步的真缺口。
- 未提交、未部署、未切换默认模型;`a1001958` 仍未应用。

## 本批改动的文件

| 文件 | 性质 |
|---|---|
| `backend/scripts/quality_eval/{__init__,cases,detectors,prompt_assembly,runner,snapshot,judge,continuity_probe}.py` | 新增:验收入口、独立评审与探针 |
| `backend/scripts/quality_eval/cases/sample_set_v1.json` | 新增:固定样本集 |
| `backend/tests/test_quality_eval_detectors.py` | 新增:33 项检测器用例 |
| `backend/tests/test_relay_error_guard.py` | 新增:网关报错泄漏回归 |
| `backend/tests/test_entity_context_character_states.py` | 新增:UUID 阻断回归 |
| `backend/tests/test_nvidia_embedding_payload.py` | 新增:批量 modality 回归 |
| `backend/tests/test_cognition_evidence_locator.py` | 新增:逐字依据定位回归 |
| `backend/tests/test_entity_evidence_locator.py` | 新增:实体提取逐字依据回归(6 项) |
| `backend/tests/test_judge_locator.py` | 新增:评审引用可定位性分级(6 项) |
| `backend/app/services/prompts/scene_writing.py` | 篇幅以本轮/本场目标为准(±15%),并保留仓库要求的"本章篇幅服从本轮目标"措辞 |
| `backend/app/services/context_pack.py` | **修复**:章节大纲不再把非内置字段静默截断成一行标题 |
| `backend/tests/test_outline_prompt_integrity.py` | 新增 2 项回归(非 schema 字段不被丢弃 / 生产字段不重复渲染) |
| `backend/app/services/scene_plan_contract.py` | 场景 user content 的目标字数行同步(不再只说"不凑字") |
| `backend/app/services/entity_context.py` | **修复**:补 `from uuid import UUID` |
| `backend/app/services/model_router.py` | **修复**:`nvidia_embedding_payload` 按输入条数展开 modality |
| `backend/app/services/character_cognition.py` | 提示加"直接复制连续片段";失败时带出 `offending_evidence` |
| `backend/alembic/versions/a1001948_cognition_author_revisions.py` | **同步** `NEW_PROMPT` 到新的内置提示(见下方说明) |
| `backend/app/services/cognition_extraction.py` | 修复提示点名具体哪几条依据找不到 |
| `backend/app/services/chapter_quality_gate.py` | 新增 `looks_like_relay_error` 并接入 `chapter_persist_status` |
| `backend/app/services/narrative_contract.py` | 契约修订(补人约束 / 不复述设定 / 开场不堆比喻) |
| `backend/app/services/narrative_quality_gates.py` | 单发链蓝图同步补人约束 |
| `backend/app/services/chapter_entity_extraction.py` | 提示加"直接复制连续片段";失败时带出 `offending_evidence` |
| `backend/app/services/numeric_pins.py` | 新增:把大纲已定的金额整理成写作约束(实测不足以约束住模型,见上文) |
| `backend/app/services/context_pack.py` | **修复**:非 schema 大纲不再静默截断;新增 `numeric_pins` 注入 |
| `backend/app/services/entity_graph_projection.py` | **修复**:同章第二地点不再建 `AT_LOCATION` 边,记进 `alternate_locations` |
| `backend/app/services/entity_timeline.py` | **修复**:同上;并修掉同章第二次调用会把本章边的 `chapter_end` 设成 `idx-1` |
| `backend/app/services/chapter_entity_extraction.py` | `evidence_locatable()`:标点形式归一化 |
| `backend/app/services/cognition_reader.py` | 读侧校验改用同一 `evidence_locatable` |
| `backend/scripts/quality_eval/analyze.py` | 新增:把一次运行摊平成可对比的数字与金额清单 |
| `backend/tests/test_numeric_pins.py` | 新增 11 项 |
| `backend/tests/test_continuity_probe_checks.py` | 新增 26 项(金额解析、钉死数额、时间两问、未点名人物) |
| `backend/tests/test_location_projection_guard.py` | 新增 2 项(结构守卫 + 真实 Neo4j) |
| `backend/tests/test_cognition_evidence_locator.py` | +2 项(标点形式容忍 / 不放行省略号与拼接) |
| `backend/tests/test_outline_prompt_integrity.py` | +2 项(非 schema 字段不被丢弃) |

## 2026-11-16 批:修正一处**错误结论**,并补上守卫与校准

### 撤回:"中文 4.00 汉字/token"

[context-window-measurement-20261115.md](/root/ai-write/docs/evaluation/context-window-measurement-20261115.md)
初版称"真实换算比 4.00 汉字/token,平台把用量高估 2.67 倍、实际只用了窗口 1.2%"。
**该结论作废。** 用真实章节正文实测(`deepseek-chat` + 本机网关)得 1.45 / 1.46 / 1.45 /
1.43 / 1.435 / 1.434(1654→8093→40000 字,线性),即 **1.43~1.48 汉字/token**。
`CHARS_PER_TOKEN = 1.5` 本来就是对的;照初版改成 4.0 会让预算实际超发约 2.7 倍。
该文件已按实测重写,并把"网关能吃 ≥200k tokens"标为未复核。

### 真正的问题:窗口写死 + 预算偏小

`context_pack` 里 `right(content_text, 3500)`、`.limit(5)`、`.limit(10)`、
全书大纲 `raw[:1500] + "(中部省略)" + raw[-500:]` 都是字面量。
`allocate_context` 对超出层权重的块是**整块省略**而不是截断。**真实 24 章项目**
(`continuity-20261114-long24-baseline`,只读)逐块 trace:

| 章节 | 预算 | prompt 字数 | `future_outlines` | `book_outline` |
|---|---|---|---|---|
| CH7 | 9500(旧) | 8,853 | **omitted_budget**(丢 3,013 字) | included(101) |
| CH7 | 20000(新默认) | 11,868 | **included 3,013** | included(101) |
| CH18 | 9500 | 9,187 | **omitted_budget**(丢 1,086 字) | included(101) |
| CH18 | 20000 | 10,275 | **included 1,086** | included(101) |

即旧配置下写手**完全看不到后续走向**(这正是"长篇越写越飘"的直接来源);
抬高预算把它救回来了。(初版草稿里说"丢的是全书大纲"是我用合成 6000 字大纲得出的,
真实项目的全书大纲只有 101 字、从未被丢 —— 合成结论不能当真实结论用。)

改动:6 个窗口改为 config 开关;`CONTEXT_PACK_TOKEN_BUDGET` 9500 → 20000;
`future_outlines` 渲染块加 3000 字上限(**截尾保近**,只截渲染块以免 `numeric_pins`
少读到金额);前章正文是 `required` 块,默认值**保持 3500**
(放大它会把 `ContextBudgetExceeded` 变成真实故障)。
回归见 [test_context_window_config.py](/root/ai-write/backend/tests/test_context_window_config.py)。

### 中转把错误当正文返回:又一批已知形态

从网关二进制 `/root/migration-deploy/cliproxyapi/cli-proxy-api` 抽出真实诊断串,
补进两道守卫(此前两道都不认识):`terminal_empty_incomplete`、
`stream error: upstream terminated with incomplete empty response (0 tokens)`、
`model_not_supported`、`quota_exhausted`、`upstream execution failed`,
以及上游透传的"The model used its entire output budget on reasoning …"(按 WEAK 处理,
要求同段两句同现,避免把正常英文句子误判)。
回归:[test_relay_error_guard.py](/root/ai-write/backend/tests/test_relay_error_guard.py)、
[test_stream_opening_guard.py](/root/ai-write/backend/tests/test_stream_opening_guard.py)。

### 金额漂移:换掉正则,查清根因

新增 [amounts.py](/root/ai-write/backend/scripts/quality_eval/amounts.py)(模型抽取 + 纯函数解析)。
**根因不是上下文不够**:ch07 大纲只写"周芸说明八百的期限当天到",**没写这批活的工钱总额**,
写手当场自己发明一个自洽的数(outline10 写 880 = 四十套×二十二;baseline 写 840 = 三十套×二十八;
pair6-2b 写 840 = 十四套×六十文),而 ch08 大纲才第一次写出"八百二" → 相邻两章必然对不上。
可测的 4 组里 **3 组冲突**、1 组一致(`scene600b` 的 900 是毛收 − 80 成本 = 820 实落,
已核实不是冲突)。样本很小,`3/4` 不是稳定比例。
详见 [numeric-drift-instrument-20261116.md](/root/ai-write/docs/evaluation/numeric-drift-instrument-20261116.md)。

## 2026-11-17 批:未闭合项收口

按 0 → 1 → 2 → 3 → 4 执行,逐项结论(含没做到的)见
[unclosed-items-closeout-20261117.md](/root/ai-write/docs/evaluation/unclosed-items-closeout-20261117.md)。

- **写后金额核对**:新增 `services/numeric_check.py`,挂在 `apply_chapter_quality_gate` 外层包装,
  三个落库路径一次覆盖;**只在计划里确实有金额时才调模型**(否则零调用),任何失败只 warning、
  不改 status、不抛。真实链日志证明它跑了且没阻断。
- **`compare_arms --amounts`**:金额行从正则换成模型抽取,输出每臂**漂移条数**(按名目+数额去重)。
  6 臂实跑:outline10 / baseline / pair6-2b 各 1 条,scene600 / scene600b / pair6-1a 为 0。
- **真实生成流程取证**:**已闭合**。换掉已挂的上游 embedding(`nemotron-3.5-embed-1b` → `nemotron-3-embed-1b`,
  2048 维、batch×32 0.33s)后,ch18 真实生成 `status: ok`,逐块 **24 块全部 included、0 省略**
  (`future_outlines` 1086 字正是旧预算 9500 下被整块丢掉的那块;`budget=20000`)。选型与逐个模型实测见
  [embedding-provider-sourcing-20261117.md](/root/ai-write/docs/evaluation/embedding-provider-sourcing-20261117.md)。
- **计划阶段定数**:`SceneBrief.money_totals`(可选)+ 章节大纲渲染 + **代码侧**字段清单
  `SCENE_CONTRACT_FIELDS_PROMPT` + `numeric_pins` 前置块。真实规划模型已写出
  `校服工钱=820 / 铺租=800 / 剩余现金=20`,并进入写手提示。
- **两章真链的漂移已修**:计划层收敛(`services/money_plan.py`,与大纲冲突的金额条目丢掉)**之后**,
  重生成 ch7/ch8 得到 `ch07 总额=[820,400] vs ch08 [820,820]` —— **同名目 820 = 820 一致**,
  原来自造的 840/880 不再出现(此前是三阶段:字段只治写手 → 规划者自造 → 收敛后修好,
  中间两个缺陷:条目只按分号切、权威集合漏了本章大纲)。唯一被标出的 `400` 是 ch07
  **新引入**的另一批校服,不是冲突。见 [plan-money-reconcile-20261117.md](/root/ai-write/docs/evaluation/plan-money-reconcile-20261117.md)。
- **写后核对:一个真阳性 + 两个误报**。ch07 的 840 被正确报出;ch08 的 820 与 ch18 的 3000 属误报。
  已把**本章大纲**与**最近 8 章核实摘要**纳入核对范围(前者修掉 ch08 那条;后者对 ch18 那种
  "大纲与摘要都没记"的数字**仍会误报** —— 正解是金额台账,本批未做)。
- **顺带查出的真实限制**:`prompt_assets.seed_builtins` 只补缺失的 task_type、**不升级已有版本**,
  所以改 `prompts/scene_writing.py` 的默认提示对已存在的库**不生效**;字段清单因此放代码侧。
  提示升级机制本身未动(独立特性)。
