#!/usr/bin/env python3
"""
hk_financials.py — 港美股 PIT 三表数据聚合函数
==============================================
从 getHkFdmtIsPit / getHkFdmtBsPit / getHkFdmtCfPit 返回的
Point-in-Time 行项目数据中聚合 FY2023/FY2024/FY2025 年度主要指标。

用法:
    # 命令行 dry-run（最小测试）
    python hk_financials.py --dry-run

    # 从 JSON 文件提取
    python hk_financials.py --input hk_fin_data.json --output hk_fin_agg.json

    # Python 库调用
    from hk_financials import extract_hk_financials
    agg = extract_hk_financials(hk_fin_data)

要求:
    - 按 fiscalEndDate / endDate / reportDate 分组
    - 映射收入、毛利、经营利润、归母净利润、EPS、经营现金流、资产负债率等标准指标
    - 缺失项保留 None，不硬造数据
    - 输出 source_api、id_field、id_value、payload_hash
"""

from __future__ import annotations
import argparse, json, sys, os, hashlib
from collections import defaultdict
from typing import Any, Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
# PIT 行项目名称 → 标准指标映射（港股常见中英文行项目）
# ═══════════════════════════════════════════════════════════════

IS_LINE_MAP = {
    # 收入
    "revenue": "营业总收入",
    "total revenue": "营业总收入",
    "營業收入": "营业总收入",
    "收入": "营业总收入",
    "营业总收入": "营业总收入",
    "营业收入": "营业总收入",
    "收入合计": "营业总收入",
    "total operating income": "营业总收入",

    # 营业成本
    "cost of sales": "营业成本",
    "cost of revenue": "营业成本",
    "營業成本": "营业成本",
    "营业成本": "营业成本",
    "銷售成本": "营业成本",

    # 毛利
    "gross profit": "毛利",
    "gross profit (loss)": "毛利",
    "毛利": "毛利",
    "毛利润": "毛利",

    # 经营利润
    "operating profit": "经营利润",
    "operating income": "经营利润",
    "operating profit (loss)": "经营利润",
    "經營利潤": "经营利润",
    "营业利润": "经营利润",
    "經營溢利": "经营利润",
    "profit from operations": "经营利润",

    # 归母净利润
    "profit attributable to owners of the parent": "归母净利润",
    "profit attributable to equity holders of the company": "归母净利润",
    "profit for the year attributable to owners of the company": "归母净利润",
    "net profit attributable to shareholders of the parent": "归母净利润",
    "歸屬於母公司股東的淨利潤": "归母净利润",
    "归属于母公司股东的净利润": "归母净利润",
    "本公司权益持有人应占溢利": "归母净利润",
    "net income attributable to common stockholders": "归母净利润",

    # EPS（基本）
    "basic earnings per share": "基本EPS",
    "basic earnings per share (cents)": "基本EPS",
    "基本每股收益": "基本EPS",
    "每股基本盈利": "基本EPS",

    # 稀释EPS
    "diluted earnings per share": "稀释EPS",
    "稀释每股收益": "稀释EPS",

    # Non-GAAP / Adjusted 指标
    "adjusted net income": "Non-GAAP净利",
    "non-gaap net income": "Non-GAAP净利",
    "non-ifrs adjusted net profit": "Non-GAAP净利",
    "经调整净利润": "Non-GAAP净利",
    "non-gaap diluted eps": "Non-GAAP稀释EPS",
    "adjusted diluted eps": "Non-GAAP稀释EPS",
    "经调整每股收益": "Non-GAAP稀释EPS",
}

BS_LINE_MAP = {
    "total assets": "总资产",
    "總資產": "总资产",
    "总资产": "总资产",

    "total liabilities": "总负债",
    "總負債": "总负债",
    "总负债": "总负债",

    "total equity attributable to owners of the parent": "归母权益",
    "total equity": "总权益",
    "總權益": "总权益",
    "总权益": "总权益",

    "interest-bearing debt": "有息负债",
    "total borrowings": "有息负债",
    "借款": "有息负债",

    "cash and cash equivalents": "现金及等价物",
    "現金及現金等價物": "现金及等价物",
}

CF_LINE_MAP = {
    "net cash from operating activities": "经营现金流净额",
    "net cash generated from operating activities": "经营现金流净额",
    "經營活動現金流量淨額": "经营现金流净额",
    "经营活动产生的现金流量净额": "经营现金流净额",
    "net cash flows from operating activities": "经营现金流净额",

    "capital expenditure": "资本开支",
    "purchase of property, plant and equipment": "资本开支",
    "购置物业、厂房及设备": "资本开支",
    "purchase of pp&e": "资本开支",

    "free cash flow": "自由现金流",
    "free cash flow (fcf)": "自由现金流",
}


def _compute_payload_hash(data: dict) -> str:
    """计算 payload 哈希，用于 source_trace。"""
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fiscal_year_from_date(date_str: str) -> Optional[str]:
    """从 PIT 日期推断财年标签。"""
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(date_str[:10])
        # 财年通常截至3月或12月；简化处理：用年份标记
        month = d.month
        if month <= 6:
            return f"FY{d.year}"
        else:
            return f"FY{d.year + 1}"
    except (ValueError, IndexError):
        return None


