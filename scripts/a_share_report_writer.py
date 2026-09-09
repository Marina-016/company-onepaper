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
    # 国际厂商
    "api.anthropic.com",
    "api.openai.com",
    # 通联代理
    "llm-proxy.datayes.com",
    "llm-proxy.wmcloud.com",
    "openai.datayes.com",
    # 俄罗斯/其他
    "gateway.ai.cloud.ru",
    # 国产 LLM 厂商
    "open.bigmodel.cn",             # 智谱AI (GLM)
    "api.deepseek.com",             # DeepSeek
    "dashscope.aliyuncs.com",       # 阿里通义千问 (Qwen)
    "api.moonshot.cn",              # 月之暗面 (Kimi)
    "api.baichuan-ai.com",          # 百川智能
    "api.minimax.chat",             # MiniMax
    "ark.cn-beijing.volces.com",    # 字节豆包 (火山引擎)
    "api.stepfun.com",              # 阶跃星辰
    "api.lingyiwanwu.com",          # 零一万物 (Yi)
    "spark-api-open.xf-yun.com",    # 讯飞星火
    "aip.baidubce.com",             # 百度文心 (ERNIE)
    "hunyuan.tencentcloudapi.com",  # 腾讯混元
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

## 数字溯源与引用适配
- 每一个带 `[N]` 的数字、比例和经营指标，必须能在该编号对应的原始接口 JSON、研报全文、纪要或调研正文中直接找到；找不到时删除数字和对应断言，不得补写、估算或“合理化”。
- `fdmtNew` 只可引用标准财务摘要字段（营收、利润、现金流、资产负债、EPS、ROE、毛利率等）；严禁以它为来源写销量、产量、吨价、单价、渠道/直销占比、市占率、产能、客户数等经营分项。
- 基酒/产品产量、销量、吨价、渠道占比和产品增速，必须引用含该术语与数值的研报全文、纪要、调研或对应业务明细接口；无法逐项证明则不写。
- 不得把 `operateProfitRatio` 称为毛利率；毛利率只使用 `grossMARgin` / 主营构成接口的 `grossMargin` 字段。

## 财务指标名称与口径（强制执行）
- 报告正文、表格、图注中不得单独简写“营收”“净利”“ROE”“现金流”；首次出现必须使用完整指标名称和期间，如“2025年营业总收入”“归属于母公司股东的净利润”“净资产收益率-加权平均”“经营活动产生的现金流量净额”。
- `fdmtNew` 中 `tRevenue` 为营业总收入，`revenue` 为营业收入；两者并存或口径不同，必须分别写明，禁止都简称为“营收”或相互替换。
- `NPAttrP` 必须写为归属于母公司股东的净利润；`grossMARgin` 为毛利率；`npMARgin` 为销售净利率；`operateProfitRatio` 为营业利润率；`ROE` 为摊薄净资产收益率，`ROEW` 为净资产收益率-加权平均。
- 若原始来源未明确指标口径、合并范围、期间或币种，不得补充推断；改写为带来源的中性表述或删除。

- 每个经营事实短语应在其末尾紧跟对应 `[N]`；同一句含多个来源时，按事实短语用分号拆开并各自标注，禁止把多个来源集中堆在段末。
- 经营事实中的同比、占比、涨跌幅等百分比，只有原始来源明确出现该百分比时才能直接写；若确需派生计算，必须在同一短语写出可复核公式及两项基础数值，并紧跟基础来源，否则删除该百分比。
## 派生测算
- 派生测算必须保留基础数据[N]、公式、单位和假设。除主营构成差额等必须解释的派生项外，最终正文不输出"内部测算""基于[N]推算"等过程标签；9.4情景推演不输出公式、目标价或其他计算过程，只保留来源绑定的定性判断。无法完整复核则删除具体数字。

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
        "grossMargin": "grossMARgin",
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

    def _is_derived_maincomp_item(name: object) -> bool:
        """Exclude residual/calculated rows; they are not disclosed business lines."""
        text = str(name or "")
        return "\u5dee\u989d" in text or "\u8ba1\u7b97" in text

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

    # 构建有序 key 列表（父级→子级→孙级），同时建立 itemID→key 映射供后续填充使用
    order = []
    id_to_key = {}  # itemID → order key（带层级前缀）
    def _add_children(parent_id: int, prefix: str):
        for child_id in children_of.get(parent_id, []):
            child_name = id_to_name.get(child_id, "")
            if child_name and not _is_derived_maincomp_item(child_name):
                key = prefix + child_name
                order.append(key)
                id_to_key[child_id] = key
                _add_children(child_id, "  " + prefix)  # 递归孙级
    for r in first_recs:
        iid = r.get("itemID", 0)
        sup = r.get("itemIDSuperior")
        if iid == 0 or sup != 0:   # 跳过合计行和非一级项目
            continue
        name = r.get("itemName", "")
        if not name or _is_derived_maincomp_item(name):
            continue
        order.append(name)                           # 一级项目（原名）
        id_to_key[iid] = name
        _add_children(iid, "└ ")                     # 递归子级/孙级

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
            if iid == 0 or not name or _is_derived_maincomp_item(name):
                continue
            # 使用 order 构建阶段确定的带层级前缀的 key
            key = id_to_key.get(iid, name)
            if key not in segments:     # 后续年份出现的新板块
                segments[key] = [None] * n
                margins[key]  = [None] * n
                order.append(key)
            rev = row.get("revenue")
            # 仅在主字段缺失时回退；合法的 0% 不能被 falsy-or 覆盖。
            mgn = row.get("grossMargin")
            if mgn is None:
                mgn = row.get("grossMarginStd")
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
    # 过滤出预测值（conEpsType∈{2,3}，排除0=实际值、4=长期预测），按 foreYear 升序
    forecasts = [x for x in lst if x.get("conEpsType") in (2, 3)]
    forecasts.sort(key=lambda x: x.get("foreYear", 0))  # v1.2.11: 按年份升序
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


def _peer_material_ref_key(index: int, item: dict) -> str:
    code = re.sub(r'\D', '', str((item or {}).get('peer_code') or '')) or 'unknown'
    return f"peer_material_{code}_{index}"


def _peer_material_entries(data: dict):
    for index, item in enumerate(data.get('peer_materials') or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get('peer_name') or '').strip()
        code = re.sub(r'\D', '', str(item.get('peer_code') or ''))
        if name and re.fullmatch(r'\d{6}', code):
            yield index, item, name, code
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

    # 机构调研详情（为 §4.5 Q&A 提供引用溯源）
    for sv in extract_surveys_detail(data):
        if sv.get("event_id"):
            key = "survey_" + sv["event_id"]
            refs[key] = {
                "n": idx,
                "type": "调研",
                "id": sv["event_id"],
                "date": sv["date"],
                "org": "机构调研",
                "title": f"{sv['type'] or '投资者关系活动'}（{sv['date']}）",
            }
            idx += 1

    # 同业定向材料：仅作为 §8.2 “相关业务进展”的来源，保留公司代码和原始索引以便审计。
    for material_index, item, peer_name, peer_code in _peer_material_entries(data):
        source_id = str(item.get("id") or item.get("materialId") or item.get("docId") or material_index)
        refs[_peer_material_ref_key(material_index, item)] = {
            "n": idx,
            "type": "同业材料",
            "id": f"{peer_code}:{source_id}",
            "date": str(item.get("date") or item.get("publishDate") or TODAY),
            "org": peer_name,
            "title": str(item.get("title") or "同业定向检索材料"),
            "api_name": "getMaterialsV2",
            "peer_name": peer_name,
            "peer_code": peer_code,
            "source_index": material_index,
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
        elif ref_type == "同业材料":
            lines.append(
                f"[{n}]Materials V2研报 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：getMaterialsV2"
            )
        elif ref_type == "研报":
            lines.append(
                f"[{n}]Datayes研报 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：batchGetReportContent（研报全文）"
            )
        elif ref_type == "纪要":
            lines.append(
                f"[{n}]Datayes纪要 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：getMeetingSummaryDetail（会议纪要详情）"
            )
        elif ref_type == "调研":
            lines.append(
                f"[{n}]Datayes调研 | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title} | API：institution_research_detail（机构调研详情）"
            )
        else:
            lines.append(
                f"[{n}]{ref_type} | {ref_date} | ID：{ref_id} | {ref_org} | {ref_title}"
            )
    return "\n".join(lines)


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
        row("销售净利率（%）", "netMargin", is_pct=True, unit=1),
        row("净资产收益率-加权平均（%）", "ROEW", is_pct=True, unit=1),
        row(f"经营活动产生的现金流量净额（{rev_unit_label}）", "operCashFlow", unit=rev_unit),
        row(f"总资产（{rev_unit_label}）",          "totalAssets",  unit=rev_unit),
        row("资产负债率（%）", "liabRatio", is_pct=True, unit=1),
        row("基本每股收益（元）", "basicEPS", unit=1, decimals=2),
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
        # 层级显示：按 ":" 数量加树状前缀，子级只显示自身名称
        depth = seg_name.count(":")
        if depth == 0:
            display_name = seg_name
        elif depth == 1:
            leaf = seg_name.rsplit(":", 1)[-1]
            display_name = f"├ {leaf}"
        else:
            leaf = seg_name.rsplit(":", 1)[-1]
            display_name = f"│  ├ {leaf}"
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
        lines.append(f"| {display_name}{cells} |")

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

def _compact_reports(reports: list, n: int = 5, ref_map: dict = None) -> str:
    """将研报列表压缩为 prompt 友好的格式，保留足够的正文供深度分析"""
    parts = []
    for r in reports[:n]:
        rid = str(r.get("id", ""))
        ref_no = str((ref_map or {}).get("report_" + rid, {}).get("n", ""))
        source_tag = f"[{ref_no}]" if ref_no else "[未编号]"
        src = f"【唯一引用{source_tag} | 文档ID:{rid} | 机构:{r.get('org','')} | 日期:{r.get('date','')} | 标题:{r.get('title','')}】"
        parts.append(f"{src}\n摘要:{(r.get('abstract') or '')[:1500]}\n正文:\n{(r.get('text') or '')[:3500]}")
    return "\n\n---\n\n".join(parts)


def _compact_meetings(meetings: list, n: int = 3, include_text: bool = False, ref_map: dict = None) -> str:
    """将会议纪要压缩为 prompt 友好的格式

    include_text=True 时追加 aiOriBody 完整正文，用于核心分析章节（第2/4节）。
    """
    if not meetings:
        return "（无会议纪要数据）"
    parts = []
    for m in meetings[:n]:
        key = "meeting_" + str(m.get("date", "")) + "_" + str(m.get("title", ""))[:20]
        ref_no = str((ref_map or {}).get(key, {}).get("n", ""))
        source_tag = f"[{ref_no}]" if ref_no else "[未编号]"
        header = f"【唯一引用{source_tag} | 纪要ID:{m.get('id','')} | {m['date']} {m['type']} {m['title']}】"
        body = ""
        if m["overview"]:
            body += f"AI概述：\n{m['overview']}\n"
        if m["qa"]:
            body += f"\nQ&A摘要：\n{m['qa']}\n"
        if include_text and m.get("text"):
            body += f"\n原文节录：\n{m['text']}"
        parts.append(f"{header}\n{body.strip()}")
    return "\n\n".join(parts)


def _fact_card_prompt_block(key_data: dict) -> str:
    """供所有叙述性章节复用的事实卡规则，避免只在某一节启用溯源。"""
    cards = key_data.get("operating_fact_cards", "（无可核验经营事实卡；不得编造经营数字）")
    return f"""【可核验经营事实卡】
{cards}

【事实卡规则】
- 对销量、产量、出货、单价、渠道占比、市占率、产能、产品增速、客户数及报告/纪要中的精确收入或利润数字，必须直接输出对应的 `{{{{FACT:F编号}}}}`；不得手写该数字或猜测[N]。
- 程序会把标记替换为原文数值及其唯一引用。没有事实卡则删除该具体数字。
- fdmtNew、consensus、估值接口不能作为经营事实的来源。
"""

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

【近期研报（3篇，含完整分析）】
{_compact_reports(reports, n=3, ref_map=ref_map)}

【近期会议纪要（含完整正文）】
{_compact_meetings(meetings, n=1, include_text=True, ref_map=ref_map)}

{"【管理层讨论（MD&A）】" + chr(10) + mgmt_text if mgmt_text else ""}

【公告列表】
{json.dumps(announcements[:5], ensure_ascii=False)[:500] if announcements else "（无）"}

【市场一致预期】
{con_summary}

【可核验经营事实卡（经营数字必须仅使用此处的标记）】
{key_data.get("operating_fact_cards", "（无可核验经营事实卡；不得编造经营数字）")}

【事实卡硬规则】
- 涉及销量、产量、出货、单价、渠道占比、市占率、产能、系列酒、基酒、客户数等经营数字时，正文必须直接输出对应的 `{{{{FACT:F编号}}}}`，不要手写数字、不要手写[N]。
- 渲染程序会把标记替换成该来源原文中的“数字[N]”；没有事实卡就删去该经营数字，不得根据主题联想或自行换算。
- 不得把 fdmtNew、consensus、估值接口当作经营事实来源；它们只能支撑财务、预测或估值字段。

【引用映射（正文中用[N]标注）】
研报：{_refs_labels(reports, ref_map, n=3)}
会议纪要：{_meeting_refs_str(meetings, ref_map, n=1)}
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
• **[催化剂1标题]**：[含精确数据和逻辑链，标注引用，聚焦核心]
• **[催化剂2标题]**：...（共3个要点）

### 2.2 长期逻辑（核心竞争力）
• **[核心壁垒]**：[含市占率/规模量化数据，标注引用]
• **[成长驱动力]**：[标注引用]
• **[商业模式优势]**：[ROE/可持续性，标注引用]

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
        f"• **客户与交付壁垒**：{profile['business']}需要长期产品验证、规模交付和质量控制，头部客户导入形成竞争门槛{ref1}。\n"
        f"• **技术与产品驱动**：核心技术迭代推动产品升级，公司核心产品线受益于行业升级{ref1}。\n"
        f"• **规模与财务弹性**：收入体量、利润释放和费用摊薄共同决定中长期ROE修复空间，财务数据需持续跟踪{fdref}。"
    )
    rows = []
    for r in reports[:3]:
        dt = str(r.get("publishTime") or r.get("date") or TODAY)[:10]
        title = (r.get("title") or r.get("articleTitle") or "研究更新")[:45]
        rows.append(f"| {dt} | {title} | 更新业务/盈利预期跟踪 |")
    rows.extend(f"| {dt} | {event} | {impact} |" for dt, event, impact in profile["catalysts"])
    s3 = "| 时间 | 事件 | 影响 |\n|:-----|:-----|:-----|\n" + "\n".join(rows[:6])
    return {"s1": s1, "s2": s2, "s3": s3}



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
{_compact_reports(reports, n=3, ref_map=ref_map)}

【会议纪要（管理层表述）】
{_compact_meetings(meetings, n=1, include_text=True, ref_map=ref_map)}

{"【管理层讨论（MD&A）】" + chr(10) + mgmt_text if mgmt_text else ""}

{_fact_card_prompt_block(key_data)}

【引用映射】
{_refs_str(reports, ref_map, 3)}
{_meeting_refs_str(meetings, ref_map, n=1)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}], maincomp=[{ref_map.get('maincomp',{}).get('n','')}]

【格式要求】**两个子节合计400字以内**（精炼专业）。直接输出以下两节：

### 4.3 业务深度分析

[对主要板块（2-3个核心板块）逐一分析，含精确数据支撑，每个板块2-3句，标注引用]

### 4.4 核心竞争力与竞争优势

