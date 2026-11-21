#!/usr/bin/env python3
"""走**项目自身的规划链**生成书纲与卷纲(不再手搓大纲文档)。

链路(全是生产代码,不绕过):
    建项目 → OutlineGenerator.generate_book_outline(staged=True)
           → OutlineGenerator.generate_volume_outline(staged=True)
           → 落 Outline(level=book / volume,is_confirmed=1)+ Volume 行

为什么用 staged:项目默认三段式生成书纲(骨架 → 世界观 → 人物),
每段一次调用 —— 这是项目为"避免长输出质量断崖"做的设计,也正是我们需要的。

外审意见(用户给的矫正建议)作为**输入**传进去:
  - 书纲:user_input(创意要求)里写清题材/口味/主角底色/规模
  - 卷纲:user_notes 里写清该卷必须遵守的设定(血脉用法、学院逻辑、伏笔指向)

用法::

    QUALITY_EVAL_API_KEY=<key> python -m scripts.ops.plan_novel \\
        --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \\
        --stage all --target-words 1000000
"""

from __future__ import annotations

import argparse
import json

# ---------------------------------------------------------------------------
# 创作输入(书纲) —— 这是项目的 user_input,不是文档
# ---------------------------------------------------------------------------
CREATIVE_INPUT = """\
题材:西式玄幻 + 北欧神话 + 架空现代世界。故事背景的文明程度等同现代(地铁、外卖、直播、学区房都在),
但世界底下压着北欧神话的旧事。规模是百万字长篇,语气要少年感,暗面可以黑,但不脏、不俗。

主角基调:草根、底层、没什么用,参考《龙族》路明非那种"废"——
自我评价极低、遇事先想"跟我没关系"、嘴碎自嘲、被人夸会不知所措;
心里有个人(初中同桌,后来考去市重点,他三年没敢发一条消息);
靠廉价娱乐止血(一款快停服的老网游、泡面、五块钱两串的炸串);没野心,只想"活得像个人";
善良但不外露,会在没人看见的地方做对的事;真正要命的时候他不退,而且他自己也说不清为什么回来。

在这之上再加一层:**腹黑**。认怂是战术,装傻是情报,记账是习惯。
他从不主动挑事,只递话、给机会,让别人替他动手;被欺负了不还手,还笑,但在心里记账;
关键时候下手最准,因为他算准了才动。底线三条:不害无辜、不卖朋友、不拿母亲的事做筹码。

世界设定要点(必须在书纲里体现,但正文不许解释):
- 组织「灰袍」:看守九界裂缝的守门人,对外挂着"白蜡树校友会"的牌子;
- 「白蜡树学院」:灰袍办的民办寄宿制高中,学费全免、包吃住,招生简章写"面向成绩不理想但身体条件良好的学生";
  本质是筛"有血脉底子、又没退路"的底层孩子当耗材——免费包吃住是诱饵,准军事化是岗前培训;
- 血脉分档:阿萨血(雷/战/智/死)> 华纳血 > 巨人血 > 混血 > 不入谱的「洛基血」(档案规定"见即报");
  做"血脉贵族"不做"金钱贵族":阿萨血学生的特权来自血脉与灰袍身份,不是家里有钱;
- 力量三条路都带代价:刻符会拖走性格、真名会让别人知道你的名字、契约会被死人住进来;
- 冲突:巨人后裔不认《灰约》,他们要"解扣"——把世界树背面的伤撕开,让诸神黄昏接着打完。

主角的血脉是洛基血:不入谱、被规定"见即报"。他的能力不是战力,是"规则"——
伪造符纹(让人误判他的档,代价是刻的符会认不出自己)、篡改真名(替人挡档案、甩黑锅,代价是自己名字多一道裂痕)、
换契约主体(把代价转走,代价是慢慢分不清自己是谁)。他最大的优势是"没人把我当威胁,所以我能当着所有人的面算账"。

第一卷从一封招生简章开始:他复读一年差本科线十七分,母亲做保洁月入两千二,他不想再让母亲掏复读费;
简章是他在替母亲领洗衣粉时、在垃圾站踢开一摞旧报纸看到的。他跟母亲说找到个地方实习、管吃住,母亲信了,
往他兜里塞了六十块。他去了城郊的白蜡树中学。\
"""

# 卷纲的补充说明(外审意见落在这里,而不是我去改输出)
VOLUME_NOTES = """\
第 1 卷必须遵守的设定与分寸(评审意见,优先级高于自由发挥):

1. 主角的能力是"规则挂",不是"输出挂"——他不靠觉醒爆种打脸,靠"没人把他当威胁"。
   三条力量的专属用法要在本卷显出雏形:伪造符纹、篡改真名、换契约主体;每次用都要付代价。
2. 学院不是学校,是筛耗材的地方:"免费包吃住"是诱饵,"准军事化"是岗前培训。
   本卷要让读者逐渐意识到"他以为抓住救命稻草,其实是上了战场",但**不要写成反派宣言**。
3. 做血脉贵族、不做金钱贵族:阿萨血学生的优越来自血脉与灰袍身份,不是家里有钱。
4. 三处伏笔拧成一股,全指向他母亲:招生简章不是他捡的(是有人放在他一定能看到的地方);
   母亲常年擦的那块抹布上的图案,与后山"解扣"试验里的东西同源;灰袍内部压下他"见即报"报告的人,
   是冲着他母亲的面子。本卷只埋,不揭。
5. 少年感靠"日常与超自然的碰撞":翻窗打游戏撞见试验,第一反应是"我游戏还没挂机";
   食堂难吃,宿舍煮泡面被抓,他把锅甩给隔壁班的混子。天大的事也挡不住一口热泡面。
6. 中心人物:韩青(阿萨血·死支,冷是"见惯生死"的疏离,不是大小姐傲娇)、徐浪(混子室友,跟班,能挡事但要防)、
   魏教官(独臂,教刻符,在他手背上多看一眼却不说话)。
"""


