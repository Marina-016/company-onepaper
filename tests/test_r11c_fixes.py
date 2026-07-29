#!/usr/bin/env python3
"""r11c targeted mock tests: no real Datayes, LLM, report generation, or DOCX."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_report_quality_v124 as checker  # noqa: E402
import hk_us_report_writer_v124 as writer  # noqa: E402
import peer_comparison_v124 as peers  # noqa: E402


def _report_with_111(rows: list[str]) -> str:
    return "\n".join([
        "# 舜宇光学科技（02382.HK）港股公司一页纸：主业修复驱动盈利质量改善",
        "",
        "## 11 估值与预测",
        "",
        "### 11.1 机构目标价汇总",
        "",
        "| 机构 | 日期 | 评级 | 目标价 | 目标价口径 | 关键假设/关注点 |",
        "|:---|:---|:---|:---|:---|:---|",
        *rows,
        "",
        "## 13 参考资料",
        "[1]Datayes Research | 2026-06-01 | ID: a1 | UBS | A | API: batchGetReportContent",
        "[2]Datayes Research | 2026-06-02 | ID: a2 | HSBC | B | API: batchGetReportContent",
        "[3]Datayes Research | 2026-06-03 | ID: a3 | Citi | C | API: batchGetReportContent",
        "[4]Datayes Research | 2026-06-04 | ID: a4 | BofA | D | API: batchGetReportContent",
        "[5]Datayes Research | 2026-06-05 | ID: a5 | MS | E | API: batchGetReportContent",
        "[6]Datayes Research | 2026-06-06 | ID: a6 | Bernstein | F | API: batchGetReportContent",
    ])


class TestC10DistinctOrganizations(unittest.TestCase):
    def test_same_org_same_ref_three_cells_no_c10(self):
        row = "| UBS | 2026-06 | 卖出 | 57港元/普通股[1] | PE 15x[1] | 出货改善[1] |"
        gate = checker.check_citation(_report_with_111([row]), "HK")
        self.assertFalse([i for i in gate.issues if i.check_id == "C10"])

    def test_two_orgs_share_ref_no_c10(self):
        rows = [
            "| UBS | 2026-06 | 卖出 | 57港元/普通股[1] | PE 15x[1] | 出货改善[1] |",
            "| HSBC | 2026-06 | 买入 | 62港元/普通股[1] | PE 16x[1] | ASP改善[1] |",
        ]
        gate = checker.check_citation(_report_with_111(rows), "HK")
        self.assertFalse([i for i in gate.issues if i.check_id == "C10"])

    def test_three_orgs_share_ref_triggers_c10(self):
        rows = [
            "| UBS | 2026-06 | 卖出 | 57港元/普通股[1] | PE 15x[1] | 出货改善[1] |",
            "| HSBC | 2026-06 | 买入 | 62港元/普通股[1] | PE 16x[1] | ASP改善[1] |",
            "| Citi | 2026-06 | 买入 | 69港元/普通股[1] | PE 17x[1] | 毛利率改善[1] |",
        ]
        gate = checker.check_citation(_report_with_111(rows), "HK")
        self.assertTrue([i for i in gate.issues if i.check_id == "C10"])

    def test_six_orgs_independent_refs_no_c10(self):
        rows = [
            f"| Org{i} | 2026-06 | 买入 | {50+i}港元/普通股[{i}] | PE {10+i}x[{i}] | 假设{i}[{i}] |"
            for i in range(1, 7)
        ]
        gate = checker.check_citation(_report_with_111(rows), "HK")
        self.assertFalse([i for i in gate.issues if i.check_id == "C10"])


class TestRefsAndSection10(unittest.TestCase):
    def test_normalize_refs_merges_and_dedupes(self):
        self.assertEqual(writer.normalize_refs("2027年小批量出货[2][7]", [2, 5, 7, 8, 2]), "2027年小批量出货[2][7][5][8]")

    def test_section10_cells_have_no_duplicate_refs(self):
        ref_map = {i: {"id": str(i)} for i in range(1, 5)}
        payload = {"market_debates": [
            {"bull_view": "光互连放量改善收入[1]", "evidence": "2027年小批量出货[1][2]", "bear_view": "客户认证慢于预期[1]", "validation": "观察出货和客户认证[1]", "source_refs": [1, 2, 1]},
            {"bull_view": "车载光学渗透率提升[2]", "evidence": "ADAS镜头需求提升[2]", "bear_view": "汽车需求拖累出货[2]", "validation": "观察车载镜头出货[2]", "source_refs": [2]},
            {"bull_view": "手机高端化改善ASP[3]", "evidence": "潜望式模组占比提升[3]", "bear_view": "安卓需求仍偏弱[3]", "validation": "观察ASP和毛利率[3]", "source_refs": [3]},
            {"bull_view": "新终端打开增量空间[4]", "evidence": "AI眼镜项目推进[4]", "bear_view": "新品量产节奏不确定[4]", "validation": "观察客户定点进度[4]", "source_refs": [4]},
        ]}
        rendered, issues = writer._validate_render_section_10(payload, ref_map)
        self.assertFalse(issues)
        for cell in [c for line in rendered.splitlines() if line.startswith("|") for c in line.split("|")]:
            nums = [int(x) for x in __import__("re").findall(r"\[(\d+)\]", cell)]
            self.assertEqual(len(nums), len(set(nums)), cell)


class TestTargetPriceBasisRetry(unittest.TestCase):
    def setUp(self):
        self.old = writer._call_llm

    def tearDown(self):
        writer._call_llm = self.old

    def test_batch_json_array_maps_article_ids(self):
        writer._call_llm = lambda *a, **k: (json.dumps([
            {"articleId": "2", "target_price_basis": "DCF", "key_assumptions": ["B"]},
            {"articleId": "1", "target_price_basis": "PE 18x", "key_assumptions": ["A"]},
        ], ensure_ascii=False), True)
        out = writer._extract_target_price_basis([
            {"article_id": "1", "org": "A", "target": 1, "date": "d", "evidence": "e"},
            {"article_id": "2", "org": "B", "target": 2, "date": "d", "evidence": "e"},
        ])
        self.assertEqual(out["1"]["target_price_basis"], "PE 18x")
        self.assertEqual(out["2"]["target_price_basis"], "DCF")

    def test_missing_article_retry_only_that_article(self):
        calls = []
        def fake(prompt, **kw):
            calls.append(kw.get("call_name"))
            if kw.get("call_name") == "target_price_basis":
                return (json.dumps([{"articleId": "1", "target_price_basis": "PE", "key_assumptions": ["A"]}]), True)
            return (json.dumps([{"articleId": "2", "target_price_basis": "SOTP", "key_assumptions": ["B"]}]), True)
        writer._call_llm = fake
        out = writer._extract_target_price_basis([
            {"article_id": "1", "org": "A", "target": 1, "date": "d", "evidence": "e"},
            {"article_id": "2", "org": "B", "target": 2, "date": "d", "evidence": "e"},
        ])
        self.assertEqual(out["2"]["target_price_basis"], "SOTP")
        self.assertIn("target_price_basis_retry:2", calls)
        self.assertNotIn("target_price_basis_retry:1", calls)

    def test_unknown_and_duplicate_article_ids_dropped(self):
        writer._call_llm = lambda *a, **k: (json.dumps([
            {"articleId": "9", "target_price_basis": "bad", "key_assumptions": ["x"]},
            {"articleId": "1", "target_price_basis": "PE", "key_assumptions": ["A"]},
            {"articleId": "1", "target_price_basis": "DCF", "key_assumptions": ["B"]},
        ]), True)
        out = writer._extract_target_price_basis([{"article_id": "1", "org": "A", "target": 1, "date": "d", "evidence": "e"}])
        self.assertEqual(out["1"]["target_price_basis"], "PE")
        self.assertNotIn("9", out)


class TestValuationAndStatus(unittest.TestCase):
    def test_113_contains_real_refs(self):
        ref_map = {1: {"id": "a1"}, 2: {"id": "a2"}, 3: {"id": "a3"}}
        materials = {"research": {"details": [
            {"articleId": "a1", "orgName": "A", "targetPrice": 57, "rating": "买入", "publishTime": "2026-06-01", "articleTitle": "舜宇光学科技", "textAbstract": "舜宇光学科技目标价"},
            {"articleId": "a2", "orgName": "B", "targetPrice": 62, "rating": "买入", "publishTime": "2026-06-02", "articleTitle": "舜宇光学科技", "textAbstract": "舜宇光学科技目标价"},
            {"articleId": "a3", "orgName": "C", "targetPrice": 69, "rating": "买入", "publishTime": "2026-06-03", "articleTitle": "舜宇光学科技", "textAbstract": "舜宇光学科技目标价"},
        ]}}
        old = writer._call_llm
        writer._call_llm = lambda *a, **k: ("[]", True)
        try:
            sec = writer._build_valuation_section(materials, ref_map, "舜宇光学科技", "02382.HK", "HK")
        finally:
            writer._call_llm = old
        self.assertIn("### 11.3", sec)
        self.assertRegex(sec, r"11\.1.*\[1\]\[2\]\[3\]")

    def test_generation_status_ok_has_no_g0(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "generation_status.json"), "w", encoding="utf-8") as f:
                json.dump({"mode": "full", "is_degraded": False, "is_skeleton": False, "failed_sections": [], "assembly_issues": []}, f)
            gate = checker.check_generation_status_file(d)
            self.assertFalse([i for i in gate.issues if i.check_id == "G0"])

    def test_generation_status_degraded_has_single_g0(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "generation_status.json"), "w", encoding="utf-8") as f:
                json.dump({"mode": "degraded", "is_degraded": True, "failed_sections": ["10"], "assembly_issues": ["x"]}, f)
            gate = checker.check_generation_status_file(d)
            self.assertEqual(len([i for i in gate.issues if i.check_id == "G0"]), 1)


class TestHeaderData(unittest.TestCase):
    def test_extract_price_market_cap_from_materials(self):
        m = {"market": "hk", "structured": {"snapshot": {"lastPrice": 72.5, "marketCap": 19500000000, "trade_date": "2026-07-15"}}}
        self.assertIn("72.5", writer._extract_price(m))
        self.assertIn("2026-07-15", writer._extract_price(m))

    def test_internal_market_cap_formula(self):
        m = {"market": "hk", "structured": {"snapshot": {"lastPrice": 72.5, "total_shares": 269000000, "trade_date": "2026-07-15"}}}
        self.assertIn("价格×总股本", writer._extract_price(m))

    def test_no_date_rejects_price(self):
        m = {"market": "hk", "structured": {"snapshot": {"lastPrice": 72.5, "marketCap": 19500000000}}}
        self.assertIn("未取得具有日期", writer._extract_price(m))

    def test_extract_industry_from_existing_materials(self):
        self.assertEqual(writer._extract_industry({"profile": {"industry_name": "光学元件"}}), "光学元件")


class TestPeerFilteringAndCheckerSmallIssues(unittest.TestCase):
    def test_customer_and_unlisted_not_peer_candidates(self):
        target_reports = [{"articleId": "T1", "title": "舜宇", "abstract": "舜宇与华为和苹果客户合作"}]
        payload = [
            {"company_name": "华为", "ticker_hint": "", "relationship": "customer", "overlap_business": "手机客户", "evidence_sentence": "华为是舜宇手机镜头客户", "evidence_source_ids": ["T1"]},
            {"company_name": "Apple", "ticker_hint": "AAPL", "relationship": "customer", "overlap_business": "手机客户", "evidence_sentence": "Apple是舜宇手机镜头客户", "evidence_source_ids": ["T1"]},
            {"company_name": "PeerCo", "ticker_hint": "PCO", "relationship": "direct_competitor", "overlap_business": "光学镜头", "evidence_sentence": "PeerCo在光学镜头业务与舜宇竞争", "evidence_source_ids": ["T1"]},
        ]
        got, status = peers.discover_peer_candidates(target_reports, "舜宇", "02382.HK", "HK", lambda *a, **k: (json.dumps(payload, ensure_ascii=False), True))
        self.assertEqual(status, "supported")
        self.assertEqual([x["peer_name"] for x in got], ["PeerCo"])

    def test_c9_allows_source_column_refs(self):
        doc = """# T

