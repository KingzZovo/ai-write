# 定点改写拒收已知不完整响应

## 修复

此前GenerationResult只保存text/usage/model/provider，供应商结束原因被丢弃。一个因max_tokens截断的局部改写，只要长度落在允许区间内，就可能替换完整原稿；句号不能证明模型正常完成。

GenerationResult新增默认None的finish_reason，不破坏既有四参数构造。Anthropic普通调用保留stop_reason；OpenAI兼容普通调用保留第一候选finish_reason；内部流拼接保留终止片段原因，后续只有usage的片段不清空它。原文与usage透传不变。

定点改写路由只接受已知正常的stop/end_turn/stop_sequence；有明确其他原因时抛出固定错误targeted_revision_incomplete_response，不包含响应原文或端点诊断。当前SSE和worker已有停止逻辑会保留候选，不升级成整章重写。

## 验证与复核

- 85项改写/路由/供应商响应回归，加18项模型分层路由回归，共103 passed。
- 新增18项覆盖OpenAI普通结束原因、内部流usage尾片与资源关闭、Anthropic结束原因，以及即使句号收尾仍拒收length/max_tokens/content_filter/tool_calls/tool_use/unknown；正常终止与旧式无元数据兼容行为也有覆盖。
- 3文件AST、git diff --check通过。
- 按shadow-mind无可靠只读隔离回退，冻结后本地核对三个GenerationResult构造点、流关闭、字段兼容性，以及上层失败不重写全文的处理。没有新增数据库字段或变更共享CPA。

## 剩余限制

None表示供应商没有提供或旧替身未声明，继续兼容既有调用，不代表已经验证输出完整。真实CPA/Gemini是否可靠返回结束原因、流截断但无终止片段的识别仍待运行验证。当前保护用于定点改写；其他生成调用虽然能拿到该字段，不代表已逐一建立完整性门禁。直接generate_stream文本流接口也未在本轮改变合同。

结束原因只提供传输/生成状态证据，不证明原文事实、人物连贯性或文学质量。未做新小说实写、真实端点/数据库/浏览器验收或部署，不能宣称全部完成。
