#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司一页纸自动评测预检器（v1.2.2 — 新增H3编号检查、稀疏数据检查、参考资料格式检查）

输入：
  1. 公司一页纸 Markdown
  2. 对应 materials JSON

输出：
  - audit.md：问题清单与核验结果
  - audit.json：结构化结果
  - claims.csv：正文中带引用的 Claim 列表

仅使用 Python 标准库。
"""

from __future__ import annotations

import argparse, csv, json, math, re, sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

# v1.2.2: use local evaluator modules
_skill_dir = Path(__file__).resolve().parent
if str(_skill_dir) not in sys.path:
    sys.path.insert(0, str(_skill_dir))

from evaluator.llm_judge import judge_findings, run_llm_judge
from evaluator.structure_validator import (
    validate_structure,
    detect_market,
    load_profile,
    StructureFinding,
)
from evaluator.logic_validator import (
    validate_logic,
    LogicFinding,
)
# v1.2.2: shared table classifier
_scripts_dir = _skill_dir / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))
from table_classifier import classify_table, should_apply_sparse_rules, get_checkable_columns


REF_PATTERN = re.compile(r"\[([0-9]+|A[0-9]+)\]")
REF_DEF_PATTERN = re.compile(r"^\[([0-9]+|A[0-9]+)\]\s*(.*)$")
SOURCE_ID_PATTERN = re.compile(r"ID[：:]\s*([A-Za-z0-9_-]+)")
URL_PATTERN = re.compile(r"https?://[^\s|]+")
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:约|超|至少|近)?-?\d+(?:\.\d+)?(?:万亿|千亿|百亿|亿|万|千)?(?:港元|美元|元|人民币|股|倍|%|pct|个百分点)?"
)


@dataclass
class Finding:
    severity: str
    category: str
    location: str
    issue: str
    evidence: str = ""
    suggestion: str = ""


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("materials JSON 顶层必须是对象")
    return obj


def split_report(md: str) -> tuple[str, str]:
    marker = "## 13 参考资料"
    if marker in md:
        return md.split(marker, 1)
    m = re.search(r"^(?:#{1,3}\s+)?\d*\s*参考资料\s*$", md, flags=re.M)
    if m:
        return md[:m.start()], md[m.end():]
    m = re.search(r"^##\s+\d*\s*参考资料\s*$", md, flags=re.M)
    if m:
        return md[:m.start()], md[m.end():]
    return md, ""


def parse_reference_definitions(ref_text: str) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for raw in ref_text.splitlines():
        line = raw.strip()
        m = REF_DEF_PATTERN.match(line)
        if not m:
            continue
        ref_no, content = m.groups()
        sid_m = SOURCE_ID_PATTERN.search(content)
        urls = URL_PATTERN.findall(content)
        refs[ref_no] = {
            "definition": content,
            "source_id": sid_m.group(1) if sid_m else None,
            "urls": urls,
        }
    return refs


def recursively_collect_sources(obj: Any) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "id" in value and value.get("id") is not None:
                index[str(value["id"])].append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return dict(index)


def source_text(source_objs: list[dict[str, Any]]) -> str:
    texts = []
    for obj in source_objs:
        if isinstance(obj.get("text"), str):
            texts.append(obj["text"])
    return "\n".join(texts)


def extract_claims(body: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        refs = REF_PATTERN.findall(line)
        if not line or not refs:
            continue
        if line.startswith("|:") or set(line) <= {"|", "-", ":", " "}:
            continue
        claims.append({
            "line": lineno,
            "text": line,
            "references": sorted(set(refs), key=lambda x: (x.startswith("A"), int(x[1:]) if x.startswith("A") else int(x))),
            "numbers": NUMBER_PATTERN.findall(line),
        })
    return claims


def normalise_title(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"[\s：:（）()\-—；;，,。\.]+", "", text).lower()


def metadata_check(
    references: dict[str, dict[str, Any]],
    source_index: dict[str, list[dict[str, Any]]],
) -> list[Finding]:
    findings: list[Finding] = []
    for ref_no, ref in references.items():
        sid = ref.get("source_id")
        if not sid or sid not in source_index:
            continue
        source = source_index[sid][0]
        source_title = str(source.get("title") or "")
        source_meta = source.get("metadata") or {}
        source_org = str(source_meta.get("organization") or source_meta.get("source") or "")
        definition = ref["definition"]

        if source_title:
            a = normalise_title(source_title)
            b = normalise_title(definition)
            if a and a[:18] not in b and b[-18:] not in a:
                findings.append(Finding(
                    severity="P2",
                    category="来源元数据",
                    location=f"参考资料 [{ref_no}]",
                    issue="参考资料标题与 JSON 中标题存在差异，建议人工确认。",
                    evidence=f"JSON标题：{source_title}\n报告定义：{definition}",
                    suggestion="生成参考资料时直接读取 JSON 标题，不要由模型改写。",
                ))

        if source_org and source_org.lower() not in definition.lower():
            findings.append(Finding(
                severity="P2",
                category="来源元数据",
                location=f"参考资料 [{ref_no}]",
                issue="参考资料中的机构/来源与 JSON 元数据不一致。",
                evidence=f"JSON来源：{source_org}\n报告定义：{definition}",
                suggestion="参考资料机构字段直接使用来源元数据；来源类型可另设字段。",
            ))
    return findings


def get_section(md: str, heading_prefix: str, next_heading_level: str = "##") -> str:
    lines = md.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        return ""
    result = []
    for line in lines[start:]:
        if line.startswith(next_heading_level + " ") and not line.strip().startswith(heading_prefix):
            break
        result.append(line)
    return "\n".join(result)


def parse_markdown_table(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
            continue
        rows.append(cells)
    return rows


def choose_is_record(records: list[dict[str, Any]], end_date: str, fiscal_period: int) -> dict[str, Any] | None:
    candidates = [
        r for r in records
        if r.get("endDate") == end_date
        and int(r.get("fiscalPeriod") or 0) == fiscal_period
        and "本期" in str(r.get("adjustedFlag") or "")
    ]
    if not candidates:
        candidates = [
            r for r in records
            if r.get("endDate") == end_date and int(r.get("fiscalPeriod") or 0) == fiscal_period
        ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: int(r.get("isNew") or 0), reverse=True)[0]


def choose_bs_record(records: list[dict[str, Any]], end_date: str) -> dict[str, Any] | None:
    candidates = [
        r for r in records
        if r.get("endDate") == end_date and str(r.get("adjustedFlag")) == "期末余额"
    ]
    if not candidates:
        candidates = [r for r in records if r.get("endDate") == end_date]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: int(r.get("isNew") or 0), reverse=True)[0]


def choose_cf_record(records: list[dict[str, Any]], end_date: str) -> dict[str, Any] | None:
    return choose_is_record(records, end_date, 12)


def parse_numeric_cell(cell: str) -> float | None:
    cell = cell.strip()
    if cell in {"", "—", "-", "N/A", "NA"}:
        return None
    cell = cell.replace(",", "").replace("约", "").replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", cell)
    return float(m.group()) if m else None


def compare(actual: float | None, expected: float | None, tolerance: float) -> tuple[bool | None, str]:
    if actual is None or expected is None:
        return None, "缺少可比较值"
    delta = abs(actual - expected)
    return delta <= tolerance, f"报告={actual:g}，JSON={expected:g}，差值={delta:g}"


def financial_table_checks(
    md: str,
    materials: dict[str, Any],
    market: str,
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Finding]]:
    """市场感知的财务表核验。"""
    checks: list[dict[str, Any]] = []
    findings: list[Finding] = []

    financial_source = profile.get("market_specific_rules", {}).get("financial_data_source", "hk_pit")

    # ── 美股：跳过 ──
    if market == "US" or financial_source == "manual_from_research":
        checks.append({
            "metric": "HK PIT financial statements",
            "period": "US market",
            "report_value": None,
            "json_value": None,
            "passed": None,
            "status": "skipped_with_reason",
            "reason": "US market case: Datayes HK PIT financial statement APIs are not applicable.",
            "evidence": "N/A - market=US",
        })
        return checks, []

    # ── A股：使用 fdmtNew ──
    if market == "A" or financial_source == "fdmtNew":
        return _a_share_financial_checks(md, materials, profile)

    # ── 港股：使用 HK PIT ──
    structured = (((materials.get("structured") or {}).get("hk_financials")) or {})
    is_records = structured.get("getHkFdmtIsPit") or []
    bs_records = structured.get("getHkFdmtBsPit") or []
    cf_records = structured.get("getHkFdmtCfPit") or []

    if not (is_records and bs_records and cf_records):
        return checks, [Finding(
            "P1", "结构化数据", "JSON structured.hk_financials",
            "缺少港股利润表、资产负债表或现金流量表，无法自动核验财务表。",
        )]

    fin_sec_id = profile.get("financial_section_id", "7")
    section = get_section(md, f"## {fin_sec_id} ")
    if not section:
        section = get_section(md, f"## {fin_sec_id}.")
    rows = parse_markdown_table(section)
    if len(rows) < 2:
        return checks, [Finding("P1", "报告结构", f"第{fin_sec_id}章", "未解析到财务表。")]

    header = rows[0]
    row_map = {row[0]: row for row in rows[1:] if row}

    periods = [
        ("FY2023", "2023-12-31", 12, 1),
        ("FY2024", "2024-12-31", 12, 2),
        ("FY2025", "2025-12-31", 12, 3),
        ("2026Q1", "2026-03-31", 3, 4),
    ]

    metric_specs = {
        "营业总收入（亿元）": ("is", "tRevenue", 1e8, 1.0),
        "毛利（亿元）": ("is", "grossProf", 1e8, 1.0),
        "营业利润（亿元）": ("is", "operateProfit", 1e8, 1.0),
        "税前利润（亿元）": ("is", "tProfit", 1e8, 1.0),
        "稀释EPS（元/股）": ("is", "dilutedEPS", 1.0, 0.02),
        "经营现金流净额（亿元）": ("cf", "nCfOperateA", 1e8, 1.0),
        "资本开支（亿元）": ("cf", "purFixAssets", 1e8, 1.0),
        "总资产（亿元）": ("bs", "tAssets", 1e8, 1.0),
    }

    record_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for label, end_date, fiscal_period, col_idx in periods:
        record_cache[("is", label)] = choose_is_record(is_records, end_date, fiscal_period)
        record_cache[("bs", label)] = choose_bs_record(bs_records, end_date)
        record_cache[("cf", label)] = choose_cf_record(cf_records, end_date) if fiscal_period == 12 else None

    for metric, (statement, field, scale, tol) in metric_specs.items():
        row = _find_metric_row(row_map, metric, profile)
        if not row:
            continue
        for label, _, _, col_idx in periods:
            if col_idx >= len(row):
                continue
            report_value = parse_numeric_cell(row[col_idx])
            rec = record_cache.get((statement, label))
            raw = rec.get(field) if rec else None
            if raw is not None and field == "purFixAssets":
                raw = abs(raw)
            expected = (raw / scale) if raw is not None else None
            ok, evidence = compare(report_value, expected, tol)
            checks.append({
                "metric": metric,
                "period": label,
                "report_value": report_value,
                "json_value": expected,
                "passed": ok,
                "evidence": evidence,
            })
            if ok is False:
                findings.append(Finding(
                    "P1", "财务数字", f"第{fin_sec_id}章 {metric} {label}",
                    "报告数字与结构化 JSON 不一致。",
                    evidence,
                    "优先从结构化字段直接渲染；禁止模型二次转录。",
                ))

    ratio_specs = {
        "毛利率（%）": ("is", lambda r: r.get("grossProf") / r.get("tRevenue") * 100 if r and r.get("grossProf") is not None and r.get("tRevenue") else None, 0.15),
        "营业利润率（%）": ("is", lambda r: r.get("operateProfit") / r.get("tRevenue") * 100 if r and r.get("operateProfit") is not None and r.get("tRevenue") else None, 0.15),
        "资产负债率（%）": ("bs", lambda r: r.get("tLiab") / r.get("tAssets") * 100 if r and r.get("tLiab") is not None and r.get("tAssets") else None, 0.15),
    }
    for metric, (statement, fn, tol) in ratio_specs.items():
        row = _find_metric_row(row_map, metric, profile)
        if not row:
            continue
        for label, _, _, col_idx in periods:
            if col_idx >= len(row):
                continue
            report_value = parse_numeric_cell(row[col_idx])
            rec = record_cache.get((statement, label))
            expected = fn(rec)
            ok, evidence = compare(report_value, expected, tol)
            checks.append({
                "metric": metric,
                "period": label,
                "report_value": report_value,
                "json_value": expected,
                "passed": ok,
                "evidence": evidence,
            })
            if ok is False:
                findings.append(Finding(
                    "P1", "财务计算", f"第{fin_sec_id}章 {metric} {label}",
                    "报告比率与 JSON 重算结果不一致。",
                    evidence,
                    "用代码重算比率，不让模型自行计算。",
                ))

    return checks, findings


def _find_metric_row(
    row_map: dict[str, list[str]],
    metric: str,
    profile: dict[str, Any],
) -> list[str] | None:
    """在行映射中按指标名或其别名查找。"""
    if metric in row_map:
        return row_map[metric]
    aliases = profile.get("financial_metric_aliases", {}).get(metric, [])
    for alias in aliases:
        if alias in row_map:
            return row_map[alias]
    return None


def _a_share_financial_checks(
    md: str,
    materials: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Finding]]:
    """A股财务核验 — 从 materials JSON 中提取 fdmtNew 数据进行比对。"""
    checks: list[dict[str, Any]] = []
    findings: list[Finding] = []

    # Try to locate fdmtNew data in materials
    # If fetch_materials.py didn't include it, skip with reason
    structured = materials.get("structured") or {}
    fdmt_data = structured.get("fdmtNew")

    if not fdmt_data or not isinstance(fdmt_data, dict) or not fdmt_data.get("dataRow"):
        checks.append({
            "metric": "A-share financial statements",
            "period": "A market",
            "report_value": None,
            "json_value": None,
            "passed": None,
            "status": "skipped_with_reason",
            "reason": "A-share fdmtNew data not present in materials JSON. Use fetch_materials.py with A-share support to include fdmtNew.",
            "evidence": "N/A - fdmtNew not in materials",
        })
        return checks, []

    title_bar = fdmt_data.get("titleBar", [])
    data_rows = fdmt_data.get("dataRow", [])

    # Build period index from titleBar
    period_index = {}
    for i, tb in enumerate(title_bar):
        year = tb.get("year")
        rpt_type = tb.get("reportPeriodType", "")
        label = f"FY{year}" if rpt_type == "A" else f"{year}Q1" if rpt_type == "Q1" else f"{year}{rpt_type}"
        period_index[label] = i

    # Build metric data lookup
    metric_data = {}
    for dr in data_rows:
        style = dr.get("fdmtItemStyle", {})
        name = style.get("name", "")
        code = style.get("code", "")
        data = dr.get("data", [])
        if name:
            metric_data[name] = {"code": code, "data": data}
        if code:
            metric_data[code] = {"code": code, "data": data, "name": name}

    # A-share metric map: (report_metric_name, fdmt_field, scale, tolerance)
    a_metrics = {
        "营业总收入（亿元）": ("tRevenue", 1e8, 1.0),
        "归母净利润（亿元）": ("NIncomeAttrP", 1e8, 1.0),
        "扣非归母净利（亿元）": ("niAttrPCut", 1e8, 1.0),
        "毛利率（%）": ("grossMARgin", 1.0, 0.5),
        "净利率（%）": ("npMARgin", 1.0, 0.5),
        "ROE（%）": ("ROE", 1.0, 0.5),
        "经营现金流净额（亿元）": ("NCFOperateANotes", 1e8, 1.0),
        "资产负债率（%）": ("asseTLiabRatio", 1.0, 0.5),
        "总资产（亿元）": ("TAssets", 1e8, 1.0),
    }

    fin_sec_id = profile.get("financial_section_id", "6")
    # Try multiple section heading formats
    section = get_section(md, f"## {fin_sec_id} ")
    if not section:
        section = get_section(md, f"## {fin_sec_id}.")
    rows = parse_markdown_table(section)

    if len(rows) < 2:
        checks.append({
            "metric": "A-share financial table",
            "period": "A market",
            "report_value": None,
            "json_value": None,
            "passed": None,
            "status": "skipped_with_reason",
            "reason": "Financial table not found or too few rows.",
            "evidence": f"Section heading: ## {fin_sec_id}, rows parsed: {len(rows)}",
        })
        return checks, findings

    row_map = {row[0]: row for row in rows[1:] if row}

    # 从表头动态建立期间→列索引映射
    header = rows[0]
    col_label_map: dict[str, int] = {}
    for col_i, cell in enumerate(header):
        cell_clean = cell.strip()
        # 匹配 FY2022 / FY2023 / 2026Q1 等期间标签
        m = re.match(r"(?:FY)?(20\d{2})\s*(Q[1-4])?", cell_clean)
        if not m:
            m = re.match(r"(20\d{2})\s*(Q[1-4])?", cell_clean)
        if m:
            yr = m.group(1)
            qt = m.group(2) or ""
            label = f"FY{yr}" if not qt else f"{yr}{qt}"
            col_label_map[label] = col_i

    # 建立期间→data index的映射（基于titleBar）
    period_data_idx: dict[str, int] = {}
    for i, tb in enumerate(title_bar):
        year = tb.get("year")
        rpt_type = tb.get("reportPeriodType", "")
        label = f"FY{year}" if rpt_type == "A" else f"{year}Q1" if rpt_type == "Q1" else f"{year}{rpt_type}"
        period_data_idx[label] = i

    # 构建检查计划
    a_periods = []
    for period_label in ["FY2023", "FY2024", "FY2025", "2026Q1"]:
        if period_label in period_data_idx and period_label in col_label_map:
            a_periods.append((period_label, period_data_idx[period_label], col_label_map[period_label]))

    for report_metric, (fdmt_field, scale, tol) in a_metrics.items():
        row = _find_metric_row(row_map, report_metric, profile)
        if not row:
            continue
        data_arr = metric_data.get(fdmt_field, {}).get("data", [])
        if not data_arr:
            continue
        for label, data_idx, col_idx in a_periods:
            if col_idx >= len(row):
                continue
            report_value = parse_numeric_cell(row[col_idx])
            raw = data_arr[data_idx] if data_idx < len(data_arr) else None
            expected = (raw / scale) if raw is not None else None
            ok, evidence = compare(report_value, expected, tol)
            checks.append({
                "metric": report_metric,
                "period": label,
                "report_value": report_value,
                "json_value": expected,
                "passed": ok,
                "evidence": evidence,
            })
            if ok is False:
                findings.append(Finding(
                    "P1", "财务数字", f"第{fin_sec_id}章 {report_metric} {label}",
                    "报告数字与 fdmtNew 接口数据不一致。",
                    evidence,
                    "优先从结构化字段直接渲染。",
                ))

    if not checks:
        checks.append({
            "metric": "A-share financial checks",
            "period": "A market",
            "report_value": None,
            "json_value": None,
            "passed": None,
            "status": "skipped_with_reason",
            "reason": "No matching financial metrics found in report table.",
            "evidence": "Table rows parsed but no metric names matched.",
        })

    return checks, findings


def report_specific_rules(
    body: str,
    references: dict[str, dict[str, Any]],
    source_index: dict[str, list[dict[str, Any]]],
    materials: dict[str, Any],
    market: str,
    profile: dict[str, Any],
) -> list[Finding]:
    """通用规则 + 市场特定规则。"""
    findings: list[Finding] = []
    market_rules = profile.get("market_specific_rules", {})

    # ── CapEx 口径检查（港股通用） ──
    capex_values = []
    capex_patterns = [
        r"2025(?:年|全年)?资本开支\s*(\d+(?:\.\d+)?)\s*亿元",
        r"FY2025[^\n。；|]{0,50}?资本开支(?:增至|为)?\s*(\d+(?:\.\d+)?)\s*亿元",
    ]
    for pattern in capex_patterns:
        for m in re.finditer(pattern, body, flags=re.I):
            context_start = max(0, m.start() - 15)
            context_end = min(len(body), m.end() + 15)
            capex_values.append((float(m.group(1)), body[context_start:context_end].replace("\n", " ").strip()))
    distinct = sorted({v for v, _ in capex_values})
    if len(distinct) >= 2:
        findings.append(Finding(
            "P1", "口径一致性", "第2章与第7章",
            f"2025 年资本开支出现多个值：{', '.join(f'{v:g}' for v in distinct)} 亿元，未解释口径差异。",
            "\n".join(text for _, text in capex_values),
            '分别命名为“公司披露 CapEx”与“购建固定资产现金流”，或统一使用一个权威口径。',
        ))

    # ── ROE 非标准定义 ──
    if re.search(r"ROE（?%?，?基于税前利润/净资产", body, flags=re.I):
        findings.append(Finding(
            "P1", "指标口径", "第7章 ROE",
            '把“税前利润/期末净资产”命名为 ROE，不符合常用 ROE 定义。',
            "报告表头：ROE（%，基于税前利润/净资产）",
            '改用归母净利润/平均归母权益；若保留现有算法，应改名为“税前利润/期末净资产”。',
        ))

    # ── Forecast→Actual ──
    src4 = references.get("4", {}).get("source_id")
    if src4 and src4 in source_index:
        src_text = source_text(source_index[src4])
        if "预计2026年4月推出" in src_text:
            for lineno, line in enumerate(body.splitlines(), 1):
                if "[4]" in line and ("混元3.0大模型发布" in line or "已发布混元3.0" in line or "于2026年4月上线" in line):
                    findings.append(Finding(
                        "P0", "数据属性", f"正文第 {lineno} 行",
                        "来源将混元 3.0 表述为预计推出，报告却写成已发布/已上线，存在 Forecast 转 Actual。",
                        f"报告：{line.strip()}\n来源[4]：预计2026年4月推出的混元3.0大模型",
                        '必须补充实际发布的官方/最新来源；否则改成”预计发布”。',
                    ))

    # ── 关联企业股权使用旧值 ──
    if "联营及合营企业股权（约2461亿元）" in body or "联营及合营企业股权（约2461" in body:
        structured = (((materials.get("structured") or {}).get("hk_financials")) or {})
        bs_records = structured.get("getHkFdmtBsPit") or []
        latest = choose_bs_record(bs_records, "2026-03-31") or choose_bs_record(bs_records, "2025-12-31")
        if latest:
            assoc = latest.get("assocEquity")
            joint = latest.get("joinEquity")
            if assoc is not None:
                total = (assoc + (joint or 0)) / 1e8
                findings.append(Finding(
                    "P1", "数据时效性", "第12章 股权投资减值风险",
                    "报告使用约 2461 亿元的旧数据，与 JSON 最新期末数据不一致。",
                    f"报告：约2461亿元；JSON最新期末联营+合营权益约{total:.0f}亿元（期末 {latest.get('endDate')}）。",
                    "风险提示应使用最新报告期，并明确指标包含联营还是联营+合营。",
                ))

    # ── 同业对比表引用 ──
    if market_rules.get("require_reference_in_peer_table", True):
        # 找行业对比/同业比较章节
        peer_sec_ids = ["9", "10"] if market == "A" else ["9"]
        for sec_id in peer_sec_ids:
            in_section = False
            next_sec_id = str(int(sec_id) + 1)
            for lineno, line in enumerate(body.splitlines(), 1):
                if line.startswith(f"## {sec_id} ") or line.startswith(f"## {sec_id}."):
                    in_section = True
                    continue
                if in_section and (line.startswith(f"## {next_sec_id} ") or line.startswith(f"## {next_sec_id}.")):
                    in_section = False
                if in_section and line.startswith("|") and re.search(r"\d", line) and not REF_PATTERN.search(line):
                    if not line.startswith("|:") and "公司（代码）" not in line and "竞争关系" not in line and "可比业务" not in line:
                        findings.append(Finding(
                            "P1", "引用覆盖", f"正文第 {lineno} 行",
                            "行业对比表包含市值、估值或产品进展等关键事实，但没有引用。",
                            line.strip(),
                            "每个可比公司行至少绑定一个来源；市值需注明日期。",
                        ))

    # ── 情景及风险中的无来源定量推演 ──
    for lineno, line in enumerate(body.splitlines(), 1):
        if ("预计影响ROE3-5pct" in line or "概率约25%" in line or "概率约50%" in line) and not REF_PATTERN.search(line):
            findings.append(Finding(
                "P2", "推导可追溯", f"正文第 {lineno} 行",
                "定量情景或影响值没有来源、公式或假设说明。",
                line.strip(),
                "标记为 Derived，并列出计算公式、关键假设和敏感性来源。",
            ))

    return findings


def markdown_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", "<br>")


def write_outputs(
    report_path: Path,
    materials_path: Path,
    output_dir: Path,
    enable_llm_judge: bool = False,
    enforce_llm_judge: bool = False,
) -> tuple[Path, Path, Path]:
    md = report_path.read_text(encoding="utf-8")
    materials = load_json(materials_path)
    body, ref_text = split_report(md)
    references = parse_reference_definitions(ref_text)
    source_index = recursively_collect_sources(materials)
    claims = extract_claims(body)

    market = detect_market(materials)
    profile = load_profile(market)

    findings: list[Finding] = []

    # ── 引用核验 ──
    used_refs = Counter(REF_PATTERN.findall(body))
    missing_defs = sorted(
        [r for r in used_refs if r not in references],
        key=lambda x: (x.startswith("A"), int(x[1:]) if x.startswith("A") else int(x)),
    )
    for ref_no in missing_defs:
        findings.append(Finding(
            "P0", "引用真实性", f"正文引用 [{ref_no}]",
            "正文使用了未在参考资料中定义的引用。",
            f"引用出现 {used_refs[ref_no]} 次。",
            "生成后执行'正文引用集合 是 参考资料引用集合子集'的强校验。",
        ))

    for ref_no, ref in references.items():
        sid = ref.get("source_id")
        if sid and sid not in source_index:
            findings.append(Finding(
                "P0", "引用真实性", f"参考资料 [{ref_no}]",
                "参考资料中的来源 ID 不存在于 materials JSON。",
                f"来源 ID：{sid}",
                "按来源 ID 在 JSON 中反查；失败时禁止生成该引用。",
            ))
        if not sid and ref_no.startswith("A"):
            # A1-A3 结构化接口：依市场判断
            if market == "HK":
                required = {
                    "A1": "getHkFdmtIsPit",
                    "A2": "getHkFdmtBsPit",
                    "A3": "getHkFdmtCfPit",
                }.get(ref_no)
                structured = (((materials.get("structured") or {}).get("hk_financials")) or {})
                if required and not structured.get(required):
                    findings.append(Finding(
                        "P0", "引用真实性", f"参考资料 [{ref_no}]",
                        f"结构化接口 {required} 在 JSON 中不存在或为空。",
                    ))
            elif market == "A":
                # A股：A1通常对应fdmtNew，B1对应一致预期
                if ref_no == "A1":
                    fdmt = (materials.get("structured") or {}).get("fdmtNew")
                    if not fdmt:
                        findings.append(Finding(
                            "P2", "引用真实性", f"参考资料 [{ref_no}]",
                            "A股财务接口 fdmtNew 未包含在 materials JSON 中。",
                            suggestion="使用支持A股的fetch_materials.py采集。",
                        ))
                elif ref_no == "B1":
                    pass  # research_sec_coredata 是独立 API，不做硬检查
        elif not sid and ref.get("urls"):
            json_text = json.dumps(materials, ensure_ascii=False)
            if not any(url in json_text for url in ref["urls"]):
                findings.append(Finding(
                    "P1", "证据打包", f"参考资料 [{ref_no}]",
                    "公开来源 URL 未包含在本次 materials JSON 中，离线评测无法验证原文。",
                    ", ".join(ref["urls"]),
                    "把公开补充材料写入 materials JSON 的 external_sources。",
                ))

    findings.extend(metadata_check(references, source_index))

    # ── 财务数据核验 ──
    financial_checks, financial_findings = financial_table_checks(md, materials, market, profile)
    findings.extend(financial_findings)

    # ── 规则核验 ──
    findings.extend(report_specific_rules(body, references, source_index, materials, market, profile))

    # ── 结构核验 ──
    structure_summary, structure_findings = validate_structure(md, materials)
    for sf in structure_findings:
        findings.append(Finding(
            sf.severity, sf.category, sf.location, sf.issue, sf.evidence, sf.suggestion,
        ))

    # ── 逻辑核验 ──
    logic_summary, logic_findings = validate_logic(md, materials)
    for lf in logic_findings:
        findings.append(Finding(
            lf.severity, lf.category, lf.location, lf.issue, lf.evidence, lf.suggestion,
        ))

    # ── v1.2.2新增：参考资料格式检查 ──
    ref_format_findings = _v122_check_reference_format(references, md, source_index, materials)
    findings.extend(ref_format_findings)

    # ── v1.2.2新增：稀疏数据检查 ──
    sparse_findings = _v122_check_sparse_data(md)
    findings.extend(sparse_findings)

    llm_judge_output: dict[str, Any] | None = None
    if enable_llm_judge:
        case_id = report_path.parents[2].name if len(report_path.parents) >= 3 else report_path.parent.name
        llm_judge_output = run_llm_judge(claims, references, source_index, case_id=case_id)
        if enforce_llm_judge:
            for item in judge_findings(llm_judge_output):
                findings.append(Finding(**item))

    severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 9), f.category, f.location))

    unused_defs = sorted(
        [r for r in references if r not in used_refs],
        key=lambda x: (x.startswith("A"), int(x[1:]) if x.startswith("A") else int(x)),
    )

    passed_financial = sum(1 for x in financial_checks if x["passed"] is True)
    failed_financial = sum(1 for x in financial_checks if x["passed"] is False)
    skipped_financial = sum(1 for x in financial_checks if x["passed"] is None)

    result = {
        "report": str(report_path),
        "materials": str(materials_path),
        "meta": materials.get("__meta__", {}),
        "summary": {
            "market": market,
            "profile": f"{market.lower()}_share",
            "used_reference_count": len(used_refs),
            "defined_reference_count": len(references),
            "missing_reference_definitions": missing_defs,
            "unused_reference_definitions": unused_defs,
            "source_id_count_in_json": len(source_index),
            "claim_line_count": len(claims),
            "financial_checks_passed": passed_financial,
            "financial_checks_failed": failed_financial,
            "financial_checks_skipped": skipped_financial,
            "structure_checks_passed": structure_summary.get("structure_checks_passed", 0),
            "structure_checks_failed": structure_summary.get("structure_checks_failed", 0),
            "structure_checks_skipped": structure_summary.get("structure_checks_skipped", 0),
            "logic_checks_passed": logic_summary.get("logic_checks_passed", 0),
            "logic_checks_failed": logic_summary.get("logic_checks_failed", 0),
            "logic_manual_review": logic_summary.get("logic_checks_manual_review", 0),
            "llm_judge": (llm_judge_output or {}).get("summary") if llm_judge_output else None,
            "llm_judge_enforced": enforce_llm_judge,
            "P0": sum(f.severity == "P0" for f in findings),
            "P1": sum(f.severity == "P1" for f in findings),
            "P2": sum(f.severity == "P2" for f in findings),
            "P3": sum(f.severity == "P3" for f in findings),
            "release_decision": "阻断" if any(f.severity in {"P0", "P1"} for f in findings) else "通过预检",
        },
        "findings": [asdict(f) for f in findings],
        "financial_checks": financial_checks,
        "structure_summary": structure_summary,
        "logic_summary": logic_summary,
        "claims": claims,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = report_path.stem
    json_out = output_dir / f"{stem}_audit.json"
    md_out = output_dir / f"{stem}_audit.md"
    csv_out = output_dir / f"{stem}_claims.csv"
    judge_json_out = output_dir / f"{stem}_llm_judge.json"
    judge_csv_out = output_dir / f"{stem}_llm_judge.csv"

    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["line", "text", "references", "numbers"])
        writer.writeheader()
        for claim in claims:
            writer.writerow({
                "line": claim["line"],
                "text": claim["text"],
                "references": ",".join(claim["references"]),
                "numbers": ",".join(claim["numbers"]),
            })

    if llm_judge_output is not None:
        judge_json_out.write_text(json.dumps(llm_judge_output, ensure_ascii=False, indent=2), encoding="utf-8")
        with judge_csv_out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "claim_id", "line", "parent_text", "claim", "references", "claim_type",
                    "split_from", "deterministic_overlap", "evidence", "evidence_source_id",
                    "verdict", "confidence", "reason", "risk_level", "manual_review",
                    "critical_fact",
                ],
            )
            writer.writeheader()
            for row in llm_judge_output.get("results", []):
                writer.writerow({key: row.get(key) for key in writer.fieldnames})

    summary = result["summary"]
    report_lines = [
        f"# {stem}｜自动评测预检",
        "",
        "## 1. 结论",
        "",
        f"**发布判断：{summary['release_decision']}**",
        f"**市场：{market}（Profile: {summary['profile']}）**",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 正文使用引用类型数 | {summary['used_reference_count']} |",
        f"| 参考资料定义数 | {summary['defined_reference_count']} |",
        f"| JSON来源ID数 | {summary['source_id_count_in_json']} |",
        f"| 带引用Claim行数 | {summary['claim_line_count']} |",
        f"| 财务自动检查通过 | {passed_financial} |",
        f"| 财务自动检查失败 | {failed_financial} |",
        f"| 财务自动检查跳过 | {skipped_financial} |",
        f"| 结构检查通过 | {summary['structure_checks_passed']} |",
        f"| 结构检查失败 | {summary['structure_checks_failed']} |",
        f"| 逻辑检查通过 | {summary['logic_checks_passed']} |",
        f"| 逻辑检查失败 | {summary['logic_checks_failed']} |",
        f"| 逻辑人工复核项 | {summary['logic_manual_review']} |",
        f"| LLM Judge启用 | {'是' if llm_judge_output else '否'} |",
        f"| P0 | {summary['P0']} |",
        f"| P1 | {summary['P1']} |",
        f"| P2 | {summary['P2']} |",
        "",
        "## 2. 优先问题",
        "",
        "| 等级 | 类别 | 位置 | 问题 | 证据 | 修复建议 |",
        "|---|---|---|---|---|---|",
    ]
    for finding in findings:
        report_lines.append(
            "| {severity} | {category} | {location} | {issue} | {evidence} | {suggestion} |".format(
                severity=finding.severity,
                category=markdown_escape(finding.category),
                location=markdown_escape(finding.location),
                issue=markdown_escape(finding.issue),
                evidence=markdown_escape(finding.evidence),
                suggestion=markdown_escape(finding.suggestion),
            )
        )

    if llm_judge_output is not None:
        judge_summary = llm_judge_output.get("summary", {})
        verdict_counts = judge_summary.get("verdict_counts", {})
        risk_counts = judge_summary.get("risk_counts", {})
        report_lines += [
            "",
            "## 2.1 LLM Judge试运行",
            "",
            f"- 模式：{llm_judge_output.get('mode')}",
            f"- Claim数量：{judge_summary.get('claim_count', 0)}",
            f"- supported：{verdict_counts.get('supported', 0)}",
            f"- partially_supported：{verdict_counts.get('partially_supported', 0)}",
            f"- unsupported：{verdict_counts.get('unsupported', 0)}",
            f"- unverifiable：{verdict_counts.get('unverifiable', 0)}",
            f"- P1/P2/P3风险映射：{risk_counts.get('P1', 0)}/{risk_counts.get('P2', 0)}/{risk_counts.get('P3', 0)}",
            f"- 低置信度人工复核：{judge_summary.get('manual_review_count', 0)}",
            f"- 是否并入正式P级：{'是' if enforce_llm_judge else '否'}",
        ]

    report_lines += [
        "",
        "## 3. 财务结构化核验",
        "",
        "| 指标 | 期间 | 报告值 | JSON值 | 结果 |",
        "|---|---|---:|---:|---|",
    ]
    for item in financial_checks:
        passed = "通过" if item["passed"] is True else ("失败" if item["passed"] is False else "跳过")
        rv = "" if item["report_value"] is None else f"{item['report_value']:g}"
        jv = "" if item["json_value"] is None else f"{item['json_value']:.4g}"
        report_lines.append(f"| {item['metric']} | {item['period']} | {rv} | {jv} | {passed} |")

    report_lines += [
        "",
        "## 4. 结构与逻辑核验",
        "",
        f"- 结构检查：通过{summary['structure_checks_passed']} / 失败{summary['structure_checks_failed']}",
        f"- 逻辑检查：通过{summary['logic_checks_passed']} / 失败{summary['logic_checks_failed']} / 人工复核{summary['logic_manual_review']}",
        "",
        "## 5. 引用集合",
        "",
        f"- 正文缺失定义的引用：{', '.join('['+x+']' for x in missing_defs) if missing_defs else '无'}",
        f"- 已定义但正文未使用：{', '.join('['+x+']' for x in unused_defs) if unused_defs else '无'}",
        "",
        "## 6. 使用说明",
        "",
        "- 本工具是确定性预检，不替代 Claim 与原文的 AI 语义匹配。",
        "- 结构/逻辑新增核验由市场 Profile 驱动，可独立配置。",
        "- P0/P1 需要人工确认后再决定是否修改 Skill。",
    ]
    md_out.write_text("\n".join(report_lines), encoding="utf-8")
    return md_out, json_out, csv_out


# ══════════════════════════ v1.2.2 新增检查 ══════════════════════════

_V122_REF_FORMAT = re.compile(
    r'^\[(\d+|A\d+)\](Materials V2\S+|Datayes\S+)\s*\|\s*(\d{4}-\d{2}-\d{2})?\s*\|\s*ID[：:]\s*(\S+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*API[：:]\s*(.+)$'
)
_V122_EMPTY = {'—', '-', 'N/A', 'NA', '', '待补充', '...', 'null', 'None'}
_V122_NUM = re.compile(r'\d')
_V122_TABLE = re.compile(r'^\|.+\|$')
_V122_SEP = re.compile(r'^\|[:\-\s|]+\|$')


def _v122_check_reference_format(
    references: dict[str, dict[str, Any]],
    md: str,
    source_index: dict[str, list[dict[str, Any]]],
    materials: dict[str, Any],
) -> list[Finding]:
    """v1.2.2 参考资料格式检查：
    - 标准格式：[N]来源类型 | YYYY-MM-DD | ID：xxx | 机构 | 标题 | API：xxx
    - [N]后无空格
    - ID存在于本次materials JSON
    - 元数据逐字一致
    - 正文引用与参考资料双向闭环
    """
    findings: list[Finding] = []

    # Parse reference section from markdown
    ref_lines: list[tuple[int, str]] = []
    in_ref = False
    for lineno, line in enumerate(md.splitlines(), 1):
        if re.match(r'^#+\s*(?:\d+\s*)?参考资料', line.strip()):
            in_ref = True
            continue
        if in_ref and line.strip().startswith('#'):
            break
        if in_ref and line.strip():
            ref_lines.append((lineno, line.strip()))

    # Build used_refs set from body
    body, _ = split_report(md)
    used_refs: set[str] = set()
    for line in body.splitlines():
        used_refs.update(REF_PATTERN.findall(line))

    ref_nums_seen: list[int] = []
    ref_a_seen: set[str] = set()
    for lineno, line in ref_lines:
        # Check [N] has no space after (only for numeric refs)
        m_space = re.match(r'^\[(\d+)\]\s+', line)
        if m_space:
            findings.append(Finding(
                "P1", "参考资料格式", f"行{lineno}",
                f"参考资料「[N]」后不应有空格：\"{line[:50]}...\"",
                suggestion="将 [N] 后的空格删除，如 [1]Materials V2研报 | ...",
            ))

        m = REF_DEF_PATTERN.match(line)
        ref_type = None
        if m:
            ref_id = m.group(1)
            if ref_id.startswith('A'):
                ref_type = 'A'
                ref_a_seen.add(ref_id)
            elif ref_id.isdigit():
                ref_type = 'numeric'
                num = int(ref_id)
                if ref_nums_seen and num != ref_nums_seen[-1] + 1:
                    findings.append(Finding(
                        "P1", "参考资料编号", f"行{lineno}",
                        f"参考资料编号不连续：期望[{ref_nums_seen[-1] + 1}]，实际[{num}]。",
                        suggestion="参考资料编号必须从[1]开始连续递增。",
                    ))
                ref_nums_seen.append(num)

        # For A-prefix (structured data), skip pipe count and sub-field checks
        if ref_type == 'A':
            continue

        # Check standard format pipe count (for non-A refs)
        if line.count('|') < 5:
            findings.append(Finding(
                "P1", "参考资料格式", f"行{lineno}",
                f"参考资料缺少必要字段（需至少5个|分隔符）：\"{line[:60]}...\"",
                suggestion="标准格式：[N]来源类型 | YYYY-MM-DD | ID：xxx | 机构 | 完整标题 | API：接口名",
            ))

        # Check for required sub-fields
        has_date = bool(re.search(r'\d{4}-\d{2}-\d{2}', line))
        has_id = bool(re.search(r'ID[：:]', line))
        has_api = bool(re.search(r'API[：:]', line))
        if not (has_date or has_id or has_api):
            findings.append(Finding(
                "P1", "参考资料格式", f"行{lineno}",
                f"参考资料缺少日期/ID/API字段：\"{line[:60]}...\"",
                suggestion="补充日期、ID和API字段。格式：[N]来源类型 | YYYY-MM-DD | ID：xxx | 机构 | 标题 | API：接口名",
            ))

        # Check ID exists in materials JSON
        id_m = re.search(r'ID[：:]\s*(\S+)', line)
        if id_m:
            sid = id_m.group(1)
            if sid not in source_index:
                findings.append(Finding(
                    "P2", "参考资料ID", f"行{lineno}",
                    f"参考资料ID「{sid}」不存在于本次materials JSON中。",
                    evidence=f"ID：{sid}",
                    suggestion="确认ID来自本次实时采集，非旧JSON或手工编造。",
                ))

    # Check bidirectional closure (include both numeric and A-prefix refs)
    defined_nums = {str(n) for n in ref_nums_seen} | ref_a_seen
    missing_from_refs = used_refs - defined_nums
    if missing_from_refs:
        findings.append(Finding(
            "P1", "引用闭环", "参考资料",
            f"正文引用但参考资料缺失的编号：{sorted(missing_from_refs)}",
            suggestion="确保正文所有引用的来源都出现在参考资料章节中。",
        ))

    return findings


def _v122_check_sparse_data(md: str) -> list[Finding]:
    """v1.2.2 稀疏数据检查：使用 table_classifier 区分表格类型。"""
    findings: list[Finding] = []
    lines = md.splitlines()

    tables: list[dict] = []
    in_table = False
    current: dict = {'start': 0, 'rows': []}

    for i, line in enumerate(lines):
        is_t = bool(_V122_TABLE.match(line.strip()))
        is_s = bool(_V122_SEP.match(line.strip().replace(' ', '') or ''))

        if is_t and not is_s:
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if not in_table:
                current = {'start': i, 'rows': [(i, cells)]}
                in_table = True
            else:
                current['rows'].append((i, cells))
        elif in_table and not is_t:
            if len(current['rows']) >= 2:
                current['end'] = i
                tables.append(current)
            in_table = False

    if in_table and len(current['rows']) >= 2:
        current['end'] = len(lines)
        tables.append(current)

    for t in tables:
        data_rows = [(ln, cells) for ln, cells in t['rows'] if not all(
            re.fullmatch(r':?-+:?', c.replace(' ', '')) for c in cells
        )]
        if len(data_rows) < 2:
            continue

        header = data_rows[0][1]
        body = data_rows[1:]

        # Classify table — skip qualitative tables
        classification = classify_table(header)
        if not should_apply_sparse_rules(classification):
            continue

        checkable_cols = get_checkable_columns(classification)

        section_name = ''
        for j in range(t['start'] - 1, -1, -1):
            if lines[j].strip().startswith('#'):
                section_name = lines[j].strip()
                break

        # Row check (only checkable columns)
        for ln, cells in body:
            valid = sum(1 for c_idx in checkable_cols
                       if c_idx < len(cells) and cells[c_idx]
                       and cells[c_idx] not in _V122_EMPTY and _V122_NUM.search(cells[c_idx]))
            total = len([c for c_idx in checkable_cols if c_idx < len(cells)])
            if valid <= 1 and total > 1:
                findings.append(Finding(
                    "P1", "稀疏数据", f"行{ln + 1}",
                    f"表格行仅{valid}/{total}个有效数据，应删除：\"{cells[0][:30]}\"",
                    evidence=f"章节：{section_name}，表类型：{classification.table_type.value}",
                    suggestion="删除有效数据≤1的行。如整表不足最低完整度则不输出该表。",
                ))

        # Column check (only checkable columns)
        for c_idx in checkable_cols:
            if c_idx >= len(header):
                continue
            valid = sum(1 for _, cells in body
                       if c_idx < len(cells) and cells[c_idx]
                       and cells[c_idx] not in _V122_EMPTY and _V122_NUM.search(cells[c_idx]))
            if valid <= 1 and len(body) > 1:
                col_name = header[c_idx] if c_idx < len(header) else f'col_{c_idx}'
                findings.append(Finding(
                    "P1", "稀疏数据", f"行{t['start'] + 1}附近",
                    f"表格列「{col_name}」仅{valid}/{len(body)}个有效数据，应删除该列。",
                    evidence=f"章节：{section_name}，表类型：{classification.table_type.value}",
                    suggestion="删除有效数据≤1的列。",
                ))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="公司一页纸自动评测预检器")
    parser.add_argument("--report", required=True, type=Path, help="公司一页纸 Markdown 路径")
    parser.add_argument("--materials", required=True, type=Path, help="materials JSON 路径")
    parser.add_argument("--output-dir", type=Path, default=Path("./evaluation_output"))
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--enforce-llm-judge", action="store_true")
    args = parser.parse_args()

    if not args.report.exists():
        parser.error(f"报告不存在：{args.report}")
    if not args.materials.exists():
        parser.error(f"JSON不存在：{args.materials}")

    try:
        outputs = write_outputs(
            args.report,
            args.materials,
            args.output_dir,
            enable_llm_judge=args.llm_judge or args.enforce_llm_judge,
            enforce_llm_judge=args.enforce_llm_judge,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1

    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