def _group_by_fiscal_year(pit_data: dict) -> dict:
    """
    将 PIT 数据按 fiscalEndDate 分组。
    返回: { "FY2023": [items...], "FY2024": [...], ... }
    """
    groups = defaultdict(list)
    items = pit_data.get("data", pit_data.get("items", []))
    if isinstance(items, dict):
        items = [items]
    for item in items:
        fy = item.get("fiscalYear") or item.get("fiscal_end_date") or ""
        end_date = item.get("fiscalEndDate") or item.get("endDate") or ""
        report_date = item.get("reportDate") or ""
        if fy:
            # 标准化FY标签
            fy_str = str(fy).strip().upper()
            if not fy_str.startswith("FY"):
                fy_str = f"FY{fy_str}"
            groups[fy_str].append(item)
        elif end_date:
            fy_str = _fiscal_year_from_date(end_date)
            if fy_str:
                groups[fy_str].append(item)
        elif report_date:
            fy_str = _fiscal_year_from_date(report_date)
            if fy_str:
                groups[fy_str].append(item)
    return dict(groups)


def _map_line_items(items: list, line_map: dict) -> dict:
    """将行项目列表按 line_map 映射为标准指标名→数值。"""
    result = {}
    for item in items:
        name = (item.get("lineItem") or item.get("itemName") or item.get("item") or "").strip()
        name_lower = name.lower()
        # 尝试精确匹配和模糊匹配
        matched_std = None
        for key, std_name in line_map.items():
            if name_lower == key.lower() or key.lower() in name_lower or name_lower in key.lower():
                matched_std = std_name
                break
        if matched_std:
            val = item.get("amount") or item.get("value") or item.get("amt")
            if val is not None:
                try:
                    result[matched_std] = float(val)
                except (ValueError, TypeError):
                    pass
    return result


def extract_hk_financials(hk_fin_data: dict) -> dict:
    """
    从 PIT 行项目聚合 FY2023/FY2024/FY2025 年度主要指标。

    参数:
        hk_fin_data: {
            "is": { "data": [...PIT items...], "source_api": "getHkFdmtIsPit", ... },
            "bs": { "data": [...PIT items...], "source_api": "getHkFdmtBsPit", ... },
            "cf": { "data": [...PIT items...], "source_api": "getHkFdmtCfPit", ... },
            "id_field": "ticker",
            "id_value": "00700"
        }

    返回:
        {
            "FY2023": { "营业总收入": 1234.5, "归母净利润": 234.5, ... },
            "FY2024": { ... },
            "FY2025": { ... },
            "_meta": {
                "source_api": ["getHkFdmtIsPit", "getHkFdmtBsPit", "getHkFdmtCfPit"],
                "id_field": "ticker",
                "id_value": "00700",
                "payload_hash": "abc123...",
                "extracted_at": "2026-07-08T..."
            }
        }
    """
    id_field = hk_fin_data.get("id_field", "ticker")
    id_value = hk_fin_data.get("id_value", "")

    is_data = hk_fin_data.get("is", {})
    bs_data = hk_fin_data.get("bs", {})
    cf_data = hk_fin_data.get("cf", {})

    # 分组
    is_groups = _group_by_fiscal_year(is_data)
    bs_groups = _group_by_fiscal_year(bs_data)
    cf_groups = _group_by_fiscal_year(cf_data)

    # 获取所有年份
    all_years = sorted(set(list(is_groups.keys()) + list(bs_groups.keys()) + list(cf_groups.keys())),
                       reverse=True)
    # 只保留最近3年
    recent_years = all_years[:3]

    result = {}
    for fy in recent_years:
        fy_result = {}

        # IS 指标
        if fy in is_groups:
            fy_result.update(_map_line_items(is_groups[fy], IS_LINE_MAP))

        # BS 指标
        if fy in bs_groups:
            fy_result.update(_map_line_items(bs_groups[fy], BS_LINE_MAP))

        # CF 指标
        if fy in cf_groups:
            fy_result.update(_map_line_items(cf_groups[fy], CF_LINE_MAP))

        # 衍生指标
        # 资产负债率
        total_assets = fy_result.get("总资产")
        total_liabilities = fy_result.get("总负债")
        if total_assets and total_liabilities and total_assets > 0:
            fy_result["资产负债率(%)"] = round(total_liabilities / total_assets * 100, 2)

        # ROE (仅当有归母净利润和归母权益时)
        net_profit = fy_result.get("归母净利润")
        equity = fy_result.get("归母权益")
        if net_profit and equity and equity > 0:
            fy_result["ROE(%)"] = round(net_profit / equity * 100, 2)

        # 毛利率
        revenue = fy_result.get("营业总收入")
        cost = fy_result.get("营业成本")
        if revenue and cost and revenue > 0:
            fy_result["毛利率(%)"] = round((revenue - cost) / revenue * 100, 2)
        elif "毛利" in fy_result and revenue and revenue > 0:
            fy_result["毛利率(%)"] = round(fy_result["毛利"] / revenue * 100, 2)

        # 净利率
        if revenue and revenue > 0:
            if "归母净利润" in fy_result:
                fy_result["净利率(%)"] = round(fy_result["归母净利润"] / revenue * 100, 2)
            elif "Non-GAAP净利" in fy_result:
                fy_result["净利率(Non-GAAP)(%)"] = round(fy_result["Non-GAAP净利"] / revenue * 100, 2)

        result[fy] = fy_result

    result["_meta"] = {
        "source_api": [
            is_data.get("source_api", "getHkFdmtIsPit"),
            bs_data.get("source_api", "getHkFdmtBsPit"),
            cf_data.get("source_api", "getHkFdmtCfPit"),
        ],
        "id_field": id_field,
        "id_value": id_value,
        "payload_hash": _compute_payload_hash(hk_fin_data),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
    }

    return result


