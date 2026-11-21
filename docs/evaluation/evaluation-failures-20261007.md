# 评审故障与文学评分分离

## 来源与改动

前章原文实测（见 previous-prose-20261007.md）到达评分阶段后收到 0 分、evaluation_parse_failed；数据库却记录模型调用失败，最后一级回退使用已失效默认模型。这个 0 是故障占位，不能当成正文质量评价。

EvaluationResult 新增 failure_reason：call_failed、timeout、parse_failed、empty_input。保留历史 parse_failed=True 作为不可信结果兼容标记，既有自动修订、验收、最佳稿比较仍拒绝故障占位分数。失败原因同时保存到 system issue，使已有 JSON 持久化能保留分类，不需要数据库迁移。

只在收到结果后进入解析验证范围。上游网络/中转层抛 JSONDecodeError 仍属于调用失败；返回列表、残缺围栏、缺少维度等属于格式错误。超时和空白正文各自标记，外部 CancelledError 不转换成评分。安全描述替代直接拼接上游异常，避免将服务内部错误放进文学诊断；路由已有调用审计保持不变。

SSE scored 附 evaluation_failure，revise_skipped 使用具体 evaluation_* 原因；前端实时进度优先显示失败原因，不把该 scored 的 0 渲染成“评分 0.0”。历史无 failure_reason 的 parse_failed 结果兼容原格式失败提示。数值字段仍保留兼容，外部消费者应依据失败标志识别不可用评分；本批不宣称所有历史评分展示都已改造。

## 测试与复核

85 项后端针对性通过，新增 8 项评审故障验证和 3 项 SSE 真数据库状态参数：上游异常不泄露到 result、上游 JSON 错误归类、格式错误归类、空输入不调用模型；SSE 原因一致、正文逐字保留、章节 needs_review、不触发认知发布。既有期限测试确认外部取消传播。

前端事件测试 18 项，全量 101 项、TypeScript 和生产构建通过；测试环境既有 jsdom navigation 提示仍存在。后端全量 1989 passed、4 条既有 warnings（316.99 秒）。

按 shadow-mind 无可信只读隔离回退条款，代码冻结后本地分别复核正确性与完成度：检查解析范围、取消路径、不可信标记和验收/改写保护；核对实际 SSE 与前端事件消费，以及持久化 system issue 不包含上游原始异常。未发现本批需要追加修改的问题。

## 尚未覆盖

后端已更新 ARM 隔离环境，前端构建尚未部署；生产及浏览器验收未完成。旧数据不回写，原有调用日志保留；T2 失效默认模型、评审漏报/误报、完整小说质量和其他长事务仍需继续验证修复。


## ARM 与真实异步评审验证

四个隔离服务更新到 d5ffc6d/a1001930，各 233 个 Python 源文件与本地逐项一致；替换前停 Beat 并确认两个 Worker 无 active/reserved，旧容器保留至新 API 健康。生产未更新。

先在隔离容器独立进程做零模型受控探针：RuntimeError、TimeoutError、非对象 JSON 分别返回 call_failed/timeout/parse_failed，都不改写、不验收，结果不含原始异常详情。

再以已有待审第二章请求真实 POST /api/evaluate/start，经 Celery→ChapterEvaluator→CPA 后轮询同一任务：19.18 秒终态 completed，result.failure_reason=call_failed、parse_failed=true、overall=0。completed 在此是队列任务终态，不是文学验收通过。同期调用日志为 Gemini error，接着已下线 DeepSeek error/410，确认新分类在真实失败链生效。两章正文 SHA256 与调用前一致，仍 needs_review。本例验证异步 API/Worker 的故障结果，不冒充真实生成 SSE 或浏览器验证；SSE 原因与前端提示已由前一批集成/单元测试覆盖。

私有构建、切换、源码校验、探针和异步任务证据：/root/.aiwrite-evaluation/evaluation-failures-runtime-20261007/。
