# 大纲 Prompt 调用契约

2026-10-06：`OutlineGenerator` 的所有模型调用经 `OutlinePromptRunner` → `run_text_prompt` / `stream_text_prompt` → prompt_cache → ModelRouter tier fallback。正文/章节扩写继续使用各自标准 Registry 入口。没有添加直接模型路由旁路。

| task_type | 职责与输入 | 输出 | 初始路由来源 |
|---|---|---|---|
| outline_book | 创作输入、目标规模；非分阶段入口 | 全书大纲 | 已有配置 |
| outline_book_skeleton | 创作输入、目标规模 | 一/三/七/八/九段、分卷计划 | outline_book |
| outline_book_characters | 创作输入、骨架、已生成角色/关系段 | 当前请求的二/四/五段 | outline_book |
| outline_book_world | 创作输入、骨架 | 六、世界观设定集 | outline_book |
| outline_volume | 全书大纲、卷号、作者补充 | 单次完整卷纲 | 已有配置 |
| outline_volume_meta | 全书大纲、卷号、章数设置、作者补充 | 卷元信息 JSON | outline_volume |
| outline_volume_batch | 全书完整规划、完整卷元信息、最近两章完整规划、较早章节事件记录、范围、命名要求 | 指定范围 batch JSON | outline_volume |
| outline_chapter | 全书/卷纲、章号、上章摘要、作者补充 | 细纲 JSON | 已有配置 |

阶段身份与提示词资产一一对应；人物阶段多次调用共享同一人物规划资产，具体段落由该次请求限定。阶段默认提示词定义职责，动态上下文保留现有完整输出合同、命名偏好和目标规模。Legacy 单次全书入口预加载了注册模板时，Registry 不重复插入相同模板；独立 ContextPack 消息仍会收到注册模板。

## 配置与部署

新增阶段由 `PromptRegistry.seed_builtins()` 初始化；只补缺失资产，不覆盖用户已编辑的资产。初始化时从对应父任务继承 endpoint/model/temperature/max_tokens/tier，之后作为独立可配置资产。父任务尚无 endpoint 时，新资产也没有可用绑定，必须在开始生成前配置；不能把无绑定种子视作可生成状态。线上部署需先核对父任务路由、初始化阶段资产，再核对每个阶段绑定。现有数据不需要 schema 迁移。

未自动同步后续父任务的路由编辑，以保留用户对阶段的独立设置。请求预算（例如批次 max_tokens）仍可覆盖路由默认值。后台推荐表为新阶段提供 chat/flagship 建议。

## 生命周期与验证边界

无注入会话的 Registry 调用使用短读取会话，在模型 IO 前关闭；并行阶段不共享 AsyncSession。统一调用携带项目 ID、实际 prompt_id、任务类型及路由，沿用调用日志和 buffered counter。阶段验证负责输出完整性，成功交付计数不代表结构或文学质量通过。

流式适配器关闭时关闭 Registry 迭代器；Registry 再关闭模型迭代器。已有分阶段调度取消时等待子任务退出。测试覆盖配置继承、自定义模板、无长读取事务、阶段任务类型和流式关闭。真实模型 A/B、输出 schema 全目录、全部上下文来源追踪与生产验收仍未完成。

章节批次不再把卷元信息限制为六个字段，也不再按字符数截掉转折/交接尾部。较早章节只保留章号、标题、关键事件；最近两章保留完整结构。所有记录明确属于规划，不能作为已写正文事实。完整规划增加输入 token；500 万字规模的输入容量、费用和时延仍需 P6 实测。


## 分阶段卷元信息检查（2026-10-06）

默认 staged 路径在 V1 与 V2 之间调用 `outline_contract.validate_volume_meta`。卷号必须等于请求卷号；章数必须为正整数并符合项目区间。核心冲突、情感变化、卷间交接、卷首末状态、资源图、信息图、机制边界、结果层级及伏笔进展必须提供非空内容，允许保留结构化字典/列表。至少五个有效转折；新/退场人物和 planted/resolved 必须显式给出数组，无变化时可为空。

不把解析后的残缺对象当作完成元信息，也不推算默认章数。首次校验失败携带原始回复和具体错误请求完整修复；第二次失败或路由/网络故障明确返回 `_parse_error`，不进入章节批次。流式包装继续拒绝将错误对象输出为可保存卷纲。旧 `_fallback_volume_meta` 辅助方法仍保留兼容测试，但已无生成链调用。显式 staged=False 单次入口现也复用同一元信息校验，并校验完整 chapter_summaries 数量与章号（见下）。

检查保证字段结构与信息非空，不以字符串长度证明剧情质量；Prompt 中更细的字数、动机/代价与深度要求仍需实际样本验收。本批未运行真实卷纲生成或生产部署。


## 显式单次模式交付

`outline_volume_single.generate_single_volume` 保留 `outline_volume` 路由，分别使用 Registry 非流式或流式接口。项目章数区间与转折/关键事件/章号约束进入输入；完整输出经过相同元信息校验及 `_validate_volume_batch`（范围1到chapter_count）后才能交付。首个不合格完整回复附具体原因请求一次修复，异常/超时明确失败，不回填模板、不重编号或截章。

单次流式输出先缓冲并关闭底层响应，再校验：只返回完整可保存JSON，错误或中断不泄漏残缺JSON。预览不再逐token显示该模式的原始回复，这是为与分阶段模式的有效结果交付一致。异步取消继续传播，底层流用 aclosing 关闭。新增12项测试覆盖流/非流修复、各类失败、断流和取消；未做该模式的真实模型/浏览器验收。