• **[优势1标题]**：[2句量化支撑，标注引用]
• **[优势2标题]**：[2句量化支撑，标注引用]
• **[优势3标题]**：[2句量化支撑，标注引用]
"""
    return call_claude(client, prompt, max_tokens=1000)


def _extract_qa_candidates(qa_blocks: list) -> list:
    """v1.2.9: 从原始调研/会议纪要中提取 Q&A 候选对，返回结构化列表。

    支持格式：**Q：**/A:, Q：/A：, N、...答:..., question:/answer:, 问：/答：
    返回: [{"q": str, "a": str, "ref": str or ""}, ...]
    不包含 Markdown 格式标记，纯文本。
    """
    import re as _re

    candidates = []
    for blk in qa_blocks[:4]:
        # 提取引用标记
        ref_tag = ""
        ref_m = _re.search(r'】\s*(\[\d+\])', blk)
        if ref_m:
            ref_tag = ref_m.group(1)
        # 剥离表头和残留引号
        text = _re.sub(r'^【.*?】\n?', '', blk, flags=_re.MULTILINE)
        text = _re.sub(r'^\s*\[\d+\]\s*$', '', text, flags=_re.MULTILINE)
        text = text.strip()
        if not text:
            continue

        extracted = False

        # ── 路径A: 尝试从已格式化的 Q/A 文本提取（如 **Q：**...**A：**...）──
        # 先把各种 Q/A 标记归一化，再按行或分隔符拆分
        normalized = text
        # 去掉 Q/A 周围的加粗标记，便于统一拆分
        normalized = _re.sub(r'\*\*([AQ])([:：])\*\*', r'\1\2', normalized)
        normalized = _re.sub(r'([AQ])([:：])\*\*', r'\1\2', normalized)
        normalized = _re.sub(r'\*\*([AQ])([:：])', r'\1\2', normalized)
        # Q1、→ Q1：归一化（格力电器2024年格式）
        normalized = _re.sub(r'(?:^|\n)\s*Q(\d+)[、]\s*', r'\nQ\1：', normalized, flags=_re.MULTILINE)
        # Detail payloads can concatenate numbered pairs as Q1…A1…Q2…A2.
        # Put each numbered marker on its own line before pairing, so later
        # questions cannot be absorbed into the preceding answer.
        normalized = _re.sub(
            r'(?<!\n)(?=(?:Q|问|A|答)\s*\d+\s*[:：])',
            '\n',
            normalized,
            flags=_re.IGNORECASE,
        )

        # 尝试按 Q：/A：拆分成单独的 QA 对
        qa_segments = _re.split(r'(?:^|\n)\s*(?:Q|问)(?:\d*)\s*[:：]\s*', normalized, flags=_re.MULTILINE | _re.IGNORECASE)
        if len(qa_segments) >= 2:
            for seg in qa_segments[1:]:  # 跳过第一个（Q 前的内容）
                seg = seg.strip()
                if len(seg) < 15:
                    continue
                # A：/答：可能在同行（无换行），所以不要求 ^|\n 锚定——只匹配首个分隔符
                a_parts = _re.split(r'(?:A|答)(?:\d*)\s*[:：]\s*', seg, maxsplit=1, flags=_re.IGNORECASE)
                if len(a_parts) >= 2:
                    q = a_parts[0].strip()
                    a_remainder = a_parts[1].strip()
                    # 同行内可能还有后续 Q/A 对：递归提取
                    while a_remainder:
                        next_q = _re.split(r'(?:Q|问)\s*\d*\s*[:：]\s*', a_remainder, maxsplit=1, flags=_re.IGNORECASE)
                        if len(next_q) >= 2:
                            a = next_q[0].strip()
                            if q and a:
                                candidates.append({"q": q, "a": a, "ref": ref_tag})
                                extracted = True
                                if len(candidates) >= 10:
                                    return candidates
                            # 下一个 Q/A
                            q = next_q[1].strip()
                            next_a = _re.split(r'(?:A|答)(?:\d*)\s*[:：]\s*', q, maxsplit=1, flags=_re.IGNORECASE)
                            if len(next_a) >= 2:
                                q = next_a[0].strip()
                                a_remainder = next_a[1].strip()
                            else:
                                # 最后一段没有 A 配对，丢弃
                                a_remainder = ""
                        else:
                            # 只剩最后一个 A 段
                            a = a_remainder
                            if q and a:
                                candidates.append({"q": q, "a": a, "ref": ref_tag})
                                extracted = True
                                if len(candidates) >= 10:
                                    return candidates
                            a_remainder = ""

        if extracted:
            continue

        # ── 路径B: 中文 N、...答:... 格式 ──
        chinese_items = _re.split(r'(?:(?:^|\s)\d+[、.]\s*)', text)
        meaningful = []
        for item in chinese_items:
            item = item.strip()
            if not item:
                continue
            if not _re.search(r'[答回]', item[:20]) and len(item) < 50:
                continue
            meaningful.append(item)
        if len(meaningful) >= 2:
            for item in meaningful:
                parts = _re.split(r'(?:回[答复]?|[答回])[\s]*[:：]\s*', item, maxsplit=1)
                if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                    q = parts[0].strip()
                    a = parts[1].strip()
                    if len(q) < 200 and len(a) > 10:
                        candidates.append({"q": q, "a": a, "ref": ref_tag})
                        if len(candidates) >= 10:
                            return candidates
            if candidates:
                continue

        # ── 路径C: full text 按 question:/answer: 拆分 ──
        qa_segs = _re.split(r'(?:^|\n)\s*question\s*\d*\s*:\s*', text, flags=_re.MULTILINE | _re.IGNORECASE)
        if len(qa_segs) >= 2:
            for seg in qa_segs[1:]:
                seg = seg.strip()
                a_parts = _re.split(r'(?:^|\n)\s*answer\s*\d*\s*:\s*', seg, maxsplit=1, flags=_re.MULTILINE | _re.IGNORECASE)
                if len(a_parts) >= 2 and a_parts[0].strip() and a_parts[1].strip():
                    candidates.append({"q": a_parts[0].strip(), "a": a_parts[1].strip(), "ref": ref_tag})
                    if len(candidates) >= 10:
                        return candidates
            if candidates:
                continue

        # ── 路径D: 主持人/董事长/总经理 对话体（同行内如"主持人：...董事长：..."）──
        speaker_parts = _re.split(r'(?:(?:^|\n|。|！|？)\s*)?(?:主持人|公司董事长|副董事长|总经理|CEO|CFO)[^：:]{0,8}[：:]\s*', text)
        if len(speaker_parts) >= 3:  # 至少有2组发言
            # 第一段可能是导语，跳过
            for i in range(1, len(speaker_parts) - 1, 2):
                q_seg = speaker_parts[i].strip()
                a_seg = speaker_parts[i + 1].strip() if i + 1 < len(speaker_parts) else ""
                # 过滤：主持人/投资者发言 = Q，高管发言 = A
                if q_seg and a_seg and len(q_seg) > 10 and len(a_seg) > 20:
                    # 取最后一个完整句作为问题
                    q_sentences = _re.split(r'[。！？]', q_seg)
                    q = q_sentences[-1].strip() if len(q_sentences) > 1 else q_seg
                    if len(q) > 5:
                        candidates.append({"q": q[:200], "a": a_seg[:500], "ref": ref_tag})
                        if len(candidates) >= 10:
                            return candidates

        # ── 路径E: 序号罗列体（"1. 公司规划? \n 回答段落" 无答/回标记）──
        # 格力电器典型格式：数字序号 + 问句 + 连续段落回答，无换行
        # 序数词前锚点：行首/句号（覆盖"。 2.格力钛..."等嵌入式边界）
        items_e = _re.split(r'(?:(?:^|\n|。)\s*)\d+[、.]\s*', text)
        if len(items_e) >= 3:  # 第一段是导语/空段，至少2组Q&A
            for item in items_e[1:]:
                item = item.strip()
                if len(item) < 30:
                    continue
                # Q = 第一个?/？之前的内容；A = 之后到下一题或段落结尾
                m = _re.match(r'(.+?[?？])\s*(.*)', item, _re.DOTALL)
                if not m or len(m.group(1)) < 5:
                    continue
                q = m.group(1).strip()
                a = m.group(2).strip()
                # A 截断到合理长度并控制在500字以内
                if len(a) > 500:
                    cut = a[:500].rfind('。')
                    a = a[:cut + 1] if cut > 100 else a[:500] + '…'
                if len(a) > 10 and len(q) < 200:
                    candidates.append({"q": q, "a": a, "ref": ref_tag})
                    if len(candidates) >= 10:
                        return candidates

    return candidates


def _format_qa_markdown(selected: list, ref_tags: list) -> str:
    """v1.2.9: 确定性排版 Q&A 为 Markdown。LLM 完全不参与格式。

    selected: [{"q": str, "a": str, "ref": str (可选)}, ...]
    ref_tags: ["[N1]", "[N2]", ...] fallback—仅当 item 无 ref 时使用
    「已有 [N]」保护：若 A 末尾 30 字符内已有引用标记，不再追加。
    """
    import re as _re
    global_ref = "".join(ref_tags[:3]) if ref_tags else ""
    lines = []
    for item in selected:
        q = item["q"].strip()
        if q and _re.search(r'[一-鿿]$', q):
            q += '？'
        a = item["a"].strip()
        if len(a) > 400:
            cut = a[:400].rfind('。')
            a = a[:cut + 1] if cut > 150 else a[:400] + '…'
        # 引用：优先用 item 自带 ref，无则用全局 fallback
        item_ref = item.get("ref", "")
        ref = item_ref if item_ref else global_ref
        # Every rendered Q&A closes with the source bound to that pair. A
        # trailing citation embedded in a raw answer must not be reused.
        if ref:
            a = _re.sub(r'(?:\s*\[\d+\])+\s*$', '', a).rstrip()
            a = a + ref
        lines.append(f"**Q：** {q}")
        lines.append(f"**A：** {a}")
    return "\n\n".join(lines)


def _llm_fallback_extract_qa(qa_blocks: list, name: str, ref_tags: list, client) -> str:
    """正则提取失败时，用 LLM 从任意格式的调研文本中提取 Q&A 对。

    覆盖格式：纪要/报告体（五粮液典型)——问题嵌在小标题中、回答混在正文里，
    正则不可能理解这种语义关系，必须 LLM 处理。
    返回已格式化的 Markdown 字符串，失败返回空串。
    """
    raw_text = "\n\n---\n\n".join(qa_blocks[:4])

    prompt = f"""从以下{name}的机构调研/业绩说明会内容中提取 3-5 组真实问答，聚焦最有基本面投资价值的问题。

【原始内容】
{raw_text[:8000]}

【提取规则】
⚠️ 只提取原文真实存在的问答，严禁编造任何问题或回答。
⚠️ 回答超过180字需压缩到180字以内，保留核心数据与结论。
⚠️ 不要在回答中输出 [N] 等引用编号——引用由系统自动添加。

不同格式按以下方式处理：
• 若有 Q:/A: 标记 → 直接提取，压缩长回答
• 若为"1. 问题? ..."序号体 → 序号标题作 Q，紧跟段落作 A
• 若为"董事长讲话... (一)行业前景..."纪要体 → 从"投资者互动交流"部分提炼：小标题作 Q，正文压缩作 A

输出严格 JSON 数组（不要任何其他文字）：
[{{"q": "问题文本", "a": "压缩后回答（≤180字）"}}, ...]"""
    from datetime import datetime
    t0 = datetime.now()
    try:
        result = call_claude(client, prompt, max_tokens=800)
        json_match = re.search(r'\[[\s\S]*\]', result)
        if json_match:
            qa_list = json.loads(json_match.group(0))
            if isinstance(qa_list, list) and len(qa_list) >= 1:
                formatted = []
                for item in qa_list:
                    q = str(item.get("q", "")).strip()
                    a = str(item.get("a", "")).strip()
                    if q and a:
                        formatted.append({"q": q, "a": a})
                if formatted:
                    return _format_qa_markdown(formatted, ref_tags[:3] if ref_tags else [])
        # LLM 返回不可解析时，做一次 compact retry
        if not json_match or not formatted:
            retry = call_claude(client, prompt + "\n\n⚠️ 上次未返回有效JSON。请严格只输出 JSON 数组。", max_tokens=500)
            jm2 = re.search(r'\[[\s\S]*\]', retry)
            if jm2:
                qa2 = json.loads(jm2.group(0))
                if isinstance(qa2, list):
                    fmt2 = [{"q": str(x.get("q","")).strip(), "a": str(x.get("a","")).strip()} for x in qa2 if x.get("q") and x.get("a")]
                    if fmt2:
                        return _format_qa_markdown(fmt2, ref_tags[:3] if ref_tags else [])
        return ""
    except Exception:
        return ""


def _normalize_survey_qa_markdown(text: str) -> str:
    """v1.2.9: 精简兜底——仅处理已格式化 Q/A 文本的残余格式问题。

    主流程已改由 _extract_qa_candidates + _format_qa_markdown 确定生产出，
    本函数仅处理遗留/降级路径可能出现的零星格式脏数据。
    """
    if not text:
        return ""
    t = str(text).strip()
    t = re.sub(r'\r\n?', '\n', t)
    # 行首 Q/A 加粗归一化
    t = re.sub(r'(?m)^(\s*)(?:\*\*)?Q[:：](?:\*\*)?\s*', r'\1**Q：** ', t)
    t = re.sub(r'(?m)^(\s*)(?:\*\*)?A[:：](?:\*\*)?\s*', r'\1**A：** ', t)
    # 同行 Q/A 拆分（极少触发，仅兜底）
    t = re.sub(r'(?<!\n)(?<!\*)([QA])([:：]\*{1,2})(?!\w)', r'\n**\1\2', t)
    # 英文小写
    t = re.sub(r'(?m)^\s*question\s*:\s*', '**Q：** ', t)
    t = re.sub(r'(?m)^\s*answer\s*:\s*', '**A：** ', t)
    # 合并多余空行
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

    # ── 4.5 结构化生成：提取候选 → LLM 选+压缩 → 代码排版 ──
    qa_blocks = []
    for sv in surveys[:5]:
        if sv.get("content"):
            # 从 ref_map 回查该调研记录的引用编号
            sv_ref = ""
            eid = sv.get("event_id", "")
            if eid:
                sv_key = "survey_" + eid
                sv_n = ref_map.get(sv_key, {}).get("n", "")
                sv_ref = f"[{sv_n}]" if sv_n else ""
            qa_blocks.append(f"【{sv['date']} {sv['type']}（机构调研）】{sv_ref}\n{sv['content'][:3000]}")
    if len(qa_blocks) < 3:
        for m in meetings[:5]:
            if m.get("qa"):
                ref_key = "meeting_" + m["date"] + "_" + m["title"][:20]
                ref_n = ref_map.get(ref_key, {}).get("n", "")
                ref_tag = f"[{ref_n}]" if ref_n else ""
                qa_blocks.append(f"【{m['date']} {m['type']} {m['title']}】{ref_tag}\n{m['qa'][:2500]}")
    has_qa = bool(qa_blocks)

    # 确定性提取所有 Q&A 候选
    candidates = _extract_qa_candidates(qa_blocks) if has_qa else []
    all_refs = list({c["ref"] for c in candidates if c["ref"]})
    # 候选人 ref 为空时，从参与 qa_blocks 的 survey 的 ref_map 补齐
    if not all_refs:
        for sv in surveys[:5]:
            eid = sv.get("event_id", "")
            if eid:
                sv_n = str(ref_map.get("survey_" + eid, {}).get("n", ""))
                if sv_n and f"[{sv_n}]" not in all_refs:
                    all_refs.append(f"[{sv_n}]")

    # ── 4.1 盈利方式（始终独立生成）──
    profit_prompt = f"""为 {name} 撰写第4章 4.1 盈利方式。
{maincomp_ctx if maincomp_ctx else ""}
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

用2-3个 bullet（• 开头），格式：**[盈利维度]**：[结合本公司实际如何通过此维度赚钱]
先说行业通用盈利逻辑，再用本公司具体数字说明竞争位置；不重复4.2表格已有数字明细；标注引用；**120-160字以内**。
只输出盈利方式内容，不要带 ### 标题。
"""
    result_41 = call_claude(client, profit_prompt, max_tokens=600)
    s41 = re.sub(r'^###\s*4\.1[^\n]*\n?', '', result_41, flags=re.MULTILINE).strip()

    # ── 4.5 机构调研 Q&A（结构化生成）──
    s45 = ""
    if candidates:
        # 压缩候选列表给 LLM 选择
        candidates_text_lines = []
        for i, c in enumerate(candidates[:10]):
            a_preview = c["a"][:300] + ("…" if len(c["a"]) > 300 else "")
            candidates_text_lines.append(f"[{i}] Q: {c['q'][:150]}\n    A: {a_preview}")
        candidates_text = "\n".join(candidates_text_lines)

        qa_select_prompt = f"""从以下 {name} 的机构调研 Q&A 候选中，选出 3-4 组最有基本面投资价值的问答。

判断标准：涉及业绩增长驱动、竞争壁垒、新产品/新市场突破、成本趋势、股东回报 > 一般性行业展望。

对选中的每个回答，若原文超过 200 字则压缩到 200 字以内（保留核心数据和结论），否则保持原样。

输出严格的 JSON 数组，不要任何其他文字：
[{{"i": 候选序号, "a": "压缩后的回答文本"}}, ...]