def extract_us_financials(us_fin_data: dict) -> dict:
    """
    美股财务数据聚合——从中概股/ADR的HK PIT兼容格式提取。
    若为纯美股（无HK PIT），主要从研报/财报点评抽取，此函数返回空聚合体。
    参见 extract_hk_financials 返回格式。
    """
    # US stocks typically don't have HK PIT, return empty meta
    return {
        "_meta": {
            "source_api": ["skipped — 美股无HK PIT结构化三表"],
            "id_field": us_fin_data.get("id_field", "ticker"),
            "id_value": us_fin_data.get("id_value", ""),
            "payload_hash": "skipped_us",
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
            "note": (
                "美股缺少HK PIT结构化三表，财务数据须从研报/财报点评/公告中抽取。"
                "评测记录须写为 skipped_with_reason / N/A。"
            ),
        }
    }


# ═══════════════════════════════════════════════════════════════
# 命令行接口 (dry-run 最小测试)
# ═══════════════════════════════════════════════════════════════

def _dry_run():
    """最小 dry-run 测试。"""
    print("hk_financials.py dry-run: 函数导入测试")
    print(f"  IS_LINE_MAP: {len(IS_LINE_MAP)} entries")
    print(f"  BS_LINE_MAP: {len(BS_LINE_MAP)} entries")
    print(f"  CF_LINE_MAP: {len(CF_LINE_MAP)} entries")

    # 模拟数据测试
    sample = {
        "is": {
            "data": [
                {"lineItem": "revenue", "amount": 6090, "fiscalYear": "FY2024"},
                {"lineItem": "cost of sales", "amount": 3290, "fiscalYear": "FY2024"},
                {"lineItem": "gross profit", "amount": 2800, "fiscalYear": "FY2024"},
                {"lineItem": "operating profit", "amount": 1840, "fiscalYear": "FY2024"},
                {"lineItem": "profit attributable to owners of the parent", "amount": 1530, "fiscalYear": "FY2024"},
                {"lineItem": "basic earnings per share", "amount": 16.8, "fiscalYear": "FY2024"},
            ],
            "source_api": "getHkFdmtIsPit",
        },
        "bs": {
            "data": [
                {"lineItem": "total assets", "amount": 20150, "fiscalYear": "FY2024"},
                {"lineItem": "total liabilities", "amount": 9340, "fiscalYear": "FY2024"},
                {"lineItem": "total equity", "amount": 10810, "fiscalYear": "FY2024"},
            ],
            "source_api": "getHkFdmtBsPit",
        },
        "cf": {
            "data": [
                {"lineItem": "net cash from operating activities", "amount": 1850, "fiscalYear": "FY2024"},
            ],
            "source_api": "getHkFdmtCfPit",
        },
        "id_field": "ticker",
        "id_value": "00700",
    }

    result = extract_hk_financials(sample)
    print(f"\n  聚合结果:")
    for fy in sorted(result.keys()):
        if fy.startswith("_"):
            continue
        print(f"  {fy}:")
        for k, v in result[fy].items():
            print(f"    {k}: {v}")
    print(f"  _meta: {json.dumps(result.get('_meta', {}), ensure_ascii=False, indent=4)}")
    print("\n  ✅ dry-run 通过")


def main():
    parser = argparse.ArgumentParser(description="港美股 PIT 三表数据聚合")
    parser.add_argument("--dry-run", action="store_true", help="最小 dry-run 测试")
    parser.add_argument("--input", help="输入 JSON 文件路径")
    parser.add_argument("--output", help="输出聚合 JSON 文件路径")
    parser.add_argument("--market", choices=["hk", "us"], default="hk", help="市场类型")
    args = parser.parse_args()

    if args.dry_run:
        _dry_run()
        return

    if not args.input:
        print("用法: python hk_financials.py --input <hk_fin_data.json> --output <agg.json>", file=sys.stderr)
        print("      python hk_financials.py --dry-run  (最小测试)", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.market == "us":
        result = extract_us_financials(data)
    else:
        result = extract_hk_financials(data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"✅ 聚合结果已保存: {args.output}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
