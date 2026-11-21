# 参考文笔链路:从"传了书"到"能用它的文笔写"(2026-11-20)

用户目标:用《龙族》《天之炽》(以及之后可能补的《赤心巡天》《天命之上》)的**文笔**来写长篇。

## 一、素材与采样

| 文件 | 规模 |
|---|---|
| 龙族全套(龙族 I–IV + 前传) | 227.6 万字 / 236 章 |
| 天之炽 | 72.2 万字 / 156 章 |
| 天之炽 2 女武神 | 22.1 万字 / 77 章 |

采样:`scripts/ops/make_style_sample.py` 每本**跨全书均匀取 25 章 × 1600 字** →
合并成 11.1 万字样本。理由:文笔蒸馏看的是句式/节奏/用词的**统计特征**,不需要读完每一个字;
均匀取样比只取开头更能代表整体(开头往往只有铺垫)。合并成一张卡是因为三本同属一位作者、
文风接近;要分开出三张卡随时可拆。

## 二、这条路上一共修掉 6 个真缺陷

每一个都会让"参考文笔"**永远不可能生效**,而且症状都指向别处,极难定位:

| # | 缺陷 | 症状 | 处置 |
|---|---|---|---|
| 1 | `text_chunks.block_idx` / `char_count` 为 NOT NULL 未填 | 导入直接 `NotNullViolationError` | 导入脚本补两个字段 |
| 2 | 端点密钥用**不同 SECRET_KEY** 加密(探针用 `continuity-probe-secret`,临时脚本用默认值) | `ApiKeyDecryptError` → 端点被跳过 | 导入脚本加"密钥自愈":解不开就用已知明文重写密文 |
| 3 | **一条坏端点行会打挂整次模型配置加载** —— embedding 分支的解密没有兜底 | `no_embedding_provider` / 模型配置加载失败,**连正文生成一起挂** | `model_router.load_from_db` 的 embedding 分支包 try/except:只禁用 embedding,不拖垮整次加载 |
| 4 | `prompt_assets` 累积到 **1290 行**(探针每次 bootstrap 重插一整套),embedding 的 180 条路由大多指向**已删除端点** | 路由反查到死端点 → `no_embedding_provider` | 探针种路由改为**幂等**(有则更新、无则插入);库里清理到 7 行 |
| 5 | Qdrant 集合维度**写死 4096**(`plots`/`styles`/`style_profiles`/`beat_sheets`/`style_samples_redacted`),而现网模型 `nvidia/nemotron-3-embed-1b` 是 **2048** 维 | 蒸馏写入全部 `400 Wrong input: Vector dimension error` | 改为 `_resolve_embed_dim()`:env `EMBEDDING_DIMENSION` > 配置项 > 2048;新增 `settings.EMBEDDING_DIMENSION=2048` |
| 6 | 蒸馏需要的提示词任务(`style_abstraction` / `beat_extraction` / `redaction`)没种进 `prompt_assets` | `No active prompt registered for task 'style_abstraction'` | `_seed_prompts` 增加 `extra_tasks` 参数,导入脚本传这三个 |

附带修复:
- **端点表幂等**:探针 bootstrap 不再每次新建旗舰端点(此前累积 383 行),改为复用 + 自愈;
- 导入脚本复用探针的环境准备(`_prepare_env`),否则 Qdrant/Redis/Neo4j 会指向默认端口(那是别的项目);
- 清理了 4 个 4096 维空集合与 383 行历史端点。

## 三、链路(可复跑)

```bash
# 1) 采样(可反复加书)
python -m scripts.ops.make_style_sample --out /root/ai-write/data/reference/_style_sample.txt \
  --chapters 25 --chars-per-chapter 1600 <每本书的 txt ...>

# 2) 导入 + 蒸馏(写 reference_books/text_chunks → 切片 → 风格卡/情节卡 → 建 StyleProfile)
QUALITY_EVAL_API_KEY=$KEY python -m scripts.ops.ingest_reference_books \
  --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \
  --file /root/ai-write/data/reference/_style_sample.txt \
  --title "龙族+天之炽 综合文笔样本" --author 江南

# 3) 用这张卡的文笔写(写作链经 settings.style_profile_id 自动注入)
python -m scripts.quality_eval.continuity_probe ... --style-profile <上一步输出的 style_profile_id>
```

`--style-profile` 接受 id 或名称;**给了却找不到会直接失败**,不会静默退化成模型默认风格 ——
这点很重要:此前所有跑批都是"没绑卡"的模型默认文笔,看起来却像"用了参考书"。

## 四、状态

<!--REF-STYLE-STATUS-->蒸馏进行中(48/208 片,0 失败)。

## 五、跑通之后

1. 用该卡跑 10 章,与"模型默认风格"**逐章并排**;
2. 两版一起加进 `_public/gallery.html`,按题材/版本/模型筛选里可对比;
3. 《天命之上》《赤心巡天》到货后同样采样追加(可合并进同一张卡,或单独出卡对比)。
