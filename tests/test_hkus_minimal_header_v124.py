#!/usr/bin/env python3
"""Minimal HK/US report header tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_report_quality_v124 as checker  # noqa: E402
import hk_us_report_writer_v124 as writer  # noqa: E402


def _md(header: str, body_extra: str = "") -> str:
    filler = "\n".join(f"填充内容{i}：用于满足交付文件大小要求。" for i in range(40))
    return f"""# 测试公司（02382.HK）港股公司一页纸：主业修复驱动盈利质量改善

{header}

## 1 关键要点
{filler}

## 5 业务拆分
{body_extra}

## 13 参考资料
[1]Datayes Research | 2026-07-16 | ID: a1 | Mock | 标题 | API: batchGetReportContent
"""


def _delivery_issues(content: str, market: str = "HK"):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "report.md"
        p.write_text(content, encoding="utf-8")
        (Path(d) / "report.docx").write_bytes(b"mock")
        (Path(d) / "source_trace.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        (Path(d) / "id_audit.json").write_text("{}", encoding="utf-8")
        gate = checker.check_delivery(str(p), market)
        return gate.issues


class MinimalHeaderTests(unittest.TestCase):
    def test_hk_writer_meta_strict(self):
        self.assertEqual(writer._build_hkus_meta_line("港股", "2026-07-16").strip(), "市场：港股 | 生成日期：2026-07-16")

    def test_us_writer_meta_strict(self):
        self.assertEqual(writer._build_hkus_meta_line("美股", "2026-07-16").strip(), "市场：美股 | 生成日期：2026-07-16")

    def test_header_no_industry(self):
        self.assertNotIn("行业：", writer._build_hkus_meta_line("港股", "2026-07-16"))

    def test_header_no_price_market_cap(self):
        self.assertNotIn("当前价格/市值：", writer._build_hkus_meta_line("港股", "2026-07-16"))

    def test_missing_industry_does_not_trigger_checker(self):
        issues = _delivery_issues(_md("市场：港股 | 生成日期：2026-07-16"))
        self.assertFalse([i for i in issues if "行业" in i.message])

    def test_missing_price_market_cap_does_not_trigger_checker(self):
        issues = _delivery_issues(_md("市场：港股 | 生成日期：2026-07-16"))
        self.assertFalse([i for i in issues if "价格" in i.message or "市值" in i.message])

    def test_missing_market_triggers_header_issue(self):
        issues = _delivery_issues(_md("生成日期：2026-07-16"))
        self.assertTrue([i for i in issues if i.check_id in ("D4", "D4_HKUS_HEADER_SCHEMA")])

    def test_missing_generation_date_triggers_header_issue(self):
        issues = _delivery_issues(_md("市场：港股"))
        self.assertTrue([i for i in issues if i.check_id in ("D4", "D4_HKUS_HEADER_SCHEMA")])

    def test_legacy_price_market_cap_header_triggers_regression_check(self):
        issues = _delivery_issues(_md("市场：港股 | 行业：光学元件 | 当前价格/市值：72港元 | 生成日期：2026-07-16"))
        self.assertTrue([i for i in issues if i.check_id == "D4_HKUS_HEADER_LEGACY"])

    def test_body_industry_word_not_flagged(self):
        issues = _delivery_issues(_md("市场：港股 | 生成日期：2026-07-16", "行业竞争格局仍需跟踪。"))
        self.assertFalse([i for i in issues if i.check_id == "D4_HKUS_HEADER_LEGACY"])


if __name__ == "__main__":
    unittest.main()
