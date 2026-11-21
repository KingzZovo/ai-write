# 后端测试入口

在 `backend/` 目录、已安装锁定依赖及 pytest/pytest-asyncio 的环境中运行。

## 离线回归

```bash
PYTHONPATH=. python -m pytest --offline -q tests/
```

纯函数、模型模拟、SQLite与ASGI进程内接口照常执行。需要真实数据库、图谱、队列或进程服务的用例标记为 `@pytest.mark.external_services`；使用共用 `runtime_db` 及其派生夹具的用例也自动获得此标记。`--offline` 在执行夹具前跳过这些测试，不把它们计为通过。

`tests.support.offline` 从pytest配置阶段起，在当前Python进程拦截连接、监听和DNS调用；未标记用例发生这些操作会失败，即使被测服务捕获异常并返回空结果。之后导入测试模块或收集用例时发生连接也会报错。SQLite本地查询和ASGI传输不受影响。检查器是防止意外访问的测试工具，不是操作系统沙箱，不覆盖此前的进程启动，也不能约束任意子进程；会启动外部服务的测试必须提前标记。

共用 `tests.support.prompt_cache_state` 夹具在每个测试结束时取消并等待该测试的Prompt计数后台任务，然后清空缓存；不会把假提示词ID的计数刷入数据库。测试过程中仍能启动、验证真正的周期刷新逻辑。它依赖 `monkeypatch`，使异步任务清理先于测试替身恢复。

`tests.support.sqlite_types`只在SQLite测试DDL中把PostgreSQL的UUID类型映射为`CHAR(32)`，避免随机ID被SQLite当作数字/科学计数法而失真或碰撞；PostgreSQL本身的UUID类型和应用绑定方式不变。固定纯数字/含e的UUID用例验证身份及唯一性。

通过 `--noconftest` 单独运行既有测试时，显式添加 `-p tests.support.offline -p tests.support.prompt_cache_state -p tests.support.sqlite_types --offline`。这不会加载其他共用夹具；需要 `auth_client` 等夹具时使用正常入口。

2026-10-08本机全目录运行显式排除了 `tests/test_v10_observability.py::test_version_endpoint_public` 和 `tests/test_v10_observability.py::test_metrics_endpoint_public`：环境中的同步ASGI处理器线程池等待无法可靠完成。这两项仍保留在CI入口，不能把本机排除记录当作通过。具体结果见 [本轮报告](../../docs/evaluation/offline-regression-profile-20261008.md)。

## 真实服务集成

不传 `--offline` 时完整套件仍执行外部服务测试。沿用CI中明确命名的可丢弃PostgreSQL、Redis与Neo4j实例及数据库初始化步骤；不能指向生产服务。共用 `runtime_db` 拒绝非本机、非 `aiwrite_pipeline_test` 的数据库，并为每次测试创建独立schema。

离线作业与原有真实服务CI作业分别阻断失败。修改服务边界时，需要两类结果；离线通过不能证明迁移、数据库锁竞争、worker恢复或真实端点可用。

## 中文生成验收

真实模型调用继续使用 `backend/scripts/evaluate_*.py` 的固定材料和人工审读记录，遵守 [中文作者/编辑标准](../../docs/evaluation/CHINESE_FICTION_EDITORIAL_STANDARD.md)。测试通过、模型自评分和词频统计都不等于正文合格。模型选择须保留同约束样章、失败结果和人工修改情况。
