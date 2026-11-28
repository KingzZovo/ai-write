"""章内同源段落折叠:生成侧的原段重复必须被清掉。

## 证据(2026-11-28,实测《白蜡树下》)

- 500 章里 **174 章**含逐字重复段落;可去掉 **93,551 字(6.6%)**。
- 最严重:卷 3 第 83 章 152 段重复 / 10,678 字(该章 12,617 字,叠完回到 ~2,300);
  卷 4 第 22 章 52 段里 18 段同源,开头 24 字完全相同。
- 客观相关性:章内近重复段落占比 vs `narrative_pacing` **r = -0.442**
  (强于字数的 -0.379)。按高频片段占比四分位,pacing 7.54 → 7.24 → 7.04 → **6.53**。
- 重复类 issue **天生没有唯一锚点**(重复的东西本来就多处出现)→ `quote` 为空
  → 局部修订 0 span → 整章进复核。这就是那 62% 空引文的来源。

## 与文风契约的边界(不许放宽)

`narrative_quality_gates` 明写"不按词频清零"、"不为避重换成失真的近义词" ——
本折叠**不违反**它们:删的是成段重复,首次出现**原样保留、措辞一字不改**,
比较范围**只在章内**,不碰跨章词频、不做同义替换。
"""
import pytest

from app.services.prose_sanitizer import collapse_duplicate_paragraphs, sanitize_prose

BASE = '不是刻刀,是指腹。他感觉到自己的指腹底下渗出一道纹路,落在裂缝边缘的石面上,像一道旧痕。'
# 只差一两个字 —— 实测同指纹段落对的中位相似度就是 1.000,这类必须折掉。
NEAR = '不是刻刀,是指腹。他感觉到自己的指腹底下渗出一道纹路,落在裂缝边缘的石面上,像一道新痕。'
FILLER = '登记本摊在桌上,纸页被灯烤得发脆,他数了数名字,三个字,笔画密。'


def _run(text):
    return collapse_duplicate_paragraphs(text)


def test_exact_duplicate_is_collapsed_and_first_kept():
    out, n = _run(f'{FILLER}\n\n{BASE}\n\n中间另一段正常叙述内容,长度足够参与判断。\n\n{BASE}\n\n收尾。')
    assert n == 1
    assert out.count(BASE) == 1
    assert FILLER in out


def test_near_identical_same_prefix_is_collapsed():
    out, n = _run(f'{BASE}\n\n中间另一段正常叙述内容,长度足够参与判断。\n\n{NEAR}')
    assert n == 1
    assert BASE in out and NEAR not in out


def test_same_prefix_but_divergent_is_kept():
    """同开头不等于同源 —— 相似度不够就必须留着,否则会误删平行结构。"""
    divergent = '不是刻刀,是指腹。可他这次什么都没有摸到,石灰的味道从缝里冒上来。'
    out, n = _run(f'{BASE}\n\n中间另一段正常叙述内容,长度足够参与判断。\n\n{divergent}')
    assert n == 0
    assert divergent in out


def test_short_paragraphs_are_never_collapsed():
    """对白短句天然会重复,不能碰。"""
    out, n = _run('"走吧。"\n\n他没动。\n\n"走吧。"\n\n她还是没动,只是把灯芯挑低了一点。')
    assert n == 0


def test_unchanged_text_is_returned_verbatim():
    text = f'{FILLER}\n\n另一段完全不同的话,句子结构也不一样。'
    out, n = _run(text)
    assert (out, n) == (text, 0)


def test_triple_repeat_collapses_to_one():
    out, n = _run(f'{BASE}\n\n{BASE}\n\n{BASE}')
    assert n == 2
    assert out.count(BASE) == 1


def test_is_idempotent():
    first, n1 = _run(f'{BASE}\n\n中间另一段正常叙述内容,长度足够参与判断。\n\n{NEAR}')
    assert n1 == 1
    second, n2 = _run(first)
    assert n2 == 0 and second == first


def test_sanitize_prose_reports_the_collapse():
    """折叠必须计入 hits —— 否则会被 `if not hits: return text` 当成无变更丢掉。"""
    out, hits = sanitize_prose(f'{BASE}\n\n中间另一段正常叙述内容,长度足够参与判断。\n\n{NEAR}')
    assert any(h.startswith('duplicate_paragraphs:') for h in hits), hits
    assert NEAR not in out


@pytest.mark.parametrize('sep', ['\n\n', '\n\n\n'])
def test_multi_newline_separators_do_not_leave_holes(sep):
    out, n = _run(f'{FILLER}{sep}{BASE}{sep}中间另一段正常叙述内容,长度足够参与判断。{sep}{BASE}')
    assert n == 1
    assert '\n\n\n\n' not in out
