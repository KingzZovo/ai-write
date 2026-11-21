# 整书生产模块

## 责任边界

- `control.py`：数据库事务内的启动、暂停、过期认领与回滚命令。返回 queued 后，调用者必须先 commit 才能向队列发送消息。
- `dispatch.py`：从数据库读取已提交的 queued 命令并投递；队列故障保留命令，30 秒后重试。恢复运行中任务前必须同时满足心跳过期和项目锁空闲。
- `locking.py`：确定性的 PostgreSQL 项目锁。Worker 独占一条没有长事务的连接；退出时丢弃物理连接，避免会话锁泄露到连接池。
- `fencing.py`：仅在显式写入上下文内启用 ORM 写入检查。核对 command/worker 凭证和章节 updated_at，再在同一事务保存正文、哈希及版本时间。旧 Worker 和异常保全写入都必须经过相同检查。
- `writer.py`：单章异步及 SSE 的共用项目锁/章节版本保护。源生成器只在归属与活动整书检查后运行；锁丢失取消生成，断开时等待已开始的保存完成；被取消任务及失效上下文不得保全文本回写。
- `runtime.py`：命令原子认领、独立心跳、逐章执行、章节边界暂停与自动终态；重复消息不调用模型，失去锁后先取消并等待生成退出，再释放连接。模型故障每条显式命令默认最多两次尝试。
- `chapter_executor.py`：整书章节与现有异步单章主链的适配器。持久化 GenerationTask ID，复用现有风格、ContextPack、生成、评审、修订和实体派发；不得在这里增加第二份正文 Prompt。

## 数据库升级

新增列由 `a1001921` 提供。迁移仅追加运行凭证、排队/心跳、任务引用和写入版本；支持旧环境已经通过 create_all 创建 pipeline 表，以及迁移历史尚未建表的环境。上线前需要完成备份和迁移验证，不能直接用新模型连接未升级的生产表。

回滚采用“全部检查后再写入”：快照必须与任务章节集合一致，作品/卷归属一致，当前正文哈希与更新时间必须匹配任务最后写入的版本。正文、摘要或其他章节字段被作者改过时拒绝自动覆盖。旧任务没有可验证版本时宁可拒绝，不能猜测哪些修改可以丢弃。

## 集成状态

HTTP start/pause/rollback 已接入事务命令；start 提交后安排即时投递，数据库 queued 行承担持久重试。Celery 整书入口已改用 runtime，旧的标题/大纲直写与另一套状态机/回滚实现已移除。无 command_id 的历史消息不执行写入，运行中遗留任务由恢复扫描重新认领。

每 30 秒的 Beat 任务负责过期检查和补投，路由至 `pipeline-control` 专用队列。Compose 增加 `celery-beat` 与 `celery-control-worker`，避免正文 Worker 全忙时控制任务饥饿。每个部署只启动一个 Beat；配置尚未应用至 141，部署时需保留其现有 Compose 修改。没有启动调度进程/控制 Worker 时，不能宣称自动恢复可用。

单章异步和 SSE 已接入同一项目互斥协议；异步锁竞争由 Celery 退避重试，SSE 返回明确错误和 DONE。独立异步入队使用固定请求并先提交；派发不明返回 503 和原任务编号，保留原任务状态，不把可能已经执行的任务改成失败。

批量接口由独立的`batch_submissions`/`BatchGenerator`适配受保护的单章任务链，使用固定客户端请求和逐章完整原稿，同事务持久化整批清单及子任务。批次原子认领，子任务不能单独发布；SSE断开排空执行，保留完成记录并取消未开始子项。GET只读原批次，重试不重建来源或盲目恢复已认领任务。新表依赖a1001952，真实迁移未执行；批次崩溃自动恢复仍待完成。cascade 当前只处理大纲/设定修复，已统一项目锁且支持弃置认领恢复；异常最多执行四次。未使用的旧 cascade 正文重写方法已移除。

前端入口已接入共用 GeneratePanel，任务状态和正文预览位于 `frontend/src/components/workspace/BookPipelinePanel.tsx`；请求/轮询由 `useBookPipeline.ts` 管理。回滚成功后消费快照，下一次启动重新记录当前版本。

真实 Redis/Beat/prefork Worker 隔离验收已通过，包含 SIGKILL 恢复与真实连接失败重投。测试入口 `tests/test_pipeline_processes.py`，专用 bootstrap 位于 `tests/support/pipeline_worker_app.py`；模型 IO 是替身，生产代码没有测试开关。

尚未完成：完整应用 SSE/浏览器验收、真实模型长流程及生产迁移。部署时必须先停止旧 Worker，旧进程不认识新的项目锁；不能混用两代写入协议。

测试入口：`tests/test_pipeline_control.py`、`tests/test_pipeline_runtime.py`、`tests/test_pipeline_api_runtime.py`、`tests/test_standalone_writer.py`、`tests/test_batch_cascade_guards.py`。运行时覆盖三卷执行顺序、重复投递、暂停恢复、进程中断、有限重试、待审继续、人工修改和锁丢失取消。该组测试建立随机测试 schema，使用真实独立连接验证并发、锁释放、持久投递、旧凭证写入、回滚冲突、迁移兼容及适配器调用；模型调用用替身，不是文学质量验收。
