# 兑现建议持久化（2026-10-08）

新增 ForeshadowReview / foreshadow_reviews 和迁移 a1001936（父版本a1001935）。每条记录保存原句、字符范围、解释以及章节和伏笔内容快照，初始状态pending；与实际伏笔状态分开。作品、章节、伏笔删除时按外键级联删除相关审阅记录。章节ID+伏笔ID+来源快照摘要唯一，重复核查ON CONFLICT DO NOTHING，不替换旧证据、不把已处理状态重置成pending。

新 foreshadow_review_store.save_resolution_review 独立拥有写事务，不提交HookManager借用的会话。模型返回后再打开短事务：锁章节和所属卷，重新核对作用域、持久化位置、正文摘要、更新时间及状态；按ID顺序锁PG伏笔记录，核对内容摘要；检查原句和字符范围后写入待审阅记录。来源已变化则跳过，无效证据抛错并由事务回滚。调用链在模型核查完成后才进入存储事务；保存不修改伏笔状态。

HookManager已接入存储，日志记录实际新增条数。没有建议不打开写会话。当前仅批量生成的现有post-hook调用链接入，其他生成入口是否触发同一流程仍需核对。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_store.py tests/test_foreshadow_review_source.py tests/test_foreshadow_resolution_suggestions.py tests/test_hook_chronology_failure.py tests/test_global_chapter_idx.py`，最终75 passed。Python编译与diff检查通过。

存储测试实际使用SQLite内存库及事务，覆盖落库、去重保留dismissed状态、正文/位置/章节状态/伏笔描述/作品归属变化拒绝、无效证据导致整批回滚、结束位置越界、钩子参数透传。迁移upgrade/downgrade在SQLite执行，核对列、唯一约束、外键和索引；PostgreSQL锁语句仅编译核对，未证明真实锁竞争、死锁恢复或并发正确性。

按shadow-mind在本地完成正确性和完成范围复核（无可信只读子代理隔离）。发现Python切片允许结束位置超出全文仍匹配，已补显式边界和回归测试后通过。核对短事务归属、来源比较、冲突不更新语义以及模型/迁移结构，无本次阻断问题。

未完成：迁移尚未执行，代码尚未部署。审阅列表API/界面、确认与驳回、确认时再次核对两侧版本均待接入。pending仅表示未处理，不保证来源此刻仍新鲜，读取/采用必须重验。图谱来源伏笔目前比较PG物化记录，图谱同步延迟与跨存储确认仍待解决。真实PG事务/浏览器/中文小说验收未完成，不以SQLite测试替代。
