# 长书认知来源校验：读取成本与失效保护

2026-10-08。基线 `7a3dac0`，代码及离线验证完成，未部署。整体修复目标仍未完成。

认知上下文原先每次读取全部已完成前章正文两遍。本批给章节增加数据库维护的正文 SHA-256，并缓存“这份确切来源及认知载荷的原句已验证”这一结果。首次读取仍验证完整正文；来源、实际载荷或作者决定变化后重新核对。缓存不保存小说正文、不决定人物实际知情状态。

## 实现与边界

- `a1001949` 新增 `chapters.content_hash`。数据库在 INSERT 或更新正文/指纹时计算原始 UTF-8 的 SHA-256，客户端提供的值不能覆盖实际正文指纹；正文比较使用 C 排序规则。摘要、章序等其他字段更新不调用该函数。
- 迁移一次回填已有正文的派生指纹，不改正文、摘要、认知结果、作者决定或时间戳。原来源 outbox 不把指纹字段自身变化当成新提取任务。回填需要真实 PG 验证耗时及锁等待，尚未执行。
- 单章、批量完整正文和轻量来源共用同一回执。未知/非法指纹回读完整正文；轻量来源本身不能证明有非空正文，也不能证明引文有效。
- `cognition_validation` 最多缓存 32768 个摘要。先核验当前来源、实际变化数组及作者修订完整性，再查缓存；缓存未命中须逐字验证完整原文。缓存逐出、进程重启只增加读取，不丢弃记录。
- 写作与评审继续通过 `load_ledger` 检查所有已完成前章，包括有效空数组。结束前重新读取全体轻量来源及实际载荷，并复查章节集合、顺序和目标边界。未完成提取、坏作者指针、同稿新校订、读取期间改稿或移动均拒绝拼成上下文。
- 补修已完成章节全书章号为 NULL、0、负数时被查询静默遗漏的问题。三项测试在修复前失败，现要求先修复顺序。无法定位的完成章不能假定为未来章节。

来源已核实不等于语义正确：引用可能支持不了模型的解释。人物的误信、怀疑、读者信息差和历史“未知”继续保留原限定，作者校订优先；文学合格仍按[中文作者/编辑标准](CHINESE_FICTION_EDITORIAL_STANDARD.md)审读。

## 500 万字读取验证

实际 SQLite SQL、测试内数据库指纹触发器、1005 章、5071235 字符、5021 条认知。规模正文使用重复填充，仅用于读量测试，不作为小说或语义召回语料。每批最多 200 章，冷/热实际提示逐字相同。

| 状态 | 本次返回给 Python 的正文字符 | 正文章节数 | SQL 次数 |
| --- | ---: | ---: | ---: |
| 冷缓存 | 5071235 | 1005 | 50 |
| 同进程热缓存 | 0 | 0 | 38 |
| 仅改一章并保存该章新认知后 | 5063 | 1 | 未单独计数 |

重点回归中的一次冷/热读取为 0.845/0.701 秒，仅为这次 SQLite 样例结果，不代表 PostgreSQL 或整条生成链的耗时。旧代码每次两遍正文读取的成本已从该路径移除；热缓存仍会读取全书轻量元信息、当前模型载荷及作者决定，时间和内存仍随认知记录数增长。不同进程各自建立缓存；超过缓存容量的工作集会重新回读，不能宣称所有长书访问都只读一章。

## 验证

从 `/root/ai-write` 使用已授权离线入口：

```bash
/root/.aiwrite-evaluation/run-tests.sh --offline \
  tests/test_cognition_validation.py tests/test_chapter_content_hash_migration.py \
  tests/test_cognition_workflow_offline.py tests/test_cognition_author_workflow.py \
  tests/test_cognition_author_edits.py tests/test_cognition_sources.py \
  tests/test_cognition_history_render.py tests/test_character_state_context.py -q

/root/.aiwrite-evaluation/run-tests.sh --offline tests -q \
  --deselect=tests/test_v10_observability.py::test_version_endpoint_public \
  --deselect=tests/test_v10_observability.py::test_metrics_endpoint_public
```

- 重点：**205 passed / 24 skipped**，17.84 秒；新增 40 项离线用例通过。
- 全目录：**2920 passed / 661 skipped / 2 deselected / 2 warnings**，66.30 秒。两项警告为既有伪签名测试短 JWT 密钥。
- 8 个变更 Python 文件 AST 通过，45 个认知/来源函数索引逐项核对；迁移源码单头 `a1001949`。
- 新增 7 项真实 PG 测试涵盖回填与原材料保留、outbox 不误派发、指纹防覆盖、正文/指纹事务回滚、元信息更新不触发 hash、实际热读取及降升级。全部在离线模式跳过，尚无实际 PG 运行结果。
- 本批无前端变更，未重复前端构建或行为测试。此前未执行的前端行为验收仍待完成。

重点命令实际另加 `--capture=tee-sys` 保存规模计数。日志及复核产物前缀为 `/tmp/aiwrite-cognition-validation-20261008-`：`focused-v2.log`、`full-v1.log`、`position-before.log`、`freeze-v1.json`、`review-v1.md`。

## 冻结复核与接续工作

按 shadow-mind 的无可信只读隔离回退规则，冻结后本地分别检查正确性/性能、归属/完成范围。两次复核期间工作树未变化；未发现需追加修复的功能缺陷，文档和索引在复核后同步。8 个源码/测试文件冻结指纹：`a7c1ab4b4304b2efb550f095c13d7ca1e5c815fd538c38c2edff1b3534c71bd6`。

该指纹以每个文件的 SHA-256 映射按路径排序后 JSON 编码，再作 SHA-256。复核不是独立子代理或真人作者盲评。当前结果不包括真实服务、迁移应用、网络、CPA 或部署验收。

后续仍须完成 Qdrant 整书认知语义召回、历史材料治理、真实 PG 迁移与写入成本核查，以及固定 CPA 上游的连续章中文编辑审读和部署。此前 Gemini 对照的路由及正文局限仍以[已有报告](editorial-prose-20261006/REVIEW.md)为准；本批没有调用模型或调整默认路由。
