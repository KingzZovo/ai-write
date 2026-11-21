"""Application configuration loaded from environment variables and .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Central configuration for the AI Write backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Database ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/aiwrite"
    )

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333

    # --- Neo4j ---
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j"

    # --- LLM API keys (optional) ---
    ANTHROPIC_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    OPENAI_COMPATIBLE_BASE_URL: str | None = None
    OPENAI_COMPATIBLE_API_KEY: str | None = None

    # --- Security ---
    SECRET_KEY: str = "change-me-in-production"
    # 2026-11-17: SECRET_KEY 同时用于 JWT 签名与库内厂商 key 加密。默认值一旦带上生产
    # 环境,等于任何人都能自签 token、也能解出加密的 api_key。生产环境启动即失败。
    APP_ENV: str = "development"

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3100,http://localhost:8080"

    # --- Generation ---
    SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SYNC_SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS: int = 1
    SINGLE_SHOT_LLM_RETRY_ATTEMPTS: int = 1
    SINGLE_SHOT_LLM_STREAM: bool = False
    SINGLE_SHOT_LLM_BUDGET_ENDPOINTS: int = 2
    SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS: float = 420.0
    FORCE_DIRECT_CHAPTER: bool = False
    SCENE_MODE_TIMEOUT_HARD_CAP_SECONDS: float = 600.0
    CHAPTER_QUALITY_GATE_TIMEOUT_SECONDS: float = 420.0
    # One deadline includes Registry lookup, retries and tier fallback for all
    # chapter-evaluation callers. The request timeout never exceeds this total.
    EVALUATION_TOTAL_TIMEOUT_SECONDS: float = Field(default=180.0, gt=0, allow_inf_nan=False)
    EVALUATION_REQUEST_TIMEOUT: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    CHAPTER_PIPELINE_ENABLED: bool = True
    LOGIC_CRITIC_MAX_ROUNDS: int = 2
    CHAPTER_MAX_REWRITE_ROUNDS: int = 2
    QUALITY_GATE_PERSIST_ON_BLOCK: bool = True
    # ROLLBACK-POLICY (2026-07-27): the auto-revise rollback keeps the run's
    # best REAL-scored draft (persisted as status=draft) instead of restoring
    # the baseline when the baseline has no valid score for its current text
    # or scored lower -- but only if the draft scored at least this floor.
    ROLLBACK_KEEP_MIN_SCORE: float = 7.0
    # --- Defect governance (docs/DEFECT_GOVERNANCE.md) ---
    # Recurring-defect ledger: how many recent evaluated chapters feed the
    # generation-preflight 【本项目高发问题】 block, and in how many of those
    # chapters a violation tag must recur before it is surfaced.
    PREFLIGHT_RECURRING_DEFECT_CHAPTERS: int = 5
    PREFLIGHT_RECURRING_DEFECT_MIN_CHAPTERS: int = 2

    # --- LLM relay workarounds ---
    # Comma-separated model-name substrings whose relay channel drops the
    # system role (e.g. "claude-"). For matching models, system content is
    # folded into the first user message. Empty = disabled.
    LLM_MERGE_SYSTEM_INTO_USER_MODELS: str = ""
    # Comma-separated model-name substrings whose relay burns the whole output
    # budget on hidden thinking for NON-streaming completions, returning empty
    # text with output_tokens == max_tokens (observed 2026-07-26 on relay
    # claude-*; streaming the same request works). Matching models route
    # non-stream generate() through internal streaming and assemble the full
    # text before returning. Empty = disabled.
    LLM_FORCE_INTERNAL_STREAM_MODELS: str = "claude-"

    # 逗号分隔的任务类型(= LLMCallLog 的 ``task_type``,如 ``scene_planner``),
    # 命中的任务**一律走非流式**(一次性响应),``*`` = 全部任务。
    # 读取顺序:环境变量 ``LLM_NONSTREAM_TASKS`` > 本配置项;两者都空 = 关闭
    # (默认;行为与改动前逐字节一致)。见 ``model_router._should_force_nonstream``。
    # 为什么需要它(2026-11-17 实测):``deepseek-v4-flash`` 先思考后输出,思考阶段
    # 很长时本网关会提前关闭 SSE 流,管线收到 ``APIError: upstream stream closed
    # before any chunk carried finish_reason``(规划类步骤思考最重、最先中招);
    # 同一模型的非流式调用完全正常,所以给"思考最重的任务"开一条非流式通路。
    LLM_NONSTREAM_TASKS: str = ""

    # --- Scene Planner ---
    ALLOW_SCENE_PLANNER_FALLBACK: bool = False
    SCENE_PLANNER_TIMEOUT_SECONDS: float = 180.0
    SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS: float = 240.0
    # 单次生成的输出上限。默认 6000 = 维持现状(整章一场);把它调小,
    # 一章就会被拆成多场,每场重新注入完整上下文与"本章此前已写正文"。
    # 一章只写一场时,这套分段重注入机制完全用不上。
    SCENE_MAX_OUTPUT_WORDS: int = 6000
    # 每场篇幅兜底:写不足目标的一定比例时,补一次有界续写。
    # 依据(2026-11-15 实测):600 字目标只写 330–500;章级质量门在"篇幅不足"时
    # 直接 blocked 落 draft、rewrite_rounds=0,从不补写。提示里已经写了目标字数,
    # 所以这里要的是机械兜底,不是再加提示。
    SCENE_LENGTH_CONTINUATION_ENABLED: bool = True
    # 每场"写够了没有"的判定线。2026-11-17:0.85 → 0.95。
    # 依据:100 章长跑中位比只有 0.887,30/100 章跌破 85% —— 模型常年落在
    # 0.85–1.0 区间,0.85 这条线几乎不触发兜底,篇幅系统性偏低约 11% 就成了默认。
    # 抬到 0.95 后,差一点点的场也会补一次有界续写(代价:每场可能多一次生成)。
    SCENE_LENGTH_MIN_RATIO: float = 0.95
    SCENE_LENGTH_CONTINUATION_MAX: int = 1
    # 章级补足:所有场写完后,若整章仍短于验收线,用最后一场的收尾上下文
    # 再补 ≤N 次。0 = 关闭。实测(2026-11-18,gemini@2000 + 超分配 1.08)
    # 10 章里仍有 4 章 <2000(最短 1582),靠"抬高送模目标"到不了下限。
    CHAPTER_LENGTH_CONTINUATION_MAX: int = 1
    # 认知提取是**软**失败(模型没照抄正文依据)时,是否允许继续往下写。
    # 章级补足的**停止线**系数(乘真实章目标)。1.0 = 补到"够 2000 字"才停;
    # 0.95 = 与质量门验收线对齐(补到能过闸就停)。
    # 2026-11-18 实测:0.95 时 1900~2000 区间的章不再补(如 1969 字),用户要求的
    # "起码 2000"仍不满足,故默认取 1.0。
    CHAPTER_LENGTH_TOPUP_RATIO: float = 1.0
    # 默认 False = 维持现状:上一章未就绪就阻断本章。
    # 打开后:那一章按"无认知变化"计入(写手拿到的历史少一层),但整本书不会停;
    # 每次降级都 warning 记录,便于事后补跑提取。
    # 依据:实测 2026-11-14/16 三次,一次提取软失败就让之后每一章都被
    # character_cognition_not_ready 拦住,而下一章的上下文里本来就有上一章完整正文。
    ALLOW_COGNITION_DEGRADED_CONTINUATION: bool = False

    # pipeline 在推进下一章前,等该章认知提取就绪的上限秒数。
    # 依据(实测 2026-11-21):下一章的上下文要求它之前所有章的认知记录为
    # completed,而提取是章后**异步**派发的 —— 不等就会出现 ch1 成功、ch2 起
    # 99 章全部 character_cognition_not_ready。设 0 关闭等待(回到旧行为)。
    # 注意:等待发生在 project_worker_lock 作用域内,所以它同时是全书的
    # pause 响应上限;单章值不宜过大。
    PIPELINE_COGNITION_WAIT_SECONDS: int = 180
    # 等待轮询间隔(秒)。
    PIPELINE_COGNITION_POLL_SECONDS: float = 5.0

    # 人物认知历史块的字符预算(送模的"认知边界"一段)。
    # 2026-11-18:默认 1200 → 3000。为什么必须提高:该块要求"每个必选人物的
    # **最新一条记录(含依据原文)**都装下",装不下就显式失败(不许退化成旧记录,
    # 否则会写出"人物忽然知道了/忽然忘了"的矛盾情节)。而上游把全书人物卡名+别名
    # 都作为必选传进来,1200 字符到 36 章以后必然撑爆 —— 实测一次 100 章验证在
    # ch36-45 连续 10 章 RequiredContextUnavailable 后整批中断。
    # 3000 字符 ≈ 2000 token,相对 20000 token 的总上下文预算可以承受。
    COGNITION_HISTORY_MAX_CHARS: int = 3000

    # 当前 embedding 模型的向量维度。Qdrant 集合维度必须与它一致:写死 4096 的那批
    # 集合(plots/styles/style_profiles/beat_sheets/style_samples_redacted)在 2026-11-20
    # 换成 2048 维模型后写入全部 400。换模型时改这一处(或设环境变量 EMBEDDING_DIMENSION)。
    EMBEDDING_DIMENSION: int = 2048

    # --- 认知召回:词法混合重排(2026-11-17) ---
    # 0 = 关闭(默认):排序与纯向量逐字等价。>0 时最终分 =
    # (1-w)·minmax(余弦) + w·minmax(词法),只在已检索到的候选内重排,
    # 不会把向量没召回的条目拉进来。词法端不依赖 embedding,是第三方嵌入
    # 服务挂掉时的兜底(降级路径另由 ALLOW_COGNITION_DEGRADED_CONTINUATION 控制)。
    # 为什么默认 0:开它会改变既有召回排序,而"哪种更好"需要人工判读的
    # gold set 才能证明(本批未做),宁可让它可运维开启,不默认改变行为。
    COGNITION_LEXICAL_BLEND: float = 0.0

    # --- 每章用量护栏(2026-11-17) ---
    # 一章的累计 token(输入+输出,含检查/修文/资料整理的所有调用)达到该值时,
    # 该章的**重新生成**被拒,返回 SSE 事件 chapter_budget_exceeded(附已用/上限/
    # 按步骤明细),已完成正文不受影响。0 = 关闭(默认;不改变现有行为)。
    # 依据:同类产品(AI-Novel-Writing-Assistant)按章设 100k 上限,而本项目此前
    # 只有全局月度额度,单章反复重试可以无限烧钱且作者看不见。
    # 只拦"已存在的章",新章(无 chapter_id)不拦——它还没有任何用量。
    CHAPTER_TOKEN_BUDGET_DEFAULT: int = 0
    # 伏笔缺铺垫检查:章落库后核对"本章计划的伏笔是否在正文里留下锚点",
    # 有缺失就发一条 SSE 事件(不阻断、不写库、不改状态)。
    FORESHADOW_SETUP_CHECK_ENABLED: bool = True
    # 改稿增量同步时的"实体抽取探针"。抽取结果只进日志、不进图谱(entity
    # 投影走独立流水线),但每改一次稿都实打实付一次模型调用。默认关闭;
    # 需要诊断时用 env INCREMENTAL_SYNC_ENTITY_PROBE=1 打开。
    INCREMENTAL_SYNC_ENTITY_PROBE: bool = False
    # --- Context Pack ---
    # Total token budget for ContextPack.to_system_prompt (split 40/33/20/7
    # across L1/L2/L3/meta). Raise for bigger-context models.
    # 2026-11-16:9500 → 20000。实测真实正文约 1.5 汉字/token,旧值只允许
    # ~14k 字上下文,而 L1 只分到 40%,于是"全书大纲 / 后文章纲"这类大块在
    # 长书里会被整块丢弃(allocate_context 对超额度块直接 omitted,不做截断)。
    # 20000 token ≈ 30k 字,常见 128k 窗口模型余量充足。
    CONTEXT_PACK_TOKEN_BUDGET: int = 20000
    # Soft target for full character cards. All names mentioned in the current
    # chapter or supplied predecessor prose retain full state even beyond it;
    # historical frequency fills remaining slots. Total prompt budget still applies.
    CONTEXT_PACK_MAX_CHARACTER_CARDS: int = 12
    # --- Context Pack: 前后文窗口 ---
    # 2026-11-16:这些窗口原先是 context_pack.py 里的字面量(前章正文 3500、
    # 近 5 章摘要、后 10 章大纲、全书大纲头 1500 + 尾 500),在长书里先于预算
    # 生效,中段上下文进不了提示。改为可配置;总量仍受 CONTEXT_PACK_TOKEN_BUDGET
    # 与 allocate_context 的层权重约束。
    # 前章正文尾部保留字数。该块是 required,放大会让 ContextBudgetExceeded
    # 变成真实故障,故默认维持旧值 3500(单章目标本就约 3500 字)。
    CONTEXT_PREV_CHAPTER_TAIL_CHARS: int = 3500
    # 紧邻前文摘要条数 / 后文章纲条数(旧值 5 / 10;仅最新摘要为 required)。
    CONTEXT_RECENT_SUMMARY_CHAPTERS: int = 8
    CONTEXT_FUTURE_OUTLINE_CHAPTERS: int = 20
    # 全书大纲节选的头/尾字符数(超出则中间省略)。旧值 1500/500 对长篇几乎
    # 等于没给;该块 required=False,若仍超 L1 额度会被整块省略。
    CONTEXT_BOOK_OUTLINE_HEAD_CHARS: int = 4000
    CONTEXT_BOOK_OUTLINE_TAIL_CHARS: int = 2000
    # 后文章纲块的字符上限。实测:预算不足时该块会被**整块**省略(优先级 20 最低),
    # 所以给它自身加上限,宁可少给最远的几章也不要全丢。
    CONTEXT_FUTURE_OUTLINE_MAX_CHARS: int = 3000

    # --- 写后账目核对(金额) ---
    # 本章写出的总额与"计划已定的数"不一致时记一条提示(见 services/numeric_check.py)。
    # 只记账:不阻断、不改正文、不自动改写。仅当本章有金额钉子时才调用模型,否则零调用。
    NUMERIC_CHECK_ENABLED: bool = True
    # 实测推理模型在小 max_tokens 下会整段返回空正文,所以给足并留一次重试;
    # 空正文一律当作"抽取失败",不当作"没有金额"。
    NUMERIC_CHECK_MAX_TOKENS: int = 3000
    NUMERIC_CHECK_RETRIES: int = 1

    # --- Memory compaction (500万字 scaling) ---
    # Auto-compaction fires (fire-and-forget, never blocks the chapter save)
    # after a chapter-summary upsert when the project's live (non-compacted)
    # chapter-summary point count exceeds this threshold.
    MEMORY_COMPACT_THRESHOLD_POINTS: int = 200

    # --- Memory pyramid (coarse-to-fine tiers) ---
    # Tier 2: max memory cards kept per (project, character). first_appearance
    # cards are always kept; oldest key_moment cards are evicted beyond cap.
    MEMORY_CARDS_PER_CHARACTER: int = 10
    # Tier 3: a chapter-relevant character absent for MORE than this many
    # chapters (gap = current global_idx - last_seen > GAP) triggers the
    # 旧人重现 drill-down block in ContextPack L3.
    MEMORY_DRILLDOWN_GAP_CHAPTERS: int = 20
    # Tier 4: chunk chapter full text into the per-project chapter_chunks
    # shard on persist (write side) / recall chunks into ContextPack L3
    # (read side).
    CHAPTER_CHUNKING_ENABLED: bool = True
    CHAPTER_CHUNK_RECALL_ENABLED: bool = True

    # --- DB scaling / retention (v1.11 partitioning groundwork) ---
    # Drop llm_call_logs monthly partitions older than this many months.
    # <= 0 disables dropping (keep forever).
    LLM_LOG_RETENTION_MONTHS: int = 6
    # How many future monthly partitions tasks.maintain_llm_log_partitions
    # keeps pre-created ahead of the current month.
    LLM_LOG_PARTITION_PRECREATE_MONTHS: int = 3
    # Keep only the newest K chapter_versions rows per chapter.
    # 0 (default) = keep all history; the active version is never deleted.
    CHAPTER_VERSION_KEEP_LAST: int = 0

    # --- 篇幅口径(2026-11-17 统一,2026-11-18 调整) ---
    # 三个旋钮管三件事,别再各写死一份:
    #   SCENE_TARGET_OVERALLOC(1.08)     = 送给规划/写手的"章目标"放大系数(见下);
    #   SCENE_LENGTH_MIN_RATIO(0.95)     = 单场写不够就补一次续写(生成侧);
    #   CHAPTER_MIN_TARGET_WORD_RATIO    = 整章交付时的验收下限(质量门)。
    #
    # SCENE_TARGET_OVERALLOC 的依据(实测,不是拍脑袋):
    #   gemini-3.1-pro @ 目标 2000 字跑满 100 章,`continuity.json` 的 word_counts:
    #   中位 **1952 字(比 0.976)**、均值 1944;92 章里 55 章 <2000、41 章 <1900。
    #   (另一口径:去掉空白字符后中位 1854 —— 中文"字数"习惯不含空白,两个都记在这。)
    #   即"每场系统性欠 2~3%",不是偶发;场级续写阈值(0.95)只对明显不够的场触发,
    #   补不回这点零头。所以在**送模的目标**上放大 8%:模型按 2160 写、欠 2.5%
    #   → 实际 ≈2106,大多数章落在 2000 以上;章节记录里的真实目标仍是 2000,
    #   验收/统计口径不变。过冲由 SCENE_MAX_OUTPUT_WORDS 与规划器的 85–115%
    #   预算窗兜住,且"长一点"本身不是缺陷。设 1.0 即完全关闭。
    SCENE_TARGET_OVERALLOC: float = 1.08
    # 整章验收下限。2026-11-17 先从写死的 0.80 收敛到配置(值不变);
    # 2026-11-18 提到 0.95 —— 用户要求"单章起码 2000 字",而 0.80(=1600)
    # 会把 1952 字这种接近目标但不足的章直接放行。0.95 允许 5% 浮动(=1900);
    # 配合上面的超分配,预计只拦下真正偏短的章。
    CHAPTER_MIN_TARGET_WORD_RATIO: float = 0.95

    # --- Reference Ingestor ---
    STYLE_REDACTION_ENABLED: bool = True
    REFERENCE_INGEST_CONCURRENCY: int = 3
    DECOMPILE_MAX_AUTO_RETRIES: int = 5
    DECOMPILE_RETRY_INITIAL_DELAY: int = 300
    DECOMPILE_RETRY_BACKOFF_FACTOR: float = 2.0
    DECOMPILE_RETRY_WAVE_BATCH: int = 50


class _SettingsGuard(Settings):
    """生产环境不允许使用默认 SECRET_KEY。"""

    def model_post_init(self, __context) -> None:  # noqa: D401 - pydantic hook
        env = (self.APP_ENV or "").strip().lower()
        # prod* / staging 都算生产口径(拼写变体不再绕过守卫)
        production_like = env.startswith("prod") or env in {"staging", "stage"}
        if production_like and self.SECRET_KEY.strip() in {
            "", "change-me-in-production", "changeme",
        }:
            raise RuntimeError(
                "SECRET_KEY 仍是默认值:生产环境必须显式设置(它用于 JWT 签名与 api_key 加密)"
            )


settings = _SettingsGuard()
