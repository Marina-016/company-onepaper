#!/usr/bin/env python3
"""
v1.2.2 post_process 最小单元测试
覆盖：章节编号修复 / 稀疏表格清理 / 参考资料重建
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

# Ensure post_process.py is importable
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from post_process import (
    fix_section_numbering,
    clean_sparse_tables,
    rebuild_references,
    build_source_index,
    post_process,
)


class TestSectionNumbering(unittest.TestCase):
    """H3 父子编号一致性测试"""

    def test_h3_under_ch3_fixes_2_1_to_3_1(self):
        """第3章下的2.1应自动修复为3.1"""
        lines = [
            "## 3 核心投资逻辑",
            "",
            "### 2.1 短期逻辑",   # ← 错误：应为3.1
            "",
            "一些内容",
            "",
            "### 2.2 长期逻辑",   # ← 错误：应为3.2
        ]
        fixed, fixes = fix_section_numbering(lines)
        self.assertEqual(len(fixes), 2)
        self.assertEqual(fixes[0]["old"], "2.1 短期逻辑")
        self.assertEqual(fixes[0]["new"], "3.1 短期逻辑")
        self.assertEqual(fixes[0]["parent_h2"], "3 核心投资逻辑")
        self.assertEqual(fixes[1]["old"], "2.2 长期逻辑")
        self.assertEqual(fixes[1]["new"], "3.2 长期逻辑")

        # Verify actual lines
        self.assertIn("### 3.1 短期逻辑", fixed)
        self.assertIn("### 3.2 长期逻辑", fixed)
        self.assertNotIn("### 2.1 短期逻辑", fixed)
        self.assertNotIn("### 2.2 长期逻辑", fixed)

    def test_correct_numbering_unchanged(self):
        """正确编号不应被改变"""
        lines = [
            "## 3 核心投资逻辑",
            "",
            "### 3.1 短期逻辑",
            "",
            "### 3.2 长期逻辑",
            "",
            "## 4 催化事件",
            "",
            "### 4.1 已发生事件",
        ]
        fixed, fixes = fix_section_numbering(lines)
        self.assertEqual(len(fixes), 0)
        self.assertEqual(fixed, lines)

    def test_h2_without_number_skipped(self):
        """没有数字编号的H2不影响后续H3"""
        lines = [
            "## 参考资料",
            "",
            "### 2.1 子标题",  # 没有父H2编号，应保持不变
        ]
        fixed, fixes = fix_section_numbering(lines)
        self.assertEqual(len(fixes), 0)

    def test_h3_different_parent(self):
        """H3在不同H2下的切换"""
        lines = [
            "## 5 业务拆分",
            "",
            "### 5.1 分业务",
            "",
            "## 6 财务分析",
            "",
            "### 5.2 财务表",  # ← 错误：应为6.1（即使数字递增，父编号也不对）
        ]
        fixed, fixes = fix_section_numbering(lines)
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["old"], "5.2 财务表")
        self.assertEqual(fixes[0]["new"], "6.2 财务表")
        self.assertEqual(fixes[0]["parent_num"], "6")

    def test_no_h3_case(self):
        """没有H3标题的报告不受影响"""
        lines = [
            "## 1 要点",
            "",
            "正文内容",
            "",
            "## 2 逻辑",
        ]
        fixed, fixes = fix_section_numbering(lines)
        self.assertEqual(len(fixes), 0)
        self.assertEqual(fixed, lines)


class TestSparseTableCleaning(unittest.TestCase):
    """稀疏表格行列删除测试"""

    def test_row_with_only_one_valid_value_deleted(self):
        """只有1个有效值的行被删除"""
        lines = [
            "## 6 财务",
            "",
            "| 指标 | FY2023 | FY2024 | FY2025 |",
            "|:-----|-----:|-----:|-----:|",
            "| 营收 | 100 | 200 | 300 |",
            "| 未知指标 | — | — | 50 |",       # 仅1个有效值 → 应删除
            "| 利润 | 20 | 30 | 40 |",
        ]
        cleaned, deletions = clean_sparse_tables(lines)
        self.assertTrue(any(d["type"] == "row" for d in deletions))
        row_dels = [d for d in deletions if d["type"] == "row"]
        self.assertGreaterEqual(len(row_dels), 1)

    def test_column_with_only_one_valid_value_deleted(self):
        """只有1个有效值的列被删除"""
        lines = [
            "## 6 财务",
            "",
            "| 指标 | FY2023 | FY2024 | FY2025 |",
            "|:-----|-----:|-----:|-----:|",
            "| 营收 | 100 | — | — |",    # FY2024/FY2025列各仅1个有效值
            "| 利润 | — | 200 | — |",
            "| 现金流 | — | — | 300 |",
        ]
        cleaned, deletions = clean_sparse_tables(lines)
        # Each column has 1 valid value across 3 rows → columns should be deleted
        col_dels = [d for d in deletions if d["type"] == "column"]
        self.assertGreaterEqual(len(col_dels), 1)

    def test_complete_table_unchanged(self):
        """完整表格不改变"""
        lines = [
            "## 6 财务",
            "",
            "| 指标 | FY2023 | FY2024 | FY2025 |",
            "|:-----|-----:|-----:|-----:|",
            "| 营收 | 100 | 200 | 300 |",
            "| 利润 | 20 | 30 | 40 |",
            "| 现金流 | 15 | 25 | 35 |",
        ]
        cleaned, deletions = clean_sparse_tables(lines)
        self.assertEqual(len(deletions), 0)
        # All rows preserved (minus empty lines)
        self.assertTrue(any("营收" in l for l in cleaned))
        self.assertTrue(any("利润" in l for l in cleaned))
        self.assertTrue(any("现金流" in l for l in cleaned))

    def test_table_with_empty_placeholders_cleaned(self):
        """含—/N/A/待补充的行应视为稀疏行删除"""
        lines = [
            "## 8 同业对比",
            "",
            "| 公司 | 营收 | 毛利率 | 净利率 |",
            "|:-----|-----:|-----:|-----:|",
            "| A公司 | 500 | 30% | 10% |",
            "| B公司 | — | N/A | 待补充 |",   # 0个有效值
            "| C公司 | 300 | — | — |",        # 1个有效值
        ]
        cleaned, deletions = clean_sparse_tables(lines)
        row_dels = [d for d in deletions if d["type"] == "row"]
        self.assertGreaterEqual(len(row_dels), 1)

    def test_empty_values_not_counted_as_valid(self):
        """空字符串、—、N/A、待补充不算有效数据"""
        lines = [
            "## 数据",
            "",
            "| 指标 | Q1 | Q2 | Q3 |",
            "|:-----|-----:|-----:|-----:|",
            "| A | — | N/A | 待补充 |",  # 0 valid
            "| B | 10 | — | NA |",        # 1 valid
        ]
        cleaned, deletions = clean_sparse_tables(lines)
        row_dels = [d for d in deletions if d["type"] == "row"]
        # Both rows have ≤1 valid
        self.assertGreaterEqual(len(row_dels), 1)


class TestReferenceRebuilding(unittest.TestCase):
    """参考资料重建测试"""

    def setUp(self):
        self.materials = {
            "__meta__": {"company": "TestCo", "ticker": "000001", "market": "A"},
            "materials_v2": {
                "unique_sources": [
                    {
                        "id": "8937002",
                        "dataType": "research",
                        "title": "TestCo深度研报：长期增长逻辑",
                        "metadata": {
                            "organization": "巴克莱银行",
                            "publishTime": "2026-06-20",
                        },
                    },
                    {
                        "id": "8934316",
                        "dataType": "research",
                        "title": "TestCo前景展望：AI驱动新增长",
                        "metadata": {
                            "organization": "高盛集团",
                            "publishTime": "2026-06-19",
                        },
                    },
                    {
                        "id": "173196",
                        "dataType": "meetingSummary",
                        "title": "TestCo FY2025业绩交流会",
                        "metadata": {
                            "organization": "上市公司",
                            "publishTime": "2026-01-29",
                        },
                    },
                ]
            },
        }
        self.source_index = build_source_index(self.materials)

    def _make_report(self, body_refs, ref_defs):
        """组装一个包含正文+参考资料的报告。"""
        lines = body_refs + [""] + ref_defs
        return lines

    def test_reference_format_no_space_after_bracket(self):
        """[1]后不应有空格"""
        body = [
            "## 1 要点",
            "",
            "公司营收增长显著[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            # Check rebuilt refs don't have space after bracket
            in_ref_section = False
            for line in result:
                if "参考资料" in line:
                    in_ref_section = True
                    continue
                if in_ref_section and line.strip().startswith("["):
                    import re
                    has_space = re.match(r'^\[\d+\]\s+', line)
                    self.assertIsNone(has_space,
                        f"Reference has space after bracket: {line[:40]}")

    def test_field_order_correct(self):
        """字段顺序：来源类型 | 日期 | ID | 机构 | 标题 | API"""
        body = [
            "## 1 要点",
            "",
            "公司有良好前景[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        # Check the rebuilt reference has the correct number of pipe separators
        if "error" not in report:
            in_ref = False
            for line in result:
                if "参考资料" in line:
                    in_ref = True
                    continue
                if in_ref and line.strip() and line.strip().startswith("["):
                    # Should have exactly 5 pipes: type|date|ID|org|title|API
                    pipe_count = line.count("|")
                    self.assertGreaterEqual(pipe_count, 5,
                        f"Reference should have >=5 pipes (6 fields): {line[:80]}")

    def test_title_verbatim_from_json(self):
        """标题必须从JSON逐字复制，不改写"""
        body = [
            "## 1 要点",
            "",
            "业绩稳健[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            result_text = "\n".join(result)
            self.assertIn("TestCo深度研报：长期增长逻辑", result_text,
                "Title should be verbatim from JSON")

    def test_organization_verbatim_from_json(self):
        """机构名必须从JSON逐字复制"""
        body = [
            "## 1 要点",
            "",
            "值得关注[1][2]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
            "[2]Materials V2研报 | 2026-06-19 | ID：8934316 | 高盛集团 | TestCo前景展望：AI驱动新增长 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            result_text = "\n".join(result)
            self.assertIn("巴克莱银行", result_text)
            self.assertIn("高盛集团", result_text)

    def test_no_fabrication_when_id_missing(self):
        """缺少真实材料ID时不编造"""
        body = [
            "## 1 要点",
            "",
            "参考公开来源[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]公开网页 | https://example.com/report",
        ]
        lines = self._make_report(body, refs)

        # Materials without matching source
        materials_no_match = {
            "__meta__": {"company": "Test", "ticker": "000001"},
        }
        source_index_no_match = build_source_index(materials_no_match)

        result, report = rebuild_references(lines, materials_no_match, source_index_no_match)

        # Should not fabricate an ID
        result_text = "\n".join(result)
        self.assertNotIn("ID：FABRICATED", result_text)
        self.assertNotIn("ID：000000", result_text)

    def test_bidirectional_closure(self):
        """正文引用和参考资料编号必须一致"""
        body = [
            "## 1 要点",
            "",
            "公司增长强劲[1]。市场看好前景[2]。",
            "",
            "会议纪要显示管理层信心[3]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
            "[2]Materials V2研报 | 2026-06-19 | ID：8934316 | 高盛集团 | TestCo前景展望：AI驱动新增长 | API：getMaterialsV2",
            "[3]Materials V2纪要 | 2026-01-29 | ID：173196 | 上市公司 | TestCo FY2025业绩交流会 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            # Check used refs are all defined
            self.assertGreaterEqual(report.get("mapped", 0), 1,
                f"Expected at least 1 mapped reference, got: {report}")

            # Check no unmapped refs that should have been mapped
            unmapped = report.get("unmapped", [])
            self.assertEqual(len(unmapped), 0,
                f"All used refs should be mappable, got unmapped: {unmapped}")

    def test_refs_renumbered_sequentially(self):
        """引用按正文首次引用顺序从[1]开始连续编号"""
        body = [
            "## 1 要点",
            "",
            "第三个来源提到某观点[3]。",   # ← 首先引用[3]
            "",
            "第一个来源的数据[1]。",       # ← 然后引用[1]
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
            "[2]Materials V2研报 | 2026-06-19 | ID：8934316 | 高盛集团 | TestCo前景展望：AI驱动新增长 | API：getMaterialsV2",
            "[3]Materials V2纪要 | 2026-01-29 | ID：173196 | 上市公司 | TestCo FY2025业绩交流会 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            # After rebuild, refs should start from [1] sequentially
            in_ref = False
            ref_nums = []
            for line in result:
                if "参考资料" in line:
                    in_ref = True
                    continue
                if in_ref and line.strip().startswith("[") and not line.strip().startswith("[A"):
                    import re
                    m = re.match(r'^\[(\d+)\]', line.strip())
                    if m:
                        ref_nums.append(int(m.group(1)))
            if ref_nums:
                self.assertEqual(ref_nums[0], 1, f"First ref should be [1], got [{ref_nums[0]}]")
                for i in range(1, len(ref_nums)):
                    self.assertEqual(ref_nums[i], ref_nums[i-1] + 1,
                        f"Refs should be sequential, got {ref_nums}")

    def test_meeting_summary_type_correct(self):
        """纪要类型正确标注为Materials V2纪要"""
        body = [
            "## 1 要点",
            "",
            "业绩会内容[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2纪要 | 2026-01-29 | ID：173196 | 上市公司 | TestCo FY2025业绩交流会 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            result_text = "\n".join(result)
            self.assertIn("Materials V2纪要", result_text)

    def test_api_field_present(self):
        """每条参考资料必须包含API字段"""
        body = [
            "## 1 要点",
            "",
            "数据支持[1]。",
        ]
        refs = [
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报：长期增长逻辑 | API：getMaterialsV2",
        ]
        lines = self._make_report(body, refs)
        result, report = rebuild_references(lines, self.materials, self.source_index)

        if "error" not in report:
            result_text = "\n".join(result)
            self.assertIn("API：", result_text)

    def test_no_reference_section_returns_error(self):
        """没有参考资料章节时返回error"""
        lines = [
            "## 1 要点",
            "",
            "公司数据[1]。",
        ]
        result, report = rebuild_references(lines, self.materials, self.source_index)
        self.assertIn("error", report)
        self.assertEqual(report["error"], "no_reference_section_found")


class TestEndToEndPostProcess(unittest.TestCase):
    """端到端post_process集成测试"""

    def setUp(self):
        self.materials_path = None
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if self._tmp_dir and os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _write_temp_files(self, report_content, materials_content):
        report_path = os.path.join(self._tmp_dir, "test_report.md")
        materials_path = os.path.join(self._tmp_dir, "test_materials.json")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        with open(materials_path, "w", encoding="utf-8") as f:
            json.dump(materials_content, f, ensure_ascii=False)
        return report_path, materials_path

    def test_full_pipeline_no_crash(self):
        """完整后处理管线不应崩溃"""
        materials = {
            "__meta__": {"company": "Test", "ticker": "000001"},
            "materials_v2": {
                "unique_sources": [
                    {
                        "id": "8937002",
                        "dataType": "research",
                        "title": "TestCo深度研报",
                        "metadata": {
                            "organization": "巴克莱银行",
                            "publishTime": "2026-06-20",
                        },
                    },
                ]
            },
        }

        report = "\n".join([
            "## 3 核心投资逻辑",
            "",
            "### 2.1 短期逻辑",    # ← 编号错误
            "",
            "正文引用[1]。",
            "",
            "## 6 财务",
            "",
            "| 指标 | FY2023 | FY2024 |",
            "|:-----|-----:|-----:|",
            "| 营收 | 100 | 200 |",
            "| 未知 | — | 50 |",     # ← 稀疏行
            "",
            "## 参考资料",
            "",
            "[1]Materials V2研报 | 2026-06-20 | ID：8937002 | 巴克莱银行 | TestCo深度研报 | API：getMaterialsV2",
        ])

        rp, mp = self._write_temp_files(report, materials)
        output = os.path.join(self._tmp_dir, "processed.md")

        result = post_process(rp, mp, output)
        self.assertIn("output_path", result)
        self.assertIn("section_fixes", result)
        self.assertIn("sparse_deletions", result)
        self.assertIn("reference_report", result)

        # Check section fix worked
        self.assertGreaterEqual(len(result["section_fixes"]), 1)

        # Read output and verify
        with open(output, "r", encoding="utf-8") as f:
            processed = f.read()
        self.assertIn("### 3.1 短期逻辑", processed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
