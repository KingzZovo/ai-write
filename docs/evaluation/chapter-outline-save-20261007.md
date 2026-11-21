# 细纲流式保存与扩写来源保护（待数据库复验）

## 发现与实现

`/api/generate/outline` 的 chapter 分支先提交 Outline，再单独合并 Chapter.outline_json 和更新标题，后一步异常仅记录日志，仍可能发出保存成功。生成期间没有源版本核对，作者新细纲可能被旧上下文的结果覆盖。多份书纲时使用 scalar_one_or_none 还会在模型调用前报错。

新增 `chapter_outline_save.py`：

- 从本项目已确认卷纲解析稳定卷ID/目标章，使用其关联书纲；旧卷纲没有父书纲时选择最新已确认书纲。
- 生成前深拷贝书纲/卷纲/有效章节预算、前章有效摘要，保存源快照。保存时锁定父数据并核对源快照，拒绝错章、重复章号、未确认父纲和生成期间修改。
- 严格解析完整JSON，复用细纲结构校验及字段规范化；Outline新版本、章节细纲和标题同事务提交，旧大纲版本及正文保留。模型输出预算不能覆盖有效章节预算。
- 流式细纲加入项目生成互斥；取消时保存任务先完成回滚再释放锁。保存失败不发送saved/completed；伏笔更新仅在主事务成功后进行。
- 先检查缺失书纲/卷纲，再加载模型路由，避免路由初始化故障掩盖资料缺失。

桌面实际使用的 `/chapters/{id}/outline/expand` 原先只比较目标章updated_at。现对生成输入做深拷贝，保存前按项目→卷→章顺序加锁，锁住既有书纲/卷纲候选并重新收集来源。项目预算、书纲、卷纲、前章原文/细纲或来源身份变化，拒绝写入；正文不改。内部来源元数据不进入模型提示词。

## 已验证与未验证

1. 扩写入口修改之前，流式保存及相关回归64 passed，21.57秒（真实隔离PostgreSQL）：坏JSON、结构错误、并发生成、作者改稿、标题/预算、提交失败、保存中取消、旧大纲保留。
2. 运行环境随后切换为workspace-write、网络受限、审批never；新一轮测试连接本地PostgreSQL时被PermissionError: Operation not permitted拒绝。60项数据库夹具初始化错误不是功能验证结果，不能计为通过。
3. 缺失父纲的两项测试起初被路由初始化失败掩盖；调整检查顺序后，离线相关88 passed、3项数据库用例明确排除。新增深拷贝/来源复核补充单测3 passed，覆盖共享字典原地修改和独立新上下文；这3项不能替代SQL并发测试。
4. AST与git diff检查通过。按shadow-mind做本地独立复核（无可信只读代理隔离）：检查身份绑定、原文保留、提交/取消边界、源数据深拷贝与锁顺序；扩大修复范围至实际扩写入口。未发现额外确定问题。
5. **新增扩写来源检查的真实PostgreSQL测试、最终全量回归、隔离部署和实际API验收仍未完成。不能用此前64项通过证明当前整批完成。** 当前运行镜像仍为c4b4a4b，生产未发布。

## 恢复验收命令

仅使用既有隔离测试运行器，勿改连生产数据库：

```sh
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_chapter_outline_save.py tests/test_outline_expansion_registry.py tests/test_chapter_planning_scope.py tests/test_context_cross_volume.py tests/test_outline_readiness.py tests/test_volume_creation.py --tb=short
/root/.aiwrite-evaluation/run-tests.sh -q
```

通过后部署隔离四服务、核对源码；实际API探针应覆盖流式坏输出/成功/生成期间改书纲，以及桌面扩写期间改项目预算或前章，确认旧正文/细纲保留。整书流式保存仍需独立审计，本批不替代它；生产发布、文学质量稳定性和全范围验收继续保留。
