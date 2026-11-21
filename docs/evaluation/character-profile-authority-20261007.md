# 人物卡作者修改保护

状态：代码与离线回归完成，真实双库验收及部署待完成。

## 发现与修正

普通 `_materialize_entities_to_postgres` 原先只要 Neo4j 档案非空，就会整份覆盖 PostgreSQL 人物卡。此前书纲导入的 `preserve_existing_profiles=True` 只保护该入口，普通后台投影仍可能把历史作者卡片覆盖成旧图档案。另一方面，非空判断会忽略作者明确保存的 `{}`，导致清空后页面仍看到旧内容。

确认当前作者入口为 `/neo4j-settings/characters`，旧 PostgreSQL 写接口已禁用。因此没有把所有更新一律屏蔽：

- 作者保存档案时，图节点同时设置 `profile_origin='author_edit'`。
- 新增 `character_profile_authority.projected_profile` 决策：只有明确作者编辑可替换已有卡片，空对象也是有效修改；自动提取、author_card 历史快照、无来源或未知来源均不覆盖已有卡片。
- 新人物仍用图档案初始化。已有空卡片也保留，因为无法推断它是不是作者主动清空。
- 别名折叠保留既有规则，别名节点的档案不能覆盖本名卡片；书纲导入的保留模式不变。

## 证据

`test_character_profile_authority.py`、`test_entity_graph_hygiene.py`、`test_outline_settings_graph.py`、`test_outline_settings_delivery.py`、`test_outline_settings_source.py`：66 passed。

除纯函数边界外，测试调用实际作者保存函数，检查来源标记；实际 materializer 在隔离图/SQL 替身下验证作者更新、清空、旧档案保留、空卡片保留及新人物创建。替身测试不证明实际数据库事务或并发正确。

扩展 `test_outline_settings_graph_pg.py`：原保留测试同时覆盖普通默认投影，新增明确作者更新及清空的双库测试。该文件 5 项成功收集，本轮增加 2 个测试分支；当前网络权限限制数据库访问，未实跑。

5 个修改/新增 Python 文件 AST 与 `git diff --check` 通过。本轮无前端改动，不沿用历史全量后端或构建通过结论。

## 本地独立复核

按 shadow-mind 无可靠只读隔离的回退流程，检查资料来源与兼容性、作者操作与遗漏两条线。复核确认不能自动填充已有空卡片，否则会恢复作者主动删除的内容，已修正并补实际投影测试。当前官方图档案写入只有作者保存设置 author_edit，书纲提取仍使用固定自动来源。

## 未完成事项

- author_edit 是来源分类，尚无版本序号；两个作者请求或旧图快照并发投影仍可能覆盖更新后的作者档案。下一步需要版本/CAS、序列化及失败恢复。
- 作者保存入口仍是图写入后尽力同步，缺少持久回执；不能把 accepted 当作已同步完成。
- 设定侧栏按字段失焦时发送整份旧档案，存在快速连续修改互相覆盖的风险；人物页面延迟刷新和项目切换隔离也待修。
- 历史无来源图档案不自动认定为作者编辑，旧数据分歧需要人工核对或明确重新保存；不回填来源、不批量改历史人物卡。
- 其他结构化资料、书纲改稿后的旧图来源清理、实际数据库与浏览器验收、发布仍在总计划中。
