import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("writer", ROOT / "scripts" / "a_share_report_writer.py")
writer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(writer)

FETCH_SPEC = importlib.util.spec_from_file_location("fetch", ROOT / "scripts" / "a_share_fetch_data.py")
fetch = importlib.util.module_from_spec(FETCH_SPEC)
FETCH_SPEC.loader.exec_module(fetch)


class AShareWriterRegressionTests(unittest.TestCase):
    def test_scenario_uses_qualitative_contract_and_rejects_unsourced_target(self):
        key_data = {
            "valuation": {"items": {
                "市盈率PE": {"val": 21.3552, "avg": 18.0},
                "市净率PB": {"val": 2.35, "avg": 2.0},
            }},
            "consensus_forecasts": [
                {"foreYear": 2026, "conEps": 3.905, "conPe": 18.2995},
                {"foreYear": 2027, "conEps": 4.3614, "conPe": 16.3848},
            ],
            "actual_consensus": {"conEps": 2.3068},
        }
        valid = """### 9.4 情景推演
**核心变量**
• **销量**：100万件[1]；影响收入兑现
• **单价**：10元[2]；影响盈利水平

**情景推演表**：

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~25%） | 销量兑现更快<br>单价稳定 | 收入和利润预期改善 | 增长预期上修，估值中枢获得支撑。 |
| 中性（概率~50%） | 延续当前节奏 | 收入和利润按预期演变 | 当前预期大体兑现，估值预期保持稳定。 |
| 悲观（概率~25%） | 销量兑现偏慢<br>单价承压 | 收入和利润预期承压 | 增长预期走弱，估值风险溢价上升。 |
"""
        self.assertEqual(writer._scenario_target_price_errors(valid, key_data), [])
        invalid = valid.replace(
            "增长预期上修，估值中枢获得支撑。",
            "EPS＝3.91元 × PE＝18.3x ＝71.55元",
            1,
        )
        self.assertTrue(writer._scenario_target_price_errors(invalid, key_data))

    def test_valuation_context_detects_cross_interface_price_conflict(self):
        key_data = {
            "valuation": {"items": {
                "市盈率PE": {"val": 21.3552, "avg": 18.0, "rank": 2, "rankBase": 5},
                "市净率PB": {"val": 2.35, "avg": 2.0},
            }},
            "consensus_forecasts": [
                {"foreYear": 2026, "conEps": 3.905, "conPe": 18.2995},
                {"foreYear": 2027, "conEps": 4.3614, "conPe": 16.3848},
            ],
            "actual_consensus": {"conEps": 2.3068},
            "ref_map": {"valuation_rank": {"n": 1}, "consensus": {"n": 2}},
        }
        ctx = writer._scenario_valuation_context(key_data)
        self.assertAlmostEqual(ctx["anchor_price"], 71.46, places=1)
        self.assertFalse(ctx["rank_reconciled"])
        section = writer._gen_section93(key_data)
        self.assertIn("接口口径未对齐", section)
        self.assertIn("不作为当前定价、PEG或情景目标价依据", section)
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
        self.assertIn("若终端需求或订单兑现弱于预期", result)
        self.assertIn("重点跟踪", result)
        self.assertEqual(result.count("**"), 6)
    def test_risk_fallback_deduplicates_same_risk_theme(self):
        original = writer._collect_a_share_risk_evidence
        writer._collect_a_share_risk_evidence = lambda *_args, **_kwargs: [
            {"risk_title": "需求不及预期", "text": "风险提示：需求不及预期", "ref": 1},
            {"risk_title": "销量不及预期", "text": "风险提示：销量不及预期", "ref": 2},
            {"risk_title": "价格竞争加剧", "text": "风险提示：价格竞争加剧", "ref": 3},
            {"risk_title": "原材料价格波动", "text": "风险提示：原材料价格波动", "ref": 4},
        ]
        try:
            result = writer._build_a_share_risk_fallback({"name": "测试公司"})
        finally:
            writer._collect_a_share_risk_evidence = original
        self.assertEqual(result.count("重点跟踪订单、出货和渠道库存"), 1)
        self.assertTrue(writer._validate_a_share_risk_body(result)[0])
    def test_risk_gate_requires_three_source_backed_company_risks(self):
        body = (
            "• **核心产品需求放缓风险**：若客户订单节奏放缓，收入兑现与产能利用率可能承压；重点跟踪订单、出货和渠道库存。[1]\n"
            "• **行业价格竞争风险**：若行业供给释放或价格竞争加剧，产品价格与盈利空间可能承压；重点跟踪产品价格、毛利率和市场份额。[2]\n"
            "• **原材料成本波动风险**：若核心原材料价格波动超预期，成本传导滞后可能压缩盈利能力；重点跟踪原材料价格、采购成本和毛利率。[3]"
        )
        valid, issues = writer._validate_a_share_risk_body(body)
        self.assertTrue(valid, issues)
    def test_risk_json_requires_trigger_impact_and_monitor(self):
        payload = {"risks": [{
            "title": "库存减值风险", "trigger": "下游去化弱于备货节奏", "impact": "库存周转承压并可能增加减值压力",
            "monitor": "库存、周转天数和资产减值损失", "source_refs": [3],
        }, {
            "title": "价格竞争加剧", "trigger": "行业供给释放或价格竞争加剧", "impact": "产品价格与盈利空间可能承压",
            "monitor": "重点跟踪产品价格、毛利率和市场份额", "source_refs": [4],
        }, {
            "title": "原材料成本波动", "trigger": "核心原材料价格波动超预期", "impact": "成本传导滞后可能压缩盈利能力",
            "monitor": "原材料价格、采购成本和毛利率", "source_refs": [5],
        }]}
        rendered, issues = writer._validate_render_section_10(payload, {"a": {"n": 3}, "b": {"n": 4}, "c": {"n": 5}})
        self.assertEqual(issues, [])
        self.assertIn("重点跟踪库存、周转天数和资产减值损失", rendered)
        self.assertIn("重点跟踪产品价格、毛利率和市场份额", rendered)
        self.assertNotIn("重点跟踪重点跟踪", rendered)
        self.assertTrue(writer._validate_a_share_risk_body(rendered)[0])

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

    def test_qa_uses_concise_answer_label(self):
        rendered = writer._format_qa_markdown([{"q": "question", "a": "answer"}], [])
        self.assertIn("**A：** answer", rendered)
        self.assertNotIn("调研纪要观点，非公司指引", rendered)

    def test_section_seven_topic_blocks_are_promoted_to_h3(self):
        md = """## 7 公司调研大纲

**议题1：渠道库存与回款节奏**
背景：渠道库存仍需跟踪[1]

**议题2：产品结构与价格带表现**
背景：核心产品动销变化[2]

**议题3：费用投放效率**
背景：销售费用率变化[3]
"""
        rendered = writer._renumber_a_share_subsections(md)
        self.assertIn("### 7.1 渠道库存与回款节奏", rendered)
        self.assertIn("### 7.2 产品结构与价格带表现", rendered)
        self.assertIn("### 7.3 费用投放效率", rendered)
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
    def test_peer_table_keeps_sourced_progress_and_drops_column_without_baseline_source(self):
        peers = [{"code": "000858", "current_name": "五粮液"}, {"code": "000568", "current_name": "泸州老窖"}]
        table = """| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品 |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| —（基准） | 贵州茅台（600519） | A股 | 白酒 | 龙头 | 渠道改革推进并优化终端触达[3] | 品牌驱动 | 高端消费 | 茅台酒 |
| 直接竞争 | 五粮液（000858） | A股 | 白酒 | 龙头 | 第八代产品推进渠道分类运营[4] | 品牌驱动 | 商务消费 | 五粮液酒 |
| 直接竞争 | 泸州老窖（000568） | A股 | 白酒 | 品牌厂商 | — | 品牌驱动 | 商务消费 | 国窖1573 |"""
        result = writer._validate_peer_table(table, "贵州茅台", "600519", peers, {"000858": [4]}, {3})
        self.assertIn("相关业务进展", result)
        self.assertIn("渠道改革推进并优化终端触达[3]", result)
        self.assertIn("第八代产品推进渠道分类运营[4]", result)
        no_baseline = writer._validate_peer_table(table.replace("[3]", "[9]"), "贵州茅台", "600519", peers, {"000858": [4]}, {3})
        self.assertNotIn("相关业务进展", no_baseline)
        self.assertNotIn("市值", no_baseline)
    def test_peer_progress_keeps_complete_sentences_without_finance_word_blacklist(self):
        complete = "第八代产品推进渠道分类运营并优化终端触达[4]"
        financial_only = "2026年营业总收入同比下降，归属于母公司股东的净利润承压[4]"
        overlong = "新品发布后持续推进渠道分类运营、终端建设、客户导入和市场拓展，后续仍将优化产品组合以提升经营质量和品牌势能，并同步加快区域市场覆盖和终端运营能力建设[4]"
        multi_sentence = "新品发布并完成首批渠道铺货。后续将围绕核心产品持续推进终端建设、客户导入、区域市场拓展、运营能力升级和渠道精细化管理，并加快重点区域市场覆盖节奏[4]"
        self.assertEqual(writer._compact_peer_progress(complete), complete)
        self.assertEqual(writer._compact_peer_progress(financial_only), financial_only)
        self.assertEqual(writer._compact_peer_progress(overlong), "—")
        self.assertEqual(writer._compact_peer_progress(multi_sentence), "新品发布并完成首批渠道铺货。[4]")
    def test_peer_material_reference_resolves_to_original_source(self):
        data = {"peer_materials": [{"peer_name": "五粮液", "peer_code": "000858", "id": "m1", "title": "五粮液进展", "text": "五粮液渠道反馈"}]}
        refs = writer.build_ref_map(data)
        peer_ref = next(item for item in refs.values() if item["type"] == "同业材料")
        evidence = writer._build_reference_evidence({"_raw_data": data, "ref_map": refs}, writer.refs_to_markdown(refs))
        self.assertIn("五粮液渠道反馈", evidence[peer_ref["n"]]["text"])
        self.assertEqual(evidence[peer_ref["n"]]["api"], "getMaterialsV2")
    def test_peer_candidates_extract_plain_name_phrases_with_leadin(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "同业公司如五粮液、泸州老窖、山西汾酒等，竞争格局稳定。"}}]
        candidates = fetch.extract_peer_names(reports, "贵州茅台")
        phrases = {item["query"] for item in candidates if not item["code"]}
        self.assertTrue({"五粮液", "泸州老窖", "山西汾酒"} <= phrases)

    def test_peer_candidates_extract_explicit_compare_and_mixed_enumeration(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "相比之下，宁德时代（CATL）在固态电池领域推进更快。主要对手包括比亚迪、国轩高科和亿纬锂能等电池厂商。"}}]
        candidates = fetch.extract_peer_names(reports, "欣旺达")
        phrases = {item["query"] for item in candidates if not item["code"]}
        self.assertTrue({"宁德时代", "比亚迪", "国轩高科", "亿纬锂能"} <= phrases)

    def test_validate_peer_code_uses_name_query_and_keeps_code_binding(self):
        seen_queries = []
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            seen_queries.append((params or {}).get("query"))
            return {"code": 1, "data": {"hits": [{"entity_id": "000858", "name": "五粮液股份有限公司"}]}}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            validated = fetch.validate_peer_names({"stock_search": {"url": "u"}}, [
                {"query": "五粮液", "code": "000858"},
            ], "tok")
        finally:
            fetch.call = original
        self.assertEqual(seen_queries, ["五粮液"])
        self.assertEqual(validated[0]["code"], "000858")
        self.assertEqual(validated[0]["current_name"], "五粮液股份有限公司")
    def test_peer_candidates_extract_competitive_brand_enumeration(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "\u98de\u5929\u8305\u53f0\u7684\u9500\u91cf\u589e\u957f\u5c06\u65e5\u76ca\u6324\u5360\u4e94\u7cae\u6db2\u3001\u6cf8\u5dde\u8001\u7a96\u7b49\u5176\u4ed6\u9ad8\u7aef\u54c1\u724c\u7684\u5e02\u573a\u9700\u6c42\u3002"}}]
        candidates = fetch.extract_peer_names(reports, "\u8d35\u5dde\u8305\u53f0")
        phrases = {item["query"] for item in candidates if not item["code"]}
        self.assertTrue({"\u4e94\u7cae\u6db2", "\u6cf8\u5dde\u8001\u7a96"} <= phrases)

    def test_peer_candidates_reject_noncompetitive_company_mentions(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "\u5408\u89c4\u62ab\u9732\uff1a\u5b89\u5fbd\u53e4\u4e95\u8d21\u9152\u80a1\u4efd\u6709\u9650\u516c\u53f8\u4e3a\u62a5\u544a\u8986\u76d6\u516c\u53f8\u3002"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "\u8d35\u5dde\u8305\u53f0"), [])
    def test_peer_candidates_reject_nonpeer_phrases_after_competition_word(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "\u67d0\u9879\u63aa\u65bd\u53ef\u80fd\u51b2\u51fb\u5e02\u573a\u7a33\u5b9a\u6027\u3002\u5206\u9f84\u8fd0\u8425\u7834\u5c40\u5e74\u8f7b\u5316\uff0c\u5e02\u503c\u7ba1\u7406\u63a5\u529b\u63a8\u8fdb\u3002"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "\u8d35\u5dde\u8305\u53f0"), [])

    def test_peer_candidates_reject_disclaimer_enumeration(self):
        reports = [{"id": "r1", "_meta": {"abstractText": "可比估值说明：如需了解我们如何计算高盛因子概况的详细说明，请联系您的高盛代表。"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "海天味业"), [])
    def test_peer_candidates_reject_generic_peer_sentences(self):
        reports = [{"id": "r2", "_meta": {"abstractText": "参考可比公司2026年底部区间为12-24倍PE，我们给予25倍PE。"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "贵州茅台"), [])

    def test_peer_candidates_extract_paren_annotation_enumeration(self):
        # fix2：同行，如X（37个）和Y（35个）的括号数量注释不再打断 enum 提取
        reports = [{"id": "r1", "_meta": {"abstractText": "这显著领先于紧随其后的同行，如信达生物（37个）和中国生物制药（35个）（图9）。此外，管线继续扩张。"}}]
        candidates = fetch.extract_peer_names(reports, "恒瑞医药")
        phrases = {item["query"] for item in candidates if not item["code"]}
        self.assertTrue({"信达生物", "中国生物制药"} <= phrases)

    def test_peer_candidates_extract_coverage_list_with_leadin(self):
        # fix1：覆盖范围内…而言：的覆盖名单是可比池信号
        reports = [{"id": "r1", "_meta": {"abstractText": "相对于其覆盖范围内的其他公司而言：贵州茅台、泸州老窖、蒙牛乳业、千禾味业。其余为概述。"}}]
        candidates = fetch.extract_peer_names(reports, "海天味业")
        phrases = {item["query"] for item in candidates if not item["code"]}
        self.assertTrue({"贵州茅台", "泸州老窖", "千禾味业"} <= phrases)

    def test_peer_candidates_reject_ib_revenue_disclosure(self):
        # fix1 安全闸：投行报酬披露名单（含石化等非同业）不得作为同业候选
        reports = [{"id": "r1", "_meta": {"abstractText": "摩根士丹利预计将从蓝星安迪苏股份有限公司、中国石油化工股份有限公司、宁德时代新能源科技股份有限公司获得投资银行服务相关报酬。"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "宁德时代"), [])

    def test_peer_candidates_reject_broker_entity_list(self):
        # fix1 安全闸：券商自身法律实体名单（杰富瑞系列）不得作为同业候选
        reports = [{"id": "r1", "_meta": {"abstractText": "本报告由与以下机构相关的人员编制：杰富瑞证券有限公司、杰富瑞国际有限公司、杰富瑞有限公司、杰富瑞香港有限公司。"}}]
        self.assertEqual(fetch.extract_peer_names(reports, "招商银行"), [])

    def test_validate_peer_names_fuzzy_only_for_coverage_candidates(self):
        # fuzzy 候选允许归一化包含（“千禾味业食品”对“千禾味业”）；普通候选仍要求完全一致
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            query = (params or {}).get("query")
            if query == "千禾味业食品":
                return {"code": 1, "data": {"hits": [{"entity_id": "603027", "name": "千禾味业"}]}}, None, None
            if query == "五粮液酒":
                return {"code": 1, "data": {"hits": [{"entity_id": "000858", "name": "五粮液"}]}}, None, None
            return {"code": 1, "data": {"hits": []}}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            ok = fetch.validate_peer_names({"stock_search": {"url": "u"}}, [
                {"query": "千禾味业食品", "code": "", "fuzzy": True},
                {"query": "五粮液酒", "code": ""},
            ], "tok")
        finally:
            fetch.call = original
        self.assertEqual([v["code"] for v in ok], ["603027"])

    def test_build_industry_peer_candidates_filters_target_and_st(self):
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            ticker = (params or {}).get('ticker')
            if ticker:
                return {"code": 1, "data": [{"ticker": "600519", "isNew": "1", "industryID3": "010321140501", "secShortName": "贵州茅台"}]}, None, None
            iid = (params or {}).get('industryID3') or (params or {}).get('industryID2')
            if iid == "010321140501":
                return {"code": 1, "data": [
                    {"ticker": "000858", "isNew": "1", "secShortName": "五粮液"},
                    {"ticker": "000568", "isNew": "1", "secShortName": "泸州老窖"},
                    {"ticker": "600519", "isNew": "1", "secShortName": "贵州茅台"},
                    {"ticker": "000799", "isNew": "1", "secShortName": "ST酒鬼"},
                ]}, None, None
            return {"code": 1, "data": []}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            cands = fetch.build_industry_peer_candidates({"getEquIndustry": {"url": "u"}}, "600519", "tok")
        finally:
            fetch.call = original
        queries = [c["query"] for c in cands]
        self.assertNotIn("贵州茅台", queries)
        self.assertNotIn("ST酒鬼", queries)
        self.assertIn("五粮液", queries)
        self.assertTrue(all(c.get("fuzzy") for c in cands))

    def test_validate_peer_names_accepts_exact_name_without_code(self):
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            query = (params or {}).get("query")
            hits = []
            if query == "五粮液":
                hits = [{"entity_id": "000858", "name": "五粮液股份有限公司"}]
            elif query == "泸州老窖":
                hits = [{"entity_id": "000568", "name": "泸州老窖"}]
            return {"code": 1, "data": {"hits": hits}}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            validated = fetch.validate_peer_names({"stock_search": {"url": "u"}}, [
                {"query": "五粮液", "code": ""},
                {"query": "泸州老窖", "code": ""},
            ], "tok")
        finally:
            fetch.call = original
        self.assertEqual([v["code"] for v in validated], ["000858", "000568"])
        self.assertEqual(validated[0]["current_name"], "五粮液股份有限公司")

    def test_validate_peer_names_rejects_name_mismatch(self):
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            return {"code": 1, "data": {"hits": [{"entity_id": "000858", "name": "五粮液"}]}}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            rejected = fetch.validate_peer_names({"stock_search": {"url": "u"}}, [{"query": "五粮液酒", "code": ""}], "tok")
        finally:
            fetch.call = original
        self.assertEqual(rejected, [])

    def test_research_report_content_is_bound_to_its_own_report_id(self):
        contents = {"r1": "正文一", "r2": "正文二"}
        self.assertEqual(fetch._bound_report_content("r1", contents), {"data": {"r1": "正文一"}})
        self.assertEqual(fetch._bound_report_content("r2", contents), {"data": {"r2": "正文二"}})
        self.assertIsNone(fetch._bound_report_content("r3", contents))
    def test_fetch_peer_materials_keeps_only_materials_naming_the_peer(self):
        def fake_call(method, url, token, params=None, body=None, timeout=None):
            question = (body or {}).get("question", "")
            items = [{"id": "m1", "title": "五粮液渠道反馈", "text": "五粮液最新动销"},
                     {"id": "m2", "title": "行业月度数据", "text": "白酒行业整体平稳"}] if "五粮液" in question else []
            return {"code": 1, "data": items}, None, None
        original = fetch.call
        fetch.call = fake_call
        try:
            materials, err = fetch.fetch_peer_materials(
                {"getMaterialsV2": {"url": "u"}}, "贵州茅台",
                [{"code": "000858", "current_name": "五粮液", "query": "五粮液"}], "tok")
        finally:
            fetch.call = original
        self.assertIsNone(err)
        self.assertEqual([item["id"] for item in materials], ["m1"])
        self.assertEqual(materials[0]["peer_code"], "000858")

    def test_incomplete_scenario_subsection_is_removed_without_losing_other_section_nine_content(self):
        md = """## 9 一致预期、盈利预测与估值

### 9.1 市场一致预期

有效的一致预期表格[1]。

### 9.4 情景推演

**核心变量**

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:--|:--|:--|:--|
| 中性 | A | B | C |
| 悲观 | A | B | C |

## 10 风险提示

正文
"""
        result = writer._drop_incomplete_optional_scenarios(md)
        self.assertIn("### 9.1 市场一致预期", result)
        self.assertNotIn("### 9.4 情景推演", result)
        self.assertNotIn("| 中性", result)
        self.assertIn("## 10 风险提示", result)

    def test_orphan_scenario_rows_outside_section_nine_are_removed(self):
        md = """## 7 公司调研大纲

### 7.3 跟踪问题

保留的问题正文。

| 中性 | A | B | C |
|:--|:--|:--|:--|
| 悲观 | A | B | C |

## 10 风险提示

正文
"""
        result = writer._drop_incomplete_optional_scenarios(md)
        self.assertIn("保留的问题正文。", result)
        self.assertNotIn("| 中性", result)
        self.assertNotIn("| 悲观", result)
        self.assertIn("## 10 风险提示", result)
    def test_title_accepts_complete_viewpoint_without_keyword_whitelist(self):
        # “渠道改革重塑价格体系” contains none of the legacy mandatory
        # terms (驱动/修复/改善/放量) but is still a complete viewpoint.
        title = "\u6e20\u9053\u6539\u9769\u91cd\u5851\u4ef7\u683c\u4f53\u7cfb"
        self.assertTrue(writer._valid_title_conclusion(title))

    def test_peer_table_preserves_nine_dimensions_during_normalization(self):
        header = [
            "竞争关系", "公司（代码）", "市场", "可比业务", "行业地位", "相关业务进展",
            "商业模式", "目标客户群体", "核心产品",
        ]
        rows = [
            ["—", "贵州茅台（600519）", "A股", "白酒", "—", "渠道改革[1]", "—", "—", "—"],
            ["直接竞争", "五粮液（000858）", "A股", "白酒", "—", "渠道分类运营[2]", "—", "—", "—"],
        ]
        md = "\n".join([
            "| " + " | ".join(header) + " |",
            "|" + "|".join([":---"] * len(header)) + "|",
            *["| " + " | ".join(row) + " |" for row in rows],
        ])
        normalized = writer._normalize_markdown_tables(md)
        self.assertEqual(len(writer._md_cells(normalized.splitlines()[0])), 9)
        self.assertIn("目标客户群体", normalized)
        self.assertNotIn("市值", normalized)

    def test_ticker_period_null_is_a_nonblocking_annual_fallback(self):
        original = fetch.call
        fetch.call = lambda *args, **kwargs: ({"code": 1, "data": None}, 200, None)
        try:
            self.assertEqual(fetch.get_ticker_period({"ticker_period": {"url": "https://example.test/{ticker}"}}, "600519", "token"), ("A", None, None))
        finally:
            fetch.call = original

    def test_invalid_scenario_uses_source_bound_deterministic_fallback(self):
        key_data = {
            "fact_marker_refs": {
                "F1": {"value": "44%", "ref": 4, "label": "i茅台"},
                "F2": {"value": "4.1万吨", "ref": 22, "label": "基酒"},
            }
        }
        md = """### 9.4 情景推演

**核心变量**
• **渠道**：44%[4]

**情景推演表**：
"""
        result = writer._drop_invalid_section94(md, key_data)
        self.assertIn("**i茅台相关指标**：44%[4]", result)
        self.assertIn("**基酒**：4.1万吨[22]", result)
        self.assertEqual(writer._scenario_target_price_errors(result, key_data), [])
        self.assertNotIn("目标价", result)
        self.assertNotIn("×", result)
    def test_fact_label_uses_numeric_indicator_context(self):
        capacity_share = "12英寸晶圆收入占比达到77.4%，产品结构继续优化。"
        asp = "平均售价为6,667元/片，同比增长2.9%。"
        self.assertEqual(
            writer._infer_operating_fact_label(capacity_share, capacity_share.index("77.4"), "77.4%", "产能"),
            "12英寸晶圆收入占比",
        )
        self.assertEqual(
            writer._infer_operating_fact_label(asp, asp.index("2.9"), "2.9%", "出货"),
            "平均售价同比",
        )

    def test_fact_marker_does_not_glue_two_numeric_assertions(self):
        facts = {"F1": {"value": "537.9万", "ref": 8}}
        self.assertEqual(writer._render_fact_markers("同比增长14.9%{{FACT:F1}}", facts), "同比增长14.9%")
        self.assertEqual(writer._render_fact_markers("产能{{FACT:F1}}", facts), "产能537.9万[8]")

    def test_peer_progress_keeps_column_with_source_bound_target_fallback(self):
        peers = [{"code": "000858", "current_name": "五粮液"}, {"code": "000568", "current_name": "泸州老窖"}]
        table = """| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品 |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| —（基准） | 贵州茅台（600519） | A股 | 白酒 | 龙头 | — | 品牌驱动 | 高端消费 | 茅台酒 |
| 直接竞争 | 五粮液（000858） | A股 | 白酒 | 龙头 | 第八代产品推进渠道分类运营[4] | 品牌驱动 | 商务消费 | 五粮液酒 |
| 直接竞争 | 泸州老窖（000568） | A股 | 白酒 | 品牌厂商 | — | 品牌驱动 | 商务消费 | 国窖1573 |"""
        result = writer._validate_peer_table(
            table, "贵州茅台", "600519", peers, {"000858": [4]}, {3}, "渠道改革推进并优化终端触达[3]"
        )
        self.assertIn("相关业务进展", result)
        self.assertIn("渠道改革推进并优化终端触达[3]", result)
    def test_scenario_cells_allow_year_and_product_generation_numbers(self):
        section = """### 9.4 情景推演
**核心变量**
• **12英寸晶圆收入占比**：77.4%[1]；反映产品结构变化。
• **平均售价同比**：2.9%[2]；反映定价变化。

**情景推演表**：
| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~25%） | 2027年12英寸产品占比继续提升 | 产品结构改善带动利润释放 | 增长预期上修，估值中枢获得支撑。 |
| 中性（概率~50%） | 12英寸产品维持当前推进节奏 | 经营表现围绕既有预期兑现 | 基本面预期稳定，估值围绕当前中枢波动。 |
| 悲观（概率~25%） | 12英寸产品导入节奏放缓 | 收入与利润预期面临下修压力 | 风险偏好下降，估值中枢承受压力。 |
"""
        self.assertEqual(writer._scenario_target_price_errors(section, {}), [])

    def test_unknown_risk_profile_fails_closed_in_fallback(self):
        original = writer._collect_a_share_risk_evidence
        writer._collect_a_share_risk_evidence = lambda *_args, **_kwargs: [
            {"risk_title": "管理层表述变化", "text": "风险提示：管理层表述变化", "ref": 1},
            {"risk_title": "需求不及预期", "text": "风险提示：需求不及预期", "ref": 2},
            {"risk_title": "价格竞争加剧", "text": "风险提示：价格竞争加剧", "ref": 3},
        ]
        try:
            result = writer._build_a_share_risk_fallback({"name": "测试公司"})
        finally:
            writer._collect_a_share_risk_evidence = original
        self.assertNotIn("管理层表述变化", result)
        self.assertNotIn("相关经营指标", result)

    def test_risk_validator_rejects_generic_operating_language(self):
        body = (
            "• **需求不及预期**：若终端订单兑现弱于预期，出货节奏可能放缓；重点跟踪相关经营指标及公司后续披露。[1]\n"
            "• **价格竞争加剧**：若行业供给释放，产品价格与盈利空间可能承压；重点跟踪产品价格、毛利率和市场份额。[2]\n"
            "• **原材料成本波动**：若原材料价格上行，成本传导滞后可能压缩盈利能力；重点跟踪原材料价格、采购成本和毛利率。[3]"
        )
        valid, issues = writer._validate_a_share_risk_body(body)
        self.assertFalse(valid)
        self.assertTrue(any("generic_operating_language" in issue for issue in issues))

    def test_risk_json_rejects_duplicate_theme(self):
        payload = {"risks": [
            {"title": "需求不及预期", "trigger": "终端订单兑现弱于预期", "impact": "出货节奏放缓可能拖累收入", "monitor": "订单、出货和渠道库存", "source_refs": [1]},
            {"title": "销量下滑风险", "trigger": "终端销量低于预期", "impact": "产能利用率和收入承压", "monitor": "销量、产能利用率和库存", "source_refs": [2]},
            {"title": "价格竞争加剧", "trigger": "行业供给释放加快", "impact": "产品价格与盈利空间承压", "monitor": "产品价格、毛利率和市场份额", "source_refs": [3]},
        ]}
        rendered, issues = writer._validate_render_section_10(payload, {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}})
        self.assertEqual(rendered, "")
        self.assertTrue(any("duplicate_theme" in issue for issue in issues))

    def test_risk_profiles_use_concrete_company_indicators(self):
        profile = writer._risk_tracking_profile("产能爬坡与折旧压力")
        self.assertIsNotNone(profile)
        self.assertIn("产能利用率", profile[2])
        self.assertIn("折旧", profile[2])
        self.assertIsNone(writer._risk_tracking_profile("管理层表述变化"))

    def test_risk_length_allows_detailed_source_backed_bullet_up_to_180(self):
        explanation = "若新增产能投放节奏慢于规划，固定成本摊薄和客户导入可能延后，进而影响收入兑现与毛利率改善；重点跟踪新增产能投放、产能利用率、客户认证进度和毛利率指引。"
        line = f"• **产能投放不及预期**：{explanation}[1]"
        self.assertLessEqual(len(__import__('re').sub(r'\[\d+\]|\*\*|\s+', '', line)), 180)
        self.assertTrue(writer._validate_a_share_risk_body("\n".join([line, line.replace('产能投放不及预期', '订单兑现不及预期'), line.replace('产能投放不及预期', '成本传导不及预期')]))[0])

    def test_target_progress_materials_excludes_multi_company_coverage_report(self):
        key_data = {
            "name": "中芯国际集成电路制造有限公司", "short_name": "中芯国际", "ticker": "688981",
            "reports": [
                {"id": "a", "title": "FII：工业富联与中芯国际覆盖报告", "text": "运营费用率下降。"},
                {"id": "b", "title": "中芯国际（688981）：产能利用率维持高位", "text": "公司新增产能投放。"},
            ],
            "ref_map": {"report_a": {"n": 1}, "report_b": {"n": 2}},
        }
        refs, materials = writer._target_progress_materials(key_data)
        self.assertEqual(refs, {2})
        self.assertIn("[2]", materials)
        self.assertNotIn("[1]", materials)

    def test_postprocess_removes_bold_embedded_reference_block(self):
        # 回归：LLM 用加粗 "**参考资料**" 而非 "## 参考资料" 输出章节内嵌引用块，
        # 阶段 0 只匹配 ## 标题，导致内嵌块残留进正文；阶段 0a 必须删除加粗形式内嵌块。
        md = (
            "## 5 产销链分析\n\n"
            "中国大陆收入570.18亿元[1]。\n\n"
            "**参考资料**\n\n"
            "[1]研报 | 2026-09-03 | 华创证券 | 中芯国际半年报点评\n\n"
            "[2]主营构成 | getFdmtMoStdItem\n\n"
            "## 6 公司财务数据分析\n\n"
            "正文[1][2]。\n\n"
            "## 参考资料\n\n"
            "[1]Datayes研报 | 华创证券\n"
            "[2]Datayes结构化接口 | getFdmtMoStdItem\n"
        )
        result = writer._postprocess_report(md, {})
        self.assertNotIn("**参考资料**", result)
        self.assertNotIn("[1]研报 | 2026-09-03", result)
        self.assertIn("## 5 产销链分析", result)
        self.assertIn("## 6 公司财务数据分析", result)
        # 真正的参考资料章节保留且唯一
        self.assertEqual(result.count("## 参考资料"), 1)
if __name__ == "__main__":
    unittest.main()