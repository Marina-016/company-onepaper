import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS))

import hk_us_report_writer_v124 as writer


REF_MAP = {i: {"id": f"a{i}"} for i in range(1, 8)}


def _materials(with_forecast: bool = True) -> dict:
    details = [
        {"articleId": f"a{i}", "orgName": f"Org{i}", "targetPrice": 10 + i, "publishTime": f"2026-07-0{i}", "articleTitle": "Co", "textAbstract": "Co revenue growth margin cash flow"}
        for i in range(1, 4)
    ]
    materials = {"research": {"details": details}}
    if with_forecast:
        samples = []
        for i in range(1, 4):
            for metric, base in (("revenue", 100), ("net profit", 10)):
                for year, add in (("FY2027E", 0), ("FY2028E", 20)):
                    samples.append({
                        "institution": f"Org{i}",
                        "article_id": f"a{i}",
                        "metric": metric,
                        "forecast_year": year,
                        "value": base + add + i,
                        "unit": "RMB bn",
                        "accounting_basis": "reported",
                        "source_id": f"a{i}",
                        "raw_value": str(base + add + i),
                    })
        materials["consensus_forecast"] = {"samples": samples}
    return materials


class TestR11GWriterRefactor(unittest.TestCase):
    def test_01_active_function_names_are_not_duplicated(self):
        tree = ast.parse((SCRIPTS / "hk_us_report_writer_v124.py").read_text(encoding="utf-8"))
        seen = {}
        duplicates = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name in seen:
                    duplicates.setdefault(node.name, [seen[node.name]]).append(node.lineno)
                else:
                    seen[node.name] = node.lineno
        self.assertFalse(duplicates)

    def test_02_legacy_combined_tasks_not_in_active_submission(self):
        submitted = []

        def fake_runner(specs, **kwargs):
            submitted.extend(s.task_name for s in specs)
            return [], {"max_workers": 3, "elapsed_seconds": 0, "budget_exceeded": False}

        source_trace = {"sources": [{"id": f"a{i}", "type": "Datayes Research", "publishTime": f"2026-07-0{i}", "organization": f"Org{i}", "title": "Co", "api": "batchGetReportContent"} for i in range(1, 4)]}
        with patch.object(writer, "run_hkus_llm_tasks", fake_runner), patch.object(writer, "_find_llm_creds", lambda: ("k", "u", "m")), patch.object(writer, "_has_target_materials", lambda *_: True):
            with tempfile.TemporaryDirectory() as d:
                writer.write_report(_materials(), source_trace, "00001.HK", "hk", "Co", d)
        self.assertIn("forecast_sample_extraction", submitted)
        for legacy in ("sections_1_4", "sections_3_4", "sections_5_7", "section_10"):
            self.assertNotIn(legacy, submitted)

    def test_03_title_never_uses_generic_template(self):
        title = writer._build_report_title("Co", "00001.HK", "港股", "")
        self.assertEqual(title, "# Co（00001.HK）港股公司一页纸")
        self.assertNotIn("核心主业稳健", title)
        self.assertNotIn("新业务打开成长空间", title)

    def test_04_section3_has_no_33_or_validation_variables(self):
        payload = {
            "short_term_logic": [{"title": f"S{i}", "text": f"短期逻辑{i}围绕收入和订单兑现", "source_refs": [1]} for i in range(2)],
            "long_term_logic": [{"title": f"L{i}", "text": f"长期逻辑{i}围绕技术和客户粘性", "source_refs": [2]} for i in range(2)],
            "validation_variables": [{"text": "不应渲染", "source_refs": [1]}],
        }
        rendered, issues = writer._validate_render_section_3(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertNotIn("3.3", rendered)
        self.assertNotIn("验证变量", rendered)

    def test_05_section5_json_renderer_fixed_h3(self):
        payload = {
            "business_model": {"text": "公司通过核心产品销售、服务订阅和生态变现形成收入，利润池主要来自高毛利产品线，并由客户续约和产品升级共同驱动增长。", "source_refs": [1]},
            "performance_mode": "kpi_table",
            "kpi_rows": [
                {"business": "业务A", "metric": "收入", "period": "FY2025", "value": "100亿元", "source_refs": [1]},
                {"business": "业务B", "metric": "用户", "period": "FY2025", "value": "200万", "source_refs": [2]},
            ],
            "deep_dives": [{"business": "业务A", "conclusion": "利润弹性更强", "text": "业务A增长驱动来自客户扩张和产品升级，能够同时影响收入确认、利润率和估值弹性。", "source_refs": [1]}],
        }
        rendered, issues = writer._validate_render_section_5(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertIn("### 5.1 公司如何赚钱", rendered)
        self.assertIn("### 5.2 分业务表现", rendered)
        self.assertIn("### 5.3 业务深度", rendered)

    def test_06_section6_table_or_fallback_no_default_chain(self):
        payload = {"rows": [
            {"type": "主要客户", "name": "客户A", "relationship": "贡献订单增长并影响收入确认", "source_refs": [1]},
            {"type": "核心资源", "name": "平台B", "relationship": "支撑产品交付和客户留存", "source_refs": [2]},
        ]}
        rendered, issues = writer._validate_render_section_6(payload, REF_MAP)
        self.assertFalse(issues)
        self.assertIn("| 类型（主要客户/主要供应商/核心资源） | 名称 | 合作情况/规模/占比 |", rendered)
        self.assertNotIn("上游", rendered)
        self.assertNotIn("核心验证变量", rendered)

    def test_07_section8_market_aware_title(self):
        payload = {
            "rows": [{"topic": f"关注{i}", "market_concern": "收入增长和利润率改善能否在后续财报兑现", "verification_metrics": ["收入增速"], "source_refs": [1]} for i in range(3)],
            "research_agenda": [{"topic": f"经营议题{i}", "background": "收入和利润率变化是核心关切", "questions": ["收入如何", "毛利率如何"], "source_refs": [1]} for i in range(3)],
        }
        hk, _ = writer._validate_render_section_8(payload, REF_MAP, "HK")
        us, _ = writer._validate_render_section_8({"rows": payload["rows"]}, REF_MAP, "US")
        self.assertIn("## 8 市场关注/调研大纲", hk)
        self.assertIn("## 8 市场关注", us)
        self.assertNotIn("调研大纲", us)

    def test_08_section10_compresses_evidence_without_prefix(self):
        text = "多：2026Q2收入增长20%；2026Q2毛利率提升3个百分点；2026Q2订单增长15%；额外句子不应保留。"
        out = writer._compress_evidence_sentences(text, max_sentences=3, max_chars=60)
        self.assertNotIn("多：", out)
        self.assertLessEqual(len(out), 60)
        self.assertFalse(out.endswith("，"))

    def test_09_forecast_samples_do_not_use_target_price_records(self):
        text, meta = writer._build_consensus_forecast_section({"research": {"details": _materials(False)["research"]["details"]}})
        self.assertFalse(text)
        self.assertEqual(meta["rows"], [])

    def test_10_forecast_aggregation_requires_three_institutions(self):
        materials = _materials(True)
        materials["consensus_forecast"]["samples"] = [x for x in materials["consensus_forecast"]["samples"] if x["institution"] != "Org3"]
        text, meta = writer._build_consensus_forecast_section(materials)
        self.assertFalse(text)
        self.assertEqual(meta["rows"], [])

    def test_11_forecast_aggregation_outputs_earnings_forecast_title(self):
        text, meta = writer._build_consensus_forecast_section(_materials(True))
        self.assertIn("### 11.1 盈利预测", text)
        self.assertGreaterEqual(len(meta["rows"]), 2)

    def test_12_valuation_hides_scenario_when_variables_insufficient(self):
        sec = writer._build_valuation_section(_materials(False), REF_MAP, "Co", "00001.HK", "HK", basis_map={})
        self.assertIn("### 11.2 估值分析", sec)
        self.assertNotIn("### 11.3", sec)
        self.assertNotIn("目标价中位数", "\n".join(re for re in sec.splitlines() if "核心变量" in re))

    def test_13_consensus_machine_payload_has_required_fields(self):
        samples = writer._extract_forecast_samples(_materials(True))
        payload = writer._forecast_samples_payload(samples)
        row = payload["samples"][0]
        for key in ("institution", "article_id", "source_id", "metric", "forecast_year", "currency", "unit", "accounting_basis", "period_basis", "raw_value", "standardized_value", "conversion_formula"):
            self.assertIn(key, row)

    def test_14_checker_default_skipped_manifest_mode(self):
        with tempfile.TemporaryDirectory() as d:
            writer._update_run_manifest(d, stage="startup", status="running")
            writer._set_run_manifest_field(d, "checker_mode", "skipped_by_default")
            manifest = json.loads((Path(d) / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["checker_mode"], "skipped_by_default")


if __name__ == "__main__":
    unittest.main()
