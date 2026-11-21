# 认知执行隔离与 ARM 更新

## 补强执行所有权

旧认知任务失去项目数据库连接锁后，后继任务可能完成；旧任务随后在异常处理里按 record_id 写 failed，会覆盖后继状态。新增 a1001930 / attempt_token，每次实际提取认领新 UUID，结果、作废和失败更新必须匹配该执行标识。失败更新还要求仍处于 running，避免成功提交后会话清理错误将完成记录降级。

队列入口的来源核对与作废标记使用同一源行锁，作废同时撤销旧 token。作者改稿后恢复原文时，已撤销执行标识的旧调用不能发布结果；未更新到行时返回 superseded，不误报完成。章节在认领间隙删除时返回 obsolete，不因记录级联删除而抛属性错误。

## 验证

30 项针对性通过（12.61 秒），新增真实 PG 连接终止测试：第一个模型调用停留时终止其持锁后台连接，第二次调用获得锁并完成，再放行第一次返回；最终仍是第二次 completed / 空变化 / 无错误。另验证两个并行恢复调度仅投递一次、作废撤销 token、改稿恢复拒绝旧完成、迁移降级/升级保留来源、认领前删章、提交后清理错误不降级。

按 shadow-mind 无可信只读隔离环境回退条款做本地独立复核，覆盖所有 Record 状态写入和模型前后边界。复核发现并补上提交后清理失败及认领前删章两个边界。

首轮全量 1917 passed；补齐提交后清理异常边界后的最终全量 **1918 passed，4 条既有 warnings，289.06 秒**。日志：/tmp/aiwrite-cognition-fence-focused-final.log、/tmp/aiwrite-cognition-fence-full.log、/tmp/aiwrite-cognition-fence-full-final.log。

## ARM 更新准备

隔离运行库迁移前确认 entity_source_outbox=0、chapter_entity_extractions=0。已完成隔离库 pg_dump 自定义备份：/root/.aiwrite-evaluation/cognition-runtime-20261007/runtime-before.dump，SHA256 c9e8e0e4a6d668d103fc30cb365b778ecc4e8ea0ed057273454ad0be877aa763。该备份是本轮隔离副本备份，不替代此前生产备份及恢复演练。

准备切换脚本：先停 Beat，检查两个 Worker active/reserved 全零，再停 Worker 和后端；新镜像检查目标 database 必须为 aiwrite_runtime_20261006 后迁移。失败则重启原隔离容器；成功后才替换四个隔离服务。已成功执行：构建源码 f249fb9，镜像 aiwrite-backend:cognition-candidate，隔离库从 a1001928 升级至 a1001930；四服务全部更新，每个容器 233 个 Python 文件与本地 app 逐项指纹一致，健康接口 ok。两个 Worker 均确认注册认知提取与恢复任务，最终检查 active/reserved 均为空。首次后置探针曾因 Beat 正常运行时并非始终空闲而触发断言，后改为核对节点和任务类型；切换前的全空检查未放宽。实际 Registry→模型发送边界的零调用探针确认 chapter_cognition 采用配置预算 8192，schema 和 attempt_token 列可读。

探针的 route.model 为空，进一步核对数据库确认它继承 T2 的默认型号 deepseek-ai/deepseek-v4-flash；该路由此前已实测失效。本次没有付费模型调用，也没有把零调用探针当成真实提取可用性证明。下一步仍须验证和调整实际可用的候选路由。

私有证据目录 /root/.aiwrite-evaluation/cognition-runtime-20261007/ 包含 build.log、switch.log、probe.log、source-result.json、worker-probe-final.log、backup-toc.txt。备份归档目录可读取，本轮未对该新备份再次做完整恢复演练；此前生产备份恢复验证记录仍独立有效。正式生产应用未启动、正式数据库未迁移。

重复队列消息仍可能出现，执行标识保护结果，不宣称 exactly-once 消息传递。历史无来源数据重建、认知语义质量、完整生成与生产发布仍未完成。
