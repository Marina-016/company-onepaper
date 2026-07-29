#!/usr/bin/env python3
"""
v1.2.2 报告自动生成器 — 从冻结 materials JSON 生成完整报告草稿

生成所有必选章节。财务数据来自结构化接口。引用来自 normalized_sources。
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from typing import Any


def load_materials(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt(v, dec=0):
    if v is None: return "—"
    return f"{v:,.{dec}f}"


def _val_at(metrics: dict, code: str, idx: int, scale: float = 1e8) -> float | None:
    m = metrics.get(code)
    if not m or not isinstance(m, dict):
        return None
    data = m.get("data", [])
    if idx >= len(data) or data[idx] is None:
        return None
    return data[idx] / scale


def _non_struct_sources(normalized: list[dict]) -> list[dict]:
    return [s for s in normalized if not s.get("is_structured")]


def _build_fin_table_a(structured: dict) -> list[str]:
    """Build A-share financial table."""
    fdmt = structured.get("fdmtNew", {})
    # Check for API error
    if "fdmtNew_error" in structured:
        return ["| 指标 | FY2023 | FY2024 | FY2025 | 2026Q1 |",
                "|:-----|-----:|-----:|-----:|-----:|",
                f"| 结构化接口调用失败 | — | — | — | — | [A1]",
                f"| （{structured['fdmtNew_error'][:60]}） | — | — | — | — |"]

    tb = fdmt.get("titleBar", [])
    if not tb:
        return ["| 指标 | FY2023 | FY2024 | FY2025 | 2026Q1 |",
                "|:-----|-----:|-----:|-----:|-----:|",
                "| 结构化数据未获取 | — | — | — | — | [A1]"]

    pidx = {}
    for i, t in enumerate(tb):
        yr = t.get("year", ""); rpt = t.get("reportPeriodType", "")
        pidx[f"FY{yr}" if rpt == "A" else f"{yr}{rpt}"] = i

    targets = ["FY2023", "FY2024", "FY2025", "2026Q1"]
    idxs = [pidx.get(p) for p in targets]

    metrics = {row.get("fdmtItemStyle", {}).get("code", ""): row for row in fdmt.get("dataRow", [])}

    lines = ["| 指标 | " + " | ".join(targets) + " |",
             "|:-----|" + "|".join(["-----:" for _ in targets]) + "|"]
    specs = [
        ("营业总收入（亿元）", "tRevenue", 1e8, 0),
        ("归母净利润（亿元）", "NIncomeAttrP", 1e8, 0),
        ("扣非归母净利（亿元）", "niAttrPCut", 1e8, 0),
        ("毛利率（%）", "grossMARgin", 1, 1),
        ("净利率（%）", "npMARgin", 1, 1),
        ("ROE（%）", "ROE", 1, 1),
        ("经营现金流净额（亿元）", "NCFOperateANotes", 1e8, 0),
        ("总资产（亿元）", "TAssets", 1e8, 0),
        ("资产负债率（%）", "asseTLiabRatio", 1, 1),
        ("基本EPS（元/股）", "basicEPS", 1, 2),
    ]
    for name, code, scale, dec in specs:
        vals = [_fmt(_val_at(metrics, code, i, scale), dec) if i is not None else "—" for i in idxs]
        lines.append("| " + name + " | " + " | ".join(vals) + " | [A1]")
    return lines


def _build_ref_section(normalized: list[dict]) -> list[str]:
    lines = []
    for s in normalized:
        if s.get("is_structured"):
            lines.append(f"[{s['internal_source_key']}]Datayes结构化接口 | {s['title']} | API：{s.get('api','')}")
    lines.append("")
    for i, s in enumerate(_non_struct_sources(normalized), 1):
        stype = s.get("source_type", "research")
        tmap = {"research": "Materials V2研报", "meetingSummary": "Materials V2纪要",
                "announcement": "Materials V2公告", "news": "Materials V2新闻",
                "wechat": "Materials V2微信", "marketView": "Materials V2市场观点"}
        lines.append(f"[{i}]{tmap.get(stype, stype)} | {s.get('date','')} | ID：{s.get('material_id','')} | "
                     f"{s.get('organization','') or '-'} | {s.get('title','')} | API：{s.get('api','getMaterialsV2')}")
    return lines


def _info_line(meta: dict, normalized: list[dict]) -> str:
    """生成标题下方信息行。"""
    market = meta.get("market", "A")
    ticker = meta.get("ticker", "")
    mkt_map = {"A": "A股", "HK": "港股", "US": "美股"}
    src_count = len(_non_struct_sources(normalized))
    return f"市场：{mkt_map.get(market, market)} | 来源数：{src_count} | 生成日期：{datetime.now().strftime('%Y-%m-%d')}"


def generate_report(materials_path: str, output_path: str) -> dict:
    materials = load_materials(materials_path)
    meta = materials.get("__meta__", {})
    structured = materials.get("structured", {})
    normalized = materials.get("normalized_sources", [])

    company = meta.get("company", "")
    ticker = meta.get("ticker", "")
    market = meta.get("market", "A")
    sha = meta.get("materials_sha256", "")
    sources = _non_struct_sources(normalized)

    L = []
    full_ticker = ticker
    if market == "A": full_ticker = f"{ticker}.SZ"
    elif market == "HK": full_ticker = f"{ticker}.HK"

    # ── Title ──
    if market == "A":
        L.append(f"# {company}（{full_ticker}）公司一页纸")
    elif market == "HK":
        L.append(f"# {company}（{full_ticker}）港股公司一页纸")
    elif market == "US":
        L.append(f"# {company}（{ticker}）美股公司一页纸")
    L.append("")
    L.append(_info_line(meta, normalized))
    L.append("")

    # ═══════════════ A-share sections ═══════════════
    if market == "A":
        _build_a_share_sections(L, materials, structured, normalized, company, ticker, sources)
    elif market == "HK":
        _build_hk_sections(L, materials, structured, normalized, company, ticker, sources)
    elif market == "US":
        _build_us_sections(L, materials, structured, normalized, company, ticker, sources)

    # ── Reference section ──
    L.append("## 参考资料" if market == "A" else "## 13 参考资料")
    L.append("")
    for ref in _build_ref_section(normalized):
        L.append(ref)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    return {"output_path": str(out), "market": market, "sections_written": len([l for l in L if l.startswith("## ")]),
            "sources_total": len(normalized), "sha256": sha}


def _build_a_share_sections(L, materials, structured, normalized, company, ticker, sources):
    """Generate all A-share required sections."""
    src_count = len(sources)

    # §1 公司近况跟踪
    L.append("## 1 公司近况跟踪")
    L.append("")
    if sources:
        for s in sources[:3]:
            title = s.get("title", "")[:80]
            org = s.get("organization", "") or ""
            date = s.get("date", "")
            L.append(f"- {date} | {org} | {title}")
        L.append("")
    else:
        L.append("- （来源数据采集中，待补充。）")
        L.append("")

    # §2 核心投资逻辑
    L.append("## 2 核心投资逻辑")
    L.append("")
    L.append("### 2.1 短期逻辑")
    L.append("")
    L.append("（从采集的研究材料中提取短期催化剂。）")
    L.append("")
    L.append("### 2.2 长期逻辑")
    L.append("")
    L.append("（从采集的研究材料中提取长期竞争优势。）")
    L.append("")

    # §3 催化事件时间表
    L.append("## 3 催化事件时间表")
    L.append("")
    L.append("| 时间 | 事件 | 影响 |")
    L.append("|:-----|:-----|:-----|")
    L.append("| — | （待从材料中提取） | — |")
    L.append("")

    # §4 公司业务拆分
    L.append("## 4 公司业务拆分")
    L.append("")
    L.append("| 业务板块 | 营收占比 | 增长趋势 | 利润贡献 |")
    L.append("|:---------|:-----|:-----|:-----|")
    L.append("| （待从结构化数据及研报中提取） | — | — | — |")
    L.append("")

    # §5 产销链分析
    L.append("## 5 产销链分析")
    L.append("")
    L.append("- （待从材料中提取供应链相关信息。）")
    L.append("")

    # §6 公司财务数据分析
    L.append("## 6 公司财务数据分析")
    L.append("")
    L.append("近三年及最新季度关键财务指标（ACCUMULATE口径，合并报表）：")
    L.append("")
    fin_rows = _build_fin_table_a(structured)
    for row in fin_rows:
        L.append(row)
    L.append("")

    # §7 公司调研大纲
    L.append("## 7 公司调研大纲")
    L.append("")
    L.append("- （待从材料中提取调研要点。）")
    L.append("")

    # §8 行业分析及同业对比
    L.append("## 8 行业分析及同业对比")
    L.append("")
    L.append("| 公司（代码） | 市值 | 营收 | 毛利率 | 竞争关系 |")
    L.append("|:---------|-----:|-----:|-----:|:-----|")
    L.append(f"| {company}（{ticker}） | — | — | — | 基准 |")
    L.append("")

    # §9 一致预期与估值
    L.append("## 9 一致预期与估值")
    L.append("")
    L.append("| 指标 | 2025A | 2026E | 2027E |")
    L.append("|:-----|-----:|-----:|-----:|")
    L.append("| 归母净利润（亿元） | — | — | — |")
    L.append("| EPS（元/股） | — | — | — |")
    L.append("")

    # §10 风险提示
    L.append("## 10 风险提示")
    L.append("")
    L.append("- （待从材料中提取风险因素。）")
    L.append("")


def _build_hk_sections(L, materials, structured, normalized, company, ticker, sources):
    """Generate all HK required sections."""
    src_count = len(sources)

    # §1-12 (HK structure)
    sections = [
        ("1 关键要点", "（从材料中提取核心要点。）"),
        ("2 近况跟踪", _source_bullets(sources[:4])),
        ("3 核心投资逻辑", ""),
        ("4 催化事件时间表", ""),
        ("5 业务拆分", ""),
        ("6 产销链与生态", ""),
        ("7 财务与盈利质量", _hk_fin_note(structured)),
        ("8 市场关注 / 待办", ""),
        ("9 行业对比", ""),
        ("10 市场分歧", ""),
        ("11 估值与预测", ""),
        ("12 风险提示", ""),
    ]
    for sec_title, content in sections:
        L.append(f"## {sec_title}")
        L.append("")
        if content:
            L.append(content)
        else:
            L.append("（待从材料中提取。）")
        # Sub-sections for §3
        if sec_title == "3 核心投资逻辑":
            L.append("### 3.1 短期逻辑")
            L.append("")
            L.append("（待提取。）")
            L.append("")
            L.append("### 3.2 长期逻辑")
            L.append("")
            L.append("（待提取。）")
        L.append("")


def _build_us_sections(L, materials, structured, normalized, company, ticker, sources):
    """US sections — same structure as HK."""
    _build_hk_sections(L, materials, structured, normalized, company, ticker, sources)


def _source_bullets(sources: list[dict]) -> str:
    if not sources:
        return "- （来源数据采集中。）"
    lines = []
    for s in sources:
        title = s.get("title", "")[:80]
        org = s.get("organization", "") or ""
        date = s.get("date", "")
        lines.append(f"- {date} | {org} | {title}")
    return "\n".join(lines)


def _hk_fin_note(structured: dict) -> str:
    has_is = structured.get("getHkFdmtIsPit")
    has_bs = structured.get("getHkFdmtBsPit")
    has_cf = structured.get("getHkFdmtCfPit")
    if has_is or has_bs or has_cf:
        return "（港股HK PIT结构化财务数据已采集。）"
    return "（港股结构化财务数据未采集。当前可能为保险等特殊行业。）"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: generate_report.py <frozen_materials.json> <output.md>")
        sys.exit(1)
    result = generate_report(sys.argv[1], sys.argv[2])
    print(json.dumps(result, ensure_ascii=False, indent=2))