async def main_async(args) -> int:
    from scripts.quality_eval.continuity_probe import _prepare_env
    _prepare_env(args.database_url)

    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.project import Outline, Project, Volume
    from app.services.outline_generator import OutlineGenerator, compute_scale

    scale = compute_scale(
        args.target_words,
        chapter_words=args.chapter_words,
        chapters_per_volume_target=args.chapters_per_volume,
    )
    print("规模:", json.dumps(scale, ensure_ascii=False))

    async with async_session_factory() as db:
        project = None
        if args.project_id:
            project = await db.get(Project, args.project_id)
        if project is None:
            project = Project(
                title=args.title,
                genre="西式玄幻 · 北欧神话 · 现代架空",
                premise=CREATIVE_INPUT[:500],
                target_word_count=args.target_words,
                settings_json={"chapter_words": args.chapter_words,
                               "chapters_per_volume": args.chapters_per_volume},
            )
            db.add(project)
            await db.commit()
            print("已建项目:", project.id)

        project_id = str(project.id)

        # ---- 1) 书纲(生产链:staged 三段式) ----
        existing_book = (await db.execute(
            select(Outline).where(Outline.project_id == project_id, Outline.level == "book")
            .order_by(Outline.version.desc()).limit(1))).scalars().first()
        if existing_book is None or args.stage in ("book", "all", "force"):
            gen = OutlineGenerator(project_id)
            print("生成书纲中(三段式,可能要几分钟)……")
            book = await gen.generate_book_outline(
                user_input=CREATIVE_INPUT, staged=True, scale=scale)
            if isinstance(book, dict):
                content = dict(book)
                content.setdefault("raw_text", CREATIVE_INPUT)
                db.add(Outline(project_id=project_id, level="book", parent_id=None,
                               content_json=content, is_confirmed=1))
                await db.commit()
                vols = content.get("volumes") or content.get("volume_list") or []
                print(f"书纲已落库:顶层键={list(content.keys())[:10]} 卷数={len(vols)}")
            else:
                print("书纲返回非 dict:", type(book))
        else:
            book = existing_book.content_json
            print("已有书纲,跳过")

        if args.stage not in ("volume", "all", "force"):
            return 0

        # ---- 2) 卷纲(第一卷) ----
        book_row = (await db.execute(
            select(Outline).where(Outline.project_id == project_id, Outline.level == "book")
            .order_by(Outline.version.desc()).limit(1))).scalars().first()
        if book_row is None:
            print("没有书纲,无法生成卷纲")
            return 1

        existing_vol = (await db.execute(
            select(Outline).where(Outline.project_id == project_id, Outline.level == "volume")
            .order_by(Outline.version.desc()).limit(1))).scalars().first()
        if existing_vol is not None and args.stage not in ("force",):
            print("已有卷纲,跳过(要重生成加 --stage force)")
            return 0

        gen = OutlineGenerator(project_id)
        print("生成第一卷卷纲中(分批,可能要十几分钟)……")
        vol = await gen.generate_volume_outline(
            book_outline=book_row.content_json, volume_idx=1, user_notes=VOLUME_NOTES, staged=True)
        if not isinstance(vol, dict):
            print("卷纲返回非 dict:", type(vol))
            return 1
        db.add(Outline(project_id=project_id, level="volume", parent_id=str(book_row.id),
                       content_json=vol, is_confirmed=1))
        volume = Volume(project_id=project_id, volume_idx=1,
                        title=vol.get("title") or "第一卷",
                        summary=(vol.get("summary") or "")[:2000],
                        target_word_count=args.target_words // max(1, scale["n_volumes"]))
        db.add(volume)
        await db.commit()
        chapters = vol.get("batch") or vol.get("chapters") or vol.get("chapter_summaries") or []
        print(f"卷纲已落库:章数={len(chapters)} 卷名={vol.get('title')}")
        if chapters:
            print("前 3 章:", json.dumps(chapters[:3], ensure_ascii=False)[:600])
    return 0


def main(argv=None) -> int:
    import asyncio
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--stage", default="all", choices=["book", "volume", "all", "force"])
    ap.add_argument("--title", default="白蜡树")
    ap.add_argument("--target-words", type=int, default=1_000_000)
    ap.add_argument("--chapter-words", type=int, default=2000)
    ap.add_argument("--chapters-per-volume", type=int, default=100)
    ap.add_argument("--project-id", default=None)
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
