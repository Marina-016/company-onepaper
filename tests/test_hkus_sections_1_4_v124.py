import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_report_quality_v124 as checker  # noqa: E402
import hk_us_report_writer_v124 as writer  # noqa: E402


def _ref_map(n=6):
    return {i: {"id": str(i), "title": f"source {i}"} for i in range(1, n + 1)}


def _key_data():
    return {
        "company_name": "测试公司",
        "ticker": "00000.HK",
        "market": "HK",
        "target_matched_reports": [{"articleId": "a1"}, {"articleId": "a2"}],
        "recent_reports_text": "测试公司研报摘要。" * 80,
    }


def _payload():
    return {
        "title_conclusion": "主业修复驱动盈利质量改善",
        "section_1": {
            "key_points": [
                {"keyword": "利润率", "statement": "云业务收入改善驱动利润率修复", "source_refs": [1]},
                {"keyword": "现金流", "statement": "资本开支纪律支撑自由现金流改善", "source_refs": [2]},
                {"keyword": "增长", "statement": "核心产品迭代带动收入增长确定性", "source_refs": [3]},
            ]
        },
        "section_2": {
            "recent_updates": [
                {"keyword": "业绩", "fact": "管理层披露季度收入继续改善", "implication": "收入质量和利润率具备跟踪价值", "source_refs": [1]},
                {"keyword": "产品", "fact": "新产品进入集中迭代窗口", "implication": "用户转化和留存率成为验证变量", "source_refs": [2]},
                {"keyword": "成本", "fact": "公司继续强调成本投入纪律", "implication": "资本开支和毛利率需要同步观察", "source_refs": [3]},
                {"keyword": "客户", "fact": "企业客户续约保持稳定", "implication": "订阅收入和现金流弹性更重要", "source_refs": [4]},
            ]
        },
        "section_3": {
            "near_term_logic": [
                {"title": "收入修复", "mechanism": "产品放量带动收入增长和毛利率改善", "verification_metrics": ["收入增速", "毛利率"], "source_refs": [1]},
                {"title": "费用纪律", "mechanism": "研发和销售费用投入节奏影响短期利润弹性", "verification_metrics": ["费用率", "经营利润率"], "source_refs": [2]},
            ],
            "long_term_logic": [
                {"title": "生态深化", "mechanism": "平台生态提升客户留存和交叉销售空间", "verification_metrics": ["续约率", "ARPU"], "source_refs": [3]},
                {"title": "技术投入", "mechanism": "AI能力提升产品效率并支撑长期估值修复", "verification_metrics": ["研发投入", "新产品收入"], "source_refs": [4]},
            ],
        },
        "section_4": {
            "catalysts": [
                {"date": "2026-Q2", "event": "季度业绩发布验证收入修复", "impact": "观察收入增速和经营利润率变化", "source_refs": [1]},
                {"date": "2026-Q3", "event": "重点产品版本迭代", "impact": "跟踪用户转化率和留存率", "source_refs": [2]},
                {"date": "2026-H2", "event": "资本开支计划披露", "impact": "验证现金流和毛利率压力", "source_refs": [3]},
                {"date": "2026-H2", "event": "企业客户续约季持续推进", "impact": "观察续约率和订阅收入弹性", "source_refs": [4]},
            ]
        },
    }


