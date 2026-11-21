# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:deepseek-v4-flash
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "7109b170f9908bf2", "scene_writer_system_sha256": "23bc9ee90d1bf072"}
- 调用:18 次,成功 16,失败 2
- 正文总字数:31260
- token 用量:输入 24080,输出 46179
- 累计耗时:634.359 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 0 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 8 | 0 |
| metaphor_stack | 6 | 0 |
| outcome_unfulfilled | 9 | 9 |
| fact_violation | 39 | 30 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:herb_stays | 2 |
| must_keep:debt_total | 3 |
| must_keep:father_gone | 3 |
| must_keep:fourteen_rooms | 3 |
| must_keep:left_arm | 2 |
| must_keep:no_neighbor_loan | 3 |
| must_keep:rain | 3 |
| must_keep:three_years | 3 |
| must_keep:three_years_vin | 2 |
| required_outcomes:brother_boards | 3 |
| required_outcomes:handles_waiter | 3 |
| required_outcomes:sister_stays | 3 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| scene_writer | 12 | 23354 | 28 |
| unknown | 4 | 7906 | 11 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | deepseek-v4-flash | stop | 1359 | 48.662 | 3495 | `eedad87c50cb03aa` |
| real_ferry-scene_writer | deepseek-v4-flash | stop | 1359 | 1.446 | 3495 | `eedad87c50cb03aa` |
| real_ferry-scene_writer | deepseek-v4-flash | stop | 1359 | 1.322 | 3495 | `eedad87c50cb03aa` |
| real_stall-scene_writer | deepseek-v4-flash | stop | 1649 | 40.288 | 2979 | `db6ec2eabb32332d` |
| real_stall-scene_writer | deepseek-v4-flash | stop | 1649 | 1.182 | 2979 | `db6ec2eabb32332d` |
| real_stall-scene_writer | deepseek-v4-flash | stop | 1649 | 1.116 | 2979 | `db6ec2eabb32332d` |
| fantasy_bell-scene_writer | deepseek-v4-flash | stop | 2419 | 149.81 | 2007 | `58dc091eda4f9b39` |
| fantasy_bell-scene_writer | deepseek-v4-flash | stop | 2419 | 0.958 | 2007 | `58dc091eda4f9b39` |
| fantasy_bell-scene_writer | deepseek-v4-flash | stop | 2419 | 1.008 | 2007 | `58dc091eda4f9b39` |
| fantasy_herb-scene_writer | - | - | - | - | - | 失败:None |
| fantasy_herb-scene_writer | deepseek-v4-flash | stop | 1759 | 169.037 | 3129 | `c628ef4c2e485ec1` |
| fantasy_herb-scene_writer | deepseek-v4-flash | stop | 1759 | 1.073 | 3129 | `c628ef4c2e485ec1` |
| mystery_bridge-scene_writer | - | - | - | - | - | 失败:None |
| mystery_bridge-scene_writer | deepseek-v4-flash | stop | 2012 | 59.463 | 3927 | `e17413e2abecd200` |
| mystery_bridge-scene_writer | deepseek-v4-flash | stop | 2012 | 0.962 | 3927 | `e17413e2abecd200` |
| mystery_lodger-scene_writer | deepseek-v4-flash | stop | 2479 | 155.517 | 2208 | `69117ed9600d971c` |
| mystery_lodger-scene_writer | deepseek-v4-flash | stop | 2479 | 1.008 | 2208 | `69117ed9600d971c` |
| mystery_lodger-scene_writer | deepseek-v4-flash | stop | 2479 | 1.507 | 2208 | `69117ed9600d971c` |

## 失败调用

- fantasy_herb-scene_writer:None
- mystery_bridge-scene_writer:None
