# 真实写作链连续章探针(2026-11-14)
> **已被取代**:本目录只跑到第 1 章(当时还没接上实体/认知提取)。完整结果见
> [`../continuity-20261114-run10/REVIEW.md`](../continuity-20261114-run10/REVIEW.md)(3 章跑通,
> 第 4 章起被环境缺件挡住)。保留本目录是因为它记录了第一个阻断缺陷的原始报错。


目的:不再用"手拼提示词"验证,而是**跑生产链本身**
(`ContextPackBuilder` → `SceneOrchestrator` → `prompt_registry` → 真实模型),
看在隔离库上连续写多章会发生什么。

## 环境

```bash
docker compose -p aiwrite-eval -f /tmp/eval-compose.yml up -d   # PG 55432 / Redis 56379 / Qdrant 56333 / Neo4j 57687
cd backend
python -m scripts.quality_eval.continuity_probe \
  --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \
  --out ../docs/evaluation/continuity-20261114 --chapters 10
```

建表用的是 `Base.metadata.create_all`,不是 `alembic upgrade head`——

> **观察**:在空库上执行 `alembic upgrade head` 会在第一个迁移处失败:
> `ALTER TABLE prompt_assets ADD COLUMN endpoint_id UUID` → `UndefinedTableError`。
> 查 `alembic/versions/` 没有任何 `down_revision = None` 的根迁移,即这套迁移链
> 无法从零建库,必须先 `create_all` 再 stamp。这是既有状态,本轮未改动迁移。

## 结果

| 项 | 结果 |
|---|---|
| 第 1 章 | 生成成功,1029 字,真实摘要 156 字已落库 |
| 第 2 章 | **阻断**:`RequiredContextUnavailable: character_cognition_not_ready` |
| 第 3–10 章 | 未执行(第 2 章阻断后循环停止) |
| 网关报错 | 出现多次 `LLMRelayResponseError`,生产链自带 `llm_retry` / `llm_stream_retry` 重试后成功 |

## 发现一:HEAD 上"有角色的项目写不出正文"(已修)

第 2 章之外,第一次运行第 1 章就失败了:

```
RequiredContextUnavailable: character_states_context_unavailable
底层:NameError: name 'UUID' is not defined
      app/services/entity_context.py:197  verified_character_states()
```

`CharacterState.project_id == UUID(str(project_id))` 用了 `UUID`,而该模块没有导入它。
HEAD 提交 `854f9b9 feat(context): character location tracking + entity timeline context`
引入。后果不是降级而是**阻断**:`ContextPackBuilder._overlay_character_states` 把
`Exception` 归类成必要上下文缺失,于是只要项目里有角色卡,章节生成就整体失败。

- 复现:隔离 PG 建项目/卷/十章大纲 + 3 张角色卡,调
  `SceneOrchestrator.orchestrate_chapter_stream` → 立即得到上述错误。
- 修复:补 `from uuid import UUID`(一行),新增
  `tests/test_entity_context_character_states.py`(3 项,含"模块必须导入 UUID"的定点断言)。
- 修复后同一探针第 1 章正常产出。

## 发现二:正文直连服务层时,认知提取门槛会挡住第 2 章

第 2 章报 `character_cognition_not_ready`。查 `cognition_reader.validate_completed_receipt`:
它要求**已保存章节**存在 `status='completed'` 且 `source_hash` 与当前正文一致的
`ChapterCognition` 记录。也就是说,生产链假设"上一章的认知提取已经跑完"。

线上由章节保存触发器 + Celery 消费链保证;本探针把正文直接写进 `chapters.content_text`,
没有触发那条链,于是第 2 章被正确拦下。这一条**不是缺陷**,但它说明:

- 连续写作不是"写完一章接着写下一章"这么简单,中间的提取/投影是**前置条件**;
- 任何绕过持久化路径的脚本(包括本探针)都无法真正连写。

要把它跑通,需要在每章落库后按生产顺序执行实体提取与认知提取;本轮未实现。

## 发现三:第 1 章内部数字自相矛盾

```bash
python -m scripts.quality_eval.continuity_probe --database-url x \
  --out ../docs/evaluation/continuity-20261114 --check-only
```

新增的同章余数检查命中:

```json
{"check": "remainder_conflict", "chapter": "ch01.txt", "values": ["一千", "八百"],
 "detail": "同一章里「剩下/还欠」的数额不一致"}
```

正文里姐姐说「剩下的那一千......」,同一段弟弟接着说「剩下的**八百**,你也不用太着急」。
一笔欠款的余数在同一场对话里从一千变成八百。这是**单章内的硬伤**,也是"数字必须保持不变"
这一条最直接的失败样本。

## 第 1 章的编辑式审读(节选)

除数字外还有两类问题:

- **没有照大纲搭台**:章节要求写的是"傍晚渡口**小面摊**、周芸把装着两千元的**牛皮纸信封**
  压在**碗下**";正文变成渡口边空手交接,钱是「用**报纸**裹着的」,碗是「印着蓝边花纹的
  **粗瓷旧碗**」,信封不见了。地点与道具被整体替换。
- 结尾「踩着湿滑的石阶上了船」把"留在岸上"写反了(要求是弟弟上船、姐姐留在岸上)。
  实际正文里是姐姐上船回家。
- 可取:交接时的推让、点钞动作、「你先把钱收好」这类对白是自然的,没有用强度词堆气氛。

结论:**需局部修订**(数字自相矛盾 + 道具/地点偏离要求 + 结尾站位写反)。

## 未做 / 未验证

- 连续 10 章**未完成**(第 2 章起被认知门槛挡住),`continuity.json` 只覆盖 1 章。
- 100 章规模稳定性**未执行**。
- 检查器只做了:字数区间、重复开场、跨章同名目金额冲突、同章余数冲突、道具是否出现。
  **位置、时间推进、知情边界没有确定性检查**,只有上面的编辑式阅读,不能声称"0 违规"。