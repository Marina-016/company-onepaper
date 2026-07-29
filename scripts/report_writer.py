# -*- coding: utf-8 -*-
"""
report_writer.py — 从 fetch_data.py 输出的 JSON 直接生成完整公司一页纸报告
==========================================================================
用法:
    python -X utf8 report_writer.py \\
        --data  <json文件路径>  \\
        --output <md输出路径>   \\
        [--docx  <docx输出路径>] \\
        [--api-key <Anthropic API Key>]

说明:
    - 从 JSON 提取所有结构化数据（表格部分纯 Python 生成）
    - 叙述性章节（1/2/3/4/5/6.2/7/8/9.3/9.4/10）并行调用 Claude API 生成
    - 所有输出直接写文件，不在终端打印完整报告（减少 token 输出耗时）
    - API Key 优先级：--api-key 参数 > ANTHROPIC_API_KEY 环境变量
    - 目标：整体完成时间 < 3 分钟
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import glob

# Windows 控制台 GBK 编码兼容：强制 stdout/stderr 输出 UTF-8
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# 常量 & 系统提示
# ─────────────────────────────────────────────────────────────────────────────

TODAY = datetime.date.today().isoformat()

# 域名白名单：LLM API 端点只能指向受信任的地址
ALLOWED_LLM_HOSTS = {
    "api.anthropic.com",
    "api.openai.com",
    "llm-proxy.datayes.com",
    "llm-proxy.wmcloud.com",
    "openai.datayes.com",
    "gateway.ai.cloud.ru",
}

def _check_llm_host(url: str):
    """校验 LLM 端点域名是否在白名单中，防止请求发送到非受信任地址。"""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    if hostname not in ALLOWED_LLM_HOSTS:
        raise ValueError(f"LLM endpoint host not in allowlist: {hostname}")

# 模型 ID —— 由 main() 根据 --model 参数或环境变量覆盖
MODEL = "claude-sonnet-4-6"

# LLM 端点配置（由 main() 填入，支持任意 OpenAI-compatible 或 Anthropic Messages 端点）
_LLM_ENDPOINT = ""   # 如 https://api.openai.com/v1 或平台代理地址
_LLM_API_KEY  = ""   # Bearer token / API key
_LLM_FORMAT   = "openai"  # "openai"（/chat/completions）或 "anthropic"（/v1/messages）

SYSTEM_PROMPT = """你是一位在顶级投行工作30年的资深券商分析师，根据结构化数据撰写中文研究报告片段。

