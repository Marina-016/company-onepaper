import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

import hk_us_report_writer_v124 as writer
import llm_adapter_v124 as adapter
import check_report_quality_v124 as checker


REF_MAP = {i: {"id": f"a{i}"} for i in range(1, 8)}


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _structure_text() -> str:
    return (_root() / "references" / "hk-us-report-structure.md").read_text(encoding="utf-8")


def _target_materials(n: int = 4, with_consensus: bool = False) -> dict:
    details = [
        {
            "articleId": f"a{i}",
            "orgName": f"Org{i}",
            "targetPrice": 10 + i,
            "rating": "买入" if i % 2 else "增持",
            "publishTime": f"2026-07-0{i}",
            "articleTitle": "Co target price report",
            "textAbstract": f"Co target price and revenue growth assumption {i}",
        }
        for i in range(1, n + 1)
    ]
    materials = {"research": {"details": details}}
    if with_consensus:
        samples = []
        for i in range(1, 4):
            for year, value in [("FY2027E", 100 + i), ("FY2028E", 120 + i), ("FY2029E", 140 + i)]:
                samples.append({
                    "institution": f"Org{i}",
                    "article_id": f"a{i}",
                    "metric": "revenue",
                    "forecast_year": year,
                    "value": value,
                    "unit": "RMB bn",
                    "accounting_basis": "reported",
                    "source_id": f"a{i}",
                    "raw_value": str(value),
                })
            for year, value in [("FY2027E", 10 + i), ("FY2028E", 12 + i), ("FY2029E", 14 + i)]:
                samples.append({
                    "institution": f"Org{i}",
                    "article_id": f"a{i}",
                    "metric": "net profit",
                    "forecast_year": year,
                    "value": value,
                    "unit": "RMB bn",
                    "accounting_basis": "reported",
                    "source_id": f"a{i}",
                    "raw_value": str(value),
                })
        materials["consensus_forecast"] = {
            "samples": samples,
            "aggregation_method": "structured_consensus",
            "sample_count": 5,
            "article_ids": ["a1", "a2", "a3"],
            "rows": [{"metric": "收入", "FY2027E": "100", "FY2028E": "120", "FY2029E": "140"}],
        }
    return materials


