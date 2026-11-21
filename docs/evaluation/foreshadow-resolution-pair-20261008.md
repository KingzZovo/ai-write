# 伏笔兑现状态与章号一致性（2026-10-08）

## 问题与行为

此前普通 PUT 允许只写 status=resolved 而没有兑现章号，或把已兑现项改回计划却保留旧兑现章号。现在这两个字段作为一个请求单元：

- `{"status":"resolved","resolved_chapter":20}`：确认或修正兑现章号。
- `{"status":"planned"}`：改回计划，同时显式清掉 resolved_chapter。pending/planted/ripening/ready 同理。
- 只提供 resolved_chapter（包括 null）、resolved 未提供章号、非 resolved 携带非空兑现章号：422，不执行存储操作。
- 描述、条件等普通局部修改仍保持省略字段不变；不自动修复旧记录。

这收紧了上一轮可空字段约定：resolved_chapter 现在须随状态一起操作，其他可空字段不受影响。OpenAPI 字段说明已更新。当前前端编辑仅提交描述/类型/条件，确认兑现走专用 resolve 端点，现有调用不受影响。外部 PUT 客户端若只提交 status=resolved 或单独章号，需改为完整组合。

Pydantic 模型将撤销时的 null 纳入 model_fields_set，确保 exclude_unset 不丢失。PG 在既有行锁内设置两个属性，图谱用同一条 SET 更新两者，避免为拼接请求而额外读取旧状态。现有专用 resolve 端点已经一起写入状态和章号。

前端仅在 status=resolved 且章号非 null 时展示“已于第…章收线”；第 0 章正常显示。旧未兑现记录上的残留章号不再被界面当成收线确认。

## 验证与复核

后端：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_request_validation.py tests/test_lifecycle_foreshadow_actions.py tests/test_outline_payoff_is_not_completion.py`：40 passed。

前端：`vitest run src/__tests__/foreshadowPlans.test.tsx`：4 passed；`tsc --noEmit` 通过。Python 编译与 diff 检查通过。`next build --webpack` 生产构建通过，20 页静态生成、类型检查和构建追踪完成。

按 shadow-mind 在本地分别完成正确性与完成范围复核（工具未提供可信只读子代理隔离）。核对了两个写入分支、图谱 null 到 PG 物化代码、现有 UI 请求及描述修改的局部更新语义；未发现本次修改的阻断问题。

## 未完成边界

存储适配器测试只验证真实端点函数的输入、属性修改和 Cypher 参数，未连接 PG/Neo4j，未证明真实事务、并发图谱物化、认证及浏览器端到端操作通过。状态组合一致不证明正文真正完成兑现，仍需要来源 ID、文本证据和作者核对。未按旧算术章号自动判断兑现早于埋设；持久化章号统一仍待完成。接近度不在本次状态/章号约定内，普通 PUT 不会重算它。旧不一致数据、后台抽取状态写入、图谱失败恢复仍待审计。未部署、未新增模型对照。
