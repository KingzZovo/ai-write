"""真实写作链连续章探针:隔离项目 -> 逐章走场景编排 -> 跨章确定性检查。

与 ``quality_eval`` 的单场对照不同,这里跑的是生产链本身
(``ContextPackBuilder`` + ``SceneOrchestrator`` + ``prompt_registry`` + 真实模型),
用来回答两个问题:

1. 连续多章能不能一直产出可读正文,而不是第 3 章开始崩;
2. 跨章的事实(数字、道具、时间、称呼)会不会自己漂移。

用法(需要先起本地 PG,详见 docs/evaluation/quality-eval-20261114-abi/REVIEW.md):

    python -m scripts.quality_eval.continuity_probe \
        --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \
        --out ../docs/evaluation/continuity-YYYYMMDD \
        --chapters 10 --model gemini-3.1-pro

产物:每章 ``ch01.txt`` …、``chapters.json``(元信息与用量)、``continuity.json``(检查结果)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_EMBED_ENDPOINT = "https://integrate.api.nvidia.com/v1"
# 2026-11-16:原默认 nvidia/llama-nemotron-embed-vl-1b-v2 已被上游下线(HTTP 410 Gone),
# 而语义召回是硬依赖,模型一挂整条链就在第 4 章起停摆。改成仍在服务、同为 2048 维的模型,
# 并允许用 QUALITY_EVAL_EMBED_MODEL 覆盖(换服务商时不必改代码)。
DEFAULT_EMBED_MODEL = os.environ.get("QUALITY_EVAL_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")

# 复用单场检测器里的人物名识别(姓氏表、称号后缀、通用名词黑名单),
# 免得两处维护同一套"什么才算一个人名"。
from scripts.quality_eval.detectors import (  # noqa: E402
    _GENERIC_TITLE_NOUNS, _NAME_PREFIXES, _NEW_TITLE_RE, _SURNAMES,
)

SUMMARY_SYSTEM = (
    "你是中文小说编辑。用 150–250 字概括这一章已经实际发生的事:"
    "人物做了什么、结果是什么、位置与时间推进到哪、物件与掌握的信息有何变化。"
    "只写正文里已经写成的,不要把计划或打算写成已经发生。只输出摘要正文。"
)

# 书级既定事实:这些是作者设定,不是模型可以自行调整的。
# 实测(2026-11-14)正文把"欠三千/还两千/缺一千"改成了"两千+八百=两千八/缺两百",
# 而且三章之间自洽地沿用,跨章"同名目冲突"检查抓不到。所以这里显式列出
# 必须出现的数字、不得出现的改写说法、必须贯穿的道具。
BOOK_FACTS = {
    "numbers": ("三千", "两千", "一千"),
    "conflicts": ("两千八", "两千八百", "缺两百", "差两百", "还差两百", "欠两百"),
    "props": ("信封", "顶针", "铁盒"),
}

# 跨章钉死的数额(名目所在章, 名目关键字, 允许的写法)。
# 长篇最容易出的错不是"算错",是"忘了自己前面写过多少"——第 7 章写工钱 880、
# 第 8 章写 820 就是实例。这里在第 11/15/16 章各钉一个数,检查后面的章节
# 只要提到同名目,就不能出现别的金额。
PINS = (
    ("ch11.txt", "复查", ("一千二", "1200")),
    ("ch15.txt", "补偿", ("八百", "800")),
    ("ch16.txt", "铺租", ("九百", "900")),
)

BLUEPRINT = {
    "title": "渡口旧账",
    "genre": "现实生活",
    "premise": (
        "周芸在县城做缝纫,弟弟周航在镇上修电器。母亲住院时周航垫了三千元,"
        "周芸答应年底还清却只凑到两千。周航的店面租期到期,房东催租。"
        "两人都不愿欠人情,钱与面子的账要在渡口两岸一笔笔算清。"
    ),
    "volume": {
        "title": "第一卷 两张船票",
        "summary": "从渡口面摊的一次交钱起,姐弟俩在县城与镇上各自想办法补上缺口。",
    },
}

# 大纲里点名的人物。正文出现别的具名人物时只提示、不判错:
# 长篇里临时补一个街坊是合理的,但需要一个能看见的计数。
ROSTER = ("周芸", "周航", "老周", "老陈", "老刘", "母亲")
# 对已有人的当面称呼:"周姐""老周婶"不是新人物。
_ADDRESS_SUFFIXES = frozenset("姐哥妹弟嫂婶叔姨伯娘妈婆爷孙")

# 单场检测器只认亲属称谓("李大姐"),长篇里更常见的是"李师傅""王老板"。
# 这里补一份探针自己的职务称谓表,不去改那个已被单场 ABI 引用的检测器。
_ROLE_TITLES = ("师傅", "老板", "老师", "医生", "大夫", "司机", "会计",
                "先生", "女士", "掌柜", "木匠", "裁缝", "屠户")
_ROLE_NAME_RE = re.compile(
    r"(?:^|(?<=[\s\n。!?、,,“”「」‘’::])|(?<=的)|(?<=个)|(?<=见))"
    r"([\u4e00-\u9fa5]{1,2}(?:" + "|".join(_ROLE_TITLES) + r"))")
# 称呼已有人的尾缀:亲属称谓、职务,以及"丫头/姑娘/小子"这类当面叫法。
_ADDRESS_OR_ROLE_TAILS = _ADDRESS_SUFFIXES | frozenset(_ROLE_TITLES) | frozenset(
    ("丫头", "姑娘", "小子", "娃子", "妹子", "伙计"))

# 每章只给必须完成的事和已经成立的事实;具体写法交给生产链。
CHAPTERS: list[dict[str, Any]] = [
    {"title": "第一章 碗底的信封", "time": "当日傍晚", "place": "县城渡口面摊",
     "time_keys": ("傍晚", "天色将晚", "天黑", "暮色", "夕阳"),
     "place_keys": ("渡口", "面摊", "码头"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸把装着两千元的牛皮纸信封压在面碗下。周航坐末班渡船来取钱,"
              "他明天要交店面租金,但没说。本场需完成:周航发现钱不够并说出明天交租;"
              "两人谈成一个只到月末的后续安排;钱仍缺一千;弟弟拿钱上船,姐姐留在岸上。"},
    {"title": "第二章 顶针与八百块", "time": "次日清晨", "place": "周芸的缝纫铺",
     "time_keys": ("清晨", "早上", "一早", "天亮", "次日", "第二天"),
     "place_keys": ("缝纫铺", "铺子", "铺面", "裁缝"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸的铺面房东来催下半年的铺租八百元。她手上有的是周航留下的地址与"
              "自己的铜顶针。本场需完成:房东说清八百元的期限;周芸决定不向弟弟开口;"
              "她把自己的顶针收进铁盒当备用抵押。结尾交代她明天要接的活。"},
    {"title": "第三章 修不好的电饭锅", "time": "同日午后", "place": "镇上周航的电器铺",
     "time_keys": ("午后", "下午", "中午", "晌午"),
     "place_keys": ("电器铺", "修理", "铺子", "卷闸"),
     "pov": "周航", "words": 2000,
     "brief": "周航交完租金,铺子里只剩不到两百元。供货的老陈要结上月的货款七百。"
              "本场需完成:老陈说清七百的货款期限;周航没有说出姐姐只给了两千;"
              "他决定接一单旧电饭锅的活换现金。结尾交代他手上的钱数。"},
    {"title": "第四章 一碗没有加蛋的面", "time": "次日傍晚", "place": "县城渡口面摊",
     "time_keys": ("傍晚", "天黑", "暮色", "夕阳"),
     "place_keys": ("渡口", "面摊", "码头"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸为了省下铺租,开始接下夜活。她在面摊遇见渡船的老周,老周提起"
              "周航昨天是站着回来的。本场需完成:老周说出周航当时的状态;"
              "周芸没有说出自己也不宽裕;她决定周末去镇上。结尾交代她给弟弟带的袋子。"},
    {"title": "第五章 袋子里的东西", "time": "周末上午", "place": "镇上周航的电器铺",
     "time_keys": ("上午", "早上", "周末", "一早"),
     "place_keys": ("电器铺", "铺子", "修理"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸把缝纫剩下的布头和三百元零钱装进袋子带到镇上。周航不肯收,"
              "说自己已经向老陈说明。本场需完成:周航说清货款的新期限;"
              "周芸把三百元留下但拿回布头;两人确认欠款的余数仍是一千。结尾交代袋子里剩下什么。"},
    {"title": "第六章 老陈的账本", "time": "当日午后", "place": "镇上的杂货铺",
     "time_keys": ("午后", "下午", "中午"),
     "place_keys": ("杂货铺", "老陈", "账本"),
     "pov": "周航", "words": 2000,
     "brief": "周航去老陈的杂货铺说货款。老陈翻出账本,记着周航上月欠七百、"
              "上上月的三百。本场需完成:老陈说清两笔账合计一千;周航只先还三百;"
              "双方约定剩下的七百在下月中秋前还。结尾交代老陈收钱后写上的日期。"},
    {"title": "第七章 铁盒里的顶针", "time": "三天后", "place": "周芸的缝纫铺",
     "time_keys": ("三天", "三日后", "过了三天", "第三天"),
     "place_keys": ("缝纫铺", "铺子", "顶针", "铁盒"),
     "pov": "周芸", "words": 2000,
     "brief": "铺租八百的期限到了。周芸把顶针从铁盒里拿出来,最后没有当出去,"
              "而是接了邻居的一批校服。本场需完成:周芸说明八百的期限当天到;"
              "她选择不当顶针并说出理由;她算出这批活能挣多少。结尾交代她当晚做到几点。"},
    {"title": "第八章 校服的钱", "time": "一周后", "place": "周芸的缝纫铺",
     "time_keys": ("一周", "七天", "一个星期", "昨天"),
     "place_keys": ("缝纫铺", "铺子", "校服"),
     "pov": "周芸", "words": 2000,
     "brief": "校服交了,周芸收下八百二。铺租八百付掉后只剩二十。她写给弟弟的信没有寄出。"
              "本场需完成:周芸算清收八百二、付八百、余二十;她写下信但决定不寄;"
              "她决定下月把一千元还给弟弟。结尾交代信放进哪里。"},
    {"title": "第九章 中秋前的七百", "time": "中秋前十天", "place": "镇上的电器铺",
     "time_keys": ("中秋", "十天"),
     "place_keys": ("电器铺", "铺子", "老陈"),
     "pov": "周航", "words": 2000,
     "brief": "周航接了三台旧电视的活,凑够七百还给老陈。老陈把账本上的两笔都划掉。"
              "本场需完成:周航交清七百;老陈当面划账;周航说出姐姐还欠他一千这件事。"
              "结尾交代他给姐姐捎的东西。"},
    {"title": "第十章 最后一班渡船", "time": "中秋前三天的傍晚", "place": "县城渡口",
     "time_keys": ("中秋", "傍晚", "天黑"),
     "place_keys": ("渡口", "船", "码头"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸把一千元装进信封,坐渡船去镇上。周航不肯收,说等姐姐铺子稳了再说。"
              "本场需完成:周芸交出一千元;周航拒绝并说明理由;两人谈成一个"
              "双方都不欠人情的安排;结尾交代信封最后在谁手里。"},
    # ---- 第二幕(第 11–24 章):母亲、渡船与铺子。
    # 前 10 章留下的一千差额、八百铺租、顶针与铁盒在这里继续滚动,再加三条钉死的数额,
    # 用来测"长篇能不能记住自己前面写下的钱"。
    {"title": "第十一章 复查单", "time": "中秋后第二天上午", "place": "县医院住院部",
     "time_keys": ("上午", "中秋后", "第二天", "复查"), "place_keys": ("医院", "住院部", "病房"),
     "pov": "周芸", "words": 2000,
     "brief": "医生把复查单交给周芸:再交一千二才能排上手术,她手头只剩上次剩的那二十元。"
              "本场需完成:医生说清一千二的期限;周芸没有告诉周航;她决定把母亲先接回"
              "自己铺子的里屋住。结尾交代复查单放进哪里。"},
    {"title": "第十二章 老周的收音机", "time": "同日午后", "place": "镇上周航的电器铺",
     "time_keys": ("午后", "下午", "中午"), "place_keys": ("电器铺", "铺子", "修理"),
     "pov": "周航", "words": 2000,
     "brief": "老周拿一台旧收音机来修,随口说起在医院看见周芸捏着复查单。周航这才知道"
              "姐姐又瞒了他。本场需完成:老周说出医院看见的那一幕;周航问清一千二的数目;"
              "周航决定先去交钱。结尾交代他从哪里取钱。"},
    {"title": "第十三章 两岸各说一半", "time": "次日傍晚", "place": "县城渡口",
     "time_keys": ("傍晚", "天黑", "暮色"), "place_keys": ("渡口", "码头", "船"),
     "pov": "周芸", "words": 2000,
     "brief": "姐弟在渡口碰上,各自只说了一半。周芸坚持复查费自己出,周航说他已经交过了"
              "(其实是撑面子的话)。本场需完成:两人都承认瞒了对方;周芸坚持要还这一千二;"
              "两人不欢而散。结尾交代谁先上船。"},
    {"title": "第十四章 收费窗口", "time": "三天后上午", "place": "县医院住院部",
     "time_keys": ("上午", "三天", "窗口"), "place_keys": ("医院", "住院部", "收费"),
     "pov": "周航", "words": 2000,
     "brief": "周航去收费窗口交一千二,收费员说昨天已经有人交过,交钱的是周芸。"
              "本场需完成:收费员说出交钱的人与时间;周航那句撑面子的话当场破了;"
              "他没有退钱,决定把这一千二记在姐姐名下。结尾交代他把收据交给谁。"},
    {"title": "第十五章 停航通知", "time": "两天后午后", "place": "县城渡口",
     "time_keys": ("午后", "下午", "两天"), "place_keys": ("渡口", "码头", "船"),
     "pov": "周芸", "words": 2000,
     "brief": "渡口贴出停航通知:下月底新桥通车。老周有八百元补偿,他不要,只想把船留下。"
              "本场需完成:老周说清八百元和停航的日子;周芸劝他收下;老周说他缺的不是钱。"
              "结尾交代周芸替老周想了什么办法。"},
    {"title": "第十六章 九百块的铺子", "time": "一周后上午", "place": "周芸的缝纫铺",
     "time_keys": ("一周", "上午", "一早"), "place_keys": ("缝纫铺", "铺子", "裁缝"),
     "pov": "周芸", "words": 2000,
     "brief": "房东老刘说下半年铺租涨到九百。周芸算了三遍,发现接童装才能撑住。"
              "本场需完成:老刘说清九百的期限;周芸算出接了童装能余多少;她决定租下隔壁半间。"
              "结尾交代她去找谁谈。"},
    {"title": "第十七章 学徒", "time": "次日午后", "place": "镇上周航的电器铺",
     "time_keys": ("次日", "午后", "下午"), "place_keys": ("电器铺", "铺子", "修理"),
     "pov": "周航", "words": 2000,
     "brief": "周航想搬去县城,老陈提醒他账还没清。他改主意:把铺子一半让给学徒,自己往县城跑。"
              "本场需完成:学徒交的押金是几成;老陈同意宽限到年底;周航写下新的时间表。"
              "结尾交代他把什么交给姐姐保管。"},
    {"title": "第十八章 摊开的账", "time": "两天后傍晚", "place": "县城渡口",
     "time_keys": ("两天", "傍晚", "天黑"), "place_keys": ("渡口", "码头", "船"),
     "pov": "周芸", "words": 2000,
     "brief": "姐弟在渡口把瞒下的事全摊开:垫过的复查费、拒收的一千、母亲住院时垫的三千。"
              "本场需完成:两人各自说出瞒了什么;算清周芸还欠周航多少;约定年底结清。"
              "结尾交代这笔账写在什么上。"},
    {"title": "第十九章 半间铺面", "time": "一周后上午", "place": "周芸的缝纫铺",
     "time_keys": ("一周", "上午", "一早"), "place_keys": ("缝纫铺", "铺子", "裁缝"),
     "pov": "周芸", "words": 2000,
     "brief": "周芸租下隔壁半间做童装,第一次雇人。她把铜顶针从铁盒里拿出来给新来的姑娘看。"
              "本场需完成:租金与工钱的数;周芸教人做第一件;她说清顶针不出这个铺子。"
              "结尾交代第一批货什么时候交。"},
    {"title": "第二十章 一半的铺子", "time": "三天后午后", "place": "镇上周航的电器铺",
     "time_keys": ("三天", "午后", "下午"), "place_keys": ("电器铺", "铺子", "修理"),
     "pov": "周航", "words": 2000,
     "brief": "学徒搬进来,周航把货架分了一半出去。老陈来划账,问他年底怎么凑。"
              "本场需完成:老陈划掉哪一笔;周航说出年底的计划;学徒第一次独立修好一台。"
              "结尾交代周航把工具箱放在哪里。"},
    {"title": "第二十一章 最后一班", "time": "十天后傍晚", "place": "县城渡口",
     "time_keys": ("十天", "傍晚", "天黑"), "place_keys": ("渡口", "码头", "船"),
     "pov": "周芸", "words": 2000,
     "brief": "停航前最后一班渡船,老周把姐弟一起送过江,给两人各一张旧船票。"
              "本场需完成:老周说清停航的日子;他把船票分给姐弟;周芸说出铺子的新打算。"
              "结尾交代船靠岸时谁先下。"},
    {"title": "第二十二章 出院", "time": "次日早上", "place": "县医院住院部",
     "time_keys": ("次日", "早上", "一早"), "place_keys": ("医院", "住院部", "病房"),
     "pov": "周芸", "words": 2000,
     "brief": "母亲出院,医药费结清。母亲问那一千二是谁交的,姐弟都不说。"
              "本场需完成:结清单据上的数;母亲问出钱的来路;姐弟各自担了一半。"
              "结尾交代母亲住进哪里。"},
    {"title": "第二十三章 第一批货", "time": "一周后上午", "place": "周芸的缝纫铺",
     "time_keys": ("一周", "上午", "一早"), "place_keys": ("缝纫铺", "铺子", "童装"),
     "pov": "周航", "words": 2000,
     "brief": "童装第一批出货,周航来帮忙装灯,第一次看见姐姐铺子里雇的人。"
              "本场需完成:这批货卖出的数;周航说出自己铺子的进项;姐弟当场结掉一部分账。"
              "结尾交代还差多少。"},
    {"title": "第二十四章 结账", "time": "除夕前三天傍晚", "place": "县城渡口",
     "time_keys": ("除夕", "傍晚", "天黑"), "place_keys": ("渡口", "码头", "桥"),
     "pov": "周芸", "words": 2000,
     "brief": "新桥通车,渡口封了。姐弟在旧码头把最后一笔账结清,信封交到母亲手里。"
              "本场需完成:两人结清剩下的钱;母亲把信封收下;说清开年各做什么。"
              "结尾交代顶针和船票放在哪。"},
]


# 探针需要的任务类型:写作两步、摘要一步,以及连写必需的实体/认知提取两步。
_PROMPT_TASKS = ("scene_planner", "scene_writer", "summary",
                 "chapter_entities", "chapter_cognition", "relationship_scope")


async def _seed_prompts(db: Any, *, endpoint_id: Any, model: str,
                  max_tokens: int = 4096,
                  embed_key: str | None = None,
                  embed_endpoint: str = DEFAULT_EMBED_ENDPOINT,
                  embed_model: str = DEFAULT_EMBED_MODEL,
                  extra_tasks: tuple[str, ...] = ()) -> None:
    """把探针需要的 prompt 资产一次性种入并绑定到同一端点。

    ``chapter_entities`` / ``chapter_cognition`` 的系统提示与输出 schema 直接取
    ``prompt_registry.BUILTIN_PROMPTS``,不另写一份,避免和线上漂移。

    ``embed_key`` 给出时还会种一个 NVIDIA embedding 端点:第 4 章起的认知语义召回
    需要它(model_router 是从 task_type='embedding' 的 prompt 资产反查端点的)。
    """
    from app.models.prompt import PromptAsset
    from app.models.project import LLMEndpoint
    from app.services.prompt_registry import BUILTIN_PROMPTS

    builtin = {item.get("task_type"): item for item in BUILTIN_PROMPTS}
    # 幂等(2026-11-20):此前每跑一次 bootstrap 就新插一整套(7 个任务 × N 次),
    # 实测库里 prompt_assets 涨到 1290 行、每个 task_type 约 200 条重复;更糟的是
    # embedding 路由的 180 条大多指向**已删除的端点**,模型路由反查到死端点后报
    # no_embedding_provider,蒸馏/写作一起挂。这里改成"有就更新、没有才插入"。
    from sqlalchemy import select as _select
    existing_rows = (await db.execute(
        _select(PromptAsset).where(PromptAsset.is_active == 1)
    )).scalars().all()
    existing_by_task: dict[str, Any] = {}
    for _row in existing_rows:
        existing_by_task.setdefault(_row.task_type, _row)
    # extra_tasks:长跑本身用不到、但别的链路要的提示词(例如参考书蒸馏需要的
    # style_abstraction / beat_extraction / redaction)。不种它们,蒸馏会报
    # "No active prompt registered for task 'xxx'"。
    for task_type in tuple(_PROMPT_TASKS) + tuple(extra_tasks):
        if task_type == "summary":
            mode, system, schema = "text", SUMMARY_SYSTEM, None
        else:
            item = builtin.get(task_type)
            if not item:
                raise RuntimeError(f"builtin prompt missing: {task_type}")
            mode = item.get("mode", "text")
            system = item["system_prompt"]
            schema = item.get("output_schema")
        _prompt_max = min(1200, max_tokens) if task_type == "summary" else max_tokens
        row = existing_by_task.get(task_type)
        if row is not None:
            row.name = f"probe-{task_type}"
            row.mode = mode
            row.system_prompt = system
            row.output_schema = schema
            row.endpoint_id = endpoint_id
            row.model_name = model
            row.model_tier = "flagship"
            row.max_tokens = _prompt_max
            row.is_active = 1
            continue
        db.add(PromptAsset(
            task_type=task_type, name=f"probe-{task_type}", mode=mode,
            system_prompt=system, output_schema=schema,
            endpoint_id=endpoint_id, model_name=model, model_tier="flagship",
            temperature=0.2 if task_type in ("summary", "chapter_entities", "chapter_cognition") else 0.3,
            max_tokens=_prompt_max,
            is_active=1))
    if embed_key:
        from app.models.project import LLMEndpoint
        from app.utils.crypto import encrypt_api_key
        # tier 必须与聊天任务错开:否则聊天任务的 tier 回退会挑到这个端点,
        # 拿 embedding 的 key 去打 /chat/completions,直接 401。
        # 同样幂等:已有可用 embedding 端点就复用它,不再每次新插一条。
        embed_ep = (await db.execute(_select(LLMEndpoint).where(
            LLMEndpoint.tier == "embedding", LLMEndpoint.enabled == 1))).scalars().first()
        if embed_ep is None:
            embed_ep = LLMEndpoint(name="probe-embedding", provider_type="nvidia",
                                   base_url=embed_endpoint, api_key=encrypt_api_key(embed_key),
                                   default_model=embed_model, tier="embedding", enabled=1)
            db.add(embed_ep)
            await db.flush()
        route = existing_by_task.get("embedding")
        if route is not None:
            route.endpoint_id = embed_ep.id
            route.model_name = embed_model
            route.model_tier = "embedding"
            route.is_active = 1
        else:
            db.add(PromptAsset(
                task_type="embedding", name="probe-embedding", mode="text",
                system_prompt="(向量化任务不使用系统提示)", endpoint_id=embed_ep.id,
                model_name=embed_model, model_tier="embedding", temperature=0.0,
                max_tokens=1, is_active=1))


def _detect_embedding_key() -> str | None:
    """找本机的 NVIDIA embedding key(显式参数/环境变量优先,其次读运行中的容器配置)。

    只返回 key,不写进任何产物;容器名可通过 ``QUALITY_EVAL_EMBED_CONTAINER`` 覆盖。
    """
    import json
    import re
    import subprocess

    container = os.environ.get("QUALITY_EVAL_EMBED_CONTAINER", "ds2api-balance")
    try:
        raw = subprocess.check_output(
            ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
            text=True, timeout=20, stderr=subprocess.DEVNULL)
        for item in json.loads(raw):
            if "nvapi-" in item:
                found = re.findall(r"nvapi-[A-Za-z0-9_\-]{20,}", item)
                if found:
                    return found[0]
    except Exception:  # noqa: BLE001
        return None
    return None


_STUB_SENTENCE = "它把碗推到桌面中间,没有立刻开口。窗外雨点敲着篷布,一下接着一下。"


def _install_stub_model() -> None:
    """把模型入口换成本地确定性实现,只用于编排层规模测试。

    **产物不是文学内容**,不要把桩文本当质量证据:这里只验证"多章连续跑时会不会
    丢章、能不能断点恢复、每章有没有明确终态"。实体/认知提取返回合法空结果
    (五个空数组 / ``[]``),这样前置条件仍然按真实规则判定,而不是把门槛关掉。
    """
    import json as _json
    import types as _types

    from app.services import prompt_registry as pr
    from app.services import scene_orchestrator as so

    # 不改写、不截断:句子重复到足够长即可,截断会让正文门禁判成 truncated。
    prose = _STUB_SENTENCE * 40
    summary = "演练用摘要:本章人物完成一次交接,时间推进一天,物件仍在原持有人手中。"
    entity_json = _json.dumps({key: [] for key in (
        "new_characters", "state_changes", "new_relationships",
        "changed_relationships", "location_changes")}, ensure_ascii=False)

    class _Result:
        __slots__ = ("text", "model", "finish_reason", "usage")

        def __init__(self, text: str, finish_reason: str = "stop") -> None:
            self.text = text
            self.model = "stub"
            self.finish_reason = finish_reason
            self.usage = _types.SimpleNamespace(input_tokens=0, output_tokens=0)

    def _scene_plan(target_words: int) -> dict:
        fields = {name: f"演练占位-{name}" for name in (
            "start_state", "time_delta", "location_path", "entity_transfers",
            "power_resource_map", "information_state", "mechanism_limits",
            "result_strength", "transition_bridge", "continuity_ledger")}
        return {"scenes": [{
            "idx": 1, "title": "演练场", "brief": "演练占位场景",
            "pov": "演练", "location": "演练地", "time_cue": "当日",
            "key_action": "演练动作", "hook": "", "target_words": target_words,
            **fields}]}

    async def _text(task_type, user_content, db=None, **kwargs):
        if task_type == "chapter_entities":
            return _Result(entity_json)
        if task_type == "chapter_cognition":
            return _Result("[]")
        if task_type == "summary":
            return _Result(summary)
        return _Result(prose)

    async def _stream(task_type, user_content, db=None, **kwargs):
        for start in range(0, len(prose), 200):
            yield prose[start:start + 200]

    async def _structured(task_type, user_content, db=None, **kwargs):
        if task_type == "scene_planner":
            return _scene_plan(int(kwargs.get("target_words") or 1200))
        return {}

    pr.run_text_prompt = _text
    pr.stream_text_prompt = _stream
    pr.run_structured_prompt = _structured
    so.run_text_prompt = _text
    so.stream_text_prompt = _stream


def _prepare_env(db_url: str) -> None:
    """把 app 配置指向本机隔离栈;必须在 import app.* 之前调用。"""
    os.environ["DATABASE_URL"] = db_url
    os.environ.setdefault("SECRET_KEY", "continuity-probe-secret")
    os.environ.setdefault("NEO4J_URI", "bolt://127.0.0.1:57687")
    os.environ.setdefault("NEO4J_USER", "neo4j")
    os.environ.setdefault("NEO4J_PASSWORD", "changeme123")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:56379/0")
    os.environ.setdefault("QDRANT_HOST", "127.0.0.1")
    os.environ.setdefault("QDRANT_PORT", "56333")
    # 接上应用日志:ContextPack 每装配一次就会 INFO 出一行
    # ``ContextPack budget=… estimated_tokens=… trace=…``,里面有"哪些材料因预算被丢/被截"。
    # 不接这一行就什么也看不到——那是"上下文用没用满、丢了什么"的唯一现场证据。
    os.environ.setdefault("LOG_LEVEL", "INFO")
    try:
        from app.observability.logging import setup_logging

        setup_logging(os.environ["LOG_LEVEL"])
    except Exception:  # noqa: BLE001
        pass

# 长链探针要求"一次提取软失败不能停住整本书",否则任何一次模型没照抄依据
# 都会让测量作废(实测 2026-11-14/16 三次)。产品默认仍是阻断;
# 这里显式打开并把降级记进日志,便于事后补跑提取。
os.environ.setdefault("ALLOW_COGNITION_DEGRADED_CONTINUATION", "1")


# 每章目标字数(2026-11-17:1200 → 2000)。用户口径是"单章至少 2000 字",
# 探针此前写死 1200,把"目标就偏低"量成了"模型写不够"。——words 可临时覆盖。
CHAPTER_TARGET_WORDS: int = 2000


def _chapter_specs(count: int) -> list[dict[str, Any]]:
    """取前 ``count`` 章大纲。超过手写 10 章的部分按同一模板续写。

    手写大纲只覆盖真实模型的 10 章验收;规模演练(桩模型)需要更多章时沿用同一套
    设定,标题与 brief 带上章号,避免生成"空大纲"让规划阶段无从判断。
    """
    specs = [dict(spec) for spec in CHAPTERS]
    for spec in specs:
        spec["words"] = int(CHAPTER_TARGET_WORDS)
    while len(specs) < count:
        seq = len(specs) + 1
        base = CHAPTERS[(seq - 1) % len(CHAPTERS)]
        specs.append({**base, "title": f"第{seq}章 演练续章", "time_keys": (), "place_keys": (),
                      "brief": f"(规模演练第 {seq} 章){base['brief']}"})
    return specs[:count]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _find(name: str, root: Path) -> Path:
    return root / name


async def bootstrap(db_url: str, api_key: str, model: str, endpoint: str,
                    chapters: int, embed_key: str | None = None,
                    max_tokens: int = 4096, style_profile: str | None = None) -> dict[str, Any]:
    _prepare_env(db_url)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.session import Base
    from app.models.project import (
        Chapter, Character, LLMEndpoint, Outline, Project, Volume,
    )
    from app.models.prompt import PromptAsset
    from app.utils.crypto import encrypt_api_key

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        # 风格卡绑定(2026-11-19):项目 settings_json 的 style_profile_id 会被
        # style_runtime.resolve_active_profile 优先读取,写作链据此注入作者文笔。
        # 这里支持按 UUID 或按名称指定;指定了却找不到就**明确失败** —— 静默
        # 不绑定会让跑批看起来"用了参考文笔",实际是模型默认风格。
        settings_json: dict[str, Any] = {}
        if style_profile:
            from sqlalchemy import select
            from app.models.project import StyleProfile

            profile = None
            try:
                from uuid import UUID
                profile = await db.get(StyleProfile, str(UUID(style_profile)))
            except Exception:  # noqa: BLE001 - 不是 UUID 就按名称查
                profile = None
            if profile is None:
                profile = (await db.execute(
                    select(StyleProfile).where(StyleProfile.name == style_profile)
                )).scalars().first()
            if profile is None:
                raise SystemExit(
                    f"找不到风格卡:{style_profile}(按 id 或 name 都没匹配到);"
                    "先用参考书跑一次导入/蒸馏,或直接给 UUID"
                )
            settings_json["style_profile_id"] = str(profile.id)
            settings_json["style_reference"] = {"profile_id": str(profile.id),
                                               "name": profile.name}
            print(json.dumps({"style_profile": profile.name, "profile_id": str(profile.id)},
                             ensure_ascii=False))
        project = Project(title=BLUEPRINT["title"], genre=BLUEPRINT["genre"],
                          premise=BLUEPRINT["premise"], target_word_count=200000,
                          settings_json=settings_json)
        db.add(project)
        await db.flush()
        volume = Volume(project_id=project.id, title=BLUEPRINT["volume"]["title"],
                        volume_idx=1, summary=BLUEPRINT["volume"]["summary"],
                        target_word_count=200000)
        db.add(volume)
        await db.flush()
        db.add(Outline(project_id=project.id, level="book", content_json={
            "title": BLUEPRINT["title"], "genre": BLUEPRINT["genre"],
            "premise": BLUEPRINT["premise"], "raw_text": BLUEPRINT["premise"],
        }, is_confirmed=1))
        db.add(Outline(project_id=project.id, level="volume", content_json={
            "volume_idx": 1, "title": BLUEPRINT["volume"]["title"],
            "summary": BLUEPRINT["volume"]["summary"],
        }, is_confirmed=1))
        for name, profile in (
            ("周芸", {"role": "姐姐", "trade": "缝纫", "base": "县城", "note": "不肯欠人情"}),
            ("周航", {"role": "弟弟", "trade": "修电器", "base": "镇上", "note": "不愿姐姐为难"}),
            ("老周", {"role": "渡船", "trade": "摆渡", "base": "渡口", "note": "话少,看得清"}),
        ):
            db.add(Character(project_id=project.id, name=name, profile_json=profile))

        chapter_rows = []
        for idx, spec in enumerate(_chapter_specs(chapters), start=1):
            row = Chapter(volume_id=volume.id, chapter_idx=idx, global_idx=idx,
                          title=spec["title"], status="draft",
                          target_word_count=spec["words"], outline_json={})
            db.add(row)
            chapter_rows.append((row, spec))
        await db.flush()
        for row, spec in chapter_rows:
            outcomes = [part.strip() for part in
                        re.split(r"[;。]", spec["brief"].split("本场需完成:")[-1]) if part.strip()]
            # 生产 schema:只有 summary / key_events 这些字段会被
            # context_pack._render_chapter_outline_block 渲染进提示。早前这里用的是
            # brief/pov/location/time_cue 这类自定义字段,渲染器不认识,于是【本章大纲】
            # 只剩标题,第 2 章起整章脱离大纲。那是这套测量装置的缺陷,不是产品行为,
            # 已改为生产字段名重跑。
            content = {
                "chapter_idx": row.chapter_idx,
                "title": spec["title"],
                "summary": (f"时间:{spec['time']};地点:{spec['place']};视角:{spec['pov']}。"
                            f"{spec['brief']}"),
                "key_events": outcomes,
                "target_words": spec["words"],
            }
            row.outline_json = content
            db.add(Outline(project_id=project.id, level="chapter", parent_id=None,
                           content_json=content, is_confirmed=1))
        await db.flush()

        # 幂等(2026-11-20):此前每跑一次 bootstrap 就**新建**一条旗舰端点,端点表
        # 一路涨到 383 行;更糟的是每行都用当时的 SECRET_KEY 加密,换环境后整批解不开,
        # 而解密失败会(曾经)打挂整次模型配置加载 —— 表现成
        # "no_embedding_provider / 模型配置加载失败",极难定位。
        # 现在:有可用旗舰端点就复用;解不开就用本次的 api_key 重写密文(自愈)。
        from sqlalchemy import select as _select3
        from app.utils.crypto import decrypt_api_key as _decrypt
        endpoint_row = (await db.execute(
            _select3(LLMEndpoint).where(LLMEndpoint.enabled == 1,
                                       LLMEndpoint.tier == "flagship")
        )).scalars().first()
        if endpoint_row is None:
            endpoint_row = LLMEndpoint(name="continuity-probe", provider_type="openai_compatible",
                                       base_url=endpoint, api_key=encrypt_api_key(api_key),
                                       default_model=model, tier="flagship", enabled=1)
            db.add(endpoint_row)
            await db.flush()
        else:
            try:
                _decrypt(endpoint_row.api_key or "")
            except Exception:  # noqa: BLE001 - 换过 SECRET_KEY 的旧行
                endpoint_row.api_key = encrypt_api_key(api_key)
                endpoint_row.default_model = endpoint_row.default_model or model
        from app.services.prompts.scene_writing import (
            SCENE_PLANNER_SYSTEM, SCENE_WRITER_SYSTEM,
        )
        # 必须绑定真实 endpoint_id:关系范围核查(relationship_scope)不像别的任务那样
        # 走 tier 回退,它要求 snapshot.endpoint_id 非空,否则整章实体投影报
        # relationship_scope_prompt_not_configured(实测 2026-11-14 第 18 章)。
        await _seed_prompts(db, endpoint_id=endpoint_row.id, model=model,
                            max_tokens=max_tokens, embed_key=embed_key)
        await db.commit()
        ids = {"project_id": str(project.id), "volume_id": str(volume.id),
               "chapters": [str(row.id) for row, _ in chapter_rows]}
    await engine.dispose()
    return ids


async def run_chain(db_url: str, ids: dict[str, Any], api_key: str, model: str,
                    endpoint: str, out: Path, chapter_limit: int | None,
                    resume: bool, extract: bool = True,
                    max_consecutive_errors: int = 1,
                    embed_key: str | None = None,
                    quality_gate: bool = True) -> dict[str, Any]:
    _prepare_env(db_url)
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.project import Chapter, LLMEndpoint
    from app.models.prompt import PromptAsset
    from app.db.neo4j import init_neo4j
    from app.services.scene_orchestrator import SceneOrchestrator
    from app.utils.crypto import encrypt_api_key

    # 生产里驱动由 FastAPI lifespan 初始化;这里直接调服务层,必须自己起。
    await init_neo4j()

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        await db.execute(update(LLMEndpoint).values(enabled=0))
        await db.execute(update(PromptAsset).values(is_active=0))
        ep = LLMEndpoint(name="continuity-probe", provider_type="openai_compatible",
                         base_url=endpoint, api_key=encrypt_api_key(api_key),
                         default_model=model, tier="flagship", enabled=1)
        db.add(ep)
        await db.flush()
        from app.services.prompts.scene_writing import (
            SCENE_PLANNER_SYSTEM, SCENE_WRITER_SYSTEM,
        )
        await _seed_prompts(db, endpoint_id=ep.id, model=model, embed_key=embed_key)
        await db.commit()
    await engine.dispose()

    orchestrator = SceneOrchestrator()
    targets = {i: spec["words"] for i, spec in enumerate(_chapter_specs(500), start=1)}

    # 记录每章的场景规划(场数与各场目标字数):先分清"写不够"是规划给的少,还是写手没写满。
    planned: dict[str, Any] = {}
    _original_plan = orchestrator.plan_scenes

    async def _plan_and_record(*, pack, db, project_id, chapter_id, target_words,
                               n_scenes_hint=None, user_instruction=""):
        briefs = await _original_plan(pack=pack, db=db, project_id=project_id,
                                      chapter_id=chapter_id, target_words=target_words,
                                      n_scenes_hint=n_scenes_hint,
                                      user_instruction=user_instruction)
        planned["scenes"] = [{"idx": brief.idx, "title": brief.title,
                              "target_words": brief.target_words} for brief in briefs]
        planned["chapter_target"] = target_words
        return briefs

    orchestrator.plan_scenes = _plan_and_record
    records: list[dict[str, Any]] = []
    consecutive_errors = 0
    for index, chapter_id in enumerate(ids["chapters"], start=1):
        if chapter_limit is not None and index > chapter_limit:
            break
        target = out / f"ch{index:02d}.txt"
        if resume and target.exists() and target.stat().st_size > 200:
            if await _chapter_is_complete(session_factory, chapter_id):
                # 与下面新写一章的分支保持同一单位:字符数,不是文件字节数。
                # 混用会让续跑章节的字数看起来是其他章的三倍。
                records.append({"index": index, "status": "resumed",
                                "chars": len(target.read_text(encoding="utf-8"))})
                continue

            if await _chapter_text_complete(session_factory, chapter_id):
                # 正文没问题,缺的是实体/认知那一步(实测:第 6 章认知提取连续失败,
                # 第 7 章起被 character_cognition_not_ready 挡住)。补跑提取,
                # 不要把已经写好的正文当残稿重写。
                records.append({"index": index, "status": "resumed_extract",
                                "chars": len(target.read_text(encoding="utf-8")),
                                "extraction": await _post_chapter_extraction(
                                    ids["project_id"], index, chapter_id)})
                continue
            # 文件在、库里没做完:重做这一章,并把旧的残缺文件标注出来
            target.rename(target.with_suffix(".incomplete.txt"))
        started = time.monotonic()
        parts: list[str] = []
        record: dict[str, Any] = {"index": index, "chapter_id": chapter_id}
        attempt = 0
        failure: Exception | None = None
        max_attempts = 1 + _chapter_retry_limit()
        while attempt < max_attempts:
            attempt += 1
            failure = None
            parts = []
            try:
                async for chunk in orchestrator.orchestrate_chapter_stream(
                    project_id=ids["project_id"], volume_id=ids["volume_id"],
                    chapter_idx=index, db=None, chapter_id=chapter_id,
                    user_instruction="",
                    target_words=int(targets.get(index) or CHAPTER_TARGET_WORDS),
                ):
                    parts.append(chunk)
            except Exception as exc:  # noqa: BLE001
                failure = exc
            if failure is None:
                break
            # 语义召回依赖第三方嵌入服务,偶发 ReadTimeout / CUDA 报错会让本章
            # 抛 character_cognition_retrieval_unavailable(上一章则 not_ready)。
            # 这是**瞬时**故障:补跑上一章的实体/认知提取后重试本章,而不是留个洞。
            if attempt < max_attempts and _is_transient_context_error(failure):
                print(json.dumps({"index": index, "retry": attempt,
                                  "reason": str(failure)[:160]}, ensure_ascii=False), flush=True)
                await _repair_context_before(ids, index)
                await asyncio.sleep(_retry_sleep_seconds())
                continue
            break
        if failure is not None:
            record.update(status="error", error_type=type(failure).__name__,
                          error_message=str(failure)[:300],
                          error_code=getattr(failure, "code", None),
                          attempts=attempt)
            records.append(record)
            target.with_suffix(".error.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "chapters.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(record, ensure_ascii=False), flush=True)
            consecutive_errors += 1
            if consecutive_errors >= max(1, max_consecutive_errors):
                break
            continue
        text = "".join(parts).strip()
        from app.services import chapter_quality_gate as cqg
        guard = "empty"
        if text:
            guard = ("relay_error" if cqg.looks_like_relay_error(text)
                     else "refusal" if cqg.looks_like_refusal(text)
                     else "truncated" if cqg.looks_truncated(text) else "ok")
        record.update(status="ok" if guard == "ok" else guard,
                      chars_raw=len(text), guard=guard)
        if guard == "ok" and quality_gate:
            # 生产链在初稿之后还有一道 apply_chapter_quality_gate(去 AI 味 + 机械痕迹门
            # + 不足目标时定向改写)。探针此前直接量初稿,会把"初稿偏短"误报成"产出偏短"。
            from app.services.chapter_quality_gate import apply_chapter_quality_gate
            try:
                gate = await apply_chapter_quality_gate(
                    text=text, db=None, project_id=ids["project_id"],
                    chapter_id=chapter_id, target_word_count=targets.get(index, CHAPTER_TARGET_WORDS))
                text = gate.final_text or text
                record["gate"] = {
                    "status": str(getattr(gate.status, "value", gate.status)),
                    "rewrite_rounds": gate.rewrite_rounds,
                    "chars_after": len(text),
                }
            except Exception as exc:  # noqa: BLE001
                record["gate_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        # 评估产物与生产落库必须走**同一个**净化器:这些 ch*.txt 是对比页/总览页的
        # 正文来源,混进服务话术会被当成作品正文展示给读者(实测
        # style-ref-10/ch01.txt 就带着 "I seem to be encountering an error…")。
        # 此前 probe 的文本直接取自 scene_orchestrator 的流,绕过了 api/generate 与
        # tasks 里的 sanitize_prose —— 这就是"评估产物脏、生产库干净"的来源。
        try:
            from app.services.prose_sanitizer import sanitize_prose

            text, _noise_hits = sanitize_prose(text)
            if _noise_hits:
                # 结构化记录(probe 不用 logger,产物是 chapters.json)
                record["sanitized"] = _noise_hits[:5]
        except Exception as sanitize_exc:  # noqa: BLE001 - 净化失败不该停掉跑批
            record["sanitize_error"] = (
                f"{type(sanitize_exc).__name__}: {str(sanitize_exc)[:120]}")
        record["chars"] = len(text)
        record["plan"] = dict(planned)
        planned.clear()
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        target.write_text(text, encoding="utf-8")
        # 落库 + 真实摘要,后续章节的上下文才和线上一致
        if text:
            async with session_factory() as db:
                chapter = await db.get(Chapter, chapter_id)
                chapter.content_text = text
                chapter.status = "completed" if guard == "ok" else "draft"
                chapter.word_count = len(text)
                await db.commit()
            if guard == "ok":
                from app.services.chapter_summarizer import summarize_and_save_chapter
                try:
                    written, summary = await summarize_and_save_chapter(
                        chapter_id=chapter_id, db=None, overwrite=True,
                        project_vectors=False, expected_content=text)
                    record["summary_written"] = bool(written)
                    record["summary_chars"] = len(summary or "")
                except Exception as exc:  # noqa: BLE001
                    record["summary_written"] = False
                    record["summary_error"] = type(exc).__name__
            if guard == "ok" and extract:
                record.update(await _post_chapter_extraction(
                    ids["project_id"], index, chapter_id))
        records.append(record)
        (out / "chapters.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if record["status"] in ("ok", "resumed", "resumed_extract"):
            consecutive_errors = 0
        else:
            consecutive_errors += 1
            if consecutive_errors >= max(1, max_consecutive_errors):
                break
    try:
        await prompt_cache.flush_pending_counts()
    except Exception:  # noqa: BLE001
        pass
    return {"records": records}


# ---------------------------------------------------------------------------
# 跨章确定性检查
# ---------------------------------------------------------------------------

_DAY_MARKERS = (("后天", 2), ("明天", 1), ("次日", 1), ("第二天", 1), ("今日", 0),
                ("今天", 0), ("当日", 0), ("昨天", -1), ("昨日", -1))


_REMAINDER_RE = re.compile(
    r"(?:剩下|还差|缺口|余下|还欠|欠着|尚欠)(?:的)?(?:那|这)?"
    r"([一二两三四五六七八九十百千万]+|\d+)(元|块钱|块)?")


def _remainder_conflicts(text: str) -> list[str]:
    """同一章、**同一句**里"还欠/剩下"的数额不一致 —— 真实链第一章就踩过(一千 vs 八百)。

    没有元/块后缀、又只有一个汉字数字的不算金额:"剩下的两"是两个人,
    "还差一"是一点点。实测(2026-11-14)这个小口子贡献了 2/3 的误报。

    还要限定在同一句:一章里分别说"剩下的一千"(欠姐姐)和"剩下的八百"
    (铺租)是完全正常的,把它们算成冲突会制造新的误报(实测 2026-11-16 两处全误报)。
    """
    values: dict[str, int] = {}
    for sentence in re.split(r"[。！？!?\n]+", text or ""):
        found: dict[str, int] = {}
        for m in _REMAINDER_RE.finditer(sentence):
            token, unit = m.group(1), m.group(2)
            if not unit and len(token) < 2:
                continue
            found[token] = found.get(token, 0) + 1
        if len(found) > 1:
            values.update(found)
    return sorted(values) if len(values) > 1 else []


_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]{2,})(元|块钱|块)?")
# 没有"元/块"后缀时,这些后缀说明它是数量或时间,不是金额。
_AMOUNT_STOP_UNITS = ("点", "分", "章", "月", "年", "天", "人", "套", "件", "岁", "米", "斤", "里", "层")
_DELTA_MARKS = ("多出", "多", "差", "缺", "少了", "少", "凑", "减", "增加了")
# 名目与金额之间出现这些,就不再算同一个短句;
# 并列连词也算:"把复查费和那一千元旧账扣在一处"里的一千元不是复查费。
_CLAUSE_MARKS = "。!?!，,、;；\n和与及"

# 能定位"这一刻"的说法(时段/天色)。
_DAY_PART_KEYS = ("清晨", "早晨", "一早", "早上", "上午", "正午", "晌午", "中午", "午后",
                  "下午", "傍晚", "黄昏", "夕阳", "日落", "入夜", "夜晚", "夜里", "深夜",
                  "凌晨", "天亮", "天色", "天光", "日头", "阳光", "日光", "月色", "半夜",
                  # "太阳往下沉""暮色"这类同样是时段交代,漏了它们会把正常的写法报成缺失
                  # (实测 2026-11-16:第 10 章写了"太阳贴着山脊往下沉",仍被判缺失)。
                  "太阳", "月亮", "星光", "暮色", "夜色", "白昼", "破晓", "正午")
# 能看出"过了多久"的说法(相对推进,或能当锚点的日子)。
_TIME_PROGRESS_MARKERS = ("次日", "第二天", "隔天", "隔了一", "过了", "天后", "日后",
                          "周后", "月后", "头一天", "前一天", "昨天", "昨日", "前天",
                          "前些天", "前几天", "几天前", "这些天", "这两天", "上回",
                          "中秋", "除夕", "月底", "月初", "入冬", "开春")


def _declares_time_progress(declared: str) -> bool:
    """大纲声明的是"往后推一段"吗?

    "当日傍晚""中秋前三天"这种不要求叙事交代过了多久,只要求时段可定位。
    """
    if not declared:
        return False
    return any(mark in declared for mark in ("后", "次日", "第二天", "第三", "次周"))
_AMOUNT_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "零": 0}
_AMOUNT_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _amount_value(token: str) -> float | None:
    """把"一千二/八百二/1200"这类写法读成数值。

    口语里末位数字承接低一级单位:"八百二"=820、"一千二"=1200,不按字面相加。
    """
    if token.isdigit():
        return float(token)
    try:
        return float(token)
    except ValueError:
        pass
    total = 0
    num = 0
    last_unit = 0
    trailing = False
    for ch in token:
        if ch in _AMOUNT_DIGITS:
            num = _AMOUNT_DIGITS[ch]
            trailing = True
        elif ch in _AMOUNT_UNITS:
            unit = _AMOUNT_UNITS[ch]
            total += (num or 1) * unit
            last_unit = unit
            num = 0
            trailing = False
    if trailing and last_unit:
        total += num * (last_unit // 10)
    elif trailing:
        total += num
    return float(total) if total else None


def _nearby_amounts(text: str, keyword: str, window: int = 12) -> set[float]:
    """取 ``keyword`` 同一个短句里、前后 ``window`` 字内出现过的金额。

    先在全篇上找出完整金额,再判距离——早期版本按窗口切片后再找金额,
    会把"一千二百"截成"一千",凭空造出一个 1200→1000 的假矛盾(实测 2026-11-14)。

    三层排除:
    * 名目与金额之间隔着句读(。!?!，,、;；)的不算同一个短句——
      "我交了铺租,手里紧,你给的那一千"里的一千不是铺租;
    * 紧挨数字前的多/涨/差/缺/少/凑/减说明它是增量或差额,不是这个数额本身;
    * 没有元/块后缀时,后面跟着量词或时间词(个、天、章…)的不算金额。
    """
    found: set[float] = set()
    amounts = list(_AMOUNT_RE.finditer(text))
    for m in re.finditer(re.escape(keyword), text):
        for a in amounts:
            if a.end() <= m.start():
                gap, distance = text[a.end():m.start()], m.start() - a.end()
            elif a.start() >= m.end():
                gap, distance = text[m.end():a.start()], a.start() - m.end()
            else:
                gap, distance = "", 0
            if distance > window:
                continue
            if any(mark in gap for mark in _CLAUSE_MARKS):
                continue
            if any(mark in text[max(0, a.start() - 4):a.start()] for mark in _DELTA_MARKS):
                continue
            if not a.group(2) and text[a.end(): a.end() + 1] in _AMOUNT_STOP_UNITS:
                continue
            value = _amount_value(a.group(1))
            if value is not None:
                found.add(value)
    return found


# 这些错误是**瞬时**的,补跑上一步再重试本章即可,不该在长链里留下一个洞:
# * 语义召回依赖第三方嵌入服务(实测该模型会被上游下线、也会偶发超时);
# * 分场规划偶发返回非 JSON(同一臂里有的章成功),规划器自己的两轮修复用完就抛。
_TRANSIENT_CONTEXT_MARKERS = (
    "character_cognition_retrieval_unavailable",
    "character_cognition_not_ready",
    "scene_planner_failed",
)


def _chapter_retry_limit() -> int:
    try:
        return max(0, int(os.environ.get("PROBE_CHAPTER_RETRIES", "1")))
    except ValueError:
        return 1


def _retry_sleep_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("PROBE_RETRY_SLEEP", "15")))
    except ValueError:
        return 15.0


def _is_transient_context_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_CONTEXT_MARKERS)


async def _repair_context_before(ids: dict[str, Any], index: int) -> None:
    """把上一章的实体/认知提取补跑一遍(生产里由周期任务做)。

    本章的检索类失败,根因通常在上一步提取没落成 completed;只重试本章没用。
    """
    if index <= 1:
        return
    previous_id = ids["chapters"][index - 2]
    try:
        result = await _post_chapter_extraction(ids["project_id"], index - 1, previous_id)
        print(json.dumps({"index": index - 1, "repair": result}, ensure_ascii=False,
                         default=str), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"index": index - 1, "repair_error": str(exc)[:160]},
                         ensure_ascii=False), flush=True)

async def _chapter_text_complete(session_factory: Any, chapter_id: str) -> bool:
    """正文是否已经落库且 completed(不看认知)。"""
    from app.models.project import Chapter

    async with session_factory() as db:
        row = await db.get(Chapter, chapter_id)
        return bool(row is not None and row.status == "completed"
                    and (row.content_text or "").strip())


async def _chapter_is_complete(session_factory: Any, chapter_id: str) -> bool:
    """恢复时以数据库状态为准,不以"文件存在"为准。

    实测(2026-11-14 100 章演练):SIGKILL 落在"正文写完文件、落库还没提交"的窗口里,
    34 章留下 txt 却没有 completed 的正文与认知记录。若只按文件跳过,第 35 章起会一直
    报 ``character_cognition_not_ready``。所以要求:正文已落库且 completed,并且该章
    的认知记录也是 completed,才算这一章真的做完。
    """
    from sqlalchemy import select

    from app.models.cognition import ChapterCognition as Record

    if not await _chapter_text_complete(session_factory, chapter_id):
        return False
    async with session_factory() as db:
        record = (await db.execute(
            select(Record).where(Record.chapter_id == chapter_id)
            .order_by(Record.updated_at.desc()).limit(1))).scalar_one_or_none()
        return bool(record is not None and record.status == "completed")


async def _post_chapter_extraction(project_id: str, chapter_idx: int, chapter_id: str) -> dict[str, Any]:
    """按生产顺序跑实体提取与认知提取,让下一章的前置条件成立。

    线上这两步由章节保存触发器 + Celery 消费链完成;探针直接写库,必须自己补上,
    否则第 2 章会被 ``character_cognition_not_ready`` 拦住(这是设计如此,不是缺陷)。
    ``_extract_chapter_async`` 结束时会把 Neo4j 驱动关掉,所以要重新初始化。
    """
    from app.db.neo4j import init_neo4j
    from app.db.session import async_session_factory
    from app.services.cognition_reader import resolve_cognition_source
    from app.tasks.entity_tasks import _extract_chapter_async, _extract_cognition_async

    result: dict[str, Any] = {}
    # 线上这两步由 Celery 任务的 autoretry 兜住;探针自己重试,次数与线上一致。
    for attempt in range(1, 4):
        try:
            entity = await _extract_chapter_async(project_id, chapter_idx,
                                                  caller="continuity_probe", chapter_id=chapter_id)
            result["entity"] = {"status": entity.get("status"), "reason": entity.get("reason"),
                                "attempts": attempt,
                                "keys": sorted(str(k) for k in entity)[:10]}
            break
        except Exception as exc:  # noqa: BLE001
            result["entity_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            await init_neo4j()
    await init_neo4j()
    for attempt in range(1, 4):
        try:
            async with async_session_factory() as db:
                source = await resolve_cognition_source(db, project_id, chapter_id)
                await db.commit()
            if source is None:
                result["cognition"] = {"status": "no_source"}
                break
            cog = await _extract_cognition_async(project_id, chapter_id, source.source_hash)
            result["cognition"] = {"status": cog.get("status") if isinstance(cog, dict) else str(cog),
                                   "attempts": attempt}
            if result["cognition"]["status"] in ("completed", "ok"):
                break
        except Exception as exc:  # noqa: BLE001
            result["cognition_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            # 依据定位失败时把"哪几条找不到"原样留下:没有它只能猜模型错在哪,
            # 而这一类失败会让整本书停住(实测三次),必须能定位。
            offending = getattr(exc, "offending_evidence", None)
            if offending:
                result["cognition_offending"] = list(offending)[:5]
    await init_neo4j()
    return result


def check(out: Path) -> dict[str, Any]:
    texts = []
    # 只认 chNN.txt:恢复时留下的 chNN.incomplete.txt 是被丢弃的残稿,
    # 不能混进字数与事实检查(否则会把"已作废"的片段算成正文)。
    chapter_file = re.compile(r"^ch\d+\.txt$")
    for path in sorted(p for p in out.glob("ch*.txt") if chapter_file.match(p.name)):
        if path.stat().st_size:
            texts.append((path.name, path.read_text(encoding="utf-8")))
    findings: list[dict[str, Any]] = []
    specs = _chapter_specs(200)
    targets = {f"ch{index:02d}.txt": spec["words"] for index, spec in enumerate(specs, 1)}
    # 每章声明的时间/地点是"读者必须能定位到这一刻"的最低要求;
    # 实测模型会整章不提时间或换掉地点(大纲写面摊,正文写渡口),读者接不上。
    markers = {f"ch{index:02d}.txt": (spec.get("time_keys", ()), spec.get("place_keys", ()),
                                      spec.get("time", ""), spec.get("pov", ""))
               for index, spec in enumerate(specs, 1)}
    word_counts = {}
    for name, text in texts:
        target = targets.get(name)
        ratio = round(len(text) / target, 3) if target else None
        word_counts[name] = {"chars": len(text), "target": target, "ratio": ratio}
        # 按本章自己的目标判,而不是一个全局字数带:实测 1200 字目标下三章分别只有
        # 59%/69%/92%,全局 850–1800 的带子只报了两章,掩盖了系统性偏低。
        if target and len(text) < target * 85 // 100:
            findings.append({"check": "word_below_target", "chapter": name,
                             "chars": len(text), "target": target, "ratio": ratio,
                             "detail": f"字数低于本章目标 {target} 字的 85%"})
        time_keys, place_keys, declared_time, declared_pov = markers.get(name, ((), (), "", ""))
        # 时间交代拆成两问,因为"读者能不能定位到这一刻"和"能不能看出过了多久"
        # 是两件事。原先只用一份关键词表,把"夕阳""清晨"这类正常写法判成缺失。
        anchored = (any(key in text for key in _DAY_PART_KEYS)
                    or (time_keys and any(key in text for key in time_keys)))
        if not anchored:
            findings.append({"check": "time_of_day_missing", "chapter": name,
                             "declared": declared_time,
                             "detail": "全章没有任何时段说法,读者无法定位这一刻"})
        if _declares_time_progress(declared_time) and not any(
                key in text for key in _TIME_PROGRESS_MARKERS):
            findings.append({"check": "time_progress_missing", "chapter": name,
                             "declared": declared_time,
                             "detail": "大纲声明的是相对时间推进,正文却没有任何"
                                       "「过了多久」的说法,读者接不上时间线"})
        if place_keys and not any(key in text for key in place_keys):
            findings.append({"check": "place_missing", "chapter": name,
                             "keys": list(place_keys),
                             "detail": "全章没有出现本场地点"})
        # 视角漂移:实测未修复前,大纲声明的视角人物在第 2 章起完全不出现在开头,
        # 整章变成另一个人的场面(读者跟丢主角)。只在开头 300 字内判定,避免把
        # 合法的\"先写旁人再转到视角人物\"当成缺陷,命中后仍需人工核对。
        if declared_pov and declared_pov not in text[:300]:
            findings.append({"check": "pov_missing", "chapter": name,
                             "declared": declared_pov,
                             "detail": f"开头 300 字内没有出现本章声明的视角人物 {declared_pov}"})
        conflict = _remainder_conflicts(text)
        if conflict:
            findings.append({"check": "remainder_conflict", "chapter": name,
                             "values": conflict,
                             "detail": "同一章里「剩下/还欠」的数额不一致"})
    # 大纲没点名的具名人物:长篇里补一个街坊是合理的,所以只提示、不判错,
    # 但必须能数出来——否则"人物越写越多"会无声发生。
    known_text = " ".join(spec["brief"] + spec["title"] for spec in specs)
    unknown: dict[str, set[str]] = {}
    for name, text in texts:
        for match in list(_NEW_TITLE_RE.finditer(text)) + list(_ROLE_NAME_RE.finditer(text)):
            candidate = match.group(1)
            if candidate in ROSTER or any(known in candidate for known in ROSTER):
                continue
            if candidate in known_text or candidate in _GENERIC_TITLE_NOUNS:
                continue
            # "周姐""陈叔""周老板"这类是对已有人物的当面称呼,不是新人物。
            # 用"首字出现在任一大纲人物名里"判定,这样"陈叔"(老陈)也能过滤掉。
            if (any(candidate[0] in name for name in ROSTER)
                    and candidate[1:] in _ADDRESS_OR_ROLE_TAILS):
                continue
            if candidate[0] not in _SURNAMES and not candidate.startswith(_NAME_PREFIXES):
                continue
            unknown.setdefault(candidate, set()).add(name)
    for candidate, chapters_seen in sorted(unknown.items()):
        findings.append({"check": "unknown_character", "name": candidate,
                         "chapters": sorted(chapters_seen),
                         "detail": f"出现大纲未点名的具名人物「{candidate}」,需核对是否为有意新增"})

    #
    # 只在同一句内、且不是增量/差额时才判定(见 ``_nearby_amounts``);
    # 另外把"钉死之前就已经出现过的同名目金额"当合法历史值——第 16 章说
    # "前阵子八百块的铺租,眼下涨到九百",八百是历史,不是矛盾。
    established: dict[str, set[float]] = {}
    for name, text in texts:
        chapter_number = int(re.sub(r"\D", "", name) or 0)
        for pin_name, pin_keyword, _allowed in PINS:
            if chapter_number < int(re.sub(r"\D", "", pin_name) or 0):
                established.setdefault(pin_keyword, set()).update(
                    _nearby_amounts(text, pin_keyword, window=12))
    for name, text in texts:
        chapter_number = int(re.sub(r"\D", "", name) or 0)
        for pin_name, pin_keyword, allowed in PINS:
            pinned_number = int(re.sub(r"\D", "", pin_name) or 0)
            if chapter_number <= pinned_number:
                continue
            allowed_values = {_amount_value(a) for a in allowed} - {None}
            stale = sorted(v for v in _nearby_amounts(text, pin_keyword, window=12)
                           if v not in allowed_values
                           and v not in established.get(pin_keyword, set()))
            if stale:
                findings.append({"check": "pinned_number_conflict", "chapter": name,
                                 "keyword": pin_keyword, "pinned_in": pin_name,
                                 "allowed": list(allowed), "found": stale,
                                 "detail": f"「{pin_keyword}」在 {pin_name} 已定为 "
                                           f"{'/'.join(allowed)},本章出现别的金额 "
                                           f"{'/'.join(str(int(v)) for v in stale)}"})
    for i in range(1, len(texts)):
        prev = _norm(texts[i - 1][1])[:60]
        cur = _norm(texts[i][1])[:60]
        if prev and cur:
            overlap = len(set(prev) & set(cur)) / max(1, len(set(prev) | set(cur)))
            if overlap >= 0.75:
                findings.append({"check": "repeated_opening", "chapter": texts[i][0],
                                 "detail": f"与上一章开头字符重合度 {overlap:.2f}"})
    # 通用的"同一名目金额跨章不同"检查这里**故意不做**:
    # 试过的两版都把"呢""的铺租""周芸"当成名目,命中几乎全是误报
    # (2026-11-14:两臂共 6 条命中,人工核对后真实矛盾 0 条)。
    # 与其给报告灌噪声,不如把这一格标成"未覆盖":需要产品侧的金额台账
    # 才能做好。现在能守的是上面 PINS 那几条**已声明**的数额,以及
    # BOOK_FACTS 里书级既定的数字与不得出现的改写说法。
    joined = "\n".join(text for _, text in texts)
    for prop in BOOK_FACTS["props"]:
        if prop not in joined:
            findings.append({"check": "prop_missing", "prop": prop,
                             "detail": f"全篇未出现已设定的道具「{prop}」"})
    for number in BOOK_FACTS["numbers"]:
        if number not in joined:
            findings.append({"check": "book_fact_missing", "fact": number,
                             "detail": f"书级既定数字「{number}」全篇未出现"})
    for bad in BOOK_FACTS["conflicts"]:
        for name, text in texts:
            if bad in text:
                findings.append({"check": "book_fact_conflict", "chapter": name, "fact": bad,
                                 "detail": f"出现与书级既定数字冲突的说法「{bad}」"})
                break
    return {"chapters": len(texts), "word_counts": word_counts,
            "findings": findings, "violations": len(findings)}



def load_seed_file(path: str) -> dict[str, Any]:
    """读外部题材种子(JSON),用于在**另一个题材**上重跑同一套长跑流程。

    为什么需要:内置题材是《渡口旧账》(现实生活)。跨模型/跨参数的结论要能推广,
    就得能在不同题材、不同人物与地点上跑同一条生产链 —— 换题材不该意味着改代码。

    种子是纯数据,格式::

        {"blueprint": {"title": ..., "genre": ..., "premise": ..., "volume": {...}},
         "roster": ["人物", ...],
         "chapters": [{"title": "第一章 …", "time": …, "place": …, …}, ...],
         "pins": [["ch11.txt", "关键词", ["允许写法"]]]}   # 可选

    ``pins`` 不提供时**置空**:内置的金额钉子是按《渡口旧账》章号写的,换题材后
    继续套用只会产生误报。
    """
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("题材种子必须是 JSON 对象")
    blueprint = raw.get("blueprint")
    chapters = raw.get("chapters")
    if not isinstance(blueprint, dict) or not blueprint.get("title") or not blueprint.get("premise"):
        raise ValueError("题材种子缺少 blueprint.title / blueprint.premise")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("题材种子缺少非空 chapters")
    for index, item in enumerate(chapters, 1):
        if not isinstance(item, dict) or not item.get("title"):
            raise ValueError(f"题材种子第 {index} 章缺少 title")
    roster = raw.get("roster") or []
    if not isinstance(roster, list):
        raise ValueError("题材种子的 roster 必须是数组")
    pins = raw.get("pins") or []
    if not isinstance(pins, list):
        raise ValueError("题材种子的 pins 必须是数组")
    return {"blueprint": blueprint, "chapters": chapters,
            "roster": tuple(str(name) for name in roster),
            "pins": tuple((str(a), str(b), tuple(str(c) for c in d)) for a, b, d in pins)}

def main(argv: list[str] | None = None) -> int:
    global CHAPTER_TARGET_WORDS

    parser = argparse.ArgumentParser(prog="continuity_probe")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="gemini-3.1-pro")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="prompt 资产的输出上限;推理模型需要比 gemini 大得多,"
                             "否则思考占满额度、正文被截断成 provider_incomplete_response")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8317/v1")
    parser.add_argument("--chapters", type=int, default=10)
    parser.add_argument("--words", type=int, default=None,
                        help=f"每章目标字数(默认 {CHAPTER_TARGET_WORDS})")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--ids", default=None, help="复用已有项目的 ids JSON")
    parser.add_argument("--limit", type=int, default=None, help="本轮最多再跑几章")
    parser.add_argument("--resume", action="store_true", help="跳过已有正文的章")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--no-quality-gate", action="store_true",
                        help="跳过章节质量门,只量初稿(用于对照)")
    parser.add_argument("--no-extract", action="store_true",
                        help="跳过每章后的实体/认知提取(默认执行,否则第 2 章起会被门槛拦住)")
    parser.add_argument("--stub-model", action="store_true",
                        help="用本地桩替换模型,只验证编排(连写/恢复/终态),不产文学内容")
    parser.add_argument("--embedding-key", default=None,
                        help="NVIDIA embedding key;缺省时读 QUALITY_EVAL_EMBED_KEY 或本机容器配置")
    parser.add_argument("--max-consecutive-errors", type=int, default=1,
                        help="连续多少章失败后停止(默认 1)")
    parser.add_argument("--seed-file", default=None,
                        help="题材种子 JSON:换到另一个题材跑同一套流程(缺省用内置《渡口旧账》)")
    parser.add_argument("--style-profile", default=None,
                        help="绑定的风格卡(id 或名称)。写作链会据此注入参考文笔;"
                             "给了却找不到会直接失败,不会静默退化成模型默认风格")
    args = parser.parse_args(argv)
    if args.seed_file:
        # 纯数据覆盖:只换题材/人物/章节大纲/金额钉子,不动任何生产代码路径。
        seed = load_seed_file(args.seed_file)
        globals().update({"BLUEPRINT": seed["blueprint"], "CHAPTERS": seed["chapters"],
                          "ROSTER": seed["roster"], "PINS": seed["pins"]})
        print(json.dumps({"seed": args.seed_file, "title": seed["blueprint"]["title"],
                          "genre": seed["blueprint"].get("genre"),
                          "chapters": len(seed["chapters"]),
                          "roster": list(seed["roster"])}, ensure_ascii=False))
    if args.words:
        CHAPTER_TARGET_WORDS = int(args.words)

    out = Path(args.out)
    if args.check_only:
        result = check(out)
        (out / "continuity.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"chapters": result["chapters"], "violations": result["violations"]},
                         ensure_ascii=False))
        return 0 if result["violations"] == 0 else 1

    # 必须在任何 app.* 导入之前落环境:settings 在导入时就固化 DATABASE_URL。
    _prepare_env(args.database_url)
    if args.stub_model:
        _install_stub_model()
        print(json.dumps({"stub_model": True}, ensure_ascii=False), flush=True)
    from scripts.quality_eval.runner import resolve_api_key
    key = args.api_key or os.environ.get("QUALITY_EVAL_API_KEY") or resolve_api_key(None)
    embed_key = args.embedding_key or os.environ.get("QUALITY_EVAL_EMBED_KEY") or _detect_embedding_key()
    print(json.dumps({"embedding_key_present": bool(embed_key),
                      "embedding_model": DEFAULT_EMBED_MODEL}, ensure_ascii=False), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    ids_path = Path(args.ids) if args.ids else out / "ids.json"
    # 章标题:对照页要显示"第 N 章 · 标题",否则两栏看起来只有序号
    titles = {f"ch{index:02d}.txt": spec.get("title", "")
              for index, spec in enumerate(_chapter_specs(args.chapters), 1)}
    (out / "titles.json").write_text(
        json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8")
    if ids_path.exists():
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
    else:
        ids = asyncio.run(bootstrap(args.database_url, key, args.model, args.endpoint,
                                    args.chapters, embed_key,
                                    max_tokens=args.max_tokens,
                                    style_profile=args.style_profile))
        ids_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
    asyncio.run(run_chain(args.database_url, ids, key, args.model, args.endpoint,
                          out, args.limit, args.resume, not args.no_extract,
                          args.max_consecutive_errors, embed_key,
                          not args.no_quality_gate))
    result = check(out)
    (out / "continuity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"chapters": result["chapters"], "violations": result["violations"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
