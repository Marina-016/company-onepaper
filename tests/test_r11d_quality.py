#!/usr/bin/env python3
"""r11d mock-only quality tests for §9/§10/§11."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import hk_us_report_writer_v124 as writer  # noqa: E402
import peer_comparison_v124 as peers  # noqa: E402
import check_report_quality_v124 as checker  # noqa: E402


def _target_materials() -> dict:
    return {"research": {"details": [{
        "articleId": "T1",
        "articleTitle": "目标公司云服务与AI平台同业比较",
        "publishTimeReadable": "2026-07-12",
        "textAbstract": "目标公司云服务与AI平台业务中，PeerA、PeerB、PeerC是可比上市公司。PeerA在云服务收入上直接竞争，PeerB在企业软件订阅业务可比，PeerC在AI平台产品可比。",
    }]}, "materials_v2": {"unique_sources": []}}


def _peer_llm(prompt: str, **_: object) -> tuple[str, bool]:
    if "latest_progress_summary" in prompt:
        name = "PeerA" if "PeerA" in prompt else ("PeerB" if "PeerB" in prompt else "PeerC")
        return json.dumps({
            "latest_progress_summary": f"{name}发布AI云产品并披露企业客户增长20%",
            "progress_date": "2026-07-10",
            "progress_type": "产品发布",
            "quantitative_metrics": ["20%"],
            "source_ids": [f"{name}_M1"],
            "supporting_sentence": f"{name}发布AI云产品，企业客户增长20%，订阅收入继续提升。",
            "status": "supported",
        }, ensure_ascii=False), True
    if "业务关键词检索材料" in prompt:
        return json.dumps({"peer_candidates": [{
            "company_name": "PeerC", "ticker_hint": "PEERC", "market_hint": "US",
            "relationship": "global_benchmark", "overlap_business": "AI平台",
            "reason": "PeerC在AI平台产品上与目标公司可比", "source_ids": ["K1"],
        }]}, ensure_ascii=False), True
    rows = []
    for name, business, relation in [
        ("PeerA", "云服务", "直接竞争"),
        ("PeerB", "企业软件订阅", "细分业务可比"),
        ("PeerC", "AI平台", "商业模式可比"),
    ]:
        rows.append({
            "peer_name": name, "ticker_hint": name.upper(), "relation_type": relation,
            "comparable_business": business,
            "evidence_sentence": f"{name}在{business}方面与目标公司可比",
            "discovery_article_id": "T1", "confidence": 0.8,
        })
    return json.dumps(rows, ensure_ascii=False), True


def _peer_llm_two_only(prompt: str, **kw: object) -> tuple[str, bool]:
    if "业务关键词检索材料" in prompt or "latest_progress_summary" in prompt:
        return _peer_llm(prompt, **kw)
    payload, _ = _peer_llm(prompt, **kw)
    return json.dumps(json.loads(payload)[:2], ensure_ascii=False), True


def _resolver(candidate: dict) -> list[dict]:
    name = candidate["peer_name"]
    market = "A" if name == "PeerB" else ("HK" if name == "PeerC" else "US")
    ticker = "000001" if market == "A" else (f"{name.upper()}.HK" if market == "HK" else name.upper())
    return [{"name": name, "ticker": ticker, "market": market, "entity_id": ticker}]


def _material_fetcher(peer: dict, question: str, days_back: int, size: int) -> list[dict]:
    name = peer["peer_name"]
    return [{
        "id": f"{name}_M1",
        "title": f"{name} AI云产品进展",
        "text": f"{name} {peer['comparable_business']} 发布AI云产品，企业客户增长20%，订阅收入继续提升。",
        "metadata": {"id": f"{name}_M1", "publishTime": "2026-07-10", "organization": "Mock Research"},
        "type": "Materials V2",
    }]


def _candidate_fetcher(query: str, days_back: int, size: int) -> list[dict]:
    return [{"id": "K1", "title": "AI平台上市公司比较", "text": "PeerC在AI平台产品上与目标公司可比。", "metadata": {"id": "K1", "publishTime": "2026-07-09"}}]


def _valuation_materials() -> dict:
    rows = []
    for aid, org, target, rating, text in [
        ("a1", "LowSec", 57, "中性", "目标价57港元，基于2027E PE 15x；汽车镜头出货节奏偏慢，毛利率恢复有限。"),
        ("a2", "MidSec", 69, "买入", "目标价69港元，基于2027E盈利预测；高端手机镜头占比提升，车载镜头订单增长。"),
        ("a3", "HighSec", 94, "买入", "目标价94港元，采用SOTP估值；光互连小批量出货，AI客户认证推进。"),
        ("a4", "TopSec", 88, "买入", "目标价88港元，基于2026E PE 20x；手机ASP提升，汽车光学收入增长。"),
    ]:
        rows.append({"articleId": aid, "orgName": org, "targetPrice": target, "rating": rating,
                     "publishTime": "2026-06-01", "articleTitle": "舜宇光学科技目标价", "textAbstract": text, "content": text})
    return {"research": {"details": rows}}


class TestPeerR11D(unittest.TestCase):
    def test_01_first_round_peer_candidates_enough(self):
        got, status = peers.discover_peer_candidates(
            peers.target_research_inputs(_target_materials(), "目标公司", "00001.HK"),
            "目标公司", "00001.HK", "HK", _peer_llm)
        self.assertEqual(status, "supported")
        self.assertGreaterEqual(len(got), 3)

    def test_02_keyword_supplement_runs_when_first_round_insufficient(self):
        bundle = peers.build_peer_comparison_bundle(
            _target_materials(), "目标公司", "00001.HK", "HK",
            llm_call=_peer_llm_two_only, resolver=_resolver,
            material_fetcher=_material_fetcher, candidate_fetcher=_candidate_fetcher)
        self.assertTrue(bundle["diagnostics"]["keyword_supplement_triggered"])
        self.assertEqual(bundle["valid_peer_rows"], 3)

    def test_03_customer_supplier_partner_unlisted_filtered(self):
        reports = [{"articleId": "T1", "title": "目标", "abstract": "华为是客户，Apple是客户，PeerCo是同业"}]
        payload = [
            {"company_name": "华为", "ticker_hint": "", "relationship": "customer", "overlap_business": "手机客户", "evidence_sentence": "华为是目标公司客户", "source_ids": ["T1"]},
            {"company_name": "Apple", "ticker_hint": "AAPL", "relationship": "customer", "overlap_business": "手机客户", "evidence_sentence": "Apple是目标公司客户", "source_ids": ["T1"]},
            {"company_name": "PeerCo", "ticker_hint": "PCO", "relationship": "direct_competitor", "overlap_business": "光学镜头", "evidence_sentence": "PeerCo在光学镜头业务与目标公司竞争", "source_ids": ["T1"]},
        ]
        got, _ = peers.discover_peer_candidates(reports, "目标公司", "00001.HK", "HK", lambda *a, **k: (json.dumps({"peer_candidates": payload}, ensure_ascii=False), True))
        self.assertEqual([x["peer_name"] for x in got], ["PeerCo"])

    def test_04_cross_market_peer_resolution(self):
        candidates = [{"peer_name": "PeerA", "ticker_hint": "PEERA", "relation_type": "直接竞争", "comparable_business": "云服务", "evidence_sentence": "PeerA在云服务方面与目标公司可比", "discovery_article_id": "T1"}]
        resolved, dropped = peers.resolve_peer_entities(candidates, "00001.HK", "目标公司", resolver=_resolver)
        self.assertFalse(dropped)
        self.assertIn(resolved[0]["market"], {"A", "HK", "US"})

    def test_05_peer_sources_are_independent(self):
        bundle = peers.build_peer_comparison_bundle(_target_materials(), "目标公司", "00001.HK", "HK", llm_call=_peer_llm, resolver=_resolver, material_fetcher=_material_fetcher)
        merged = peers.merge_peer_sources_into_materials(_target_materials(), bundle)
        self.assertTrue(all(s["company_match"] == "exact_peer" and s["source_role"] == "peer_business_progress" for s in merged["materials_v2"]["unique_sources"]))

    def test_06_target_plus_two_peers_supported(self):
        bundle = peers.build_peer_comparison_bundle(_target_materials(), "目标公司", "00001.HK", "HK", llm_call=_peer_llm_two_only, resolver=_resolver, material_fetcher=_material_fetcher)
        self.assertGreaterEqual(bundle["valid_peer_rows"], 2)


class TestSection10R11D(unittest.TestCase):
    def _payload(self):
        return {"market_debates": [
            {"theme": "AI云收入", "bull_view": "AI云需求拉动收入", "bull_evidence": "2026Q2云收入同比增长20%", "bull_source_ids": [1], "bear_view": "AI云毛利率承压", "bear_evidence": "2026Q2资本开支同比增长30%", "bear_source_ids": [2], "validation_metric": "云收入和毛利率", "validation_window": "2026Q3"},
            {"theme": "广告商业化", "bull_view": "广告工具提升eCPM", "bull_evidence": "2026Q2广告收入增长15%", "bull_source_ids": [1], "bear_view": "投放回报仍需验证", "bear_evidence": "商户投放预算环比下降5%", "bear_source_ids": [2], "validation_metric": "eCPM与填充率", "validation_window": "2026H2"},
            {"theme": "游戏流水", "bull_view": "新游流水改善带动收入", "bull_evidence": "重点产品上线首月流水增长18%", "bull_source_ids": [3], "bear_view": "老游戏生命周期下行", "bear_evidence": "存量游戏流水同比下降8%", "bear_source_ids": [4], "validation_metric": "季度游戏收入", "validation_window": "2026Q3"},
            {"theme": "资本开支", "bull_view": "AI投入形成产品转化", "bull_evidence": "AI产品客户数增长25%", "bull_source_ids": [3], "bear_view": "投入拖累自由现金流", "bear_evidence": "资本开支同比增长40%", "bear_source_ids": [4], "validation_metric": "FCF与capex", "validation_window": "2026全年"},
        ]}

    def test_07_valid_new_schema_renders(self):
        rendered, issues = writer._validate_render_section_10(self._payload(), {i: {"id": str(i)} for i in range(1, 5)})
        self.assertTrue(rendered, issues)
        self.assertNotIn("多：", rendered)

    def test_08_duplicate_theme_is_deduped_to_fail_if_below_four(self):
        payload = self._payload()
        payload["market_debates"][1]["theme"] = "AI云收入"
        rendered, issues = writer._validate_render_section_10(payload, {i: {"id": str(i)} for i in range(1, 5)})
        self.assertTrue(rendered)
        self.assertTrue(any("duplicate_theme" in x for x in issues))

    def test_09_missing_bear_evidence_filtered(self):
        payload = self._payload()
        payload["market_debates"][0]["bear_evidence"] = ""
        rendered, issues = writer._validate_render_section_10(payload, {i: {"id": str(i)} for i in range(1, 5)})
        self.assertTrue(rendered)
        self.assertTrue(any("bear_evidence" in x for x in issues))

    def test_10_cell_refs_are_deduped(self):
        payload = self._payload()
        payload["market_debates"][0]["bull_view"] += "[1][1]"
        rendered, issues = writer._validate_render_section_10(payload, {i: {"id": str(i)} for i in range(1, 5)})
        self.assertFalse(issues)
        self.assertNotIn("[1][1]", rendered)

    def test_11_too_few_rows_fails(self):
        rendered, issues = writer._validate_render_section_10({"market_debates": self._payload()["market_debates"][:3]}, {1: {"id": "1"}})
        self.assertFalse(rendered)
        self.assertTrue(any("rows_count" in x for x in issues))


class TestValuationR11D(unittest.TestCase):
    def setUp(self):
        self.old = writer._call_llm

    def tearDown(self):
        writer._call_llm = self.old

    def test_12_batch_article_id_binding(self):
        writer._call_llm = lambda *a, **k: (json.dumps([{"articleId": "2", "target_price_basis": "DCF", "basis_evidence": "DCF估值", "key_assumptions": [{"text": "2027年收入增长20%", "evidence": "收入增长20%"}]}, {"articleId": "1", "target_price_basis": "PE 18x", "basis_evidence": "PE 18x", "key_assumptions": [{"text": "毛利率提升", "evidence": "毛利率提升"}]}], ensure_ascii=False), True)
        out = writer._extract_target_price_basis([{"article_id": "1", "org": "A", "target": 1, "date": "d"}, {"article_id": "2", "org": "B", "target": 2, "date": "d"}])
        self.assertEqual(out["1"]["target_price_basis"], "PE 18x")
        self.assertEqual(out["2"]["target_price_basis"], "DCF")

    def test_13_missing_article_retries_single_article(self):
        calls = []
        def fake(*args, **kw):
            calls.append(kw.get("call_name"))
            if kw.get("call_name") == "target_price_basis":
                return (json.dumps([{"articleId": "1", "target_price_basis": "PE", "basis_evidence": "PE", "key_assumptions": [{"text": "2027年收入增长", "evidence": "收入增长"}]}]), True)
            return (json.dumps([{"articleId": "2", "target_price_basis": "SOTP", "basis_evidence": "SOTP", "key_assumptions": [{"text": "车载订单增长", "evidence": "订单增长"}]}]), True)
        writer._call_llm = fake
        out = writer._extract_target_price_basis([{"article_id": "1", "org": "A", "target": 1, "date": "d"}, {"article_id": "2", "org": "B", "target": 2, "date": "d"}])
        self.assertEqual(out["2"]["target_price_basis"], "SOTP")
        self.assertIn("target_price_basis_retry:2", calls)

    def test_14_report_text_enters_prompt(self):
        prompt = writer._build_target_price_basis_prompt([{"article_id": "1", "org": "A", "target": 1, "date": "d", "report_text": "完整正文包含PE 18x"}])
        self.assertIn("完整正文包含PE 18x", prompt)

    def test_15_basis_evidence_parsed(self):
        out, _ = writer._normalize_target_basis_payload([{"articleId": "1", "target_price_basis": "PE 18x", "basis_evidence": "正文写PE 18x", "key_assumptions": []}], {"1"})
        self.assertEqual(out["1"]["basis_evidence"], "正文写PE 18x")

    def test_16_no_method_but_assumptions_parse(self):
        out, _ = writer._normalize_target_basis_payload([{"articleId": "1", "target_price_basis": "研报披露目标价,正文未披露估值方法", "key_assumptions": [{"text": "2027年车载收入增长20%", "evidence": "车载收入增长20%"}]}], {"1"})
        self.assertIn("车载收入增长", out["1"]["key_assumptions"])

    def test_17_assumption_evidence_bound_to_same_article(self):
        out, _ = writer._normalize_target_basis_payload([{"articleId": "1", "target_price_basis": "PE", "key_assumptions": [{"text": "ASP提升", "evidence": "同篇研报披露ASP提升"}]}], {"1"})
        self.assertEqual(out["1"]["assumption_evidence"], ["同篇研报披露ASP提升"])

    def test_18_even_median(self):
        self.assertEqual(writer.calculate_target_price_stats([57, 62, 69, 86.9, 88, 94])["median"], 77.95)

    def test_19_nearest_median_record(self):
        recs = [{"target": 57, "date": "1"}, {"target": 69, "date": "2"}, {"target": 86.9, "date": "3"}]
        self.assertEqual(writer._nearest_median_record(recs, 77.95)["target"], 69)

    def test_20_valuation_section_has_three_113_paras(self):
        writer._call_llm = lambda *a, **k: (json.dumps([{"articleId": x, "target_price_basis": "2027E PE 18x", "basis_evidence": "PE 18x", "key_assumptions": [{"text": "2027年收入增长20%", "evidence": "收入增长20%"}]} for x in ["a1", "a2", "a3", "a4"]], ensure_ascii=False), True)
        sec = writer._build_valuation_section(_valuation_materials(), {i: {"id": f"a{i}"} for i in range(1, 5)}, "舜宇光学科技", "02382.HK", "HK")
        self.assertNotIn("### 11.1", sec)
        self.assertIn("### 11.2", sec)
        self.assertNotIn("### 11.3", sec)
        self.assertNotIn("### 11.4", sec)

    def test_21_valuation_section_cites_111_refs(self):
        writer._call_llm = lambda *a, **k: ("[]", True)
        sec = writer._build_valuation_section(_valuation_materials(), {i: {"id": f"a{i}"} for i in range(1, 5)}, "舜宇光学科技", "02382.HK", "HK")
        self.assertRegex(sec, r"\[1\].*\[4\]")

    def test_22_nearest_median_not_called_median_target(self):
        writer._call_llm = lambda *a, **k: ("[]", True)
        sec = writer._build_valuation_section(_valuation_materials(), {i: {"id": f"a{i}"} for i in range(1, 5)}, "舜宇光学科技", "02382.HK", "HK")
        self.assertNotIn("中位数目标价机构", sec)


class TestCheckerR11D(unittest.TestCase):
    def test_23_checker_detects_duplicate_section10_refs(self):
        doc = "## 10 市场分歧\n| 多头观点 | 证据 | 空头观点 | 需要观察的验证点 |\n|:---|:---|:---|:---|\n" + "\n".join([
            "| A增长[1][1] | 多：收入20%[1][1]；空：capex30%[2] | B承压[2] | 2026Q3收入[1] |",
            "| C增长[1] | 多：收入20%[1]；空：capex30%[2] | D承压[2] | 2026Q3收入[1] |",
            "| E增长[1] | 多：收入20%[1]；空：capex30%[2] | F承压[2] | 2026Q3收入[1] |",
            "| G增长[1] | 多：收入20%[1]；空：capex30%[2] | H承压[2] | 2026Q3收入[1] |",
        ]) + "\n## 11 估值与预测\n"
        gate = checker.check_section_quality(doc, "HK")
        self.assertTrue([i for i in gate.issues if i.check_id == "E10_REF_DUP"])

    def test_24_checker_detects_113_missing_structure(self):
        doc = "### 11.1 机构目标价汇总\n| 机构 | 日期 | 评级 | 目标价 | 目标价口径 | 关键假设/关注点 |\n|:---|:---|:---|:---|:---|:---|\n| A | d | 买入 | 1港元/普通股[1] | PE 18x[1] | 2027年收入增长20%；毛利率提升[1] |\n### 11.3 估值分析\n目标价已经绑定来源[1]\n## 12 风险提示\n"
        gate = checker.check_section_quality(doc, "HK")
        self.assertTrue([i for i in gate.issues if i.check_id == "E11_3_STRUCTURE"])

    def test_25_checker_detects_assumption_coverage(self):
        doc = "### 11.1 机构目标价汇总\n| 机构 | 日期 | 评级 | 目标价 | 目标价口径 | 关键假设/关注点 |\n|:---|:---|:---|:---|:---|:---|\n" + "\n".join([
            "| A | d | 买入 | 1港元/普通股[1] | PE 18x[1] | 正文未披露可验证的关键假设[1] |",
            "| B | d | 买入 | 2港元/普通股[2] | PE 19x[2] | 正文未披露可验证的关键假设[2] |",
            "| C | d | 买入 | 3港元/普通股[3] | PE 20x[3] | 正文未披露可验证的关键假设[3] |",
        ])
        gate = checker.check_section_quality(doc, "HK")
        self.assertTrue([i for i in gate.issues if i.check_id == "E11_ASSUMPTION_COVERAGE"])

    def test_26_complete_mock_sections_9_10_11_render(self):
        bundle = peers.build_peer_comparison_bundle(_target_materials(), "目标公司", "00001.HK", "HK", llm_call=_peer_llm_two_only, resolver=_resolver, material_fetcher=_material_fetcher, candidate_fetcher=_candidate_fetcher)
        section9 = peers.build_peer_comparison_section(bundle, {1: {"id": "T1"}, 2: {"id": "PeerA_M1"}, 3: {"id": "PeerB_M1"}, 4: {"id": "PeerC_M1"}})
        section10, issues = writer._validate_render_section_10(TestSection10R11D()._payload(), {i: {"id": str(i)} for i in range(1, 5)})
        old_call = writer._call_llm
        writer._call_llm = lambda *a, **k: ("[]", True)
        try:
            section11 = writer._build_valuation_section(_valuation_materials(), {i: {"id": f"a{i}"} for i in range(1, 5)}, "舜宇光学科技", "02382.HK", "HK")
        finally:
            writer._call_llm = old_call
        self.assertIn("## 9", section9)
        self.assertTrue(section10, issues)
        self.assertNotIn("### 11.3", section11)


if __name__ == "__main__":
    unittest.main()
