#!/usr/bin/env python3
"""创意简报生成器:把创作请求交给指定模型,产出候选书名 / 立意 / 展开稿。

为什么单独做这个而不是直接在会话里写:规划要的是**创作**产出,不是工程文档。
把"禁用套路清单 + 具体质感要求"固化成 prompt,让模型写,人只负责筛。

用法::

    CREATIVE_API_KEY=<网关 key> python -m scripts.ops.creative_brief \
        --stage names --model deepseek-v4-flash --out /tmp/names.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

GATEWAY = os.environ.get("CREATIVE_GATEWAY", "http://127.0.0.1:8317/v1/chat/completions")

BRIEF = """你在为一个中文长篇(规模百万字)做创作简报。

已知条件:
- 类型:西式玄幻 + 北欧神话元素 + 现代社会,架空世界
- 主角:草根、底层、没什么用、被所有人看轻(处境参考《龙族》路明非:城市里打工、嘴碎、自卑、被当成废物),但**不要**照抄"隐藏血统/真身觉醒"那一套
- 读者:中文网络小说读者,但作者想写得比流水线产品有味道

**以下套路一律不许出现(用了就重写)**:
- 诸神黄昏之后神格碎成碎片沉入人间 / 九界重叠 / 世界树根系
- 命运登记、命运档案、预言系统、"空白行""空位""被选中的人"
- 废柴觉醒血脉、预言之子、唯一能拯救世界的人
- 主角牺牲自己、被所有人遗忘的"反高潮"结尾
- 神秘组织挂着"文化遗产基金会/协会"的壳
- 机构名叫"XX局""XX理事会""XX委员会"
- 用"秩序与自由的路线之争"这种抽象对立当主题

要求:
- 世界观必须**具体到有生活质感**:写得出具体的行话、职业、价格、天气、城市里的行当。不要泛泛而谈。
- 北欧元素要**用得偏、用得冷**:可以用诺恩的线、瓦尔基里的抉择、被忘掉的神、契约的细则、地方迷信、丧葬习俗、借贷关系。不要雷神奥丁洛基三件套。
- 每个候选要有**气味**:读起来像真有人在那样活着,不是设定说明书。

输出格式(严格照此,不要任何解释、不要客套):
=== 书名 10 个 ===
1. 短名(2-4 字,要有余味,不要故弄玄虚)
...
10. 长名(8-16 字,要有奇异的画面感)
每条后面用括号写一句不超过 10 字的副题。

=== 立意 6 个 ===
每条 120-200 字,必须交代清楚:主角靠什么活着、他遇到的第一件怪事具体是什么、这件事和他兜里那点钱/他那份工有什么关系。
不要写主题升华,只写具体的人与具体的麻烦。
"""

STAGES = {
    "names": BRIEF,
    "premise": BRIEF + """
在上一轮的基础上,只输出「立意 12 个」(不要书名)。每个 150-250 字,要求:
- 主角的**职业**必须具体(送外卖、修棺材、替人哭丧、给死人化妆、夜间代驾、殡葬乐队小号手、旧货翻新、给老人跑腿……任选或另想)
- 第一件怪事必须是**能看见的**(不是"他感到不对劲",而是"他数错了一具尸体""镜子里的债主比他还急")
- 他**为钱**卷进去(这才是底层动力),而不是为了使命
- 每条结尾留一个具体的疑问,不要解释世界观
""",
    "fantasy": BRIEF + """
在上一轮基础上,只输出「西式玄幻方向的立意 10 个」(不要书名)。要求:
- 必须有一个**可操作的力量体系**:读者能立刻想象怎么用、代价是什么、谁在管。
  但要新 —— 不要魔力/斗气/等级制/契约兽/系统面板/魔法学院/公会任务,这些全是老面孔。
- 力量必须与主角的**底层生计**绑死:他靠它吃饭,或者它是他欠账的原因。
- 北欧元素要**冷**:诺恩纺的线、瓦尔基里挑人时的规矩、被忘掉的神、献祭怎么记账、
  地名底下埋的旧名、担保与抵押。不要雷神奥丁洛基。
- 每条 150-250 字,写清楚:主角干什么活、力量怎么到他手里、第一件看得见的怪事、
  他为钱还是为命卷进去。结尾留一个具体疑问,不要解释世界观,不要"他其实是XX之子"。
""",
    "names2": BRIEF + """
只输出「书名 20 个」,不要立意。分两组:
A 组 10 个:2-4 字短名,要有余味(参考:《活着》《皮带扣》《铁皮》这种质感)
B 组 10 个:5-12 字,要有一个具体的画面或行话(参考:《收尸人的假》《给死人擦脸的第七夜》)
不要 XX的第七次XX 这种两头不靠的结构。每条后面括号写不超过 10 字副题。
""",
}


def call(prompt: str, *, model: str, temperature: float, max_tokens: int) -> str:
    key = os.environ.get("CREATIVE_API_KEY") or os.environ.get("QUALITY_EVAL_API_KEY", "")
    if not key:
        raise SystemExit("缺少 CREATIVE_API_KEY / QUALITY_EVAL_API_KEY")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        GATEWAY, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    print(f"[{model}] tokens: prompt={usage.get('prompt_tokens')} "
          f"completion={usage.get('completion_tokens')}", file=sys.stderr)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", default="names", choices=sorted(STAGES))
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--temperature", type=float, default=1.1)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    text = call(STAGES[args.stage], model=args.model,
                temperature=args.temperature, max_tokens=args.max_tokens)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"写入 {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