候选列表：
{candidates_text}
"""
        try:
            result_qa = call_claude(client, qa_select_prompt, max_tokens=800)
            json_match = re.search(r'\[[\s\S]*\]', result_qa)
            if json_match:
                selected = json.loads(json_match.group(0))
                formatted = []
                for sel in selected:
                    idx = int(sel.get("i", -1))
                    if 0 <= idx < len(candidates):
                        formatted.append({
                            "q": candidates[idx]["q"],
                            "a": sel.get("a", candidates[idx]["a"])[:400],
                            "ref": candidates[idx].get("ref", ""),
                        })
                if 1 <= len(formatted) <= 5:
                    s45 = _format_qa_markdown(formatted, all_refs)
        except Exception:
            pass  # JSON 解析失败走兜底

        # 兜底：LLM 失败时直接取前 4 个候选
        if not s45:
            top4 = [{"q": c["q"], "a": c["a"], "ref": c.get("ref", "")} for c in candidates[:4]]
            s45 = _format_qa_markdown(top4, all_refs)

    elif has_qa:
        # 正则未提取到足够候选 → LLM 通用提取（覆盖纪要/报告体等无标记格式）
        s45 = _llm_fallback_extract_qa(qa_blocks, name, all_refs, client)
        if not s45:
            # LLM 也失败 → 最后兜底取首段原文（至少不丢数据）
            first = re.sub(r'^【.*?】\n?', '', qa_blocks[0], flags=re.MULTILINE) if qa_blocks else ""
            if first.strip():
                s45 = first.strip()[:500]

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
{_compact_reports(reports, n=3, ref_map=ref_map)}
{region_block}
{_fact_card_prompt_block(key_data)}
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

    asset_turn = revenue / total_assets if revenue is not None and total_assets not in (None, 0) else None
    eq_mult = total_assets / equity if total_assets is not None and equity not in (None, 0) else None
    dupont_roe = (net_margin / 100 * asset_turn * eq_mult * 100) if net_margin is not None and asset_turn is not None and eq_mult is not None else None
    # 加权 ROE 与期末口径杜邦值不可直接混称；差异过大时不输出误导性公式。
    dupont = (
        f"期末口径近似ROE：销售净利率{_pct(net_margin)} × 资产周转率{asset_turn:.3f}次 × 权益乘数{eq_mult:.2f} = {dupont_roe:.2f}%"
        if dupont_roe is not None and (roe is None or abs(dupont_roe - roe) <= 1.0)
        else "（杜邦口径与加权ROE差异较大，暂不展开）"
    )

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
摊薄ROE: {_pct(roe)}；加权平均ROE: {_pct(d.get('ROEW'))}

【引用映射】
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】**每条不超过100字**，输出以下四条，每条单独一行，紧凑精炼，**每条必须以加粗的小标题开头**（如 **盈利能力**：）：

【财务口径规则（强制执行）】
- 不得单独简写“营收”“净利”“ROE”“现金流”；必须写完整指标名称及口径。
- `tRevenue` 为营业总收入；`NPAttrP` 为归属于母公司股东的净利润；`netMargin` 为销售净利率；`ROEW` 为净资产收益率-加权平均；`operCashFlow` 为经营活动产生的现金流量净额。
- 若同时涉及“营业收入”与“营业总收入”，必须明确区分，禁止笼统称为“营收”。

⚠️ **因果归因规则（强制执行）**：
- fdmtNew结构化接口仅提供数字变化，**不提供因果解释**
- "主因""因为""导致""拖累""受益于"等因果表述，**必须**来自年报MD&A/公告/纪要/研报等文字来源
- 无因果证据时改为**中性描述**（如"净利同比下降X%""毛利率下滑X个百分点"），不自行归因
- 💡 **ROE与分红关系**：高分红减少净资产，在利润不变时**通常提高**当期ROE（分母缩小），而非拖累；可表述为"高分红可能限制未来资本积累和再投资能力"，**不得**使用"高分红直接拖累ROE"的错误表述

- **盈利能力**：净利率/ROE趋势，精确数字，标注引用。若需说明原因，必须引用MD&A/研报文字，否则只陈述变化幅度
- **偿债能力**：资产负债率/净资本充足性/偿债压力，标注引用
- **现金流质量**：经营现金流净额与净利润的比值（精确计算后直接写出数字），标注引用。若需说明原因，引用MD&A/研报
- **ROE杜邦分析（{y0}A）**：直接使用上方已提供的杜邦公式数据，明确写为“期末口径近似ROE”；仅可与“摊薄ROE”比较，必须说明“加权平均ROE采用不同期间口径，不直接比较”，标注引用
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
{_meeting_refs_str(meetings, ref_map, n=1)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]

【格式要求】**全节250-280字**，精炼专业。精选3-4个议题，每议题格式如下：

**议题N：[议题名称]**
背景：[1句话，不超过30字，含1个关键数字，标注引用]
• 问题1）：[≤60字，具体可量化]
• 问题2）：[≤60字，执行层面追问]

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
{_compact_reports(reports, n=3, ref_map=ref_map)}

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


def _has_valuation_data(valuation: dict) -> bool:
    """检查是否有任何有效的估值维度数据，所有维度均无效时跳过 9.3"""
    if not valuation or not isinstance(valuation.get("items"), dict):
        return False
    for dim_name, d in valuation["items"].items():
        v = d.get("val")
        if v is not None and str(v).strip() not in ("", "—", "0", "0.0"):
            return True
    return False


def _as_positive_float(value):
    """安全解析接口中的数值；无法解析或非正数时返回 None。"""
    try:
        parsed = float(re.sub(r"[,xX倍元\s]", "", str(value)))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _scenario_valuation_context(key_data: dict) -> dict:
    """Return the single valuation anchor shared by §9.3 and §9.4."""
    valuation = key_data.get("valuation") or {}
    items = valuation.get("items") or {}
    pe_item = items.get("市盈率PE") or {}
    pb_item = items.get("市净率PB") or {}
    pe_ttm = _as_positive_float(pe_item.get("val"))
    pb = _as_positive_float(pb_item.get("val"))
    pb_avg = _as_positive_float(pb_item.get("avg"))

    forecast_points = []
    for index, forecast in enumerate(key_data.get("consensus_forecasts") or []):
        eps = _as_positive_float(forecast.get("conEps"))
        pe = _as_positive_float(forecast.get("conPe"))
        if eps is None or pe is None:
            continue
        try:
            year = int(forecast.get("foreYear"))
        except (TypeError, ValueError):
            year = None
        forecast_points.append({
            "year": year,
            "eps": eps,
            "pe": pe,
            "implied_price": round(eps * pe, 4),
            "index": index,
        })

    baseline = forecast_points[0] if forecast_points else {}
    implied_prices = sorted(point["implied_price"] for point in forecast_points)
    anchor_price = anchor_spread = None
    if implied_prices:
        mid = len(implied_prices) // 2
        median_price = (
            implied_prices[mid]
            if len(implied_prices) % 2
            else (implied_prices[mid - 1] + implied_prices[mid]) / 2
        )
        anchor_price = round(median_price, 2)
        if anchor_price:
            anchor_spread = (max(implied_prices) - min(implied_prices)) / anchor_price

    actual = key_data.get("actual_consensus") or {}
    actual_eps = _as_positive_float(actual.get("conEps"))
    rank_implied_price = round(pe_ttm * actual_eps, 2) if pe_ttm and actual_eps else None
    rank_price_gap = None
    rank_reconciled = None
    if anchor_price and rank_implied_price:
        rank_price_gap = abs(rank_implied_price - anchor_price) / anchor_price
        rank_reconciled = rank_price_gap <= 0.15

    disable_reasons = []
    raw_pe = pe_item.get("val")
    try:
        if raw_pe is not None and float(raw_pe) <= 0:
            disable_reasons.append("PE(TTM)≤0")
    except (TypeError, ValueError):
        pass
    if pb is not None and pb_avg is not None and pb >= pb_avg * 3:
        disable_reasons.append(f"PB={pb:.2f}x，为行业均值{pb_avg:.2f}x的{pb / pb_avg:.1f}倍")

    # Quantitative target prices remain disabled until the fetch pipeline supplies a fully source-bound Bull/Base/Bear input set.
    quantitative_scenarios_allowed = False
    return {
        "pe_ttm": pe_ttm,
        "pb": pb,
        "pb_avg": pb_avg,
        "forecast_points": forecast_points,
        "baseline_eps": baseline.get("eps"),
        "baseline_pe": baseline.get("pe"),
        "baseline_year": baseline.get("year"),
        "anchor_price": anchor_price,
        "anchor_spread": anchor_spread,
        "actual_eps": actual_eps,
        "rank_implied_price": rank_implied_price,
        "rank_price_gap": rank_price_gap,
        "rank_reconciled": rank_reconciled,
        "quantitative_scenarios_allowed": quantitative_scenarios_allowed,
        "disable_reasons": disable_reasons,
    }


def _gen_section93(key_data: dict) -> str:
    """Render §9.3 deterministically so separate valuation APIs are not blended."""
    valuation = key_data.get("valuation") or {}
    items = valuation.get("items") or {}
    ref_map = key_data.get("ref_map") or {}
    valuation_ref = (ref_map.get("valuation_rank") or {}).get("n")
    consensus_ref = (ref_map.get("consensus") or {}).get("n")
    valuation_cite = f"[{valuation_ref}]" if valuation_ref else ""
    consensus_cite = f"[{consensus_ref}]" if consensus_ref else ""
    ctx = _scenario_valuation_context(key_data)

    rows = []
    priority = ["市盈率PE", "市净率PB", "市销率PS", "EV/EBITDA", "市现率PCF"]
    ordered_names = priority + [name for name in items if name not in priority]
    for name in ordered_names:
        item = items.get(name) or {}
        value = _as_positive_float(item.get("val"))
        if value is None:
            continue
        avg = _as_positive_float(item.get("avg"))
        rank = item.get("rank")
        rank_base = item.get("rankBase")
        level = f"{value:.2f}x"
        if avg is not None:
            level += f"；行业均值{avg:.2f}x"
        if rank and rank_base:
            level += f"；排名{rank}/{rank_base}"
        level += valuation_cite

        interpretations = []
        if avg is not None and avg:
            gap = (value / avg - 1) * 100
            direction = "溢价" if gap >= 0 else "折价"
            interpretations.append(f"较行业均值{direction}{abs(gap):.1f}%")
        if rank and rank_base:
            interpretations.append(f"同接口样本排名{rank}/{rank_base}")
        rows.append(f"| {name} | {level} | {'；'.join(interpretations) or '仅作同接口横向观察'} |")

    if not rows:
        return ""

    narrative = []
    anchor = ctx.get("anchor_price")
    if anchor:
        narrative.append(
            f"一致预期各预测年度EPS与对应PE隐含的统一价格锚约为{anchor:.2f}元{consensus_cite}。"
        )
    if ctx.get("rank_reconciled") is False:
        gap_pct = (ctx.get("rank_price_gap") or 0) * 100
        rank_price = ctx.get("rank_implied_price")
        narrative.append(
            f"估值排名接口PE按最新实际EPS折算约{rank_price:.2f}元{valuation_cite}{consensus_cite}，"
            f"与统一锚偏离{gap_pct:.1f}%；因接口口径未对齐，下表仅用于同接口横向比较，"
            "不作为当前定价、PEG或情景目标价依据。"
        )
    elif ctx.get("rank_reconciled") is True:
        gap_pct = (ctx.get("rank_price_gap") or 0) * 100
        narrative.append(
            f"估值排名接口与统一价格锚的隐含价格偏差为{gap_pct:.1f}%，可作交叉验证；"
            "下表仍仅解释同接口的行业相对位置。"
        )
    else:
        narrative.append(
            "估值排名接口缺少可与统一价格锚交叉核验的同口径实际EPS，"
            "下表仅用于同接口横向比较，不延伸为目标价或PEG判断。"
        )

    table = (
        "| 估值维度 | 接口口径值（横向） | 横向解读 |\n"
        "|:---------|:---------------------|:---------|\n"
        + "\n".join(rows)
    )
    return "### 9.3 估值分析\n\n" + "".join(narrative) + "\n\n" + table


def _scenario_factor(label: str) -> str:
    label = str(label or '')
    if any(word in label for word in ('直销', '经销', '渠道', 'i茅台', '直营')):
        return '渠道与消费者触达'
    if any(word in label for word in ('产能', '产量', '基酒', '系列酒')):
        return '供给与产品结构'
    if any(word in label for word in ('销量', '出货', '单价', '吨价', '价格', '批价')):
        return '量价表现'
    if '市占率' in label:
        return '竞争份额'
    if '毛利率' in label:
        return '盈利质量'
    return '经营指标'


def _scenario_fact_candidates(key_data: dict, max_items: int = 4) -> list:
    """选择不同传导维度的事实卡，避免同一渠道指标重复充当两个变量。"""
    selected, seen_factors, candidates = [], set(), []
    facts = key_data.get('fact_marker_refs') or {}
    priority = {
        '渠道与消费者触达': 0,
        '供给与产品结构': 1,
        '量价表现': 2,
        '竞争份额': 3,
        '盈利质量': 4,
        '经营指标': 5,
    }
    for marker, fact in facts.items():
        if not isinstance(fact, dict) or not fact.get('value') or not fact.get('ref'):
            continue
        factor = _scenario_factor(fact.get('label', ''))
        candidates.append((priority.get(factor, 99), str(marker), fact, factor))
    for _priority, marker, fact, factor in sorted(candidates, key=lambda item: item[0]):
        if factor in seen_factors:
            continue
        selected.append((marker, fact, factor))
        seen_factors.add(factor)
        if len(selected) >= max_items:
            break
    return selected


def _scenario_driver_label(fact: dict, factor: str) -> str:
    label = str(fact.get('label') or '').strip()
    if label == 'i茅台' and str(fact.get('value') or '').endswith('元'):
        return 'i茅台价格'
    if label in {'i茅台', '直营', '直销', '经销', '渠道'}:
        return f'{label}相关指标'
    if label:
        return label
    return factor


def _build_deterministic_section94(key_data: dict) -> str:
    """在 LLM 结构或溯源失败时，以同一批真实事实卡重建安全情景表。"""
    facts = _scenario_fact_candidates(key_data, max_items=2)
    if len(facts) < 2:
        return ''
    core_lines, assumptions = [], []
    for marker, fact, factor in facts:
        label = _scenario_driver_label(fact, factor)
        value = f"{fact['value']}[{int(fact['ref'])}]"
        core_lines.append(f'• **{label}**：{value}；反映{factor}的当前经营基础。')
        assumptions.append(label)
    good = '<br>'.join(f'{item}优于当前基础' for item in assumptions)
    base = '<br>'.join(f'{item}大体延续当前基础' for item in assumptions)
    bad = '<br>'.join(f'{item}弱于当前基础' for item in assumptions)
    return f'''### 9.4 情景推演

**核心变量**
{chr(10).join(core_lines)}

**情景推演表**：

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~25%） | {good} | 经营条件改善，收入、利润与现金流预期边际改善。 | 增长预期上修，估值中枢获得支撑。 |
| 中性（概率~50%） | {base} | 当前经营节奏大体延续，收入、利润与现金流按既有预期演变。 | 当前预期大体兑现，估值预期保持稳定。 |
| 悲观（概率~25%） | {bad} | 经营条件承压，收入、利润与现金流预期面临下修压力。 | 增长预期走弱，估值风险溢价上升。 |
'''


def _has_safe_scenario_input(key_data: dict) -> bool:
    """至少两项不同经营维度的来源绑定事实卡才允许生成 §9.4。"""
    return len(_scenario_fact_candidates(key_data, max_items=2)) >= 2


def _scenario_fact_card_prompt_block(key_data: dict) -> str:
    selected = {marker for marker, _fact, _factor in _scenario_fact_candidates(key_data)}
    lines = [
        line for line in str(key_data.get('operating_fact_cards') or '').splitlines()
        if any(f'{{{{FACT:{marker}}}}}' in line for marker in selected)
    ]
    return '\n'.join(lines) or '（无可核验经营事实卡；不得生成情景推演）'

def _gen_section94(client, key_data: dict) -> str:
    """Generate operating scenarios, never unsourced numerical target prices."""
    name = key_data["name"]

    valuation_context = "只讨论经营预期变化对估值中枢、风险溢价和市场预期的方向性影响。"

    prompt = f"""为{name}生成第9.4节情景推演。

【可核验经营事实卡】
{_scenario_fact_card_prompt_block(key_data)}

【统一估值口径】
{valuation_context}

【硬规则】
1. 只选2个真实业务驱动变量。当前基准值必须原样使用{{{{FACT:F编号}}}}，每条核心变量只保留该一个标记和定性传导说明，不得自行填写任何其他数字或引用。
2. 三档假设只写相对当前基准的方向与触发条件，如“订单兑现快于当前预期”“毛利率维持/承压”；不得编造新的销量、收入、利润、EPS、PE或股价数字。
3. 经营含义只写收入、利润、现金流的方向和传导路径，不写没有来源的预测值。
4. 核心假设、经营含义和估值含义都必须是纯文字判断；不得出现公式、EPS、PE、股价、目标价、估值倍数或任何数字。估值含义只讨论估值中枢、风险溢价和市场预期的方向性变化。
5. 严格输出4列表格，顺序为乐观/中性/悲观；概率分别25%/50%/25%，合计100%。
6. 不输出任何解释、注释、内部测算或额外章节。

严格按以下格式输出：

### 9.4 情景推演
**核心变量**
• **[变量1]**：{{{{FACT:F编号}}}}；[为什么影响公司经营]
• **[变量2]**：{{{{FACT:F编号}}}}；[为什么影响公司经营]

**情景推演表**：

| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:-----|:---------|:---------|:---------|
| 乐观（概率~25%） | [各变量向好条件，用<br>分隔] | [经营传导方向] | 增长预期上修，估值中枢获得支撑。 |
| 中性（概率~50%） | [各变量大体延续当前基准，用<br>分隔] | [经营传导方向] | 当前预期大体兑现，估值预期保持稳定。 |
| 悲观（概率~25%） | [各变量转弱条件，用<br>分隔] | [经营传导方向] | 增长预期走弱，估值风险溢价上升。 |
"""
    return call_claude(client, prompt, max_tokens=1000)


def _scenario_target_price_errors(section: str, key_data: dict) -> list:
    """Validate the safe qualitative §9.4 contract."""
    text = str(section or "")
    errors = []
    formula = re.search(r"(?:EPS|PE|PB|PS|EV/EBITDA)\s*(?:[＝=×x*]|乘以)|[＝=]\s*\d", text, re.I)
    forbidden_valuation = re.search(r"(?:目标价|目标价格|对应股价|每股价值|每股价格|股价)", text, re.I)
    if formula or forbidden_valuation:
        errors.append("情景推演只允许定性文字，不得输出公式、估值倍数或目标价相关表述")

    core_match = re.search(r"\*\*核心变量\*\*(.*?)(?=\*\*情景推演表\*\*)", text, re.S)
    core_lines = []
    if core_match:
        core_lines = [
            line for line in core_match.group(1).splitlines()
            if re.match(r"^\s*[-*•]\s+", line)
        ]
    if len(core_lines) < 2:
        errors.append("核心变量不足2项")
    elif any(not re.search(r"\d", line) or not re.search(r"\[\d+\]", line) for line in core_lines):
        errors.append("核心变量缺少来源化基准值")

    table_rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells and (cells[0] == "情景" or re.match(r"^(乐观|中性|悲观)", cells[0])):
            table_rows.append(cells)
    expected_header = ["情景", "核心假设", "经营含义", "估值含义"]
    if not table_rows or table_rows[0] != expected_header:
        errors.append("情景表必须严格使用四列表头")
        return errors

    scenario_rows = [
        row for row in table_rows[1:]
        if row and re.match(r"^(乐观|中性|悲观)", row[0])
    ]
    if len(scenario_rows) != 3 or any(len(row) != 4 for row in scenario_rows):
        errors.append("情景表必须包含三档且每行严格四列")
        return errors
    labels = [re.match(r"^(乐观|中性|悲观)", row[0]).group(1) for row in scenario_rows]
    if labels != ["乐观", "中性", "悲观"]:
        errors.append("三档情景顺序必须为乐观/中性/悲观")
    probabilities = []
    for row in scenario_rows:
        match = re.search(r"概率\s*[~～约]?\s*(\d+(?:\.\d+)?)%", row[0])
        if match:
            probabilities.append(float(match.group(1)))
    if len(probabilities) != 3 or abs(sum(probabilities) - 100) > 0.5:
        errors.append("三档情景概率必须完整且合计100%")
    for row in scenario_rows:
        for cell_name, cell in zip(expected_header[1:], row[1:]):
            plain_cell = re.sub(r'<br\s*/?>', '', cell, flags=re.I).strip()
            if not plain_cell:
                errors.append(f"{row[0]}档{cell_name}不能为空")
            if re.search(r"\d|(?:EPS|PE|PB|PS|目标价|股价|每股|[＝=×*])", plain_cell, re.I):
                errors.append(f"{row[0]}档{cell_name}必须为纯文字判断")
    return errors

def _normalize_section9_llm_fragment(fragment: str) -> str:
    """Accept only a substantive §9 fragment; never let a model inject a second H2."""
    text = str(fragment or "").strip()
    if not text:
        return ""
    text = re.sub(r'(?m)^##\s*9(?:\s|[\.、：:]).*\n?', '', text).strip()
    # A standalone H2/H3 is an empty shell, not usable section content.
    meaningful = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not re.match(r'^#{1,3}\s+', line.strip())
    ).strip()
    return text if len(re.sub(r'[|:\-\s]', '', meaningful)) >= 12 else ""

def gen_section9_valuation(client, key_data: dict) -> str:
    """9.3/9.4 估值分析 + 情景推演（v1.2.11：按数据可用性分别处理）

    - 估值维度全无效 → 跳过 9.3
    - 无一致预期 EPS/PE 且材料无可提取业务变量 → 跳过 9.4
    - 两者均跳过时返回空字符串，由组装处决定是否省略整章
    """
    parts = []

    if _has_valuation_data(key_data.get("valuation", {})):
        s93 = _normalize_section9_llm_fragment(_gen_section93(key_data))
        if s93:
            parts.append(s93)

    if _has_safe_scenario_input(key_data):
        s94 = _normalize_section9_llm_fragment(_gen_section94(client, key_data))
        if s94:
            parts.append(s94)

    if not parts:
        return ""

    # 两个都有时，中间加空行分隔
    return "\n\n".join(parts)


_A_SHARE_RISK_BULLET_RE = re.compile(r'^\s*[-*•]\s+', re.M)
_A_SHARE_RISK_SIGNAL_RE = re.compile(
    r'风险|不及预期|承压|下滑|下降|减值|回款|应收|库存|价格|批价|毛利率|净利率|'
    r'现金流|客户集中|客户依赖|订单|需求|原材料|汇率|产能|延期|延迟|诉讼|合规|'
    r'监管|政策|审批|版号|竞争加剧|波动|不确定'
)
_A_SHARE_GENERIC_RISK_PATTERNS = (
    r'核心业务需求若放缓',
    r'行业竞争加剧可能压缩',
    r'原材料.*渠道.*费用投入',
    r'宏观环境和政策变化可能影响',
    r'收入增长可能低于预期',
    r'关键假设不及预期的风险',
    r'数据缺失风险',
    r'模型不确定性风险',
    r'本报告不构成投资建议',
)


def _risk_ref_no(ref_map: dict, key: str) -> int:
    try:
        return int((ref_map.get(key) or {}).get("n") or 0)
    except (TypeError, ValueError):
        return 0


def _risk_sentences(text: str) -> list:
    clean = re.sub(r'<[^>]+>', ' ', str(text or ""))
    clean = re.sub(r'[ \t\r\f\v]+', ' ', clean)
    return [
        s.strip(" \t\r\n•*-：:")
        for s in re.split(r'[。\n；;]', clean)
        if 12 <= len(s.strip()) <= 180
    ]


def _explicit_risk_titles(sentence: str) -> list:
    """Split a source's explicit risk list into individually citable risk titles."""
    match = re.search(r'(?:风险提示|主要下行风险|下行风险)\s*[：:]\s*(.+)', str(sentence or ""))
    if not match:
        return []
    titles = []
    for item in re.split(r'[、，,；;]|以及', match.group(1)):
        title = re.sub(r'等$', '', item.strip(" \t\r\n。．·•*-：:（）()")).strip()
        title = re.sub(r'^(?:主要|相关|等)$', '', title).strip()
        title = re.sub(r'^(?:存在|公司)', '', title).strip()
        if 4 <= len(title) <= 20:
            titles.append(title)
    return titles


def _collect_a_share_risk_evidence(key_data: dict, max_items: int = 10) -> list:
    """Collect source-backed risk evidence, prioritising explicit risk disclosures."""
    ref_map = key_data.get("ref_map", {}) or {}
    explicit, general, seen = [], [], set()

    def add(text: str, ref_no: int, source: str) -> None:
        if ref_no <= 0:
            return
        for sentence in _risk_sentences(text):
            normalized = re.sub(r'\W+', '', sentence)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            titles = _explicit_risk_titles(sentence)
            if titles:
                for title in titles:
                    explicit.append({"text": sentence, "risk_title": title, "ref": ref_no, "source": source})
                continue
            if re.search(r'风险|不及预期|承压|下滑|下降|减值|回款|库存|延期|延迟|诉讼|合规|竞争加剧|不确定', sentence):
                general.append({"text": sentence, "ref": ref_no, "source": source})

    for report in (key_data.get("reports", []) or [])[:8]:
        report_ref = _risk_ref_no(ref_map, "report_" + str(report.get("id", "")))
        report_text = "\n".join(
            str(report.get(field) or "")
            for field in ("title", "detail_text", "abstract", "text")
        )
        add(report_text, report_ref, "研报")

    for meeting in (key_data.get("meetings", []) or [])[:4]:
        meeting_key = "meeting_" + str(meeting.get("date", "")) + "_" + str(meeting.get("title", ""))[:20]
        meeting_ref = _risk_ref_no(ref_map, meeting_key)
        meeting_text = "\n".join(
            str(meeting.get(field) or "")
            for field in ("title", "overview", "qa", "text")
        )
        add(meeting_text, meeting_ref, "纪要")

    for survey in (key_data.get("surveys", []) or [])[:4]:
        survey_ref = _risk_ref_no(ref_map, "survey_" + str(survey.get("event_id", "")))
        add(str(survey.get("content") or ""), survey_ref, "调研")

    return (explicit + general)[:max_items]

def _build_a_share_event_context(key_data: dict, max_items: int = 5) -> str:
    """Build source-backed recent-event context without relying on a parallel section result."""
    ref_map = key_data.get("ref_map", {}) or {}
    lines = []

    for report in (key_data.get("reports", []) or [])[:4]:
        ref_no = _risk_ref_no(ref_map, "report_" + str(report.get("id", "")))
        title = re.sub(r'\s+', ' ', str(report.get("title") or "")).strip()
        if ref_no and title:
            lines.append(f"- {title[:80]}[{ref_no}]")

    for meeting in (key_data.get("meetings", []) or [])[:3]:
        key = "meeting_" + str(meeting.get("date", "")) + "_" + str(meeting.get("title", ""))[:20]
        ref_no = _risk_ref_no(ref_map, key)
        summary = str(meeting.get("overview") or meeting.get("title") or "").strip()
        if ref_no and summary:
            clean_summary = re.sub(r'\s+', ' ', summary)
            lines.append(f"- {clean_summary[:100]}[{ref_no}]")

    for survey in (key_data.get("surveys", []) or [])[:2]:
        ref_no = _risk_ref_no(ref_map, "survey_" + str(survey.get("event_id", "")))
        content = str(survey.get("content") or "").strip()
        if ref_no and content:
            clean_content = re.sub(r'\s+', ' ', content)
            lines.append(f"- {clean_content[:100]}[{ref_no}]")

    return "\n".join(lines[:max_items])


def _latest_a_share_financial_snapshot(fin: dict, ref_map: dict) -> str:
    """Render the latest available A-share financial values with the fdmtNew citation."""
    latest = fin.get("latest_data") if isinstance(fin.get("latest_data"), dict) else None
    label = (fin.get("latest") or {}).get("label", "") if isinstance(fin.get("latest"), dict) else ""
    if not latest:
        years = [y for y in (fin.get("years") or []) if isinstance(fin.get(y), dict)]
        if years:
            year = max(years)
            latest = fin.get(year) or {}
            label = f"{year}A"
    if not latest:
        return ""

    ref_no = _risk_ref_no(ref_map, "fdmtNew")
    cite = f"[{ref_no}]" if ref_no else ""
    items = []
    amount_fields = (("tRevenue", "营业收入"), ("NPAttrP", "归母净利润"), ("operCashFlow", "经营现金流"))
    pct_fields = (
        ("revenueYOY", "营收同比"), ("NPAttrPYOY", "归母净利润同比"),
        ("grossMargin", "毛利率"), ("netMargin", "净利率"),
        ("ROE", "ROE"), ("liabRatio", "资产负债率"),
    )
    for key, label_cn in amount_fields:
        value = latest.get(key)
        if isinstance(value, (int, float)):
            items.append(f"{label_cn}{value / 1e8:.2f}亿元{cite}")
    for key, label_cn in pct_fields:
        value = latest.get(key)
        if isinstance(value, (int, float)):
            items.append(f"{label_cn}{value:.2f}%{cite}")

    prefix = f"{label}：" if label else ""
    return prefix + "；".join(items[:8])


def _derive_a_share_risk_title(sentence: str, company_name: str = "") -> str:
    clean = re.sub(r'\[\d+\]|\*\*', '', str(sentence or ""))
    clean = re.sub(r'^(?:主要)?风险(?:提示)?[:：]?', '', clean).strip()
    if company_name:
        clean = clean.replace(company_name, "").strip()
    clean = re.sub(r'^(?:公司|我们认为|我们预计|预计|若|如果|存在)', '', clean).strip()
    candidate = re.split(r'可能|或将|若|如果|导致|影响|存在', clean, maxsplit=1)[0].strip("，,：:；; ")
    if len(candidate) < 4:
        candidate = clean
    candidate = re.sub(r'[，,。；;：:].*$', '', candidate).strip()
    return candidate[:24].rstrip("的") or "公司特有风险"


def _is_valid_a_share_risk_title(title: str) -> bool:
    title = str(title or "").strip()
    if not 3 <= len(title) <= 24:
        return False
    if re.fullmatch(r'(?:因此|同时|具体来看|此外|其中|一是|二是|三是|首先|其次|最后|\d{4}年.*)', title):
        return False
    return bool(re.search(r'风险|不及预期|下滑|下降|受阻|削减|放缓|竞争|波动|库存|减值|回款|价格|批价|需求|政策|税|替代|延期|延迟', title))


def _risk_theme_key(title: str) -> str:
    """按可投资的风险传导主题去重，避免同一需求/价格风险换词重复出现。"""
    title = str(title or "")
    themes = (
        (r'库存|减值', 'inventory'),
        (r'需求|订单|客户|销量|出货', 'demand'),
        (r'价格|批价|竞争', 'price_competition'),
        (r'原材料|成本', 'input_cost'),
        (r'产能|延期|延迟', 'capacity_execution'),
        (r'政策|监管|税|合规|审批', 'policy'),
        (r'汇率|海外|贸易', 'overseas'),
        (r'回购', 'buyback'),
    )
    for pattern, theme in themes:
        if re.search(pattern, title):
            return theme
    return re.sub(r'\W+', '', title)[:18]


def _risk_tracking_profile(title: str) -> tuple:
    """将来源明确的风险标题转换为不含虚构数字的投研跟踪框架。"""
    title = str(title or "")
    profiles = (
        (r'库存|减值', ('下游去化弱于备货节奏', '库存周转承压并可能增加减值压力', '库存、周转天数和资产减值损失')),
        (r'需求|订单|客户|销量|出货', ('终端需求或订单兑现弱于预期', '出货节奏放缓可能拖累收入与产能利用率', '订单、出货和渠道库存')),
        (r'价格|批价|竞争', ('行业供给释放或价格竞争加剧', '产品价格与盈利空间可能承压', '产品价格、毛利率和市场份额')),
        (r'原材料|成本', ('核心原材料价格波动超预期', '成本传导滞后可能压缩盈利能力', '原材料价格、采购成本和毛利率')),
        (r'产能|延期|延迟', ('项目建设或产能投放进度不及预期', '新增供给释放放缓并影响业务兑现节奏', '项目进度、产能利用率和投产安排')),
        (r'政策|监管|税|合规|审批', ('政策规则或审批节奏出现变化', '业务推进成本或市场准入预期可能承压', '政策落地、审批进度和公司应对措施')),
        (r'汇率|海外|贸易', ('海外政策或汇率波动超预期', '海外业务盈利与扩张节奏可能受扰动', '政策进展、汇率和海外订单')),
        (r'回购', ('回购计划执行节奏或规模不及预期', '市场预期支撑减弱，估值情绪可能承压', '回购公告、执行进度和注销安排')),
    )
    for pattern, profile in profiles:
        if re.search(pattern, title):
            return profile
    return ('风险事项持续或超出当前预期', '经营预期与估值判断可能承压', '相关经营指标及公司后续披露')


def _compose_investor_risk_explanation(trigger: str, impact: str, monitor: str) -> str:
    """统一渲染可执行的风险表述，避免无来源阈值和空泛占位语。"""
    trigger = re.sub(r'\s+', ' ', str(trigger or '')).strip('，,；;。：: ')
    impact = re.sub(r'\s+', ' ', str(impact or '')).strip('，,；;。：: ')
    monitor = re.sub(r'\s+', ' ', str(monitor or '')).strip('，,；;。：: ')
    if not trigger or not impact or not monitor:
        return ''
    if not re.match(r'^(?:若|当|在|受)', trigger):
        trigger = '若' + trigger
    return f'{trigger}，{impact}；重点跟踪{monitor}。'


def _build_a_share_risk_fallback(key_data: dict, max_items: int = 4) -> str:
    """以真实风险标题为锚，重建含触发、影响和跟踪项的来源化风险。"""
    name = str(key_data.get("short_name") or key_data.get("name") or "")
    lines = []
    titles, themes = set(), set()
    for item in _collect_a_share_risk_evidence(key_data, max_items=16):
        title = str(item.get("risk_title") or _derive_a_share_risk_title(item["text"], name)).strip()
        theme = _risk_theme_key(title)
        if not _is_valid_a_share_risk_title(title) or title in titles or theme in themes:
            continue
        trigger, impact, monitor = _risk_tracking_profile(title)
        body = _compose_investor_risk_explanation(trigger, impact, monitor)
        if not body:
            continue
        titles.add(title)
        themes.add(theme)
        lines.append(f"• **{title}**：{body}[{item['ref']}]")
        if len(lines) >= max_items:
            break
    rendered = "\n".join(lines)
    valid, _ = _validate_a_share_risk_body(rendered)
    return rendered if valid else ""

def _validate_a_share_risk_body(body: str) -> tuple:
    issues = []
    lines = [line.strip() for line in str(body or "").splitlines() if _A_SHARE_RISK_BULLET_RE.match(line)]
    if not 2 <= len(lines) <= 5:
        issues.append(f"risk_bullet_count:{len(lines)}")
    if any(re.search(pattern, body) for pattern in _A_SHARE_GENERIC_RISK_PATTERNS):
        issues.append("generic_risk_template")
    titles = []
    for idx, line in enumerate(lines):
        title_match = re.search(r'\*\*([^*]{3,24})\*\*', line)
        if not title_match:
            issues.append(f"risk[{idx}].missing_bold_title")
        else:
            titles.append(title_match.group(1).strip())
        if not re.search(r'\*\*[^*]+\*\*\s*[：:]\s*.{6,}', line):
            issues.append(f"risk[{idx}].missing_explanation")
        if not re.search(r'若.{2,}[，,].{4,}[；;]重点跟踪.{2,}', line):
            issues.append(f"risk[{idx}].missing_trigger_impact_monitor")
        if not re.search(r'\[\d+\]', line):
            issues.append(f"risk[{idx}].missing_citation")
        plain_len = len(re.sub(r'\[\d+\]|\*\*|\s+', '', line))
        if plain_len > 140:
            issues.append(f"risk[{idx}].too_long:{plain_len}")
    if len(set(titles)) != len(titles):
        issues.append("duplicate_risk_titles")
    if len({_risk_theme_key(title) for title in titles}) != len(titles):
        issues.append("duplicate_risk_themes")
    return not issues, issues


