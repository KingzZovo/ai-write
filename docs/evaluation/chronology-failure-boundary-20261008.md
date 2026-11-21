# 全书位置解析失败不再冒用卷内章号

日期：2026-10-08。未部署。

ContextPackBuilder._resolve_global_chapter_idx 和 build 都有失败后退回 chapter_idx 的路径。卷内第3章与全书第3章不是同一位置；该值不仅影响伏笔年龄，还传给人物状态和历史出场读取，不能把错误伪装为可用位置。

本次移除两层回退，转为 RequiredContextUnavailable。卷查询同时校验 project_id，缺失/其他作品的卷不调用编号转换；转换结果必须为正整数，拒绝 None、布尔、字符串和非正数。日志只记异常类型，公开提示包含章节位置读取失败。

55项离线回归通过，包含真实解析/build方法适配器测试、作品过滤SQL检查、无效编号、查询失败、停止后续历史/事实读取，以及正常跨卷偏移、生成错误和前文失败保护。两个原先断言“失败退回本地编号”的测试更新为明确失败契约。Python编译和git diff --check通过。

按shadow-mind作本地正确性/完成度复核，无可信只读隔离未启动子代理。此处正常编号仍采用 foreshadow_lifecycle.chapter_global_idx 的旧章节数量偏移；持久化 Chapter.global_idx 和 history_before_global_idx 属于另一条时间线。删除章节后两者可能不同，后续必须统一读写与历史数据来源，不能把本次失败保护称作跨卷记忆全部修复。真实PG故障/多卷作品/模型产出尚未验收。
