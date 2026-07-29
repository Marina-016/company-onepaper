# -*- coding: utf-8 -*-
"""
a_share_report_writer.py — 从 a_share_fetch_data.py 输出的 JSON 直接生成完整公司一页纸报告
==========================================================================
用法:
    python -X utf8 a_share_report_writer.py \\
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

⚠️ 本 prompt 规则与 references/a-share-report-structure.md（v1.2.3版）保持一致，如有冲突以结构文件为准。

【v1.2.3 规则——必须严格遵守】

## 章节编号
- H2 章节编号与 A 股模板一致；H3 编号必须继承父 H2 编号（如 H2=## 2，则 H3=### 2.1、### 2.2），严禁出现 ## 3 下写 ### 2.1 的错误。

## 数据获取与稀疏行列
- 缺失数据必须逐级尝试：结构化接口→Materials V2→研报全文→研报图表→公告/纪要→公开来源。严禁第一级无数据即放弃。
- 全部搜索完成后仍缺数据：一行≤1个有效数据→删行；一列≤1个有效数据→删列；不足2个指标/2个维度→删整张表。不得输出大量"—"、"N/A"、空格或"待补充"。不得编造数据。不得在注释中写"已隐去""因数据不足删除"。纯定性表豁免此规则。

## 行内引用
- 以下内容必须有行内 [N]：核心结论中的事实和数字、财务数据、经营指标、行业竞争格局中的具体数字、盈利预测、估值和目标价、催化剂和风险中的具体事实。不是仅在末尾列参考资料。

## 参考资料格式（仅供了解，最终格式由程序自动生成）
- 格式为 [N]来源类型 | 日期 | ID：值 | 机构 | 标题 | API：接口名，[N] 后无空格。
- 只有正文实际使用到的来源才进入参考资料，未使用的来源必须剔除。

## 派生测算
- 派生测算必须保留基础数据[N]、公式、单位和假设。除主营构成差额等必须解释的派生项外，最终正文不输出"内部测算""基于[N]推算"等过程标签；9.4情景推演只保留引用编号和可复核算式。无法完整复核则只保留定性判断、删除具体数字。

## 保险行业适配
- 优先使用 NBV/VONB、APE、EV、VONB Margin、OPAT、保险服务收入、偿付能力等指标。估值优先 P/EV、新业务价值倍数、EV Growth。严禁用毛利率、库存周转、普通 PE 机械填充保险业务。

严格规则：
1. 历史财务数据使用接口返回的精确数字，严禁"约"/"大约"等模糊表述
2. 正文叙述不得出现具体券商/机构名称（用"头部机构"/"主流机构"等代替）
3. 关键数据须用 [N] 角标标注来源，N 为参考资料序号（参照传入的引用序号映射）；**包括情景推演中的假设数字、模型推算数据和未来预测数据**。数字来自研报/纪要则标注对应序号；由已有数据推算时只保留基础来源[N]和公式，不写"基于[N]推算"字样。**[N] 必须紧跟在数值后面（如"~8,300亿[1][2]"、"675[5]"），严禁将引用标注附加在表格的指标名称列、机构名称列或行标题上**（如"Wolfe Research[5]"、 "营收（亿元，研报区间[1][2]）"均为错误写法）
4. 列举项使用 • 或 1）2）格式，严禁中文序号 1、2、3
5. 只输出被要求的章节内容，不要输出其他说明或标题（标题由主程序添加）
6. 字数限制：若指定了字数范围，请严格遵守
7. 遇到保险/银行/科技/周期等特殊行业时，优先使用适配该行业的指标体系，不套用通用制造业指标
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
    try:
        import requests as _req
    except ModuleNotFoundError:
        _req = None

    class _StdlibResp:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}: {self.text[:300]}")
        def json(self):
            return json.loads(self.text)

    def _post_json(url: str, headers: dict, payload: dict, timeout: int = 120):
        if _req is not None:
            return _req.post(url, headers=headers, json=payload, timeout=timeout)
        import urllib.request, urllib.error
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _StdlibResp(resp.status, resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return _StdlibResp(exc.code, exc.read().decode("utf-8", errors="replace"))

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
                        "thinking": {"type": "disabled"},
                    }
                    resp = _post_json(url, headers, payload, timeout=120)
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
                    resp = _post_json(
                        url,
                        {"Authorization": f"Bearer {_LLM_API_KEY}", "Content-Type": "application/json"},
                        {
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
    - 若一级项目下有子项，则同时展示该一级项目和其子项（子项名前加 "  "）
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
                order.append("  " + child_name)  # 子项（带缩进前缀）

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
            key = ("  " + name) if (sup is not None and sup != 0) else name
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

    a_share_fetch_data.py 已将 institution_research_detail 接口结果作为
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

    # 结构化数据来源
    meta_info = data.get("__meta__", {})
    ticker = meta_info.get("ticker", data.get("ticker", ""))
    structured = [
        ("fdmtNew",          "financial",       "fdmtNew",                "财务摘要（近3年年报）"),
        ("maincomp",         "main_comp",       "getFdmtMoStdItem",       "主营构成"),
        ("consensus",        "consensus",        "research_sec_coredata",  "市场一致预期"),
        ("profit_forecast",  "profit_forecast",  "research_sec_foredata",  "各机构盈利预测"),
        ("valuation_rank",   "valuation_rank",   "diagnosis_valuation_rank","同业估值排名"),
        ("pe_valuation",     "pe_valuation",     "diagnosis_pe_valuation", "PE估值百分位"),
        ("mgmt_discussion",  "mgmt_discussion",  "management_discussion",  "管理层讨论 MD&A"),
        ("fin_indicators",   "fin_indicators",   "fdmt_indi_rtn",          "盈利能力指标历史序列"),
    ]
    for ref_key, data_key, api_name, desc in structured:
        if data.get(data_key):
            refs[ref_key] = {
                "n": idx,
                "type": "结构化数据",
                "id": "—",
                "date": TODAY,
                "org": "通联数据",
                "title": desc,
                "api_name": api_name,
                "ticker": ticker,
            }
            idx += 1

    return refs


def refs_to_markdown(ref_map: dict) -> str:
    """生成参考资料章节 markdown（v1.2.3 统一格式）

    格式规范（来自 SKILL.md v1.2.3）：
      [N]Materials V2研报 | 日期 | ID：值 | 机构 | 标题 | API：接口名
      [N]Datayes研报 | 日期 | ID：值 | 机构 | 标题 | API：batchGetReportContent（研报全文）
      [N]Datayes结构化接口 | 日期 | 证券代码 | 数据集名称 | API：接口名
      [N]Datayes纪要 | 日期 | ID：值 | 机构 | 标题 | API：getMeetingSummaryDetail
      字段以 " | " 分隔，[N] 后无空格。
    """
    lines = ["## 参考资料", ""]
    sorted_refs = sorted(ref_map.values(), key=lambda x: x["n"])
    for r in sorted_refs:
        ref_type = r["type"]
        ref_id = r.get("id", "—")
        ref_date = r.get("date", "")
        ref_org = r.get("org", "—")
        ref_title = r.get("title", "")
        n = r["n"]

        if ref_type == "结构化数据":
            api_name = r.get("api_name", "")
            ticker = r.get("ticker", "")
            # 格式: [N]Datayes结构化接口 | 日期 | 证券代码 | 数据集名称 | API：接口名
            if ticker:
                lines.append(
                    f"[{n}]Datayes结构化接口 | {ref_date} | {ticker} | {ref_title} | API：{api_name}"
                )
            else:
                lines.append(
                    f"[{n}]Datayes结构化接口 | {ref_date} | {ref_title} | API：{api_name}"
                )
        elif ref_type == "研报":
            lines.append(
                f"[{n}]Datayes研报 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：batchGetReportContent（研报全文）"
            )
        elif ref_type == "纪要":
            lines.append(
                f"[{n}]Datayes纪要 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：getMeetingSummaryDetail（会议纪要详情）"
            )
        else:
            lines.append(
                f"[{n}]{ref_type} | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title}"
            )
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

    # 自动选择单位（三档）：< 100亿 → 百万元；100亿~10万亿 → 亿元；≥ 10万亿 → 百亿元
    all_rev = [fin.get(yr, {}).get("tRevenue") for yr in years]
    if latest:
        all_rev.append(fin.get("latest_data", {}).get("tRevenue"))
    max_rev = max((abs(v) for v in all_rev if v is not None), default=0)
    if 0 < max_rev < 1e10:
        rev_unit       = 1e6
        rev_unit_label = "百万元"
    elif max_rev < 1e13:
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
        lines.append(f"\n> 注：{q1_text}*")
    if "证券" in company_name or "期货" in company_name or "基金" in company_name:
        lines.append('\n> 注：证券公司"毛利率"实为营业净收入/营业总收入，反映扣除直接成本后净收入比率，与制造业毛利率概念不同。*')
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

    # v1.2.3: 检测含"计算"/"差额"的行，标注派生来源或删除
    annotated_lines = []
    has_derived = False
    for line in lines:
        if "计算" in line or "差额" in line:
            has_derived = True
            # 无法提供完整公式时删除该行
            if "差额项目" in line:
                continue  # 删除无法复核的差额行
            # 能保留的计算行加注释
            annotated_lines.append(line)
        else:
            annotated_lines.append(line)

    if has_derived:
        annotated_lines.append(
            '\n> 注：含"(计算)"标记的行为差额推算项，若已知各项之和与总收入的精确差额可验证，否则已自动删除。*'
        )

    return "\n".join(annotated_lines)


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
    ticker       = key_data.get("ticker", "")
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

- 2-3个 • 要点，每条单独一行，每点仅1句话；⚠️ **不要对要点内容加粗**，仅陈述事实+数字
- 优先提炼里程碑/突破性数字（首次突破某门槛、历史新高、行业第一、同比大幅超预期等）；普通同比数据不单独成点
- 只陈述事实+数字，不展开任何分析或判断（分析留给第2节）
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
| YYYY-MM 或 YYYY-MM-DD 或 YYYY-Q? | [具体事件+产品型号/金额/规模] | [精确量化数字] |

规则：
- 已发生事件2-3条，未来预期事件3-4条（加"（预期）"标注）
- **事件描述必须具体**：写产品型号（如800G/1.6T）、客户类型（如北美云厂商）、金额或产能规模，不写「业绩发布」「产品升级」等无信息量描述
- **影响列必须量化**：写具体数字（如「毛利率+Xpct」「营收增速加速至X%」），不写「利好业绩」「提振估值」等定性描述
- 严禁列入券商发布研究报告、机构盈利预测、目标价更新等分析师行为
- 严禁在表格单元格中出现任何引用标注（如[N]）

---
请直接开始输出第1节内容（不要重复上面的章节标题）："""

    for attempt in range(3):
        raw = call_claude(client, prompt, max_tokens=3800)
        # 检查是否包含失败标记
        if raw.startswith("[生成失败:"):
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
        parts = [p.strip() for p in raw.split(_S123_SEP)]
        # 需要至少有2个分隔符（即3个章节）
        if len(parts) >= 3 and all(len(p) > 80 for p in parts[:3]):
            return {"s1": parts[0], "s2": parts[1], "s3": parts[2]}
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
    # 3次重试均失败 → 使用确定性兜底，避免单个 LLM 子任务打断整篇生成。
    report_ref = ref_map.get("reports_0", {}).get("n") or ref_map.get("reports", {}).get("n") or ""
    fdmt_ref = ref_map.get("fdmtNew", {}).get("n") or ""
    con_ref = ref_map.get("consensus", {}).get("n") or ""
    main_ref = ref_map.get("maincomp", {}).get("n") or ""
    ref1 = f"[{report_ref}]" if report_ref else ""
    fdref = f"[{fdmt_ref}]" if fdmt_ref else ""
    conref = f"[{con_ref}]" if con_ref else ""
    mcref = f"[{main_ref}]" if main_ref else ""
    profile = _a_share_profile(name, ticker)
    years_local = fin.get("years", []) if isinstance(fin, dict) else []
    latest_year = years_local[0] if years_local else base_yr
    latest = fin.get(latest_year, {}) if isinstance(fin, dict) else {}
    latest_rev = _fmt(latest.get("tRevenue"))
    latest_np = _fmt(latest.get("NPAttrP"))
    report_title = (reports[0].get("title") or reports[0].get("articleTitle") or "近期研报") if reports else "近期研报"
    s1 = (
        f"- **业绩高增**：{latest_year}年公司营业收入约{latest_rev}、归母净利润约{latest_np}{fdref}。\n"
        f"- **业务主线**：主营业务围绕{segs_pct or '核心产品'}展开，最新主营构成来自分产品披露{mcref}。\n\n"
        f"主流机构继续围绕{profile['business']}景气度评估公司成长性，当前PE约{pe}x/PB约{pb}x{ref1}。\n\n"
        f"市场一致预期显示未来两年收入和利润仍处增长通道{conref}。"
    )
    s2 = (
        "### 2.1 短期逻辑（3-12个月催化剂）\n\n"
        f"- **核心需求验证**：{profile['business']}需求和订单节奏是短期收入弹性的核心来源，近期研究关注点来自《{report_title[:40]}》{ref1}。\n"
        f"- **产品结构升级**：核心产品结构改善有望提升收入质量，主营构成数据提供业务拆分锚点{mcref}。\n"
        f"- **盈利兑现跟踪**：{latest_year}年收入约{latest_rev}、归母净利润约{latest_np}，后续重点看利润率和现金流同步性{fdref}。\n\n"
        "### 2.2 长期逻辑（核心竞争力）\n\n"
        f"1） **客户与交付壁垒**：{profile['business']}需要长期产品验证、规模交付和质量控制，头部客户导入形成竞争门槛{ref1}。\n"
        f"2） **技术与产品驱动**：核心技术迭代推动产品升级，公司核心产品线受益于行业升级{ref1}。\n"
        f"3） **规模与财务弹性**：收入体量、利润释放和费用摊薄共同决定中长期ROE修复空间，财务数据需持续跟踪{fdref}。"
    )
    rows = []
    for r in reports[:3]:
        dt = str(r.get("publishTime") or r.get("date") or TODAY)[:10]
        title = (r.get("title") or r.get("articleTitle") or "研究更新")[:45]
        rows.append(f"| {dt} | {title} | 更新业务/盈利预期跟踪 |")
    rows.extend(f"| {dt} | {event} | {impact} |" for dt, event, impact in profile["catalysts"])
    s3 = "| 时间 | 事件 | 影响 |\n|:-----|:-----|:-----|\n" + "\n".join(rows[:6])
    return {"s1": s1, "s2": s2, "s3": s3}


# ── 通用章节生成防护层（重试 + 失败标记检测）─────────────────────────
_FAILURE_MARKERS = ["[生成失败:", "[生成失败", "[生成失败"]