def _enforce_a_share_risk_section(md_content: str, key_data: dict) -> str:
    risk_pat = r'(## 10 风险提示\n\n)(.*?)(?=\n## 参考资料|\n---\n\n## 参考资料)'
    match = re.search(risk_pat, md_content, re.DOTALL)
    if not match:
        return md_content
    valid, issues = _validate_a_share_risk_body(match.group(2))
    if valid:
        return md_content
    fallback = _build_a_share_risk_fallback(key_data)
    if fallback:
        print(f"  ⚠ v1.2.10: §10 风险提示校验失败，使用带引用的公司证据重建：{issues}")
        return re.sub(risk_pat, lambda m: m.group(1) + fallback, md_content, count=1, flags=re.DOTALL)
    print(f"  ⚠ v1.2.10: §10 风险提示校验失败且证据不足，交由最终自检阻断：{issues}")
    return md_content


def _valid_ref_numbers(ref_map: dict) -> set:
    """从 ref_map 提取所有合法引用编号（即各 entry 的 'n' 值）。"""
    nums = set()
    for v in (ref_map or {}).values():
        if isinstance(v, dict):
            n = v.get("n")
            if isinstance(n, int) and n > 0:
                nums.add(n)
    return nums


def _validate_render_section_10(payload: dict, ref_map: dict) -> tuple:
    """从 JSON payload 校验并渲染 A 股 §10 风险提示。返回 (markdown, issues)。"""
    issues = []
    valid_nums = _valid_ref_numbers(ref_map)
    risks = payload.get("risks") if isinstance(payload, dict) else None
    if not isinstance(risks, list) or not (2 <= len(risks) <= 5):
        return "", [f"risk_count:{0 if not isinstance(risks, list) else len(risks)}"]
    lines = []
    for i, risk in enumerate(risks):
        if not isinstance(risk, dict):
            issues.append(f"risk[{i}].not_object")
            continue
        title = str(risk.get("title") or "").strip()
        if len(title) < 3 or len(title) > 24:
            issues.append(f"risk[{i}].title_len:{len(title)}")
        trigger = str(risk.get("trigger") or "").strip()
        impact = str(risk.get("impact") or "").strip()
        monitor = str(risk.get("monitor") or "").strip()
        explanation = _compose_investor_risk_explanation(trigger, impact, monitor)
        for field_name, field_value in (("trigger", trigger), ("impact", impact), ("monitor", monitor)):
            if len(field_value) < 3:
                issues.append(f"risk[{i}].{field_name}_len:{len(field_value)}")
        if len(explanation) < 12:
            issues.append(f"risk[{i}].invalid_investor_explanation")
        refs = risk.get("source_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(n, int) and n > 0 for n in refs):
            issues.append(f"risk[{i}].invalid_refs")
            refs = []
        else:
            # v1.2.10+: 清洗无效引用而非整条丢弃——保留合法引用编号
            clean_refs = [n for n in refs if n in valid_nums]
            if not clean_refs:
                issues.append(f"risk[{i}].no_valid_refs")
            refs = clean_refs
        combined = title + explanation
        if any(re.search(p, combined) for p in _A_SHARE_GENERIC_RISK_PATTERNS):
            issues.append(f"risk[{i}].generic")
        if title and explanation and refs and not any(
            re.search(p, combined) for p in _A_SHARE_GENERIC_RISK_PATTERNS
        ):
            ref_str = "".join(f"[{n}]" for n in refs)
            lines.append(f"• **{title}**：{explanation}{ref_str}")
    if len(lines) < 2:
        issues.append(f"valid_lines:{len(lines)}")
    return ("\n".join(lines), []) if lines and not issues else ("", issues)


def gen_section10(client, key_data: dict) -> str:
    """10 风险提示（JSON schema，2-5条；失败则交 fallback 证据重建）。"""
    reports = key_data["reports"]
    fin = key_data["fin"]
    name = key_data["name"]
    ref_map = key_data["ref_map"]
    risk_evidence = _collect_a_share_risk_evidence(key_data)
    risk_context = "\n".join(f"- {item['text']}[{item['ref']}]" for item in risk_evidence[:8])
    event_context = _build_a_share_event_context(key_data)
    fin_snapshot = _latest_a_share_financial_snapshot(fin, ref_map)

    issues = []
    for call_name in ("risk_json", "risk_json_repair"):
        prompt = f"""Return ONLY JSON for A-share report §10 risk section of {name}.
Schema: {{"risks": [{{"title": "不超过20个中文字的风险小标题", "trigger": "触发条件", "impact": "对经营或估值的影响路径", "monitor": "后续跟踪的指标或事件", "source_refs": [1]}}]}}
Rules: output 2-5 source-backed risks. For every risk, trigger / impact / monitor are all required and must form a concise investor checklist after rendering: “若触发条件，影响路径；重点跟踪跟踪项”。Use only the context refs below. A monitor may be a qualitative observable (for example, order, inventory, project progress or policy implementation), but never invent a number, threshold, customer or product detail absent from the cited evidence. Competition, demand and macro risks are allowed when the cited evidence explicitly ties them to this company; reject unsupported generic boilerplate.

⚠️ FORBIDDEN generic risk patterns:
  - "核心业务需求若放缓"
  - "行业竞争加剧可能压缩"
  - "原材料、渠道或费用投入"
  - "宏观环境和政策变化"

Risk evidence (cite only real refs here):
{risk_context or "（no risk evidence extracted — do not fabricate）"}

Recent events:
{event_context or "（no events）"}

Financial snapshot:
{fin_snapshot or "（no financial data）"}

Report excerpts:
{_compact_reports(reports[:4])}

VALID REF NUMBERS (only these integers may appear in source_refs):
{sorted(_valid_ref_numbers(ref_map))}

Citation map:
{_refs_str(reports, ref_map, 4)}
fdmtNew=[{ref_map.get('fdmtNew',{}).get('n','')}]
"""
        if call_name.endswith("repair") and issues:
            prompt += f"\nSchema issues to fix:\n" + "\n".join(issues)

        raw = call_claude(client, prompt, max_tokens=1000)
        payload = None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                try:
                    payload = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if not isinstance(payload, dict):
            issues.append(f"{call_name}:json_parse_failed")
            continue
        rendered, render_issues = _validate_render_section_10(payload, ref_map)
        if rendered and not render_issues:
            return rendered
        issues.extend(f"{call_name}:{x}" for x in render_issues)

    # JSON 路径失败，记录具体拒因后由 enforcer 使用来源化 fallback。
    print(f"  ⚠ v1.2.31: §10 JSON路径未通过，转来源化fallback：{issues[:8]}")
    return ""

_A_SHARE_PEER_HEADERS = ['竞争关系', '公司（代码）', '市场', '可比业务', '行业地位', '相关业务进展', '商业模式', '目标客户群体', '核心产品']
_PEER_PROGRESS_MAX_CHARS = 60

def _peer_progress_refs(key_data: dict) -> dict:
    """返回 {peer_code: [引用编号]}，只供同业表的“相关业务进展”列使用。"""
    refs = key_data.get('ref_map') or {}
    grouped = {}
    for index, item, _name, code in _peer_material_entries(key_data.get('_raw_data') or {}):
        ref_no = refs.get(_peer_material_ref_key(index, item), {}).get('n')
        if ref_no:
            grouped.setdefault(code, []).append(int(ref_no))
    return grouped


def _target_progress_materials(key_data: dict, max_items: int = 2) -> tuple:
    """返回标的公司进展列可使用的研报证据及其引用编号。"""
    refs, lines = set(), []
    for report in key_data.get('reports') or []:
        report_id = str(report.get('id') or '')
        ref_no = (key_data.get('ref_map') or {}).get(f'report_{report_id}', {}).get('n')
        source = ' '.join(str(report.get(key) or '') for key in ('title', 'detail_text', 'abstract', 'text'))
        if not ref_no or not source.strip():
            continue
        refs.add(int(ref_no))
        lines.append(f'[{int(ref_no)}] {source[:1200]}')
        if len(lines) >= max_items:
            break
    return refs, '\n'.join(lines)


def _compact_peer_progress(cell: str, max_chars: int = _PEER_PROGRESS_MAX_CHARS) -> str:
    """保留短而完整的来源化业务进展；超长或财务化单元格不作硬截断。"""
    raw = re.sub(r'\s+', ' ', str(cell or '')).strip()
    refs = []
    for ref in re.findall(r'\[(\d+)\]', raw):
        if ref not in refs:
            refs.append(ref)
    body = re.sub(r'\[\d+\]', '', raw).strip(' ，,;；')
    if not body or not refs:
        return '—'
    if '…' in body:
        return '—'
    if len(body) > max_chars:
        complete_sentences = [
            sentence.strip() for sentence in re.split(r'(?<=[。！？；;])', body)
            if sentence.strip() and len(sentence.strip()) <= max_chars
        ]
        if not complete_sentences:
            return '—'
        body = complete_sentences[0]
    finance_terms = r'营业总收入|营业收入|归属于母公司股东的净利润|归母净利润|净利润|毛利率|净利率|同比|环比|EPS|PE|亿元|万元'
    business_terms = r'新品|产品|渠道|价格|产能|订单|客户|出货|项目|技术|市场|份额|组织|改革|投产|发布|上市|推进|导入|认证|扩张|布局|运营|经营'
    if re.search(finance_terms, body, re.I) and not re.search(business_terms, body):
        return '—'
    return body + ''.join(f'[{ref}]' for ref in refs)


def _render_peer_table(rows: list, include_progress: bool) -> str:
    """渲染固定 schema；进展列无基准证据时整体省略。"""
    header = list(_A_SHARE_PEER_HEADERS)
    rendered_rows = [list(row) for row in rows]
    if not include_progress:
        del header[5]
        rendered_rows = [row[:5] + row[6:] for row in rendered_rows]
    sep = '|' + '|'.join([':---'] * len(header)) + '|'
    return '\n'.join([
        '| ' + ' | '.join(header) + ' |',
        sep,
        *['| ' + ' | '.join(row) + ' |' for row in rendered_rows],
    ])


def _validate_peer_table(table_md: str, name: str, ticker: str, allowed_peers: list, progress_refs: dict, target_progress_refs=None) -> str:
    """仅约束公司身份和进展来源；非进展列保留模型的定性研究画像。"""
    parsed = _parse_markdown_table(table_md or '')
    if not parsed or parsed.get('col_count') != len(_A_SHARE_PEER_HEADERS):
        return ''
    if parsed.get('header_cells') != _A_SHARE_PEER_HEADERS:
        return ''
    rows = parsed.get('rows') or []
    if len(rows) < 3 or ticker not in rows[0][1] or name not in rows[0][1]:
        return ''

    target_allowed_refs = {int(n) for n in (target_progress_refs or [])}
    cleaned_rows = [list(rows[0])]
    target_actual_refs = {int(n) for n in re.findall(r'\[(\d+)\]', cleaned_rows[0][5])}
    if target_allowed_refs and target_actual_refs and target_actual_refs.issubset(target_allowed_refs):
        cleaned_rows[0][5] = _compact_peer_progress(cleaned_rows[0][5])
    else:
        cleaned_rows[0][5] = '—'

    allowed = {str(item.get('code')): str(item.get('current_name')) for item in allowed_peers}
    found = set()
    for row in rows[1:]:
        cleaned = list(row)
        company_cell, progress_cell = cleaned[1], cleaned[5]
        codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', company_cell)
        if len(codes) != 1 or codes[0] not in allowed or allowed[codes[0]] not in company_cell:
            return ''
        code = codes[0]
        if code in found:
            return ''
        found.add(code)
        required_refs = set(progress_refs.get(code) or [])
        actual_refs = {int(n) for n in re.findall(r'\[(\d+)\]', progress_cell)}
        if required_refs and actual_refs and actual_refs.issubset(required_refs):
            cleaned[5] = _compact_peer_progress(progress_cell)
        else:
            cleaned[5] = '—'
        cleaned_rows.append(cleaned)
    if len(found) < 2:
        return ''

    # 进展是唯一必须逐格引源的列。基准没有可靠来源时整列跳过，避免首行
    # 空白且不让无来源信息进入交付物；其他研究维度仍保留。
    return _render_peer_table(cleaned_rows, include_progress=cleaned_rows[0][5] != '—')


def gen_peer_table(client, key_data: dict) -> str:
    """生成来源绑定的同业表；无法取得两家代码候选时整节跳过。"""
    name, ticker = key_data['name'], key_data.get('ticker', '')
    peers = key_data.get('peer_validated') or []
    if len(peers) < 2:
        return ''
    mc = key_data.get('mc', {})
    primary_biz = '、'.join(list(mc.get('segments') or {})[:3])
    progress_refs = _peer_progress_refs(key_data)
    target_progress_refs, target_materials = _target_progress_materials(key_data)
    materials_by_code = {}
    for index, item, _peer_name, peer_code in _peer_material_entries(key_data.get('_raw_data') or {}):
        ref_no = (key_data.get('ref_map') or {}).get(_peer_material_ref_key(index, item), {}).get('n')
        if ref_no:
            text = ' '.join(str(item.get(k, '') or '') for k in ('title', 'text', 'content', 'summary', 'abstract'))
            materials_by_code.setdefault(peer_code, []).append(f'[{ref_no}] {text[:1200]}')
    peer_lines = []
    for peer in peers:
        code, peer_name = str(peer.get('code') or ''), str(peer.get('current_name') or '')
        evidence = '\n'.join(materials_by_code.get(code, [])[:2]) or '无可核验进展材料：相关业务进展列必须填“—”。'
        peer_lines.append(f'【{peer_name}（{code}）】\n{evidence}')
    header = '| ' + ' | '.join(_A_SHARE_PEER_HEADERS) + ' |'
    sep = '|' + '|'.join([':---'] * len(_A_SHARE_PEER_HEADERS)) + '|'
    allowed_text = '\n'.join(f"- {p['current_name']}（{p['code']}）" for p in peers)
    target_evidence = target_materials or '无可核验标的进展材料：基准行进展填“—”，程序会删除整列。'
    prompt = f"""为 {name} 生成来源受限的同业比较表，只输出 Markdown 表格。

标的基准：{name}（{ticker}）；主营业务：{primary_biz or '见主营构成'}。
允许的可比公司仅限以下名单，禁止新增、替换或凭行业知识补充任何公司：
{allowed_text}

【标的公司定向材料（仅可用于基准行“相关业务进展”）】
{target_evidence}

【可比公司定向材料（仅可用于对应 peer 的“相关业务进展”）】
{chr(10).join(peer_lines)}

{header}
{sep}

规则：
1. 第一行必须为 {name}（{ticker}），竞争关系填“—（基准）”；其后逐一列出上述全部可比公司。
2. 每行严格9列；除“相关业务进展”外，其余列可基于公司公开常识和行业常识写简洁的定性画像，无需引用。避免编造精确财务数字、排名或客户名单；仅在确无合理描述时填“—”。
3. “相关业务进展”是唯一需要引用的列。基准行只可使用标的定向材料，peer 行只可使用自身定向材料。每格仅一句完整业务进展，正文控制在20–60个汉字、引用置末尾；不得用省略号或截断句。优先新品、产品结构、渠道、价格、产能、订单、客户导入、技术、组织改革或市场份额。
4. 相关业务进展不得以营业总收入、归属于母公司股东的净利润、毛利率或同比等财务数据为主体；可在业务事件后以一句短背景补充。材料没有明确业务事件时，可依据该公司自身定向材料作克制的业务动向归纳并标注该材料来源，但不得虚构具体产品、客户、产能、订单或市场份额。
5. 不要使用目标公司研报、常识或推测为可比公司补写业务进展；不要把多篇观点、正反判断或整段研报塞进一个单元格。
"""
    result = re.sub(r'\[research\]', '', call_claude(client, prompt, max_tokens=1500) or '')
    return _validate_peer_table(result, name, ticker, peers, progress_refs, target_progress_refs)


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


def _valid_title_conclusion(text: str, short_name: str = "") -> bool:
    """Keep title quality checks structural; wording should not depend on a keyword whitelist."""
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
    # The generation prompt asks for an investment conclusion. Do not reject an
    # otherwise complete viewpoint merely because it omits a fixed word such as
    # “驱动” or “修复”; those words remain useful only in the deterministic fallback.
    return True


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
        return ""
    return text


def _build_deterministic_fallback_title(sections: dict, short_name: str, ticker: str) -> str:
    """Build title conclusion from already-generated sections (v1.2.6-R3: no hardcoded strings).

    A-share section layout: s1=近况跟踪, s2=核心投资逻辑, s3=催化事件时间表.
    §2 is the richest source for investment thesis titles.
    """
    s1 = str(sections.get("s1", ""))
    s2 = str(sections.get("s2", ""))  # ← A-share: 核心投资逻辑
    s3 = str(sections.get("s3", ""))  # ← A-share: 催化事件时间表 (table, skip)
    judgment_terms = (
        "驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量",
        "商业化", "变现", "渗透", "增速", "加速", "提升", "放量", "超预期",
        "增长", "成长", "领先", "优势", "布局", "落地", "兑现", "验证",
        "承压", "风险", "扩张", "拓展", "回购", "分红", "利润率",
        "提价", "定价", "改革", "回报", "品牌", "渠道", "旺季", "弹性",
    )
    # 1) A-share §2 核心投资逻辑: **加粗子标题**
    bold_titles = re.findall(r'\*\*([^*]{4,22})\*\*', s2[:2500])
    for h in bold_titles:
        h = h.strip()
        zh_len = len(re.findall(r'[一-鿿]', h))
        if 8 <= zh_len <= 25 and any(t in h for t in judgment_terms):
            return h
    # 2) A-share §2 的 H2/H3 子标题
    logic_h = re.findall(r'#{2,3}\s*\d*\.?\d*\s*(.+?)(?:[（(]|$)', s2[:2000], re.M)
    for h in logic_h:
        h = re.sub(r'\*\*|[\[\]\d+]', '', h).strip()
        zh_len = len(re.findall(r'[一-鿿]', h))
        if 8 <= zh_len <= 25 and any(t in h for t in judgment_terms):
            return h
    # 3) §1 近况跟踪 bullet 首句
    bullets = re.findall(r'^[•\*\-]\s*(.+)$', s1[:800], re.M)
    for b in bullets:
        b = re.sub(r'\[\d+\]|\*\*', '', b).strip()
        core = re.split(r'[，,。；;]', b)[0].strip()
        zh_len = len(re.findall(r'[一-鿿]', core))
        if 8 <= zh_len <= 25 and any(t in core for t in judgment_terms):
            return core
    # 4) 兜底：拼接加粗关键词
    kp_matches = re.findall(r'\*\*([^*]{4,15})\*\*', s2 + s1)
    kws = [m.strip() for m in kp_matches if any(t in m for t in judgment_terms)][:2]
    if len(kws) >= 2:
        return "与".join(kws[:2]) + "双轮驱动"
    if kws:
        return kws[0]
    return ""


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
        "chain": "- **上游**：关注关键原材料、技术和服务供给。\n- **中游**：关注公司制造、服务和运营效率。\n- **下游**：关注客户需求、渠道库存和价格变化。",
        "questions": "- 核心业务收入和订单趋势如何？\n- 毛利率和费用率变化是否可持续？\n- 行业竞争格局是否影响价格？\n- 现金流和资本开支是否匹配增长节奏？",
        "profit": "公司收入来自主营业务披露的核心产品和服务，利润弹性取决于收入增长、毛利率和费用率控制。",
        "deep": "业务深度分析应围绕当前公司主营业务、客户结构、成本和竞争格局展开，禁止复用其他行业模板。",
    }


