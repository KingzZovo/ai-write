# 参考书检索接口兼容性

本机安装的qdrant-client已移除旧search方法，而QdrantStore的plots/styles、style_profiles/beat_sheets/style_samples_redacted和通用向量检索仍调用它。多数入口吞掉异常并返回空结果，导致绑定参考书也可能没有向量风格/情节参考。

统一为`_search_vector_points`适配：新版客户端使用query_points，旧客户端仍可用search；项目记忆与场景样本共用同一适配，不更改各自的作用域、阈值、数量和异常处理约定。参考书不会混入项目历史事实槽位，原有不向写作提示注入参考书原文的行为未改变。

新增8项真实本地Qdrant/接口兼容测试：五类参考集合的书籍过滤与相似度排序，通用过滤和场景类型+画像双条件过滤，旧客户端兼容，以及真实查询结果进入ContextPack最终system prompt。异书候选故意具有更高相似度，验证仍被排除。

针对性组合78 passed、2 skipped（两项既有PG章序测试因该命令没有设置专用测试库变量跳过）；完整回归设置专用PG/Redis环境，**1602 passed, 5 warnings，无跳过**（108.24秒）。日志 `/tmp/aiwrite-reference-query-full-suite.log`；既有短JWT测试密钥、SQLAlchemy连接清理及Qdrant版本探测警告未抑制。

按shadow-mind在无可信只读子代理隔离的条件下完成本地正确性、架构双视角复核：所有QdrantStore向量查询已共用适配；通用查询原本抛出的错误仍抛出，参考检索原本降级为空的行为保留；book_id/scene_type/profile_id过滤未放宽。真实Qdrant过滤/排序与上下文渲染测试补足旧Mock Store不能发现SDK接口失效的覆盖缺口。

这是调用链可用性修复，不是新一轮文学质量通过证明。测试参考特征是合成夹具，不作为已阅读中文小说的证据；真实中文作品对照沿用文学评测文档。ContextPack v2仍需启用开关和绑定参考书；本轮未改线上配置、未部署，后续仍需真实模型与整书连续性验收。
