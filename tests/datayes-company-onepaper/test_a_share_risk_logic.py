# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path
import unittest


WRITER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "a_share_report_writer.py"
SPEC = importlib.util.spec_from_file_location("a_share_report_writer_v1210", WRITER_PATH)
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


class AShareRiskLogicTests(unittest.TestCase):
    def setUp(self):
        self.key_data = {
            "name": "示例科技股份有限公司",
            "short_name": "示例科技",
            "ticker": "000001",
            "reports": [
                {
                    "id": "101",
                    "title": "核心产品与客户跟踪",
                    "detail_text": (
                        "核心客户订单下滑可能拖累收入兑现。"
                        "关键原材料价格上涨可能压缩产品毛利率。"
                        "海外认证进度不及预期可能导致新产品放量延期。"
                        "应收账款回款周期拉长可能影响经营现金流。"
                    ),
                    "abstract": "",
                    "text": "",
                }
            ],
            "meetings": [],
            "surveys": [],
            "fin": {
                "latest": {"label": "2026Q1"},
                "latest_data": {
                    "tRevenue": 1_500_000_000,
                    "NPAttrP": 120_000_000,
                    "revenueYOY": -5.2,
                    "NPAttrPYOY": -8.1,
                    "grossMargin": 26.5,
                    "netMargin": 8.0,
                    "ROE": 6.1,
                    "liabRatio": 62.0,
                },
            },
            "ref_map": {
                "report_101": {"n": 1},
                "fdmtNew": {"n": 2},
            },
        }

    def test_validator_accepts_normalized_bullet_markers(self):
        for marker in ("•", "-", "*"):
            body = "\n".join([
                f"{marker} **核心客户订单下滑**：订单变化可能拖累收入兑现[1]",
                f"{marker} **关键原材料价格上涨**：成本上行可能压缩毛利率[1]",
                f"{marker} **海外认证进度延迟**：认证延期可能推迟产品放量[1]",
            ])
            valid, issues = WRITER._validate_a_share_risk_body(body)
            self.assertTrue(valid, issues)

    def test_validator_rejects_generic_or_uncited_risks(self):
        body = "\n".join([
            "• **需求风险事项**：核心业务需求若放缓，收入增长可能低于预期",
            "• **竞争风险事项**：行业竞争加剧可能压缩价格和利润率",
            "• **宏观风险事项**：宏观环境和政策变化可能影响估值",
        ])
        valid, issues = WRITER._validate_a_share_risk_body(body)
        self.assertFalse(valid)
        self.assertIn("generic_risk_template", issues)
        self.assertTrue(any("missing_citation" in issue for issue in issues))

    def test_evidence_uses_real_report_fields_and_builds_cited_fallback(self):
        evidence = WRITER._collect_a_share_risk_evidence(self.key_data)
        self.assertGreaterEqual(len(evidence), 4)
        self.assertTrue(all(item["ref"] == 1 for item in evidence))
        fallback = WRITER._build_a_share_risk_fallback(self.key_data)
        valid, issues = WRITER._validate_a_share_risk_body(fallback)
        self.assertTrue(valid, issues)
        self.assertEqual(fallback.count("[1]"), 4)

    def test_financial_snapshot_reads_latest_data(self):
        snapshot = WRITER._latest_a_share_financial_snapshot(
            self.key_data["fin"], self.key_data["ref_map"]
        )
        self.assertIn("2026Q1", snapshot)
        self.assertIn("营业收入15.00亿元[2]", snapshot)
        self.assertIn("营收同比-5.20%[2]", snapshot)

    def test_enforcer_preserves_valid_bullets_and_repairs_generic_output(self):
        valid_body = "\n".join([
            "• **核心客户订单下滑**：订单变化可能拖累收入兑现[1]",
            "• **关键原材料价格上涨**：成本上行可能压缩毛利率[1]",
            "• **海外认证进度延迟**：认证延期可能推迟产品放量[1]",
        ])
        valid_md = f"## 10 风险提示\n\n{valid_body}\n\n## 参考资料\n\n[1]研报"
        self.assertEqual(
            WRITER._enforce_a_share_risk_section(valid_md, self.key_data), valid_md
        )

        generic_md = (
            "## 10 风险提示\n\n"
            "• **需求风险事项**：核心业务需求若放缓，收入增长可能低于预期\n"
            "• **竞争风险事项**：行业竞争加剧可能压缩价格和利润率\n"
            "• **宏观风险事项**：宏观环境和政策变化可能影响估值\n\n"
            "## 参考资料\n\n[1]研报"
        )
        repaired = WRITER._enforce_a_share_risk_section(generic_md, self.key_data)
        repaired_body = repaired.split("## 10 风险提示\n\n", 1)[1].split("\n\n## 参考资料", 1)[0]
        valid, issues = WRITER._validate_a_share_risk_body(repaired_body)
        self.assertTrue(valid, issues)
        self.assertNotIn("核心业务需求若放缓", repaired)


    def test_fallback_references_survive_dead_reference_cleanup(self):
        generic_md = (
            "# 示例报告\n\n## 10 风险提示\n\n"
            "• **需求风险事项**：核心业务需求若放缓，收入增长可能低于预期\n"
            "• **竞争风险事项**：行业竞争加剧可能压缩价格和利润率\n"
            "• **宏观风险事项**：宏观环境和政策变化可能影响估值\n\n"
            "## 参考资料\n\n"
            "[1]Datayes研报 | 2026-01-01 | ID：101 | 测试机构 | 风险研报 | API：batchGetReportContent\n"
            "[2]Datayes结构化接口 | 2026-01-01 | 000001 | 财务摘要 | API：fdmtNew\n"
        )
        repaired = WRITER._enforce_a_share_risk_section(generic_md, self.key_data)
        cleaned = WRITER._postprocess_v123(repaired, self.key_data["ref_map"])
        risk_body = cleaned.split("## 10 风险提示\n\n", 1)[1].split("\n\n## 参考资料", 1)[0]
        valid, issues = WRITER._validate_a_share_risk_body(risk_body)
        self.assertTrue(valid, issues)
        self.assertIn("[1]Datayes研报", cleaned)
        self.assertNotIn("[2]Datayes结构化接口", cleaned)

    def test_sparse_evidence_remains_invalid_for_final_fail_closed_gate(self):
        sparse = dict(self.key_data)
        sparse["reports"] = []
        sparse["meetings"] = []
        sparse["surveys"] = []
        invalid_md = (
            "# 示例报告\n\n## 10 风险提示\n\n"
            "• **数据缺失风险**：当前材料不足，后续持续跟踪\n\n"
            "## 参考资料\n\n"
        )
        self.assertEqual(WRITER._enforce_a_share_risk_section(invalid_md, sparse), invalid_md)
        blockers = WRITER._final_self_check_v123(invalid_md, sparse["ref_map"])
        self.assertTrue(any(item.startswith("15.") for item in blockers), blockers)
if __name__ == "__main__":
    unittest.main()