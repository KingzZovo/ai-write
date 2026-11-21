# `stamp` 工具化 + 两个外部仓库的价值判断(2026-11-17)

## 一、`stamp` 到底是什么,以及为什么我没法替你"直接处理"

### 1.1 两条命令的区别

| 命令 | 做什么 |
|---|---|
| `alembic upgrade <rev>` | **执行 DDL**,并把"记账"推进到 `<rev>` |
| `alembic stamp <rev>` | **只写记账**(`alembic_version` 表里一行版本号),一条 SQL 都不执行 |

本仓库的迁移链**不自带建表**(假定 schema 已由 `Base.metadata.create_all` 建好),
所以在"表已建好、alembic 从未记账"的库上,`upgrade` 会 `DuplicateTable`/`DuplicateColumnError`,
正确动作是 `stamp`。但 `stamp` 用错方向很危险(把没应用的版本标成已应用 → 以后 `upgrade` 跳过那段 DDL)。

### 1.2 我直接做掉的

| 库 | 之前 | 现在 |
|---|---|---|
| 隔离评估库 `aiwrite_eval` | **连 `alembic_version` 表都没有**(从未用过 alembic),但 schema 已是最新形态 | `a1001959 (head)` |

核对方式是先查两条痕迹(`status_digest` 列、`uq_character_states_source` 唯一键),再 `stamp`。

### 1.3 生产库为什么没动

- 应用配置里的 `DATABASE_URL` 指向 `localhost:5432/aiwrite`;
- 本机 5432 是一条 **SSH 隧道**,通到 `root@82.158.226.97` —— 那台机器上是 **quant 项目**的库
  (`/root/quant/*` 下有对应的同步脚本),不是 ai-write;用默认口令连它会 `password authentication failed`;
- 本机没有 ai-write 的部署进程/容器/compose(8000 端口那个 uvicorn 是 python3.10 的别的应用),
  也没有它的 `.env`。

**结论:本机没有可达的 ai-write 生产库**,所以"直接处理"没有对象。我不去猜口令、也不去改别人的库。

### 1.4 所以做成一条命令(已验证)

新增 `scripts/ops/alembic_sync.py`。它**先核对 head 迁移的痕迹,再决定**,且**永不执行 DDL**:

| 判定 | 含义 | 动作 |
|---|---|---|
| `noop` | 记账已是 head 且痕迹齐全 | 什么都不做 |
| `stamp` | 痕迹齐全但记账落后/缺失 | `--apply` 时执行 `alembic stamp head` |
| `upgrade` | 痕迹缺失(真的没应用) | 打印 `upgrade` 命令,**不代替你跑 DDL** |
| `fix_heads` | 迁移图有多个 head | 拒绝,提示先修分支 |
| `manual` | head 没登记核对痕迹 | **拒绝自动判断**,给人工步骤 |

实跑验证(临时库,用完即删):

```
新库(create_all 后,无 alembic_version)
  → 判定: stamp —— schema 已是 a1001959 的形态,但记账是 (无) → 只补记账,不跑 DDL
  → --apply: 已执行 alembic stamp a1001959 / 复核 current=a1001959
  → 再体检: noop
评估库        → noop
```

用法:

```bash
cd /root/ai-write/backend
DATABASE_URL="postgresql+asyncpg://user:pass@host:port/db" python -m scripts.ops.alembic_sync          # 只体检
DATABASE_URL="..."                                         python -m scripts.ops.alembic_sync --apply  # 判定为 stamp 才执行
```

**加新迁移时必须往 `SCHEMA_PROBES` 补一行**(否则新 head 会落到 `manual`,这是有意的安全默认;
有一条测试专门钉住这点)。

## 二、两个仓库:一个高价值,一个中等价值

### 2.1 `Zleap-AI/SAG-Wiki` —— **高价值,但只在一个点上**

它是一套 Rust/Tauri 的本地知识库,核心是自研的 **SAG2 检索**:

- **索引与检索都不调 LLM、不做嵌入**;自称 100 万字符索引约 **200ms**;
- 机制是"Jev 证据打分 + 查询时扩展",在 MuSiQue / 2WikiMultiHopQA / HotpotQA 上
  自称 Recall@10 优于 BM25、GraphRAG 等(有论文,arXiv 2610.22588);MIT 许可。

**对我们的价值恰好在一个痛点上**:我们**唯一硬依赖第三方服务的环节就是语义召回**——
它挂过两次(上游 500,整章写不出来),换模型后还会**静默变空**(集合名带 space 指纹)。
我们已经做了"不排序继续"的兜底,但那是**不检索**;SAG2 这类**无嵌入检索**能兜到"照样检索,只是换成词法+证据打分"。

**不能直接用**:它是文档 KB(带 PDF/OCR/桌面 UI),不是叙事引擎;要用是**移植算法**,不是接产品。

### 2.2 `skygazer42/MimirQ` —— **中等价值,当工程参考**

中文优先的企业 RAG 平台:30 个解析后端、86 种切块策略、13 类重排器、混合检索、引用与 trace、
Golden 题集回归、Dify 接入(FastAPI + Next.js + Milvus + PG + Redis)。

三条可直接借:

1. **BM25 + 向量 + 重排**:我们只有向量召回,人名/物件名的精确匹配是弱项;
2. **按结构切块**(标题/章节/业务记录/父子),而不是我们的固定长度切块 —— 直接影响长程召回;
3. **"更换 Embedding 模型后必须重建索引"** —— 他们已经写进文档,正是我这轮修的洞(有 backfill 入口)。

**不该整仓上**:Milvus、16GB 起步、30 个解析器,我们不是文档问答。

## 三、按性价比排序(如果继续借)

| 优先级 | 借什么 | 为什么 | 代价 |
|---|---|---|---|
| **1** | SAG2 式**无嵌入检索**作为召回兜底 | 去掉唯一的硬依赖;现在兜底等于"不检索" | 需要读论文/实现,纯 Python 原型可先跑 |
| **2** | **BM25 + 向量混合 + 重排** | 人名/物件名精确匹配是我们测出来的弱项 | 中:要加索引与重排调用(可用本地 BM25,不必接外部) |
| **3** | **按章节/父子结构切块** | 现有切块是定长,长程召回吃亏 | 小:改 `chapter_chunker` |
| 4 | MimirQ 的 trace/Golden 回归框架 | 我们已有自己的检测器+评审 | 低价值,我们这套更贴叙事 |

## 四、验证

- 新增 7 项离线回归(`tests/test_alembic_sync.py`),含"新 head 必须登记痕迹"的守卫;
- 全量离线回归 **4872 passed / 762 skipped / 0 failed**;
- 临时库 `aiwrite_sync_demo` 用完即删;未动任何用户库;未猜任何口令;
- 100 章长跑仍在进行:**73/100**。