严格规则：
1. 历史财务数据使用接口返回的精确数字，严禁"约"/"大约"等模糊表述
2. 正文叙述不得出现具体券商/机构名称（用"头部机构"/"主流机构"等代替）
3. 关键数据须用 [N] 角标标注来源，N 为参考资料序号（参照传入的引用序号映射）
4. 列举项使用 • 或 1）2）格式，严禁中文序号 1、2、3
5. 只输出被要求的章节内容，不要输出其他说明或标题（标题由主程序添加）
6. 字数限制：若指定了字数范围，请严格遵守
"""


# ─────────────────────────────────────────────────────────────────────────────
# 全局LLM并发控制（429自动降级）
# ─────────────────────────────────────────────────────────────────────────────
_LLM_CONCURRENCY = 4
_llm_sem = threading.Semaphore(_LLM_CONCURRENCY)
_llm_sem_lock = threading.Lock()
_llm_concurrency_reduced = [False]

def _reduce_llm_concurrency():
    """LLM API 429持续触发时将并发降至1（只执行一次）。"""
    with _llm_sem_lock:
        if _llm_concurrency_reduced[0]:
            return
        _llm_concurrency_reduced[0] = True
    print("  ⚠️ LLM API 429限流，并发降至1（串行模式）", flush=True)
    for _ in range(_LLM_CONCURRENCY - 1):
        _llm_sem.acquire()

# ─────────────────────────────────────────────────────────────────────────────
# Claude API 调用
# ─────────────────────────────────────────────────────────────────────────────

def call_claude(_client, prompt: str, max_tokens: int = 2000) -> str:
    """调用 LLM API（纯 HTTP，自动适配 OpenAI-compatible 或 Anthropic Messages 格式）。
    _client 参数保留仅为兼容现有调用签名。
    失败时重试 2 次；429 限流时触发全局并发降至 1。
    """
    import requests as _req
    _max = max_tokens
    _llm_sem.acquire()
    try:
        for attempt in range(3):
            try:
                if _LLM_FORMAT == "anthropic":
                    # Anthropic Messages API（支持 Claude Code 代理 / 直连 Anthropic）
                    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
                    if _LLM_API_KEY.startswith("sk-ant-"):
                        headers["x-api-key"] = _LLM_API_KEY
                    else:
                        headers["Authorization"] = f"Bearer {_LLM_API_KEY}"
                    url = _LLM_ENDPOINT.rstrip("/") + "/v1/messages"
                    payload = {
                        "model": MODEL,
                        "max_tokens": _max,
                        "system": SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    resp = _req.post(url, headers=headers, json=payload, timeout=120)
                    if resp.status_code == 429:
                        _reduce_llm_concurrency()
                        if attempt < 2:
                            time.sleep(3 * (attempt + 1))
                            continue
                        return "[生成失败: 429 Rate Limit]"
                    resp.raise_for_status()
                    data = resp.json()
                    # Anthropic Messages 响应兼容：
                    # - 标准：content[].type="text" → content[].text
                    # - Thinking 模型（DeepSeek-V4 等）：content 中可能先有 type="thinking" 块，
                    #   正文在后续 type="text" 块中
                    # - 部分代理/Bedrock 可能返回 content[0] 为字符串或其他形态
                    content_blocks = data.get("content", [])
                    if not content_blocks:
                        text = data.get("output") or data.get("message") or data.get("text") or ""
                        if isinstance(text, list):
                            text = "".join(str(b.get("text", b)) for b in text)
                    else:
                        # 优先取所有 type="text" 的块拼接
                        text_parts = []
                        for b in content_blocks:
                            if isinstance(b, str):
                                text_parts.append(b)
                            elif isinstance(b, dict):
                                if b.get("type") == "text":
                                    text_parts.append(b.get("text", ""))
                        text = "".join(text_parts)
                        # 如果没有任何 text 块，回退到最后一个块的 text 字段或整体转字符串
                        if not text:
                            last = content_blocks[-1]
                            if isinstance(last, dict):
                                text = last.get("text", "") or str(last)
                            else:
                                text = str(last)
                    if not text:
                        raise RuntimeError(f"无法从响应提取文本，content块数={len(content_blocks)}")
                    text = text.strip()
                    if data.get("stop_reason") == "max_tokens" and _max < 4096:
                        _max = min(_max + 1000, 4096)
                        continue
                    return text
                else:
                    # OpenAI-compatible /chat/completions
                    url = _LLM_ENDPOINT.rstrip("/") + "/chat/completions"
                    resp = _req.post(
                        url,
                        headers={"Authorization": f"Bearer {_LLM_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "model": MODEL,
                            "max_tokens": _max,
                            "messages": [
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user",   "content": prompt},
                            ],
                        },
                        timeout=120,
                    )
                    if resp.status_code == 429:
                        _reduce_llm_concurrency()
                        if attempt < 2:
                            time.sleep(3 * (attempt + 1))
                            continue
                        return "[生成失败: 429 Rate Limit]"
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    if data["choices"][0].get("finish_reason") == "length" and _max < 4096:
                        _max = min(_max + 1000, 4096)
                        continue
                    return text
            except Exception as e:
                err_str = str(e)
                is_429 = "429" in err_str or "rate_limit" in err_str.lower()
                if is_429:
                    _reduce_llm_concurrency()
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                else:
                    return "[生成失败: " + err_str + "]"
    finally:
        _llm_sem.release()


# ─────────────────────────────────────────────────────────────────────────────
# 数据提取
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(val, unit=1e8, decimals=2):
    """将原始数值转换为亿元字符串"""
    if val is None:
        return "—"
    v = val / unit
    return f"{v:.{decimals}f}"


def _pct(val, decimals=2):
    if val is None:
        return "—"
    return f"{val:.{decimals}f}%"


def extract_financial(data: dict) -> dict:
    """从 fdmtNew dataRow 提取关键财务指标。
    - 年报列：近3年 reportPeriodType==A 的记录，key 为整数年份
    - 最新期列：若最新一期不是年报（Q1/S1/CQ3），单独提取，key 为 'latest_data'
    - 返回 latest={year, type, label, idx, prev_idx} 描述最新期
    """
    fin = data.get("financial", {})
    fin_data = fin.get("data", {}) if isinstance(fin, dict) else {}
    rows = fin_data.get("dataRow", []) if isinstance(fin_data, dict) else []
    title_bar = fin_data.get("titleBar", []) if isinstance(fin_data, dict) else []

    codes_map = {}
    for row in rows:
        c = row.get("code", "")
        d = row.get("data", [])
        if c and d:
            codes_map[c] = d

    def get_by_idx(code, idx):
        vals = codes_map.get(code, [])
        return vals[idx] if idx is not None and idx < len(vals) else None

    # 固定财务指标码表
    CODES = {
        "tRevenue":    "tRevenue",
        "revenueYOY":  "revenueYOY",
        "NPAttrP":     "NIncomeAttrP",
        "NPAttrPYOY":  "niAttrPYOY",
        "grossMargin": "operateProfitRatio",
        "netMargin":   "npMARgin",
        "ROEW":        "ROEW",
        "ROE":         "ROE",
        "operCashFlow":"NCFOperateANotes",
        "totalAssets": "TAssets",
        "liabRatio":   "asseTLiabRatio",
        "basicEPS":    "basicEPS",
        "EPS":         "EPS",
        "totalEquity": "TShEquity",
        "nAssetsPS":   "nAssetsPS",
    }

    # 只取年报（reportPeriodType == "A"），降序按年份取最近3条
    annual_entries = sorted(
        [(i, tb) for i, tb in enumerate(title_bar) if tb.get("reportPeriodType") == "A"],
        key=lambda x: x[1]["year"], reverse=True
    )[:3]
    years = [e[1]["year"] for e in annual_entries]

    # 映射最新期 period type → 报告标签后缀
    _PERIOD_LABEL = {"Q1": "Q1", "Q2": "S1", "S1": "S1", "H1": "S1",
                     "Q3": "CQ3", "CQ3": "CQ3", "A": "A"}

    # 最新一条 titleBar 是否为非年报期
    latest_period = None
    if title_bar:
        first_tb = title_bar[0]
        first_type = first_tb.get("reportPeriodType", "A")
        if first_type != "A":
            latest_year = first_tb["year"]
            label = f"{latest_year}{_PERIOD_LABEL.get(first_type, first_type)}"
            # 找同期上年数据索引（用于 YoY）
            prev_idx = None
            for j, tb in enumerate(title_bar):
                if tb.get("year") == latest_year - 1 and tb.get("reportPeriodType") == first_type:
                    prev_idx = j
                    break
            latest_period = {"year": latest_year, "type": first_type,
                             "label": label, "idx": 0, "prev_idx": prev_idx}

    result = {"years": years, "raw": codes_map}

    # 填充3年年报数据
    for ann_idx, tb in annual_entries:
        yr = tb["year"]
        result[yr] = {key: get_by_idx(raw_code, ann_idx) for key, raw_code in CODES.items()}

    # 填充最新期数据
    if latest_period:
        lt_idx = latest_period["idx"]
        pr_idx = latest_period.get("prev_idx")
        result["latest"] = latest_period
        result["latest_data"]      = {key: get_by_idx(raw_code, lt_idx) for key, raw_code in CODES.items()}
        result["latest_prev_data"] = ({key: get_by_idx(raw_code, pr_idx) for key, raw_code in CODES.items()}
                                      if pr_idx is not None else {})

    return result


def extract_maincomp(data: dict) -> dict:
    """从 getFdmtMoStdItem 返回数据中提取主营构成（近3年年报，带层级结构）

    层级规则：
    - 一级项目直接展示（原始名称）
    - 若一级项目下有子项，则同时展示该一级项目和其子项（子项名前加 "└ "）
    - 返回 order 列表控制显示顺序：父级在前，子级紧随其后
    """
    mc_raw = data.get("main_comp", {})
    records = mc_raw.get("data", []) if isinstance(mc_raw, dict) else []
    if not records:
        return {"years": [], "totals": [], "segments": {}, "margins": {}, "order": []}

    # 只取年报期（endDate 以 -12-31 结尾），降序取最近 3 年
    annual_dates = sorted(
        set(r["endDate"] for r in records if str(r.get("endDate", "")).endswith("-12-31")),
        reverse=True
    )[:3]
    if not annual_dates:
        return {"years": [], "totals": [], "segments": {}, "margins": {}, "order": []}

    n = len(annual_dates)
    years = [d[:4] for d in annual_dates]

    # 用最新一期数据确定层级顺序与 key 映射
    first_recs = [r for r in records if r.get("endDate") == annual_dates[0]]

    # itemID -> 原始 itemName
    id_to_name = {r["itemID"]: r.get("itemName", "")
                  for r in first_recs if r.get("itemID", 0) != 0}
    # parent itemID -> [child itemID, ...]（按原始顺序）
    children_of = {}
    for r in first_recs:
        sup = r.get("itemIDSuperior")
        iid = r.get("itemID", 0)
        if sup is not None and sup != 0 and iid != 0:
            children_of.setdefault(sup, []).append(iid)

    # 构建有序 key 列表（父级→子级），key = segments dict 的键
    order = []
    for r in first_recs:
        iid = r.get("itemID", 0)
        sup = r.get("itemIDSuperior")
        if iid == 0 or sup != 0:   # 跳过合计行和非一级项目
            continue
        name = r.get("itemName", "")
        if not name:
            continue
        order.append(name)                        # 一级项目（原名）
        for child_id in children_of.get(iid, []):
            child_name = id_to_name.get(child_id, "")
            if child_name:
                order.append("└ " + child_name)  # 子项（带缩进前缀）

    # 初始化 segments / margins
    segments = {k: [None] * n for k in order}
    margins  = {k: [None] * n for k in order}
    totals   = []

    for i, date in enumerate(annual_dates):
        period_recs = [r for r in records if r.get("endDate") == date]

        # 合计行
        total_row = next((r for r in period_recs
                          if r.get("itemID") == 0 and "itemIDSuperior" not in r), None)
        totals.append(total_row["revenue"] / 1e8
                      if total_row and total_row.get("revenue") is not None else None)

        # 填充各板块数据
        for row in period_recs:
            iid  = row.get("itemID", 0)
            sup  = row.get("itemIDSuperior")
            name = row.get("itemName", "")
            if iid == 0 or not name:
                continue
            key = ("└ " + name) if (sup is not None and sup != 0) else name
            if key not in segments:     # 后续年份出现的新板块
                segments[key] = [None] * n
                margins[key]  = [None] * n
                order.append(key)
            rev = row.get("revenue")
            mgn = row.get("grossMargin") or row.get("grossMarginStd")
            segments[key][i] = rev / 1e8 if rev is not None else None
            margins[key][i]  = mgn if mgn is not None else None

    # 毛利率全为 None 则不显示毛利率列
    margins = {k: v for k, v in margins.items() if any(x is not None for x in v)}

    return {"years": years, "totals": totals, "segments": segments,
            "margins": margins, "order": order}


def extract_main_comp_region(data: dict) -> str:
    """提取按地区分类的主营业务构成，返回近3年年报的 Markdown 表格字符串。
    无数据时返回空字符串。
    """
    raw = data.get("main_comp_region", {})
    items = []
    if isinstance(raw, dict):
        inner = raw.get("data", raw)
        if isinstance(inner, list):
            items = inner
    elif isinstance(raw, list):
        items = raw

    if not items:
        return ""

    _SKIP = {"合计", "其他差额项目(计算)"}

    # 只取年报（endDate 以 -12-31 结尾）
    annual = [
        it for it in items
        if str(it.get("endDate", "")).endswith("-12-31")
        and it.get("itemName") not in _SKIP
    ]
    if not annual:
        return ""

    # 近3年的年份（降序）
    years = sorted({str(it["endDate"])[:4] for it in annual}, reverse=True)[:3]

    # 按地区聚合：{region: {year: {rev, yoy, gm}}}
    regions: dict = {}
    for it in annual:
        yr = str(it["endDate"])[:4]
        if yr not in years:
            continue
        name = it.get("itemName", "")
        if name not in regions:
            regions[name] = {}
        rev = it.get("revenue")
        yoy = it.get("revYOY")
        gm  = it.get("grossMargin")
        regions[name][yr] = {
            "rev": round(rev / 1e8, 2) if rev is not None else None,
            "yoy": round(yoy, 2) if yoy is not None else None,
            "gm":  round(gm, 2) if gm is not None else None,
        }

    if not regions:
        return ""

    def _fv(v): return f"{v:.2f}" if v is not None else "—"
    def _yoy(v): return f"{v:+.1f}%" if v is not None else "—"

    # 表头：最新年在左，同比紧随其后
    y0, y1, y2 = (years + ["", ""])[:3]
    header = f"| 地区 | {y0}A营收（亿） | 同比 | {y1}A营收（亿） | 同比 | {y2}A营收（亿） | 毛利率（{y0}A） |"
    sep    = "|:-----|:------|:------|:------|:------|:------|:------|"
    rows   = []
    for region, yr_data in regions.items():
        d0 = yr_data.get(y0, {})
        d1 = yr_data.get(y1, {}) if y1 else {}
        d2 = yr_data.get(y2, {}) if y2 else {}
        rows.append(
            f"| {region} | {_fv(d0.get('rev'))} | {_yoy(d0.get('yoy'))} "
            f"| {_fv(d1.get('rev'))} | {_yoy(d1.get('yoy'))} "
            f"| {_fv(d2.get('rev'))} | {_fv(d0.get('gm'))}% |"
        )

    return "\n".join([header, sep] + rows)


def extract_consensus(data: dict) -> list:
    """提取一致预期数据列表"""
    con = data.get("consensus", {})
    con_data = con.get("data", {}) if isinstance(con, dict) else {}
    lst = con_data.get("list", []) if isinstance(con_data, dict) else []
    # 过滤出 conEpsType=2（预测值），按 conProfit 升序（近年在前）
    forecasts = [x for x in lst if x.get("conEpsType") == 2]
    forecasts.sort(key=lambda x: x.get("conProfit", 0))
    return forecasts


def extract_actual_consensus(data: dict) -> dict:
    """提取最新实际年度数据（conEpsType=0, PE较低的那条）"""
    con = data.get("consensus", {})
    con_data = con.get("data", {}) if isinstance(con, dict) else {}
    lst = con_data.get("list", []) if isinstance(con_data, dict) else []
    actuals = [x for x in lst if x.get("conEpsType") == 0]
    # 取 conPe 最小的（最近期实际）
    if actuals:
        return min(actuals, key=lambda x: x.get("conPe", 999))
    return {}


def extract_profit_forecast(data: dict) -> dict:
    """提取各机构盈利预测，按 orgName 分组（research_sec_foredata 接口）"""
    pf = data.get("profit_forecast", {})
    pf_data = pf.get("data", {}) if isinstance(pf, dict) else {}
    lst = pf_data.get("list", []) if isinstance(pf_data, dict) else []

    orgs = {}
    for item in lst:
        org_name = item.get("orgName", "")
        fy = item.get("foreYear")
        if not org_name or not fy:
            continue
        if org_name not in orgs:
            orgs[org_name] = {"name": org_name, "data": {}}
        # 同一机构同一年取最新一条（列表已按 writeDate desc 排序）
        if fy not in orgs[org_name]["data"]:
            orgs[org_name]["data"][fy] = {
                "profit": item.get("foreProfit"),   # 元
                "eps":    item.get("foreEps"),
                "income": item.get("foreIncome"),   # 元
                "date":   item.get("writeDate", ""),
            }
    # 取最新日期的前5家
    def latest_date(org):
        dates = [v["date"] for v in org["data"].values() if v.get("date")]
        return max(dates) if dates else ""

    sorted_orgs = sorted(orgs.values(), key=latest_date, reverse=True)
    return sorted_orgs[:5]


def extract_valuation(data: dict) -> dict:
    """提取估值排名数据"""
    vr = data.get("valuation_rank", {})
    vr_data = vr.get("data", {}) if isinstance(vr, dict) else {}
    comment = vr_data.get("comment", "") if isinstance(vr_data, dict) else ""
    items = vr_data.get("rankItemList", []) if isinstance(vr_data, dict) else []
    result = {"comment": comment, "items": {}}
    for it in items:
        result["items"][it.get("name", "")] = {
            "val": it.get("val"),
            "avg": it.get("avg"),
            "rank": it.get("rank"),
            "rankBase": it.get("rankBase"),
            "evaluation": it.get("evaluation"),
        }
    return result


def extract_reports_summary(data: dict) -> list:
    """提取研报关键信息列表"""
    reports = data.get("research_reports", [])
    result = []
    for r in reports:
        meta = r.get("_meta", {})
        detail = r.get("detail", {})
        dd = detail.get("data", {}) if isinstance(detail, dict) else {}
        content_data = r.get("content", {})
        cdata = content_data.get("data", {}) if isinstance(content_data, dict) else {}
        rid = str(meta.get("id", ""))
        full_text = cdata.get(rid, "") if isinstance(cdata, dict) else ""

        # detail.data.textAbstract 通常比 _meta.abstractText 更完整
        detail_text_raw = dd.get("textAbstract", "") or ""
        detail_text = re.sub(r"<[^>]+>", " ", detail_text_raw).strip()

        result.append({
            "id":          meta.get("id", ""),
            "org":         meta.get("orgName", ""),
            "date":        meta.get("publishTime", "")[:10],
            "title":       meta.get("title", ""),
            "abstract":    meta.get("abstractText", ""),
            "detail_text": detail_text[:4000],   # HTML-stripped textAbstract，比abstract更完整
            "rating":      dd.get("rating", ""),
            "target":      dd.get("targetPrice", ""),
            "text":        full_text[:8000] if full_text else "",  # 保留更多正文用于深度分析
        })
    return result


def extract_meetings_summary(data: dict) -> list:
    """提取会议纪要关键信息

    字段路径说明（经实测确认）：
      detail.data.aiOverview  — AI 概述（字符串，top-level 字段）
      detail.data.aiQa        — Q&A 摘要（字符串，top-level 字段）
      detail.data.aiOriBody   — 完整会议正文 / 转录文本（字符串，top-level 字段）
      detail.data.aiSummary   — 极短摘要（字符串，非 dict，不包含子字段）
    """
    meetings = data.get("meetings", [])
    result = []
    for m in meetings:
        detail = m.get("detail", {})
        d = detail.get("data", {}) if isinstance(detail, dict) else {}
        if not isinstance(d, dict):
            continue

        # 顶层字段（不在 aiSummary 内）
        overview  = str(d.get("aiOverview",  "") or "")
        qa_raw    = str(d.get("aiQa",        "") or "")
        ori_body  = str(d.get("aiOriBody",   "") or "")

        # 日期优先从 detail.data 取
        pub_time = d.get("publishTime") or m.get("publishTime") or ""

        result.append({
            "id":      m.get("id", ""),
            "title":    m.get("title", ""),
            "date":     str(pub_time)[:10],
            "type":     str(d.get("meetingType") or m.get("meetingType") or ""),
            "overview": overview[:2000],
            "qa":       qa_raw[:4000],
            "text":     ori_body[:8000],   # 完整正文，供需要深度阅读的章节使用
        })

    # 过滤无内容条目，并按日期降序排列（最新最重要的在前）
    result = [m for m in result if m["overview"] or m["qa"] or m["text"]]
    result.sort(key=lambda x: x["date"], reverse=True)
    return result


def extract_surveys_detail(data: dict) -> list:
    """提取机构调研详情（institution_research_detail.content）

    fetch_data.py 已将 institution_research_detail 接口结果作为
    org_survey 每条记录的 detail_content 字段存入。
    """
    raw = data.get("org_survey", {})
    raw_data = raw.get("data", {}) if isinstance(raw, dict) else {}
    if isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        items = raw_data.get("list") or []
    else:
        items = []

    result = []
    for it in (items if isinstance(items, list) else []):
        content = it.get("detail_content") or ""
        if not content:
            continue
        result.append({
            "event_id":   it.get("eventID", ""),
            "date":       str(it.get("surveyDate", ""))[:10],
            "type":       str(it.get("activityType", "") or ""),
            "content":    str(content)[:6000],
        })

    result.sort(key=lambda x: x["date"], reverse=True)
    return result


def build_ref_map(data: dict) -> dict:
    """构建引用序号映射 {描述: 序号}，用于生成参考资料章节"""
    refs = {}
    idx = 1

    # 研报
    for r in extract_reports_summary(data):
        if r["id"]:
            key = "report_" + str(r["id"])
            refs[key] = {
                "n": idx,
                "type": "研报",
                "id": r["id"],
                "date": r["date"],
                "org": r["org"],
                "title": r["title"],
            }
            idx += 1

    # 会议纪要（按日期降序，已在 extract_meetings_summary 中排过序）
    for m in extract_meetings_summary(data):
        if m["title"]:
            key = "meeting_" + m["date"] + "_" + m["title"][:20]
            refs[key] = {
                "n": idx,
                "type": "纪要",
                "id": str(m.get("id") or "—"),
                "date": m["date"],
                "org": m["type"],
                "title": m["title"],
            }
            idx += 1

    # 结构化数据来源（ref_key: 引用键名, data_key: JSON中的实际字段名, desc: 描述）
    structured = [
        ("fdmtNew",          "financial",       "fdmtNew 财务摘要（近3年年报）"),
        ("maincomp",         "main_comp",       "getFdmtMoStdItem 主营构成"),
        ("consensus",        "consensus",       "research_sec_coredata 市场一致预期"),
        ("profit_forecast",  "profit_forecast", "research_sec_foredata 各机构盈利预测"),
        ("valuation_rank",   "valuation_rank",  "diagnosis_valuation_rank 同业估值排名"),
        ("pe_valuation",     "pe_valuation",    "diagnosis_pe_valuation PE估值百分位"),
        ("mgmt_discussion",  "mgmt_discussion", "management_discussion MD&A"),
        ("fin_indicators",   "fin_indicators",  "fdmt_indi_rtn 盈利能力指标历史序列"),
    ]
    for ref_key, data_key, desc in structured:
        if data.get(data_key):
            refs[ref_key] = {"n": idx, "type": "结构化数据", "id": "—", "date": TODAY, "org": "通联数据", "title": desc}
            idx += 1

    return refs


def refs_to_markdown(ref_map: dict) -> str:
    """生成参考资料章节 markdown"""
    lines = ["## 参考资料\n", "```"]
    sorted_refs = sorted(ref_map.values(), key=lambda x: x["n"])
    for r in sorted_refs:
        if r["type"] == "结构化数据":
            lines.append(
                f"[{r['n']}] {r['type']} | {r['date']} | {r['org']} | {r['title']}"
            )
        else:
            lines.append(
                f"[{r['n']}] {r['type']} | id:{r['id']} | {r['date']} | {r['org']} | {r['title']}"
            )
    lines.append("```")
    return "\n".join(lines)


def get_ref_n(ref_map: dict, *keys) -> str:
    """返回多个引用序号，如 [1][2][3]"""
    ns = []
    for k in keys:
        if k in ref_map:
            ns.append(f"[{ref_map[k]['n']}]")
    return "".join(ns)


# ─────────────────────────────────────────────────────────────────────────────
# 纯 Python 表格生成
# ─────────────────────────────────────────────────────────────────────────────

def gen_financial_table(fin: dict, q1_text: str = "", company_name: str = "") -> str:
    """生成 6.1 关键财务指标表格。
    - 若最新期为年报（fin['latest'] 不存在）：3列年报 + YoY(y0 vs y1)
    - 若最新期为季报/半年报：3列年报 + 最新期列 + YoY(最新期 vs 同期上年)
    """
    years = fin["years"]  # [2025, 2024, 2023]
    if not years:
        return ""

    y0, y1, y2 = (years + [None, None, None])[:3]
    latest = fin.get("latest")          # {year, type, label, idx, prev_idx}

    def _v(yr_key, code):
        if yr_key == "lt":
            return fin.get("latest_data", {}).get(code)
        elif yr_key == "lt_prev":
            return fin.get("latest_prev_data", {}).get(code)
        else:
            return fin.get(yr_key, {}).get(code) if yr_key else None

    def _fv(v, unit=1e8, decimals=2, is_pct=False):
        if v is None: return "—"
        return _pct(v, decimals) if is_pct else _fmt(v, unit, decimals)

    def row(label, code, unit=1e8, decimals=2, is_pct=False):
        v0 = _v(y0, code); v1 = _v(y1, code); v2 = _v(y2, code)
        if latest:
            v_lt      = _v("lt",      code)
            v_lt_prev = _v("lt_prev", code)
            yoy = (f"{(v_lt - v_lt_prev) / abs(v_lt_prev) * 100:+.2f}%"
                   if v_lt is not None and v_lt_prev not in (None, 0) else "—")
            # 最新期在前，历史年报在后
            return (f"| {label} | {_fv(v_lt,unit,decimals,is_pct)} | {yoy} | "
                    f"{_fv(v0,unit,decimals,is_pct)} | "
                    f"{_fv(v1,unit,decimals,is_pct)} | {_fv(v2,unit,decimals,is_pct)} |")
        else:
            yoy = (f"{(v0 - v1) / abs(v1) * 100:+.2f}%"
                   if v0 is not None and v1 not in (None, 0) else "—")
            return (f"| {label} | {_fv(v0,unit,decimals,is_pct)} | "
                    f"{_fv(v1,unit,decimals,is_pct)} | {_fv(v2,unit,decimals,is_pct)} | {yoy} |")

    if latest:
        lt_label = latest["label"]
        header = f"| 指标 | 最新期（{lt_label}） | YoY（{lt_label}同比） | {y0}A | {y1}A | {y2}A |"
        sep    = "|:-----|:----------------|:------------|:------|:------|:------|"
    else:
        header = f"| 指标 | {y0}A | {y1}A | {y2}A | YoY（{y0}vs{y1}） |"
        sep    = "|:-----|:------|:------|:------|:------------|"

    # 自动选择单位（三档）：< 100亿 → 百万元；100亿~1万亿 → 亿元；≥ 1万亿 → 百亿元
    all_rev = [fin.get(yr, {}).get("tRevenue") for yr in years]
    if latest:
        all_rev.append(fin.get("latest_data", {}).get("tRevenue"))
    max_rev = max((abs(v) for v in all_rev if v is not None), default=0)
    if 0 < max_rev < 1e10:
        rev_unit       = 1e6
        rev_unit_label = "百万元"
    elif max_rev < 1e12:
        rev_unit       = 1e8
        rev_unit_label = "亿元"
    else:
        rev_unit       = 1e10
        rev_unit_label = "百亿元"

    lines = [
        header, sep,
        row(f"营业总收入（{rev_unit_label}）", "tRevenue", unit=rev_unit),
        row(f"归母净利润（{rev_unit_label}）", "NPAttrP",  unit=rev_unit),
        row("毛利率（%）*", "grossMargin", is_pct=True, unit=1),
        row("净利率（%）", "netMargin", is_pct=True, unit=1),
        row("ROE-加权（%）", "ROEW", is_pct=True, unit=1),
        row(f"经营现金流净额（{rev_unit_label}）", "operCashFlow", unit=rev_unit),
        row(f"总资产（{rev_unit_label}）",          "totalAssets",  unit=rev_unit),
        row("资产负债率（%）", "liabRatio", is_pct=True, unit=1),
        row("基本EPS（元）", "basicEPS", unit=1, decimals=2),
    ]
    if q1_text:
        lines.append(f"\n*注：{q1_text}*")
    if "证券" in company_name or "期货" in company_name or "基金" in company_name:
        lines.append('\n*注：证券公司"毛利率"实为营业净收入/营业总收入，反映扣除直接成本后净收入比率，与制造业毛利率概念不同。*')
    return "\n".join(lines)


def gen_maincomp_fallback(key_data: dict) -> str:
    """当 getFdmtMoStdItem classifCD=2 无数据时（银行/保险/券商等金融股常见），
    从 mgmt_discussion 管理层讨论中提取营收构成数据生成替代表格。"""
    raw_data = key_data.get("_raw_data", {})
    mgmt = raw_data.get("mgmt_discussion", {})
    mgmt_items = mgmt.get("data", []) if isinstance(mgmt, dict) else []

    if not mgmt_items:
        return ""

    # 从 mgmt_discussion 中提取利息净收入/手续费净收入/其他非息等数据
    import re
    records = []
    for item in mgmt_items:
        desc = item.get("reviewDesc", "")
        end_date = item.get("endDate", "")
        if not desc or not end_date:
            continue
        # 提取: 利息净收入、手续费及佣金净收入、营业收入
        rec = {"endDate": end_date}
        for line in desc.replace("\\n", "").split("。"):
            line = line.strip()
            # 利息净收入
            m = re.search(r'利息净收入\s*([\d,]+\.?\d*)\s*亿', line)
            if m and "net_int" not in rec:
                rec["net_int"] = float(m.group(1).replace(",", ""))
            # 手续费及佣金净收入
            m = re.search(r'手续费及佣金净收入\s*([\d,]+\.?\d*)\s*亿', line)
            if m and "fee" not in rec:
                rec["fee"] = float(m.group(1).replace(",", ""))
            # 其他非利息收益
            m = re.search(r'其他非利息收益\s*([\d,]+\.?\d*)\s*亿', line)
            if m and "other_nonint" not in rec:
                rec["other_nonint"] = float(m.group(1).replace(",", ""))
            # 非利息收入（总额）
            m = re.search(r'非利息收入\s*([\d,]+\.?\d*)\s*亿', line)
            if m and "nonint_total" not in rec:
                rec["nonint_total"] = float(m.group(1).replace(",", ""))
            # 营业收入
            m = re.search(r'营业收入\s*([\d,]+\.?\d*)\s*亿', line)
            if m and "revenue" not in rec:
                rec["revenue"] = float(m.group(1).replace(",", ""))
            # 利息净收入 YoY
            m = re.search(r'利息净收入.*?([+-]?\d+\.?\d*)%', line)
            if m and "net_int_yoy" not in rec:
                rec["net_int_yoy"] = float(m.group(1))
            # 手续费 YoY
            m = re.search(r'手续费及佣金净收入.*?([+-]?\d+\.?\d*)%', line)
            if m and "fee_yoy" not in rec:
                rec["fee_yoy"] = float(m.group(1))

        if rec.get("revenue"):
            records.append(rec)

    if len(records) < 2:
        return ""

    # 取最近3期年报（按 endDate 降序，去重年份）
    seen_years = set()
    annual = []
    for r in sorted(records, key=lambda x: x.get("endDate", ""), reverse=True):
        y = r["endDate"][:4]
        if y not in seen_years and r["endDate"].endswith("-12-31"):
            seen_years.add(y)
            annual.append(r)
    if len(annual) < 2:
        annual = sorted(records, key=lambda x: x.get("endDate", ""), reverse=True)[:3]

    lines = []
    years = [r["endDate"][:4] for r in annual]
    # 表头
    header = f"| 业务板块 | {years[0]}A（亿） | YoY"
    sep = "|:-----|:------|:------|"
    for y in years[1:]:
        header += f" | {y}A（亿）"
        sep += "|:------|"
    header += " |"
    sep += "|"
    lines = [header, sep]

    for label, key, yoy_key in [
        ("利息净收入", "net_int", "net_int_yoy"),
        ("手续费及佣金净收入", "fee", "fee_yoy"),
        ("其他非利息收益", "other_nonint", None),
        ("**营业收入合计**", "revenue", None),
    ]:
        row = f"| {label}"
        v0 = annual[0].get(key)
        row += f" | {v0:.2f}" if v0 else " | —"
        if label == "**营业收入合计**":
            row += " | —"
        elif yoy_key and annual[0].get(yoy_key) is not None:
            row += f" | {annual[0][yoy_key]:+.1f}%"
        else:
            v0_val = annual[0].get(key, 0) or 0
            v1_val = annual[1].get(key, 0) if len(annual) > 1 else 0
            if v0_val and v1_val:
                row += f" | {(v0_val/v1_val-1)*100:+.1f}%"
            else:
                row += " | —"
        for r in annual[1:]:
            v = r.get(key)
            row += f" | {v:.2f}" if v else " | —"
        row += " |"
        lines.append(row)

    return "\n".join(lines)


def gen_maincomp_table(mc: dict) -> str:
    """生成业务拆分表格（含同比增速，有毛利率数据时增加毛利率列）"""
    years = mc["years"]
    segs = mc["segments"]
    margins = mc.get("margins", {})
    totals = mc["totals"]
    if not years or not segs:
        return ""

    y0, y1, y2 = (years + [None, None, None])[:3]
    has_margins = bool(margins)
    total_vals = (totals + [None, None, None])[:3]

    # 自动选择单位（三档，vals 已是亿元）：< 100亿 → 百万；100~1万亿 → 亿；≥ 1万亿 → 百亿
    max_tot = max((abs(v) for v in total_vals if v is not None), default=0)
    if 0 < max_tot < 100:
        mc_disp_unit  = 0.01   # v/0.01 = v*100 → 百万
        mc_unit_label = "百万"
    elif max_tot < 10000:
        mc_disp_unit  = 1
        mc_unit_label = "亿"
    else:
        mc_disp_unit  = 100    # v/100 → 百亿
        mc_unit_label = "百亿"

    # 表头：最新年收入 | 同比 | [毛利率] | 上一年收入 | [毛利率] | 前年收入 | [毛利率]
    if has_margins:
        header_parts = [f"| 业务板块 | {y0}收入（{mc_unit_label}） | 同比 | 毛利率"]
        sep_parts = ["|:---------|:---|:---|:---|"]
        if y1:
            header_parts[0] += f" | {y1}收入（{mc_unit_label}） | 毛利率"
            sep_parts[0] += ":---|:---|"
        if y2:
            header_parts[0] += f" | {y2}收入（{mc_unit_label}） | 毛利率"
            sep_parts[0] += ":---|:---|"
    else:
        header_parts = [f"| 业务板块 | {y0}收入（{mc_unit_label}） | 同比"]
        sep_parts = ["|:---------|:---|:---|"]
        if y1:
            header_parts[0] += f" | {y1}收入（{mc_unit_label}）"
            sep_parts[0] += ":---|"
        if y2:
            header_parts[0] += f" | {y2}收入（{mc_unit_label}）"
            sep_parts[0] += ":---|"
    header_parts[0] += " |"
    lines = [header_parts[0], sep_parts[0]]

    order = mc.get("order", list(segs.keys()))
    for seg_name in order:
        if seg_name not in segs:
            continue
        vals = segs[seg_name]
        v0 = vals[0] if len(vals) > 0 else None
        v1 = vals[1] if len(vals) > 1 else None
        v2 = vals[2] if len(vals) > 2 else None
        # 同比
        if v0 is not None and v1 is not None and v1 != 0:
            yoy = f"{(v0 - v1) / abs(v1) * 100:+.1f}%"
        else:
            yoy = "—"
        # v0/v1/v2 已是亿元单位，用 mc_disp_unit 转换为目标单位
        cells = f" | {_fmt(v0, unit=mc_disp_unit) if v0 is not None else '—'} | {yoy}"
        if has_margins:
            mg_vals = margins.get(seg_name, [])
            cells += f" | {_pct(mg_vals[0]) if len(mg_vals) > 0 and mg_vals[0] is not None else '—'}"
        if y1:
            cells += f" | {_fmt(v1, unit=mc_disp_unit) if v1 is not None else '—'}"
            if has_margins:
                mg_vals = margins.get(seg_name, [])
                cells += f" | {_pct(mg_vals[1]) if len(mg_vals) > 1 and mg_vals[1] is not None else '—'}"
        if y2:
            cells += f" | {_fmt(v2, unit=mc_disp_unit) if v2 is not None else '—'}"
            if has_margins:
                mg_vals = margins.get(seg_name, [])
                cells += f" | {_pct(mg_vals[2]) if len(mg_vals) > 2 and mg_vals[2] is not None else '—'}"
        lines.append(f"| {seg_name}{cells} |")

    # 合计行
    t0, t1, t2 = total_vals
    if t0 is not None and t1 is not None and t1 != 0:
        t_yoy = f"**{(t0 - t1) / abs(t1) * 100:+.1f}%**"
    else:
        t_yoy = "—"
    total_cells = f" | **{_fmt(t0, unit=mc_disp_unit) if t0 else '—'}** | {t_yoy}"
    if has_margins:
        total_cells += " | —"
    if y1:
        total_cells += f" | **{_fmt(t1, unit=mc_disp_unit) if t1 else '—'}**"
        if has_margins:
            total_cells += " | —"
    if y2:
        total_cells += f" | **{_fmt(t2, unit=mc_disp_unit) if t2 else '—'}**"
        if has_margins:
            total_cells += " | —"
    lines.append(f"| **合计（主营口径）**{total_cells} |")

    return "\n".join(lines)


def gen_consensus_table(forecasts: list, actual: dict, fin: dict) -> str:
    """生成 9.1 市场一致预期表格"""
    if not forecasts:
        return "_（一致预期数据暂缺）_"

    years_fin = fin.get("years", [])
    actual_yr = years_fin[0] if years_fin else ""
    actual_fin = fin.get(actual_yr, {}) if actual_yr else {}

    rows = []
    headers = [f"{actual_yr}A（实际）"] if actual_yr else []
    rev_row = [_fmt(actual_fin.get("tRevenue"), unit=1e8)] if actual_yr else []
    rev_yoy_row = [_pct(actual_fin.get("revenueYOY"), decimals=2)] if actual_yr else []
    profit_row = [_fmt(actual_fin.get("NPAttrP"), unit=1e8)] if actual_yr else []
    profit_yoy_row = [_pct(actual_fin.get("NPAttrPYOY"), decimals=2)] if actual_yr else []
    eps_row = [str(actual_fin.get("EPS", "—"))] if actual_yr else []

    current_pe_actual = actual.get("conPe", "—")
    try:
        pe_row = [str(round(float(current_pe_actual), 2)) + "x（TTM）"] if actual_yr else []
    except (TypeError, ValueError):
        pe_row = [str(current_pe_actual)] if actual_yr else []

    for fc_idx, fc in enumerate(forecasts[:3]):
        inc = fc.get("conIncome")
        prof = fc.get("conProfit")
        eps = fc.get("conEps")
        inc_yoy = fc.get("conIncomeYoy")
        prof_yoy = fc.get("conProfitYoy")
        pe = fc.get("conPe")
        est_yr = f"{int(actual_yr) + fc_idx + 1}E" if actual_yr else f"?{fc_idx+1}E"
        headers.append(est_yr)
        rev_row.append(_fmt(inc, unit=1e4) if inc else "—")   # conIncome in 万元
        rev_yoy_row.append(_pct(inc_yoy) if inc_yoy else "—")
        profit_row.append(_fmt(prof, unit=1e4) if prof else "—")  # conProfit in 万元
        profit_yoy_row.append(_pct(prof_yoy) if prof_yoy else "—")
        try:
            eps_row.append(str(round(float(eps), 2)) if eps else "—")
        except (TypeError, ValueError):
            eps_row.append(str(eps) if eps else "—")
        try:
            pe_row.append(f"{round(float(pe), 1)}x" if pe else "—")
        except (TypeError, ValueError):
            pe_row.append(str(pe) if pe else "—")

    col_sep = " | ".join([""] + headers + [""])
    lines = [
        f"| 预测指标 | {' | '.join(headers)} |",
        "|:---------|" + ":--------|" * len(headers),
        "| 营业收入一致预期（亿元） | " + " | ".join(rev_row) + " |",
        "| YoY增速（%） | " + " | ".join(rev_yoy_row) + " |",
        "| 归母净利润一致预期（亿元） | " + " | ".join(profit_row) + " |",
        "| YoY增速（%） | " + " | ".join(profit_yoy_row) + " |",
        "| EPS一致预期（元） | " + " | ".join(eps_row) + " |",
        "| 对应PE（当前价） | " + " | ".join(pe_row) + " |",
    ]
    return "\n".join(lines)


def gen_forecast_table(orgs: list) -> str:
    """生成 9.2 各机构盈利预测表格"""
    if not orgs:
        return ""

    # 收集预测年份
    all_years = sorted(set(fy for org in orgs for fy in org["data"].keys()))
    yr_headers = " | ".join(f"{y}E" for y in all_years)

    def org_rows(metric_key, unit=1e3, decimals=2):
        rows = []
        for i, org in enumerate(orgs):
            cells = []
            for fy in all_years:
                v = org["data"].get(fy, {}).get(metric_key)
                cells.append(_fmt(v, unit, decimals) if v else "—")
            first_col = org["name"] if i == 0 else ""
            rows.append(f"|  | {org['name']} | {' | '.join(cells)} |")
        return rows

    lines = [
        f"| 指标 | 机构 | {yr_headers} |",
        "|:-----|:-----|" + ":---------|" * len(all_years),
    ]
    # 营业收入
    lines.append(f"| 营业收入（亿元） | {orgs[0]['name']} | " +
                 " | ".join(_fmt(orgs[0]["data"].get(fy, {}).get("income"), unit=1e4) for fy in all_years) + " |")
    for org in orgs[1:]:
        lines.append(f"|  | {org['name']} | " +
                     " | ".join(_fmt(org["data"].get(fy, {}).get("income"), unit=1e4) for fy in all_years) + " |")
    # 归母净利润
    lines.append(f"| 归母净利润（亿元） | {orgs[0]['name']} | " +
                 " | ".join(_fmt(orgs[0]["data"].get(fy, {}).get("profit"), unit=1e4) for fy in all_years) + " |")
    for org in orgs[1:]:
        lines.append(f"|  | {org['name']} | " +
                     " | ".join(_fmt(org["data"].get(fy, {}).get("profit"), unit=1e4) for fy in all_years) + " |")
    # EPS
    lines.append(f"| EPS（元/股） | {orgs[0]['name']} | " +
                 " | ".join(str(orgs[0]["data"].get(fy, {}).get("eps") or "—") for fy in all_years) + " |")
    for org in orgs[1:]:
        lines.append(f"|  | {org['name']} | " +
                     " | ".join(str(org["data"].get(fy, {}).get("eps") or "—") for fy in all_years) + " |")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 叙述性章节生成（调用 Claude API）
# ─────────────────────────────────────────────────────────────────────────────

def _compact_reports(reports: list, n: int = 5) -> str:
    """将研报列表压缩为 prompt 友好的格式，保留足够的正文供深度分析"""
    parts = []
    for r in reports[:n]:
        src = f"[{r['id']}]机构:{r['org']} 日期:{r['date']} 评级:{r['rating']} 目标价:{r['target']}"
        parts.append(f"{src}\n摘要:{r['abstract'][:1500]}\n正文:\n{r['text'][:3500]}")
    return "\n\n---\n\n".join(parts)


def _compact_meetings(meetings: list, n: int = 3, include_text: bool = False) -> str:
    """将会议纪要压缩为 prompt 友好的格式

    include_text=True 时追加 aiOriBody 完整正文，用于核心分析章节（第2/4节）。
    """
    if not meetings:
        return "（无会议纪要数据）"
    parts = []
    for m in meetings[:n]:
        header = f"【{m['date']} {m['type']} {m['title']}】"
        body = ""
        if m["overview"]:
            body += f"AI概述：\n{m['overview']}\n"
        if m["qa"]:
            body += f"\nQ&A摘要：\n{m['qa']}\n"
        if include_text and m.get("text"):
            body += f"\n原文节录：\n{m['text']}"
        parts.append(f"{header}\n{body.strip()}")
    return "\n\n".join(parts)


def _compact_mgmt(data: dict, max_len: int = 3000) -> str:
    """提取管理层讨论（MD&A）关键内容"""
    md = data.get("mgmt_discussion")
    if not md:
        return ""
    # md 可能是 dict 或 list，尝试提取文本
    if isinstance(md, dict):
        d = md.get("data", md)
        if isinstance(d, list) and d:
            d = d[0]
        if isinstance(d, dict):
            text = d.get("content") or d.get("text") or d.get("summary") or str(d)
        else:
            text = str(d)
    elif isinstance(md, list) and md:
        item = md[0]
        text = item.get("content") or item.get("text") or str(item) if isinstance(item, dict) else str(item)
    else:
        text = str(md)
    return str(text)[:max_len]


def _meeting_refs_str(meetings: list, ref_map: dict, n: int = 3) -> str:
    """构建会议纪要引用序号字符串（Python 3.7 兼容）"""
    parts = []
    for m in meetings[:n]:
        key = "meeting_" + m["date"] + "_" + m["title"][:20]
        n_val = str(ref_map.get(key, {}).get("n", ""))
        if n_val:
            parts.append(m["title"][:30] + "=[" + n_val + "]")
    return ", ".join(parts) if parts else ""


def _refs_str(reports: list, ref_map: dict, n: int = 5) -> str:
    """构建报告引用映射字符串（Python 3.7 兼容，避免f-string内反斜杠）
    返回如: report_123=[1], report_456=[2], ...
    """
    parts = []
    for r in reports[:n]:
        rid = str(r.get('id', ''))
        key = "report_" + rid
        n_val = str(ref_map.get(key, {}).get('n', ''))
        parts.append("report_" + rid + "=[" + n_val + "]")
    return ', '.join(parts)


def _refs_labels(reports: list, ref_map: dict, n: int = 4) -> str:
    """构建角标引用串（Python 3.7 兼容）
    返回如: [1] [2] [3]
    """
    result = []
    for r in reports[:n]:
        rid = str(r.get('id', ''))
        key = "report_" + rid
        n_val = str(ref_map.get(key, {}).get('n', ''))
        if n_val:
            result.append("[" + n_val + "]")
    return " ".join(result)


def _compact_fin(fin: dict) -> str:
    """财务数据压缩摘要"""
    years = fin["years"]
    lines = []
    for yr in years:
        d = fin.get(yr, {})
        rev = _fmt(d.get("tRevenue"))
        rev_yoy = _pct(d.get("revenueYOY"))
        np_ = _fmt(d.get("NPAttrP"))
        np_yoy = _pct(d.get("NPAttrPYOY"))
        roe = _pct(d.get("ROEW"), 2)
        lines.append(f"{yr}A: 营收{rev}亿({rev_yoy}), 归母净利{np_}亿({np_yoy}), ROE加权{roe}")
    return "\n".join(lines)


_S123_SEP = "<<<SECTION_BREAK>>>"


def gen_sections_1_2_3(client, key_data: dict) -> dict:
    """合并生成第1、2、3节（单次 LLM 调用），利用模型在同一生成流中的上下文感知避免跨节重复。
    返回 {"s1": ..., "s2": ..., "s3": ...}
    """
    reports      = key_data["reports"]
    fin          = key_data["fin"]
    name         = key_data["name"]
    ref_map      = key_data["ref_map"]
    valuation    = key_data["valuation"]
    meetings     = key_data.get("meetings", [])
    forecasts    = key_data.get("consensus_forecasts", [])
    mc           = key_data["mc"]
    announcements= key_data.get("announcements", [])
    raw_data     = key_data.get("_raw_data", {})
    mgmt_text    = _compact_mgmt(raw_data)

    pe_val = valuation["items"].get("市盈率PE", {})
    pb_val = valuation["items"].get("市净率PB", {})
    pe = pe_val.get("val", "—")
    pb = pb_val.get("val", "—")

    years_fin = fin.get("years", [])
    base_yr = int(years_fin[0]) if years_fin else 2025
    con_lines = []
    for i, fc in enumerate(forecasts[:3]):
        yr = base_yr + i + 1
        inc = fc.get("conIncome")
        prf = fc.get("conProfit")
        con_lines.append(f"{yr}E营收{inc/1e4:.0f}亿/净利{prf/1e4:.0f}亿" if inc and prf else f"{yr}E—")
    con_summary = "、".join(con_lines) if con_lines else "（暂无一致预期数据）"

    segs_pct = ", ".join([
        f"{seg}:{mc['segments'][seg][0]/mc['totals'][0]*100:.1f}%"
        if mc['totals'] and mc['totals'][0] and mc['segments'][seg] and mc['segments'][seg][0] is not None
        else seg
        for seg in mc['segments']
    ])

    prompt = f"""你是顶级券商分析师，为 {name} 撰写公司一页纸报告的**前三节**。