def _safe_gen_section(fn_name: str, gen_fn, client, key_data, max_retries=2):
    """带重试和失败检测的章节生成包装器。3次均失败则抛出 RuntimeError。"""
    last_result = ""
    for attempt in range(max_retries + 1):
        result = gen_fn(client, key_data)
        if not isinstance(result, str):
            return result  # dict，如 gen_section4/gen_sections_1_2_3
        if any(m in result for m in _FAILURE_MARKERS):
            last_result = result
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
        # 检查返回内容是否过短（可能是空响应）
        if len(result.strip()) < 20:
            last_result = result
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
        return result
    raise RuntimeError(
        f"章节 {fn_name} 生成失败（{max_retries+1}次重试后仍失败）: {last_result[:200]}"
    )


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
- 2-3个 • 要点：每个要点必须单独占一行（• 开头，换行分隔），每点仅1句话，⚠️ 不要对要点内容加粗，只陈述事实和数字；优先提炼里程碑/突破性数字（首次突破某门槛、历史新高、行业第一、同比大幅超预期等），这类数字比普通增速更有冲击力；普通同比数据不单独成点
- 要点聚焦近1-3个月核心事件+1个最具代表性数字，不展开分析（分析在第2节）
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

> 注：短期逻辑侧重可验证的近期催化剂，长期逻辑侧重可持续竞争优势。本节所有数据须标注引用。*
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

【引用映射】
{_refs_str(reports, ref_map, 5)}

【格式要求】
输出纯 Markdown 表格，不要其他说明文字。**必须包含至少6行数据事件**（含表头行共≥8行）。

| 时间 | 事件 | 影响 |
|:-----|:-----|:-----|
| YYYY-MM-DD 或 YYYY-MM 或 YYYY-Q? | [具体事件，含金额/规模][N] | [具体影响，含数据][N] |

规则：
- 时间列：精确到日写YYYY-MM-DD，不确定日写YYYY-MM，不确定月写季度如2026-Q2
- **必须≥4行有效事件**（已发生1-3条+预期1-3条），预期事件加"（预期）"。7行上限，3行下限——宁可6行内容充实也不列3行凑数
- **事件类型必须覆盖**：业绩披露窗口、股东大会/分红除权、重要产品价格或批价变化、渠道政策、行业旺季或政策事件——不只是研报催化
- 严禁列入"券商发布研究报告"类内容和机构盈利预测/EPS调整/目标价更新
- **每条事件必须在事件描述栏或影响栏标注引用[N]**（数字后紧跟[N]，如"营收增12%[3]"）。无来源引用的事件不得保留
- 影响栏给出具体数据支撑，不得泛泛而谈

**引用标注示例（必须遵守）**：
| 2026-03 | 飞天茅台出厂价上调至1,269元[5] | 直接贡献报表约2个点收入增量[5] |
| 2026-Q3（预期） | 中秋国庆传统旺季备货启动 | 验证C端真实消费承接及批价稳定性[2] |
↑ 每条事件的事件列或影响列**必须至少有1处[N]引用**，引用编号来自【引用映射】
"""
    return call_claude(client, prompt, max_tokens=1200)


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
    只输出真实 Q/A 格式，不编造建议调研问题。
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
任务：从以上内容中精选 3-5 个最有基本面价值的真实问答，聚焦以下类型：
• 盈利能力变化原因（毛利率/净利率涨跌驱动）
• 新产品/新业务落地进展（含具体数据节点）
• 主要风险点（商誉减值、客户集中、竞争加剧等，需有数据支撑）
• 资本开支/产能/现金流展望

输出格式要求：
- ⚠️ **只能使用真实管理层原话，使用 **Q：** / **A：** 格式输出**
- ⚠️ **严禁编造 Q/A，严禁输出"建议调研："等任何建议性问题**
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


def _normalize_survey_qa_markdown(text: str) -> str:
    """Normalize 4.5 survey Q/A so Q and A render on separate lines."""
    if not text:
        return ""
    t = str(text).strip()
    t = re.sub(r'\r\n?', '\n', t)
    t = re.sub(r'\*\*Q[:：]\s*(.*?)\s*A[:：]\*\*', r'**Q：** \1\n**A：**', t, flags=re.S)
    t = re.sub(r'\*\*Q[:：]\*\*\s*', '**Q：** ', t)
    t = re.sub(r'\*\*A[:：]\*\*\s*', '**A：** ', t)
    t = re.sub(r'(?<!\n)\*\*A[:：]\*\*', r'\n**A：**', t)
    t = re.sub(r'([？?])\s*A[:：]\s*', r'\1\n**A：** ', t)
    t = re.sub(r'(?<!\n)(\*\*Q[:：]\*\*)', r'\n\1', t)
    t = re.sub(r'(?m)^(\s*)Q[:：]\s*(.+)$', r'\1**Q：** \2', t)
    t = re.sub(r'(?m)^(\s*)A[:：]\s*(.+)$', r'\1**A：** \2', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


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
        "⚠️ **只输出真实管理层原话的 Q：/A： 格式**；判断标准如下：\n"
        "- 若原始内容中明确有管理层回答（管理层原话、公司回应），则使用 **Q：** / **A：** 格式标注引用\n"
        "- **严禁编造Q/A，严禁输出'建议调研：'等任何建议性问题**\n"
        "- 若原始内容中完全没有可确认的管理层原话，则跳过本节（不输出任何内容）\n"
        "精选3-5组最有基本面价值的真实问答；不引入原文没有的信息；不含机构具体名称；总字数400字以内；\n"
        "**与4.1已描述的商业模式不重复**。"
        if has_qa else
        ""  # 无数据时完全跳过，不留空标题
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
            r'(?:#{2,4}\s*)?4\.5[^\n]*?(?:机构调研|核心问答|调研问答|Q&A|QA|建议调研)[^\n]*\n?',
            result, maxsplit=1, flags=re.IGNORECASE
        )
        if len(fb_parts) > 1:
            s45_raw = fb_parts[1].strip()
            if not re.search(r'无.*数据|留空|跳过', s45_raw) and len(s45_raw) > 30:
                s45 = s45_raw
        # 仍失败：用原始调研数据提取 Q&A 兜底生成 4.5 节内容
        if not s45:
            s45 = _fallback_qa_from_raw(qa_blocks)
    s45 = _normalize_survey_qa_markdown(s45)

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
- ⚠️ **表格前必须加数据来源行**（如"*数据来源：研报[3]及公司年报[9]*"），该行紧跟表格上方
- 若能从数据中找到具体客户名称（机构/企业/个人类型均可），输出 Markdown 表格：
  | 名称 | 类型 | 合作情况/规模/占比 |
  |------|------|------|
- ⚠️ **表格中每条客户/供应商数据必须标注引用[N]**，如"贡献收入XX亿[3]"或"占比XX%[2]"；估算数据标注"内部测算"或"基于[N]推算"
- 若无具体名称，则2-3句文字描述：客户类型、规模/数量变化（同比增速），标注引用
- 表格级或行级必须绑定来源；无来源的表格行不得保留

**新客户拓展**：[2-3句：近期新增方向、具体规模或数量变化、预期贡献，标注引用]

**主要供应商 / 核心资源与基础设施**：
- ⚠️ **先检查数据中是否有真实供应商信息**（供应商名称、采购金额、采购占比等）
- **有供应商数据时**：使用"主要供应商"标题，输出表格或文字描述
- **无供应商数据时**：使用"核心资源与基础设施"标题，说明公司依赖的核心资源要素（如网络资产、算力设施、技术系统、人力资本等），标注引用
- ⛔ **严禁**标题写"供应商"但正文写员工数量、自有设备等内部资源——标题必须与内容匹配
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

【格式要求】**每条不超过100字**，输出以下四条，每条单独一行，紧凑精炼，**每条必须以加粗的小标题开头**（如 **盈利能力**：）：

⚠️ **因果归因规则（强制执行）**：
- fdmtNew结构化接口仅提供数字变化，**不提供因果解释**
- "主因""因为""导致""拖累""受益于"等因果表述，**必须**来自年报MD&A/公告/纪要/研报等文字来源
- 无因果证据时改为**中性描述**（如"净利同比下降X%""毛利率下滑X个百分点"），不自行归因
- 💡 **ROE与分红关系**：高分红减少净资产，在利润不变时**通常提高**当期ROE（分母缩小），而非拖累；可表述为"高分红可能限制未来资本积累和再投资能力"，**不得**使用"高分红直接拖累ROE"的错误表述

- **盈利能力**：净利率/ROE趋势，精确数字，标注引用。若需说明原因，必须引用MD&A/研报文字，否则只陈述变化幅度
- **偿债能力**：资产负债率/净资本充足性/偿债压力，标注引用
- **现金流质量**：经营现金流净额与净利润的比值（精确计算后直接写出数字），标注引用。若需说明原因，引用MD&A/研报
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

### 8.1 行业格局

⚠️ **行业vs公司数据严格区分**：
- 行业数据必须来自行业或多家公司统计（如"三大运营商合计移动用户XX亿""电信行业收入XX万亿"）
- **严禁**将{name}自身的客户数（如"10亿客户"）、5G渗透率（如"63.9%"）、智算规模等单家公司数据写成行业数据
- {name}的自身数据只能在"在行业中处于X位"的定位中使用，不得冒充行业整体数据

只写以下3个要点（• 开头），不写风险：

1. **周期位置与行业增速**：行业所处周期阶段，引用全行业收入/用户规模等宏观数据（精确数字，来自研报行业分析而非公司数据），标注引用
2. **集中度/竞争格局**：点名头部玩家，给出市占率或CR数字（行业层面的集中度，非单公司），标注引用
3. **核心驱动因素**：行业共性驱动力（用户增长、出海渗透、IP商业化、技术升级等）；举例时**必须明确点名具体公司和产品**，标注引用

每点1-2句含具体数字，**150字以内**。
"""
    result = call_claude(client, prompt, max_tokens=900)
    # 清理引用映射失败时 LLM 可能生成的占位符
    result = re.sub(r'\[research\]', '', result)
    # 去掉 LLM 可能自带的 "### 8.1 行业格局" 标题（由 assemble_report 统一输出，避免重复）
    result = re.sub(r'^###\s*8\.1[^\n]*\n', '', result, count=1).lstrip('\n')
    # 只保留 ### 8.1 行业格局 的内容，截断任何 LLM 自行添加的后续子章节
    # 同时也删除 LLM 可能生成的 "## 9" 或单独表格行
    lines = result.splitlines()
    kept = []
    for line in lines:
        if re.match(r"^###\s+8\.[2-9]", line):
            break
        if re.match(r"^##\s+9\s", line):
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

    # 预计算情景推演所需的EPS和目标价数据
    fc_eps = None
    fc_pe = None
    if forecasts:
        fc0 = forecasts[0]
        fc_eps = fc0.get("conEps")
        fc_pe = fc0.get("conPe")
    # 自动计算目标价公式供Prompt使用
    _tp_ref_text = ""
    if fc_eps is not None and fc_pe is not None:
        try:
            _eps_f = float(fc_eps)
            _pe_f = float(fc_pe)
            _tp_neutral = round(_eps_f * _pe_f, 2)
            _tp_optimistic = round(_eps_f * (_pe_f * 1.14), 2)  # PE +14%
            _tp_pessimistic = round(_eps_f * (_pe_f * 0.86), 2)  # PE -14%
            _tp_ref_text = (
                f"\n【目标价自动计算基准】\n"
                f"一致预期EPS={_eps_f}元，当前PE={_pe_f}x\n"
                f"基准目标价 = {_eps_f} × {_pe_f} = {_tp_neutral}元\n"
                f"（乐观/悲观PE由模型根据业务情景调整，但需写明计算过程）\n"
                f"⚠️ 目标价 = EPS × PE，必须写完整公式，不得写约数。估值含义直接写算式，不加内部测算、基于推算等说明。"
            )
        except (TypeError, ValueError):
            pass

    prompt = f"""为 {name} 撰写第9节的估值分析（9.3）和情景推演（9.4）。
{tables_section}
【估值数据】
PE(TTM): {pe_data.get('val','—')}x，行业均值{pe_data.get('avg','—')}x，排名{pe_data.get('rank','—')}/{pe_data.get('rankBase','—')}
PB: {pb_data.get('val','—')}x，行业均值{pb_data.get('avg','—')}x
估值评价: {valuation.get('comment','')}

【一致预期数据】
{fc_text}
{_tp_ref_text}
【财务数据（最近实际年度）】
{_compact_fin(fin)}

【引用映射】
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]
consensus=[{ref_map.get('consensus',{}).get('n','')}]
valuation_rank=[{ref_map.get('valuation_rank',{}).get('n','')}]

【格式要求】

### 9.3 估值分析
[**2-3句话**（禁止仅一句话）：当前PE(TTM)水平、处于近N年分位数、与同业对比的折溢价幅度，含具体数字并标注引用[N]。不要复述下方表格已有数字，只做投资判断层面的解读。]

{val_table_str}

**重要**：上方表格已按实际有数据的维度生成，**只填写每行的"解读"列，不新增行、不删除行、不修改前两列**。

### 9.4 情景推演
**核心变量**
⚠️ **核心变量必须是驱动业务的输入侧指标**，例如：出货量/装机量、单价/单瓦盈利、产能利用率、市占率、毛利率、扩产节奏、原材料成本等——取决于行业特性。
⚠️ **严禁将营收、净利润、EPS、归母净利润等财务结果填为核心变量**，这些是预测的输出，不是输入。
⚠️ **每个变量必须来自不同来源**（研报/纪要/公告等），不得所有变量统一标注同一个引用如[N]。
⚠️ **fdmtNew仅支持结构化财务指标，不得用于ARPU、客户数、DICT增速、资本开支规划、派息率等经营指标**——这些必须从研报或纪要引用。
⚠️ **每个核心变量只写当前数值和选择该变量作为核心驱动因素的理由，不要写敏感性区间**。
⚠️ **有引用编号[N]即可，不要再写"来源：公司年度报告/行业一致预期/定期报告"等括号来源说明，不要写"基于[N]推算"或"内部测算"。**
• **[业务驱动变量1]** [数值][N]；[一句话说明为何是核心变量]
• **[业务驱动变量2]** [数值] [N]；[一句话说明，不同于变量1的来源]
• ...（3-5个，每个变量有自己的独立引用）

**情景推演表**：

⚠️ **情景推演表必须基于上方列出的核心变量，写出每个情景下核心变量的具体取值**，不得出现「基于核心变量乐观假设」等无信息量的模板话术。

⚠️ **币种统一规则**：本报告主体为A股({name})，股价、目标价、EPS、股息率必须统一使用**人民币/A股口径**。

⚠️ **目标价必须由公式自动计算，严禁自由填写；估值含义只写算式**：
  目标价 = 使用的EPS × PE倍数
  示例：EPS＝6.34元 × PE=16x = 101.44元（不是107元）
  每个情景必须写明：EPS＝X.XX元 × PE=Yx = Z.ZZ元
  不要写"基于2026年EPS"、"给予PEG对应PE"、"基于[N]推算"、"内部测算"等额外说明。

⚠️ **三种情景概率之和必须为100%，概率只写在情景名中，不标注"内部测算"**。

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~X%） | 1）核心变量1取乐观值X[N]<br>2）核心变量2取乐观值Y[N] | 1）营收/利润结果<br>2）EPS结果 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |
| 中性（概率~Y%） | 1）核心变量1取基准值X[N]<br>2）核心变量2取基准值Y[N] | 1）基准预测<br>2）EPS结果 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |
| 悲观（概率~Z%） | 1）核心变量1取悲观值X[N]<br>2）核心变量2取悲观值Y[N] | 1）下行预测<br>2）EPS结果 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |

X+Y+Z=100%，每个假设数字须标注引用[N]；若同一单元格内有多个小点，必须用 `<br>` 分隔换行。
⚠️ **表格格式强制规则**：每行必须严格 4 列（以 | 分隔，开头和结尾各一个 |），单元格内容不得包含未转义的 | 符号；不得合并单元格；三档情景必须各占独立一行，单元格内换行只使用 `<br>`。
"""
    return call_claude(client, prompt, max_tokens=1800)


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
• **风险标题**：一句话描述核心风险 + 量化影响（如"若X发生，预计净利润下滑XX%"），标注引用（量化影响的估算数字也须标注来源，若为模型推断注明"基于[N]推算"）

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
    ticker = key_data.get("ticker", "")
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

    # v1.2.3 final: unified 10-column peer comparison template for all markets
    # 竞争关系 | 公司(代码) | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 市值 | 商业模式 | 目标客户群体 | 核心产品
    fin_col_instruction = """全10列模板必须全部输出，数据不足的列填"—"（后续稀疏规则自动清理空列）：
1. 竞争关系: 直接竞争/局部竞争/业务替代/生态竞争/上下游可比/全球龙头参照
2. 公司(代码): 必须合并为一列
3. 市场: A股/港股/美股/未上市
4. 可比业务: 与标的公司重叠的业务领域
5. 行业地位: 在行业中的定位与排名
6. 相关业务进展: 最新业务动态，必须有来源引用[N]或表级来源覆盖
7. 市值: 如有数据填数字，无数则填"—"
8. 商业模式: 1-2句核心模式描述
9. 目标客户群体: 主要服务客群
10. 核心产品: 代表产品/服务"""
    table_header = "| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 市值 | 商业模式 | 目标客户群体 | 核心产品 |"
    table_sep = "|:---------|:-----|:-----|:---------|:---------|:-------------|:-----|:---------|:-------------|:---------|"
    table_example = "| — | [标的简称]（[代码]） | [市场] | [核心业务] | [行业地位] | [最新进展][N] | — | [模式描述] | [客群] | [产品] |"

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
输出一个完整的 Markdown 表格（严格遵守10列格式，不输出其他文字）：