| 指标 | 引用来源 |
|:---|:---|
| 收入 | Datayes Research[1] |

## 13 参考资料
[1]Datayes Research | 2026-01-01 | ID: a | Org | T | API: batchGetReportContent
"""
        gate = checker.check_citation(doc, "HK")
        self.assertFalse([i for i in gate.issues if i.check_id == "C9"])

    def test_pit_source_line_three_refs_passes_d9d(self):
        doc = """# T

## 7 财务与盈利质量
数据来源：港股 PIT 利润表、资产负债表及现金流量表[1][2][3]。

| 指标 | FY2025 | FY2024 | FY2023 |
|:---|---:|---:|---:|
| 营业收入 | 1 | 2 | 3 |

## 13 参考资料
[1]Datayes结构化接口 | 2026-07-15 | 02382.HK | 港股 PIT 利润表 | API：getHkFdmtIsPit
[2]Datayes结构化接口 | 2026-07-15 | 02382.HK | 港股 PIT 资产负债表 | API：getHkFdmtBsPit
[3]Datayes结构化接口 | 2026-07-15 | 02382.HK | 港股 PIT 现金流量表 | API：getHkFdmtCfPit
"""
        gate = checker.check_data_integrity(doc, "HK")
        self.assertFalse([i for i in gate.issues if i.check_id == "D9d"])


if __name__ == "__main__":
    unittest.main()
