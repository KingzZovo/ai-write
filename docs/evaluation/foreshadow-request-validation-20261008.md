# 伏笔请求校验（2026-10-08）

创建时拒绝空白类型/描述；更新时拒绝显式清空必填属性，省略字段仍表示保持原值，可空字段仍允许清空。状态限定为 planned、pending、planted、ripening、ready、resolved。创建/更新/兑现章号只接受非负整数，不将布尔、字符串或小数强制转换成章号。有效正文描述保留原始空格。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_request_validation.py tests/test_lifecycle_foreshadow_actions.py tests/test_outline_payoff_is_not_completion.py`，31 passed。Python 编译和 diff 检查通过。首次同步测试依赖调用停滞后中断，改为异步适配器后重跑通过；中断运行不计入通过结果。

复核：依照 shadow-mind 在本地分别核对正确性和完成范围，当前工具不提供可信只读子代理隔离。检查了 exclude_unset 的省略语义、前端整数字段、PG 与图谱两个写入分支。HTTP 测试仅挂载路由并替换存储依赖，验证非法 PUT 在 execute/flush/graph.session 前返回 422，不证明真实认证、数据库事务或图谱集成通过。

遗留：状态和兑现章号组合仍需结合已有记录校验；旧数据未清理。图谱 JSON 可空字段现有物化语义与 PG 原生 null 不完全一致，本次未改变。发现 pending 记录缺前端分组，将单独修复。未部署、未运行模型，不能据此宣称中文小说质量达标。
