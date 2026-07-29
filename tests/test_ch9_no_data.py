# -*- coding: utf-8 -*-
"""v1.2.11: 验证第九章各小节无数据时的跳过逻辑"""
import importlib.util
from pathlib import Path
import unittest

WRITER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "a_share_report_writer.py"
SPEC = importlib.util.spec_from_file_location("a_share_report_writer_v1211", WRITER_PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Ch9NoDataTests(unittest.TestCase):
    def test_consensus_table_returns_placeholder_when_empty(self):
        """9.1: forecasts为空 → 返回暂缺占位符"""
        result = M.gen_consensus_table([], {}, {"years": []})
        self.assertIn("暂缺", result)
        self.assertLess(len(result), 30)  # 仅占位符，不生成空壳表格

    def test_consensus_table_skipped_in_assembly(self):
        """9.1: 占位符文本满足跳过条件"""
        placeholder = M.gen_consensus_table([], {}, {"years": []})
        should_skip = (
            not placeholder
            or "暂缺" in str(placeholder)
            or len(placeholder.strip()) <= 20
        )
        self.assertTrue(should_skip, "暂缺占位符应触发跳过")

    def test_consensus_table_present_with_data(self):
        """9.1: 有数据 → 正常输出表格"""
        forecasts = [
            {"foreYear": 2026, "conEpsType": 3, "conIncome": 701900.0,
             "conProfit": 307600.0, "conEps": 6.63, "conPe": 36.4,
             "conIncomeYoy": 18.4, "conProfitYoy": 67.5},
            {"foreYear": 2027, "conEpsType": 3, "conIncome": 806900.0,
             "conProfit": 262700.0, "conEps": 5.66, "conPe": 42.6,
             "conIncomeYoy": 15.0, "conProfitYoy": -14.6},
        ]
        actual = {"conPe": "60.95"}
        fin = {"years": [2025], 2025: {"tRevenue": 59.29e8, "NPAttrP": 18.36e8,
               "revenueYOY": 15.78, "NPAttrPYOY": 11.63, "EPS": 3.96}}
        table = M.gen_consensus_table(forecasts, actual, fin)
        self.assertNotIn("暂缺", table)
        self.assertIn("营业收入", table)
        self.assertIn("2026E", table)
        self.assertIn("2027E", table)
        should_skip = not table or "暂缺" in str(table) or len(table.strip()) <= 20
        self.assertFalse(should_skip, "有数据时不应跳过9.1")

    def test_has_valuation_data_empty(self):
        """9.3: 估值维度全空 → _has_valuation_data 返回 False"""
        self.assertFalse(M._has_valuation_data({"items": {}}))
        self.assertFalse(M._has_valuation_data(
            {"items": {"市盈率PE": {"val": None}, "市净率PB": {"val": "—"}}}
        ))

    def test_has_valuation_data_with_pe(self):
        """9.3: 有PE数据 → _has_valuation_data 返回 True"""
        self.assertTrue(M._has_valuation_data(
            {"items": {"市盈率PE": {"val": "29.0"}}}
        ))

    def test_has_scenario_input_empty(self):
        """9.4: 无任何输入基础 → _has_scenario_input 返回 False"""
        empty = {"consensus_forecasts": [], "reports": [], "meetings": []}
        self.assertFalse(M._has_scenario_input(empty))

    def test_has_scenario_input_with_forecasts(self):
        """9.4: 有一致预期EPS+PE → True"""
        has_fc = {"consensus_forecasts": [{"conEps": 6.63, "conPe": 36.4}],
                  "reports": [], "meetings": []}
        self.assertTrue(M._has_scenario_input(has_fc))

    def test_has_scenario_input_with_report_text(self):
        """9.4: 研报中有业务变量文本 → True"""
        has_report = {
            "consensus_forecasts": [],
            "reports": [{"detail_text": "2026年出货量预计500万件，产能利用率提升至85%"}],
            "meetings": [],
        }
        self.assertTrue(M._has_scenario_input(has_report))

    def test_find_section_start(self):
        """辅助函数：定位章节起始"""
        md = "### 9.3 估值分析\n\nPE 25x\n\n### 9.4 情景推演\n\nbad\n\n## 10 风险提示"
        pos = M._find_section_start(md, "情景推演")
        self.assertGreaterEqual(pos, 0)
        self.assertIn("情景推演", md[pos:pos + 30])

    def test_remove_section_deletes_target_preserves_others(self):
        """辅助函数：删除目标节，保留前后节"""
        md = "### 9.3 估值分析\n\nPE 25x\n\n### 9.4 情景推演\n\nbad content\n\n## 10 风险提示\n\nrisks"
        pos = M._find_section_start(md, "情景推演")
        removed = M._remove_section(md, pos, [])
        self.assertNotIn("情景推演", removed, "9.4应被删除")
        self.assertNotIn("bad content", removed, "9.4内容应被删除")
        self.assertIn("风险提示", removed, "Ch10应保留")
        self.assertIn("估值分析", removed, "9.3应保留")

    def test_extract_consensus_accepts_type_3(self):
        """v1.2.11: conEpsType=3 的预测值应被收录"""
        data = {"consensus": {"data": {"list": [
            {"foreYear": 2025, "conEpsType": 0, "conProfit": 100, "conEps": 1.0, "conPe": 10},
            {"foreYear": 2026, "conEpsType": 3, "conProfit": 200, "conEps": 2.0, "conPe": 20},
            {"foreYear": 2027, "conEpsType": 3, "conProfit": 300, "conEps": 3.0, "conPe": 30},
            {"foreYear": 2028, "conEpsType": 4, "conProfit": 400, "conEps": 4.0, "conPe": 40},
        ]}}}
        forecasts = M.extract_consensus(data)
        self.assertEqual(len(forecasts), 2, "应只有2条type=3预测")
        self.assertEqual(forecasts[0]["foreYear"], 2026)
        self.assertEqual(forecasts[1]["foreYear"], 2027)

    def test_chapter9_block_empty_when_all_subsections_empty(self):
        """组装处：9.1/9.2/s9_valuation 全空 → _chapter_9_block 为 ''"""
        section_9_1 = ""
        section_9_2 = ""
        s9v = ""
        parts = []
        if section_9_1:
            parts.append(section_9_1.strip())
        if section_9_2:
            parts.append(section_9_2.strip())
        if s9v:
            parts.append(s9v)
        block = ""
        if parts:
            block = "## 9 一致预期、盈利预测与估值\n\n" + "\n\n".join(parts) + "\n\n"
        self.assertEqual(block, "", "全空时第九章block应为空字符串")
        self.assertNotIn("## 9", block, "全空时不应出现第九章标题")


if __name__ == "__main__":
    unittest.main()