class SectionsJsonTests(unittest.TestCase):
    def setUp(self):
        self.old_call = writer._call_llm

    def tearDown(self):
        writer._call_llm = self.old_call

    def _mock_llm(self, responses):
        calls = {"n": 0}

        def fake_call(*args, **kwargs):
            idx = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return responses[idx]

        writer._call_llm = fake_call
        return calls

    def test_complete_json_renders_sections_1_4(self):
        self._mock_llm([(json.dumps(_payload(), ensure_ascii=False), True)])
        sections, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertTrue(ok, meta)
        self.assertIn("## 1 关键要点", sections["s12"])
        self.assertIn("## 4 催化事件时间表", sections["s34"])
        self.assertIn("| 时间 | 事件 | 影响 |", sections["s34"])
        self.assertIn("### 3.1 短期逻辑", sections["s34"])
        self.assertIn("### 3.2 长期逻辑", sections["s34"])

    def test_fenced_json_cleanup(self):
        self._mock_llm([("```json\n" + json.dumps(_payload(), ensure_ascii=False) + "\n```", True)])
        _, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertTrue(ok, meta)

    def test_missing_section_fails(self):
        bad = _payload()
        bad.pop("section_4")
        self._mock_llm([(json.dumps(bad, ensure_ascii=False), True), (json.dumps(bad, ensure_ascii=False), True), (json.dumps(bad, ensure_ascii=False), True)])
        _, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertFalse(ok)
        self.assertIn("failed", meta["call_mode"])

    def test_section4_less_than_four_fails(self):
        bad = _payload()
        bad["section_4"]["catalysts"] = bad["section_4"]["catalysts"][:3]
        self._mock_llm([(json.dumps(bad, ensure_ascii=False), True)] * 4)
        _, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertFalse(ok)
        self.assertTrue(any("section_4.catalysts_count" in x for x in meta["schema_issues"]))

    def test_out_of_range_and_string_refs_fail(self):
        for refs in ([99], ["1"]):
            bad = _payload()
            bad["section_1"]["key_points"][0]["source_refs"] = refs
            self._mock_llm([(json.dumps(bad, ensure_ascii=False), True)] * 4)
            _, ok, _ = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
            self.assertFalse(ok)

    def test_section1_over_four_fails(self):
        bad = _payload()
        bad["section_1"]["key_points"].append({"keyword": "估值", "statement": "估值修复依赖收入和利润持续改善", "source_refs": [5]})
        bad["section_1"]["key_points"].append({"keyword": "需求", "statement": "需求恢复继续支撑增长和现金流改善", "source_refs": [6]})
        self._mock_llm([(json.dumps(bad, ensure_ascii=False), True)] * 4)
        _, ok, _ = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertFalse(ok)

    def test_combined_fail_then_split_succeeds(self):
        p = _payload()
        part12 = {"title_conclusion": p["title_conclusion"], "section_1": p["section_1"], "section_2": p["section_2"]}
        part34 = {"section_3": p["section_3"], "section_4": p["section_4"]}
        calls = self._mock_llm([
            ("not json", True),
            ("not json", True),
            (json.dumps(part12, ensure_ascii=False), True),
            (json.dumps(part34, ensure_ascii=False), True),
        ])
        _, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertTrue(ok, meta)
        self.assertEqual(meta["call_mode"], "ok_json_split")
        self.assertEqual(calls["n"], 4)

    def test_both_combined_and_split_fail_no_semantic_fallback(self):
        self._mock_llm([("not json", True)] * 4)
        sections, ok, meta = writer._legacy_gen_hkus_ch1_to_4_unused(_key_data(), _ref_map())
        self.assertFalse(ok)
        self.assertEqual(sections, {})
        self.assertEqual(meta["call_mode"], "failed_schema")

    def test_title_extraction_and_no_title_blocking_signal(self):
        good = "- **利润率**：云业务收入改善驱动利润率修复[1]\n\n## 2 近况跟踪\n"
        self.assertEqual(writer._derive_title_conclusion(good, "测试公司", "港股"), "云业务收入改善驱动利润率修复")
        self.assertEqual(writer._derive_title_conclusion("", "测试公司", "港股"), "")

    def test_section8_standard_table_and_section12_risk_titles(self):
        sec8, issues8 = writer._validate_render_section_8(
            {"rows": [
                {"topic": "收入修复", "market_concern": "收入修复持续性仍需验证", "verification_metrics": ["收入增速"], "source_refs": [1]},
                {"topic": "利润弹性", "market_concern": "费用投入可能压制利润", "verification_metrics": ["费用率"], "source_refs": [2]},
                {"topic": "现金流质量", "market_concern": "资本开支可能影响现金流", "verification_metrics": ["经营现金流"], "source_refs": [3]},
            ]},
            _ref_map(),
        )
        self.assertFalse(issues8)
        self.assertIn("| 关注点 | 市场在担心什么 | 需要验证的数据 |", sec8)
        sec12, issues12 = writer._validate_render_section_12(
            {"risks": [
                {"title": "收入修复不及预期", "explanation": "若订单交付低于预期，将影响收入和利润率", "source_refs": [1]},
                {"title": "费用投入高于预期", "explanation": "若研发投入持续高于预期，将压制利润释放", "source_refs": [2]},
                {"title": "现金流承压风险", "explanation": "若资本开支高于预期，将影响自由现金流", "source_refs": [3]},
                {"title": "监管政策变化风险", "explanation": "若监管要求收紧，将影响产品上线和商业化节奏", "source_refs": [4]},
            ]},
            _ref_map(),
        )
        self.assertFalse(issues12)
        self.assertIn("**收入修复不及预期**", sec12)
        self.assertIn("[1]", sec12)

    def test_c9_allows_event_text_citation_but_flags_pure_label(self):
        ok_doc = """# T

## 4 催化事件时间表

| 时间 | 事件 | 影响 |
|:---|:---|:---|
| 2026-Q2 | 产品发布验证收入修复[1] | 观察收入增速和毛利率[1] |

## 13 参考资料
[1]Datayes Research | 2026-01-01 | ID: a | 机构 | 标题 | API: batchGetReportContent
"""
        ok_gate = checker.check_citation(ok_doc, "HK")
        self.assertFalse([i for i in ok_gate.issues if i.check_id == "C9"])
        bad_doc = ok_doc.replace("| 2026-Q2 | 产品发布验证收入修复[1] |", "| 2026-Q2 | 公司[1] |")
        bad_gate = checker.check_citation(bad_doc, "HK")
        self.assertTrue([i for i in bad_gate.issues if i.check_id == "C9"])


if __name__ == "__main__":
    unittest.main()
