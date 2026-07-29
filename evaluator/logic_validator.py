#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逻辑核验器 — 确定性规则检查章节内容的逻辑一致性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class LogicFinding:
    severity: str
    category: str
    location: str
    issue: str
    evidence: str = ""
    suggestion: str = ""


# ── 可复用模式 ──────────────────────────────────────────────
_HEADING_PATTERN = re.compile(r"^#{1,3}\s+(.*)$")
_FORECAST_KEYWORDS = re.compile(r"(预计|预期|有望|目标|或将|预测|指引)", re.I)
_ACTUAL_PRETENSE = re.compile(r"(已发布|已上线|已实现|已达|已确认为)", re.I)
_YEAR_PATTERN = re.compile(r"(FY\d{4}|CY\d{2,4}|\d{4}[AE]|\d{4}年)")


def parse_headings(md: str) -> list[dict[str, Any]]:
    headings = []
    for lineno, raw in enumerate(md.splitlines(), 1):
        m = _HEADING_PATTERN.match(raw.strip())
        if not m:
            continue
        level = raw.strip().count("#", 0, 3)
        headings.append({"line": lineno, "level": level, "title": m.group(1).strip()})
    return headings


def validate_logic(
    md: str,
    materials: dict[str, Any],
) -> tuple[dict[str, Any], list[LogicFinding]]:
    """
    执行逻辑核验——确定性规则，不确定的标记为 manual_review。
    """
    findings: list[LogicFinding] = []
    manual_reviews: list[dict[str, str]] = []
    lines = md.splitlines()
    headings = parse_headings(md)

    # ── 规则1: 核心结论在报告前部 ──
    core_section = _find_heading_range(headings, lines, r"(投资摘要|关键要点|近况跟踪|公司近况)")
    risk_section = _find_heading_range(headings, lines, r"(风险提示|风险因素)")
    if core_section and risk_section:
        core_end = core_section["end_line"]
        risk_start = risk_section["start_line"]
        if risk_start < core_end:
            findings.append(LogicFinding(
                "P1", "内容布局", f"行{risk_start}",
                "风险提示章节出现在核心结论之前。",
            ))

    # ── 规则2: 财务章节包含历史实际数据 ──
    fin_section = _find_heading_range(headings, lines, r"(财务|财务全景|财务与盈利质量|公司财务数据)")
    if fin_section:
        fin_text = "\n".join(lines[fin_section["start_line"]:fin_section["end_line"]])
        has_numbers = bool(re.search(r"\d{2,}", fin_text))
        has_years = bool(re.search(r"(FY|20\d{2}|202[0-6])", fin_text))
        if has_years and not has_numbers:
            findings.append(LogicFinding(
                "P2", "内容完整性", f"行{fin_section['start_line']}",
                "财务章节疑似缺乏具体历史财务数字。",
            ))

    # ── 规则3: 盈利预测不混用 Actual/Forecast ──
    forecast_section = _find_heading_range(headings, lines, r"(盈利预测|一致预期|估值)")
    if forecast_section:
        for lineno in range(forecast_section["start_line"], forecast_section["end_line"]):
            line = lines[lineno - 1]
            if _FORECAST_KEYWORDS.search(line) and _ACTUAL_PRETENSE.search(line):
                findings.append(LogicFinding(
                    "P0", "数据属性", f"行{lineno}",
                    "同一行同时出现预测关键词和已完成表述，疑似混淆Forecast/Actual。",
                    evidence=line.strip(),
                    suggestion="区分预测和实际数据，预测数据标注'E'后缀。",
                ))

    # ── 规则4: 估值章节包含必要元素 ──
    valuation_section = _find_heading_range(headings, lines, r"(估值|估值框架|估值与预测|估值分析)")
    if valuation_section:
        val_text = "\n".join(lines[valuation_section["start_line"]:valuation_section["end_line"]])
        has_pe = bool(re.search(r"PE|市盈率|P/E", val_text))
        has_method = bool(re.search(r"(SOTP|DCF|EV/|PS|PB|可比|PE倍数|目标价)", val_text))
        if not has_method and not has_pe:
            manual_reviews.append({
                "location": f"行{valuation_section['start_line']}",
                "issue": "估值章节未检测到明确估值方法(PE/DCF/SOTP等)或估值指标。",
                "type": "manual_review",
            })

    # ── 规则5: 风险提示不含买入建议 ──
    if risk_section:
        for lineno in range(risk_section["start_line"], risk_section["end_line"]):
            line = lines[lineno - 1]
            if re.search(r"(买入|增持|目标价|推荐|强烈建议)", line):
                findings.append(LogicFinding(
                    "P1", "内容归属", f"行{lineno}",
                    "风险提示章节出现目标价或推荐结论。",
                    evidence=line.strip(),
                    suggestion="将投资建议移至估值章节。",
                ))

    # ── 规则6: 同一表格不跨章节重复 ──
    table_signatures = {}
    for lineno, raw in enumerate(lines, 1):
        if raw.strip().startswith("|") and not raw.strip().startswith("|:"):
            sig = _table_signature(raw)
            if sig and len(sig) > 20:
                if sig in table_signatures:
                    findings.append(LogicFinding(
                        "P2", "内容重复", f"行{lineno} vs 行{table_signatures[sig]}",
                        "疑似相同表格在多个位置重复出现。",
                    ))
                else:
                    table_signatures[sig] = lineno

    # ── 规则7: 章节标题与内容不一致 ──
    for heading in headings:
        if heading["level"] != 2:
            continue
        # 标题含"财务"但内容无数字 → manual_review
        if re.search(r"财务", heading["title"]):
            next_sec = _next_heading(headings, heading)
            end = next_sec["line"] - 1 if next_sec else len(lines)
            content = "\n".join(lines[heading["line"]:end])
            if not re.search(r"\d{4,}", content):
                manual_reviews.append({
                    "location": f"行{heading['line']}",
                    "issue": f"章节「{heading['title']}」内容中未发现大额数字，可能为空或摘要。",
                    "type": "manual_review",
                })

    # 汇聚
    manual_count = len(manual_reviews)
    finding_count = len(findings)
    summary = {
        "logic_checks_passed": max(0, 7 - finding_count - manual_count),
        "logic_checks_failed": finding_count,
        "logic_checks_manual_review": manual_count,
        "manual_review_items": manual_reviews,
    }

    return summary, findings


def _find_heading_range(headings: list[dict[str, Any]], lines: list[str], pattern: str) -> dict[str, Any] | None:
    for h in headings:
        if h["level"] == 2 and re.search(pattern, h["title"]):
            next_h = _next_heading(headings, h)
            return {
                "start_line": h["line"],
                "end_line": next_h["line"] - 1 if next_h else len(lines),
            }
    return None


def _next_heading(headings: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any] | None:
    for h in headings:
        if h["level"] <= current["level"] and h["line"] > current["line"]:
            return h
    return None


def _table_signature(raw: str) -> str:
    """取表格首列的签名，用于检测重复表。"""
    cells = [c.strip() for c in raw.strip().strip("|").split("|")]
    if not cells:
        return ""
    return cells[0]