请**按顺序依次输出**第1节、第2节、第3节，节与节之间用以下分隔符单独占一行隔开：
{_S123_SEP}

---

【共享数据】

【财务数据（近3年）】
{_compact_fin(fin)}

【主营构成（最新年占比）】
{segs_pct}

【近期研报（5篇，含完整分析）】
{_compact_reports(reports, n=5)}

【近期会议纪要（含完整正文）】
{_compact_meetings(meetings, n=3, include_text=True)}

{"【管理层讨论（MD&A）】" + chr(10) + mgmt_text if mgmt_text else ""}

【公告列表】
{json.dumps(announcements[:5], ensure_ascii=False)[:500] if announcements else "（无）"}

【市场一致预期】
{con_summary}

【引用映射（正文中用[N]标注）】
研报：{_refs_labels(reports, ref_map, n=5)}
会议纪要：{_meeting_refs_str(meetings, ref_map, n=3)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], consensus=[{ref_map.get('consensus',{}).get('n','')}], valuation_rank=[{ref_map.get('valuation_rank',{}).get('n','')}], maincomp=[{ref_map.get('maincomp',{}).get('n','')}]{"," + "mgmt=[" + str(ref_map.get('mgmt_discussion',{}).get('n','')) + "]" if mgmt_text else ""}