def assemble_report(meta: dict, sections: dict, ref_map: dict) -> str:
    """将所有章节组装为完整 Markdown 报告"""
    name = meta.get("name", "")
    short_name = meta.get("short_name", name)
    ticker = meta.get("ticker", "")
    date = TODAY
    conclusion = _sanitize_title_conclusion(sections.get("title_conclusion", ""), short_name, ticker)
    if not conclusion:
        conclusion = _build_deterministic_fallback_title(sections, short_name, ticker)
    title_line = f"# {short_name}（{ticker}）公司一页纸：{conclusion}" if conclusion else f"# {short_name}（{ticker}）公司一页纸"

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

    # 9.1 表格：只有有数据才显示（v1.2.11: 无数据时跳过整节，对齐 9.2）
    consensus_table = sections.get("consensus_table", "")
    section_9_1 = ""
    if consensus_table and "暂缺" not in str(consensus_table) and len(consensus_table.strip()) > 20:
        section_9_1 = f"""
### 9.1 市场一致预期

**数据来源**：research_sec_coredata接口[{ref_map.get('consensus',{}).get('n','')}]

{consensus_table}
"""

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

    # ── 第八章：8.1 与 8.2 独立降级，缺少 peer 不得带走行业格局 ──
    s8_industry = sections.get("s8_industry", "").strip()
    peer_table = sections.get("peer_table", "").strip()
    peer_section_block = f"""
### 8.2 同业比较

{peer_table}
""" if peer_table else ""
    _chapter_8_block = ""
    if s8_industry or peer_section_block:
        _chapter_8_block = (
            "## 8 行业分析及同业对比\n\n"
            + (f"### 8.1 行业格局\n\n{s8_industry}\n\n" if s8_industry else "")
            + peer_section_block
            + "\n"
        )
    # ── 第九章整章条件拼接（v1.2.11：任一小节无数据则单独跳过，全空则整章不出现）──
    _ch9_parts = []
    if section_9_1:
        _ch9_parts.append(section_9_1.strip())
    if section_9_2:
        _ch9_parts.append(section_9_2.strip())
    _s9v = sections.get('s9_valuation', '').strip()
    if _s9v:
        _ch9_parts.append(_s9v)
    _chapter_9_block = ""
    if _ch9_parts:
        _chapter_9_block = "## 9 一致预期、盈利预测与估值\n\n" + "\n\n".join(_ch9_parts) + "\n\n"
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

{_strip_header_prefix(sections['s1'])}

## 2 核心投资逻辑

{_strip_header_prefix(sections['s2'])}

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

{_chapter_8_block}{_chapter_9_block}## 10 风险提示

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


def _postprocess_report(md_content: str, ref_map: dict) -> str:
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
            # A peer comparison table is a fixed research schema, not a sparse
            # numeric table. Preserve the nine research dimensions; the sourced
            # progress column may be omitted as a whole when the baseline has no
            # auditable progress material.
            header_without_progress = [cell for index, cell in enumerate(_A_SHARE_PEER_HEADERS) if index != 5]
            is_peer_comparison = header == _A_SHARE_PEER_HEADERS or header == header_without_progress
            if not is_peer_comparison:
                normalized = _drop_all_empty_table_columns(normalized)
            out.extend(normalized)
    return '\n'.join(out)


def _replace_empty_h2_body(md_content: str, number: int, replacement: str) -> str:
    """Replace an empty H2 body using the next H2 as the only boundary.

    Post-processing intentionally removes visual `---` separators, so recovery must
    never depend on them.
    """
    pattern = rf'(?ms)^(##\s+{number}\s+[^\n]+)\n*(.*?)(?=^##\s+\d+\s+|\Z)'
    match = re.search(pattern, md_content)
    if not match or match.group(2).strip():
        return md_content
    return md_content[:match.start()] + match.group(1) + "\n\n" + replacement.strip() + "\n\n" + md_content[match.end():]


def _replace_empty_h3_body(md_content: str, number: str, replacement: str) -> str:
    """Replace an empty H3 body without relying on removable separators."""
    pattern = rf'(?ms)^(###\s+{re.escape(number)}\s+[^\n]+)\n*(.*?)(?=^###\s+\d+\.\d+\s+|^##\s+\d+\s+|\Z)'
    match = re.search(pattern, md_content)
    if not match or match.group(2).strip():
        return md_content
    return md_content[:match.start()] + match.group(1) + "\n\n" + replacement.strip() + "\n\n" + md_content[match.end():]


def _first_report_citation(key_data: dict, md_content: str = ""):
    """Resolve a report's current citation after post-processing re-numbering."""
    ref_map = key_data.get("ref_map", {}) or {}
    for report in key_data.get("reports", []) or []:
        title = re.sub(r'\s+', ' ', str(report.get("title") or "")).strip()
        if md_content and title:
            match = re.search(r'^\[(\d+)\].*' + re.escape(title), md_content, re.MULTILINE)
            if match:
                return title, int(match.group(1))
        ref_no = _risk_ref_no(ref_map, "report_" + str(report.get("id", "")))
        if ref_no and title:
            return title, ref_no
    return "", 0


def _build_a_share_chain_fallback(key_data: dict, current_main_ref: str = "") -> str:
    """Minimal source-backed §5 fallback; it contains no fabricated supply-chain facts."""
    ref_no = str(current_main_ref or "").strip("[]")
    if not ref_no:
        ref_no = str(_risk_ref_no(key_data.get("ref_map", {}) or {}, "maincomp") or "")
    segments = [str(x) for x in (key_data.get("mc", {}) or {}).get("segments", {}).keys() if "差额" not in str(x) and "计算" not in str(x)][:3]
    if not ref_no or not segments:
        return ""
    names = "、".join(str(x) for x in segments)
    return (
        f"• **主营业务锚点**：公司主营构成披露的主要业务包括{names}[{ref_no}]。\n"
        "• **链条跟踪重点**：后续应结合公司披露，持续核验上游关键投入、产能与交付节奏、下游需求及渠道库存的变化。"
    )


def _build_a_share_questions_fallback(key_data: dict, md_content: str = "") -> str:
    """Ground §7 questions in a real report title instead of a static template."""
    title, ref_no = _first_report_citation(key_data, md_content)
    if not ref_no:
        return ""
    return (
        f"1. **经营验证**：围绕《{title}》提及的核心经营变化，最新订单、收入确认与回款节奏如何？[{ref_no}]\n"
        "2. **盈利质量**：产品结构、毛利率和费用率的边际变化能否持续？\n"
        "3. **竞争跟踪**：行业供需、价格与竞争格局变化将如何影响公司兑现节奏？\n"
        "4. **风险边界**：管理层将以哪些可量化指标跟踪库存、现金流和资本开支？"
    )


def _build_a_share_industry_fallback(key_data: dict, md_content: str = "") -> str:
    """Keep §8 non-empty with a cited research anchor when the peer table is unavailable."""
    title, ref_no = _first_report_citation(key_data, md_content)
    if not ref_no:
        return ""
    return (
        f"• **行业跟踪材料**：《{title}》提供了本公司所处产业的研究锚点[{ref_no}]。\n"
        "• **跟踪重点**：后续应基于可核验材料持续观察行业景气、竞争格局与估值传导；同业数据不足时不虚构可比公司表。"
    )

def _enforce_a_share_required_blocks(md_content: str, key_data: dict, ref_map: dict) -> str:
    """Final deterministic guard for A-share RC output blocks."""
    name = key_data.get("name", "")
    ticker = key_data.get("ticker", "")
    short_name = key_data.get("short_name", name) or name
    profile = _a_share_profile(short_name, ticker, key_data)
    main_ref_match = re.search(r'^\[(\d+)\].*主营构成', md_content, re.M)
    main_ref_no = main_ref_match.group(1) if main_ref_match else ref_map.get("maincomp", {}).get("n", "")
    main_ref = f"[{main_ref_no}]" if main_ref_no else "[3]"

    # Remove LLM short-error leakage from the title and body.
    md_content = re.sub(r'^(# .+?公司一页纸：)User Points Not Enough\s*$',
                        r'\1', md_content, flags=re.M)
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

    # §8.2 已在生成阶段按候选公司和定向来源校验；此处禁止二次兜底重建。

    # v1.2.9: §2.1 短期逻辑标题强制检测（LLM 格式漂移时可能丢失）
    _s2_match = re.search(r'(## 2 核心投资逻辑\n\n)(.*?)(?=\n### 2\.2|\n## 3 )', md_content, re.DOTALL)
    if _s2_match:
        _s2_body = _s2_match.group(2)
        if '### 2.1' not in _s2_body and '2.1' not in _s2_body:
            # 在 ## 2 后的第一个内容行前插入 ### 2.1
            md_content = md_content.replace(
                _s2_match.group(0),
                _s2_match.group(1) + "### 2.1 短期逻辑\n\n" + _s2_body,
                1
            )

    # v1.2.10: preserve valid •/-/* bullets; otherwise rebuild only from cited company evidence.
    md_content = _enforce_a_share_risk_section(md_content, key_data)

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
    # `_postprocess_report` has already removed `---`, so section recovery is based
    # on heading boundaries only.  Fall back only to cited/source-derived text.
    _chain_fallback = _build_a_share_chain_fallback(key_data, main_ref_no)
    if _chain_fallback:
        md_content = _replace_empty_h2_body(md_content, 5, _chain_fallback)
    md_content = _replace_empty_h3_body(
        md_content, "6.2",
        "公司财务健康度需重点跟踪收入增速、净利率、经营现金流和资本开支匹配度；若高端产品占比提升与现金流同步改善，盈利质量更具持续性。"
    )
    _questions_fallback = _build_a_share_questions_fallback(key_data, md_content)
    if _questions_fallback:
        md_content = _replace_empty_h2_body(md_content, 7, _questions_fallback)
    _industry_fallback = _build_a_share_industry_fallback(key_data, md_content)
    if _industry_fallback:
        md_content = _replace_empty_h2_body(md_content, 8, _industry_fallback)

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
    # 注意：表格行必须写成 [^\n]*（每行只有唯一切分方式），不能用 `.+\|.*?`。
    # 后者在一行内有 k 个竖线时就有 k 种切法，外层 (?:...)* 套 m 行 → k^m 条回溯路径；
    # 一旦末尾 (?:\n|$) 匹配失败（表格后面紧跟非空行），引擎会把这 k^m 条全试一遍，
    # 实测 6 列表格 8 行需 1.7s、12 行超 5 分钟、16 行以上等同于挂死。
    # 同理分隔行不能用 [-:\s|]，\s 会吃掉 \n 从而跨行，再次引入歧义。
    table_pattern = re.compile(
        r'(^```[^\n]*\n)?^\|[^\n]*\n(?:^\|[-:| \t]+\|?[ \t]*\n)(?:^\|[^\n]*\n)*(?:\n|$)',
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

def _drop_invalid_section94(md_text: str, key_data: dict) -> str:
    """修复或移除不合格的 §9.4；兜底仍只使用来源绑定事实卡。"""
    text = str(md_text or "")
    match = re.search(r"(?m)^###\s*9\.4\s*情景推演\s*$", text)
    if not match:
        return text
    tail = text[match.end():]
    next_heading = re.search(r"(?m)^###\s+9\.\d+\s+|^##\s+", tail)
    end = match.end() + (next_heading.start() if next_heading else len(tail))
    fragment = text[match.start():end]
    errors = _scenario_target_price_errors(fragment, key_data)
    if not errors:
        return text
    fallback = _build_deterministic_section94(key_data)
    if fallback and not _scenario_target_price_errors(fallback, key_data):
        print(f"  ⚠ v1.2.34: §9.4 LLM产物不合格（{errors}），改用来源绑定的确定性情景表")
        return text[:match.start()].rstrip() + "\n\n" + fallback.strip() + "\n\n" + text[end:].lstrip()
    print(f"  ⚠ v1.2.33: 删除不满足安全情景规则的§9.4：{errors}")
    return text[:match.start()].rstrip() + "\n\n" + text[end:].lstrip()

def _drop_empty_optional_section9(md_text: str) -> str:
    """Remove only an empty optional §9 after all table/LLM cleanup has completed."""
    pattern = r'(?ms)^##\s+9\s+[^\n]+\n*(.*?)(?=^##\s+\d+\s+|\Z)'
    match = re.search(pattern, str(md_text or ""))
    if not match or match.group(1).strip():
        return md_text
    return md_text[:match.start()] + md_text[match.end():].lstrip("\n")

def _drop_incomplete_optional_scenarios(md_text: str) -> str:
    """Fail closed on incomplete §9.4 and scenario rows that escaped §9.

    Scenario analysis is optional. A partial table is worse than an omitted section:
    it can be rendered with a data row as the DOCX header or become attached to the
    preceding chapter after a cleanup pass. Keep §9.1–§9.3 intact and remove only
    an invalid §9.4 fragment or an orphaned scenario table.
    """
    text = str(md_text or "")
    section9 = None
    h2_matches = list(re.finditer(r'^##\s+(\d+)\s+.+$', text, re.MULTILINE))
    for index, heading in enumerate(h2_matches):
        if heading.group(1) == "9":
            section9 = (heading.start(), h2_matches[index + 1].start() if index + 1 < len(h2_matches) else len(text))
            break
    if section9:
        start, end = section9
        block = text[start:end]
        match = re.search(r'^###\s*9\.4\s*情景推演\s*$', block, re.MULTILINE)
        if match:
            fragment_start = match.start()
            next_heading = re.search(r'^###\s+9\.\d+\s+|^##\s+', block[match.end():], re.MULTILINE)
            fragment_end = match.end() + (next_heading.start() if next_heading else len(block) - match.end())
            fragment = block[fragment_start:fragment_end]
            labels = set(re.findall(r'^\|\s*(乐观|中性|悲观)', fragment, re.MULTILINE))
            has_header = bool(re.search(r'^\|\s*情景\s*\|', fragment, re.MULTILINE))
            if labels != {"乐观", "中性", "悲观"} or not has_header:
                block = block[:fragment_start] + block[fragment_end:]
                text = text[:start] + block + text[end:]

    # No scenario table is valid outside §9. Remove its contiguous table block,
    # including a possible orphan separator row, without touching prose.
    lines = text.splitlines()
    output = []
    current_h2 = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        h2 = re.match(r'^##\s+(\d+)\s+', line)
        if h2:
            current_h2 = h2.group(1)
        if current_h2 != "9" and line.lstrip().startswith("|") and re.search(r'\|\s*(?:乐观|中性|悲观)', line):
            while output and (not output[-1].strip() or output[-1].lstrip().startswith("|")):
                output.pop()
            while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("|")):
                index += 1
            continue
        output.append(line)
        index += 1
    return "\n".join(output) + ("\n" if text.endswith("\n") else "")

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


def _dedent_body_paragraphs(md_text: str) -> str:
    """v1.2.8-R1: 去除正文段落的前导缩进空格。

    保留 markdown 构造行（标题/表格/列表/代码/图片/引用/分隔线）不做处理，
    仅去除普通正文行的前导空白字符。
    """
    if not md_text:
        return md_text
    lines = md_text.split('\n')
    result = []
    for line in lines:
        s = line.strip()
        if not s:
            result.append(line)
            continue
        # 保留 markdown 构造行：标题、表格、无序列表、有序列表、代码、图片、引用、分隔线
        first_char = s[0]
        if first_char in '#|-*>![`' or s.startswith('```') or s.startswith('---'):
            result.append(line)
            continue
        # 有序列表：数字开头 + 点号/顿号
        if re.match(r'^\d+[\.\、\)]', s):
            result.append(line)
            continue
        # 普通正文行 → 去前导空白
        result.append(line.lstrip())
    return '\n'.join(result)


