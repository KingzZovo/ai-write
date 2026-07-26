"""Tests for the deterministic prose sanitizer (mechanism Fix2/Fix4 companion).

The sanitizer is the last-line guard that strips structural/meta-narrative
leakage ([CH-n]/[VOL-n] context delimiters, 第X章/上一章 references) from
generated prose right before persistence. These tests pin down:
- leaked fragments are stripped and reported as hits
- clean prose passes through byte-identical with zero hits
- ordinary words containing 章/卷 (章程/文章/试卷) are never touched
"""

from app.services.prose_sanitizer import sanitize_prose
from app.services.chinese_prose_mechanics_checker import (
    analyze_chinese_prose_mechanics,
)


class TestSanitizeProse:
    def test_clean_text_returned_unchanged_with_no_hits(self):
        text = "夜色沉了下来。她把灯拧亮，继续翻检桌上的旧信。"
        cleaned, hits = sanitize_prose(text)
        assert cleaned == text
        assert hits == []

    def test_empty_and_none_like_input(self):
        assert sanitize_prose("") == ("", [])

    def test_strips_context_delimiters(self):
        text = "他想起[CH-5]里那场雨。[VOL-1]的伏笔至此收束。"
        cleaned, hits = sanitize_prose(text)
        assert "[CH-5]" not in cleaned
        assert "[VOL-1]" not in cleaned
        assert "[CH-5]" in hits and "[VOL-1]" in hits

    def test_strips_numbered_chapter_refs(self):
        text = "正如第10章提到的那样，密码藏在钟楼里。"
        cleaned, hits = sanitize_prose(text)
        assert "第10章" not in cleaned
        assert any("第10章" in h for h in hits)

    def test_strips_chinese_numeral_chapter_refs(self):
        text = "第三章的暗格里还有一封信。"
        cleaned, hits = sanitize_prose(text)
        assert "第三章" not in cleaned
        assert hits

    def test_strips_prev_next_chapter_refs(self):
        text = "上一章的争执还没有平息，他已经收拾好了行李。"
        cleaned, hits = sanitize_prose(text)
        assert "上一章" not in cleaned
        assert hits

    def test_ordinary_words_with_zhang_juan_untouched(self):
        # 章程 / 文章 / 试卷 / 印章 must never be flagged.
        text = "他签了章程，把那篇文章折好，压在一摞试卷和一枚印章下面。"
        cleaned, hits = sanitize_prose(text)
        assert cleaned == text
        assert hits == []

    def test_preserves_newlines_and_paragraphs(self):
        text = "第一段。\n\n第二段提到[CH-3]的事。\n\n第三段。"
        cleaned, hits = sanitize_prose(text)
        assert cleaned.count("\n\n") == 2
        assert hits == ["[CH-3]"]


class TestMetaStructureLeakageGate:
    def test_clean_prose_meta_count_zero(self):
        text = "她推开门，屋里只有一盏昏黄的灯。桌上摊着没写完的信。"
        report = analyze_chinese_prose_mechanics(text)
        assert report.meta_structure_leakage_count == 0

    def test_meta_labels_counted_and_fail_gate(self):
        text = "第12章 复仇之始\n他冷笑一声。\n本章完。未完待续。"
        report = analyze_chinese_prose_mechanics(text)
        assert report.meta_structure_leakage_count >= 3
        assert report.passed is False

    def test_neutral_delimiters_counted_as_leakage(self):
        text = "[CH-7]的旧账，[VOL-2]才会清算。"
        report = analyze_chinese_prose_mechanics(text)
        assert report.meta_structure_leakage_count >= 2