---

## 第1节：公司近况跟踪

**字数上限：220字**

- 2-3个 • 要点，每条单独一行，每点仅1句话
- **优先提炼里程碑/突破性数字**（首次突破某门槛、历史新高、行业第一、同比大幅超预期等）；普通同比数据不单独成点
- 只陈述事实+数字，**不展开任何分析或判断**（分析留给第2节）
- 第二段（独立行，1句）：主流机构评级方向、目标价区间、当前PE约{pe}x/PB约{pb}x
- 最后一段（独立行，1句）：市场一致预期{base_yr+1}-{base_yr+2}年营收/净利润关键数字，标注[N]
- 所有数字标注[N]，不介绍商业模式，不重复历史背景

{_S123_SEP}

## 第2节：核心投资逻辑

**总字数700字以内（2.1+2.2合计）**

⚠️ 你刚刚写完第1节，其中已提及的具体事件名称和数字——第2节**不重复陈述这些事件**，直接分析其背后的驱动机制和投资空间。

### 2.1 短期逻辑（3-12个月催化剂）
- **[催化剂1标题]**：[含精确数据和逻辑链，标注引用，聚焦核心]
- **[催化剂2标题]**：...（共3个要点）

### 2.2 长期逻辑（核心竞争力）
1） **[核心壁垒]**：[含市占率/规模量化数据，标注引用]
2） **[成长驱动力]**：[标注引用]
3） **[商业模式优势]**：[ROE/可持续性，标注引用]

{_S123_SEP}

## 第3节：催化事件时间表

⚠️ 前两节已对这些事件作了充分描述和分析——第3节只做时间线格式记录，**不重复分析语言**，影响栏只填关键数字。

输出纯 Markdown 表格，不要其他说明文字：

| 时间 | 事件 | 影响 |
|:-----|:-----|:-----|
| YYYY-MM 或 YYYY-MM-DD 或 YYYY-Q? | [具体事件+规模] | [关键数字] |

规则：
- 已发生事件2-3条，未来预期事件3-4条（加"（预期）"标注）
- 严禁列入券商发布研究报告、机构盈利预测、目标价更新等分析师行为
- 严禁在表格单元格中出现任何引用标注（如[N]）

---
请直接开始输出第1节内容（不要重复上面的章节标题）："""

    raw = call_claude(client, prompt, max_tokens=3800)
    parts = [p.strip() for p in raw.split(_S123_SEP)]
    return {
        "s1": parts[0] if len(parts) > 0 else raw,
        "s2": parts[1] if len(parts) > 1 else "[生成失败: 未找到分隔符]",
        "s3": parts[2] if len(parts) > 2 else "[生成失败: 未找到分隔符]",
    }


def gen_section1(client, key_data: dict) -> str:
    """1 公司近况跟踪 (300-400字)"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    valuation = key_data["valuation"]
    meetings = key_data.get("meetings", [])
    forecasts = key_data.get("consensus_forecasts", [])

    pe_val = valuation["items"].get("市盈率PE", {})
    pb_val = valuation["items"].get("市净率PB", {})
    pe = pe_val.get("val", "—")
    pb = pb_val.get("val", "—")

    report_refs = _refs_labels(reports, ref_map, n=4)

    meetings_text = _compact_meetings(meetings, n=2)

    # 构造简短的一致预期摘要（营收/净利润三年预测）
    years_fin = fin.get("years", [])
    base_yr = int(years_fin[0]) if years_fin else 2025
    con_lines = []
    for i, fc in enumerate(forecasts[:3]):
        yr = base_yr + i + 1
        inc = fc.get("conIncome")
        prf = fc.get("conProfit")
        inc_str = f"{inc/1e4:.0f}亿" if inc else "—"
        prf_str = f"{prf/1e4:.0f}亿" if prf else "—"
        con_lines.append(f"{yr}E营收{inc_str}/净利{prf_str}")
    con_summary = "、".join(con_lines) if con_lines else "（暂无一致预期数据）"

    prompt = f"""为 {name} 撰写"公司近况跟踪"章节（第1节），严格控制在220字以内。

【财务数据】
{_compact_fin(fin)}

【近期研报摘要（最新3篇）】
{_compact_reports(reports, n=3)}

【近期会议纪要（路演/业绩说明会）】
{meetings_text}

【市场一致预期（三年简览）】
{con_summary}

【引用映射（正文中用[N]标注，N取括号内数字）】
研报引用序号：{report_refs}
会议纪要引用：{_meeting_refs_str(meetings, ref_map, n=2)}
结构化数据引用：fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], valuation_rank=[{ref_map.get('valuation_rank',{}).get('n','')}], consensus=[{ref_map.get('consensus',{}).get('n','')}]

【格式要求】220字严格上限，超出必须压缩。
- 2-3个 • 要点：**每个要点必须单独占一行**（• 开头，换行分隔），每点仅1句话，**优先提炼里程碑/突破性数字**（首次突破某门槛、历史新高、行业第一、同比大幅超预期等），这类数字比普通增速更有冲击力；普通同比数据不单独成点
- 要点聚焦近1-3个月核心事件+1个最具代表性数字，**不展开分析**（分析在第2节）
- 第二段（独立行，1句）：主流机构评级方向、目标价区间、当前PE约{pe}x/PB约{pb}x
- 最后一段（独立行，1句）：一致预期未来两年营收/净利润关键数字，格式"市场一致预期{base_yr+1}-{base_yr+2}年营收/净利润分别为…"，标注[N]
- 不要重复历史背景，不要介绍商业模式，所有数字标注[N]
"""
    return call_claude(client, prompt, max_tokens=900)


def gen_section2(client, key_data: dict) -> str:
    """2 核心投资逻辑"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    mc = key_data["mc"]
    meetings = key_data.get("meetings", [])
    raw_data = key_data.get("_raw_data", {})
    mgmt_text = _compact_mgmt(raw_data)

    prompt = f"""为 {name} 撰写"核心投资逻辑"章节（第2节），包含2.1短期逻辑和2.2长期逻辑。
这是报告中分析深度要求最高的章节，要求有独立判断、量化支撑、可验证的逻辑链条。

【财务数据（近3年）】
{_compact_fin(fin)}

【主营构成（业务结构）】
板块: {list(mc['segments'].keys())}
年份: {mc['years']}
占比（最新年）: """ + ", ".join([
        f"{seg}:{mc['segments'][seg][0]/mc['totals'][0]*100:.1f}%" if mc['totals'] and mc['totals'][0] and mc['segments'][seg][0] is not None else f"{seg}"
        for seg in mc['segments']
    ]) + f"""

【近期研报全文（5篇，含完整分析）】
{_compact_reports(reports, n=5)}

【近期会议纪要（管理层路演/业绩发布会，含完整正文）】
{_compact_meetings(meetings, n=3, include_text=True)}

{"【管理层讨论（MD&A）】" + chr(10) + mgmt_text if mgmt_text else ""}

【引用映射】
{_refs_str(reports, ref_map, 5)}
{_meeting_refs_str(meetings, ref_map, n=3)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], maincomp=[{ref_map.get('maincomp',{}).get('n','')}]
{"mgmt=[" + str(ref_map.get('mgmt_discussion',{}).get('n','')) + "]" if mgmt_text else ""}

【格式要求】**总字数700字以内**（2.1+2.2合计）。
### 2.1 短期逻辑（3-12个月催化剂）
- **[催化剂1标题]**：[含精确数据和逻辑链，标注引用，聚焦核心]
- **[催化剂2标题]**：...（共3个要点）

### 2.2 长期逻辑（核心竞争力）
1） **[核心壁垒]**：[含市占率/规模量化数据，标注引用，聚焦核心]
2） **[成长驱动力]**：[标注引用]
3） **[商业模式优势]**：[ROE/可持续性，标注引用]

*注：短期逻辑侧重可验证的近期催化剂，长期逻辑侧重可持续竞争优势。本节所有数据须标注引用。*
"""
    return call_claude(client, prompt, max_tokens=1800)


def gen_section3(client, key_data: dict) -> str:
    """3 催化事件时间表"""
    reports = key_data["reports"]
    announcements = key_data.get("announcements", [])
    name = key_data["name"]
    ref_map = key_data["ref_map"]

    prompt = f"""为 {name} 生成"催化事件时间表"，格式为 Markdown 表格。

【研报摘要（提取事件和时间节点）】
{_compact_reports(reports[:5])}

【公告列表】
{json.dumps(announcements[:5], ensure_ascii=False)[:500] if announcements else "（无）"}

【格式要求】
输出纯 Markdown 表格，不要其他说明文字：

| 时间 | 事件 | 影响 |
|:-----|:-----|:-----|
| YYYY-MM-DD 或 YYYY-MM 或 YYYY-Q? | [具体事件，含金额/规模] | [具体影响，含数据] |

规则：
- 时间列：精确到日写YYYY-MM-DD，不确定日写YYYY-MM，不确定月写季度如2026-Q2
- 已发生事件2-3条，未来预期事件3-4条（加"（预期）"标注）
- 严禁列入"券商发布研究报告"类内容
- **严禁**将机构盈利预测、EPS预测调整、目标价更新等分析师预测类内容列入表格；仅列入公司层面真实发生或预期发生的经营/政策/市场事件
- 影响栏给出具体数据支撑，不得泛泛而谈
- **严禁**在表格单元格中出现任何引用标注（如 [N]、[数字]、[报告ID] 等），表格内容只包含事实描述
"""
    return call_claude(client, prompt, max_tokens=800)


def gen_section4_intro(client, key_data: dict) -> str:
    """4.1 业务概况与边际变化（盈利模式 + 边际变化，各1段，共约200字）"""
    fin = key_data["fin"]
    mc = key_data["mc"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    years = mc["years"]
    meetings = key_data.get("meetings", [])
    raw_data = key_data.get("_raw_data", {})
    mgmt_text = _compact_mgmt(raw_data, max_len=1500)

    segs_summary = ", ".join([
        f"{seg}:{_fmt(mc['segments'][seg][0])}亿" for seg in mc["segments"]
        if mc["segments"][seg] and mc["segments"][seg][0]
    ]) if mc["segments"] else "无数据"

    prompt = f"""为 {name} 撰写"4.1 业务概况与边际变化"的两段内容。

【主营构成（最新年 {years[0] if years else '?'}）】
{segs_summary}

【财务概览】
{_compact_fin(fin)}

{"【管理层讨论】" + chr(10) + mgmt_text if mgmt_text else ""}

【引用映射】
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], maincomp=[{ref_map.get('maincomp',{}).get('n','')}]
{"mgmt=[" + str(ref_map.get('mgmt_discussion',{}).get('n','')) + "]" if mgmt_text else ""}

【格式要求】**两段合计200字以内**。直接输出以下两段，不要输出章节标题：

**盈利模式**：[1段约100字，说明收入来源结构和主要盈利路径，含各板块收入占比，标注引用]

