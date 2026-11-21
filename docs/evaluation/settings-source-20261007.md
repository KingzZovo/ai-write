# 设定提取来源复核

2026-10-07。未部署，真实PG/Neo4j验收未完成。

整书设定提取此前使用请求开始时的raw_text，模型返回后直接写人物卡和图数据库。期间作者修改书纲时，旧提取结果仍可能写入。新增 `outline_settings_source.py`，在模型前记录项目/书纲行摘要并释放读事务；模型完成后按项目→书纲顺序加锁、刷新缓存和比较内容/版本/确认状态/项目资料。变化或删除返回409，发生在任何图数据库初始化、人物新增和资料投影之前。来源合法性错误保留400/404区分。

核对成功后的来源行锁保持到请求事务结束，避免写资料期间官方改稿入口修改来源。旧人物卡仍采用原有去重语义，不擅自覆盖作者资料。

验证：

- 33项离线测试通过（0.57秒）：新增12项来源及真实API模拟测试，加10项文字优先和11项确认回归。覆盖原文、版本、确认、项目设置、删除变化、错误类型/空来源、图数据库写入前拒绝、未变化正常返回。日志 `/tmp/aiwrite-settings-source.log`。
- 新增2项真实PG门禁：旧会话刷新后识别改稿、来源核对事务结束前阻塞官方编辑。**当前网络限制下未运行**。
- AST、git diff --check通过；无前端改动。
- 按shadow-mind执行本地独立复核（无可信只读子代理隔离）：源锁顺序与编辑接口一致；模型调用前rollback；冲突检查前无图/人物写入；无实体结果仍可结束。复核保留了原缺失资源404语义，未将其统一误改为400。

待完成：现有跨PG/Neo4j写入不是单一原子事务，图写入成功而后续PG失败的恢复仍需处理；已有陈旧人物卡/规则的来源标记、作者编辑保护及重新提取冲突管理另待审。本批不证明这类已有资料已经修复，也不证明图数据库实测通过。源资产之外的共享风格配置、长篇文学质量及发布验收继续。

```bash
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_settings_source.py tests/test_outline_authority.py tests/test_outline_confirmation_unit.py
# 网络恢复后执行
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_settings_source_pg.py tests/test_outline_confirmation_pg.py tests/test_chapter_project_scope.py
```
