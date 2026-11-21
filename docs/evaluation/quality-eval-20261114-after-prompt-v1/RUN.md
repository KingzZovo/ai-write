# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14"}
- 调用:18 次,成功 18,失败 0
- 正文总字数:25613
- token 用量:输入 7800,输出 6395
- 累计耗时:228.778 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 2 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 28 | 0 |
| metaphor_stack | 1 | 0 |
| outcome_unfulfilled | 12 | 12 |
| fact_violation | 32 | 17 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:new_income | 1 |
| must_keep:bell_destroyed | 2 |
| must_keep:father_gone | 3 |
| must_keep:fourteen_rooms | 2 |
| must_keep:no_neighbor_loan | 1 |
| must_keep:rain | 1 |
| must_keep:rent_tomorrow | 1 |
| must_keep:rule | 1 |
| must_keep:sister_ignorant | 2 |
| must_keep:ten_breaths | 1 |
| must_keep:tonight_seal | 1 |
| must_keep:wet_shoe | 1 |
| required_outcomes:consequence | 1 |
| required_outcomes:handles_waiter | 2 |
| required_outcomes:judgment_changes | 2 |
| required_outcomes:next_morning | 1 |
| required_outcomes:recognizes_conflict | 3 |
| required_outcomes:shortfall_declared | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| bare | 6 | 7723 | 10 |
| chapter_single_shot | 6 | 10531 | 11 |
| scene_writer | 6 | 7359 | 8 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1572 | 9.305 | 393 | `28f0fa670d3d6b8e` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1533 | 9.843 | 383 | `14264d1d1992006b` |
| real_ferry-bare | gemini-3.1-pro | stop | 1525 | 8.457 | 381 | `d305ceafed68a3ae` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1497 | 8.503 | 374 | `cc326cb4b42d233c` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1669 | 10.653 | 417 | `55f8f9e9c69fef48` |
| real_stall-bare | gemini-3.1-pro | stop | 1332 | 8.086 | 333 | `2602c65238c4a302` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1078 | 7.05 | 269 | `1347176b10948952` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1686 | 15.304 | 421 | `25e8ef4aec92aec5` |
| fantasy_bell-bare | gemini-3.1-pro | stop | 1231 | 26.352 | 307 | `f6d26cebedba713d` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1455 | 7.376 | 363 | `33f1360f419a733a` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 3250 | 33.703 | 812 | `6d98dfa5a4d33d8b` |
| fantasy_herb-bare | gemini-3.1-pro | stop | 898 | 6.425 | 224 | `3d613fbfa8ea3a63` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 823 | 6.829 | 205 | `2a042b6a061601d8` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1319 | 17.438 | 329 | `6fef63d4c65d592f` |
| mystery_bridge-bare | gemini-3.1-pro | stop | 1227 | 7.853 | 306 | `c37051aeb418576a` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 934 | 6.61 | 233 | `64f8b66e9f09155d` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1074 | 7.456 | 268 | `ccef71f34e35ffe2` |
| mystery_lodger-bare | gemini-3.1-pro | stop | 1510 | 31.535 | 377 | `abf3541cc54bfaa8` |