**业务边际变化**：[1段约100字，当前最重要的1-2个业务变化，含量化同比数据，标注引用]
"""
    return call_claude(client, prompt, max_tokens=500)


def gen_section4_deep(client, key_data: dict) -> str:
    """4.3 业务深度分析 + 4.4 核心竞争力（合计约400字）"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    mc = key_data["mc"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    years = mc["years"]
    meetings = key_data.get("meetings", [])
    raw_data = key_data.get("_raw_data", {})
    mgmt_text = _compact_mgmt(raw_data)

    segs_summary = "\n".join([
        f"- {seg}: " + ", ".join([
            f"{yr}={_fmt(mc['segments'][seg][i])}亿" for i, yr in enumerate(years[:3]) if i < len(mc['segments'][seg])
        ])
        for seg in mc["segments"]
    ])

    prompt = f"""为 {name} 撰写第4节的两个深度子节。

【主营构成数据（精确，亿元）】
年份: {years}
{segs_summary}

【财务概览】
{_compact_fin(fin)}

【研报分析（含各板块深度分析）】
{_compact_reports(reports, n=4)}

【会议纪要（管理层表述）】
{_compact_meetings(meetings, n=2, include_text=True)}

{"【管理层讨论（MD&A）】" + chr(10) + mgmt_text if mgmt_text else ""}

【引用映射】
{_refs_str(reports, ref_map, 4)}
{_meeting_refs_str(meetings, ref_map, n=2)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], maincomp=[{ref_map.get('maincomp',{}).get('n','')}]

【格式要求】**两个子节合计400字以内**（精炼专业）。直接输出以下两节：

### 4.3 业务深度分析

[对主要板块（2-3个核心板块）逐一分析，含精确数据支撑，每个板块2-3句，标注引用]

### 4.4 核心竞争力与竞争优势

1） **[优势1标题]**：[2句量化支撑，标注引用]
2） **[优势2标题]**：[2句量化支撑，标注引用]
3） **[优势3标题]**：[2句量化支撑，标注引用]
"""
    return call_claude(client, prompt, max_tokens=1000)


