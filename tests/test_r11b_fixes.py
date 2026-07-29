#!/usr/bin/env python3
"""r11b mock tests — no real Datayes, no real LLM, no real company reports."""

from __future__ import annotations
import sys, os, json, re, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# ── Test title conclusion pipeline ──

class TestTitleConclusion(unittest.TestCase):
    """Title must never output '标题生成失败'."""

    def test_01_title_conclusion_normal(self):
        """title_conclusion normal return via sanitize."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion, _valid_title_conclusion
        result = _sanitize_title_conclusion("手机高端化改善盈利，光互连打开第二曲线", "舜宇光学科技", "02382.HK", "港股")
        self.assertTrue(result, f"Expected valid title, got empty")
        self.assertTrue(_valid_title_conclusion(result))

    def test_02_title_conclusion_missing(self):
        """title_conclusion missing → should return empty."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion
        result = _sanitize_title_conclusion("", "舜宇光学科技", "02382.HK", "港股")
        self.assertEqual(result, "")

    def test_03_title_conclusion_with_error_text(self):
        """title_conclusion containing '标题生成失败' → rejected."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion
        result = _sanitize_title_conclusion("标题生成失败", "舜宇光学科技", "02382.HK", "港股")
        self.assertEqual(result, "")

    def test_04_title_conclusion_with_forbidden(self):
        """title_conclusion containing forbidden terms → rejected."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion
        result = _sanitize_title_conclusion("近期研报：公司业绩持续增长", "舜宇光学科技", "02382.HK", "港股")
        self.assertEqual(result, "")

    def test_05_title_conclusion_too_short(self):
        """title_conclusion with <10 zh chars → rejected."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion, _valid_title_conclusion
        self.assertFalse(_valid_title_conclusion("增长"))

    def test_06_title_conclusion_too_long(self):
        """title_conclusion with >30 zh chars → rejected."""
        from hk_us_report_writer_v124 import _sanitize_title_conclusion, _valid_title_conclusion
        self.assertFalse(_valid_title_conclusion("这是一个非常非常非常非常非常非常非常非常非常非常长的标题结论"))

    def test_07_title_conclusion_no_judgment(self):
        """title_conclusion without judgment terms → rejected."""
        from hk_us_report_writer_v124 import _valid_title_conclusion
        self.assertFalse(_valid_title_conclusion("公司发布新战略"))

    def test_08_fallback_from_content(self):
        """Build deterministic fallback from §1+§3 content."""
        from hk_us_report_writer_v124 import _build_deterministic_fallback_title
        texts = {
            "s12": "- **手机高端化**：高端摄像头占比提升，推动ASP改善[1]\n- **汽车光学**：车载镜头放量[2]\n## 2 近况跟踪",
            "s34": "- **光互连**：组件进入客户验证[1]\n- **AI眼镜**：千亿TAM[3]",
        }
        result = _build_deterministic_fallback_title(texts, "舜宇光学科技", "02382.HK")
        self.assertTrue(result, f"Fallback should produce non-empty title, got: '{result}'")
        self.assertIn("，", result)

    def test_09_build_report_title_never_fails(self):
        """_build_report_title must never output '标题生成失败'."""
        from hk_us_report_writer_v124 import _build_report_title
        result = _build_report_title("舜宇光学科技", "02382.HK", "港股", "")
        self.assertNotIn("标题生成失败", result)
        result2 = _build_report_title("舜宇光学科技", "02382.HK", "港股", "bad")
        self.assertNotIn("标题生成失败", result2)


# ── Test §11.2 target price median ──

class TestTargetPriceStats(unittest.TestCase):
    """Even-count median must be correct."""

    def test_10_shunyu_6_targets(self):
        """[57, 62, 69, 86.9, 88, 94] → median = 77.95."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([57, 62, 69, 86.9, 88, 94])
        self.assertEqual(stats["low"], 57)
        self.assertEqual(stats["median"], 77.95)
        self.assertEqual(stats["high"], 94)
        self.assertEqual(stats["count"], 6)

    def test_11_odd_count(self):
        """[10, 20, 30] → median = 20."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([10, 20, 30])
        self.assertEqual(stats["median"], 20)

    def test_12_even_count(self):
        """[10, 20, 30, 40] → median = 25."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([10, 20, 30, 40])
        self.assertEqual(stats["median"], 25.0)

    def test_13_with_none(self):
        """[10, None, 20, 30] → median = 20 (ignores None)."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([10, None, 20, 30])
        self.assertEqual(stats["median"], 20)

    def test_14_with_string_numbers(self):
        """['10', 20, '30'] → median = 20."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats(["10", 20, "30"])
        self.assertEqual(stats["median"], 20)

    def test_15_all_invalid(self):
        """[None, None] → count = 0."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([None, None])
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["median"])

    def test_16_single_value(self):
        """[42] → median = 42."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([42])
        self.assertEqual(stats["median"], 42)

    def test_17_max_values(self):
        """[999.99, 100, 50.5] → correctly sorted."""
        from hk_us_report_writer_v124 import calculate_target_price_stats
        stats = calculate_target_price_stats([999.99, 100, 50.5])
        self.assertEqual(stats["low"], 50.5)
        self.assertEqual(stats["median"], 100)
        self.assertEqual(stats["high"], 999.99)