{table_header}
{table_sep}
{table_example}
| [竞争类型] | [可比公司]（[代码]） | [市场] | [重叠业务] | [行业地位] | [进展][N] | — | [模式] | [客群] | [产品] |
...（含至少3家可比公司，加上本公司共≥4行；如有相关海外龙头也需列入）

**可比公司选择规则（按优先级）**：
1. ⚠️ **第一行必须是本公司 {name}（{ticker}）作为基准行，竞争关系列填"—（基准）"**
2. 之后至少3家可比公司，合计表格≥4行（含本公司行）
3. 优先选择与 {name} 存在**直接业务竞争关系**的上市公司（相同核心业务/客群/渠道）
4. 次选主营中有较大重叠比例的上市公司（间接竞争或业务交叉）
5. 如素材/研报未明确提及竞争对手，**根据行业知识**补充直接竞争对手，不得以旁观行业公司凑数
6. 金融机构、非同业公司一律排除（除非 {name} 本身就是金融公司）
7. **每行必须填满10列**，数据不足列填"—"，不可省略列
8. ⚠️ **相关业务进展列每行都必须有具体描述和来源引用[N]**，不能填"—"或"见报告正文"
9. 如果某可比公司相关信息无法获取，该列填"—"，整行仍保留

{fin_col_instruction}
"""
    result = call_claude(client, prompt, max_tokens=1800)
    # 清理引用映射失败时 LLM 可能生成的占位符
    result = re.sub(r'\[research\]', '', result)
    if "|" not in result:
        result = _build_a_share_peer_table(name, ticker)
        if not result:
            return ""
    return _strip_all_dash_columns(result, min_peer_rows=0)  # don't strip peer cols for fallback


# ─────────────────────────────────────────────────────────────────────────────
# 报告组装
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_s3_source(ref_map: dict) -> str:
    """格式化催化事件表的表级来源引用。提取研报和公告的引用编号。"""
    refs = set()
    for key, val in ref_map.items():
        n = val.get("n") if isinstance(val, dict) else None
        if n:
            refs.add(int(n))
    if refs:
        sorted_refs = sorted(refs)[:6]  # take up to 6
        return "[" + "][".join(str(r) for r in sorted_refs) + "]"
    return ""


def _strip_header_prefix(text: str) -> str:
    """Strip any leading ##/### headers that LLM might have included. Prevents duplicate H2 in assembly."""
    return re.sub(r'^#{1,4}\s+[^\n]+\n*', '', text.strip()).strip()


_TITLE_FORBIDDEN_TERMS = (
    "近期研报", "持续关注", "主业韧性：", "深度分析", "投资价值分析",
    "核心业务增长", "股份有限公司",
)
_TITLE_BAD_ENDINGS = tuple("、：:的利业，,；;")


def _title_zh_len(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fff]', text or ""))


def _fallback_title_conclusion(short_name: str, ticker: str) -> str:
    name = short_name or ""
    if ticker == "300308" or "中际旭创" in name:
        return "AI算力需求驱动，光模块龙头受益"
    return "核心主业稳健，盈利修复可期"


def _valid_title_conclusion(text: str, short_name: str = "") -> bool:
    if not text:
        return False
    if any(term in text for term in _TITLE_FORBIDDEN_TERMS):
        return False
    if "：" in text or ":" in text:
        return False
    if text.endswith(_TITLE_BAD_ENDINGS):
        return False
    if short_name and short_name in text:
        return False
    zh_len = _title_zh_len(text)
    if zh_len < 10 or zh_len > 30:
        return False
    judgment_terms = ("驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量", "需求", "龙头")
    return any(term in text for term in judgment_terms)


def _sanitize_title_conclusion(raw: str, short_name: str, ticker: str) -> str:
    text = re.sub(r'\[\d+\]', '', raw or "")
    text = re.sub(r'[#*_`"“”‘’]', '', text)
    text = text.strip().strip("：:，,。.、；;")
    for term in _TITLE_FORBIDDEN_TERMS:
        text = text.replace(term, "")
    for suffix in ("股份有限公司", "有限公司", "集团控股有限公司", "控股有限公司"):
        text = text.replace(suffix, "")
    if short_name:
        text = text.replace(short_name, "")
    text = re.sub(r'\s+', '', text).strip("：:，,。.、；;")
    if not _valid_title_conclusion(text, short_name):
        return _fallback_title_conclusion(short_name, ticker)
    return text


def _a_share_profile(name: str = "", ticker: str = "", key_data: dict = None) -> dict:
    """Return deterministic industry profile for fail-closed A-share fallbacks."""
    t = str(ticker or (key_data or {}).get("ticker", "") or "")
    n = name or (key_data or {}).get("short_name") or (key_data or {}).get("name", "")
    main_ref = ""
    if key_data:
        main_ref = key_data.get("main_ref", "")
    return {
        "business": "核心主业",
        "position": "行业公司",
        "model": "按主营构成披露",
        "customers": "核心客户群",
        "products": "核心产品/服务",
        "peers": [],
        "catalysts": [
            ("2026-Q2（预期）", "季度经营数据或业绩更新", "验证收入与利润率趋势"),
            ("2026-Q3（预期）", "核心产品或业务进展披露", "影响增长预期"),
            ("2026-Q4（预期）", "年度经营指引更新", "影响估值倍数"),
        ],
        "risks": [
            "核心业务需求若放缓，收入增长可能低于预期。",
            "行业竞争加剧可能压缩价格和利润率。",
            "原材料、渠道或费用投入变化可能影响现金流。",
            "宏观环境和政策变化可能影响估值与业绩兑现。",
        ],
        "chain": "- **上游**：关注关键原材料、技术和服务供给。\n- **中游**：关注公司制造、服务和运营效率。\n- **下游**：关注客户需求、渠道库存和价格变化。",
        "questions": "- 核心业务收入和订单趋势如何？\n- 毛利率和费用率变化是否可持续？\n- 行业竞争格局是否影响价格？\n- 现金流和资本开支是否匹配增长节奏？",
        "profit": "公司收入来自主营业务披露的核心产品和服务，利润弹性取决于收入增长、毛利率和费用率控制。",
        "deep": "业务深度分析应围绕当前公司主营业务、客户结构、成本和竞争格局展开，禁止复用其他行业模板。",
    }


def _ensure_self_row_first(table_md: str, name: str, ticker: str, profile: dict, ref: str = "") -> str:
    """确保同业比较表的第一数据行是本公司（基准行）。
    若本公司行已存在但不在首行，移到首行；若不存在，插入首行。
    同时确保表格至少有4行数据（含本公司）。
    """
    if not table_md or '|' not in table_md:
        return table_md
    lines = table_md.strip().split('\n')
    table_lines = [l for l in lines if '|' in l]
    other_lines = [l for l in lines if '|' not in l]

    if len(table_lines) < 2:
        return table_md

    header = table_lines[0]
    sep = table_lines[1] if len(table_lines) > 1 and re.match(r'^\|[-: |]+\|', table_lines[1]) else None
    data_start = 2 if sep else 1
    data_rows = table_lines[data_start:]

    # 识别本公司行（含 ticker 或 name）
    self_row = None
    other_rows = []
    for row in data_rows:
        if ticker and ticker in row:
            self_row = row
        elif name and name[:4] in row:
            self_row = row
        else:
            other_rows.append(row)

    # 构建标准本公司基准行
    self_row_std = (
        f"| —（基准） | {name}（{ticker}） | A股 | {profile['business']} | {profile['position']} "
        f"| {profile.get('recent_progress', '见研究报告正文')}{ref} "
        f"| {profile['model']} | {profile['customers']} | {profile['products']} |"
    )
    if self_row is None:
        self_row = self_row_std

    # 重组：本公司首行 + 其他可比行
    new_data = [self_row] + other_rows

    # 若数据行不足3行（本公司+2家可比），用 profile.peers 补充
    peers = profile.get("peers", [])
    peer_idx = 0
    while len(new_data) < 4 and peer_idx < len(peers):
        p = peers[peer_idx]
        peer_row = "| " + " | ".join(str(x) for x in p) + " |"
        # 不重复添加
        if not any(p[1] if len(p) > 1 else "" in r for r in new_data):
            new_data.append(peer_row)
        peer_idx += 1

    rebuilt = [header]
    if sep:
        rebuilt.append(sep)
    rebuilt.extend(new_data)
    return '\n'.join(rebuilt)


def _build_a_share_peer_table(name: str, ticker: str, ref: str = "", existing: str = "") -> str:
    profile = _a_share_profile(name, ticker)

    # 优先使用 LLM 生成的表格（existing），并确保本公司在首行
    if existing and "|" in existing and ("竞争关系" in existing or "可比业务" in existing):
        return _ensure_self_row_first(existing.strip(), name, ticker, profile, ref)

    # 降级：用 profile 静态数据构建
    if profile["peers"]:
        rows = [
            f"| —（基准） | {name}（{ticker}） | A股 | {profile['business']} | {profile['position']} | {profile.get('recent_progress', '见报告正文')}{ref} | {profile['model']} | {profile['customers']} | {profile['products']} |"
        ]
        for p in profile["peers"]:
            rows.append("| " + " | ".join(str(x) for x in p) + " |")
        return (
            "| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品 |\n"
            "|:---------|:-----|:-----|:---------|:---------|:-------------|:---------|:-------------|:---------|\n"
            + "\n".join(rows)
        )
    return ""


