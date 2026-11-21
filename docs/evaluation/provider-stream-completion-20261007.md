# 流式响应结束原因校验

## 已发现问题

直接OpenAI流只传出正文，忽略finish_reason；Anthropic获取最终消息被包在best-effort指标捕获中，也未校验stop_reason。因此即便供应商明确返回length/max_tokens，消费者仍可能看到正常迭代结束，进入评审或保存。句末有句号不能证明传输完整。

## 改动

`provider_response_guard.require_normal_finish`拒绝明确非正常结束，固定异常`provider_incomplete_response`不携供应商内容。兼容已知正常stop/end_turn/stop_sequence；缺失元数据None沿用旧端点行为，完整性仍未知。

OpenAIProvider.generate_stream记录最终非空结束原因，尾部usage块不清除；统计usage后检查并抛出。AnthropicProvider.generate_stream在text_stream结束后获取final_message并检查，异常不再被指标catch吞掉；指标记录仍best-effort。已经交付的文字不会被倒退删除，消费者收到异常后须按已有恢复规则处理。非流式GenerationResult路径本次未改动。

## 证据与复核

79项离线测试通过：新供应商流完成用例12项，及已有结束原因、代理错误、流式重试与指标、内部流相关用例。新用例验证带句号的半稿仍失败、尾部usage保留、错误后关闭供应商流、Anthropic最终消息读取失败传播。已有tier测试覆盖中途报错不切换模型拼接内容。

本地按shadow-mind做两轮复核：正确性检查确认错误在正文已交付后传播、未落入供应商重试循环；调用链检查确认scene_writer经registry流消费、tier在yielded_any时重抛，registry仅正常迭代结束记成功。无可靠只读子代理隔离，未启动子代理。AST/diff检查通过。

## 验收边界

未实跑CPA/Anthropic端点；完整ASGI/Celery的失败保留与UI显示本次未复验。缺少结束元数据的旧端点、非流式普通正文、整章改写对原稿的事实/情节保留仍需继续检查。结束正常只代表供应商正常停止，不代表章节文学完整或中文质量合格。未新增迁移，未部署；累计代码仍依赖a1001934及前序迁移。