def _fix_truncated_chinese(md_text: str) -> str:
    """v1.2.9: 检测并修复中文截断——加粗文本末尾断字、段落末尾缺标点。

    两种常见截断模式：
    1. **加粗文本** 末尾是残缺中文（如 "**板块估值修复契**" 缺 "机"字）
    2. 正文段落末尾无中文标点（。！？；）」】…）结尾
    修复策略：移除残缺加粗片段、在截断处标注。
    """
    if not md_text:
        return md_text

    def _chinese_end_ok(end_char: str) -> bool:
        """中文正常结尾：标点、英文、数字、右括号、空白"""
        if not end_char:
            return True
        if end_char in '。！？；）」】…\'"”':
            return True
        if 'a' <= end_char.lower() <= 'z' or '0' <= end_char <= '9':
            return True
        if end_char in '>%‰‱':
            return True
        return False

    # 模式1: 已移除——加粗纯中文无法区分完整词与截断词（如"契机"vs"契"），
    # 导致大量误报（"出海规模化放量构筑利润引擎"被误标为截断）。
    # 中文截断检测仅保留模式2（短段落末尾）。

    # 模式2: 短段落末尾截断检测——仅检查短行（≤50字）末尾为汉字无标点的情况
    lines = md_text.split('\n')
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue
        # 跳过非正文行
        if (re.match(r'^#{1,6}\s', stripped)
                or stripped.startswith('|')
                or re.match(r'^[-*•·●►]\s', stripped)
                or stripped.startswith('```')
                or stripped.startswith('![')
                or re.match(r'^\[v?\d', stripped)
                or stripped.startswith('**Q：')
                or stripped.startswith('**A：')):
            result.append(line)
            continue
        # 仅短行（≤50字）末为纯汉字才检查：如长段末位是正常正文结束，不处理
        last_char = stripped[-1] if stripped else ''
        if len(stripped) <= 50 and '一' <= last_char <= '鿿':
            result.append(line.rstrip() + '…')
        else:
            result.append(line)
    return '\n'.join(result)


def _normalize_bullet_markers(md_text: str) -> str:
    """v1.2.9: 统一列表标记为无缩进 •，消除 -/*/1）2）3）混用。

    只处理行首（允许前导空白）的列表标记，不处理表格/代码/图片/标题。
    """
    if not md_text:
        return md_text
    lines = md_text.split('\n')
    result = []
    for line in lines:
        s = line.strip()
        if not s:
            result.append(line)
            continue
        # 跳过非正文行
        if s.startswith('#') or s.startswith('|') or s.startswith('![') or s.startswith('```'):
            result.append(line)
            continue
        # 转换: "- text" / "* text" → "• text"
        m = re.match(r'^(\s*)(-|\*)\s+(.+)$', line)
        if m:
            result.append(f'{m.group(1)}• {m.group(3)}')
            continue
        # 转换: "1）text" / "1) text" / "2）text" / "3）text" → "• text"
        m = re.match(r'^(\s*)[123][\)）]\s*(.+)$', line)
        if m:
            result.append(f'{m.group(1)}• {m.group(2)}')
            continue
        result.append(line)
    return '\n'.join(result)


def _normalize_final_markdown_format(md_text: str) -> str:
    """Final format guardrails for LLM markdown drift.
    v1.2.8-R1: 正则正文段落去除前导缩进空格，Q/A 换行强化。
    v1.2.9: 增加中文截断检测；v1.2.10: 风险列表统一识别 •/-/*。"""
    if not md_text:
        return md_text
    md_text = _strip_bold_from_markdown_headings(md_text)
    md_text = _normalize_section1_recent_format(md_text)
    # v1.2.9: 清理 LLM 输出的 "第X节：..." 占位文本（prompt 要求不输出但 LLM 仍可能泄漏）
    # 覆盖两种变体：纯文本 "第1节：公司近况跟踪"/"第1节：公司近况跟踪…" 和加粗 "**第1节：公司近况跟踪**"
    md_text = re.sub(r'(?m)^\*{0,2}第\d节：[一-龥A-Za-z]+…?\*{0,2}\s*$', '', md_text)
    # v1.2.9: 列表标记归一化（-/*/1）2）3）→ • 无缩进）——必须在 dedent 之前，保证 • 行被后续去缩进处理
    md_text = _normalize_bullet_markers(md_text)
    # v1.2.8-R1: 去除正文段落前导缩进（非标题/非表格/非列表/非代码/非图片行）
    md_text = _dedent_body_paragraphs(md_text)
    # v1.2.9: 中文截断检测与修复
    md_text = _fix_truncated_chinese(md_text)
    return md_text


def _renumber_a_share_subsections(md_text: str) -> str:
    """在删节后，按 H2 重排直属 H3 编号，并规范第7章的调研议题标题。"""
    current_h2 = None
    next_h3 = 1
    out = []
    for line in str(md_text or "").split("\n"):
        h2 = re.match(r'^(##)\s+(\d+)\s+(.+)$', line)
        if h2:
            current_h2 = h2.group(2)
            next_h3 = 1
            out.append(line)
            continue
        h3 = re.match(r'^(###)\s+\d+\.\d+\s+(.+)$', line)
        if h3 and current_h2:
            out.append(f"### {current_h2}.{next_h3} {h3.group(2)}")
            next_h3 += 1
            continue
        # 第7章由 LLM 输出时常将议题写成加粗行而非 H3；在此确定性提升为小节，
        # 使后续编号重排和 DOCX 样式均遵循统一章节结构。
        topic = re.match(r'^\s*(?:\*\*)?议题\s*\d+\s*[：:]\s*(.+?)(?:\*\*)?\s*$', line)
        if current_h2 == "7" and topic:
            title = topic.group(1).strip().strip("*").strip()
            if title:
                out.append(f"### 7.{next_h3} {title}")
                next_h3 += 1
                continue
        out.append(line)
    return "\n".join(out)

_OPERATIONAL_NUMERIC_TERMS = (
    "销量", "产量", "出货", "装机", "吨价", "单价", "直销", "经销", "渠道占比",
    "市占率", "产能", "利用率", "系列酒", "基酒", "客户数", "门店数", "渗透率", "员工", "职工",
)
_FINANCIAL_ONLY_APIS = {
    "fdmtNew", "fdmt_indi_rtn", "research_sec_coredata", "research_sec_foredata",
    "diagnosis_valuation_rank", "diagnosis_pe_valuation",
}
_QUANTITATIVE_TOKEN_RE = re.compile(
    r'(?<!\d)(\d+(?:\.\d+)?)\s*(%|pct|万吨|万台|吨|亿元|亿|万元|万|元/吨|元|名|人|家|项)'
)
_FACT_MARKER_RE = re.compile(r'\{\{FACT:([A-Z]\d+)\}\}')


def _build_operating_fact_cards(key_data: dict, max_cards: int = 30) -> tuple:
    """从原始来源提取可程序化渲染的高风险事实卡。

    LLM 只能在报告/纪要/MD&A承载的精确数字位置输出 ``{{FACT:F1}}``；
    渲染阶段再由程序写入原文数值和对应引用，避免句末按语义贴号。
    """
    evidence = _build_reference_evidence(key_data)
    allowed_apis = {
        "batchGetReportContent", "getMeetingSummaryDetail",
        "management_discussion", "getFdmtMoStdItem",
    }
    unit_re = r'(%|pct|万吨|万台|吨|亿元|亿|万元|万|元/吨|元|名|人|家|项)'
    token_re = re.compile(r'(?<!\d)([-+]?\d+(?:\.\d+)?)\s*' + unit_re)
    grouped_re = re.compile(r'([-+]?\d+(?:\.\d+)?(?:\s*/\s*[-+]?\d+(?:\.\d+)?){1,5})\s*' + unit_re)
    # §9.4 的核心变量必须是经营驱动，而不是营收、净利润或券商预测结果。
    # 这些卡同时作为确定性情景兜底的唯一数值来源。
    fact_terms = ("销量", "产量", "出货", "吨价", "单价", "直销", "经销", "渠道占比", "市占率", "产能", "系列酒", "基酒", "渠道", "直营", "i茅台", "毛利率", "价格", "批价")
    primary_terms = set(fact_terms)

    candidates, seen = [], set()
    for ref_no in sorted(evidence):
        src = evidence[ref_no]
        if src.get("api") not in allowed_apis:
            continue
        text = re.sub(r'\s+', ' ', str(src.get("text") or ""))
        for sentence in re.split(r'(?<=[。；;！？])', text):
            terms = [term for term in fact_terms if term in sentence]
            # 预测营收/利润是结果，不应伪装成场景驱动的当前基准值。
            if not terms or re.search(r'预计|预测|一致预期|CAGR|forecast', sentence, re.I):
                continue
            grouped = list(grouped_re.finditer(sentence))
            tokens = [
                (f"{m.group(1)}{m.group(2)}", m.start())
                for m in token_re.finditer(sentence)
                if not any(group.start() <= m.start() < group.end() for group in grouped)
            ]
            # “375.75/172.74亿元”这类并列数字共用单位：优先保留首项，
            # 既保留该句的核心指标，又避免同一句的并列数字占满来源配额。
            grouped_positions = set()
            for group in grouped:
                number = next(re.finditer(r'[-+]?\d+(?:\.\d+)?', group.group(1)), None)
                if number:
                    group_pos = group.start(1) + number.start()
                    tokens.append((f"{number.group(0)}{group.group(2)}", group_pos))
                    grouped_positions.add(group_pos)
            for display, pos in tokens:
                # 经营驱动卡不接受“亿元/亿/万元”收入、利润类金额；即使句中
                # 恰好出现渠道或产品词，也不能将结果指标错配为驱动变量。
                if re.search(r'(?:亿元|亿|万元)$', display):
                    continue
                positions = [(term, match.start()) for term in terms for match in re.finditer(re.escape(term), sentence)]
                nearest_term, nearest_pos = min(positions, key=lambda x: abs(pos - x[1]))
                nearest = abs(pos - nearest_pos)
                if nearest > 120:
                    continue
                key = (int(ref_no), display, nearest_term, pos)
                if key in seen:
                    continue
                seen.add(key)
                score = (10 if nearest_term in primary_terms else 5) + max(0, 120 - nearest) / 20
                if pos in grouped_positions:
                    score += 20  # 共用单位的首项通常是该句最需要引用的核心指标。
                start = max(0, pos - 110)
                end = min(len(sentence), pos + len(display) + 150)
                snippet = sentence[start:end].strip(" ，,;；")
                candidates.append({
                    "score": score, "ref": int(ref_no), "value": display,
                    "api": src.get("api", ""), "snippet": snippet,
                    "label": nearest_term,
                })

    # 相同来源的同一个数值只保留相关性最高的一张卡，避免 61% 等重复事实挤掉唯一数据。
    best_by_value = {}
    for item in candidates:
        identity = (item["ref"], item["value"])
        if identity not in best_by_value or item["score"] > best_by_value[identity]["score"]:
            best_by_value[identity] = item
    candidates = list(best_by_value.values())

    # 每个来源选最高相关的四项，并按来源轮转取数：每个唯一宿主都有机会进入 prompt。
    by_source = {}
    for item in candidates:
        by_source.setdefault(item["ref"], []).append(item)
    selected, selected_ids = [], set()
    ranked_by_source = {
        ref: sorted(items, key=lambda x: -x["score"])[:4]
        for ref, items in by_source.items()
    }
    for rank in range(4):
        for ref_no in sorted(ranked_by_source):
            items = ranked_by_source[ref_no]
            if rank >= len(items) or len(selected) >= max_cards:
                continue
            item = items[rank]
            identity = (item["ref"], item["value"])
            if identity not in selected_ids:
                selected.append(item)
                selected_ids.add(identity)
    for item in sorted(candidates, key=lambda x: (-x["score"], x["ref"])):
        identity = (item["ref"], item["value"])
        if identity in selected_ids or len(selected) >= max_cards:
            continue
        selected.append(item)
        selected_ids.add(identity)

    cards, fact_map = [], {}
    for item in selected[:max_cards]:
        marker = f"F{len(fact_map) + 1}"
        fact_map[marker] = {
            "value": item["value"], "ref": item["ref"], "api": item["api"],
            "label": item.get("label", "经营指标"),
        }
        cards.append(
            f"{{{{FACT:{marker}}}}} = {item['value']}[{item['ref']}] | "
            f"来源:{item['api']} | 原文:{item['snippet'][:300]}"
        )
    return "\n".join(cards) if cards else "（无可核验经营事实卡；不得编造经营数字）", fact_map

def _render_fact_markers(md_content: str, fact_map: dict) -> str:
    """把 LLM 输出的事实标记替换为程序绑定的“数值[N]”。"""
    def replace(match):
        fact = (fact_map or {}).get(match.group(1))
        if not fact:
            return match.group(0)
        return f"{fact['value']}[{fact['ref']}]"
    return _FACT_MARKER_RE.sub(replace, str(md_content or ""))


def _build_reference_evidence(key_data: dict, md_content: str = "") -> dict:
    """建立最终 [N] -> 原始来源文本/API 的审计索引。

    后处理会按正文首次出现顺序重编号引用，因此不能直接把最终 [N]
    当作 ref_map 的原始 n；必须先以最终“参考资料”中的 ID/API 反查来源。
    """
    raw = key_data.get("_raw_data") or {}
    refs = key_data.get("ref_map") or {}
    reports = {str(x.get("id")): x for x in (key_data.get("reports") or [])}
    meetings = {str(x.get("id")): x for x in (key_data.get("meetings") or [])}
    surveys = {str(x.get("event_id")): x for x in (key_data.get("surveys") or [])}
    api_data_keys = {
        "fdmtNew": "financial", "getFdmtMoStdItem": "main_comp",
        "research_sec_coredata": "consensus", "research_sec_foredata": "profit_forecast",
        "diagnosis_valuation_rank": "valuation_rank", "diagnosis_pe_valuation": "pe_valuation",
        "management_discussion": "mgmt_discussion", "fdmt_indi_rtn": "fin_indicators",
    }
    original_entries = list(refs.values())

    def source_for(entry: dict) -> dict:
        """从原始 ref_map entry 构建不可伪造的来源证据。"""
        source_type = entry.get("type", "")
        api_name = entry.get("api_name", "")
        source = ""
        if source_type == "结构化数据":
            dataset = raw.get(api_data_keys.get(api_name, ""), {})
            source = json.dumps(dataset, ensure_ascii=False)
            # 将接口原始金额同时转为报告使用的亿元口径，避免“原始元值 vs 正文亿元值”误拦。
            if api_name == "getFdmtMoStdItem":
                rows = dataset.get("data", []) if isinstance(dataset, dict) else []
                normalized = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rev = row.get("revenue")
                    margin = row.get("grossMargin")
                    if isinstance(rev, (int, float)):
                        normalized.append(f"{row.get('endDate','')} {row.get('itemName','')} 收入{rev / 1e8:.2f}亿元")
                    if isinstance(margin, (int, float)):
                        normalized.append(f"{row.get('endDate','')} {row.get('itemName','')} 毛利率{margin:.2f}%")
                source += "\n" + "\n".join(normalized)
        elif source_type == "研报":
            item = reports.get(str(entry.get("id", "")), {})
            source = " ".join(str(item.get(k, "") or "") for k in ("title", "abstract", "detail_text", "text"))
            api_name = "batchGetReportContent"
        elif source_type == "纪要":
            item = meetings.get(str(entry.get("id", "")), {})
            source = " ".join(str(item.get(k, "") or "") for k in ("title", "overview", "qa", "text"))
            api_name = "getMeetingSummaryDetail"
        elif source_type == "调研":
            item = surveys.get(str(entry.get("id", "")), {})
            source = str(item.get("content", "") or "")
            api_name = "institution_research_detail"
        elif source_type == "同业材料":
            material_index = entry.get("source_index")
            materials = raw.get("peer_materials") or []
            item = materials[material_index] if isinstance(material_index, int) and 0 <= material_index < len(materials) else {}
            source = " ".join(str(item.get(k, "") or "") for k in ("title", "text", "content", "summary", "abstract"))
            api_name = "getMaterialsV2"
        return {"api": api_name, "type": source_type, "text": source}

    # 最终参考资料的编号优先：结构化数据以 API 匹配，非结构化数据以类型+ID 匹配。
    final_entries = {}
    if md_content and "## 参考资料" in md_content:
        ref_section = md_content.rsplit("## 参考资料", 1)[1]
        for raw_line in ref_section.splitlines():
            match = re.match(r'^\[(\d+)\](.*)$', raw_line.strip())
            if not match:
                continue
            final_no, rendered = int(match.group(1)), match.group(2)
            candidates = []
            api_match = re.search(r'API：[^(（\s|]+', rendered)
            if "Datayes结构化接口" in rendered and api_match:
                api_name = api_match.group(0).replace("API：", "")
                candidates = [x for x in original_entries if x.get("type") == "结构化数据" and x.get("api_name") == api_name]
            else:
                id_match = re.search(r'ID：([^|\s]+)', rendered)
                source_type = "同业材料" if "Materials V2" in rendered else ("研报" if "Datayes研报" in rendered else ("纪要" if "Datayes纪要" in rendered else ("调研" if "Datayes调研" in rendered else "")))
                if id_match and source_type:
                    candidates = [x for x in original_entries if x.get("type") == source_type and str(x.get("id", "")) == id_match.group(1)]
            if len(candidates) == 1:
                final_entries[final_no] = source_for(candidates[0])

    if final_entries:
        return final_entries

    # 仅用于尚未生成参考资料的局部单测/调用场景。
    evidence = {}
    for entry in original_entries:
        try:
            ref_no = int(entry.get("n"))
        except (TypeError, ValueError):
            continue
        evidence[ref_no] = source_for(entry)
    return evidence


