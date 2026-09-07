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

    def test_high_valuation_rejects_disguised_per_share_price(self):
        key_data = {
            "valuation": {"items": {
                "\u5e02\u76c8\u7387PE": {"val": -1},
                "\u5e02\u51c0\u7387PB": {"val": 56.6, "avg": 8.0},
            }},
            "consensus_forecasts": [],
        }
        section = "\u4e2d\u6027\u60c5\u666f\u6bcf\u80a1\u4ef7\u503c\u4e3a18.90\u5143\u3002\u4f20\u7edfPE\u6cd5\u5931\u6548\u3002"
        self.assertTrue(writer._scenario_target_price_errors(section, key_data))

    def test_provenance_cleanup_keeps_valid_clause_on_same_line(self):
        key_data = {
            "_raw_data": {"financial": {"data": {"dataRow": []}}},
            "reports": [{"id": "r1", "text": "\u6e20\u9053\u6548\u7387\u6539\u5584\u6709\u52a9\u4e8e\u76c8\u5229\u3002"}],
            "ref_map": {
                "fdmtNew": {"n": 1, "type": "\u7ed3\u6784\u5316\u6570\u636e", "api_name": "fdmtNew"},
                "report_r1": {"n": 2, "type": "\u7814\u62a5", "id": "r1"},
            },
        }
        md = (
            "## 2 \u6838\u5fc3\u6295\u8d44\u903b\u8f91\n\n"
            "\u8305\u53f0\u9152\u9500\u91cf4.6\u4e07\u5428[1]\uff1b\u6e20\u9053\u6548\u7387\u6539\u5584\u6709\u52a9\u4e8e\u76c8\u5229[2]\u3002\n\n"
            "## \u53c2\u8003\u8d44\u6599\n"
            "[1]Datayes\u7ed3\u6784\u5316\u63a5\u53e3 | API\uff1afdmtNew\n"
            "[2]Datayes\u7814\u62a5 | ID\uff1ar1\n"
        )
        result, removed = writer._drop_unverifiable_numeric_lines(md, key_data)
        self.assertNotIn("\u9500\u91cf4.6\u4e07\u5428", result)
        self.assertIn("\u6e20\u9053\u6548\u7387\u6539\u5584\u6709\u52a9\u4e8e\u76c8\u5229[2]", result)
        self.assertEqual(len(removed), 1)

    def test_financial_mapping_uses_gross_margin_not_operating_margin(self):
        data = {"financial": {"data": {
            "titleBar": [{"year": 2025, "reportPeriodType": "A"}],
            "dataRow": [
                {"code": "grossMARgin", "data": [91.18]},
                {"code": "operateProfitRatio", "data": [66.73]},
            ],
        }}}
        result = writer.extract_financial(data)
        self.assertEqual(result[2025]["grossMargin"], 91.18)
    def test_inline_numbered_qa_is_split_and_each_answer_gets_its_own_ref(self):
        block = (
            "【2026-07-28 电话会议（机构调研）】[11]\n"
            "Q1：第一问的详细背景是什么？A1：第一答包含足够的经营信息和背景说明。"
            "Q2：第二问的详细背景是什么？A2：第二答包含足够的经营信息和背景说明。"
            "Q3：第三问的详细背景是什么？A3：第三答包含足够的经营信息和背景说明。"
        )
        candidates = writer._extract_qa_candidates([block])
        self.assertEqual(
            [item["q"] for item in candidates],
            ["第一问的详细背景是什么？", "第二问的详细背景是什么？", "第三问的详细背景是什么？"],
        )
        rendered = writer._format_qa_markdown(candidates, [])
        self.assertEqual(rendered.count("**Q：**"), 3)
        self.assertEqual(rendered.count("[11]"), 3)
        for match in __import__("re").finditer(r"\*\*Q：\*\*", rendered):
            self.assertRegex(rendered[match.start():match.start() + 500], r"\[11\]")

    def test_qa_labels_meeting_note_as_non_guidance(self):
        rendered = writer._format_qa_markdown([{"q": "question", "a": "answer"}], [])
        self.assertIn("\u8c03\u7814\u7eaa\u8981\u89c2\u70b9\uff0c\u975e\u516c\u53f8\u6307\u5f15", rendered)

    def test_structure_gate_rejects_heading_inside_table_cell(self):
        errors = writer._markdown_structure_errors("| metric | ## 2 malformed heading |\n|:--|:--|")
        self.assertTrue(any("\u8868\u683c\u5355\u5143\u683c\u5185\u5305\u542b\u6807\u9898" in error for error in errors))

    def test_financial_table_uses_full_metric_names(self):
        financial = {"years": [2025, 2024, 2023], 2025: {"tRevenue": 100, "NPAttrP": 20, "grossMargin": 40,
                            "netMargin": 20, "ROEW": 15, "operCashFlow": 12,
                            "totalAssets": 200, "liabRatio": 30, "basicEPS": 1.2}}
        table = writer.gen_financial_table(financial)
        self.assertIn("\u9500\u552e\u51c0\u5229\u7387", table)
        self.assertIn("\u51c0\u8d44\u4ea7\u6536\u76ca\u7387-\u52a0\u6743\u5e73\u5747", table)
        self.assertIn("\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d", table)
    def test_global_prompt_requires_full_financial_metric_names(self):
        self.assertIn("\u4e0d\u5f97\u5355\u72ec\u7b80\u5199\u201c\u8425\u6536\u201d", writer.SYSTEM_PROMPT)
        self.assertIn("`tRevenue` \u4e3a\u8425\u4e1a\u603b\u6536\u5165", writer.SYSTEM_PROMPT)
        self.assertIn("`revenue` \u4e3a\u8425\u4e1a\u6536\u5165", writer.SYSTEM_PROMPT)
    def test_peer_table_allows_dash_when_peer_progress_has_no_material(self):
        peers = [{"code": "000858", "current_name": "五粮液"}, {"code": "000568", "current_name": "泸州老窖"}]
        table = """| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 市值 | 商业模式 | 目标客户群体 | 核心产品 |
|:---------|:-----|:-----|:---------|:---------|:-------------|:-----|:---------|:-------------|:---------|
| —（基准） | 贵州茅台（600519） | A股 | 白酒 | — | — | — | — | — | — |
| 直接竞争 | 五粮液（000858） | A股 | 白酒 | — | 经营进展[4] | — | — | — | — |
| 直接竞争 | 泸州老窖（000568） | A股 | 白酒 | — | — | — | — | — | — |"""
        self.assertEqual(writer._validate_peer_table(table, "贵州茅台", "600519", peers, {"000858": [4]}), table)
        self.assertIn("| 直接竞争 | 五粮液（000858） | A股 | 白酒 | — | — |", writer._validate_peer_table(table.replace("[4]", "[9]"), "贵州茅台", "600519", peers, {"000858": [4]}))

    def test_peer_material_reference_resolves_to_original_source(self):
        data = {"peer_materials": [{"peer_name": "五粮液", "peer_code": "000858", "id": "m1", "title": "五粮液进展", "text": "五粮液渠道反馈"}]}
        refs = writer.build_ref_map(data)
        peer_ref = next(item for item in refs.values() if item["type"] == "同业材料")
        evidence = writer._build_reference_evidence({"_raw_data": data, "ref_map": refs}, writer.refs_to_markdown(refs))
        self.assertIn("五粮液渠道反馈", evidence[peer_ref["n"]]["text"])
        self.assertEqual(evidence[peer_ref["n"]]["api"], "getMaterialsV2")
if __name__ == "__main__":
    unittest.main()