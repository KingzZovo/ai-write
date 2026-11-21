# 设定提取图写入与投影修复

2026-10-07。未部署，真实PostgreSQL/Neo4j验收待网络恢复。

## 已改动

旧接口先在请求事务中创建PG人物，图中只按关系参与者建空人物节点；再调用独立PG会话从全图投影，既可能漏掉无关系人物档案，也可能与未提交人物写入互相等待。图上的每条规则/关系分开提交，后一步失败会留下半份提取；投影默认吞错，接口仍返回预估新增数量。

- 新增 `outline_settings_graph.py`，把本次人物/规则/关系放进一个Neo4j managed transaction。所有提取人物均建节点，新增节点保存完整profile_json；已有人物不覆盖，补缺失图节点时优先使用已有作者卡片。
- 新增图记录携来源书纲ID/摘要；人物区分author_card和outline_extraction来源。MERGE只在创建时赋值，避免重试重写既有资料。无有效参与者的关系不编造新人物。
- 删除请求会话中的PG人物新增/flush，统一由图投影创建。设定接口调用strict投影，失败返回503，新增数来自实际投影结果；使用同一个driver。
- 投影增加可选preserve_existing_profiles，仅本次设定导入启用，保护已经存在的PG人物卡；其他调用保持原行为。
- 本地复核发现：来源检查的项目FOR UPDATE会阻塞另一个会话新增资料的FK KEY SHARE。改为FOR NO KEY UPDATE，仍阻止项目修改/删除，允许外键检查；书纲仍锁定。未把SQL形态检查当作真实死锁验收。

## 验证

- 29项离线通过（0.56秒）：新增7项graph payload/事务包装/API失败路径，加设定源与作者文字优先回归。日志 `/tmp/aiwrite-settings-graph.log`。
- 覆盖人物档案完整性、已有作者档案来源、图写中断后不调用投影、投影失败不报成功、单次managed transaction、API严格模式和正确计数；源检查测试检验实际项目查询的锁SQL。
- 新增2项真实PG/Neo4j测试：来源锁保持期间完成投影，独立人物档案保存、已有作者卡保护、相同payload重试无新增；事务提交前异常回滚节点。**未执行**，环境禁止数据库网络连接。
- AST及diff检查通过。无前端改动，未重复前端测试。
- shadow-mind执行本地独立复核，无可信只读子代理隔离。架构视角核对去掉未提交PG写入后的单向投影；并发视角发现并修正来源项目锁与外键投影冲突。真实并发证据仍缺失。

## 仍待完成

Neo4j提交与PG投影不是分布式原子事务。图成功而投影失败会明确报错，图中已提交资料保留；相同payload可重试，但自动恢复队列和提取结果持久化还未接入，不能保证重试不再次调用模型。全图投影还会处理先前图资料，来源过期治理仍需专门处理；本次保留已有PG卡片并不代表其他投影入口已保护作者编辑。旧节点的空档案/历史来源修复也未自动执行。

整体目标仍未完成；需真实双库/全量回归、来源过期与恢复机制、浏览器联调和发布验收。

```bash
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_settings_graph.py tests/test_outline_settings_source.py tests/test_outline_authority.py
# 网络恢复后
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_settings_graph_pg.py tests/test_outline_settings_source_pg.py tests/test_entity_projection.py
```
