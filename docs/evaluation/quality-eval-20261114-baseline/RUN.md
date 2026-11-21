# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14"}
- 调用:18 次,成功 18,失败 0
- 正文总字数:24275
- token 用量:输入 7239,输出 6062
- 累计耗时:215.41 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 3 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 15 | 0 |
| metaphor_stack | 2 | 0 |
| outcome_unfulfilled | 22 | 22 |
| fact_violation | 28 | 20 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:both_saved | 1 |
| forbidden:windfall | 1 |
| must_keep:father_gone | 2 |
| must_keep:fourteen_rooms | 2 |
| must_keep:master_away | 1 |
| must_keep:no_neighbor_loan | 2 |
| must_keep:one_person_wide | 3 |
| must_keep:sister_ignorant | 3 |
| must_keep:three_years | 1 |
| must_keep:tonight_seal | 2 |
| must_keep:two_days | 1 |
| must_keep:wet_shoe | 1 |
| required_outcomes:brother_boards | 2 |
| required_outcomes:concrete_action | 2 |
| required_outcomes:handles_waiter | 1 |
| required_outcomes:judgment_changes | 2 |
| required_outcomes:makes_choice | 2 |
| required_outcomes:next_morning | 1 |
| required_outcomes:plan_made | 2 |
| required_outcomes:reads_letter | 1 |
| required_outcomes:recognizes_conflict | 2 |
| required_outcomes:shortfall_declared | 1 |
| required_outcomes:walks_to_pier | 3 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| bare | 6 | 7085 | 17 |
| chapter_single_shot | 6 | 8375 | 12 |
| scene_writer | 6 | 8815 | 13 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1752 | 10.786 | 438 | `4e26f7c3dd429d88` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1394 | 8.184 | 348 | `82ba7dd14be28774` |
| real_ferry-bare | gemini-3.1-pro | stop | 1373 | 9.093 | 343 | `9f78874bcf702623` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1574 | 9.39 | 393 | `6e7fd2d495f940dc` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1488 | 19.41 | 372 | `c2e48acb8a032a3a` |
| real_stall-bare | gemini-3.1-pro | stop | 1139 | 7.28 | 284 | `f12f4748420d075e` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1313 | 8.478 | 328 | `4d61fc0134f554f5` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1133 | 8.327 | 283 | `e311479e4e9ef3be` |
| fantasy_bell-bare | gemini-3.1-pro | stop | 1337 | 9.078 | 334 | `32e85230d8badd82` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1283 | 9.573 | 320 | `2e73212c8204c664` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1306 | 7.938 | 326 | `e817d43ff1c34b02` |
| fantasy_herb-bare | gemini-3.1-pro | stop | 1170 | 6.93 | 292 | `d1d0639920b3462b` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1372 | 21.717 | 343 | `7237b7495ac3f9e4` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1744 | 33.389 | 436 | `c3089020ddfb540a` |
| mystery_bridge-bare | gemini-3.1-pro | stop | 911 | 7.58 | 227 | `f093cbebb51f03ac` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1521 | 22.075 | 380 | `3bde80b5d396a59a` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1310 | 8.493 | 327 | `cd0ce96b82313cd1` |
| mystery_lodger-bare | gemini-3.1-pro | stop | 1155 | 7.689 | 288 | `01ef562f4fd79ecc` |