def _source_has_number(source: str, value: float) -> bool:
    """允许有限舍入，但不把数量级不同的数字视为同一事实。"""
    for raw_num in re.findall(r'(?<!\d)\d+(?:\.\d+)?', str(source or "")):
        try:
            candidate = float(raw_num)
        except ValueError:
            continue
        if abs(candidate - value) <= max(0.02, abs(value) * 0.005):
            return True
    return False


def _citation_claim_groups(line: str) -> list:
    """把一行拆为“紧邻事实片段 + 对应引用组”，避免多来源整句互相误判。"""
    groups = []
    matches = list(re.finditer(r'\[(\d+)\]', line))
    pos = 0
    i = 0
    while i < len(matches):
        group_start = matches[i].start()
        refs = [int(matches[i].group(1))]
        group_end = matches[i].end()
        i += 1
        while i < len(matches) and line[group_end:matches[i].start()].strip() == "":
            refs.append(int(matches[i].group(1)))
            group_end = matches[i].end()
            i += 1
        fragment = line[pos:group_start]
        # 仅保留本引用前最近的完整短句/表格单元，引用后的下一条事实归下一组。
        fragment = re.split(r'[。；;！？|]', fragment)[-1].strip(' ，,、:：-')
        if fragment:
            groups.append((fragment, refs))
        pos = group_end
    return groups


def _validate_numeric_source_claims(md_content: str, key_data: dict) -> list:
    """校验高风险经营数字与其 [N] 来源是否相容；问题一律进入最终阻断门。"""
    evidence = _build_reference_evidence(key_data, md_content)
    issues = []
    for line_no, line in enumerate(str(md_content or "").splitlines(), start=1):
        if line.startswith("## 参考资料"):
            break
        claim_groups = _citation_claim_groups(line)
        if not claim_groups:
            continue
        for plain, refs in claim_groups:
            operational_terms = [term for term in _OPERATIONAL_NUMERIC_TERMS if term in plain]
            is_roe_claim = bool(re.search(r'\bROE\b', plain, re.IGNORECASE))
            is_base_liquor_output = "基酒" in plain and "产量" in plain
            if not (operational_terms or is_roe_claim or is_base_liquor_output):
                continue
            measures = [(float(v), unit) for v, unit in _QUANTITATIVE_TOKEN_RE.findall(plain)]
            sources = []
            for ref_no in refs:
                src = evidence.get(ref_no)
                if not src:
                    issues.append(f"16.数字来源不可审计: 行{line_no} 引用[{ref_no}]无原始来源")
                else:
                    sources.append((ref_no, src))
            if not sources:
                continue

            # 经营分项不得借用财务摘要接口；该错误需逐一指出，不能被其他来源掩盖。
            if operational_terms:
                financial_refs = [(n, s) for n, s in sources if s["api"] in _FINANCIAL_ONLY_APIS]
                for ref_no, src in financial_refs:
                    issues.append(f"16.经营数字错误引用: 行{line_no} 的{','.join(operational_terms[:3])}不能引用[{ref_no}] {src['api']}")
                usable_sources = [(n, s) for n, s in sources if s["api"] not in _FINANCIAL_ONLY_APIS]
            else:
                usable_sources = sources
            if not usable_sources:
                continue

            # 连续 [N][M] 是一个联合来源组：只要求至少一个原始来源完整支撑该短语，
            # 不再把相邻但负责不同事实的来源强行要求覆盖全部数字。
            if is_roe_claim:
                allowed = {"fdmtNew", "fdmt_indi_rtn", "batchGetReportContent", "getMeetingSummaryDetail"}
                if not any(src["api"] in allowed for _, src in usable_sources):
                    joined = ",".join(str(n) for n, _ in usable_sources)
                    issues.append(f"16.ROE错误引用: 行{line_no} 的ROE不能引用[{joined}]")
                continue
            if is_base_liquor_output:
                allowed = {"management_discussion", "batchGetReportContent", "getMeetingSummaryDetail"}
                if not any(src["api"] in allowed for _, src in usable_sources):
                    joined = ",".join(str(n) for n, _ in usable_sources)
                    issues.append(f"16.基酒产量错误引用: 行{line_no} 不能引用[{joined}]")
                continue
            if not operational_terms:
                continue

            def supports_claim(src: dict) -> bool:
                source = src["text"]
                return (all(term in source for term in operational_terms)
                        and all(_source_has_number(source, value) for value, _ in measures))

            if any(supports_claim(src) for _, src in usable_sources):
                continue
            all_source_text = " ".join(src["text"] for _, src in usable_sources)
            missing_terms = [term for term in operational_terms if term not in all_source_text]
            joined = ",".join(str(n) for n, _ in usable_sources)
            if missing_terms:
                issues.append(f"16.经营数字无来源文本: 行{line_no} 引用[{joined}]缺少{','.join(missing_terms[:2])}")
                continue
            absent = [f"{value:g}{unit}" for value, unit in measures if not _source_has_number(all_source_text, value)]
            if absent:
                issues.append(f"16.经营数字无来源数值: 行{line_no} 引用[{joined}]缺少{','.join(absent[:3])}")
            else:
                # 术语/数值分别出现于不同来源也不能证明同一事实，维持 fail-closed。
                issues.append(f"16.经营数字来源组合不完整: 行{line_no} 引用[{joined}]无法由单一来源完整支撑")
    return issues


def _meeting_subject_source_issues(md_content: str, key_data: dict) -> list:
    """Reject numeric claims from meetings that are not target-company material."""
    target = str((key_data or {}).get("name") or "").strip()
    if not target:
        return []
    aliases = {target}
    for suffix in ("股份有限公司", "集团股份有限公司", "有限公司", "集团"):
        aliases.add(target.replace(suffix, "").strip())
    aliases.discard("")
    evidence = _build_reference_evidence(key_data, md_content)
    issues = []
    for line_no, line in enumerate(str(md_content or "").splitlines(), start=1):
        if line.startswith("## 参考资料"):
            break
        if not _QUANTITATIVE_TOKEN_RE.search(line):
            continue
        for _, refs in _citation_claim_groups(line):
            for ref_no in refs:
                source = evidence.get(ref_no) or {}
                if source.get("api") != "getMeetingSummaryDetail":
                    continue
                source_text = f"{source.get('title', '')}\n{source.get('text', '')}"
                target_subject = any(alias in str(source.get("title", "")) for alias in aliases)
                if not target_subject:
                    target_subject = any(source_text.count(alias) >= 2 for alias in aliases if len(alias) >= 2)
                if not target_subject:
                    issues.append(f"16.纪要主体不匹配: 行{line_no} 引用[{ref_no}]不是目标公司主体材料")
    return issues


def _issue_reference_numbers(issue: str) -> set:
    """Extract cited source numbers from one provenance issue."""
    refs = set()
    for match in re.finditer(r"\[([\d,\s]+)\]", str(issue or "")):
        refs.update(int(value) for value in re.findall(r"\d+", match.group(1)))
    return refs


def _strip_unverifiable_claims(line: str, invalid_refs: set) -> str:
    """Remove only bad cited clauses, preserving independently sourced content.

    A source mismatch must never survive delivery, but deleting a full mixed-fact
    sentence or a whole table row also discards valid research and makes the report
    materially thinner than v1.2.13. Claims are split at natural Chinese sentence
    boundaries (and at Markdown table cells) before fail-closing.
    """
    if not invalid_refs:
        return line

    fact_terms = "|".join(re.escape(term) for term in _OPERATIONAL_NUMERIC_TERMS)
    risk_pattern = re.compile(rf"(?:{fact_terms}|\bROE\b|毛利率)", re.I)

    def clean_fragment(fragment: str) -> str:
        refs = {int(value) for value in re.findall(r"\[(\d+)\]", fragment)}
        has_measure = bool(_QUANTITATIVE_TOKEN_RE.search(fragment))
        if refs & invalid_refs and has_measure and risk_pattern.search(fragment):
            return ""
        return fragment

    def clean_text(value: str) -> str:
        parts = re.split(r"(?<=[。；;！？])", value)
        return "".join(clean_fragment(part) for part in parts).strip()

    stripped = line.strip()
    if stripped.startswith("|") and stripped.count("|") >= 2:
        cells = line.split("|")
        for index in range(1, len(cells) - 1):
            cells[index] = clean_text(cells[index])
        return "|".join(cells)
    return clean_text(line)


def _drop_unverifiable_numeric_lines(md_content: str, key_data: dict, max_rounds: int = 3) -> tuple:
    """Fail-close unsupported numeric claims without discarding valid peer content.

    The validator remains strict: an unsupported number is always removed. v1.2.23
    narrows the removal unit from a whole line to the cited fact clause/table cell,
    so a single bad data point cannot erase separately sourced analysis on that line.
    """
    text = str(md_content or "")
    removed = []
    for _ in range(max_rounds):
        issues = _validate_numeric_source_claims(text, key_data)
        invalid_refs_by_line = {}
        for issue in issues:
            match = re.search(r'行(\d+)', issue)
            if match:
                line_no = int(match.group(1))
                invalid_refs_by_line.setdefault(line_no, set()).update(_issue_reference_numbers(issue))
        if not invalid_refs_by_line:
            break
        lines = text.splitlines()
        changed = False
        for line_no in sorted(invalid_refs_by_line, reverse=True):
            index = line_no - 1
            if index < 0 or index >= len(lines):
                continue
            line = lines[index]
            # Do not mutate headings or table separators; the final gate must handle
            # those structural failures explicitly.
            if re.match(r'^#{1,3}\s|^\|\s*:?-{3,}', line.strip()):
                continue
            replacement = _strip_unverifiable_claims(line, invalid_refs_by_line[line_no])
            if replacement != line:
                removed.append(f"行{line_no}:移除不可核验事实，保留其余已验证内容")
                if replacement.strip():
                    lines[index] = replacement
                else:
                    del lines[index]
                changed = True
            elif line.strip() and invalid_refs_by_line[line_no]:
                # The issue has a citation but cannot be isolated safely. Preserve
                # fail-closed behavior for this single-fact line instead of delivery.
                removed.append(f"行{line_no}:无法安全拆分的不可核验事实")
                del lines[index]
                changed = True
        if not changed:
            break
        text = "\n".join(lines)
    return text, removed
def _markdown_structure_errors(md_content: str) -> list:
    """Detect headings injected into Markdown table cells before delivery."""
    issues = []
    for line_no, raw_line in enumerate(str(md_content or "").splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("|") and re.search(r"(?<!\w)#{2,6}\s+", line):
            issues.append(f"17.表格单元格内包含标题: 行{line_no}")
    return issues


def _final_self_check(md_content: str, ref_map: dict, key_data: dict = None) -> list:
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
    15. 风险提示条数、格式、引用与去模板化
    """
    import re
    blockers = []

    blockers.extend(_markdown_structure_errors(md_content))
    if key_data:
        blockers.extend(_validate_numeric_source_claims(md_content, key_data))
        blockers.extend(_meeting_subject_source_issues(md_content, key_data))

    unresolved_facts = sorted(set(_FACT_MARKER_RE.findall(str(md_content or ""))))
    if unresolved_facts:
        blockers.append("16.事实卡未渲染: " + ",".join(unresolved_facts[:8]))

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

    # ── 15. 风险提示质量门禁 ──
    risk_match = re.search(
        r'## 10 风险提示\n\n(.*?)(?=\n## 参考资料|\n---\n\n## 参考资料)',
        md_content,
        re.DOTALL,
    )
    if not risk_match:
        blockers.append("15.缺少风险提示章节")
    else:
        risk_valid, risk_issues = _validate_a_share_risk_body(risk_match.group(1))
        if not risk_valid:
            blockers.append(f"15.风险提示不合格: {', '.join(risk_issues)}")
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
        if orphans:
            blockers.append(f"14.孤儿引用: 正文引用{orphans}未在参考资料中定义")

    return blockers


def _repair_catalyst_section(md_content: str, key_data: dict) -> tuple:
    """Apply only deterministic, section-local post-generation repairs.

    §9.4 is intentionally excluded pending its dedicated redesign. Re-generating a
    scenario table after source filtering can reintroduce unverified claims and has
    previously detached table rows from §9. The final structural gate removes an
    incomplete optional §9.4 instead.
    """
    repair_log = []
    catalyst = _extract_section(md_content, "催化事件时间表", end_markers=["## 4 ", "## 公司业务拆分"])
    if _valid_catalyst_table(catalyst, min_rows=3):
        return md_content, repair_log

    repaired_table = _gen_catalyst_table(key_data)
    if not repaired_table:
        repair_log.append("催化事件表不足3行，未生成可核验补充")
        return md_content, repair_log

    start = md_content.find("## 3 催化事件时间表")
    if start < 0:
        start = md_content.find("催化事件时间表")
    end = md_content.find("## 4 ", start) if start >= 0 else -1
    if end < 0 and start >= 0:
        end = md_content.find("## 公司业务拆分", start)
    if start >= 0 and end > start:
        md_content = md_content[:start] + repaired_table.strip() + "\n\n" + md_content[end:]
        repair_log.append("催化事件表不足3行→来源化补充")
    else:
        repair_log.append("催化事件表不足3行，未找到安全替换边界")
    return md_content, repair_log

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
    # Strip leading "## 3" header to avoid duplicate when inserted by _repair_catalyst_section
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
    # 无法从真实材料提取合格事件时，禁止写入泛化季度占位事件。
    return ""


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
    # 先从原始来源构建事实卡；生成阶段只能引用标记，最终由程序渲染数值和引用。
    key_data["operating_fact_cards"], key_data["fact_marker_refs"] = _build_operating_fact_cards(key_data)

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

    # v1.2.6-R3: 读全文关键章节后生成标题结论
    _s1 = str(sections.get("s1", ""))[:500]
    _s2 = str(sections.get("s2", ""))[:500]
    _s5 = str(sections.get("s5", ""))[:300]
    _full_ctx = f"近况跟踪：{_s1}\n\n核心投资逻辑：{_s2}"
    if _s5.strip():
        _full_ctx += f"\n\n业务概况：{_s5}"
    _tc_prompt = (
        f"为{name}（{ticker}）研究报告生成标题副标题：一句话投资结论，要求：\n"
        "1. 字数控制在20字左右，可超但不超过25字，必须在语义完整处自然结尾，禁止句子中断\n"
        "2. 直接给出结论，无前缀、无引号、无标点符号结尾\n"
        "3. 有明确观点导向，体现最核心驱动力或最重要投资判断\n"
        "4. 示例风格：直销占比跃升，分红回购支撑估值修复\n\n"
        f"报告核心内容：\n{_full_ctx}\n\n"
        "一句话结论（直接输出，语义完整）："
    )
    _title_conclusion = call_claude(client, _tc_prompt, max_tokens=80).strip()
    # 清理可能出现的引号、前缀
    for _pfx in ("一句话结论：", "结论：", "：", ":", "\u201c", "\u201d", '"', "'"):
        _title_conclusion = _title_conclusion.lstrip(_pfx)
    _title_conclusion = _title_conclusion.strip().rstrip("。").strip()
    _title_conclusion = _sanitize_title_conclusion(_title_conclusion, meta.get("short_name", name), ticker)
    if not _title_conclusion:
        _title_conclusion = _build_deterministic_fallback_title(sections, meta.get("short_name", name), ticker)
    sections["title_conclusion"] = _title_conclusion
    print(f"[{time.time()-t0:.1f}s] 标题结论: {_title_conclusion}")

    # ── 5. 组装并写文件 ────────────────────────────────────────────────────────
    print(f"[{time.time()-t0:.1f}s] 组装报告...")
    md_content = assemble_report(meta, sections, ref_map)
    md_content = _render_fact_markers(md_content, key_data.get("fact_marker_refs", {}))

    # 只保留确定性催化表修复；情景推演不再由后处理二次调用 LLM 重写。
    md_content, _repair_log = _repair_catalyst_section(md_content, key_data)
    if _repair_log:
        print(f"[{time.time()-t0:.1f}s] 自动修复: {_repair_log}")
    md_content = _enforce_a_share_required_blocks(md_content, key_data, ref_map)
    md_content = _normalize_markdown_tables(md_content)
    md_content = _fix_orphan_refs(md_content)

    # 无法审计的经营数字绝不进入交付物。
    md_content, _provenance_drops = _drop_unverifiable_numeric_lines(md_content, key_data)
    if _provenance_drops:
        print(f"[{time.time()-t0:.1f}s] 溯源清洗：删除{len(_provenance_drops)}条无法核验的生成行")
    md_content = _drop_invalid_section94(md_content, key_data)

    # 终检只执行一次：死引用清理、编号重排和表格标准化均在此之后。
    md_content = _postprocess_report(md_content, ref_map)
    md_content = _normalize_markdown_tables(md_content)
    md_content = _drop_incomplete_optional_scenarios(md_content)
    # ── v1.2.3 生成完成前自检 ──────────────────────────────────────────────────
    md_content = _clean_empty_bold_tags(md_content)
    md_content = _normalize_final_markdown_format(md_content)
    md_content = _drop_incomplete_optional_scenarios(md_content)
    md_content = _drop_empty_optional_section9(md_content)
    md_content = _renumber_a_share_subsections(md_content)

    # ── v1.2.5-R3: 图表标题还原（cleaner 修了残缺 !(url) 但丢掉了原标题）────────
    md_content = _restore_chart_captions(md_content, charts)
    blockers = _final_self_check(md_content, ref_map, key_data)
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

    total = time.time() - t0
    print(f"\n🎉 报告生成完成！总耗时：{total:.1f}秒（{total/60:.1f}分钟）")


if __name__ == "__main__":
    main()