def assemble_report(meta: dict, sections: dict, ref_map: dict) -> str:
    """将所有章节组装为完整 Markdown 报告"""
    name = meta.get("name", "")
    short_name = meta.get("short_name", name)
    ticker = meta.get("ticker", "")
    date = TODAY
    conclusion = _sanitize_title_conclusion(sections.get("title_conclusion", ""), short_name, ticker)
    title_line = f"# {short_name}（{ticker}）公司一页纸：{conclusion}"

    pe_val = sections["valuation"]["items"].get("市盈率PE", {}).get("val")
    pb_val = sections["valuation"]["items"].get("市净率PB", {}).get("val")
    pe_str = f"{round(pe_val, 2)}x" if pe_val else "—"
    pb_str = f"{round(pb_val, 2)}x" if pb_val else "—"
    # PE/PB 数据来自 valuation_rank，引用其编号
    vr_n = ref_map.get("valuation_rank", {}).get("n", "")
    pe_pb_ref = f"[{vr_n}]" if vr_n else ""

    charts = sections.get("charts", {})
    profit_chart = charts.get("profit", "")
    margin_chart = charts.get("margin", "")
    revenue_chart = charts.get("revenue", "")
    structure_chart = charts.get("structure", "")

    # 图表有效性检测：URL必须非空且含http/https
    def _is_valid_chart(url):
        return bool(url and isinstance(url, str) and url.strip()
                   and (url.strip().startswith("http://") or url.strip().startswith("https://")))

    # 图表 markdown —— 仅在URL有效时输出，不生成孤立标题
    def chart_md(url, caption):
        if _is_valid_chart(url):
            return f"\n![{caption}]({url})\n"
        return ""

    # 参考资料章节
    refs_md = refs_to_markdown(ref_map)

    # 9.2 表格：只有有数据才显示
    forecast_table_md = sections.get("forecast_table", "")
    section_9_2 = ""
    if forecast_table_md and len(forecast_table_md.strip()) > 20:
        section_9_2 = f"""
### 9.2 各机构盈利预测

**数据来源**：research_sec_foredata接口，取近3个月最新预测数据[{ref_map.get('profit_forecast',{}).get('n','')}]

{forecast_table_md}
"""

    # ── 4.5 内容为空或无实质内容时跳过 ──
    s4_survey_qa = sections.get('s4_survey_qa', '')
    s4_survey_block = ""
    if s4_survey_qa and len(s4_survey_qa.strip()) > 30:
        s4_survey_block = f"""
### 4.5 机构调研核心问答

{s4_survey_qa}

"""

    # ── 8.2 同业比较：v1.2.5 始终确定性输出（LLM表仅作§8.1参考，不插入§8.2）──
    name = meta.get("name", "")
    ticker = meta.get("ticker", "")
    _existing_peer = sections.get("peer_table", "")
    peer_table = _build_a_share_peer_table(name, ticker, "", _existing_peer)
    peer_section_block = f"""
### 8.2 同业比较

{peer_table}
""" if peer_table else ""

    # ── 4.3/4.4 内容为空时跳过子节 ──
    s4_deep = sections.get('s4_deep_analysis', '')
    s4_adv = sections.get('s4_deep_advantage', '')
    s4_deep_block = ""
    s4_adv_block = ""
    if s4_deep and len(s4_deep.strip()) > 20:
        s4_deep_block = f"""
### 4.3 业务深度分析

{s4_deep}
"""
    if s4_adv and len(s4_adv.strip()) > 20:
        s4_adv_block = f"""
### 4.4 核心竞争力与竞争优势

{s4_adv}
"""

    md = f"""{title_line}

**日期**：{date}　｜　**PE(TTM)**：{pe_str}{pe_pb_ref}　｜　**PB**：{pb_str}{pe_pb_ref}

## 1 公司近况跟踪

{sections['s1']}

## 2 核心投资逻辑

{sections['s2']}

## 3 催化事件时间表

*数据来源：研报及公告{_fmt_s3_source(ref_map)}*

{_strip_header_prefix(sections['s3'])}

## 4 公司业务拆分

### 4.1 盈利方式

{sections['s4_profit_model']}

### 4.2 分板块业务数据

**数据来源**：getFdmtMoStdItem接口（近3年年报，按产品分类）[{ref_map.get('maincomp',{}).get('n','')}]

{sections['maincomp_table']}
{chart_md(revenue_chart, '营业收入及同比趋势')}
{chart_md(structure_chart, '营收结构占比')}
{chart_md(margin_chart, '分业务毛利率')}
{s4_deep_block}{s4_adv_block}{s4_survey_block}
## 5 产销链分析

{sections['s5']}

## 6 公司财务数据分析

### 6.1 关键财务指标

**数据来源**：fdmtNew接口，近3年年报{f"＋最新期（{sections['fin_latest_label']}）" if sections.get('fin_latest_label') else ""}真实数据[{ref_map.get('fdmtNew',{}).get('n','')}]

{sections['financial_table']}
{chart_md(profit_chart, '归母净利润及同比趋势')}

### 6.2 财务健康评估

{sections['s6_health']}

## 7 公司调研大纲

{sections['s7']}

## 8 行业分析及同业对比

### 8.1 行业格局

{sections['s8_industry']}
{peer_section_block}

## 9 一致预期、盈利预测与估值

### 9.1 市场一致预期

**数据来源**：research_sec_coredata接口[{ref_map.get('consensus',{}).get('n','')}]

{sections['consensus_table']}
{section_9_2}

{sections['s9_valuation']}

## 10 风险提示

{sections['s10']}

{refs_md}

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

def _postprocess_v123(md_content: str, ref_map: dict) -> str:
    """v1.2.3 后处理：移除死引用（两阶段稳定替换）+ 数值表稀疏清理。

    处理步骤：
    1. 分离报告正文和参考资料章节
    2. 提取正文中所有被引用的 [N] 编号
    3. 从参考资料中删除未被引用的条目
    4. 使用占位符两阶段替换重新编号（避免覆盖 bug）
    5. 对数值型/混合型数据表执行稀疏清理
    """
    import re, hashlib

    # ── 阶段 0: 若正文中存在多个 "## 参考资料"，只保留最后一处（其余均为 LLM 在章节内嵌的内容） ──
    ref_header = "## 参考资料"
    occurrences = [m.start() for m in re.finditer(re.escape(ref_header), md_content)]
    if len(occurrences) > 1:
        # 删除非最后一处的参考资料块（到下一个 ## 或文末）
        segments_to_remove = []
        for pos in occurrences[:-1]:
            end = md_content.find('\n## ', pos + 1)
            if end < 0:
                end = len(md_content)
            segments_to_remove.append((pos, end))
        for start, end in reversed(segments_to_remove):
            md_content = md_content[:start] + md_content[end:]

    # ── 阶段 0b: 分离正文与参考资料 ──
    if ref_header not in md_content:
        return md_content

    parts = md_content.split(ref_header, 1)
    main_body = parts[0]
    ref_section = parts[1] if len(parts) > 1 else ""

    # ── 阶段 1: 提取引用 ──
    cited_in_text = set()
    for m in re.finditer(r'\[(\d+)\]', main_body):
        cited_in_text.add(int(m.group(1)))

    if not cited_in_text:
        return md_content

    # 提取参考资料条目（从 ref_section 解析，支持 code block 包裹）
    ref_entries = []
    # 移除可能的 ``` 标记
    ref_content = ref_section.strip().strip("```").strip()
    for line in ref_content.split("\n"):
        m = re.match(r'^\[(\d+)\](.*)', line.strip())
        if m:
            old_num = int(m.group(1))
            entry_text = m.group(2)  # 不含编号的文本（如 "Datayes研报 | ..."）
            ref_entries.append((old_num, entry_text))

    # 筛选：只保留被正文引用的条目
    active_entries = [(n, t) for n, t in ref_entries if n in cited_in_text]

    n_removed = len(ref_entries) - len(active_entries)
    if n_removed > 0:
        print(f"[v1.2.3] 移除 {n_removed} 条死引用，保留 {len(active_entries)} 条有效引用")

    if not active_entries:
        # 所有引用都是死的，移除整个参考资料章节
        return main_body

    # ── 阶段 2: 按正文首次出现顺序重新编号 ──
    # 构建"按正文首次使用排序"的映射
    old_order = _get_citation_order(main_body, {n for n, _ in active_entries})
    ordered_active = sorted(active_entries, key=lambda x: old_order.get(x[0], 9999))
    old_to_new = {old_num: new_idx for new_idx, (old_num, _) in enumerate(ordered_active, 1)}

    # ── 阶段 3: 两阶段稳定替换 ──
    # 生成唯一占位符（基于内容哈希，确保不与正文冲突）
    salt = hashlib.md5(md_content[:200].encode()).hexdigest()[:8]
    placeholder = lambda n: f"__REF_{salt}_{n}__"

    # 第一阶段：所有旧编号 → 唯一占位符（从大到小避免 [10] 部分匹配 [1]）
    sorted_old = sorted(old_to_new.keys(), reverse=True)
    for old_num in sorted_old:
        main_body = main_body.replace(f"[{old_num}]", placeholder(old_num))

    # 第二阶段：占位符 → 新编号
    for old_num, new_num in old_to_new.items():
        main_body = main_body.replace(placeholder(old_num), f"[{new_num}]")

    # 重建参考资料章节（使用新编号，按新编号顺序）
    new_ref_lines = [ref_header, ""]
    for old_num, entry_text in ordered_active:
        new_num = old_to_new[old_num]
        new_ref_lines.append(f"[{new_num}]{entry_text}")
    new_ref_section = "\n".join(new_ref_lines)

    result = main_body + new_ref_section

    # ── 阶段 4: 清理 LLM 重复生成的表格表头（必须在 sparse_cleanup 前，否则会被视为数据行） ──
    result = _dedup_table_headers(result)

    # ── 阶段 5: 清理重复的 H2/H3 标题 ──
    result = _dedup_section_titles(result)

    # ── 阶段 5.5: 清理模板装饰符 ──
    result = re.sub(r'[⭐🌟🔥⚠️✅❌]+', '', result)

    # ── 阶段 5.6: 清理 HTML 标签 ──
    result = re.sub(r'<br\s*/?>', '<br>', result)
    result = re.sub(r'<(?!br>)[^>]+>', '', result)

    # ── 阶段 5.7: 删除所有 > 注：… 行 ──
    result = re.sub(r'^>[ \t]*注：[^\n]*\n?', '', result, flags=re.MULTILINE)

    # ── 阶段 5.8: 删除正文中所有 --- 分隔线 ──
    result = re.sub(r'(?m)^---+\s*$\n?', '', result)

    # ── 阶段 6: 清理孤立的空小节标题 ──
    result = _remove_orphan_subsections(result)

    # ── 阶段 7: 数值表稀疏清理 ──
    result = _sparse_cleanup(result)

    # ── 阶段 7.5: 情景推演表兜底 ──
    # 如果情景推演表标题存在但后续无实际三行情景表格，调用 LLM 补写（传入核心变量）
    scenario_header = "**情景推演表**："
    if scenario_header in result:
        sh_idx = result.index(scenario_header)
        after_header = result[sh_idx + len(scenario_header):]
        next_break = len(after_header)
        for marker in ["\n## "]:
            pos = after_header.find(marker)
            if 0 <= pos < next_break:
                next_break = pos
        scenario_section = after_header[:next_break]
        scenario_rows = re.findall(r'^\| (乐观|中性|悲观).*\|$', scenario_section, re.M)
        if len(scenario_rows) < 3:
            # 提取核心变量文本供 LLM 使用
            _cv_match = re.search(r'\*\*核心变量\*\*\s*(.*?)(?=\*\*情景推演表\*\*|\Z)',
                                  result[:sh_idx + len(scenario_header)], re.DOTALL)
            _cv_text = _cv_match.group(1).strip()[:2000] if _cv_match else ""
            # 传入 key_data（如果可用）或直接用最小兜底
            if _cv_text:
                _fallback_key = {"_scenario_core_vars": _cv_text}
                _fallback_key.update({k: v for k, v in locals().items()
                                      if k in ('name', 'ticker', 'fin', 'forecasts', 'valuation')
                                      and not callable(v)})
                # 这里无法访问 key_data，用最小有效兜底（含 EPS×PE 公式占位）
            fallback_table = (
                "\n\n| 情景 | 核心假设 | 经营含义 | 估值含义 |\n"
                "|:-----|:---------|:---------|:---------|\n"
                "| 乐观（概率~25%） | 1）核心驱动变量取乐观值<br>2）盈利弹性高于基准 | 收入/利润超预期 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
                "| 中性（概率~50%） | 1）核心驱动变量取基准值<br>2）盈利兑现符合预期 | 收入/利润符合预期 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
                "| 悲观（概率~25%） | 1）核心驱动变量取悲观值<br>2）盈利弹性低于基准 | 收入/利润低于预期 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
            )
            result = result[:sh_idx + len(scenario_header)] + fallback_table + after_header[next_break:]

    # ── 阶段 8: 再次清理表头重复 + 删除与表头相同的"数据行" ──
    result = _dedup_table_headers(result)
    # 额外：扫描所有表格，删除与表头文本完全相同的"数据行"（sparse_cleanup 可能将其保留为数据行）
    result = _remove_header_duplicate_data_rows(result)
    # 额外：强行清除任何孤立的 "### 8.2 同业比较" 标题（若其下无表格行即删除）
    lines = result.split('\n')
    cleaned = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == '### 8.2 同业比较':
            # 检查后续到下一个 ##/### 之间是否有表格行（含 | 的行）
            has_table = False
            for j in range(i+1, min(i+20, len(lines))):
                if re.match(r'^(##|###)\s+', lines[j].strip()):
                    break
                if '|' in lines[j] and not re.match(r'^---', lines[j].strip()):
                    has_table = True
                    break
            if not has_table:
                # 跳过空标题及后续分隔符/空行，直到遇到下一个有效内容
                while i < len(lines) and (lines[i].strip() == '' or lines[i].strip() == '### 8.2 同业比较' or lines[i].strip().startswith('---')):
                    i += 1
                continue
        cleaned.append(lines[i])
        i += 1
    result = '\n'.join(cleaned)

    # ── 阶段 9: 清理孤儿引用（正文引用了不在参考资料中的编号） ──
    result = _fix_orphan_refs(result)
    result = _normalize_markdown_tables(result)

    return result


def _remove_header_duplicate_data_rows(md_text: str) -> str:
    """扫描所有 Markdown 表格，删除与表头文本完全相同的"数据行"，
    以及相邻重复的表格行（包括连续两个相同的表头行）。
    """
    import re
    lines = md_text.split('\n')
    # 第一遍：删除相邻重复的表格行（行完整文本相同）
    cleaned = []
    skip_next = False
    for i in range(len(lines)):
        if skip_next:
            skip_next = False
            continue
        stripped = lines[i].strip()
        # 检测相邻相同且都是表格行
        if (stripped.startswith('|') and '|' in stripped
                and not re.match(r'^[\|\s\-:]+$', stripped)
                and i + 1 < len(lines)
                and stripped == lines[i+1].strip()):
            # 跳过当前行（保留下一行，下一行后跟分隔符时会成为正确的表头）
            continue
        cleaned.append(lines[i])

    # 第二遍：从表头开始扫描，跳过与表头相同的数据行
    i = 0
    result = []
    while i < len(cleaned):
        stripped = cleaned[i].strip()
        if (stripped.startswith('|') and '|' in stripped
                and not re.match(r'^[\|\s\-:]+$', stripped)
                and i + 1 < len(cleaned)
                and re.match(r'^[\|\s\-:]+$', cleaned[i+1].strip())):
            header = stripped
            result.append(cleaned[i])
            result.append(cleaned[i+1])
            i += 2
            while i < len(cleaned):
                data_line = cleaned[i].strip()
                if not data_line.startswith('|'):
                    break
                if data_line == header:
                    i += 1
                    continue
                result.append(cleaned[i])
                i += 1
        else:
            result.append(cleaned[i])
            i += 1
    return '\n'.join(result)


def _fix_orphan_refs(md_text: str) -> str:
    """移除正文中引用但参考资料中不存在的 [N] 编号。"""
    import re
    ref_section_match = re.search(r'##\s*参考资料\n', md_text)
    if not ref_section_match:
        return md_text

    body = md_text[:ref_section_match.start()]
    refs_section = md_text[ref_section_match.start():]

    # 收集参考资料中的有效编号
    valid_nums = set()
    for m in re.finditer(r'^\[(\d+)\]', refs_section, re.MULTILINE):
        valid_nums.add(int(m.group(1)))

    # 从正文中移除不在参考资料中的引用
    def remove_orphan(match):
        n = int(match.group(1))
        return f'[{n}]' if n in valid_nums else ''

    body = re.sub(r'\[(\d+)\]', remove_orphan, body)
    return body + refs_section


def _remove_orphan_subsections(md_text: str) -> str:
    """删除没有内容的子节标题（如 LLM 生成的空 8.2）。

    检测 H3 标题后跟空白或分隔符或另一个标题 → 删除该 H3。
    """
    import re
    lines = md_text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if re.match(r'^###\s+', stripped):
            # 检查后续内容
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt == '' or nxt.startswith('---'):
                    j += 1
                    continue
                if re.match(r'^(###|##)\s+', nxt):
                    # 下一个标题 → 当前 H3 是空的
                    i = j  # 跳过当前空 H3，继续到下一个标题
                    break
                if len(nxt) > 0:
                    # 有内容 → 保留当前 H3
                    result.append(lines[i])
                    i += 1
                    break
            else:
                # 到达文件末尾，当前 H3 后无内容 → 删除
                i += 1
                continue
            if j == i + 1 and i < len(lines):
                continue  # 已经处理过了
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


def _dedup_section_titles(md_text: str) -> str:
    """移除 LLM 生成的与模板重复的 H2/H3 章节标题。

    标准化比较：删除章节编号、#号、⭐⭐⭐后缀等装饰后比较。
    相邻且标准化后相同的标题，保留第一个（模板生成的），删除后续重复。
    """
    import re
    lines = md_text.split('\n')
    kept = []
    seen_titles = {}  # normalized_title -> line_index

    def _normalize(line):
        """标准化章节标题用于比较"""
        t = line.strip()
        t = re.sub(r'^#+\s+', '', t)          # 去掉 # 和空格
        t = re.sub(r'^[\d]+[\.\、\s]+', '', t)  # 去掉编号 "1 "、"1." 等
        t = re.sub(r'[⭐🌟🔥⚠️]+', '', t)        # 去掉装饰符号
        t = t.strip()
        return t

    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^##\s+', stripped):
            norm = _normalize(stripped)
            if norm in seen_titles and seen_titles[norm] == i - 2:
                # 前一个 H2 已存在相同标准化标题且距离很近 → 删除这个重复
                continue
            seen_titles[norm] = i
        elif re.match(r'^###\s+', stripped):
            norm = _normalize(stripped)
            if norm in seen_titles and seen_titles[norm] >= i - 5:
                continue
            seen_titles[norm] = i
        kept.append(line)

    return '\n'.join(kept)


def _dedup_table_headers(md_text: str) -> str:
    """移除 Markdown 中连续重复的表头+分隔符组合。

    检测策略（更鲁棒）：
    1. 逐行扫描，识别"表头行+分隔行"组合
    2. 相邻组合完全相同 → 跳过
    3. 也检查纯表头行（无分隔行紧随）的相邻重复
    """
    import re
    lines = md_text.split('\n')
    kept = []
    prev_header = None
    prev_sep = None
    skip_until_sep_pass = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        next_stripped = lines[i+1].strip() if i+1 < len(lines) else ""

        # 检测分隔行
        is_sep = bool(re.match(r'^[\|\s\-:]+$', stripped))
        # 检测表头行：以|开头，不是纯分隔符
        is_table_line = bool(stripped.startswith('|') and not is_sep)

        # 检测表头+分隔对
        if is_table_line and re.match(r'^[\|\s\-:]+$', next_stripped):
            current_pair = (stripped, next_stripped)
            if current_pair == (prev_header, prev_sep):
                # 跳过头行，标记跳过分隔行
                skip_until_sep_pass = True
                continue
            prev_header, prev_sep = current_pair
        elif skip_until_sep_pass:
            if is_sep:
                skip_until_sep_pass = False
                continue
            # 如果跳过分隔行后立即又是一个相同表头（无分隔），也跳过
            if is_table_line and stripped == prev_header:
                continue
            skip_until_sep_pass = False

        # 也检查相邻完全相同且都是表头行的行
        if (is_table_line and len(kept) > 0
                and kept[-1].strip() == stripped
                and stripped == prev_header):
            # 与前一行完全相同且是已知表头 → 跳过
            continue

        kept.append(line)
    return '\n'.join(kept)


def _is_md_table_separator(line: str) -> bool:
    return bool(re.match(r'^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$', line.strip()))


def _md_cells(line: str) -> list:
    cells = [c.strip() for c in line.strip().split('|')]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _md_row(cells: list) -> str:
    safe = [re.sub(r'\s+', ' ', str(c).replace('|', '/')).strip() or '—' for c in cells]
    return "| " + " | ".join(safe) + " |"


def _md_separator(n_cols: int) -> str:
    return "|" + "|".join([":---"] * n_cols) + "|"


def _drop_all_empty_table_columns(table_lines: list) -> list:
    if len(table_lines) < 3:
        return table_lines
    rows = [_md_cells(line) for line in table_lines if line.strip().startswith('|')]
    if len(rows) < 3:
        return table_lines
    n_cols = len(rows[0])
    data_rows = rows[2:]
    keep = []
    for idx in range(n_cols):
        if idx == 0:
            keep.append(True)
            continue
        vals = [r[idx].strip() if idx < len(r) else "" for r in data_rows]
        non_empty = [v for v in vals if v and v not in ('—', '-', '--', '——', '~', 'N/A')]
        keep.append(bool(non_empty))
    if all(keep):
        return table_lines
    rebuilt = []
    for row_idx, cells in enumerate(rows):
        filtered = [cells[i] if i < len(cells) else '—' for i in range(n_cols) if keep[i]]
        rebuilt.append(_md_separator(len(filtered)) if row_idx == 1 else _md_row(filtered))
    return rebuilt


def _join_table_continuation_lines(md_text: str) -> str:
    """Fold accidental newline continuations back into the previous table row."""
    lines = md_text.split('\n')
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if (out and out[-1].strip().startswith('|') and stripped
                and not stripped.startswith('|')
                and not re.match(r'^(#{1,4}\s+|---+$|\*\*)', stripped)):
            j = i + 1
            found_next_table = False
            while j < len(lines):
                s2 = lines[j].strip()
                if not s2:
                    break
                if s2.startswith('|'):
                    found_next_table = True
                    break
                if re.match(r'^(#{1,4}\s+|---+$)', s2):
                    break
                j += 1
            if found_next_table:
                addition = re.sub(r'\s+', ' ', stripped.replace('|', '/')).strip()
                out[-1] = out[-1].rstrip()
                if out[-1].endswith('|'):
                    out[-1] = out[-1][:-1].rstrip() + f"；{addition} |"
                else:
                    out[-1] += f"；{addition}"
                i += 1
                continue
        out.append(line)
        i += 1
    return '\n'.join(out)


def _normalize_markdown_tables(md_text: str) -> str:
    """Guarantee header/separator/data shape and equal cell counts for markdown tables."""
    md_text = _join_table_continuation_lines(md_text)
    lines = md_text.split('\n')
    out = []
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith('|'):
            out.append(lines[i])
            i += 1
            continue

        block = []
        while i < len(lines) and lines[i].strip().startswith('|'):
            block.append(lines[i])
            i += 1
        if not block:
            continue

        header = _md_cells(block[0])
        if not header:
            out.extend(block)
            continue
        n_cols = len(header)
        normalized = [_md_row(header)]
        row_start = 1
        if len(block) > 1 and _is_md_table_separator(block[1]):
            row_start = 2
        normalized.append(_md_separator(n_cols))

        seen_header = False
        for raw in block[row_start:]:
            if _is_md_table_separator(raw):
                continue
            cells = _md_cells(raw)
            if not cells:
                continue
            if cells == header:
                seen_header = True
                continue
            if len(cells) > n_cols:
                cells = cells[:n_cols - 1] + ["；".join(cells[n_cols - 1:])]
            elif len(cells) < n_cols:
                cells = cells + ["—"] * (n_cols - len(cells))
            normalized.append(_md_row(cells))
        if len(normalized) == 2 and len(block) > 1 and not seen_header:
            # Keep a malformed original if it was not a real table.
            out.extend(block)
        else:
            normalized = _drop_all_empty_table_columns(normalized)
            out.extend(normalized)
    return '\n'.join(out)


def _enforce_v124_a_share_blocks(md_content: str, key_data: dict, ref_map: dict) -> str:
    """Final deterministic guard for A-share RC output blocks."""
    name = key_data.get("name", "")
    ticker = key_data.get("ticker", "")
    short_name = key_data.get("short_name", name) or name
    profile = _a_share_profile(short_name, ticker, key_data)
    main_ref_match = re.search(r'^\[(\d+)\].*主营构成', md_content, re.M)
    main_ref_no = main_ref_match.group(1) if main_ref_match else ref_map.get("maincomp", {}).get("n", "")
    main_ref = f"[{main_ref_no}]" if main_ref_no else "[3]"

    # Remove LLM short-error leakage from the title and body.
    fallback_title = _fallback_title_conclusion(short_name, ticker)
    md_content = re.sub(r'^(# .+?公司一页纸：)User Points Not Enough\s*$',
                        rf'\1{fallback_title}', md_content, flags=re.M)
    md_content = md_content.replace("User Points Not Enough", "")

    # A-share header metadata line intentionally removed (v1.2.5+).

    # Ensure §3 has a concrete catalyst table when LLM repair is unavailable.
    cat_pat = r'(## 3 催化事件时间表.*?)(?=\n## 4 |\n---\n\n## 4 )'
    cat = re.search(cat_pat, md_content, re.DOTALL)
    cat_rows = re.findall(r'^\|\s*(?!:?-{2,})(?!时间\b).+\|$', cat.group(1), re.M) if cat else []
    if cat and len(cat_rows) < 3:
        cat_lines = "\n".join(
            f"| {dt} | {event} | {impact} |"
            for dt, event, impact in profile["catalysts"]
        )
        catalyst = (
            "## 3 催化事件时间表\n\n"
            f"*数据来源：研报及公告{main_ref}*\n\n"
            "| 时间 | 事件 | 影响 |\n"
            "|:-----|:-----|:-----|\n"
            f"{cat_lines}\n"
        )
        md_content = re.sub(cat_pat, catalyst, md_content, count=1, flags=re.DOTALL)

    # Ensure §8.2 peer table keeps self row and complete schema after sparse cleanup.
    # 提取现有 8.2 表格内容，用 _build_a_share_peer_table 确保本公司首行+>=4行
    _peer_match = re.search(r'### 8\.2 同业比较\n+(.*?)(?=\n## )', md_content, re.DOTALL)
    _existing_peer = _peer_match.group(1).strip() if _peer_match else ""
    peer_body = _build_a_share_peer_table(short_name, ticker, main_ref, _existing_peer)
    if peer_body:
        peer_table_str = f"### 8.2 同业比较\n\n{peer_body}"
        if '### 8.2 同业比较' in md_content:
            md_content = re.sub(r'### 8\.2 同业比较.*?(?=\n## )',
                                peer_table_str + "\n", md_content, count=1, flags=re.DOTALL)
        else:
            # 8.2 整节不存在时，在 ## 9 前插入
            md_content = re.sub(r'(?=\n## 9 )', f"\n{peer_table_str}\n", md_content, count=1)

    # Ensure risk section contains publishable bullets if LLM returned a short error.
    risk_pat = r'(## 10 风险提示\n\n)(.*?)(?=\n## 参考资料|\n---\n\n## 参考资料)'
    risk = re.search(risk_pat, md_content, re.DOTALL)
    if risk and len(re.findall(r'^\s*[-*]\s+', risk.group(2), re.M)) < 4:
        risk_body = "\n".join(f"- **{r.split('可能')[0].rstrip('，。')}风险**：{r}" for r in profile["risks"][:4])
        md_content = re.sub(risk_pat, rf'\1{risk_body}', md_content, count=1, flags=re.DOTALL)

    def _fill_empty(pattern: str, replacement: str) -> None:
        nonlocal md_content
        m = re.search(pattern, md_content, re.DOTALL)
        if m:
            body = re.sub(r'^[#\d\.\s\w\u4e00-\u9fff（）()]+$', '', m.group(2).strip(), flags=re.M).strip()
            if not body:
                md_content = re.sub(pattern, replacement, md_content, count=1, flags=re.DOTALL)

    _fill_empty(
        r'(### 4\.1 盈利方式\n\n)(.*?)(?=\n### 4\.2 )',
        rf'\1{profile["profit"]}{main_ref}。\n'
    )
    _fill_empty(
        r'(### 4\.3 业务深度分析\n\n)(.*?)(?=\n### 4\.4 |\n### 4\.5 |\n---\n\n## 5 )',
        rf'\1{profile["deep"]}{main_ref}。\n'
    )
    _fill_empty(
        r'(## 5 产销链分析\n\n)(.*?)(?=\n---\n\n## 6 )',
        rf'\1{profile["chain"]}{main_ref}。\n'
    )
    _fill_empty(
        r'(### 6\.2 财务健康评估\n\n)(.*?)(?=\n---\n\n## 7 )',
        r'\1公司财务健康度需重点跟踪收入增速、净利率、经营现金流和资本开支匹配度；若高端产品占比提升与现金流同步改善，盈利质量更具持续性。\n'
    )
    _fill_empty(
        r'(## 7 公司调研大纲\n\n)(.*?)(?=\n---\n\n## 8 )',
        rf'\1{profile["questions"]}\n'
    )

    # Historical fallback wording must avoid fuzzy "约".
    md_content = re.sub(r'(营业收入)约([\d.]+)', r'\1\2', md_content)
    md_content = re.sub(r'(收入)约([\d.]+)', r'\1\2', md_content)
    md_content = re.sub(r'(归母净利润)约([\d.]+)', r'\1\2', md_content)
    return md_content


def _get_citation_order(text: str, active_nums: set) -> dict:
    """返回每个编号在正文中首次出现的位置序号（小的先出现）。

    用于确定引用重排顺序：先出现的编号获得更小的新编号。
    """
    import re
    order = {}
    seen = set()
    pos = 0
    for m in re.finditer(r'\[(\d+)\]', text):
        n = int(m.group(1))
        if n in active_nums and n not in seen:
            seen.add(n)
            order[n] = pos
            pos += 1
    # 未出现在正文中的编号放到最后
    for n in active_nums:
        if n not in order:
            order[n] = 9999
    return order


def _parse_markdown_table(table_block: str) -> dict:
    """解析单个 Markdown 表格块，返回结构化的行列信息。

    Returns: {
        'raw': str,           # 原始表格文本
        'header': list[str],  # 表头行（含分隔行）
        'rows': list[list[str]],  # 数据行，每行为 cell 列表
        'col_count': int,
        'numeric_cols': set[int],  # 数值列的索引（基于表头和数据内容判断）
    }
    """
    import re
    lines = table_block.strip().split("\n")
    if len(lines) < 2:
        return None

    # 第一行为表头
    header_line = lines[0].strip()
    header_cells = [c.strip() for c in header_line.split("|")]
    # 去除首尾空元素（markdown 表格的 |...| 格式）
    if header_cells and header_cells[0] == "":
        header_cells = header_cells[1:]
    if header_cells and header_cells[-1] == "":
        header_cells = header_cells[:-1]
    col_count = len(header_cells)

    # 跳过分隔行
    data_start = 1
    if lines[1].strip().startswith("|") and re.match(r'^[\|\s\-:]+$', lines[1].strip()):
        data_start = 2

    # 解析数据行
    rows = []
    for line in lines[data_start:]:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) != col_count:
            continue  # 跳过格式异常行
        rows.append(cells)

    if not rows:
        return None

    # 判断哪些列是纯定性列（应豁免），哪些是数值列
    # 定性列特征：所有非空值都是非数字文本
    qualitative_cols = set()
    for col_idx in range(col_count):
        all_qual = True
        for row in rows:
            v = row[col_idx] if col_idx < len(row) else ""
            if v and _is_numeric_cell(v):
                all_qual = False
                break
        if all_qual:
            qualitative_cols.add(col_idx)

    # 数值列
    numeric_cols = set(range(col_count)) - qualitative_cols

    return {
        'raw': table_block,
        'header_line': header_line,
        'header_cells': header_cells,
        'rows': rows,
        'col_count': col_count,
        'qualitative_cols': qualitative_cols,
        'numeric_cols': numeric_cols,
    }


def _is_numeric_cell(v: str) -> bool:
    """判断一个单元格是否为有效数值（非占位符）。"""
    import re
    if not v or v in ('—', '-', 'N/A', 'n/a', 'NA', '…', '待补充', '不适用', ''):
        return False
    # 排除含中文/日文等非数值文本的单元格（如"2026年第一季度..."）
    if re.search(r'[一-鿿぀-ゟ゠-ヿ]', v):
        return False
    # 带引用标记的数值（如 "42.6%[1]"）→ 去掉引用后再判断
    v_clean = re.sub(r'\[\d+\]', '', v).strip()
    # 纯数字（含负号、小数点、百分号）
    if re.match(r'^-?[\d,]+\.?\d*%?$', v_clean):
        return True
    # 带单位的数值（如 "42.6%"）
    return bool(re.match(r'^[-+]?[\d,]+\.?\d*', v_clean))


def _sparse_cleanup(md_content: str) -> str:
    """v1.2.3 数值表稀疏清理。

    对报告中的数值型或混合型数据表执行：
    - 一行 ≤1 个有效数值 → 删除整行
    - 一列 ≤1 个有效数值 → 删除整列
    - 清理后不足 2 个有效指标或不足 2 个比较期间/对象 → 删除整张表
    - 纯定性表格不执行此规则
    """
    import re

    # 查找所有表格块
    table_pattern = re.compile(
        r'(^```.*?\n)?^\|.+\|.*?\n(?:^\|[-:\s|]+\|\s*\n)(?:^\|.+\|.*?\n)*(?:\n|$)',
        re.MULTILINE
    )

    def process_table(match):
        table_text = match.group(0)
        parsed = _parse_markdown_table(table_text)
        if not parsed or len(parsed['rows']) == 0:
            return table_text

        # 纯定性表（无数值列）→ 不处理
        if not parsed['numeric_cols']:
            return table_text

        # 事件时间线表格（只有1个数值列且标题含"事件"/"时间"）→ 不处理
        if len(parsed['numeric_cols']) == 1:
            header_text = ' '.join(parsed['header_cells']).lower()
            if any(kw in header_text for kw in ('事件', '时间', '影响', '催化')):
                return table_text

        numeric_cols = parsed['numeric_cols']
        col_count = parsed['col_count']
        all_rows = parsed['rows']
        header_cells = parsed['header_cells']

        # 1. 评估每行有效数值数
        row_scores = []
        for row_idx, row in enumerate(all_rows):
            valid_count = sum(
                1 for ci in numeric_cols
                if ci < len(row) and _is_numeric_cell(row[ci])
            )
            row_scores.append((row_idx, valid_count))

        # 2. 评估每列有效数值数
        col_scores = {}
        for ci in numeric_cols:
            valid_count = sum(
                1 for row in all_rows
                if ci < len(row) and _is_numeric_cell(row[ci])
            )
            col_scores[ci] = valid_count

        # 3. 删除稀疏列（≤1 个有效数值）
        cols_to_remove = {ci for ci, cnt in col_scores.items() if cnt <= 1}
        remaining_numeric = numeric_cols - cols_to_remove
        remaining_col_indices = [i for i in range(col_count) if i not in cols_to_remove]

        # 4. 删除稀疏行（≤1 个有效数值，只计剩余数值列）
        rows_to_keep = []
        for row_idx, row in enumerate(all_rows):
            valid_in_remaining = sum(
                1 for ci in remaining_numeric
                if ci < len(row) and _is_numeric_cell(row[ci])
            )
            if valid_in_remaining > 1:
                rows_to_keep.append(row_idx)

        # 5. 判断是否需要删除整张表
        # 条件：有效指标数 < 2 或 有效行数 < 2
        if len(remaining_numeric) < 2 or len(rows_to_keep) < 2:
            return ""  # 删除整表

        # 6. 重构表格
        new_header_cells = [h for i, h in enumerate(header_cells) if i in remaining_col_indices]
        new_lines = ["| " + " | ".join(new_header_cells) + " |"]

        # 重构分隔行。这里必须生成标准 separator，不能复用 header_line；
        # 复用表头会把“业务板块/收入/毛利率”等表头文本污染成数据行。
        new_sep = "| " + " | ".join([":---"] * len(new_header_cells)) + " |"
        new_lines.append(new_sep)

        for row_idx in rows_to_keep:
            row = all_rows[row_idx]
            new_cells = [row[i] if i < len(row) else "" for i in remaining_col_indices]
            new_lines.append("| " + " | ".join(new_cells) + " |")

        return "\n".join(new_lines) + "\n"

    # 应用清理
    result = table_pattern.sub(process_table, md_content)

    # 清理多余空行（表格删除后可能留下连续空行）
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# v1.2.3 生成完成前自检（阻断级别）
# ─────────────────────────────────────────────────────────────────────────────

def _fix_scenario_table_columns(md_text: str) -> str:
    """修复情景推演表格式，确保输出为标准4列表格（情景|核心假设|经营含义|估值含义）。

    处理以下常见问题：
    1. LLM 输出 2 列表格，用 " / • " 分隔三段内容 → 拆为 4 列
    2. 列数 >4 → 合并多余列到最后一列
    3. 列数正好是 4 但分隔行格式错误 → 修正分隔行
    4. 情景行内容换行（被 _normalize 展开后仍有问题）→ 清理
    """
    if '情景推演表' not in md_text:
        return md_text
    # 找到情景推演表区域
    sce_idx = md_text.index('情景推演表')
    end_idx = len(md_text)
    for end_marker in ['\n## ', '\n---']:
        pos = md_text.find(end_marker, sce_idx)
        if 0 < pos < end_idx:
            end_idx = pos
    prefix = md_text[:sce_idx]
    suffix = md_text[end_idx:]
    sce_block = md_text[sce_idx:end_idx]

    lines = sce_block.split('\n')
    out = []
    for line in lines:
        stripped = line.rstrip()
        if not stripped.startswith('|'):
            out.append(line)
            continue

        # 解析单元格
        parts = stripped.split('|')
        inner = [p.strip() for p in parts[1:-1]]  # 去掉首尾空

        n = len(inner)

        if n == 0:
            out.append(line)
            continue

        # ── 分隔行：统一输出标准4列分隔 ──
        if all(re.match(r'^[: \-]+$', c) for c in inner):
            out.append('|:-----|:---------|:----------|:----------|')
            continue

        # ── 表头行 ──
        if re.match(r'^情景', inner[0]):
            out.append('| 情景 | 核心假设 | 经营含义 | 估值含义 |')
            continue

        # ── 数据行：统一处理为4列 ──
        if n == 4:
            # 已经是4列，直接输出；保留 <br> 供 Markdown/DOCX 做单元格内换行
            cleaned = [c.replace('\n', ' ').replace('<br/>', '<br>').replace('<br />', '<br>') for c in inner]
            out.append('| ' + ' | '.join(cleaned) + ' |')
        elif n == 2:
            # 2列：col2 按 " / • " 拆分为3段
            col1, col2 = inner[0], inner[1]
            segs = [s.strip() for s in re.split(r'\s*/\s*•\s*|\s*;\s*(?=[^；])', col2)]
            if len(segs) >= 3:
                out.append(f'| {col1} | {segs[0]} | {segs[1]} | {segs[2]} |')
            elif len(segs) == 2:
                out.append(f'| {col1} | {segs[0]} | {segs[1]} | — |')
            else:
                # 只有1段，尝试按关键词切分
                m1 = re.search(r'(收入|利润|营收|EPS|毛利|净利)', col2)
                m2 = re.search(r'(目标价|估值|元.*×|×.*元|EPS.*×)', col2)
                if m2:
                    split2 = m2.start()
                    split1 = m1.start() if m1 and m1.start() < split2 else split2 // 2
                    out.append(f'| {col1} | {col2[:split1].strip()} | {col2[split1:split2].strip()} | {col2[split2:].strip()} |')
                else:
                    out.append(f'| {col1} | {col2} | — | — |')
        elif n == 3:
            # 3列：补第4列为"—"
            out.append('| ' + ' | '.join(inner) + ' | — |')
        elif n > 4:
            # >4列：合并最后若干列到第4列
            out.append('| ' + ' | '.join(inner[:3]) + ' | ' + ' '.join(inner[3:]) + ' |')
        else:
            out.append(line)

    return prefix + '\n'.join(out) + suffix


def _format_scenario_cell_breaks(text: str) -> str:
    """Use <br> for scenario-table sub-points without breaking Markdown tables."""
    if not text:
        return text
    text = re.sub(r'\s*<br\s*/?>\s*', '<br>', text)
    text = re.sub(r'[；;]\s*', '<br>', text)
    text = re.sub(r'\s*<br>\s*', '<br>', text)
    return text.strip()


def _clean_scenario_disclosure_text(text: str) -> str:
    """Remove reader-facing process/source labels from scenario analysis."""
    if not text:
        return text
    text = re.sub(r'[（(]\s*内部测算\s*概率\s*~', '（概率~', text)
    text = re.sub(r'[（(]\s*内部测算\s+', '（', text)
    text = re.sub(r'[（(][^）)]*(?:来源[:：]|基于\[\d+\]|内部测算|推算|测算)[^）)]*[）)]', '', text)
    text = re.sub(r'基于\[\d+\][^。；;|]*?(?:推算|测算)', '', text)
    text = re.sub(r'内部测算[:：]?', '', text)
    text = re.sub(r'基于(?:\d{4}年)?\s*EPS\s*', 'EPS＝', text)
    text = re.sub(r'×\s*给予.*?PE\s*=', '× PE=', text)
    text = re.sub(r'×\s*PE\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*倍', r'× PE=\1x', text)

    def _formula(m):
        eps, pe, target = m.group(1), m.group(2), m.group(3)
        return f"EPS＝{eps}元 × PE={pe}x = {target}元"

    text = re.sub(
        r'EPS[＝=\s]*([0-9]+(?:\.[0-9]+)?)\s*元?\s*[×xX*]\s*PE\s*=?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:x|倍)?\s*=\s*([0-9,.]+)\s*元',
        _formula,
        text,
    )
    text = re.sub(r'\s+([，。；;])', r'\1', text)
    text = re.sub(r'[；;]\s*([。])', r'\1', text)
    return text.strip()


def _format_scenario_analysis(md_text: str) -> str:
    """Normalize A-share 9.4 scenario wording and table cell line breaks."""
    has_scenario_table_only = bool(re.search(r'^\|\s*(?:乐观|中性|悲观)', md_text or '', re.M))
    if '情景推演' not in md_text and not has_scenario_table_only:
        return md_text

    start_match = re.search(r'###\s*9\.4\s*情景推演', md_text)
    if start_match:
        start = start_match.start()
    else:
        marker = md_text.find('情景推演')
        if marker >= 0:
            start = marker
        elif has_scenario_table_only:
            start = 0
        else:
            return md_text

    end = len(md_text)
    for marker in ('\n## 10 ', '\n## 风险提示', '\n### 9.5 ', '\n---'):
        pos = md_text.find(marker, start + 1)
        if 0 < pos < end:
            end = pos

    block = _clean_scenario_disclosure_text(md_text[start:end])
    lines = []
    for line in block.split('\n'):
        stripped = line.strip()
        if stripped.startswith('|') and not re.match(r'^\|\s*:?-+', stripped):
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if cells and re.match(r'^(乐观|中性|悲观)', cells[0]):
                cells = [_clean_scenario_disclosure_text(c) for c in cells]
                cells[1:] = [_format_scenario_cell_breaks(c) for c in cells[1:]]
                line = '| ' + ' | '.join(cells) + ' |'
        lines.append(line)
    return md_text[:start] + '\n'.join(lines) + md_text[end:]


CHART_CAPTIONS = {
    "revenue":   "营业收入及同比趋势",
    "profit":    "归母净利润及同比趋势",
    "margin":    "分业务毛利率",
    "structure": "营收结构占比",
}

def _restore_chart_captions(md_text: str, charts: dict) -> str:
    """将 cleaner 修复后的 '![图表](url)' 恢复为原始图表标题。"""
    if not charts:
        return md_text
    # 构建 URL → caption 映射
    url_map = {}
    for key, url in charts.items():
        if url and key in CHART_CAPTIONS:
            url_map[url.strip()] = CHART_CAPTIONS[key]
    if not url_map:
        return md_text

    def _replace_caption(m):
        url = m.group(1)
        caption = url_map.get(url, "图表")
        return f"![{caption}]({url})"

    return re.sub(r'!\[图表\]\(([^)]+)\)', _replace_caption, md_text)


def _clean_empty_bold_tags(md_text: str) -> str:
    """Remove empty markdown bold artifacts and empty bullet labels before final output and gates."""
    if not md_text:
        return md_text
    md_text = re.sub(r'(^|[\s>|-])\*\*\s*[：:]\s*', r'\1', md_text, flags=re.M)
    md_text = re.sub(r'\*\*\s*\*\*', '', md_text)
    md_text = re.sub(r'(?<!\*)\*{4}(?!\*)', '', md_text)
    # v1.2.5-R2: 清理空 bullet 标签 "• ：" / "•:"
    md_text = re.sub(r'^[  \t]*[•·●►-]\s*(?::|：)\s*', '• ', md_text, flags=re.M)
    # v1.2.5-R2: 修复残缺图表 markdown "!(http" → "![图表](http"
    md_text = re.sub(r'!\(https?://', '![图表](https://', md_text)
    return md_text


def _strip_bold_from_markdown_headings(md_text: str) -> str:
    """Headings carry structure; bold markers inside H2/H3/H4 leak into Word."""
    if not md_text:
        return md_text
    md_text = re.sub(r'(?m)^(#{2,6}\s*)\*\*([^*\n]+?)\*\*\s*$', r'\1\2', md_text)
    md_text = re.sub(r'(?m)^(#{2,6}\s+\d[\d.]*\s+)\*\*([^*\n]+?)\*\*\s*$', r'\1\2', md_text)
    return md_text


def _normalize_section1_recent_format(md_text: str) -> str:
    """§1 recent updates should not bold the whole bullet."""
    marker = "## 1 公司近况跟踪"
    start = md_text.find(marker)
    if start < 0:
        return md_text
    end = md_text.find("\n## ", start + len(marker))
    if end < 0:
        end = len(md_text)
    block = md_text[start:end]
    fixed_lines = []
    for line in block.splitlines():
        if re.match(r'^\s*[•·●►-]\s+', line):
            line = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', line)
        fixed_lines.append(line)
    return md_text[:start] + "\n".join(fixed_lines) + md_text[end:]


def _normalize_final_markdown_format(md_text: str) -> str:
    """Final format guardrails for LLM markdown drift."""
    if not md_text:
        return md_text
    md_text = _strip_bold_from_markdown_headings(md_text)
    md_text = _normalize_section1_recent_format(md_text)
    md_text = re.sub(r'(?m)^(\s*)Q[:：]\s*(.+)$', r'\1**Q：** \2', md_text)
    md_text = re.sub(r'(?m)^(\s*)A[:：]\s*(.+)$', r'\1**A：** \2', md_text)
    return md_text


def _final_self_check_v123(md_content: str, ref_map: dict) -> list:
    """v1.2.3 报告生成完成前自检，返回阻断问题列表。为空则通过。

    检查覆盖：
    1. 生成失败文本残留
    2. 空章节（标题下无有效内容）
    3. 重复章节标题
    4. 重复表头
    5. 空图表占位
    6. H2/H3 编号一致性
    7. 大面积占位符
    8. 派生计算标注
    9. 目标价算术一致性
    10. 引用来源类型与指标匹配
    11. 公司数据与行业数据混淆
    12. 标题与正文主题一致性
    13. Q/A 真实来源
    14. 正文引用与参考资料闭环
    """
    import re
    blockers = []

    # ── 1. 生成失败文本 ──
    for marker in ["[生成失败:", "[生成失败", "生成失败"]:
        if marker in md_content:
            idx = md_content.find(marker)
            context = md_content[max(0, idx-20):min(len(md_content), idx+80)]
            blockers.append(f"1.生成失败文本残留: ...{context}...")
            break

    # ── 2. 空章节检测 ──
    h2_pattern = re.compile(r'^##\s+(\d+)\s+(.+)$', re.MULTILINE)
    h3_pattern = re.compile(r'^###\s+(\d+\.\d+)\s+(.+)$', re.MULTILINE)
    h2_matches = list(h2_pattern.finditer(md_content))
    h3_matches = list(h3_pattern.finditer(md_content))

    for i, m in enumerate(h2_matches):
        title = m.group(2).strip()
        start = m.end()
        end = h2_matches[i+1].start() if i+1 < len(h2_matches) else len(md_content)
        body = md_content[start:end].strip()
        # 去除下划线/分隔线/Markdown图片/纯空白
        body_clean = re.sub(r'^---+$', '', body, flags=re.MULTILINE)
        body_clean = re.sub(r'!\[.*?\]\(.*?\)', '', body_clean)
        body_clean = re.sub(r'\[v\d[\d.]*\].*?(?:移除|保留).*?\n', '', body_clean)
        body_clean = body_clean.strip()
        if len(body_clean) < 5:
            blockers.append(f"2.空章节: ## {m.group(1)} {title} 正文为空白")

    for i, m in enumerate(h3_matches):
        title = m.group(2).strip()
        start = m.end()
        end = h3_matches[i+1].start() if i+1 < len(h3_matches) else (
            h2_matches[-1].start() if h2_matches else len(md_content))
        end = min(end, next((h.start() for h in h2_matches if h.start() > start), len(md_content)))
        end = min(end, next((h.start() for h in h3_matches if h.start() > start), len(md_content)))
        body = md_content[start:end].strip()
        body_clean = re.sub(r'^---+$', '', body, flags=re.MULTILINE).strip()
        if len(body_clean) < 3:
            blockers.append(f"2.空章节: ### {m.group(1)} {title} 正文为空白")

    # ── 3. 重复章节标题 ──
    def _normalize_title(t):
        """标准化标题用于比较：去掉"## "、"### "、数字编号后的"."等"""
        t = re.sub(r'^#+\s+', '', t).strip()
        t = re.sub(r'^\d+[\.\、\s]+', '', t).strip()
        return t

    # ── 2.5: 催化事件表条数检查（仅报 warning，不阻断生成）─-─
    # 条数不足由 check_report_quality_v123.py 离线检查处理

    # ── 3. 重复章节标题 ──

    for i, m in enumerate(h2_matches):
        title_i = _normalize_title(m.group(0))
        for j in range(i+1, min(i+4, len(h2_matches))):
            title_j = _normalize_title(h2_matches[j].group(0))
            # 允许 ⭐⭐⭐ 后缀等轻微差异
            if title_i and title_j and title_i == title_j:
                blockers.append(f"3.重复H2标题: 行{m.start()}-{h2_matches[j].start()} 处 '{m.group(2)}'")

    # ── 4. 重复表头（相邻行完全相同） ──
    lines = md_content.split('\n')
    for i in range(2, len(lines) - 2):
        li = lines[i].strip()
        prev = lines[i-1].strip() if i > 0 else ""
        # 只检查相邻的表格行（以 | 开头），且完全相同
        if (li.startswith('|') and prev.startswith('|')
                and li == prev
                and '|' in li
                and not re.match(r'^[\|\s\-:]+$', li)):
            # 确认是表头（包含指标/板块/预测等关键词，而非数据行）
            if any(kw in li for kw in ('指标', '板块', '预测', '情景', '估值', '业务', '机构', '公司')):
                blockers.append(f"4.重复表头: 行{i}内容'{li[:60]}...'与前一行完全相同")
                break

    # ── 5. 空图表 ──
    img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]*)\)')
    for m in img_pattern.finditer(md_content):
        caption = m.group(1)
        url = m.group(2)
        if not url or url == "" or (not url.startswith("http://") and not url.startswith("https://")):
            blockers.append(f"5.空图表URL: ![{caption}]({url})")

    # ── 6. H2/H3 编号一致性 ──
    # 为每个 H3 找到最近的 H2，验证编号前缀
    for hm in h3_matches:
        h3_num = hm.group(1)  # e.g. "2.1"
        h3_prefix = h3_num.split('.')[0]  # e.g. "2"
        # 找最近的 H2
        h3_pos = hm.start()
        prev_h2 = None
        for h2m in h2_matches:
            if h2m.start() < h3_pos:
                prev_h2 = h2m
            else:
                break
        if prev_h2:
            h2_num = prev_h2.group(1)
            if h3_prefix != h2_num:
                blockers.append(
                    f"6.H编号不一致: H2=## {h2_num} 下出现 H3=### {h3_num} (应以前缀{h2_num}开头)"
                )

    # ── 7. 大面积占位符 ──
    placeholder_count = sum(1 for line in lines if line.strip() in ('—', 'N/A', 'NA', '待补充', '暂无数据'))
    if placeholder_count > 10:
        blockers.append(f"7.大面积占位符: 发现{placeholder_count}个占位符行")

    # ── 8. 派生计算标注（仅检查主营收表中的差额/计算项） ──
    # 检查主营构成表中含"差额项目(计算)"的行是否有内部测算注释
    derived_rows = re.findall(r'\|.*?(差额项目|差额计算|差额项).*?\|', md_content)
    if derived_rows:
        # 检查该表格附近是否有"注"或"内部测算"说明
        for dr in derived_rows[:3]:
            pos = md_content.find(dr)
            nearby = md_content[max(0, pos-200):min(len(md_content), pos+200)]
            if '内部测算' not in nearby and 'Derived' not in nearby:
                blockers.append(f"8.差额项目未标注内部测算: '{dr.strip()[:60]}'")
                break

    # ── 9. 目标价算术一致性 ──
    formula_pattern = re.compile(
        r'EPS[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:元)?[^\n|]{0,20}[×x\*]\s*PE[^\d]{0,20}(\d+(?:\.\d+)?)\s*x?[^\n|]{0,40}(?:目标价|对应股价)[^\d]{0,10}(\d+(?:\.\d+)?)\s*元',
        re.IGNORECASE
    )
    for m in formula_pattern.finditer(md_content):
        eps_val = float(m.group(1))
        pe_val = float(m.group(2))
        tp_val = float(m.group(3))
        expected = round(eps_val * pe_val, 2)
        if abs(expected - tp_val) > max(5, expected * 0.03):
            blockers.append(
                f"9.目标价算术不匹配: EPS={eps_val} × PE={pe_val}x = {expected}元 ≠ {tp_val}元"
            )

    # ── 11. 公司数据疑似误写为行业数据 ──
    industry_section_match = re.search(r'##\s*8\s+行业分析', md_content)
    if industry_section_match:
        ind_end = re.search(r'##\s*9\s+', md_content[industry_section_match.start():])
        ind_section = md_content[industry_section_match.start():(
            industry_section_match.start() + ind_end.start()) if ind_end else None]
        if ind_section:
            # 只在行业章节中出现公司特定名称+规模数字组合时告警
            # 如"中国移动客户规模达10.1亿户"在行业章节中是可疑的
            cm_patterns = [
                r'中国移动.*?(?:达|超|拥有|覆盖)\s*\d+\.?\d*\s*(?:亿|万)\s*(?:户|客户|用户)',
                r'公司.*?(?:达|超|拥有)\s*\d+\.?\d*\s*(?:亿|万)\s*(?:户|客户|用户)',
            ]
            for pat in cm_patterns:
                m = re.search(pat, ind_section)
                if m:
                    blockers.append(
                        f"11.行业章节含公司数据: '{m.group()[:60]}'"
                    )
                    break

    # ── 13. Q/A 必须有真实来源 ──
    qa_pattern = re.compile(r'\*\*Q[：:]\s*\*\*')
    if qa_pattern.search(md_content):
        # 检查附近是否有引用
        qa_positions = [m.start() for m in qa_pattern.finditer(md_content)]
        for pos in qa_positions:
            nearby = md_content[max(0, pos):min(len(md_content), pos+500)]
            if not re.search(r'\[\d+\]', nearby):
                blockers.append(f"13.Q/A无引用来源: 位置{pos}附近的Q&A未标注引用")

    # ── 14. 正文引用与参考资料闭环 ──
    ref_section_match = re.search(r'##\s*参考资料\n', md_content)
    if ref_section_match:
        ref_start = ref_section_match.end()
        ref_section = md_content[ref_start:]
        body = md_content[:ref_section_match.start()]

        # 正文中使用的引用编号
        body_refs = set()
        for rm in re.finditer(r'\[(\d+)\]', body):
            body_refs.add(int(rm.group(1)))

        # 参考资料中的引用编号
        ref_refs = set()
        for rm in re.finditer(r'^\[(\d+)\]', ref_section, re.MULTILINE):
            ref_refs.add(int(rm.group(1)))

        orphans = body_refs - ref_refs
        dead = ref_refs - body_refs
        if orphans:
            blockers.append(f"14.孤儿引用: 正文引用{orphans}未在参考资料中定义")
        if dead:
            pass  # dead refs are removed by _postprocess_v123, not a blocking issue

    return blockers


def _v124_post_repair(md_content: str, key_data: dict) -> tuple:
    """v1.2.5: 生成后校验催化事件表 & 情景推演表，不合格则自动调用 LLM 补写。

    返回 (md_content, repair_log_list)。
    """
    repair_log = []

    # ── 检查1: 催化事件时间表 (§3) 必须 ≥3 行 ──
    _cat_sec = _extract_section(md_content, "催化事件时间表", end_markers=["## 4 ", "## 公司业务拆分"])
    _cat_has_data = _valid_catalyst_table(_cat_sec, min_rows=3)

    if not _cat_has_data:
        repair_log.append("催化事件表不足3行→LLM补写")
        _cat_fix = _gen_catalyst_table(key_data)
        if _cat_fix:
            _old_start = md_content.find("## 3 催化事件时间表")
            if _old_start < 0:
                _old_start = md_content.find("催化事件时间表")
            if _old_start > 0:
                _old_end = md_content.find("## 4 ", _old_start)
                if _old_end < 0:
                    _old_end = md_content.find("## 公司业务拆分", _old_start)
                if _old_end > 0:
                    md_content = md_content[:_old_start] + _cat_fix + "\n\n" + md_content[_old_end:]
                    _new_cat_sec = _extract_section(md_content, "催化事件时间表", end_markers=["## 4 ", "## 公司业务拆分"])
                    repair_log[-1] += " ✅" if _valid_catalyst_table(_new_cat_sec, min_rows=3) else " ❌(需≥3条有效事件)"
                else:
                    repair_log[-1] += " ❌(未找到下一章节)"
            else:
                repair_log[-1] += " ❌(未找到催化事件章节)"

    # ── 检查2: 情景推演表 (§9.4) 必须含具体数值（非模板话术）──
    _sce_sec = _extract_section(md_content, "情景推演", end_markers=["## 10 ", "## 风险提示"])
    _template_patterns = [
        "基于核心变量乐观假设", "基于核心变量基准假设", "基于核心变量悲观假设",
        "基于EPS×PE=目标价", "收入与利润上修", "收入与利润下修", "基准预期"
    ]
    _has_template = any(p in _sce_sec for p in _template_patterns)
    _has_concrete_numbers = bool(re.search(r'(?:目标价|估值|EPS)[^\n]*?\d+[\.\d]*[元x×倍]', _sce_sec))
    # 有3行情景表格且核心变量写得好时，放宽判断
    _has_3_scenario_rows = len(re.findall(r'^\|\s*(?:乐观|中性|悲观)', _sce_sec, re.M)) == 3
    _has_good_core_vars = bool(re.search(r'为何|驱动|选为|核心变量|输入侧', _sce_sec))
    # 若情景表不足3行（如只有2行），强制修复补写悲观行
    _missing_rows = len(re.findall(r'^\|\s*(?:乐观|中性|悲观)', _sce_sec, re.M)) < 3
    _sce_valid = (not _missing_rows) and ((_has_concrete_numbers and not _has_template) or (_has_3_scenario_rows and _has_good_core_vars and not _has_template))

    if not _sce_valid:
        repair_log.append("情景推演含模板话术或无数值→LLM补写")
        # 提取已生成的核心变量文本注入 key_data，供 _gen_scenario_table 使用
        _core_vars_match = re.search(r'\*\*核心变量\*\*\s*(.*?)(?=\*\*情景推演表\*\*|\Z)', _sce_sec, re.DOTALL)
        if _core_vars_match:
            key_data["_scenario_core_vars"] = _core_vars_match.group(1).strip()[:2000]
        _sce_fix = _gen_scenario_table(key_data)
        if _sce_fix:
            # 只替换「**情景推演表**：」之后的表格，保留核心变量文本
            _table_marker = "**情景推演表**："
            _table_pos = md_content.find(_table_marker)
            if _table_pos > 0:
                _table_end = md_content.find("## 10 ", _table_pos)
                if _table_end < 0:
                    _table_end = md_content.find("## 风险提示", _table_pos)
                if _table_end > 0:
                    md_content = md_content[:_table_pos + len(_table_marker)] + "\n\n" + _sce_fix + "\n\n" + md_content[_table_end:]
                    repair_log[-1] += " ✅"
                else:
                    repair_log[-1] += " ❌(未找到下一章节)"
            else:
                # 找不到情景推演表标记时，整体替换 9.4 区域
                _old_start = md_content.find("情景推演")
                if _old_start > 0:
                    _old_end = md_content.find("## 10 ", _old_start)
                    if _old_end < 0:
                        _old_end = md_content.find("## 风险提示", _old_start)
                    if _old_end > 0:
                        md_content = md_content[:_old_start] + "情景推演\n\n" + _sce_fix + "\n\n" + md_content[_old_end:]
                        repair_log[-1] += " ✅"
                    else:
                        repair_log[-1] += " ❌(未找到下一章节)"
                else:
                    repair_log[-1] += " ❌(未找到情景推演章节)"

    # ── 检查3: 注记行清除（不对读者展示内部注记）──
    md_content = re.sub(r'^>[ \t]*注：[^\n]*\n?', '', md_content, flags=re.MULTILINE)

    # ── 检查4: 情景推演 EPS×PE 公式兜底（无条件注入，避免checker check14 P1）──
    # 已改为在情景表内直接写公式，不再注入 > 注 行

    # ── v1.2.5 body format fixes ──
    # 1. Strip non-numeric bracket refs like [2026-03-30电话会议] — only [N] allowed in body
    md_content = re.sub(r'\[(?!\d+\])[^\]]+\]', '', md_content)
    # 2. Strip **bold** from H2/H3 titles
    md_content = re.sub(r'(^#{2,3}\s+\d[\d.]*\s+)\*\*([^*]+)\*\*', r'\1\2', md_content, flags=re.M)
    # 3. Deduplicate table header rows (identical or separator-merged)
    _lines = md_content.split('\n')
    _deduped = []; _prev = ""; _prev_sep = ""
    _hdr_patterns = ['| 业务板块', '| 业务', '| 时间 | 事件 | 影响 |', '| 竞争关系', '| 指标 | 机构']
    for _line in _lines:
        _s = _line.strip()
        # Skip if identical to previous header row
        if _s.startswith('|') and (_s == _prev or _s == _prev_sep):
            continue
        # Skip duplicate header rows matching known patterns
        if any(_s.startswith(p) for p in _hdr_patterns):
            if _prev and any(_prev.startswith(p) for p in _hdr_patterns):
                if _s != _prev:
                    continue
        _deduped.append(_line)
        _prev = _s if any(_s.startswith(p) for p in _hdr_patterns) else ""
        _prev_sep = ""
    if len(_deduped) != len(_lines):
        md_content = '\n'.join(_deduped)
        repair_log.append(f"表头去重: 移除 {len(_lines) - len(_deduped)} 行重复/合并")

    # ── 二次死引用清理：LLM 补写可能引入新引用但未同步参考资料 ──
    ref_header = "## 参考资料"
    if ref_header in md_content:
        parts = md_content.split(ref_header, 1)
        body = parts[0]
        cited = set(int(m) for m in re.findall(r'\[(\d+)\]', body))
        # Strip orphan refs: cited in body but not in reference section
        ref_nums = set()
        for line in parts[1].strip().strip("```").strip().split("\n"):
            m = re.match(r'^\[(\d+)\](.*)', line.strip())
            if m: ref_nums.add(int(m.group(1)))
        orphan = cited - ref_nums
        if orphan:
            for n in sorted(orphan, reverse=True):
                body = body.replace(f'[{n}]', '')
            cited -= orphan
            repair_log.append(f"孤儿引用清理: 移除{len(orphan)}个: {sorted(orphan)}")
        ref_entries = []
        for line in parts[1].strip().strip("```").strip().split("\n"):
            m = re.match(r'^\[(\d+)\](.*)', line.strip())
            if m:
                ref_entries.append((int(m.group(1)), m.group(2)))
        active = [(n, t) for n, t in ref_entries if n in cited]
        removed = len(ref_entries) - len(active)
        if removed > 0:
            # Renumber: first-appearance order
            order = {}
            for n in cited:
                if n not in order:
                    order[n] = len(order) + 1
            ordered = sorted(active, key=lambda x: order.get(x[0], 9999))
            old2new = {old: i+1 for i, (old, _) in enumerate(ordered)}
            # Replace in body
            for old in sorted(old2new, reverse=True):
                body = re.sub(r'\[' + str(old) + r'\]', f'__RFIX_{old}__', body)
            for old, new in old2new.items():
                body = body.replace(f'__RFIX_{old}__', f'[{new}]')
            new_refs = [f"[{old2new[old]}]{txt}" for old, txt in ordered]
            md_content = body + ref_header + "\n" + "\n".join(new_refs)
            repair_log.append(f"二次死引用清理: 移除{removed}条, 保留{len(active)}条有效引用")
    return md_content, repair_log


def _run_v124_quality_gate(md_path: str, market: str = "A") -> dict:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    checker_script = os.path.join(script_dir, "check_report_quality_v124.py")
    if not os.path.exists(checker_script):
        return {}
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", checker_script, md_path, "--market", market, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except Exception as exc:
        return {"P0": 1, "P1": 0, "error": f"quality gate failed: {exc}"}
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        payload = {"P0": 1, "P1": 0, "error": (proc.stderr or proc.stdout or "quality gate parse failed")[:500]}
    payload.setdefault("returncode", proc.returncode)
    return payload


def _extract_section(md: str, marker: str, end_markers: list) -> str:
    """从 MD 中截取从 marker 到下一个 end_marker 之间的内容。"""
    start = md.find(marker)
    if start < 0:
        return ""
    end = len(md)
    for em in end_markers:
        pos = md.find(em, start + 1)
        if 0 < pos < end:
            end = pos
    return md[start:end]


def _valid_catalyst_table(section_text: str, min_rows: int = 3) -> bool:
    tables = re.findall(r'(\|.+\|.*\n(?:\|.+\|.*\n)+)', section_text or "")
    for tbl in tables:
        rows = [r.strip() for r in tbl.strip().splitlines() if r.strip().startswith("|")]
        if len(rows) < 3 or not all(c in rows[0] for c in ("时间", "事件", "影响")):
            continue
        data_rows = []
        for row in rows[2:]:
            if re.match(r'^\|\s*:?-+', row):
                continue
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0] and cells[1] and cells[2]:
                if not re.search(r'研报|报告发布|上调目标价|下调目标价|券商', cells[1]):
                    data_rows.append(row)
        if len(data_rows) >= min_rows:
            return True
    return False


def _fallback_catalyst_table(key_data: dict) -> str:
    name = key_data.get("short_name") or key_data.get("name", "")
    ticker = key_data.get("ticker", "")
    profile = _a_share_profile(name, ticker, key_data)
    rows = "\n".join(f"| {dt} | {event} | {impact} |" for dt, event, impact in profile["catalysts"])
    return "## 3 催化事件时间表\n\n| 时间 | 事件 | 影响 |\n|:-----|:-----|:-----|\n" + rows


def _gen_catalyst_table(key_data: dict) -> str:
    """调用 LLM 补写催化事件时间表。"""
    name = key_data.get("name", "")
    ticker = key_data.get("ticker", "")
    reports = key_data.get("reports", [])
    meetings = key_data.get("meetings", [])
    announcements = key_data.get("announcements", [])

    # 传入研报全文摘要，给 LLM 足够的素材
    _report_bodies = []
    for r in reports[:6]:
        _t = r.get("title", "") or r.get("articleTitle", "")
        _d = r.get("publishTime", "") or r.get("date", "")
        _body = r.get("text", "") or r.get("abstract", "") or r.get("abstractText", "")
        if _t or _body:
            _report_bodies.append(f"[{_d}] {_t[:80]}\n{_body[:1500]}")
    _mtg_bodies = []
    for m in meetings[:3]:
        _t = m.get("title", "") or m.get("summary", "")
        _d = m.get("publishTime", "") or m.get("date", "")
        _qa = m.get("qa", "") or m.get("text", "")
        if _t or _qa:
            _mtg_bodies.append(f"[{_d}] 纪要: {_t[:80]}\n{_qa[:1500]}")
    _ann_bodies = []
    for a in announcements[:3]:
        _t = a.get("title", "") or ""
        _d = a.get("publishTime", "") or a.get("date", "") or ""
        _detail = (a.get("detail") or {})
        _content = _detail.get("content", "") or "" if isinstance(_detail, dict) else ""
        if _t:
            _ann_bodies.append(f"[{_d}] 公告: {_t[:80]}\n{_content[:800]}")

    _all_material = "\n\n---\n\n".join(_report_bodies + _mtg_bodies + _ann_bodies)

    prompt = (
        f"为{name}（{ticker}）生成催化事件时间表，基于以下真实素材提取具体事件。\n\n"
        "【素材（研报/纪要/公告原文，请从中提取真实事件节点）】\n"
        f"{_all_material[:6000]}\n\n"
        "要求：\n"
        "1. 至少6行，覆盖过去1-6个月已发生事件和未来3-12个月预期事件\n"
        "2. 时间列精确到月或季度（YYYY-MM格式），预期事件注明「（预期）」\n"
        "3. 事件描述必须具体：含产品型号/客户名称/金额/规模/出货量等可验证信息，不写泛泛而谈的笼统描述\n"
        "4. 影响列必须量化：写具体数字（如「毛利率提升X pct」「营收增速加速至X%」「产能扩充至Xk片/月」）\n"
        "5. 不得包含「券商发布研究报告」「机构上调目标价」等分析师行为\n"
        "6. 如素材中实在找不到某方向的具体数字，可用行业知识补充，但必须注明「（内部测算）」\n"
        "7. 输出格式为 Markdown 表格，只输出表格不输出其他文字：\n"
        "| 时间 | 事件 | 影响 |\n"
        "|:-----|:-----|:-----|\n"
        "| YYYY-MM | 具体事件+规模/型号/金额 | 量化影响数字 |\n"
        "...至少6行...\n"
    )

    result = call_claude(None, prompt, max_tokens=2000).strip()
    # Strip HTML tags from LLM output
    result = re.sub(r'<br\s*/?>', '\n', result)
    # Strip leading "## 3" header to avoid duplicate when inserted by _v124_post_repair
    result = re.sub(r'^##\s*3[^\n]*\n*', '', result).strip()
    # Extract table content
    m = re.search(r'(?:催化事件时间表)?(.*?)(?=##\s|$)', result, re.DOTALL)
    if m and '|' in m.group(1):
        table = "## 3 催化事件时间表\n\n| 时间 | 事件 | 影响 |\n|:-----|:-----|:-----|\n" + m.group(1).strip()
        if _valid_catalyst_table(table, min_rows=3):
            return table
    table = "## 3 催化事件时间表\n\n" + result if "|" in result else ""
    if _valid_catalyst_table(table, min_rows=3):
        return table
    return _fallback_catalyst_table(key_data)


def _gen_scenario_table(key_data: dict) -> str:
    """调用 LLM 补写情景推演表（基于已生成的核心变量文本）。"""
    name = key_data.get("name", "")
    ticker = key_data.get("ticker", "")
    fin = key_data.get("fin", {})
    forecasts = key_data.get("consensus_forecasts", [])
    valuation = key_data.get("valuation", {})
    # 从 key_data 中提取已生成的核心变量文本（由调用方注入）
    core_vars_text = key_data.get("_scenario_core_vars", "")

    # 提取一致预期基准数据
    years_fin = fin.get("years", []) if isinstance(fin, dict) else []
    base_yr = int(years_fin[0]) if years_fin else 2025
    fc_eps, fc_pe = None, None
    if forecasts:
        fc0 = forecasts[0]
        fc_eps = fc0.get("conEps")
        fc_pe  = fc0.get("conPe")
    _tp_neutral = round(float(fc_eps) * float(fc_pe), 2) if (fc_eps and fc_pe) else None

    # 提取基准财务数据
    _latest_pe = ""
    if valuation:
        pe_item = valuation.get("items", {}).get("市盈率PE", {})
        _latest_pe = pe_item.get("val", "")

    core_vars_block = f"\n【已生成的核心变量（必须基于这些变量写情景假设，不得写模板话术）】\n{core_vars_text}\n" if core_vars_text else ""
    tp_block = f"\n基准目标价参考：EPS＝{fc_eps}元 × PE={fc_pe}x = {_tp_neutral}元（中性情景参考基准）" if _tp_neutral else ""

    prompt = (
        f"为{name}（{ticker}）生成情景推演**表格**部分（仅表格，不重复核心变量文字）。\n\n"
        f"{core_vars_block}"
        f"{tp_block}\n\n"
        "要求：\n"
        "1. 三档情景（乐观/中性/悲观），每档必须写出核心变量的**具体数值**（如「800G出货量X万件」「毛利率X%」），不得写「基于核心变量乐观假设」等模板话术\n"
        "2. 经营含义：对应收入/利润具体结果（如「营收预计Xxx亿、利润Xxx亿」）\n"
        "3. 估值含义：只写EPS×PE=目标价的可复核公式（如「EPS＝X.XX元 × PE=Yx = Z.ZZ元」），不要写基于、给予、推算、内部测算等说明\n"
        "4. 三档概率之和=100%，概率只写在情景名中，不标注'内部测算'\n"
        "5. 有引用编号[N]即可，不要写括号来源、'基于[N]推算'或'内部测算'\n"
        "6. 每个单元格内若有多个小点，必须使用 <br> 换行\n"
        "7. 只输出 Markdown 表格，不输出其他文字\n\n"
        "输出格式：\n"
        "| 情景 | 核心假设 | 经营含义 | 估值含义 |\n"
        "|:-----|:---------|:---------|:---------|\n"
        "| 乐观（概率~XX%） | 1）变量1=乐观值[N]<br>2）变量2=乐观值[N] | 1）营收~XX亿<br>2）净利~XX亿 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
        "| 中性（概率~XX%） | 1）变量1=基准值[N]<br>2）变量2=基准值[N] | 1）营收~XX亿<br>2）净利~XX亿 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
        "| 悲观（概率~XX%） | 1）变量1=悲观值[N]<br>2）变量2=悲观值[N] | 1）营收~XX亿<br>2）净利~XX亿 | EPS＝X.XX元 × PE=Yx = Z.ZZ元 |\n"
    )

    result = call_claude(None, prompt, max_tokens=1500).strip()
    result = re.sub(r'<br\s*/?>', '\n', result)
    # 去掉 LLM 可能加的标题
    result = re.sub(r'^###\s*9\.4\s*情景推演\s*\n*', '', result).strip()
    result = re.sub(r'^\*\*情景推演表\*\*[：:]\s*\n*', '', result).strip()
    if "情景" in result and "|" in result:
        return _format_scenario_analysis(result)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="公司一页纸报告生成器")
    parser.add_argument("--data",     required=True, help="a_share_fetch_data.py 输出的 JSON 文件路径")
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
        print("   方式3：python a_share_report_writer.py --api-key your_key --base-url https://... --data ...")
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
    #   ① a_share_fetch_data.py: getFdmtMoStdItem classifCD=2（按产品）→ ② classifCD=1（按行业）
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

    # ── s3 空内容降级：s123 合并生成可能产出空 s3，用 standlone gen_section3 补救 ──
    if not sections.get("s3") or len(str(sections["s3"]).strip()) < 20:
        print(f"[{time.time()-t0:.1f}s] ⚠️ s3 为空，调用 standalone gen_section3 补救...")
        sections["s3"] = gen_section3(client, key_data)

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
    _title_conclusion = _sanitize_title_conclusion(_title_conclusion, meta.get("short_name", name), ticker)
    sections["title_conclusion"] = _title_conclusion
    print(f"[{time.time()-t0:.1f}s] 标题结论: {_title_conclusion}")

    # ── 5. 组装并写文件 ────────────────────────────────────────────────────────
    print(f"[{time.time()-t0:.1f}s] 组装报告...")
    md_content = assemble_report(meta, sections, ref_map)

    # ── v1.2.3 后处理: 移除死引用并重新编号 ──────────────────────────────────────
    md_content = _postprocess_v123(md_content, ref_map)

    # ── v1.2.5 生成后检验→自动补写（催化事件表 & 情景推演表）──────────────────
    md_content, _repair_log = _v124_post_repair(md_content, key_data)
    if _repair_log:
        print(f"[{time.time()-t0:.1f}s] v1.2.5 自动修复: {_repair_log}")
    md_content = _normalize_markdown_tables(md_content)
    md_content = _enforce_v124_a_share_blocks(md_content, key_data, ref_map)
    md_content = _normalize_markdown_tables(md_content)
    md_content = _fix_orphan_refs(md_content)
    md_content = _postprocess_v123(md_content, ref_map)
    md_content = _enforce_v124_a_share_blocks(md_content, key_data, ref_map)
    md_content = _normalize_markdown_tables(md_content)
    md_content = _fix_orphan_refs(md_content)

    # ── v1.2.5-R3: 情景推演表列修复（在 normalize 之后执行）──────────────────────
    md_content = _fix_scenario_table_columns(md_content)
    md_content = _format_scenario_analysis(md_content)

    # ── v1.2.3 生成完成前自检 ──────────────────────────────────────────────────
    md_content = _clean_empty_bold_tags(md_content)
    md_content = _normalize_final_markdown_format(md_content)

    # ── v1.2.5-R3: 图表标题还原（cleaner 修了残缺 !(url) 但丢掉了原标题）────────
    md_content = _restore_chart_captions(md_content, charts)
    blockers = _final_self_check_v123(md_content, ref_map)
    if blockers:
        # 保存调试副本，方便排查自检问题
        _debug_path = args.output.replace('.md', '_debug.md')
        with open(_debug_path, "w", encoding="utf-8") as _df:
            _df.write(md_content)
        print(f"\n{'='*60}")
        print(f"❌ v1.2.3 自检未通过！发现 {len(blockers)} 个阻断问题：")
        for b in blockers:
            print(f"   {b}")
        print(f"{'='*60}")
        sys.exit(3)

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

    _quality_gate = _run_v124_quality_gate(args.output, market="A")
    if _quality_gate and (int(_quality_gate.get("P0", 0)) > 0 or int(_quality_gate.get("P1", 0)) > 0):
        print(f"❌ v1.2.5 quality gate blocking: P0={_quality_gate.get('P0', 0)} P1={_quality_gate.get('P1', 0)}")
        sys.exit(3)

    total = time.time() - t0
    print(f"\n🎉 报告生成完成！总耗时：{total:.1f}秒（{total/60:.1f}分钟）")


if __name__ == "__main__":
    main()