# ── Test §2 punctuation cleanup ──

class TestPunctuationCleanup(unittest.TestCase):
    """Repeated punctuation in §2 must be cleaned."""

    def test_18_clean_repeated(self):
        from hk_us_report_writer_v124 import _clean_repeated_punctuation
        self.assertNotIn("。；", _clean_repeated_punctuation("公司业绩增长。；市场关注"))
        self.assertNotIn("；。", _clean_repeated_punctuation("业绩改善；。估值合理"))
        self.assertNotIn("。。", _clean_repeated_punctuation("高增长。。"))

    def test_19_clean_noop(self):
        from hk_us_report_writer_v124 import _clean_repeated_punctuation
        text = "公司业绩增长；市场关注。估值合理。"
        self.assertEqual(_clean_repeated_punctuation(text), text)


# ── Test banned phrases ──

class TestBannedPhrases(unittest.TestCase):
    """Banned phrases must not appear in key assumptions."""

    def test_20_banned_phrases_list(self):
        from hk_us_report_writer_v124 import _MARKET_DEBATE_BANNED_PHRASES
        self.assertIn("收入增长与需求兑现", _MARKET_DEBATE_BANNED_PHRASES)
        self.assertIn("产品迭代与客户转化", _MARKET_DEBATE_BANNED_PHRASES)

    def test_21_clean_assumption_no_generic(self):
        """_clean_assumption_text must not output generic phrases."""
        from hk_us_report_writer_v124 import _clean_assumption_text
        # Mock a report dict without concrete assumptions
        rd = {"articleTitle": "舜宇光学科技2026年投资者日要点", "textAbstract": "管理层强调收入增长与需求兑现"}
        result = _clean_assumption_text(rd, "舜宇光学科技", "02382.HK")
        # Should not output the generic phrase directly
        banned = ["收入增长与需求兑现", "产品迭代与客户转化"]
        for phrase in banned:
            self.assertNotIn(phrase, result, f"Should not output banned phrase: {phrase}")


# ── Test §10 JSON schema validation ──

class TestSection10Schema(unittest.TestCase):
    """§10 JSON validation must enforce correct structure."""

    def test_22_section10_valid_payload(self):
        from hk_us_report_writer_v124 import _validate_render_section_10
        ref_map = {1: {"id": "a1"}, 2: {"id": "a2"}, 3: {"id": "a3"}, 4: {"id": "a4"}}
        payload = {
            "market_debates": [
                {"bull_view": "AI算力需求驱动光学组件出货增长", "evidence": "2027年光互连小批量出货[1]", "bear_view": "光互连商业化慢于预期", "validation": "客户认证进展[1]", "source_refs": [1]},
                {"bull_view": "汽车光学渗透率持续提升", "evidence": "ADAS渗透率提升至60%[2]", "bear_view": "汽车行业增速放缓拖累业务", "validation": "车载镜头出货增速[2]", "source_refs": [2]},
                {"bull_view": "手机高端化改善ASP", "evidence": "高端摄像头占比升至55%[3]", "bear_view": "安卓需求疲软抵消增长", "validation": "手机业务ASP季度变动[3]", "source_refs": [3]},
                {"bull_view": "新终端打开千亿空间", "evidence": "AI眼镜2027年量产[4]参考[4]", "bear_view": "XR需求不明", "validation": "XR客户定点[4]", "source_refs": [4]},
            ]
        }
        rendered, issues = _validate_render_section_10(payload, ref_map)
        self.assertTrue(rendered, f"Issues: {issues}")
        self.assertIn("多头观点", rendered)
        self.assertIn("空头观点", rendered)

    def test_23_section10_too_few_rows(self):
        from hk_us_report_writer_v124 import _validate_render_section_10
        ref_map = {1: {"id": "a1"}}
        payload = {"market_debates": [{"bull_view": "a", "evidence": "b[1]", "bear_view": "c", "validation": "d[1]", "source_refs": [1]}]}
        rendered, issues = _validate_render_section_10(payload, ref_map)
        self.assertFalse(rendered)
        self.assertTrue(any("rows_count" in i for i in issues))

    def test_24_section10_missing_source_refs(self):
        from hk_us_report_writer_v124 import _validate_render_section_10
        ref_map = {1: {"id": "a1"}, 2: {"id": "a2"}, 3: {"id": "a3"}, 4: {"id": "a4"}}
        payload = {"market_debates": [
            {"bull_view": "a1", "evidence": "e1[1]", "bear_view": "c1", "validation": "d1[1]", "source_refs": [1]},
            {"bull_view": "a2", "evidence": "e2[2]", "bear_view": "c2", "validation": "d2[2]", "source_refs": [2]},
            {"bull_view": "a3", "evidence": "e3[3]", "bear_view": "c3", "validation": "d3[3]", "source_refs": [3]},
            {"bull_view": "a4", "evidence": "e4[4]", "bear_view": "c4", "validation": "d4[4]", "source_refs": []},  # missing refs
        ]}
        rendered, _ = _validate_render_section_10(payload, ref_map)
        self.assertFalse(rendered, "Should reject row without source_refs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
