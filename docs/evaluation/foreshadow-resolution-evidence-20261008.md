# 伏笔兑现核查：模型建议不再直接改事实（2026-10-08）

## 已修复

原 `ForeshadowManager.check_resolution` 只传正文前3000字，使用 `bool(data.get("resolved"))`，因此字符串 `"false"` 也会被当作真值。随后直接把PG记录改为resolved，缺少原文依据且绕过作者核对，图谱物化还可能覆盖它。

现在返回不可变的 ResolutionSuggestion 列表，不修改任何伏笔属性、不flush。即使模型返回真实布尔true，也只形成待核对建议；必须同时包含原文连续引句和非空解释，引句必须逐字存在于本次输入。建议附伏笔ID、输入全书章号、引文字符起止位置（Python字符索引）、完整正文SHA256。重复原句采用首次出现位置，不代表唯一定位。日志改为“需作者审阅”，不再宣称已兑现。

提示按中文编辑核查问题展开：区分复述悬念、猜测、预告与具体揭示，要求指出原问题得到怎样的回答及尚存不确定性。引句存在只能证明引用来自输入，不能证明文学上的收束充分。

移除3000字截断，使用完整正文；正文与提示材料合计超过24000字符明确抛错，不作截段判断。章号缺失/布尔/负数等拒绝，空正文不调模型。只认字典内真正的布尔true；伪造引文、空解释、非对象JSON、字符串布尔及明确截断响应不形成建议。

修正旧 tracking_chain 测试仍要求大纲直接标planted的过期约定，现要求planned，与已有大纲计划/正文事实边界一致。

## 验证

最终命令：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_resolution_suggestions.py tests/test_foreshadow_tracking_chain.py tests/services/test_foreshadow_debt.py tests/test_foreshadow_request_validation.py tests/test_lifecycle_foreshadow_actions.py tests/test_outline_payoff_is_not_completion.py`。

88 passed；Python编译及diff检查通过。新增19项测试包含第3000字之后的引文、来源摘要与位置、无状态变更/flush、字符串false及伪造证据、两种预算越界、无章号与明确截断。模型和存储使用适配器，未新增真实模型调用，未证明数据库和实际小说验收通过。

按shadow-mind在本地做正确性与完成范围复核；当前无可信只读子代理隔离。核对唯一调用者HookManager与返回类型兼容、状态属性未写入、原文完整传递、错误不会自动确认事实。未发现本次边界修改的阻断问题。

## 仍需完成

目前HookManager仅记录建议数量，不持久化建议或展示审阅卡片；作者仍可通过原有手动兑现操作确认，但不能在界面查看这些自动建议。因此证据审阅工作流未完成，当前后台核查仍有模型开销。后续需接入稳定章节ID、正文/伏笔版本核对、持久化与审阅界面，避免确认过时建议。现有hook全书章号回退也尚待替换；摘要仅证明输入快照，不证明已落库的当前正文。

旧误兑现状态未自动清理，计划转真实埋设和后台抽取来源仍待审计；超长正文暂未支持分段覆盖核查。语义错误仍可能产生建议，必须由作者结合前文检查。尚未部署、未运行数据库集成或新的Gemini文学对照。
