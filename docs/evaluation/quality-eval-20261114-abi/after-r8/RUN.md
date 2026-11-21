# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "7109b170f9908bf2", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:48 次,成功 48,失败 0
- 正文总字数:59092
- token 用量:输入 26480,输出 14758
- 累计耗时:452.539 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 7 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 6 | 0 |
| metaphor_stack | 1 | 0 |
| outcome_unfulfilled | 60 | 60 |
| fact_violation | 73 | 46 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:both_saved | 2 |
| forbidden:new_benefactor | 1 |
| must_keep:bell_destroyed | 2 |
| must_keep:debt_total | 1 |
| must_keep:father_gone | 5 |
| must_keep:fourteen_rooms | 5 |
| must_keep:half_jar | 1 |
| must_keep:master_away | 1 |
| must_keep:no_neighbor_loan | 4 |
| must_keep:one_person_wide | 1 |
| must_keep:price | 1 |
| must_keep:rain | 3 |
| must_keep:rule | 2 |
| must_keep:sister_ignorant | 3 |
| must_keep:ten_breaths | 3 |
| must_keep:three_years | 2 |
| must_keep:three_years_vin | 2 |
| must_keep:tonight_seal | 1 |
| must_keep:wet_shoe | 3 |
| required_outcomes:acts | 4 |
| required_outcomes:brother_boards | 5 |
| required_outcomes:concrete_action | 6 |
| required_outcomes:consequence | 1 |
| required_outcomes:decision | 2 |
| required_outcomes:discovers_shortage | 1 |
| required_outcomes:handles_waiter | 6 |
| required_outcomes:judgment_changes | 5 |
| required_outcomes:makes_choice | 2 |
| required_outcomes:next_morning | 1 |
| required_outcomes:plan_made | 3 |
| required_outcomes:price_named | 1 |
| required_outcomes:reads_letter | 4 |
| required_outcomes:recognizes_conflict | 6 |
| required_outcomes:shortfall_declared | 5 |
| required_outcomes:sister_stays | 3 |
| required_outcomes:walks_to_pier | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| scene_writer | 42 | 51508 | 88 |
| unknown | 6 | 7584 | 18 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1705 | 9.277 | 426 | `c83649a75b2a2b04` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1593 | 16.187 | 398 | `165015ffac6fab34` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1219 | 8.222 | 304 | `b08c3f380116f02a` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1576 | 8.904 | 394 | `d603fcf43f3f81a7` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1223 | 7.982 | 305 | `56a502929abf86c0` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1648 | 9.513 | 412 | `827ad9b87b5d0350` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1926 | 10.07 | 481 | `2f6fbef93c0b335f` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1492 | 8.949 | 373 | `f8535a8bdf07d35e` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1749 | 9.092 | 437 | `18d08401fbe17a48` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1600 | 8.72 | 400 | `3ad361905649b378` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1248 | 7.899 | 312 | `b537c012ea5a6592` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1373 | 7.615 | 343 | `9c693579cae1961a` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1399 | 7.25 | 349 | `15043fdc84020ce2` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1482 | 8.458 | 370 | `531022ea254964ba` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1239 | 6.699 | 309 | `02f56669b58cba1c` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1499 | 9.327 | 374 | `02de47e47d820572` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1223 | 15.486 | 305 | `42b55df9827b9350` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 892 | 6.167 | 223 | `6f012102e6a54230` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1072 | 6.864 | 268 | `c5c1773396ec5d2d` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 992 | 7.35 | 248 | `eb075337db85bb8f` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 970 | 6.638 | 242 | `11ccbaf3b0188d16` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 983 | 5.833 | 245 | `e1c92a76a0586731` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1412 | 16.23 | 353 | `c8cee69e975572b0` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1132 | 7.167 | 283 | `31a54b93e953dd40` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1049 | 7.084 | 262 | `24fbce55270714af` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1248 | 8.581 | 312 | `52d0f4abb331b007` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1290 | 8.501 | 322 | `63c49110cb888603` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 908 | 6.663 | 227 | `850f8ca6f8428a5c` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1234 | 24.078 | 308 | `6544fded77508f6f` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1371 | 18.561 | 342 | `f4cb73e8d62de2d8` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1221 | 6.612 | 305 | `fbd9b5421cd6c083` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 658 | 5.225 | 164 | `6bfeb6a770a36f89` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 853 | 6.113 | 213 | `78c712b5723b4b9c` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 885 | 6.503 | 221 | `1c6f5879902edbcb` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1068 | 7.035 | 267 | `3197145e3a4471b9` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1145 | 6.345 | 286 | `111b85da4887145f` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 823 | 7.378 | 205 | `4b59ffbc93801e05` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 941 | 6.237 | 235 | `df0b6633a2dc2a8a` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 885 | 6.28 | 221 | `1e9f26444daa4783` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1105 | 6.163 | 276 | `4438f9f9a5777fa2` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1005 | 7.402 | 251 | `c9374420dfdc72da` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1241 | 18.939 | 310 | `6668ba3b53821d62` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1092 | 7.291 | 273 | `865bf99a42ced45a` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1248 | 7.084 | 312 | `c0055941b8e99dec` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1413 | 23.349 | 353 | `d650cd42f72802a0` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1373 | 19.38 | 343 | `ec9db720e97c3570` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1230 | 8.501 | 307 | `17e7fe88ccb5bd72` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1159 | 7.335 | 289 | `575fe0feed28c7ff` |