def gen_section4_profit_model(client, key_data: dict) -> str:
    """4.1 盈利方式：结合公司实际经营情况说明盈利路径（120-160字）"""
    mc = key_data["mc"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    raw_data = key_data.get("_raw_data", {})
    company_info = key_data.get("company_info", {})
    reports = key_data["reports"]
    mgmt_text = _compact_mgmt(raw_data, max_len=800)
    segs = list(mc["segments"].keys())

    # 提取最新年主营数据用于支撑描述
    years = mc.get("years", [])
    y0 = years[0] if years else ""
    segs_data = "\n".join([
        f"- {seg}: {_fmt(mc['segments'][seg][0])}亿（{y0}）"
        for seg in mc["segments"] if mc["segments"][seg]
    ]) if y0 else ""

    prompt = f"""为 {name} 撰写"盈利方式"（第4.1节），**120-160字以内**。

【主营业务板块及规模（{y0}年）】
{segs_data or segs}

【财务概览】
{_compact_fin(fin)}

【公司基础信息】
{json.dumps(company_info, ensure_ascii=False)[:400] if company_info else "（无）"}

{"【管理层讨论（摘要）】" + chr(10) + mgmt_text if mgmt_text else ""}

【研报摘要（理解商业模式）】
{_compact_reports(reports, n=2)}

【引用映射】
maincomp=[{ref_map.get('maincomp',{}).get('n','')}], fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】
- 用2-3个 bullet（• 开头），每条格式：**[盈利维度]**：[结合本公司实际经营情况说明如何通过这个维度赚钱]
- 盈利维度要**同时结合行业特点和本公司实际经营特色**，体现差异化（示例维度仅供参考，根据公司实际调整）：
  - 金融/券商："靠通道与交易量赚钱"/"靠资管规模赚钱"/"靠资本杠杆和自营赚钱"
  - 消费品："靠品牌溢价赚钱"/"靠渠道铺设赚钱"/"靠产品升级赚钱"
  - 科技："靠技术壁垒赚钱"/"靠平台生态赚钱"/"靠规模效应赚钱"
  - 周期品："靠低成本产能赚钱"/"靠价差赚钱"
- 每条描述建议结构：**先说行业通用盈利逻辑**（该业务为何能赚钱）→ **再用本公司具体数字说明竞争位置**（规模/市占率/客户数等），两层逻辑缺一不可
- **可以引用关键规模数字或市场地位数字**支撑描述（如市占率、资产规模、客户数等），但不要堆砌收入/利润明细（明细在下方表格）
- 语言简练，说清楚本质，标注引用
"""
    return call_claude(client, prompt, max_tokens=400)


def gen_section4_survey_qa(client, key_data: dict) -> str:
    """4.5 机构调研核心问答

    数据优先级：
    1. surveys（institution_research_detail.content）— 官方机构调研接口，内容最完整
    2. meetings（getMeetingSummaryDetail.aiQa）— 会议纪要 AI 摘要，作为补充
    任意来源有数据即可生成本节；两者均无数据则跳过。
    """
    surveys  = key_data.get("surveys", [])   # institution_research_detail
    meetings = key_data.get("meetings", [])
    name     = key_data["name"]
    ref_map  = key_data["ref_map"]

    qa_blocks = []

    # 优先：机构调研接口 detail_content
    for sv in surveys[:5]:
        if not sv.get("content"):
            continue
        qa_blocks.append(
            f"【{sv['date']} {sv['type']}（机构调研）】\n{sv['content'][:4000]}"
        )

    # 补充：会议纪要 aiQa（若调研接口内容不足时）
    if len(qa_blocks) < 3:
        for m in meetings[:5]:
            if not m.get("qa"):
                continue
            ref_key = "meeting_" + m["date"] + "_" + m["title"][:20]
            ref_n   = ref_map.get(ref_key, {}).get("n", "")
            ref_tag = f"[{ref_n}]" if ref_n else ""
            qa_blocks.append(
                f"【{m['date']} {m['type']} {m['title']}】{ref_tag}\n{m['qa'][:3000]}"
            )

    if not qa_blocks:
        return ""   # 无任何 Q&A 数据则跳过本节

    qa_text = "\n\n".join(qa_blocks)
    meeting_refs = _meeting_refs_str(meetings, ref_map, n=5)

    prompt = f"""你是顶级券商分析师，正在为{name}撰写公司一页纸报告的"机构调研核心问答"小节。

以下是最近机构调研/业绩说明会/路演的原始内容（来源：机构调研接口 + 会议纪要）：
{qa_text}

---
任务：从以上内容中精选 3-5 个最有基本面价值的问答，聚焦以下类型：
• 盈利能力变化原因（毛利率/净利率涨跌驱动）
• 新产品/新业务落地进展（含具体数据节点）
• 主要风险点（商誉减值、客户集中、竞争加剧等，需有数据支撑）
• 资本开支/产能/现金流展望

输出格式要求：
- 每条用 **Q：** / **A：** 标注，A 中须保留关键数字和时间节点
- 每条末尾标注引用 [N]（若有对应引用编号）
- 不引入原文中没有的信息
- 不含券商/机构具体名称
- 总字数 400 字以内

引用映射（供标注用）：{meeting_refs}
"""
    return call_claude(client, prompt, max_tokens=700)


def _fallback_qa_from_raw(qa_blocks: list) -> str:
    """从原始调研/会议纪要数据中提取 Q&A 对，兜底生成 4.5 节。

    输入 qa_blocks 格式：每条为 【日期 类型（机构调研）】\\n原始内容 或
    【日期 类型 标题】\\n原始内容。
    输出：**Q：** / **A：** 格式的 Markdown 文本，最多 5 组问答。
    """
    import re as _re

    qa_pairs = []
    for blk in qa_blocks[:4]:
        text = _re.sub(r'^【.*?】\n?', '', blk, flags=_re.MULTILINE)
        # 按 QA: / Q1: / question: 等模式拆出问答对
        segments = _re.split(
            r'(?:(?:^|\n)\s*(?:QA\s*[环节]?\s*[:：]|Q\d*\s*[:：]\s*|question\s*\d*\s*[:：]\s*))',
            text, flags=_re.IGNORECASE
        )
        for seg in segments:
            seg = seg.strip()
            if not seg or len(seg) < 20:
                continue
            # 拆分 Q 和 A
            a_match = _re.split(
                r'(?:(?:^|\n)\s*A\d*\s*[:：]\s*)',
                seg, maxsplit=1, flags=_re.IGNORECASE
            )
            if len(a_match) >= 2 and a_match[0].strip() and a_match[1].strip():
                q_text = a_match[0].strip()
                a_text = a_match[1].strip()
                # 截断过长的回答
                if len(a_text) > 400:
                    # 在句号处截断
                    cut = a_text[:400].rfind('。')
                    a_text = a_text[:cut + 1] if cut > 200 else a_text[:400] + '…'
                qa_pairs.append(f"**Q：** {q_text}\n**A：** {a_text}")
                if len(qa_pairs) >= 5:
                    break
        if len(qa_pairs) >= 5:
            break

    if not qa_pairs:
        # 完全无法解析时，取第一条原始内容的前 600 字
        first = _re.sub(r'^【.*?】\n?', '', qa_blocks[0], flags=_re.MULTILINE) if qa_blocks else ""
        if first.strip():
            qa_pairs.append(first.strip()[:600])

    return "\n\n".join(qa_pairs) if qa_pairs else ""


def gen_section4(client, key_data: dict) -> dict:
    """4.1盈利方式 + 4.5机构调研核心问答 — 合并一次LLM调用，避免章内内容重复"""
    mc = key_data["mc"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    raw_data = key_data.get("_raw_data", {})
    company_info = key_data.get("company_info", {})
    reports = key_data["reports"]
    surveys = key_data.get("surveys", [])
    meetings = key_data.get("meetings", [])
    maincomp_ctx = key_data.get("maincomp_table_ctx", "")

    mgmt_text = _compact_mgmt(raw_data, max_len=800)
    years = mc.get("years", [])
    y0 = years[0] if years else ""
    segs_data = "\n".join([
        f"- {seg}: {_fmt(mc['segments'][seg][0])}亿（{y0}）"
        for seg in mc["segments"] if mc["segments"][seg]
    ]) if y0 else str(list(mc["segments"].keys()))

    # 4.5 Q&A 原始数据
    qa_blocks = []
    for sv in surveys[:5]:
        if sv.get("content"):
            qa_blocks.append(f"【{sv['date']} {sv['type']}（机构调研）】\n{sv['content'][:3000]}")
    if len(qa_blocks) < 3:
        for m in meetings[:5]:
            if m.get("qa"):
                ref_key = "meeting_" + m["date"] + "_" + m["title"][:20]
                ref_n = ref_map.get(ref_key, {}).get("n", "")
                ref_tag = f"[{ref_n}]" if ref_n else ""
                qa_blocks.append(f"【{m['date']} {m['type']} {m['title']}】{ref_tag}\n{m['qa'][:2500]}")
    has_qa = bool(qa_blocks)
    qa_text = "\n\n".join(qa_blocks) if qa_blocks else ""
    meeting_refs = _meeting_refs_str(meetings, ref_map, n=5)

    maincomp_section = (
        f"\n【4.2分板块业务数据表格（已生成，4.1不要重复表中数字明细，可用'如下表'指向）】\n{maincomp_ctx}\n"
        if maincomp_ctx else ""
    )
    qa_data_section = (
        f"\n【机构调研/会议纪要原始内容（用于生成4.5）】\n{qa_text}\n引用映射（4.5用）：{meeting_refs}\n"
        if has_qa else ""
    )
    qa_instruction = (
        "### 4.5 机构调研核心问答\n"
        "精选3-5个最有基本面价值的问答（来自以上机构调研/会议纪要）；\n"
        "每条用 **Q：** / **A：** 格式，A须保留关键数字和时间节点；\n"
        "聚焦：盈利能力变化原因、新产品/业务进展、主要风险、资本开支/现金流展望；\n"
        "不引入原始内容中没有的信息，不含机构具体名称；末尾标注引用[N]；总字数400字以内；\n"
        "**与4.1已描述的商业模式不重复**。"
        if has_qa else
        "### 4.5 机构调研核心问答\n（本节无调研/会议数据，仅输出此标题行，内容留空）"
    )

    prompt = f"""为 {name} 合并撰写第4章中的两个小节（4.1和4.5），目标是两节内容不重复、上下呼应。
{maincomp_section}
【主营业务板块及规模（{y0}年）】
{segs_data}

【财务概览】
{_compact_fin(fin)}

【公司基础信息】
{json.dumps(company_info, ensure_ascii=False)[:400] if company_info else "（无）"}

{"【管理层讨论（摘要）】" + chr(10) + mgmt_text if mgmt_text else ""}

【研报摘要（理解商业模式）】
{_compact_reports(reports, n=2)}

【引用映射】
maincomp=[{ref_map.get('maincomp',{}).get('n','')}], fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]
{qa_data_section}
【格式要求】按顺序输出以下两个小节，不输出其他内容：

### 4.1 盈利方式
用2-3个 bullet（• 开头），格式：**[盈利维度]**：[结合本公司实际如何通过此维度赚钱]
先说行业通用盈利逻辑，再用本公司具体数字说明竞争位置；不重复4.2表格已有数字明细；标注引用；**120-160字以内**。

{qa_instruction}
"""
    result = call_claude(client, prompt, max_tokens=1200)

    # 按 ### 4.1 / ### 4.5 切分，连同标题行一并消掉（模板已有标题，避免重复）
    parts_45 = re.split(r'###\s*4\.5[^\n]*\n?', result, maxsplit=1)
    s41_parts = re.split(r'###\s*4\.1[^\n]*\n?', parts_45[0], maxsplit=1)
    s41 = s41_parts[1].strip() if len(s41_parts) > 1 else parts_45[0].strip()
    s45 = ""
    if len(parts_45) > 1:
        s45_raw = parts_45[1].strip()
        # 跳过无数据提示
        if not re.search(r'无.*数据|留空|跳过', s45_raw) and len(s45_raw) > 30:
            s45 = s45_raw
    # ── fallback：正则切分失败时尝试更宽松的匹配 ──
    if not s45 and has_qa:
        # 宽松切分：容忍 deepseek 等模型用 ## / ** / 无标题等变体
        fb_parts = re.split(
            r'(?:#{2,4}\s*)?4\.5[^\n]*?(?:机构调研|核心问答|调研问答|Q&A|QA)[^\n]*\n?',
            result, maxsplit=1, flags=re.IGNORECASE
        )
        if len(fb_parts) > 1:
            s45_raw = fb_parts[1].strip()
            if not re.search(r'无.*数据|留空|跳过', s45_raw) and len(s45_raw) > 30:
                s45 = s45_raw
        # 仍失败：用原始调研数据提取 Q&A 兜底生成 4.5 节内容
        if not s45:
            s45 = _fallback_qa_from_raw(qa_blocks)

    return {"s4_profit_model": s41, "s4_survey_qa": s45}


def gen_section5(client, key_data: dict) -> str:
    """5 产销链分析"""
    reports = key_data["reports"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    company_info = key_data.get("company_info", {})
    raw_data = key_data.get("_raw_data", {})
    mgmt_text = _compact_mgmt(raw_data, max_len=1500)
    mc_region = key_data.get("mc_region", "")   # 近3年年报地区收入数据（Markdown表格字符串）

    region_block = ""
    if mc_region:
        region_block = f"""
【地区收入拆分（来自 getFdmtMoStdItem classifCD=3，精确数据）】
{mc_region}
*（数据来源：getFdmtMoStdItem接口）*[{ref_map.get('maincomp',{}).get('n','')}]
"""

    prompt = f"""为 {name} 撰写"产销链分析"章节（第5节）。

⚠️ **禁止输出任何标题**（如 ### 5. 产销链分析 或 ### 5.1 等），直接输出内容。上方模板已有 ## 5 产销链分析 标题，不要再重复。

【公司信息】
{json.dumps(company_info, ensure_ascii=False)[:600] if company_info else "（无结构化数据）"}

{"【管理层讨论（客户/供应商相关）】" + chr(10) + mgmt_text if mgmt_text else ""}

【研报关键信息（客户结构、供应商、竞争关系）】
{_compact_reports(reports, n=3)}
{region_block}
【引用映射】
{_refs_str(reports, ref_map, 3)}
{"mgmt=[" + str(ref_map.get('mgmt_discussion',{}).get('n','')) + "]" if mgmt_text else ""}
maincomp=[{ref_map.get('maincomp',{}).get('n','')}]

【格式要求】整节200-320字，数字密度要高，具体数字越多越好（收入占比、同比增速等均需量化）。

**区域/渠道收入拆分**：
{"- 已提供精确地区数据（见上方表格），**直接将该表格原样输出**，不要改写数字，可在表格后加1句趋势说明，标注引用" if mc_region else "- 若无区域拆分数据，则省略本部分"}

**主要客户**：
- 若能从数据中找到具体客户名称（机构/企业/个人类型均可），输出 Markdown 表格：
  | 名称 | 类型 | 合作情况/规模/占比 |
  |------|------|------|
- 若无具体名称，则2-3句文字描述：客户类型、规模/数量变化（同比增速），标注引用

**新客户拓展**：[2-3句：近期新增方向、具体规模或数量变化、预期贡献，标注引用]

**主要供应商**：
- 制造业：若有具体供应商名称，输出表格（| 名称 | 供应内容 | 占比/规模 |）；否则文字描述集中度和议价能力，含具体数字
- 服务业/金融业：说明核心资源要素（资本金规模、员工人数、技术系统），标注引用
"""
    return call_claude(client, prompt, max_tokens=900)


def gen_section6_health(client, key_data: dict) -> str:
    """6.2 财务健康评估"""
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    years = fin["years"]

    # 杜邦数据
    y0 = years[0] if years else None
    d = fin.get(y0, {}) if y0 else {}
    net_margin = d.get("netMargin")
    total_assets = d.get("totalAssets")
    revenue = d.get("tRevenue")
    equity = d.get("totalEquity")
    roe = d.get("ROE")

    asset_turn = revenue / total_assets if (revenue and total_assets) else None
    eq_mult = total_assets / equity if (total_assets and equity) else None
    dupont = f"净利率{_pct(net_margin)} × 资产周转率{asset_turn:.3f}次 × 权益乘数{eq_mult:.2f} = ROE约{(net_margin/100 * asset_turn * eq_mult * 100):.2f}%" if (net_margin and asset_turn and eq_mult) else "（数据不足）"

    fin_table_ctx = key_data.get("financial_table_ctx", "")
    fin_table_section = (
        f"\n【6.1关键财务指标表格（已生成，6.2解读趋势与驱动因素，不要重复表中已有数字，可用'如上表'指向）】\n{fin_table_ctx}\n"
        if fin_table_ctx else ""
    )

    prompt = f"""为 {name} 撰写"财务健康评估"（第6.2节）。
{fin_table_section}
【财务数据】
{_compact_fin(fin)}

【杜邦数据（{y0}A）】
{dupont}
报告ROE: {_pct(roe)}（加权ROE: {_pct(d.get('ROEW'))}）

【引用映射】
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】**每条不超过100字**，输出以下四条，每条单独一行，紧凑精炼：
- **盈利能力**：净利率/ROE趋势+核心驱动因素，精确数字，标注引用
- **偿债能力**：资产负债率/净资本充足性/偿债压力，标注引用
- **现金流质量**：经营现金流净额与净利润的比值（精确计算后直接写出数字），说明主因，标注引用
- **ROE杜邦分析（{y0}A）**：直接使用上方已提供的杜邦公式数据，格式"净利率X% × 资产周转率X次 × 权益乘数X = ROE约X%"，标注引用
"""
    return call_claude(client, prompt, max_tokens=800)


def gen_section7(client, key_data: dict) -> str:
    """7 公司调研大纲"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    meetings = key_data.get("meetings", [])

    prompt = f"""为 {name} 撰写"公司调研大纲"（第7节），围绕当前市场核心关切，精选3-4个议题，每议题2个核心问题。
问题要具体、可量化，能体现对公司的深度理解。

【近期研报核心关注点（市场分歧和关键催化剂）】
{_compact_reports(reports, n=4)}

【近期会议纪要（已有的管理层回应，帮助识别未解决的问题）】
{_compact_meetings(meetings, n=3)}

【财务数据（识别需要追问的财务异常点）】
{_compact_fin(fin)}

【引用映射】
{_refs_str(reports, ref_map, 4)}
{_meeting_refs_str(meetings, ref_map, n=3)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】**全节250-280字**，精炼专业。精选3-4个议题，每议题格式如下：

**议题N：[议题名称]**
背景：[1句话，不超过30字，含1个关键数字，标注引用]
- 问题1）：[≤60字，具体可量化]
- 问题2）：[≤60字，执行层面追问]

严格执行：背景≤30字，每个问题≤60字，不要超长叙述。
"""
    return call_claude(client, prompt, max_tokens=900)


def gen_section8(client, key_data: dict) -> str:
    """8 行业分析及同业对比（8.1行业格局）"""
    reports = key_data["reports"]
    valuation = key_data["valuation"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    fin = key_data["fin"]
    peer_ctx = key_data.get("peer_table_ctx", "")

    pe_data = valuation["items"].get("市盈率PE", {})

    peer_section = f"""
【8.2同业比较表格（已生成，供行业分析参考——可引用表中可比公司的数据做横向对比，但不要重复表格内容）】
{peer_ctx}
""" if peer_ctx else ""

    prompt = f"""为 {name} 所在行业撰写第8节"行业分析及同业对比"中的行业格局分析。

⚠️ 分析对象是整个行业，不是 {name} 个股本身。不要出现 {name} 的具体产品名称、流水数据或个股估值。
核心驱动因素写行业共性驱动（用户规模趋势、出海渗透、IP商业化、技术升级等），举例时点名行业头部玩家及其数据；
主要风险写行业共性风险（版号监管、渠道佣金、用户增长瓶颈、竞争格局恶化等），不写某产品流水不及预期之类的个股风险。
{peer_section}
【估值数据】
PE: {pe_data.get('val','—')}x (行业均值{pe_data.get('avg','—')}x, 排名{pe_data.get('rank','—')}/{pe_data.get('rankBase','—')})
估值评价: {valuation.get('comment','')}

【研报行业分析内容（含同业对比数据）】
{_compact_reports(reports, n=5)}

【财务数据】
{_compact_fin(fin)}

【引用映射】
{_refs_str(reports, ref_map, 5)}
valuation_rank=[{ref_map.get('valuation_rank',{}).get('n','')}]

【格式要求】
以下面的标题行作为你的第一行，然后输出内容。不得生成任何其他子章节（不要 ### 8.2 或其他标题）。风险在第10章单独处理，此处不写风险要点。

### 8.1 行业格局
只写以下3个要点（• 开头），不写风险：

1. **周期位置与行业增速**：行业所处周期阶段，引用全行业收入/用户规模等宏观数据（精确数字），标注引用
2. **集中度/竞争格局**：点名头部玩家（如腾讯、网易）及A股中腰部厂商，给出市占率或CR数字，标注引用
3. **核心驱动因素**：行业共性驱动力（用户增长、出海渗透、IP商业化、技术升级等）；举例时**必须明确点名具体公司和产品**（如"网易《逆水寒》海外版……"、"米哈游《原神》开创……"），禁止使用"头部新品""某款产品"等模糊表述，标注引用

每点1-2句含具体数字，**150字以内**。
"""
    result = call_claude(client, prompt, max_tokens=900)
    # 清理引用映射失败时 LLM 可能生成的占位符
    result = re.sub(r'\[research\]', '', result)
    # 只保留 ### 8.1 行业格局 的内容，截断任何 LLM 自行添加的后续子章节
    lines = result.splitlines()
    kept = []
    for line in lines:
        if re.match(r"^###\s+8\.[2-9]", line):
            break
        kept.append(line)
    return "\n".join(kept).rstrip()


def gen_section9_valuation(client, key_data: dict) -> str:
    """9.3/9.4 估值分析文字 + 情景推演表格"""
    valuation = key_data["valuation"]
    fin = key_data["fin"]
    forecasts = key_data["consensus_forecasts"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]

    pe_data = valuation["items"].get("市盈率PE", {})
    pb_data = valuation["items"].get("市净率PB", {})

    fc_text = "\n".join([
        f"2026E: 净利{_fmt(f.get('conProfit'), unit=1e4)}亿, EPS{f.get('conEps','—')}, PE{round(f.get('conPe',0),1)}x"
        for f in forecasts[:2]
    ])

    # ── 动态构建估值维度表格行（只保留有实际数据的维度）──────────────────────
    def _has_val(d):
        v = d.get("val")
        return v is not None and str(v).strip() not in ("", "—", "0", "0.0")

    val_rows = []
    # 按优先级遍历所有已知维度，也兜底遍历接口返回的其他维度
    _dim_priority = ["市盈率PE", "市净率PB", "市销率PS", "EV/EBITDA", "市现率PCF"]
    _seen = set()
    for dim_name in _dim_priority + [k for k in valuation["items"] if k not in _dim_priority]:
        d = valuation["items"].get(dim_name, {})
        if not _has_val(d):
            continue
        _seen.add(dim_name)
        avg_str = f"，行业均值{d.get('avg','—')}x" if d.get("avg") else ""
        rank_str = (f"，排名{d.get('rank','—')}/{d.get('rankBase','—')}"
                    if d.get("rank") and d.get("rankBase") else "")
        val_rows.append(
            f"| {dim_name} | {d.get('val','—')}x{avg_str}{rank_str} | [分析此维度当前是否低估/合理/偏高，1句话] |"
        )

    if not val_rows:
        val_rows = ["（当前无可用估值分位数据，从研报及PE/PB角度简述估值判断）"]

    val_table_str = (
        "| 估值维度 | 当前水平 | 解读 |\n"
        "|:---------|:---------|:-----|\n"
        + "\n".join(val_rows)
    )

    consensus_ctx = key_data.get("consensus_table_ctx", "")
    forecast_ctx  = key_data.get("forecast_table_ctx", "")
    tables_section = ""
    if consensus_ctx or forecast_ctx:
        tables_section = "\n【9.1/9.2已生成表格（9.3不重复表中数字，只做解读和判断）】\n"
        if consensus_ctx:
            tables_section += f"9.1市场一致预期：\n{consensus_ctx}\n"
        if forecast_ctx:
            tables_section += f"9.2各机构预测：\n{forecast_ctx}\n"

    prompt = f"""为 {name} 撰写第9节的估值分析（9.3）和情景推演（9.4）。
{tables_section}
【估值数据】
PE(TTM): {pe_data.get('val','—')}x，行业均值{pe_data.get('avg','—')}x，排名{pe_data.get('rank','—')}/{pe_data.get('rankBase','—')}
PB: {pb_data.get('val','—')}x，行业均值{pb_data.get('avg','—')}x
估值评价: {valuation.get('comment','')}

【一致预期数据】
{fc_text}

【财务数据（最近实际年度）】
{_compact_fin(fin)}

【引用映射】
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]
consensus=[{ref_map.get('consensus',{}).get('n','')}]
valuation_rank=[{ref_map.get('valuation_rank',{}).get('n','')}]

【格式要求】

### 9.3 估值分析
[**1句话**：当前估值所处位置（高/低/合理）及核心判断依据，数字已在下方表格中，此处不重复；标注引用]

{val_table_str}

**重要**：上方表格已按实际有数据的维度生成，**只填写每行的"解读"列，不新增行、不删除行、不修改前两列**。

### 9.4 情景推演
**核心变量**（3-5个）：
⚠️ **核心变量必须是驱动业务的输入侧指标**，例如：出货量/装机量、单价/单瓦盈利、产能利用率、市占率、毛利率、扩产节奏、原材料成本等——取决于行业特性。
⚠️ **严禁将营收、净利润、EPS、归母净利润等财务结果填为核心变量**，这些是预测的输出，不是输入。
• **[业务驱动变量1，如出货量/装机量/单价]**：基准值X
• **[业务驱动变量2]**：基准值X
• ...（3-5个，与下方情景表不重复，只填名称和基准值）

**情景推演**：

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~X%） | [具体数字] | [收入/利润结果] | [PE/目标价] |
| 中性（概率~X%） | [具体数字] | [基准预测] | [基准PE/目标价] |
| 悲观（概率~X%） | [具体数字] | [下行结果] | [下行PE/价格] |

三种情景概率之和100%，所有假设给出具体数字，标注引用。
"""
    return call_claude(client, prompt, max_tokens=1600)


def gen_section10(client, key_data: dict) -> str:
    """10 风险提示"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]

    prompt = f"""为 {name} 撰写"风险提示"章节（第10节）。

【研报风险提示内容】
{_compact_reports(reports[:4])}

【财务数据（识别财务风险指标）】
{_compact_fin(fin)}

【引用映射】
{_refs_str(reports, ref_map, 4)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】**全节严格控制在250字以内**，输出3-4条风险（不要5条），每条1-2句话，每条格式：
- **风险标题**：一句话描述核心风险 + 量化影响（如"若X发生，预计净利润下滑XX%"），标注引用

**重要**：每条风险合计不超过60字，宁可少写一条，也不要超字数。风险须针对本公司特有风险，不泛泛而谈。
"""
    return call_claude(client, prompt, max_tokens=500)


def _strip_all_dash_columns(table_md: str, min_peer_rows: int = 0) -> str:
    """移除Markdown表格中数据不足的列（保留表头不变）。
    min_peer_rows=0（默认）：移除所有行均为'—'的列
    min_peer_rows=1：移除仅第一行（标的公司）有数据、其余可比公司均为'—'的列
    """
    if not table_md or '|' not in table_md:
        return table_md
    lines = [l for l in table_md.strip().split('\n') if '|' in l]
    if len(lines) < 3:
        return table_md

    def parse_row(line):
        parts = line.split('|')
        return [p.strip() for p in parts[1:-1]]  # 去掉首尾空项

    def is_dash(v):
        return v.strip() in ('—', '-', '', '——', '--', '─', '－')

    header_parts = parse_row(lines[0])
    sep_parts    = parse_row(lines[1])
    data_rows    = [parse_row(l) for l in lines[2:]]
    n_cols = len(header_parts)

    cols_to_keep = []
    for i in range(n_cols):
        col_vals = [row[i] if i < len(row) else '' for row in data_rows]
        if min_peer_rows > 0 and len(col_vals) > 1:
            # 检查除第一行（标的公司）外，有几行有实际数据
            peer_non_dash = sum(1 for v in col_vals[1:] if not is_dash(v))
            cols_to_keep.append(peer_non_dash >= min_peer_rows)
        else:
            cols_to_keep.append(not all(is_dash(v) for v in col_vals))

    if all(cols_to_keep):
        return table_md  # 无列需要移除

    def rebuild(parts):
        filtered = [parts[i] if i < len(parts) else '' for i in range(n_cols) if cols_to_keep[i]]
        return '| ' + ' | '.join(filtered) + ' |'

    result_lines = [rebuild(header_parts), rebuild(sep_parts)]
    for row in data_rows:
        result_lines.append(rebuild(row))
    return '\n'.join(result_lines)


def gen_peer_table(client, key_data: dict) -> str:
    """生成同业比较表格（优先使用getMaterialsV2素材，其次研报）"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    valuation = key_data["valuation"]
    name = key_data["name"]
    peer_materials = key_data.get("peer_materials") or []
    mc = key_data.get("mc", {})

    years = fin.get("years", [])
    y0 = years[0] if years else "?"
    d = fin.get(y0, {}) if y0 else {}
    rev = _fmt(d.get("tRevenue"))
    np_ = _fmt(d.get("NPAttrP"))
    nm = _pct(d.get("netMargin"))

    # 提取主营业务段（前3个，用于告诉LLM公司所在行业）
    mc_segs = mc.get("segments", {})
    mc_years = mc.get("years", [])
    primary_biz = ""
    if mc_segs and mc_years:
        y = mc_years[0]
        tops = sorted(mc_segs.items(), key=lambda kv: (kv[1][0] or 0) if isinstance(kv[1], list) else 0, reverse=True)[:3]
        primary_biz = "、".join(k for k, _ in tops) if tops else ""

    pe_data = valuation["items"].get("市盈率PE", {})
    pb_data = valuation["items"].get("市净率PB", {})
    pe = f"{round(pe_data.get('val', 0), 1)}x" if pe_data.get("val") else "—"

    # API验证过的可比公司当前官方名称（防止曾用名污染）
    peer_validated = key_data.get("peer_validated") or []
    peer_validated_text = ""
    if peer_validated:
        lines = ["【API已验证的可比公司当前A股注册简称（必须使用以下名称，不使用历史/曾用名）】"]
        for pv in peer_validated:
            q = pv.get("query", "")
            cn = pv.get("current_name", "")
            code = pv.get("code", "")
            note = f"（原查询词：{q}）" if q != cn else ""
            lines.append(f"  代码 {code}：当前注册简称为「{cn}」{note}")
        peer_validated_text = "\n".join(lines)

    # getMaterialsV2 素材（优先，最多5条）
    peer_mat_text = ""
    if peer_materials and isinstance(peer_materials, list):
        snippets = []
        for item in peer_materials[:5]:
            title = item.get("title", "")
            text = item.get("text", "")[:2000]
            dtype = item.get("dataType", "")
            snippets.append(f"[{dtype}] {title}\n{text}")
        peer_mat_text = "\n\n---\n\n".join(snippets)

    # 研报摘要（兜底）
    peer_reports_text = "\n\n---\n\n".join(
        f"[{r['id']}]{r['org']} {r['date']}\n"
        f"研报摘要：{(r.get('detail_text') or r['abstract'])[:2000]}\n"
        f"正文：\n{r['text'][:2000]}"
        for r in reports[:4]
    )

    # 判断是否有足够丰富的同业素材（有丰富素材时才尝试填入财务数字）
    has_rich_peer_data = (
        peer_materials and len(peer_materials) >= 3
        and any(len(str(item.get("text", ""))) > 800 for item in peer_materials)
    )

    if has_rich_peer_data:
        fin_col_instruction = """财务数字列（市值、营收、净利、净利率、PE）：
1. 标的公司（第一行）的财务数字已由程序填入，直接使用，不要修改
2. 可比公司：**优先从素材文本中提取**实际财务数字填入对应单元格；若素材中明确有某指标数字，务必填入；若素材中完全没有提及，填"—"
3. 严禁自行估算或标注"约"——只填素材中明确出现的数字"""
        table_header = "| 公司 | 代码 | 可比业务（与标的重叠） | 相关业务进展 | 竞争关系 | 市值（亿） | 营收（亿） | 净利（亿） | 净利率 | PE（TTM） |"
        table_sep = "|:-----|:-----|:------|:------|:------|:------|:------|:------|:------|:------|"
        table_example = f"| [标的简称] | [代码] | [核心业务] | [最新进展] | — | — | {rev} | {np_} | {nm} | {pe} |"
    else:
        fin_col_instruction = """此次素材不足以可靠填充财务数字列，**输出不含财务列的精简表格**（仅5列）：
| 公司 | 代码 | 可比业务（与标的重叠） | 相关业务进展 | 竞争关系 |"""
        table_header = "| 公司 | 代码 | 可比业务（与标的重叠） | 相关业务进展 | 竞争关系 |"
        table_sep = "|:-----|:-----|:------|:------|:------|"
        table_example = f"| [标的简称] | [代码] | [核心业务] | [最新进展] | — |"

    prompt = f"""为 {name} 生成同业可比公司 Markdown 表格。

【标的公司（{name}）已知数据】
{y0}A 营收: {rev}亿  净利: {np_}亿  净利率: {nm}  PE(TTM): {pe}  PB: {pb_data.get('val','—')}x
主营业务（按收入排序）：{primary_biz or "见研报"}

{peer_validated_text}

{"【同业对比素材（getMaterialsV2，信息密度最高）】" + chr(10) + peer_mat_text if peer_mat_text else ""}

【研报内容（补充参考）】
{peer_reports_text}

⚠️ 素材过滤规则：如素材/研报中出现的公司与 {name} 主营业务差异显著（如仅有旅游景点运营、文旅综合体、非免税零售），则忽略这些素材数据，改用你对该行业直接竞争对手的知识填充表格。
【格式要求】
输出一个完整的 Markdown 表格（严格遵守格式，不输出其他文字）：

{table_header}
{table_sep}
{table_example}
| [可比公司简称] | [代码] | [重叠业务] | [进展] | [直接竞争/部分竞争/互补] |（财务列按上述规则处理）
...（含3-5家国内可比公司，如有相关海外龙头也需列入）

**可比公司选择规则（按优先级）**：
1. 优先选择与 {name} 存在**直接业务竞争关系**的上市公司（相同核心业务/客群/渠道）
2. 次选主营中有较大重叠比例的上市公司（间接竞争或业务交叉）
3. 如素材/研报未明确提及竞争对手，**根据行业知识**补充直接竞争对手，不得以旁观行业公司凑数
4. 金融机构、非同业公司一律排除（除非 {name} 本身就是金融公司）

**⚠️ 公司名称严格规范**：
- 只使用**当前有效的A股注册简称**，严禁使用曾用名、已更名公司的旧名称（如某公司已更名，必须用新名称）
- 已知更名示例：**600185 的注册简称已从"格力地产"更名为"珠免集团"**，必须写"珠免集团"而非"格力地产"
- 如不确定某公司当前注册简称，宁可写"代码XXXXXX（简称待确认）"，也不要写过时的历史名称
- 代码格式：A股6位数字，港股+.HK，美股+.US
- 公司列填2-4字股票简称

文本列填写规则：
1. 优先使用同业素材和研报中的描述
2. **相关业务进展**：只写业务动态、产品/市场/战略进展等定性信息，**严禁写营收/净利润等财报数字**（财报数字放财务列）
3. 竞争关系仅填"直接竞争"/"部分竞争"/"互补"

{fin_col_instruction}
"""
    result = call_claude(client, prompt, max_tokens=1100)
    # 清理引用映射失败时 LLM 可能生成的占位符
    result = re.sub(r'\[research\]', '', result)
    if "|" not in result:
        result = (
            f"| 公司 | 代码 | 可比业务 | 相关进展 | 竞争关系 | 市值（亿） | 营收（亿） | 净利（亿） | 净利率 | PE(TTM) |\n"
            f"|:-----|:-----|:------|:------|:------|:------|:------|:------|:------|:------|\n"
            f"| {name} | — | 全业务线 | 见报告正文 | — | — | {rev} | {np_} | {nm} | {pe} |\n"
            "_（同业数据：素材中未找到可比公司数据，请参考第8.1节文字分析）_"
        )
    return _strip_all_dash_columns(result, min_peer_rows=1)


# ─────────────────────────────────────────────────────────────────────────────
# 报告组装
# ─────────────────────────────────────────────────────────────────────────────

def assemble_report(meta: dict, sections: dict, ref_map: dict) -> str:
    """将所有章节组装为完整 Markdown 报告"""
    name = meta.get("name", "")
    short_name = meta.get("short_name", name)
    ticker = meta.get("ticker", "")
    date = TODAY
    conclusion = sections.get("title_conclusion", "")
    title_line = (f"# {short_name}（{ticker}）公司一页纸：{conclusion}"
                  if conclusion else f"# {short_name}（{ticker}）公司一页纸")

    pe_val = sections["valuation"]["items"].get("市盈率PE", {}).get("val")
    pb_val = sections["valuation"]["items"].get("市净率PB", {}).get("val")
    pe_str = f"{round(pe_val, 2)}x" if pe_val else "—"
    pb_str = f"{round(pb_val, 2)}x" if pb_val else "—"

    charts = sections.get("charts", {})
    profit_chart = charts.get("profit", "")
    margin_chart = charts.get("margin", "")
    revenue_chart = charts.get("revenue", "")
    structure_chart = charts.get("structure", "")

    # 图表 markdown
    def chart_md(url, caption):
        if url:
            return f"\n![{caption}]({url})\n"
        return ""

    # 参考资料章节
    refs_md = refs_to_markdown(ref_map)

    # 9.2 表格：只有有数据才显示
    forecast_table_md = sections.get("forecast_table", "")
    section_9_2 = ""
    if forecast_table_md:
        section_9_2 = f"""
### 9.2 各机构盈利预测

**数据来源**：research_sec_foredata接口，取近3个月最新预测数据[{ref_map.get('profit_forecast',{}).get('n','')}]

{forecast_table_md}
"""

    md = f"""{title_line}

**日期**：{date}　｜　**PE(TTM)**：{pe_str}　｜　**PB**：{pb_str}

---

## 1 公司近况跟踪 ⭐⭐⭐

{sections['s1']}

---

## 2 核心投资逻辑 ⭐⭐⭐

{sections['s2']}

---

## 3 催化事件时间表

{sections['s3']}

---

## 4 公司业务拆分

### 4.1 盈利方式

{sections['s4_profit_model']}

### 4.2 分板块业务数据

**数据来源**：getFdmtMoStdItem接口（近3年年报，按产品分类）[{ref_map.get('maincomp',{}).get('n','')}]

{sections['maincomp_table']}
{chart_md(revenue_chart, '营业收入及同比趋势')}
{chart_md(structure_chart, '营收结构占比')}
{chart_md(margin_chart, '分业务毛利率')}

### 4.3 业务深度分析

{sections.get('s4_deep_analysis', '')}

### 4.4 核心竞争力与竞争优势

{sections.get('s4_deep_advantage', '')}

{f"### 4.5 机构调研核心问答{chr(10)}{chr(10)}{sections['s4_survey_qa']}{chr(10)}{chr(10)}---" if sections.get('s4_survey_qa') else "---"}

## 5 产销链分析

{sections['s5']}

---

## 6 公司财务数据分析

### 6.1 关键财务指标

**数据来源**：fdmtNew接口，近3年年报{f"＋最新期（{sections['fin_latest_label']}）" if sections.get('fin_latest_label') else ""}真实数据[{ref_map.get('fdmtNew',{}).get('n','')}]

{sections['financial_table']}
{chart_md(profit_chart, '归母净利润及同比趋势')}

### 6.2 财务健康评估

{sections['s6_health']}

---

## 7 公司调研大纲

{sections['s7']}

---

## 8 行业分析及同业对比

{sections['s8_industry']}

### 8.2 同业比较

{sections['peer_table']}

---

## 9 一致预期、盈利预测与估值

### 9.1 市场一致预期

**数据来源**：research_sec_coredata接口[{ref_map.get('consensus',{}).get('n','')}]

{sections['consensus_table']}
{section_9_2}

{sections['s9_valuation']}

---

## 10 风险提示

{sections['s10']}

---

{refs_md}

---

*本报告仅供参考，不构成投资建议。数据截止日期{date}。*
"""
    return md


# ─────────────────────────────────────────────────────────────────────────────
# 平台配置读取（Claude Code / Codex / Qoder / Workbuddy / OpenClaw / datayesclaw 等）
# ─────────────────────────────────────────────────────────────────────────────

def _first_env(names):
    """Return (name, value) for the first non-empty environment variable."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value.strip()
    return "", ""


def _candidate_model_config_paths():
    """Known local config locations used by common agent shells and wrappers."""
    paths = [
        "~/.datayesclaw/agents/main/agent/models.json",
        "~/.openclaw/agents/main/agent/models.json",
        "~/.qoder/agents/main/agent/models.json",
        "~/.qoder/models.json",
        "~/.workbuddy/agents/main/agent/models.json",
        "~/.workbuddy/models.json",
        "~/.codex/models.json",
        "~/.config/codex/models.json",
        "~/.claude/models.json",
        "~/.config/claude/models.json",
    ]
    expanded = [os.path.expanduser(p) for p in paths]
    # Some platforms put agent configs under versioned/profile subdirectories.
    expanded.extend(glob.glob(os.path.expanduser("~/.*/agents/*/agent/models.json")))
    expanded.extend(glob.glob(os.path.expanduser("~/.config/*/models.json")))
    # Preserve order while de-duplicating.
    seen, result = set(), []
    for p in expanded:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _iter_model_providers_from_config(cfg, source_path):
    """Normalize several provider config shapes to provider rows."""
    result = []
    providers = cfg.get("providers", cfg if isinstance(cfg, dict) else {})
    if isinstance(providers, list):
        iterable = [(str(i), p) for i, p in enumerate(providers)]
    elif isinstance(providers, dict):
        iterable = list(providers.items())
    else:
        iterable = []

    for pid, prov in iterable:
        if not isinstance(prov, dict):
            continue
        key = (prov.get("apiKey") or prov.get("api_key") or prov.get("key")
               or prov.get("token") or prov.get("authToken") or "").strip()
        url = (prov.get("baseUrl") or prov.get("base_url") or prov.get("apiBase")
               or prov.get("api_base") or prov.get("endpoint") or prov.get("url") or "").strip()
        models = prov.get("models") or prov.get("modelList") or []
        if isinstance(models, dict):
            models = list(models.values())
        if not models:
            models = [{"id": prov.get("model") or prov.get("modelId") or prov.get("model_id") or ""}]
        for m in models:
            model_id = m.get("id") if isinstance(m, dict) else str(m)
            result.append({
                "provider_id": str(pid),
                "api_key": key,
                "base_url": url,
                "model_id": model_id or "",
                "source": source_path,
            })
    return result


def _load_models_json():
    """Read known platform model configs, returning provider rows.

    Each row contains api_key, base_url, model_id, provider_id, source.
    Missing files are ignored; malformed files do not abort report generation.
    """
    result = []
    for path in _candidate_model_config_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            result.extend(_iter_model_providers_from_config(cfg, path))
        except Exception as e:
            print(f"  ⚠️ 跳过无法读取的平台模型配置: {path} ({e})")
    return result


def _infer_llm_format(api_key, base_url, source_var=""):
    s = " ".join([api_key or "", base_url or "", source_var or ""]).lower()
    # Anthropic 原生 key 或显式 anthropic 字样 → anthropic
    if "anthropic" in s or (api_key or "").startswith("sk-ant-"):
        return "anthropic"
    # Datayes llm-proxy / Bedrock 代理 → 使用 Anthropic Messages API 格式
    if "datayes" in s or "llm-proxy" in s or "bedrock" in s:
        return "anthropic"
    return "openai"


def _print_credential_source(label, source, base_url, model, fmt):
    safe_url = base_url if base_url else "(default)"
    print(f"  [{label}] 凭据来源: {source}; endpoint={safe_url}; format={fmt}; model={model}")


def _legacy_load_models_json_single_path():
    """Backward-compatible reader kept for old datayesclaw config shape."""
    path = os.path.expanduser("~/.datayesclaw/agents/main/agent/models.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        result = []
        for pid, prov in cfg.get("providers", {}).items():
            key = prov.get("apiKey", "").strip()
            url = prov.get("baseUrl", "").strip()
            for m in prov.get("models", []):
                result.append({
                    "provider_id": pid,
                    "api_key":     key,
                    "base_url":    url,
                    "model_id":    m.get("id", ""),
                })
        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="公司一页纸报告生成器")
    parser.add_argument("--data",     required=True, help="fetch_data.py 输出的 JSON 文件路径")
    parser.add_argument("--output",   required=True, help="MD 输出路径")
    parser.add_argument("--docx",     default=None,  help="Word 输出路径（可选）")
    parser.add_argument("--api-key",  default=None,  help="API Key（可选，通常由平台环境变量自动注入）")
    parser.add_argument("--base-url", default=None,  help="API Base URL（可选，用于代理/自定义端点）")
    # 模型：优先命令行 > 平台注入的环境变量 > 默认值
    _env_model = (os.environ.get("ANTHROPIC_MODEL")
                  or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                  or os.environ.get("OPENAI_MODEL_NAME")
                  or os.environ.get("MODEL_NAME")
                  or "claude-sonnet-4-6")
    parser.add_argument("--model", default=_env_model,
                        help="模型 ID（默认自动从平台环境变量读取）")
    args = parser.parse_args()

    global MODEL, _LLM_ENDPOINT, _LLM_API_KEY, _LLM_FORMAT
    MODEL = args.model

    t0 = time.time()
    print("[" + str(round(time.time()-t0, 1)) + "s] 使用模型: " + MODEL)

    # ── 1. 初始化 LLM 端点（纯 HTTP，无 SDK 依赖）──────────────────────────────

    # 优先加载脚本同目录的 .env 文件
    _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_file):
        with open(_env_file, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())
        print(f"[{round(time.time()-t0,1)}s] 已加载配置文件: {_env_file}")

    key_env_names = [
        # OpenAI-compatible agent platforms.
        "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "OPENAI_ACCESS_TOKEN",
        "QODER_API_KEY", "QODER_AUTH_TOKEN", "QODER_OPENAI_API_KEY",
        "WORKBUDDY_API_KEY", "WORKBUDDY_AUTH_TOKEN", "WORKBUDDY_OPENAI_API_KEY",
        "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "CODEX_OPENAI_API_KEY",
        "OPENCLAW_API_KEY", "DATAYESCLAW_API_KEY", "CUSTOM_API_KEY",
        # Anthropic / Claude Code.
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY",
        # Generic fallbacks.
        "LLM_API_KEY", "AI_API_KEY", "CHAT_API_KEY", "MODEL_API_KEY",
        "API_KEY", "SECRET_KEY",
    ]
    url_env_names = [
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ENDPOINT",
        "QODER_BASE_URL", "QODER_OPENAI_BASE_URL", "QODER_ENDPOINT",
        "WORKBUDDY_BASE_URL", "WORKBUDDY_OPENAI_BASE_URL", "WORKBUDDY_ENDPOINT",
        "CODEX_BASE_URL", "CODEX_OPENAI_BASE_URL", "CODEX_ENDPOINT",
        "OPENCLAW_BASE_URL", "DATAYESCLAW_BASE_URL", "CUSTOM_BASE_URL",
        "ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL",
        "LLM_BASE_URL", "AI_BASE_URL", "API_BASE_URL", "API_ENDPOINT",
    ]

    key_var, key_from_env = _first_env(key_env_names)
    url_var, url_from_env = _first_env(url_env_names)

    # API Key 查找顺序：--api-key > 多平台环境变量 > 本地模型配置
    api_key = (args.api_key or key_from_env or "")
    base_url = (args.base_url or url_from_env or "")
    credential_source = "command line" if args.api_key else (key_var or "")

    # Model 补充查找
    if MODEL == "claude-sonnet-4-6":
        MODEL = (os.environ.get("ANTHROPIC_MODEL")
                 or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                 or os.environ.get("OPENAI_MODEL_NAME")
                 or os.environ.get("LLM_MODEL")
                 or os.environ.get("AI_MODEL")
                 or os.environ.get("MODEL_NAME")
                 or os.environ.get("MODEL_ID")
                 or MODEL)

    # 从多平台模型配置读取缺失的 api_key / base_url / model_id
    _mj = _load_models_json()
    if _mj and (not api_key or not base_url):
        _chosen = (next((p for p in _mj if p["api_key"] == api_key and p["base_url"]), None)
                   if api_key else None)
        if not _chosen:
            _chosen = next((p for p in _mj if p["api_key"] and p["base_url"]), None)
        if _chosen:
            if not api_key:
                api_key = _chosen["api_key"]
                credential_source = _chosen.get("source") or _chosen["provider_id"]
            if not base_url:
                base_url = _chosen["base_url"]
            if MODEL == "claude-sonnet-4-6":
                MODEL = _chosen["model_id"]
            print(f"  [platform-config] 使用模型配置: {_chosen['provider_id']} / {MODEL} / {_chosen.get('source','')}")

    if not api_key:
        cands = [k for k in os.environ if any(w in k.lower()
                 for w in ("key", "token", "api", "auth", "secret", "model", "endpoint", "url"))]
        print("❌ 未找到 API Key。")
        if cands:
            print("   当前环境中检测到以下可能相关的变量（供参考）：")
            for k in sorted(cands):
                v = os.environ[k]
                print(f"     {k}={v[:16]}..." if len(v) > 16 else f"     {k}={v}")
        else:
            print("   当前环境未检测到任何 API 相关变量。")
        print("   方式1：在脚本同目录创建 .env 文件，写入：OPENAI_API_KEY=your_key  OPENAI_BASE_URL=https://...")
        print("   方式2：export OPENAI_API_KEY=...  OPENAI_BASE_URL=...")
        print("   方式3：python report_writer.py --api-key your_key --base-url https://... --data ...")
        sys.exit(2)

    # 推断端点：若未指定 base_url，根据 key 类型选择默认端点
    if not base_url:
        is_anthropic_key = (
            (args.api_key and args.api_key.startswith("sk-ant-"))
            or os.environ.get("ANTHROPIC_API_KEY")
            or (os.environ.get("ANTHROPIC_AUTH_TOKEN") and not os.environ.get("OPENAI_API_KEY"))
        )
        base_url = "https://api.anthropic.com/v1" if is_anthropic_key else "https://api.openai.com/v1"

    _LLM_ENDPOINT = base_url
    _check_llm_host(_LLM_ENDPOINT)  # 域名白名单校验
    _LLM_API_KEY  = api_key
    _LLM_FORMAT = _infer_llm_format(api_key, base_url, credential_source)
    _print_credential_source("llm", credential_source or "default/env", _LLM_ENDPOINT, MODEL, _LLM_FORMAT)

    client = None  # 保留变量供现有函数签名兼容（call_claude 忽略此参数）

    print("[" + str(round(time.time()-t0, 1)) + "s] LLM 端点: " + _LLM_ENDPOINT
          + " | 格式: " + _LLM_FORMAT + " | 模型: " + MODEL)

    # ── 2. 加载数据 ───────────────────────────────────────────────────────────
    print("[" + str(round(time.time()-t0, 1)) + "s] 加载 JSON 数据...")
    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("__meta__", {})
    name = meta.get("name", "公司")
    ticker = meta.get("ticker", "")

    # ── 公司名称兜底：当 stock_search 缺失时 name 可能为纯数字 ticker ──────────
    if name == ticker or (name and name.isdigit() and len(name) == 6):
        # 尝试从 company_info 提取
        _ci_raw = data.get("company_info", {})
        _ci = _ci_raw.get("data", {}) if isinstance(_ci_raw, dict) else {}
        if isinstance(_ci, list):
            _ci = _ci[0] if _ci else {}
        _fallback = (_ci.get("secShortName") or _ci.get("CHNName") or
                     _ci.get("companyName") or _ci.get("name") or "")
        if not _fallback:
            # 尝试从 main_comp 提取（getFdmtMoStdItem 返回数组，取第一条 secShortName）
            _mc_raw = data.get("main_comp", {})
            if isinstance(_mc_raw, dict):
                _mc_list = _mc_raw.get("data", [])
                if isinstance(_mc_list, list) and _mc_list:
                    _fallback = _mc_list[0].get("secShortName") or ""
        if not _fallback:
            # 尝试从 mgmt_discussion 提取
            _md_raw = data.get("mgmt_discussion", {})
            if isinstance(_md_raw, dict):
                _fallback = _md_raw.get("secShortName") or _md_raw.get("name") or ""
        if _fallback:
            name = _fallback

    # 提取股票简称（secShortName）供标题使用；若无则截断全称末尾"股份有限公司"等
    _mc_raw2 = data.get("main_comp", {})
    _mc_list2 = _mc_raw2.get("data", []) if isinstance(_mc_raw2, dict) else []
    _short_name = (_mc_list2[0].get("secShortName", "") if isinstance(_mc_list2, list) and _mc_list2 else "")
    if not _short_name:
        # 从全称推断简称：去掉"股份有限公司"/"集团股份有限公司"等后缀
        _short_name = (name.replace("股份有限公司", "").replace("集团股份有限公司", "")
                       .replace("有限公司", "").replace("集团有限公司", "").strip()) or name
    meta["short_name"] = _short_name

    print(f"[{time.time()-t0:.1f}s] 公司: {name}（{ticker}）| 简称: {_short_name}")

    # ── 3. 提取结构化数据 ──────────────────────────────────────────────────────
    print(f"[{time.time()-t0:.1f}s] 提取结构化数据...")
    fin         = extract_financial(data)
    mc          = extract_maincomp(data)
    mc_region   = extract_main_comp_region(data)
    forecasts   = extract_consensus(data)
    actual_con  = extract_actual_consensus(data)
    orgs        = extract_profit_forecast(data)
    valuation   = extract_valuation(data)
    reports     = extract_reports_summary(data)
    meetings    = extract_meetings_summary(data)
    surveys     = extract_surveys_detail(data)
    anns        = data.get("announcements", [])
    company_info_raw = data.get("company_info", {})
    ci_data = company_info_raw.get("data", {}) if isinstance(company_info_raw, dict) else {}
    # data 可能是 dict（单条记录）或 list（多条）
    if isinstance(ci_data, list):
        company_info = ci_data[0] if ci_data else {}
    else:
        company_info = ci_data if ci_data else {}
    ref_map     = build_ref_map(data)
    charts      = data.get("charts", {})

    # key_data 传递给所有 LLM 生成函数
    key_data = {
        "name":              name,
        "ticker":            ticker,
        "fin":               fin,
        "mc":                mc,
        "mc_region":         mc_region,
        "consensus_forecasts": forecasts,
        "actual_consensus":  actual_con,
        "profit_forecast_orgs": orgs,
        "valuation":         valuation,
        "reports":           reports,
        "meetings":          meetings,
        "surveys":           surveys,
        "announcements":     anns,
        "company_info":      company_info,
        "ref_map":           ref_map,
        "charts":            charts,
        "peer_materials":    data.get("peer_materials") or [],  # getMaterialsV2 同业素材
        "peer_validated":    data.get("peer_validated") or [],  # stock_search验证的当前官方简称
        "_raw_data":         data,   # 供 _compact_mgmt 等函数提取 mgmt_discussion 等字段
    }

    # ── 4. 并行生成所有章节 ────────────────────────────────────────────────────
    print(f"[{time.time()-t0:.1f}s] 并行生成报告章节（LLM + 表格）...")

    # 纯 Python 表格任务（无 LLM，快速完成）
    _table_tasks = {
        "financial_table": (gen_financial_table, (fin, "", name)),
        "maincomp_table":  (gen_maincomp_table,  (mc,)),
        "consensus_table": (gen_consensus_table, (forecasts, actual_con, fin)),
        "forecast_table":  (gen_forecast_table,  (orgs,)),
    }
    def _is_timeout(val):
        return (isinstance(val, str) and "[生成失败:" in val
                and any(kw in val.lower() for kw in ("timed out", "timeout", "time out", "超时")))

    sections = {}

    # 表格任务一次并行完成
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {k: ex.submit(fn, *args) for k, (fn, args) in _table_tasks.items()}
        for k, fut in futs.items():
            sections[k] = fut.result()

    # 将 Python 表格结果注入 key_data，供同章 LLM 节引用（避免章内数字重复）
    # 4.2 三级降级链：
    #   ① fetch_data.py: getFdmtMoStdItem classifCD=2（按产品）→ ② classifCD=1（按行业）
    #   ③ 两者均无数据（银行/保险/券商等金融股常见）→ 从 mgmt_discussion 提取营收构成
    if not sections.get("maincomp_table", "").strip():
        fallback = gen_maincomp_fallback(key_data)
        if fallback:
            sections["maincomp_table"] = fallback
    key_data["maincomp_table_ctx"]  = sections.get("maincomp_table", "")
    key_data["financial_table_ctx"] = sections.get("financial_table", "")
    key_data["consensus_table_ctx"] = sections.get("consensus_table", "")
    key_data["forecast_table_ctx"]  = sections.get("forecast_table", "")

    # peer_table 先单独生成，结果注入 key_data 供 gen_section8（8.1行业格局）引用
    print(f"[{time.time()-t0:.1f}s] 生成同业比较表格（供8.1行业格局引用）...")
    sections["peer_table"] = gen_peer_table(client, key_data)
    key_data["peer_table_ctx"] = sections["peer_table"]

    # LLM 叙述章节（可能超时，支持降并发重试）
    # 第4章合并为 s4，避免 4.1/4.5 内容重复；6/9章已通过 key_data 注入表格上下文
    _llm_tasks = {
        "s123":        (gen_sections_1_2_3,     (client, key_data)),
        "s4":          (gen_section4,           (client, key_data)),
        "s4_deep":     (gen_section4_deep,      (client, key_data)),
        "s5":          (gen_section5,           (client, key_data)),
        "s6_health":   (gen_section6_health,    (client, key_data)),
        "s7":          (gen_section7,           (client, key_data)),
        "s8_industry": (gen_section8,           (client, key_data)),
        "s9_valuation":(gen_section9_valuation, (client, key_data)),
        "s10":         (gen_section10,          (client, key_data)),
    }

    # LLM 章节：先用 12 并发
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {k: ex.submit(fn, *args) for k, (fn, args) in _llm_tasks.items()}
        for k, fut in futs.items():
            sections[k] = fut.result()

    # 超时章节降并发至 5 重试
    _timed_out = {k: _llm_tasks[k] for k in _llm_tasks if _is_timeout(sections.get(k, ""))}
    if _timed_out:
        print(f"[{time.time()-t0:.1f}s] ⚠️ {len(_timed_out)} 个章节超时，降并发至 5 重试: {list(_timed_out)}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futs = {k: ex.submit(fn, *args) for k, (fn, args) in _timed_out.items()}
            for k, fut in futs.items():
                sections[k] = fut.result()

    # s123 合并生成结果 unpack → s1 / s2 / s3
    if isinstance(sections.get("s123"), dict):
        sections.update(sections.pop("s123"))
    elif "s123" in sections:
        sections["s1"] = sections.pop("s123")

    # s4 合并生成结果 unpack → s4_profit_model / s4_survey_qa
    if isinstance(sections.get("s4"), dict):
        sections.update(sections.pop("s4"))
    elif "s4" in sections:
        sections["s4_profit_model"] = sections.pop("s4")
        sections.setdefault("s4_survey_qa", "")
        sections.setdefault("s2", "")
        sections.setdefault("s3", "")

    # s4_deep 拆分 → s4_deep_analysis (4.3) / s4_deep_advantage (4.4)
    s4_deep_text = sections.get("s4_deep", "")
    if s4_deep_text and not isinstance(s4_deep_text, dict):
        parts_43 = re.split(r'###\s*4\.4[^\n]*\n?', s4_deep_text, maxsplit=1)
        parts_43_clean = re.split(r'###\s*4\.3[^\n]*\n?', parts_43[0], maxsplit=1)
        s43 = parts_43_clean[1].strip() if len(parts_43_clean) > 1 else parts_43[0].strip()
        s44 = parts_43[1].strip() if len(parts_43) > 1 else ""
        sections["s4_deep_analysis"] = s43
        sections["s4_deep_advantage"] = s44

    sections["valuation"]        = valuation
    sections["charts"]           = charts
    sections["fin_latest_label"] = fin.get("latest", {}).get("label", "") if fin.get("latest") else ""

    print(f"[{time.time()-t0:.1f}s] 所有章节生成完毕")

    # 生成标题一句话结论（基于 s1/s2 提炼核心投资判断）
    _s1_preview = sections.get("s1", "")[:400]
    _s2_preview = sections.get("s2", "")[:400]
    _tc_prompt = (
        f"为{name}（{ticker}）研究报告生成标题副标题：一句话投资结论，要求：\n"
        "1. 字数控制在20字左右，可超但不超过25字，必须在语义完整处自然结尾，禁止句子中断\n"
        "2. 直接给出结论，无前缀、无引号、无标点符号结尾\n"
        "3. 有明确观点导向，体现最核心驱动力或最重要投资判断\n"
        "4. 示例风格：直销占比跃升，分红回购支撑估值修复\n\n"
        f"近况摘要：{_s1_preview}\n\n"
        f"核心逻辑：{_s2_preview}\n\n"
        "一句话结论（直接输出，语义完整）："
    )
    _title_conclusion = call_claude(client, _tc_prompt, max_tokens=80).strip()
    # 清理可能出现的引号、前缀
    for _pfx in ("一句话结论：", "结论：", "：", ":", "\u201c", "\u201d", '"', "'"):
        _title_conclusion = _title_conclusion.lstrip(_pfx)
    _title_conclusion = _title_conclusion.strip().rstrip("。").strip()
    sections["title_conclusion"] = _title_conclusion
    print(f"[{time.time()-t0:.1f}s] 标题结论: {_title_conclusion}")

    # ── 5. 组装并写文件 ────────────────────────────────────────────────────────
    print(f"[{time.time()-t0:.1f}s] 组装报告...")
    md_content = assemble_report(meta, sections, ref_map)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[{time.time()-t0:.1f}s] ✅ MD 文件已保存：{args.output}")

    # ── 6. 生成 Word ────────────────────────────────────────────────────────────
    if args.docx:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        docx_script = os.path.join(script_dir, "markdown_to_docx.py")
        subprocess.run(
            [sys.executable, "-X", "utf8", docx_script, args.output, args.docx],
            check=True
        )
        print(f"[{time.time()-t0:.1f}s] ✅ Word 文件已保存：{args.docx}")

    total = time.time() - t0
    print(f"\n🎉 报告生成完成！总耗时：{total:.1f}秒（{total/60:.1f}分钟）")


if __name__ == "__main__":
    main()
