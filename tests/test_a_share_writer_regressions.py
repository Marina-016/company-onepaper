import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("writer", ROOT / "scripts" / "a_share_report_writer.py")
writer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(writer)


class AShareWriterRegressionTests(unittest.TestCase):
    def test_high_valuation_allows_no_target_price_statement(self):
        key_data = {
            "valuation": {"items": {
                "市盈率PE": {"val": -1},
                "市净率PB": {"val": 56.6, "avg": 8.0},
            }},
            "consensus_forecasts": [],
        }
        valid = "传统PE法失效：当前处于主题/预期定价阶段，不输出目标价。"
        invalid = "EPS＝0.42元 × PE＝45x ＝18.90元"
        self.assertEqual(writer._scenario_target_price_errors(valid, key_data), [])
        self.assertTrue(writer._scenario_target_price_errors(invalid, key_data))

    def test_scenario_cleanup_stays_inside_section_nine(self):
        md = """## 4 公司业务拆分

| 情景 | 核心变量 | 估值含义 |
|:--|:--|:--|
| 乐观 | A | B |

## 5 产销链分析

保留内容

## 9 一致预期、盈利预测与估值

### 9.4 情景推演

情景推演表
| 情景 | 核心变量 | 估值含义 |
|:--|:--|:--|
| 乐观 | X | Y |

## 10 风险提示

保留内容
"""
        result = writer._format_scenario_analysis(writer._fix_scenario_table_columns(md))
        self.assertIn("## 5 产销链分析\n\n保留内容", result)
        self.assertIn("## 10 风险提示\n\n保留内容", result)
        self.assertEqual(result.count("## "), md.count("## "))

    def test_empty_section_recovery_uses_heading_not_separator(self):
        md = "## 5 产销链分析\n\n## 6 公司财务数据分析\n\n正文\n"
        result = writer._replace_empty_h2_body(md, 5, "来源化 fallback[3]。")
        self.assertIn("## 5 产销链分析\n\n来源化 fallback[3]。", result)
        self.assertIn("## 6 公司财务数据分析\n\n正文", result)

    def test_risk_fallback_rejects_connectors_and_closes_bold_titles(self):
        original = writer._collect_a_share_risk_evidence
        writer._collect_a_share_risk_evidence = lambda *_args, **_kwargs: [
            {"risk_title": "需求不及预期", "text": "风险提示：需求不及预期", "ref": 1},
            {"risk_title": "价格竞争加剧", "text": "风险提示：价格竞争加剧", "ref": 2},
            {"risk_title": "产能投放延期", "text": "风险提示：产能投放延期", "ref": 3},
            {"risk_title": "因此", "text": "因此，需要持续跟踪", "ref": 4},
        ]
        try:
            result = writer._build_a_share_risk_fallback({"name": "测试公司"})
        finally:
            writer._collect_a_share_risk_evidence = original
        valid, issues = writer._validate_a_share_risk_body(result)
        self.assertTrue(valid, issues)
        self.assertNotIn("**因此**", result)
        self.assertEqual(result.count("**"), 6)

    def test_section_nine_rejects_heading_only_fragment(self):
        self.assertEqual(writer._normalize_section9_llm_fragment("## 9 一致预期、盈利预测与估值"), "")
        fragment = "## 9 一致预期、盈利预测与估值\n\n### 9.3 估值分析\n\nPB显著偏离行业均值[1]。"
        result = writer._normalize_section9_llm_fragment(fragment)
        self.assertNotIn("## 9 一致预期", result)
        self.assertIn("### 9.3", result)


    def test_empty_optional_section_nine_is_removed_after_cleanup(self):
        md = "## 8 行业分析\n\n正文\n\n## 9 一致预期、盈利预测与估值\n\n## 10 风险提示\n\n正文\n"
        result = writer._drop_empty_optional_section9(md)
        self.assertNotIn("## 9 一致预期", result)
        self.assertIn("## 8 行业分析", result)
        self.assertIn("## 10 风险提示", result)

    def test_maincomp_omits_calculated_residual_lines(self):
        data = {
            "main_comp": {"data": [
                {"endDate": "2025-12-31", "itemID": 0, "revenue": 10000000000},
                {"endDate": "2025-12-31", "itemID": 1, "itemIDSuperior": 0,
                 "itemName": "\u8305\u53f0\u9152", "revenue": 8500000000},
                {"endDate": "2025-12-31", "itemID": 2, "itemIDSuperior": 0,
                 "itemName": "\u5176\u4ed6\u5dee\u989d\u9879\u76ee(\u8ba1\u7b97)", "revenue": 1500000000},
            ]}
        }
        result = writer.extract_maincomp(data)
        self.assertEqual(list(result["segments"]), ["\u8305\u53f0\u9152"])
    def test_fallback_resolves_current_reference_after_renumbering(self):
        key_data = {
            "reports": [{"id": "r1", "title": "测试研报"}],
            "ref_map": {"report_r1": {"n": 8}},
            "mc": {"segments": {"主营业务": []}},
        }
        current_refs = "## 参考资料\n\n[3]Datayes研报 | 测试研报\n"
        title, ref_no = writer._first_report_citation(key_data, current_refs)
        self.assertEqual((title, ref_no), ("测试研报", 3))
        self.assertIn("[3]", writer._build_a_share_chain_fallback(key_data, "3"))

if __name__ == "__main__":
    unittest.main()