class TestR11FTaskContracts(unittest.TestCase):
    def test_active_task_names_are_small(self):
        submitted = []

        def fake_runner(specs, **kwargs):
            submitted.extend(s.task_name for s in specs)
            return [], {"max_workers": 3, "elapsed_seconds": 0, "budget_exceeded": False}

        materials = {"research": {"details": [{"articleId": "a1", "articleTitle": "Co", "orgName": "Org", "publishTime": "2026-07-01", "textAbstract": "Co revenue growth", "targetPrice": "10"}]}}
        source_trace = {"sources": [{"id": "a1", "type": "Datayes Research", "publishTime": "2026-07-01", "organization": "Org", "title": "Co", "api": "batchGetReportContent"}]}
        with patch.object(writer, "run_hkus_llm_tasks", fake_runner), patch.object(writer, "_find_llm_creds", lambda: ("k", "u", "m")), patch.object(writer, "_has_target_materials", lambda *_: True):
            with tempfile.TemporaryDirectory() as d:
                writer.write_report(materials, source_trace, "00001.HK", "hk", "Co", d)
        self.assertNotIn("sections_1_4", submitted)
        self.assertNotIn("sections_3_4", submitted)
        self.assertNotIn("sections_5_7", submitted)
        self.assertNotIn("section_10", submitted)
        for name in ["sections_1_2", "section_3", "section_4", "section_5", "section_6", "section_7", "section_8", "section_10_a", "section_10_b", "section_12"]:
            self.assertIn(name, submitted)

    def test_section10_renders_five_columns(self):
        payload = {"market_debates": [
            {"theme": "A", "bull_view": "收入增长改善盈利", "bull_evidence": "2026Q2收入增长20%", "bull_source_ids": [1], "bear_view": "成本上升压制利润", "bear_evidence": "2026Q2成本增长30%", "bear_source_ids": [2], "validation_metric": "2026Q3收入和毛利率", "validation_window": "2026Q3"},
            {"theme": "B", "bull_view": "订单增长支撑收入", "bull_evidence": "2026H1订单增长15%", "bull_source_ids": [1], "bear_view": "交付延迟影响确认", "bear_evidence": "2026H1交付周期延长10天", "bear_source_ids": [2], "validation_metric": "订单交付周期", "validation_window": "2026H2"},
            {"theme": "C", "bull_view": "新产品放量带动ASP", "bull_evidence": "2026Q2ASP提升8%", "bull_source_ids": [1], "bear_view": "价格竞争压缩ASP", "bear_evidence": "2026Q2竞品降价5%", "bear_source_ids": [2], "validation_metric": "ASP和份额", "validation_window": "2026H2"},
        ]}
        rendered, issues = writer._validate_render_section_10(payload, {1: {"id": "a1"}, 2: {"id": "a2"}})
        self.assertTrue(rendered, issues)
        self.assertIn("| 多头观点 | 多头证据 | 空头观点 | 空头证据 | 需要观察的验证点 |", rendered)
        self.assertNotIn("多：", rendered)
        self.assertNotIn("空：", rendered)

    def test_section12_requires_title_and_explanation(self):
        payload = {"risks": [
            {"title": f"核心变量风险{i}", "explanation": f"若变量{i}低于预期，将影响收入、利润或估值节奏", "source_refs": [1]}
            for i in range(1, 5)
        ]}
        rendered, issues = writer._validate_render_section_12(payload, {1: {"id": "a1"}})
        self.assertTrue(rendered, issues)
        self.assertIn("**核心变量风险1**", rendered)

    def test_section8_hk_us_contract(self):
        payload = {
            "rows": [{"topic": f"关注{i}", "market_concern": "收入增长能否兑现", "verification_metrics": ["收入增速"], "source_refs": [1]} for i in range(3)],
            "research_agenda": [{"topic": f"经营议题{i}", "background": "收入增长和利润率变化是市场核心关切", "questions": ["收入增速多少", "毛利率如何"], "source_refs": [1]} for i in range(3)],
        }
        hk, issues = writer._validate_render_section_8(payload, {1: {"id": "a1"}}, "HK")
        self.assertTrue(hk, issues)
        self.assertIn("市场关注/调研大纲", hk)
        us, issues = writer._validate_render_section_8({"rows": payload["rows"]}, {1: {"id": "a1"}}, "US")
        self.assertTrue(us, issues)
        self.assertNotIn("调研大纲", us)

    def test_valuation_contract_has_no_114_or_institution_table(self):
        materials = {"research": {"details": [
            {"articleId": f"a{i}", "orgName": f"Org{i}", "targetPrice": 10 + i, "rating": "买入", "publishTime": f"2026-07-0{i}", "articleTitle": "Co", "textAbstract": "Co target"}
            for i in range(1, 4)
        ]}}
        ref_map = {i: {"id": f"a{i}"} for i in range(1, 4)}
        old = writer._call_llm
        writer._call_llm = lambda *a, **k: ("[]", True)
        try:
            out = writer._build_valuation_section(materials, ref_map, "Co", "00001.HK", "HK")
        finally:
            writer._call_llm = old
        self.assertIn("### 11.2 估值分析", out)
        self.assertNotIn("### 11.3", out)
        self.assertNotIn("11.4", out)
        self.assertNotIn("机构目标价汇总", out)

    def test_env_task_budget_bounds(self):
        old = os.environ.get("HKUS_LLM_TASK_BUDGET_SECONDS")
        try:
            os.environ["HKUS_LLM_TASK_BUDGET_SECONDS"] = "30"
            self.assertEqual(writer._hkus_llm_task_budget_seconds(), writer.HKUS_LLM_TASK_BUDGET_SECONDS_DEFAULT)
            os.environ["HKUS_LLM_TASK_BUDGET_SECONDS"] = "240"
            self.assertEqual(writer._hkus_llm_task_budget_seconds(), 240)
        finally:
            if old is None:
                os.environ.pop("HKUS_LLM_TASK_BUDGET_SECONDS", None)
            else:
                os.environ["HKUS_LLM_TASK_BUDGET_SECONDS"] = old

    def test_adapter_http_attempts_capped_at_two(self):
        cfg = adapter.LLMConfig(api_key="k", endpoint="https://example.invalid/v1/chat/completions", model="m", api_format="openai")
        calls = {"n": 0}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"choices": [{"message": {"content": ""}}]}).encode()

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise TimeoutError("timeout")

        with patch("urllib.request.urlopen", boom), patch("time.sleep", lambda *_: None):
            result = adapter.call_llm("prompt", config=cfg, timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(calls["n"], 2)


class TestR11FExpandedContracts(unittest.TestCase):
    def _submitted_tasks(self, n_targets: int = 4):
        submitted = []

        def fake_runner(specs, **kwargs):
            submitted.extend(s.task_name for s in specs)
            return [], {"max_workers": 3, "elapsed_seconds": 0, "budget_exceeded": False}

        details = _target_materials(n_targets)["research"]["details"]
        materials = {"research": {"details": details}}
        source_trace = {"sources": [
            {"id": d["articleId"], "type": "Datayes Research", "publishTime": d["publishTime"], "organization": d["orgName"], "title": d["articleTitle"], "api": "batchGetReportContent"}
            for d in details
        ]}
        with patch.object(writer, "run_hkus_llm_tasks", fake_runner), patch.object(writer, "_find_llm_creds", lambda: ("k", "u", "m")), patch.object(writer, "_has_target_materials", lambda *_: True):
            with tempfile.TemporaryDirectory() as d:
                writer.write_report(materials, source_trace, "00001.HK", "hk", "Co", d)
        return submitted

    def test_01_active_path_does_not_submit_sections_1_4(self):
        self.assertNotIn("sections_1_4", self._submitted_tasks())

    def test_02_active_path_does_not_submit_sections_3_4(self):
        self.assertNotIn("sections_3_4", self._submitted_tasks())

    def test_03_active_path_does_not_submit_sections_5_7(self):
        self.assertNotIn("sections_5_7", self._submitted_tasks())

    def test_04_sections_1_2_fields_and_render_boundary(self):
        payload = {
            "title_conclusion": "收入修复驱动估值重估",
            "section_1": {"key_points": [{"keyword": f"K{i}", "text": f"核心投资判断{i}具有明确变量", "source_refs": [1]} for i in range(4)]},
            "section_2": {"recent_updates": [{"keyword": f"U{i}", "date": f"2026-07-0{i}", "fact": f"近况变化{i}包含收入和订单事实", "implication": "对收入和利润预期有影响", "source_refs": [1]} for i in range(1, 4)]},
        }
        rendered, issues = writer._validate_render_sections_1_2(payload, REF_MAP, "Co", "00001.HK")
        self.assertFalse(issues)
        self.assertIn("s12", rendered)
        self.assertIn("## 1", rendered["s12"])
        self.assertIn("## 2", rendered["s12"])
        self.assertEqual(rendered["_title_conclusion"], "收入修复驱动估值重估")

    def test_05_section_3_independent_schema(self):
        payload = {
            "short_term_logic": [{"text": f"短期逻辑{i}围绕订单收入毛利率验证", "source_refs": [1]} for i in range(2)],
            "long_term_logic": [{"text": f"长期逻辑{i}围绕技术渠道生态优势", "source_refs": [2]} for i in range(2)],
            "validation_variables": [{"text": f"验证变量{i}观察收入利润现金流", "source_refs": [3]} for i in range(2)],
        }
        rendered, issues = writer._validate_render_section_3(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertIn("## 3", rendered)
        self.assertNotIn("| 时间 | 事件 | 影响 |", rendered)

    def test_06_section_4_independent_schema(self):
        payload = {"catalysts": [{"time": f"2026-Q{i}", "event": f"核心产品订单发布验证{i}", "impact": f"验证收入和利润率变量{i}", "source_refs": [1]} for i in range(1, 5)]}
        rendered, issues = writer._validate_render_section_4(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertIn("|", rendered)
        self.assertIn("[1]", rendered)

    def test_07_section_5_requires_515253(self):
        s = _structure_text()
        self.assertIn("### 5.1 公司如何赚钱", s)
        self.assertIn("### 5.2 分业务表现", s)
        self.assertIn("### 5.3 业务深度", s)

    def test_08_latest_annual_report_uses_three_year_table(self):
        self.assertIn("若最新财报季为年报", _structure_text())
        self.assertIn("展示近3年年报", _structure_text())

    def test_09_latest_quarter_report_uses_quarter_plus_two_years(self):
        self.assertIn("若最新财报季不是年报", _structure_text())
        self.assertIn("展示最新季报 + 前两年年报", _structure_text())

    def test_10_all_empty_year_columns_are_hidden(self):
        self.assertIn("年份列隐藏规则", _structure_text())

    def test_11_actual_and_estimate_not_mixed(self):
        s = _structure_text()
        self.assertIn("Actual", s)
        self.assertIn("Estimate", s)
        self.assertIn("必须分开写", s)

    def test_12_four_quarter_sum_requires_estimated_label(self):
        self.assertIn("加总估算", _structure_text())

    def test_13_no_segment_switches_to_kpi_table(self):
        s = _structure_text()
        self.assertIn("若公司没有分业务收入", s)
        self.assertIn("KPI 表", s)

    def test_14_section_6_three_column_table(self):
        s = _structure_text()
        self.assertIn("| 类型（主要客户/主要供应商/核心资源） | 名称 | 合作情况/规模/占比 |", s)

    def test_15_hk_section_8_contains_research_agenda(self):
        payload = {
            "rows": [{"topic": f"关注{i}", "market_concern": "市场担心收入兑现持续性", "verification_metrics": ["收入增速"], "source_refs": [1]} for i in range(3)],
            "research_agenda": [{"topic": f"经营议题{i}", "background": "市场关注收入和利润率变化趋势", "questions": ["收入增速多少", "毛利率如何"], "source_refs": [1]} for i in range(3)],
        }
        rendered, issues = writer._validate_render_section_8(payload, REF_MAP, "HK")
        self.assertFalse(issues)
        self.assertIn("/", rendered)

    def test_16_us_section_8_excludes_research_agenda(self):
        rows = [{"topic": f"关注{i}", "market_concern": "市场担心收入兑现持续性", "verification_metrics": ["收入增速"], "source_refs": [1]} for i in range(3)]
        rendered, issues = writer._validate_render_section_8({"rows": rows}, REF_MAP, "US")
        self.assertFalse(issues)
        self.assertNotIn("议题", rendered)

    def test_17_section10_ab_merge_to_five_column_table(self):
        rows_a, _ = writer._normalize_section_10_rows({"market_debates": [self._debate("A", 1), self._debate("B", 2)]}, REF_MAP)
        rows_b, _ = writer._normalize_section_10_rows({"market_debates": [self._debate("C", 3)]}, REF_MAP)
        rendered, ok, issues = writer.merge_hkus_section_10_parts([rows_a, rows_b])
        self.assertTrue(ok, issues)
        header = next(line for line in rendered.splitlines() if line.startswith("|") and "---" not in line)
        self.assertEqual(len([c for c in header.split("|")[1:-1]]), 5)

    def test_18_section10_duplicate_theme_filtered(self):
        rows_a, _ = writer._normalize_section_10_rows({"market_debates": [self._debate("A", 1)]}, REF_MAP)
        rows_b, _ = writer._normalize_section_10_rows({"market_debates": [self._debate("A", 2), self._debate("B", 3)]}, REF_MAP)
        _, _, issues = writer.merge_hkus_section_10_parts([rows_a, rows_b])
        self.assertTrue(any("duplicate_theme" in x for x in issues))

    def test_19_section10_refs_stay_in_bull_and_bear_evidence(self):
        rendered, issues = writer._validate_render_section_10({"market_debates": [self._debate("A", 1), self._debate("B", 2), self._debate("C", 3)]}, REF_MAP)
        self.assertFalse(issues)
        first_data = [line for line in rendered.splitlines() if line.startswith("|")][2]
        cells = [c.strip() for c in first_data.split("|")[1:-1]]
        self.assertNotIn("[1]", cells[0])
        self.assertIn("[1]", cells[1])
        self.assertIn("[2]", cells[3])

    def test_20_111_structured_consensus_priority(self):
        text, meta = writer._build_consensus_forecast_section(_target_materials(3, with_consensus=True), [], {}, "港元")
        self.assertIn("institution_forecast_median", meta["aggregation_method"])
        self.assertIn("1020", text)

    def test_21_three_institution_revenue_forecast_median(self):
        text, meta = writer._build_consensus_forecast_section(_target_materials(3, with_consensus=True), [], {}, "港元")
        self.assertEqual(meta["aggregation_method"], "institution_forecast_median")
        self.assertTrue(all(row["sample_count"] == 3 for row in meta["rows"]))
        self.assertTrue(text)

    def test_22_even_sample_median_correct(self):
        self.assertEqual(writer.calculate_target_price_stats([10, 20, 30, 40])["median"], 25)

    def test_23_less_than_three_does_not_make_fake_consensus(self):
        materials = _target_materials(2, with_consensus=True)
        materials["consensus_forecast"]["samples"] = materials["consensus_forecast"]["samples"][:2]
        text, meta = writer._build_consensus_forecast_section(materials, [], {}, "港元")
        self.assertFalse(text)
        self.assertEqual(meta["sample_count"], 0)

    def test_24_dynamic_future_three_fiscal_years(self):
        labels = writer._forecast_year_labels({"consensus_forecast": {"samples": [{"institution": "A", "article_id": "a", "metric": "revenue", "forecast_year": "FY2028E", "value": 1, "unit": "RMB", "accounting_basis": "reported"}]}})
        self.assertEqual(labels, ["FY2028E", "FY2029E", "FY2030E"])

    def test_25_consensus_forecast_keeps_sample_org_and_article_ids(self):
        _, meta = writer._build_consensus_forecast_section(_target_materials(3, with_consensus=True), [], {}, "港元")
        self.assertTrue(all(row["sample_count"] == 3 for row in meta["rows"]))
        self.assertEqual(meta["article_ids"], ["a1", "a2", "a3"])

    def test_26_112_keeps_only_sourced_valuation_dimensions(self):
        sec = writer._build_valuation_section(_target_materials(3), REF_MAP, "Co", "00001.HK", "HK", basis_map={})
        self.assertIn("### 11.2", sec)
        self.assertNotIn("DCF", sec)
        self.assertNotIn("SOTP", sec)

    def test_27_113_hidden_when_fewer_than_three_variables(self):
        sec = writer._build_valuation_section(_target_materials(2), REF_MAP, "Co", "00001.HK", "HK", basis_map={})
        self.assertNotIn("### 11.3", sec)

    def test_28_internal_calculation_without_formula_is_removed(self):
        sec = writer._build_valuation_section(_target_materials(3), REF_MAP, "Co", "00001.HK", "HK", basis_map={})
        self.assertNotIn("内部测算", sec)

    def test_29_section12_bold_title_explanation_not_bold(self):
        payload = {"risks": [{"title": f"收入兑现风险{i}", "explanation": "若订单交付低于预期，将影响收入和利润率", "source_refs": [1]} for i in range(4)]}
        rendered, issues = writer._validate_render_section_12(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertRegex(rendered, r"- \*\*[^*]+\*\*")
        self.assertNotRegex(rendered, r"\*\*[^*]+：[^*]+\*\*")

    def test_30_504_same_scale_attempts_two(self):
        cfg = adapter.LLMConfig(api_key="k", endpoint="https://example.invalid/v1/chat/completions", model="m", api_format="openai")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")), patch("time.sleep", lambda *_: None):
            result = adapter.call_llm("prompt", config=cfg, timeout=1)
        self.assertEqual(result.attempt_count, 2)

    def test_31_task_budget_exhausted_skips_retry(self):
        records = [{"article_id": f"a{i}", "org": f"O{i}", "target": 10 + i, "date": "2026-01-01", "evidence": "e"} for i in range(3)]
        old = writer._call_llm
        try:
            writer._call_llm = lambda *a, **k: ("[]", True)
            with patch.object(writer, "_hkus_llm_task_budget_seconds", lambda: 0):
                out = writer._extract_target_price_basis(records)
        finally:
            writer._call_llm = old
        self.assertEqual(set(out.keys()), {"a0", "a1", "a2"})

    def test_32_hkus_d1_checks_section_11_only(self):
        self.assertIn('valuation_section_no = 9 if str(market).upper() == "A" else 11', Path(checker.__file__).read_text(encoding="utf-8"))

    def test_33_target_price_basis_requires_at_least_three_records_in_active_path(self):
        self.assertNotIn("target_price_basis", self._submitted_tasks(2))
        self.assertIn("target_price_basis", self._submitted_tasks(3))

    def test_34_target_price_basis_selector_limits_records(self):
        selected = writer._select_target_price_basis_records(writer._collect_target_price_records(_target_materials(8), {i: {"id": f"a{i}"} for i in range(1, 9)}, "Co", "00001.HK", "HK"))
        self.assertLessEqual(len(selected), writer.TARGET_PRICE_BASIS_MAX_RECORDS)
        self.assertGreaterEqual(len(selected), 3)

    def test_35_target_price_records_do_not_enter_111_forecast_samples(self):
        targets = writer._collect_target_price_records(_target_materials(4), REF_MAP, "Co", "00001.HK", "HK")
        stats = writer.calculate_target_price_stats([x["target"] for x in targets])
        text, meta = writer._build_consensus_forecast_section({}, targets, stats, "HKD")
        self.assertFalse(text)
        self.assertEqual(meta["rows"], [])

    def test_36_same_institution_duplicate_report_not_double_counted(self):
        materials = _target_materials(3, with_consensus=True)
        dup = dict(materials["consensus_forecast"]["samples"][0])
        materials["consensus_forecast"]["samples"].append(dup)
        _, meta = writer._build_consensus_forecast_section(materials)
        first = meta["rows"][0]
        self.assertEqual(first["sample_count"], 3)

    def test_37_gaap_and_non_gaap_are_not_mixed(self):
        samples = []
        for i in range(1, 4):
            samples.append({"institution": f"GAAP{i}", "article_id": f"g{i}", "metric": "EPS", "forecast_year": "FY2028E", "value": i, "unit": "USD/share", "accounting_basis": "GAAP"})
            samples.append({"institution": f"NG{i}", "article_id": f"n{i}", "metric": "EPS", "forecast_year": "FY2028E", "value": i + 10, "unit": "USD/share", "accounting_basis": "Non-GAAP"})
        _, meta = writer._build_consensus_forecast_section({"consensus_forecast": {"samples": samples}})
        bases = {row["accounting_basis"] for row in meta["rows"]}
        self.assertEqual(bases, {"GAAP", "Non-GAAP"})

    def test_38_hkd_and_usd_are_not_mixed(self):
        samples = []
        for i in range(1, 4):
            samples.append({"institution": f"HK{i}", "article_id": f"h{i}", "metric": "revenue", "forecast_year": "FY2028E", "value": i, "unit": "HKD mn", "accounting_basis": "reported"})
            samples.append({"institution": f"US{i}", "article_id": f"u{i}", "metric": "revenue", "forecast_year": "FY2028E", "value": i + 10, "unit": "USD mn", "accounting_basis": "reported"})
        _, meta = writer._build_consensus_forecast_section({"consensus_forecast": {"samples": samples}})
        units = {row["unit"] for row in meta["rows"]}
        self.assertEqual(units, {"HKD亿元", "USD亿元"})

    def test_39_fy_and_cy_are_not_mixed(self):
        samples = []
        for i in range(1, 4):
            samples.append({"institution": f"FY{i}", "article_id": f"f{i}", "metric": "revenue", "forecast_year": "FY2028E", "value": i, "unit": "RMB", "accounting_basis": "reported"})
            samples.append({"institution": f"CY{i}", "article_id": f"c{i}", "metric": "revenue", "forecast_year": "CY2028E", "value": i + 10, "unit": "RMB", "accounting_basis": "reported"})
        _, meta = writer._build_consensus_forecast_section({"consensus_forecast": {"samples": samples}})
        years = {row["forecast_year"] for row in meta["rows"]}
        self.assertEqual(years, {"FY2028E", "CY2028E"})

    def test_40_consensus_forecast_json_written_by_write_report(self):
        submitted = []

        def fake_runner(specs, **kwargs):
            submitted.extend(s.task_name for s in specs)
            return [], {"max_workers": 3, "elapsed_seconds": 0, "budget_exceeded": False}

        materials = _target_materials(3, with_consensus=True)
        source_trace = {"sources": [{"id": f"a{i}", "type": "Datayes Research", "publishTime": f"2026-07-0{i}", "organization": f"Org{i}", "title": "Co", "api": "batchGetReportContent"} for i in range(1, 4)]}
        with patch.object(writer, "run_hkus_llm_tasks", fake_runner), patch.object(writer, "_find_llm_creds", lambda: ("k", "u", "m")), patch.object(writer, "_has_target_materials", lambda *_: True):
            with tempfile.TemporaryDirectory() as d:
                writer.write_report(materials, source_trace, "00001.HK", "hk", "Co", d)
                payload = json.loads((Path(d) / "consensus_forecast.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["rows"])
        self.assertEqual(payload["rows"][0]["article_id"], ["a1", "a2", "a3"])

    def test_41_112_still_uses_target_price_stats(self):
        sec = writer._build_valuation_section(_target_materials(3, with_consensus=True), REF_MAP, "Co", "00001.HK", "HK", basis_map={})
        self.assertIn("### 11.2", sec)
        self.assertIn("11", sec)

    def test_42_113_can_read_consensus_and_target_assumptions(self):
        basis = {"a1": {"key_assumptions": "revenue growth"}, "a2": {"key_assumptions": "margin expansion"}, "a3": {"key_assumptions": "cash flow"}}
        sec = writer._build_valuation_section(_target_materials(3, with_consensus=True), REF_MAP, "Co", "00001.HK", "HK", basis_map=basis)
        self.assertIn("### 11.1", sec)
        self.assertIn("### 11.3", sec)

    def test_43_forecast_sample_output_has_required_machine_fields(self):
        _, meta = writer._build_consensus_forecast_section(_target_materials(3, with_consensus=True))
        row = meta["rows"][0]
        for key in ("forecast_year", "metric", "unit", "accounting_basis", "aggregation_method", "sample_count", "consensus_value", "institution", "article_id", "source_id"):
            self.assertIn(key, row)
        self.assertIn("raw_value", row["samples"][0])

    def test_44_target_price_basis_not_required_for_111(self):
        sec = writer._build_valuation_section(_target_materials(0, with_consensus=True), {}, "Co", "00001.HK", "HK", basis_map={})
        self.assertIn("### 11.1", sec)
        self.assertIn("### 11.2", sec)
        self.assertNotIn("### 11.3", sec)

    @staticmethod
    def _debate(theme: str, ref: int) -> dict:
        return {
            "theme": f"{theme}收入分歧",
            "bull_view": f"{theme}收入增长改善盈利能力",
            "bull_evidence": f"2026Q{ref}收入增长{10 + ref}%",
            "bull_source_ids": [1],
            "bear_view": f"{theme}成本上升压制利润率",
            "bear_evidence": f"2026Q{ref}成本增长{20 + ref}%",
            "bear_source_ids": [2],
            "validation_metric": f"2026Q{ref}收入和毛利率",
            "validation_window": f"2026Q{ref}",
        }


if __name__ == "__main__":
    unittest.main()
