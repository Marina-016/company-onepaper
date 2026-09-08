# -*- coding: utf-8 -*-
"""
fetch_data.py — 公司一页纸数据采集脚本
=======================================
用法:
    python -X utf8 fetch_data.py --ticker 601888 --token <DATAYES_TOKEN> [--output ./data.json]

说明:
    - 并行调用所有 Datayes 接口，封装了每个接口的正确参数、URL占位符替换、分页逻辑
    - 结果保存为 JSON 文件，供模型直接读取，无需模型自行构造 curl
    - 失败接口不阻塞流程，统一记录到 __errors__ 字段

输出 JSON 结构:
    {
      "__meta__": { ticker, name, report_date, period_type, ... },
      "company_info":    Ashare_info 返回,
      "financial":       fdmtNew 返回,
      "fin_indicators":  fdmt_indi_rtn 返回,
      "main_comp":       getFdmtMoStdItem 返回,
      "main_comp_ratio": main_composition_ratio 返回,
      "fin_chart_revenue":   stock_financial_indicator_revenue 返回,
      "fin_chart_profit":    stock_financial_indicator_net_profit 返回,
      "fin_chart_margin":    stock_financial_indicator_gross_margin 返回,
      "fin_chart_structure": stock_financial_indicator_earning_structure 返回,
      "executives":      Executive_information 返回,
      "top_holders":     Ashare_tenHolders 返回,
      "inst_holding":    Ashare_orgHoldingdetail 返回,
      "bonus":           Ashare_bonus 返回,
      "announcements":   [ { ...ann_meta, "detail": getAnnouncementDetail 返回 }, ... ],
      "ann_types":       announcement_type 返回,
      "mgmt_discussion": management_discussion 返回,
      "research_reports": [ { ...report_meta, "detail": None, "content": batchGetReportContentDomestic/Foreign（按orgType分流）, "graph": report_graph }, ... ],
      "meetings":        [ { ...meeting_meta, "detail": getMeetingSummaryDetail 返回 }, ... ],
      "consensus":       research_sec_coredata 返回,
      "profit_forecast": research_sec_foredata 返回,
      "peer_materials":  getMaterialsV2 返回（同业可比素材列表），
      "inst_survey":     Org_survey 返回,
      "pe_valuation":    diagnosis_pe_valuation 返回,
      "valuation_rank":  diagnosis_valuation_rank 返回,
      "charts":          { revenue: url, profit: url, margin: url, structure: url },
      "__errors__":      [ { api, error, curl }, ... ],
      "__failed_curls__": "报告末尾展示给用户的curl调试块"
    }
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import threading
import traceback
from urllib.parse import urlencode

# Windows 控制台 GBK 编码兼容：强制 stdout/stderr 输出 UTF-8
# 仅在作为主脚本运行时重包装标准流；被 import 时不得劫持解释器 stdout/stderr
if sys.platform == "win32" and __name__ == "__main__":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "-q"], check=True)
    import requests

# ─────────────────────────────────────────────
# 全局并发控制（429自动降级）
# ─────────────────────────────────────────────
_HTTP_CONCURRENCY = 8                          # 初始并发数
_http_sem = threading.Semaphore(_HTTP_CONCURRENCY)
_http_sem_lock = threading.Lock()
_http_concurrency_reduced = [False]            # 已降级标志

def _reduce_http_concurrency():
    """429持续触发时将全局HTTP并发降至1（只执行一次）。
    通过永久占用多余的信号量槽实现：释放后其他线程最多1个同时运行。"""
    with _http_sem_lock:
        if _http_concurrency_reduced[0]:
            return
        _http_concurrency_reduced[0] = True
    print("  ⚠️ 429限流持续，全局HTTP并发降至1（串行模式）", flush=True)
    # 永久占用 N-1 个槽，使剩余可用并发 = 1
    for _ in range(_HTTP_CONCURRENCY - 1):
        _http_sem.acquire()

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
META_BASE = "https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api"

ALLOWED_HOSTS = {"gw.datayes.com", "api.datayes.com", "api.wmcloud.com", "r.datayes.com"}

TODAY8 = datetime.date.today().strftime("%Y%m%d")
ONE_YEAR_AGO8 = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y%m%d")
THREE_MONTHS_AGO8 = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y%m%d")
ONE_MONTH_AGO8 = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d")

ALL_API_NAMES = [
    "stock_search", "ticker_period",
    "fdmtNew", "fdmt_indi_rtn", "management_discussion",
    "announcement", "getAnnouncementDetail", "announcement_type",
    "getFdmtMoStdItem", "main_composition_ratio",
    "stock_financial_indicator_revenue", "stock_financial_indicator_net_profit",
    "stock_financial_indicator_gross_margin", "stock_financial_indicator_earning_structure",
    "data_to_image",
    "research_search",
    "batchGetReportContentDomestic", "batchGetReportContentForeign", "report_graph",
    "meeting_search", "getMeetingSummaryDetail",
    "research_sec_coredata", "research_sec_foredata",
    "diagnosis_pe_valuation", "diagnosis_valuation_rank",
    "Org_survey", "institution_research_detail",
    "Ashare_tenHolders", "Ashare_orgHoldingdetail",
    "Executive_information", "Ashare_info", "Ashare_bonus",
    "getMaterialsV2",
]

# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def make_headers(token, json_body=False):
    h = {"Authorization": f"Bearer {token}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def safe_get_data(resp_json):
    """从响应中提取 data 字段，兼容多种结构"""
    if not isinstance(resp_json, dict):
        return resp_json
    code = resp_json.get("code") or resp_json.get("retCode")
    if code not in (1, "1", 200, "200", "success", "Success"):
        return None
    return resp_json.get("data")


def build_curl(method, url, params=None, body=None, token=""):
    """构造 curl 命令字符串（token 脱敏）"""
    tok_display = token[:8] + "..." + token[-8:] if len(token) > 16 else token
    parts = [f"curl -s -X {method}"]
    parts.append(f'  -H "Authorization: Bearer {tok_display}"')
    if method == "POST" and body is not None:
        parts.append('  -H "Content-Type: application/json"')
        parts.append(f"  -d '{json.dumps(body, ensure_ascii=False)}'")
        parts.append(f'  "{url}"')
    else:
        qs = ("?" + urlencode(params)) if params else ""
        parts.append(f'  "{url}{qs}"')
    return " \\\n".join(parts)


def call(method, url, token, params=None, body=None, timeout=25):
    """发起 HTTP 请求，返回 (resp_json, http_status, error_msg)；429 自动退避重试3次。
    4次全部429时触发全局并发降至1（_reduce_http_concurrency）。"""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    if hostname not in ALLOWED_HOSTS:
        return None, None, f"域名不在白名单: {hostname}"
    import time as _t
    _http_sem.acquire()
    try:
        for attempt in range(4):
            try:
                h = make_headers(token, json_body=(method == "POST"))
                if method == "POST":
                    r = requests.post(url, json=body, headers=h, timeout=timeout)
                else:
                    r = requests.get(url, params=params, headers=h, timeout=timeout)
                if r.status_code == 429:
                    wait = 3 * (2 ** attempt)   # 3s, 6s, 12s, 24s
                    _t.sleep(wait)
                    continue
                try:
                    return r.json(), r.status_code, None
                except Exception:
                    return None, r.status_code, f"JSON解析失败: {r.text[:200]}"
            except requests.exceptions.Timeout:
                return None, None, f"超时(>{timeout}s)"
            except Exception as e:
                return None, None, str(e)[:200]
        # 4次全是429 → 触发全局降并发
        _reduce_http_concurrency()
        return None, 429, "429限流：重试3次仍失败"
    finally:
        _http_sem.release()


# ─────────────────────────────────────────────
# Phase 1: 并行获取所有接口元信息
# ─────────────────────────────────────────────

def fetch_all_meta(token):
    def _get(name):
        rj, status, err = call("GET", META_BASE, token, params={"nameEn": name}, timeout=10)
        if err or not rj:
            return name, {"url": "", "method": "GET", "error": err}
        d = rj.get("data") or {}
        return name, {
            "url": d.get("httpUrl", ""),
            "method": (d.get("httpMethod") or "GET").upper(),
            "params_input": d.get("parametersInput", []),
        }

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_get, n): n for n in ALL_API_NAMES}
        for fut in concurrent.futures.as_completed(futs):
            name, meta = fut.result()
            result[name] = meta

    # 对请求失败（有 error 字段）的接口逐一重试，避免高并发限流导致 URL 缺失
    import time as _time
    failed = [n for n, m in result.items() if not m.get("url") and m.get("error")]
    if failed:
        print(f"  ⚠️ {len(failed)} 个元信息请求失败，逐一重试: {failed}")
        for n in failed:
            _time.sleep(0.3)
            _, meta_info = _get(n)
            result[n] = meta_info

    return result


# ─────────────────────────────────────────────
# Phase 2: 基础 ID — stock_search + ticker_period
# ─────────────────────────────────────────────

def resolve_ticker(meta, raw_ticker, company_name, token):
    """
    通过 stock_search 确认 entity_id（6位纯数字）和公司简称。
    raw_ticker 可以是6位代码或公司名称。
    """
    url = meta.get("stock_search", {}).get("url", "")
    if not url:
        return raw_ticker, company_name or raw_ticker, "stock_search URL缺失"

    is_code = bool(re.match(r'^\d{6}$', raw_ticker.strip()))

    # 若输入是6位代码，直接使用，并额外搜索一次以获取公司名
    if is_code:
        rj, _, err = call("GET", url, token,
                          params={"query": raw_ticker, "dataType": "1", "topK": "10"})
        if not err and rj:
            hits = (rj.get("data") or {}).get("hits") or []
            # 精确匹配 entity_id
            for h in hits:
                if str(h.get("entity_id") or "").strip() == raw_ticker:
                    return raw_ticker, h.get("name") or raw_ticker, None
        # 无精确命中，直接用输入代码，名称暂时为代码本身
        return raw_ticker, company_name or raw_ticker, None

    # 输入是公司名称，取第一条结果
    rj, _, err = call("GET", url, token,
                      params={"query": raw_ticker, "dataType": "1", "topK": "5"})
    if err or not rj:
        return raw_ticker, company_name or raw_ticker, err

    hits = (rj.get("data") or {}).get("hits") or []
    if not hits:
        return raw_ticker, company_name or raw_ticker, "stock_search 无结果"

    best = hits[0]
    entity_id = str(best.get("entity_id") or "").strip()
    name = best.get("name") or company_name or raw_ticker
    if not re.match(r'^\d{6}$', entity_id):
        entity_id = raw_ticker
    return entity_id, name, None


def get_ticker_period(meta, ticker, token):
    """获取最新财报期类型，返回 (period_type, period_value, error)"""
    url = meta.get("ticker_period", {}).get("url", "")
    if not url:
        return "A", None, "ticker_period URL缺失"

    # URL 末尾可能是 {ticker} 占位符，也可能是硬编码示例代码（如 002594）
    # 统一替换末尾6位数字或{ticker}为实际ticker
    url = re.sub(r'\{ticker\}', ticker, url)
    url = re.sub(r'/\d{6}$', f'/{ticker}', url)

    rj, _, err = call("GET", url, token)
    if err or not rj:
        return "A", None, err

    data = rj.get("data")

    # 部分标的该接口会以 HTTP/API 成功的 ``data: null`` 表示未提供最新
    # 财报期，而不是调用失败。后续接口可以安全使用年度口径，不能把它计入
    # 失败接口数，否则会误导运行结果。
    if data is None:
        return "A", None, None

    # data 可能是 list（ticker_period 接口返回期列表，按时间倒序）
    if isinstance(data, list) and data:
        latest = data[0]  # 最新一期
        quarter = latest.get("quarter")
        label = latest.get("label") or ""
        end_date = latest.get("endDate") or ""
        # 根据 quarter 映射 period_type
        period_map = {4: "A", 2: "S1", 1: "Q1", 3: "Q3"}
        period_type = period_map.get(quarter, "A")
        return period_type, end_date, None

    if isinstance(data, dict):
        ptype = (data.get("reportPeriodType") or data.get("periodType") or
                 data.get("type") or data.get("period") or "A")
        pvalue = data.get("period") or data.get("reportPeriod") or data.get("endDate")
        return str(ptype), pvalue, None

    return "A", None, f"ticker_period data格式未知: {type(data)}"


# ─────────────────────────────────────────────
# Phase 3: 并行主数据采集
# ─────────────────────────────────────────────

def _api_ok(rj):
    """检查 API 级别响应是否成功：code==1 或 retCode==1 或无 code/retCode 字段"""
    if not isinstance(rj, dict):
        return True  # 非标准响应，不过滤
    code = rj.get("code") or rj.get("retCode")
    if code is not None and code != 1:
        return False
    return True


def fetch_financial(meta, ticker, period_type, token):
    """fdmtNew — 财务摘要"""
    url = meta.get("fdmtNew", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    url = url.replace("{ticker}", ticker)
    is_annual = period_type in ("A", "a", "annual")
    params = {
        "displaySort": "left",
        "duration": "ACCUMULATE",
        "includeLatest": "true" if not is_annual else "false",
        "mergedFlag": "1",
        "period": "3",
        "reportPeriodType": "A" if is_annual else f"A,{period_type}",
        "reportType": "SUMMARY",
    }
    rj, status, err = call("GET", url, token, params=params)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_fin_indicators(meta, ticker, token):
    """fdmt_indi_rtn — 盈利指标（含ROE分解）"""
    url = meta.get("fdmt_indi_rtn", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    # fdmt_indi_rtn 的元信息要求 beginDate/endDate；``period`` 并不是该
    # 接口的入参。保留最近三个完整年度并覆盖当年最新披露，供 ROE/杜邦分析使用。
    today = datetime.date.today()
    params = {
        "ticker": ticker,
        "beginDate": f"{today.year - 3}0101",
        "endDate": today.strftime("%Y%m%d"),
    }
    rj, _, err = call("GET", url, token, params=params)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def _has_meaningful_data(rj):
    """检查响应中是否有实际数据（非 null、非空列表、非无有效记录的 dict）"""
    if not isinstance(rj, dict):
        return False
    data = rj.get("data")
    if data is None:
        return False
    if isinstance(data, list) and len(data) == 0:
        return False
    if isinstance(data, dict):
        # 部分接口用 datas 字段
        ds = data.get("datas")
        if ds is None:
            # data dict 无 datas 字段且无其他有效内容
            return bool(data)
        if isinstance(ds, list) and len(ds) == 0:
            return False
        return ds is not None
    return True


def fetch_main_comp(meta, ticker, period_type, token):
    """getFdmtMoStdItem — 主营业务构成（近3年年报）。
    优先 classifCD=2（按产品），无数据时降级 classifCD=1（按行业），
    两者均无数据时返回错误，由 report_writer 的 mgmt_discussion 兜底。"""
    url = meta.get("getFdmtMoStdItem", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    today = datetime.date.today()
    base_params = {
        "ticker": ticker,
        "beginDate": f"{today.year - 3}0101",
        "endDate": f"{today.year}1231",
    }

    # 优先 classifCD=2（按产品分类）
    for classif_cd in ("2", "1"):
        params = {**base_params, "classifCD": str(classif_cd)}
        rj, _, err = call("GET", url, token, params=params)
        if err:
            continue
        if _api_ok(rj) and _has_meaningful_data(rj):
            return rj, None

    # 两种分类均无数据，返回最后一次的原始响应供上层判断
    return None, "classifCD=2/1均无数据，需 mgmt_discussion 兜底"


def fetch_main_comp_region(meta, ticker, period_type, token):
    """getFdmtMoStdItem — 主营业务构成（按地区分类，近3年年报）"""
    url = meta.get("getFdmtMoStdItem", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    today = datetime.date.today()
    params = {
        "ticker": ticker,
        "classifCD": "3",
        "beginDate": f"{today.year - 3}0101",
        "endDate": f"{today.year}1231",
    }
    rj, _, err = call("GET", url, token, params=params)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_main_comp_ratio(meta, ticker, period_type, token):
    """main_composition_ratio — 主营业务占比"""
    url = meta.get("main_composition_ratio", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    is_annual = period_type in ("A", "a")
    params = {
        "ticker": ticker,
        "classify": "2",
        "reportType": "A" if is_annual else "LAST",
        "year": str(datetime.date.today().year - 1),
    }
    rj, _, err = call("GET", url, token, params=params)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_fin_chart(meta, name, ticker, token):
    """stock_financial_indicator_* — 关键数据图谱"""
    url = meta.get(name, {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_company_info(meta, ticker, token):
    url = meta.get("Ashare_info", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_executives(meta, ticker, token):
    url = meta.get("Executive_information", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_top_holders(meta, ticker, token):
    url = meta.get("Ashare_tenHolders", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker, "shType": "1", "shNum": "10"})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_inst_holding(meta, ticker, token):
    url = meta.get("Ashare_orgHoldingdetail", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    # {type} 是路径参数（0=全部），需替换到 URL 中，不放入 query params
    url = re.sub(r'\{type\}', '0', url)
    cur_year = datetime.date.today().year
    cur_q = (datetime.date.today().month - 1) // 3
    if cur_q == 0:
        cur_q = 4
        cur_year -= 1
    rj, _, err = call("GET", url, token,
                      params={"ticker": ticker, "year": str(cur_year), "quarter": str(cur_q)})
    # 若当期无数据，退一期
    if err or (isinstance(rj, dict) and rj.get("code") not in (1, "1", 200)):
        prev_q = cur_q - 1 if cur_q > 1 else 4
        prev_y = cur_year if cur_q > 1 else cur_year - 1
        rj, _, err = call("GET", url, token,
                          params={"ticker": ticker, "year": str(prev_y), "quarter": str(prev_q)})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_bonus(meta, ticker, token):
    url = meta.get("Ashare_bonus", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker, "type": "ALL"})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_mgmt_discussion(meta, ticker, token):
    url = meta.get("management_discussion", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token,
                      params={"ticker": ticker, "startDate": ONE_YEAR_AGO8, "endDate": TODAY8,
                              "pageNo": 1, "pageSize": 20},
                      timeout=35)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_org_survey(meta, ticker, token):
    url = meta.get("Org_survey", {}).get("url", "")
    detail_url = meta.get("institution_research_detail", {}).get("url", "")
    if not url:
        return None, "URL缺失"

    # 获取近1个月机构调研列表
    rj, _, err = call("GET", url, token,
                      params={"ticker": ticker, "startDate": ONE_MONTH_AGO8, "endDate": TODAY8,
                              "pageNow": 1, "pageSize": 20})
    if err or not rj:
        return None, err

    # 提取调研记录列表
    raw_data = rj.get("data", {})
    if isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        items = raw_data.get("list") or []
    else:
        items = []

    # 并行获取前5条调研的详情内容
    def get_detail(item):
        event_id = item.get("eventID", "")
        if not detail_url or not event_id:
            return {**item, "detail_content": None}
        det_rj, _, det_err = call("GET", detail_url, token,
                                  params={"eventID": event_id, "ticker": ticker})
        content = None
        if not det_err and det_rj:
            content = (det_rj.get("data") or {}).get("content")
        return {**item, "detail_content": content}

    enriched = []
    top_items = items[:5] if isinstance(items, list) else []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(get_detail, it) for it in top_items]
        enriched = [f.result() for f in concurrent.futures.as_completed(futs)]
    # 补充剩余无需详情的条目
    for it in (items[5:] if isinstance(items, list) else []):
        enriched.append({**it, "detail_content": None})

    # 保持原始响应结构，替换 list 字段
    if isinstance(raw_data, dict) and "list" in raw_data:
        rj = {**rj, "data": {**raw_data, "list": enriched}}
    elif isinstance(raw_data, list):
        rj = {**rj, "data": enriched}

    return (rj, None)


def fetch_pe_valuation(meta, ticker, token):
    url = meta.get("diagnosis_pe_valuation", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_valuation_rank(meta, ticker, token):
    url = meta.get("diagnosis_valuation_rank", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"ticker": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_consensus(meta, ticker, token):
    url = meta.get("research_sec_coredata", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token, params={"tickers": ticker})
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


def fetch_profit_forecast(meta, ticker, token):
    url = meta.get("research_sec_foredata", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    cur_year = datetime.date.today().year
    fore_years = f"{cur_year},{cur_year+1},{cur_year+2}"
    # research_sec_foredata 使用 yyyy-MM-dd 日期格式
    pub_start = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    pub_end   = datetime.date.today().strftime("%Y-%m-%d")
    rj, _, err = call("GET", url, token, params={
        "tickers":      ticker,
        "foreYears":    fore_years,
        "pubTimeStart": pub_start,
        "pubTimeEnd":   pub_end,
        "pageSize":     50,
    })
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


_PEER_NOISE_SUFFIXES = (
    '公司', '行业', '产业', '格局', '优势', '壁垒', '市场', '领域', '层面', '方面',
    '来看', '而言', '地位', '空间', '趋势', '发展', '水平', '数量', '赛道', '龙头',
    '集中度', '增速', '占比', '估值', '股价', '竞争力', '护城河', '同行', '同业',
    '稳定', '加剧', '激烈', '充分', '持续', '保持', '给予', '提升', '下降', '恶化',
)


def _normalize_security_name(name):
    """归一化证券名称：去空白并剥离尾部公司后缀，供“名称完全一致”核验。"""
    s = re.sub(r'\s+', '', str(name or ''))
    return re.sub(r'(股份有限公司|有限责任公司|集团有限公司|集团公司|有限公司|公司)$', '', s)


def extract_peer_names(reports, company_short_name):
    """从研报明确的同业或竞争语境提取候选；无代码名称仍须经精确证券名称核验。"""
    contexts = re.compile(r'(?:可比公司|同类公司|竞争对手|竞争公司|同业公司|主要竞争|对标公司|可比上市|同类上市|行业对比|同业比较|可比估值|同业估值|相比(?:同行|同业|竞争)).{0,300}')
    named_code = re.compile(r'(?:^|[、，,；;：:\s])(?P<name>[\u4e00-\u9fa5]{2,12}?)(?:股份有限公司|集团)?[（(]\s*(?P<code>[036]\d{5})\s*[）)]')
    enum = re.compile(r'(?:如|例如|包括|主要有|涵盖|涉及|对标|分别是)[:：]?\s*([\u4e00-\u9fa5]{2,8}(?:[、，,][\u4e00-\u9fa5]{2,8}){1,8})')
    # Match explicit competition verbs plus an enumeration of two or more names only.
    competitive_enum = re.compile(
        '(?:\u6324\u5360|\u5206\u6d41|\u62a2\u5360|\u66ff\u4ee3|\u51b2\u51fb|\u4e89\u593a).{0,80}?'
        '(?P<names>[\u4e00-\u9fa5]{2,8}(?:[\u3001\uff0c,][\u4e00-\u9fa5]{2,8}){1,8})'
        '(?=\u7b49(?:\u5176\u4ed6)?(?:\u9ad8\u7aef)?(?:\u54c1\u724c|\u516c\u53f8|\u5382\u5546|\u9152\u4f01|\u540c\u884c|\u7ade\u4e89\u8005))'
    )
    phrase = re.compile(r'[\u4e00-\u9fa5]{2,8}')
    blocked = {'公司', '行业', '可比公司', '同业公司', '同类公司', '竞争对手', '主要竞争对手', '竞争公司', '对标公司', '可比上市', '同类上市', company_short_name or ''}
    candidates = {}
    for report in (reports or [])[:8]:
        meta_r = report.get('_meta', {}) or {}
        abstract = meta_r.get('abstractText', '') or ''
        values = [v for v in (((report.get('content', {}) or {}).get('data') or {}).values()) if isinstance(v, str)]
        body = ' '.join(values)[:20000]
        report_id = str(report.get('id') or meta_r.get('reportID') or '')
        for source in (abstract, body):
            source_contexts = contexts.findall(source or '') or ['']
            competitive_names = [match.group('names') for match in competitive_enum.finditer(source or '')]
            for context in source_contexts:
                for match in named_code.finditer(context):
                    name = re.sub(r'^(?:可比公司|同类公司|同业公司|竞争对手|竞争公司|对标公司|主要竞争|包括|如|例如|主要有|涉及|涵盖|分别是)+', '', match.group('name')).strip()
                    name = re.sub(r'(股份有限公司|集团)$', '', name)
                    code = match.group('code')
                    if not name or name in blocked or company_short_name in name:
                        continue
                    entry = candidates.setdefault('c:' + code, {'query': name, 'code': code, 'source_report_id': report_id, 'count': 0})
                    entry['count'] += 1
                # 放宽：同业列举中的纯名称短语（无代码），后续须经 stock_search 名称一致核验才有效
                enum_texts = enum.findall(context)
                enum_texts.extend(competitive_names)
                competitive_names = []
                for enum_text in enum_texts:
                    for name in phrase.findall(enum_text):
                        name = re.sub(r'[等]$', '', name)
                        if len(name) < 2 or name in blocked or company_short_name in name:
                            continue
                        if name.endswith(_PEER_NOISE_SUFFIXES):
                            continue
                        entry = candidates.setdefault('n:' + name, {'query': name, 'code': '', 'source_report_id': report_id, 'count': 0})
                        entry['count'] += 1
    return sorted(candidates.values(), key=lambda item: (-item['count'], item['code']))[:8]


def validate_peer_names(meta, peer_candidates, token, target_ticker=''):
    """核验可比公司候选：有代码按证券代码精确匹配；无代码按名称短语检索，证券名称归一化后完全一致才接受。"""
    url = meta.get('stock_search', {}).get('url', '')
    if not url or not peer_candidates:
        return []
    validated, seen = [], set()
    for candidate in peer_candidates[:10]:
        code = str(candidate.get('code') or '').strip()
        if re.match(r'^\d{6}$', code):
            if code == str(target_ticker or ''):
                continue
            rj, _, err = call('GET', url, token, params={'query': code, 'dataType': '1', 'topK': '10'})
            if err or not rj:
                continue
            hits = (rj.get('data') or {}).get('hits') or []
            exact = next((item for item in hits if str(item.get('entity_id') or '').strip() == code), None)
            if not exact:
                continue
            current_name = str(exact.get('name') or '').strip()
            if not current_name or code in seen:
                continue
            seen.add(code)
        else:
            phrase = str(candidate.get('query') or '').strip()
            if len(phrase) < 2:
                continue
            rj, _, err = call('GET', url, token, params={'query': phrase, 'dataType': '1', 'topK': '10'})
            if err or not rj:
                continue
            hits = (rj.get('data') or {}).get('hits') or []
            exact = next((item for item in hits if _normalize_security_name(str(item.get('name') or '')) == _normalize_security_name(phrase)), None)
            if not exact:
                continue
            code = str(exact.get('entity_id') or '').strip()
            if not re.match(r'^\d{6}$', code) or code == str(target_ticker or '') or code in seen:
                continue
            seen.add(code)
            current_name = str(exact.get('name') or '').strip()
        validated.append({
            'query': candidate.get('query', ''), 'code': code, 'current_name': current_name,
            'source_report_id': candidate.get('source_report_id', ''),
        })
        if len(validated) >= 3:
            break
    return validated


def _peer_material_text(item):
    return ' '.join(str(item.get(k, '') or '') for k in ('title', 'text', 'content', 'summary', 'abstract'))


def fetch_peer_materials(meta, company_name, peers, token):
    """按已精确核验的每个可比公司并发检索，并只保留该公司可识别的原始材料。"""
    url = meta.get('getMaterialsV2', {}).get('url', '')
    if not url:
        return None, 'getMaterialsV2 URL缺失'
    peers = [item for item in (peers or []) if item.get('current_name') and item.get('code')][:3]
    if not peers:
        return [], None
    def fetch_one(peer):
        body = {'question': f"{peer['current_name']}（{peer['code']}）最新业务进展、产品、订单、产能或业绩变化", 'queryScope': 'research,researchTable,meetingSummary', 'rewriteQuestion': False, 'size': 5}
        rj, status, err = call('POST', url, token, body=body, timeout=30)
        if err:
            return [], err
        raw_items = safe_get_data(rj) or []
        if not isinstance(raw_items, list):
            return [], f"无数据(status={status})"
        name, code = peer['current_name'], peer['code']
        matched = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            source = _peer_material_text(item)
            if name not in source and code not in source:
                continue
            enriched = dict(item)
            enriched.update({'peer_name': name, 'peer_code': code, 'peer_query': peer.get('query', '')})
            matched.append(enriched)
        return matched, None
    items, errors, seen = [], [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(peers)) as executor:
        futures = [executor.submit(fetch_one, peer) for peer in peers]
        for future in futures:
            matched, err = future.result()
            if err:
                errors.append(err)
            for item in matched:
                key = (item.get('peer_code'), str(item.get('id') or item.get('materialId') or item.get('title') or ''))
                if key not in seen:
                    seen.add(key)
                    items.append(item)
    return items, ('; '.join(errors) if errors and not items else None)

def fetch_ann_types(meta, token):
    url = meta.get("announcement_type", {}).get("url", "")
    if not url:
        return None, "URL缺失"
    rj, _, err = call("GET", url, token)
    if err:
        return None, err
    if not _api_ok(rj):
        return None, f"API返回失败: code={rj.get('code')}, msg={rj.get('message','')[:80]}"
    return rj, None


# ─────────────────────────────────────────────
# 公告链: announcement -> getAnnouncementDetail
# ─────────────────────────────────────────────

def fetch_announcements(meta, ticker, token, max_detail=3):
    ann_url = meta.get("announcement", {}).get("url", "")
    det_url = meta.get("getAnnouncementDetail", {}).get("url", "")
    if not ann_url:
        return [], "announcement URL缺失"

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    one_month_ago  = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    two_months_ago = (datetime.date.today() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")

    # 投资者关系类：优先排列，近30天，供投资者问答章节内容生成
    INVESTOR_TYPES = ["投资者关系"]
    # 其他重要类型：用于分析经营变化、资本运作、股权结构（近60天，事件频率较低）
    IMPORTANT_TYPES = [
        "经营数据", "业绩快报", "业绩预告",                         # 财务披露
        "非公开发行", "公开发行", "配股", "公司债", "可转债",         # 再融资
        "限制性股票", "员工持股计划",                                # 股权激励
        "增持", "减持", "回购", "股权质押",                          # 股权变动
        "资产重组", "吸收合并",                                       # 收购兼并
        "重大合同", "重大事项", "人事变动",                           # 日常经营重要事项
    ]

    def search_by_type(ann_type, start_date, page_size=5):
        rj, _, err = call("GET", ann_url, token,
                          params={"ticker": ticker, "startDate": start_date,
                                  "endDate": today_str, "pageSize": page_size,
                                  "annTypeID": ann_type})
        if err or not rj:
            return []
        data = rj.get("data") or {}
        return data if isinstance(data, list) else (
            data.get("list") or data.get("data") or data.get("announcements") or []
        )

    # 并行按类型定向搜索
    irq_items, other_items = [], []
    all_type_tasks = (
        [(t, one_month_ago,  5) for t in INVESTOR_TYPES] +
        [(t, two_months_ago, 3) for t in IMPORTANT_TYPES]
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(search_by_type, t, d, n): (t in INVESTOR_TYPES)
                for t, d, n in all_type_tasks}
        for fut in concurrent.futures.as_completed(futs):
            is_irq = futs[fut]
            items = fut.result()
            (irq_items if is_irq else other_items).extend(items)

    # 合并去重：投资者关系优先，排除定期报告
    EXCLUDE_KEYWORDS = ("年度报告", "半年度报告", "一季报", "三季报", "季度报告")
    seen_ids = set()
    merged = []
    for it in irq_items + other_items:
        uid = str(it.get("id") or it.get("annId") or "")
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        if not any(kw in (it.get("title") or "") for kw in EXCLUDE_KEYWORDS):
            merged.append(it)

    to_detail = merged[:max_detail]

    # 并行获取全文（优先前3条）
    def get_detail(item):
        ann_id = str(item.get("id") or item.get("annId") or "")
        detail = None
        if det_url and ann_id:
            det_rj, _, det_err = call("GET", det_url, token, params={"id": ann_id})
            detail = det_rj if not det_err else {"error": det_err}
        return {**item, "detail": detail}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(get_detail, it) for it in to_detail]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]
    for it in merged[max_detail:]:
        results.append({**it, "detail": None})

    return results, None


# ─────────────────────────────────────────────
def _bound_report_content(report_id, content_by_report):
    """Return the report-specific batch body, or ``None`` when it is unavailable."""
    body = (content_by_report or {}).get(str(report_id))
    if body is None:
        body = (content_by_report or {}).get(report_id)
    if not str(body or "").strip():
        return None
    return {"data": {str(report_id): body}}

# 研报链: research_search -> getReportDetail -> batchGetReportContentDomestic/Foreign + report_graph
# ─────────────────────────────────────────────

def fetch_research_reports(meta, ticker, company_name, token,
                           search_pages=2, max_detail=10):
    """研报链路：research_search → batchGetReportContentDomestic/Foreign（按orgType分流）+ report_graph（并行）
    不再调用 getReportDetail，一次批量拉取所有研报全文。
    """
    search_url = meta.get("research_search", {}).get("url", "")
    graph_url = meta.get("report_graph", {}).get("url", "")

    if not search_url:
        return [], "research_search URL缺失"

    # ── Step 1: 双路搜索，按代码 + 按名称，优先近1个月，无结果降级至近3个月
    def search_one(body):
        rj, _, err = call("POST", search_url, token, body=body)
        if err or not rj:
            return []
        data = rj.get("data") or {}
        lst = data if isinstance(data, list) else (
            data.get("list") or data.get("data") or []
        )
        return lst

    def _do_search(pub_time_start):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [
                ex.submit(search_one, {"type": "EXTERNAL_REPORT", "ticker": ticker,
                                       "reportType": "COMPANY", "pageNow": p,
                                       "pubTimeStart": pub_time_start, "pubTimeEnd": TODAY8,
                                       "sortOrder": "desc"})
                for p in range(1, search_pages + 1)
            ] + [
                ex.submit(search_one, {"type": "EXTERNAL_REPORT", "query": company_name,
                                       "reportType": "COMPANY", "pageNow": 1,
                                       "pubTimeStart": pub_time_start, "pubTimeEnd": TODAY8,
                                       "sortOrder": "desc"})
            ]
            results = []
            for f in concurrent.futures.as_completed(futs):
                results.extend(f.result())
        return results

    raw = _do_search(ONE_MONTH_AGO8)
    if not raw:
        raw = _do_search(THREE_MONTHS_AGO8)

    # 去重（按 report_id）
    seen, deduped = set(), []
    for item in raw:
        inner = item.get("data") if isinstance(item, dict) and "data" in item else item
        rid = str(inner.get("id") or inner.get("reportId") or "")
        if rid and rid not in seen:
            seen.add(rid)
            deduped.append({"_id": rid, "_meta": inner, "_raw": item})

    # 按机构多样性优先选 max_detail 篇
    selected = _select_diverse_reports(deduped, max_detail)

    if not selected:
        return [], None

    # ── Step 2: 按标题关键词排序（无需 getReportDetail）
    priority = sorted(selected,
                      key=lambda x: _report_priority(x.get("_meta")),
                      reverse=True)

    # ── Step 3: 批量获取全文（按内外资拆分接口）+ 并行获取图表
    # orgType 是整数：1/3 内资、2 外资。别按 orgName 有没有中文猜——高盛集团 /
    # 花旗集团 / 美国银行 / 巴克莱银行在库里都是中文机构名，orgType 却是 2。
    # 两个接口拿到不属于自己的 reportId 时返回 code=1 + 空 data（不是错误），
    # 所以分错边＝该篇正文静默消失。分类只当快通道，缺口必须反向再打一次。
    FOREIGN_ORG_TYPE = 2

    domestic_ids, foreign_ids = [], []
    for it in priority:
        try:
            rid_int = int(it["_id"])
        except (TypeError, ValueError, KeyError):
            continue
        try:
            org_type = int((it.get("_meta") or {}).get("orgType"))
        except (TypeError, ValueError):
            org_type = None
        (foreign_ids if org_type == FOREIGN_ORG_TYPE else domestic_ids).append(rid_int)

    domestic_url = meta.get("batchGetReportContentDomestic", {}).get("url", "")
    foreign_url  = meta.get("batchGetReportContentForeign", {}).get("url", "")

    def _batch_call(url, ids):
        if not ids or not url:
            return None
        rj, _, err = call("POST", url, token, body={"reportIds": ids})
        return rj if not err else None

    merged_data = {}

    def _merge(resp):
        if not isinstance(resp, dict):
            return
        sub = resp.get("data") or {}
        if not isinstance(sub, dict):
            return
        for key, value in sub.items():
            if str(value or "").strip() or key not in merged_data:
                merged_data[key] = value

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_dom = ex.submit(_batch_call, domestic_url, domestic_ids)
        f_for = ex.submit(_batch_call, foreign_url,  foreign_ids)
        dom_result = f_dom.result()
        for_result = f_for.result()
    _merge(dom_result)
    _merge(for_result)

    # 反向回捞：orgType 分错边或缺失的 id 上一轮拿不到正文，换另一个接口再打一次。
    # 正常情况下两个列表都是空的，不产生额外请求。
    def _blank(rid):
        return not str(merged_data.get(str(rid)) or "").strip()

    retry_foreign  = [rid for rid in domestic_ids if _blank(rid)]
    retry_domestic = [rid for rid in foreign_ids if _blank(rid)]
    if retry_foreign or retry_domestic:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f2_for = ex.submit(_batch_call, foreign_url,  retry_foreign)
            f2_dom = ex.submit(_batch_call, domestic_url, retry_domestic)
            _merge(f2_for.result())
            _merge(f2_dom.result())

    def get_graph(item):
        rid = item["_id"]
        graph = None
        if graph_url and rid:
            rj, _, err = call("GET", graph_url, token, params={"reportId": rid})
            graph = rj if not err else None
        return {**item, "detail": None, "content": _bound_report_content(rid, merged_data), "graph": graph}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(get_graph, it) for it in priority]
        final = [f.result() for f in concurrent.futures.as_completed(futs)]

    return final, None


def _select_diverse_reports(deduped, max_n):
    """按机构多样性贪心选取：每家机构最多2篇"""
    org_count = {}
    selected = []
    for item in deduped:
        inner = item.get("_meta") or {}
        org = inner.get("orgName") or inner.get("institution") or "unknown"
        if org_count.get(org, 0) < 2:
            selected.append(item)
            org_count[org] = org_count.get(org, 0) + 1
        if len(selected) >= max_n:
            break
    return selected


def _report_priority(item):
    if not item:
        return 0
    score = 0
    title = str(item.get("title") or item.get("reportTitle") or "")
    pages = item.get("pages") or item.get("pageCount") or 0
    try:
        pages = int(pages)
    except Exception:
        pages = 0
    if pages > 20:
        score += 3
    for kw in ("深度", "首次覆盖", "深度报告", "深度研究"):
        if kw in title:
            score += 2
            break
    return score


# ─────────────────────────────────────────────
# 会议纪要链: meeting_search (5页) -> getMeetingSummaryDetail
# ─────────────────────────────────────────────

def fetch_meetings(meta, ticker, token, pages=5, max_detail=5, company_name=""):
    search_url = meta.get("meeting_search", {}).get("url", "")
    detail_url = meta.get("getMeetingSummaryDetail", {}).get("url", "")
    if not search_url:
        return [], "meeting_search URL缺失"

    # 并行拉取5页（不传日期，结果按时间倒序）
    def fetch_page(page_no):
        rj, _, err = call("POST", search_url, token,
                          body={"ticker": ticker, "pageSize": 20, "pageNo": page_no})
        if err or not rj:
            return []
        data = rj.get("data") or {}
        lst = data if isinstance(data, list) else (
            data.get("list") or data.get("data") or data.get("records") or []
        )
        return lst or []

    raw = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=pages) as ex:
        futs = [ex.submit(fetch_page, p) for p in range(1, pages + 1)]
        for f in concurrent.futures.as_completed(futs):
            raw.extend(f.result())

    if not raw:
        return [], None

    # 筛选：优先近1个月，其次近3个月，最后取最新5条
    today = datetime.date.today()
    one_month_ago = today - datetime.timedelta(days=30)
    three_months_ago = today - datetime.timedelta(days=90)

    def parse_date(item):
        for key in ("pubTime", "publishTime", "meetingDate", "date", "createTime"):
            v = item.get(key) or ""
            if v:
                try:
                    return datetime.date.fromisoformat(str(v)[:10])
                except Exception:
                    pass
        return datetime.date(2000, 1, 1)

    PRIORITY_TITLES = ("路演", "业绩", "投资者", "调研", "策略会", "说明会")

    def score(item):
        d = parse_date(item)
        s = 0
        if d >= one_month_ago:
            s += 100
        elif d >= three_months_ago:
            s += 50
        title = str(item.get("title") or item.get("subject") or "")
        if any(kw in title for kw in PRIORITY_TITLES):
            s += 20
        # 机构维度加分
        org = item.get("orgName") or item.get("institution") or ""
        if org:
            s += 5
        return s

    ranked = sorted(raw, key=score, reverse=True)
    to_detail = ranked[:max_detail]

    # 并行获取详情
    def get_detail(item):
        mid = str(item.get("id") or item.get("meetingId") or "")
        detail = None
        if detail_url and mid:
            rj, _, err = call("GET", detail_url, token, params={"id": mid})
            detail = rj if not err else {"error": err}
        return {**item, "detail": detail}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_detail) as ex:
        futs = [ex.submit(get_detail, it) for it in to_detail]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    # A ticker search tag can represent a multi-stock industry meeting. Keep a
    # meeting only when its title names the target or its body discusses the target
    # repeatedly; otherwise its numeric facts are unsafe for a company report.
    target = str(company_name or "").strip()
    if target:
        aliases = {target}
        for suffix in ("股份有限公司", "集团股份有限公司", "有限公司", "集团"):
            aliases.add(target.replace(suffix, "").strip())
        aliases.discard("")

        def is_target_subject(item):
            title = str(item.get("title") or item.get("subject") or "")
            detail_data = ((item.get("detail") or {}).get("data") or {})
            body = "\n".join(str(detail_data.get(key) or "") for key in
                             ("aiOverview", "aiQa", "aiOriBody", "aiSummary"))
            if any(alias in title for alias in aliases):
                return True
            return any(body.count(alias) >= 2 for alias in aliases if len(alias) >= 2)

        results = [item for item in results if is_target_subject(item)]

    return results, None


# ─────────────────────────────────────────────
# 图表生成: data_to_image
# ─────────────────────────────────────────────

def generate_charts(meta, financial_data, main_comp_data, token,
                    ticker="", company_name=""):
    url = meta.get("data_to_image", {}).get("url", "")
    if not url:
        return {}, "data_to_image URL缺失"

    title_prefix = f"{company_name}({ticker})" if company_name and ticker else (company_name or ticker or "")

    # ── 取近n年年报，可选追加最新一期季报 ────────────────────────────
    def _select_periods(lst, n=3, include_latest_quarter=False):
        """
        取最近 n 个年报 (reportType==A)。
        先按 year 升序排列，确保不受接口返回顺序影响。
        若 include_latest_quarter=True，且排序后末尾有更新的非年报期（Q1/CQ1/S1等），
        则追加该期，使柱状图体现最新财报季。
        margin/structure 接口仅有年报数据，不需追加季报。
        """
        all_rows = sorted(lst or [], key=lambda x: str(x.get("year", "")))
        annual = [x for x in all_rows if x.get("reportType") == "A"]
        selected = annual[-n:]
        if include_latest_quarter and all_rows:
            last = all_rows[-1]
            if last.get("reportType") != "A":
                selected = selected + [last]
        return selected

    # ── 期间标签 ─────────────────────────────────────────────────────
    def _period_label(x):
        rt = x.get("reportType", "")
        yr = x.get("year", "")
        if rt == "A":
            return f"{yr}年报"
        if rt in ("Q1", "CQ1"):
            return f"{yr}Q1"
        if rt in ("Q3", "CQ3"):
            return f"{yr}前三季"
        if rt in ("S1", "H1"):
            return f"{yr}中报"
        return f"{yr}{rt}"

    # ── 图1: 营业收入及同比增速 ────────────────────────────────────────
    def _text_revenue():
        rows = _select_periods(
            (financial_data.get("fin_chart_revenue") or {}).get("data"),
            n=3, include_latest_quarter=True)
        if not rows:
            return None
        lines = "\n".join(
            f"  {_period_label(x)}: 营业收入={round(x['revenue']/1e8, 2)}亿元, 同比增速={round(x['revenueYOY'], 2)}%"
            for x in rows if x.get("revenue") is not None
        )
        return (f"图表标题: {title_prefix}:营业收入及增速\n"
                f"图表类型: 柱状图+折线图（左轴蓝色柱状图=营业收入单位亿元；右轴橙色折线图=同比增速单位%）\n"
                f"数据如下:\n{lines}\n"
                f"样式要求: 柱状图每柱顶部显示营业收入数值（保留1位小数，单位亿元）；折线每点标注增速数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
                f"数据来源: Datayes!")

    # ── 图2: 扣非净利润及同比增速 ─────────────────────────────────────
    def _text_profit():
        rows = _select_periods(
            (financial_data.get("fin_chart_profit") or {}).get("data"),
            n=3, include_latest_quarter=True)
        if not rows:
            return None
        lines = "\n".join(
            f"  {_period_label(x)}: 扣非净利润={round(x['niAttrPCut']/1e8, 2)}亿元, 同比增速={round(x['niAttrPCutYOY'], 2)}%"
            for x in rows if x.get("niAttrPCut") is not None
        )
        return (f"图表标题: {title_prefix}:扣非净利润及增速\n"
                f"图表类型: 柱状图+折线图（左轴蓝色柱状图=扣非净利润单位亿元；右轴橙色折线图=同比增速单位%）\n"
                f"数据如下:\n{lines}\n"
                f"样式要求: 柱状图每柱顶部显示扣非净利润数值（保留1位小数，单位亿元）；折线每点标注增速数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
                f"数据来源: Datayes!")

    # ── 图3: 分业务毛利率（分组柱状图）──────────────────────────────────
    def _text_margin():
        mg = financial_data.get("fin_chart_margin") or {}
        datas = (mg.get("data") or {}).get("datas") or []
        rows = _select_periods(datas, n=3, include_latest_quarter=False)
        if not rows:
            return None
        lines = []
        for row in rows:
            lbl = _period_label(row)
            for item in (row.get("items") or []):
                name = item.get("itemName") or item.get("name") or "?"
                val  = item.get("itemValue")
                if name and val is not None:
                    lines.append(f"  {lbl} {name}: {round(val, 2)}%")
        if not lines:
            return None
        return (f"图表标题: {title_prefix}:历年毛利率（业务板块）\n"
                f"图表类型: 分组柱状图（X轴=年份，Y轴=毛利率%，按业务板块分色分组）\n"
                f"数据如下:\n" + "\n".join(lines) + "\n"
                f"样式要求: 每个柱子顶部显示毛利率数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
                f"数据来源: Datayes!")

    # ── 图4: 历年收入结构（堆叠柱状图）──────────────────────────────────
    def _text_structure():
        st = financial_data.get("fin_chart_structure") or {}
        all_rows = st.get("data") if isinstance(st, dict) else st
        rows = _select_periods(all_rows, n=3, include_latest_quarter=False)
        if not rows:
            return None
        lines = []
        for row in sorted(rows, key=lambda x: x.get("year", "")):
            lbl = _period_label(row)
            for item in (row.get("items") or []):
                name = item.get("name") or "?"
                rev  = item.get("revenue")
                if name and rev is not None:
                    lines.append(f"{lbl},{name},{round(rev/1e8, 2)}亿元")
        if not lines:
            return None
        return (f"图表标题: {title_prefix}:历年收入结构\n"
                f"图表类型: 堆叠柱状图（X轴=年份，Y轴=亿元，各业务收入堆叠）\n"
                f"数据(格式:期间,业务,金额):\n" + "\n".join(lines) + "\n"
                f"样式要求: 堆叠柱中每层显示对应业务收入数值（保留1位小数，单位亿元）；占比过小或标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
                f"数据来源: Datayes!")

    tasks = {
        "revenue":   _text_revenue(),
        "profit":    _text_profit(),
        "margin":    _text_margin(),
        "structure": _text_structure(),
    }

    def gen_one(key, text):
        if not text:
            return key, None
        import time as _time
        for attempt in range(3):
            rj, _, err = call("POST", url, token, body={"text": text}, timeout=60)
            if not err and rj:
                img = (rj.get("chart_urls") or [None])[0]
                if img:
                    return key, str(img) if not isinstance(img, str) else img
            # 指数退避：1s, 3s, 5s
            _time.sleep([1, 3, 5][attempt])
        return key, None

    # 限制并发为2，避免触发服务端限流导致部分图表失败
    import concurrent.futures as _cf
    charts = {}
    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(gen_one, k, t): k for k, t in tasks.items()}
        for fut in _cf.as_completed(futs):
            k, img = fut.result()
            charts[k] = img

    return charts, None


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def run(ticker_input, token, output_path):
    log = []
    errors = []
    result = {}

    def record_error(api, err, url="", method="GET", params=None, body=None):
        curl_cmd = build_curl(method, url, params, body, token) if url else "（URL未知）"
        errors.append({"api": api, "error": str(err), "curl": curl_cmd})
        log.append(f"  ✗ {api}: {err}")

    print(f"\n{'='*60}")
    print(f"公司一页纸数据采集 | 输入: {ticker_input}")
    print(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # ── Phase 1: 元信息
    print("\n[1/5] 获取接口元信息...")
    meta = fetch_all_meta(token)
    ok_count = sum(1 for m in meta.values() if m.get("url"))
    print(f"  元信息获取完成: {ok_count}/{len(ALL_API_NAMES)} 个接口有URL")

    # ── Phase 2: 基础ID
    print("\n[2/5] 确认股票代码...")
    ticker, company_name, err = resolve_ticker(meta, ticker_input, ticker_input, token)
    if err:
        record_error("stock_search", err, meta.get("stock_search", {}).get("url", ""),
                     "GET", {"query": ticker_input, "dataType": "1", "topK": "5"})
        ticker = ticker_input
        company_name = ticker_input
    print(f"  ticker={ticker}  name={company_name}")

    period_type, period_value, err = get_ticker_period(meta, ticker, token)
    if err:
        record_error("ticker_period", err, meta.get("ticker_period", {}).get("url", ""),
                     "GET", {"ticker": ticker})
        period_type = "A"
    print(f"  最新财报期: type={period_type}  value={period_value}")

    result["__meta__"] = {
        "ticker": ticker,
        "name": company_name,
        "period_type": period_type,
        "period_value": period_value,
        "fetch_time": datetime.datetime.now().isoformat(),
    }

    # ── Phase 3: 并行主数据
    print("\n[3/5] 并行采集主数据...")

    tasks_def = {
        "company_info":       (fetch_company_info,    [meta, ticker, token]),
        "financial":          (fetch_financial,        [meta, ticker, period_type, token]),
        "fin_indicators":     (fetch_fin_indicators,   [meta, ticker, token]),
        "main_comp":          (fetch_main_comp,        [meta, ticker, period_type, token]),
        "main_comp_region":   (fetch_main_comp_region, [meta, ticker, period_type, token]),
        "main_comp_ratio":    (fetch_main_comp_ratio,  [meta, ticker, period_type, token]),
        "fin_chart_revenue":  (fetch_fin_chart,        [meta, "stock_financial_indicator_revenue", ticker, token]),
        "fin_chart_profit":   (fetch_fin_chart,        [meta, "stock_financial_indicator_net_profit", ticker, token]),
        "fin_chart_margin":   (fetch_fin_chart,        [meta, "stock_financial_indicator_gross_margin", ticker, token]),
        "fin_chart_structure":(fetch_fin_chart,        [meta, "stock_financial_indicator_earning_structure", ticker, token]),
        "executives":         (fetch_executives,       [meta, ticker, token]),
        "top_holders":        (fetch_top_holders,      [meta, ticker, token]),
        "inst_holding":       (fetch_inst_holding,     [meta, ticker, token]),
        "bonus":              (fetch_bonus,            [meta, ticker, token]),
        "ann_types":          (fetch_ann_types,        [meta, token]),
        "mgmt_discussion":    (fetch_mgmt_discussion,  [meta, ticker, token]),
        "org_survey":         (fetch_org_survey,       [meta, ticker, token]),
        "pe_valuation":       (fetch_pe_valuation,     [meta, ticker, token]),
        "valuation_rank":     (fetch_valuation_rank,   [meta, ticker, token]),
        "consensus":          (fetch_consensus,        [meta, ticker, token]),
        "profit_forecast":    (fetch_profit_forecast,  [meta, ticker, token]),
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fn, *args): key for key, (fn, args) in tasks_def.items()}
        for fut in concurrent.futures.as_completed(futs):
            key = futs[fut]
            try:
                data, err = fut.result()
                result[key] = data
                icon = "✓" if data and not err else ("△" if not err else "✗")
                print(f"  {icon} {key}: {'有数据' if data else '无数据'}" + (f" | {err}" if err else ""))
                if err:
                    api_name = key  # 近似，实际api名在函数内
                    record_error(api_name, err)
            except Exception as e:
                print(f"  ✗ {key}: 异常 {e}")
                record_error(key, str(e))

    # 从 company_info 补全公司名称（当 stock_search 未能精确匹配时）
    if re.match(r'^\d{6}$', company_name):
        ci = result.get("company_info") or {}
        ci_data = ci.get("data") or ci
        if isinstance(ci_data, dict):
            name_from_api = (ci_data.get("secShortName") or ci_data.get("name") or
                             ci_data.get("companyName") or ci_data.get("shortName") or "")
            if name_from_api:
                company_name = name_from_api
                result["__meta__"]["name"] = company_name
                print(f"  公司名称补全: {company_name}")

    # 公告链、研报链、会议纪要链并行启动
    print("  → 公告/研报/会议三链并行...")
    ex3 = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    ann_fut = ex3.submit(fetch_announcements, meta, ticker, token)
    rep_fut = ex3.submit(fetch_research_reports, meta, ticker, company_name, token)
    mtg_fut = ex3.submit(fetch_meetings, meta, ticker, token, company_name=company_name)

    anns, err = ann_fut.result()
    result["announcements"] = anns
    if err:
        record_error("announcement", err, meta.get("announcement", {}).get("url", ""), "GET")
    else:
        print(f"  ✓ announcements: {len(anns)} 条（含详情）")

    reports, err = rep_fut.result()
    result["research_reports"] = reports
    if err:
        record_error("research_search", err, meta.get("research_search", {}).get("url", ""), "GET")
    else:
        print(f"  ✓ research_reports: {len(reports)} 篇（含全文）")

    # 研报拿到后立即提取可比公司名，通过 stock_search 验证当前官方简称，再启动 getMaterialsV2
    peers = extract_peer_names(result.get("research_reports") or [], company_name)
    peer_validated = validate_peer_names(meta, peers, token, target_ticker=ticker)
    result["peer_validated"] = peer_validated
    peer_labels = [p['current_name'] for p in peer_validated]
    if peer_validated:
        print(f"  ✓ peer_validated: {[p['current_name'] for p in peer_validated]}")
    peer_fut = ex3.submit(fetch_peer_materials, meta, company_name, peer_validated, token)

    meetings, err = mtg_fut.result()
    result["meetings"] = meetings
    if err:
        record_error("meeting_search", err, meta.get("meeting_search", {}).get("url", ""), "POST")
    else:
        print(f"  ✓ meetings: {len(meetings)} 条（含详情）")

    peer_data, peer_err = peer_fut.result()
    ex3.shutdown(wait=False)
    result["peer_materials"] = peer_data
    if peer_err:
        record_error("getMaterialsV2", peer_err)
        print(f"  △ peer_materials: 无数据 | {peer_err}")
    elif not peer_validated:
        print("  - peer_materials: 跳过（无可核验候选）")
    elif peer_data:
        print(f"  ✓ peer_materials: {len(peer_data)} 条（可比公司: {', '.join(peer_labels)}）")
    else:
        print(f"  △ peer_materials: 未找到可核验材料（可比公司: {', '.join(peer_labels)}）")

    # ── Phase 4: 图表
    print("\n[4/5] 生成图表...")
    charts, err = generate_charts(meta, result, result.get("main_comp_ratio"), token,
                                   ticker=ticker, company_name=company_name)
    result["charts"] = charts
    chart_ok = sum(1 for v in charts.values() if v)
    print(f"  图表生成: {chart_ok}/4 成功")

    # ── Phase 5: 汇总错误、保存
    print("\n[5/5] 保存结果...")
    result["__errors__"] = errors
    if errors:
        curl_block = "⚠️ 以下接口调用失败，可手动验证：\n\n"
        for e in errors:
            curl_block += f"[{e['api']}]\n{e['curl']}\n\n"
        result["__failed_curls__"] = curl_block
    else:
        result["__failed_curls__"] = ""

    # 写入 JSON
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 数据已保存: {output_path}")
    print(f"  接口成功: {len(tasks_def) + 3 - len(errors)}/{len(tasks_def) + 3}")
    if errors:
        print(f"  失败接口({len(errors)}):")
        for e in errors:
            print(f"    - {e['api']}: {e['error']}")

    return output_path


def resolve_token(cli_token: str) -> str:
    """
    Token 查找优先级：
    1. --token 命令行参数
    2. DATAYES_TOKEN 环境变量
    3. 本地 token 文件（~/token.txt → 脚本同目录 token.txt → ~/.datayes_token）
    4. 以上均无则打印申请地址并退出
    """
    if cli_token:
        return cli_token

    # 2. 环境变量
    env_token = os.environ.get("DATAYES_TOKEN", "").strip()
    if env_token:
        print(f"  Token 来源: 环境变量 DATAYES_TOKEN")
        return env_token

    # 3. 本地文件（按优先级依次尝试）
    candidates = [
        os.path.expanduser("~/token.txt"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt"),
        os.path.expanduser("~/.datayes_token"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                token = open(path, encoding="utf-8").read().strip()
                if token:
                    print(f"  Token 来源: 本地文件 {path}")
                    return token
            except Exception:
                pass

    # 4. 未找到，提示申请
    print("❌ 未找到 Datayes Token。请通过以下任一方式提供：")
    print("   方式1（推荐）：设置环境变量  export DATAYES_TOKEN=your_token")
    print("   方式2：将 token 写入文件  ~/token.txt（仅一行，无引号）")
    print("   方式3：命令行参数  --token your_token")
    print()
    print("   尚未有 Token？访问通联数据 API 平台申请：")
    print("   https://ai.datayes.com")
    sys.exit(1)


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="公司一页纸数据采集脚本")
    parser.add_argument("--ticker", required=True, help="股票代码或名称，如 601888 或 中国中免")
    parser.add_argument("--token", default="", help="Datayes token（可选：优先读环境变量 DATAYES_TOKEN 或本地 ~/token.txt）")
    parser.add_argument("--output", default="", help="输出JSON路径（默认：当前目录/{ticker}_data.json）")
    args = parser.parse_args()

    ticker = args.ticker.strip()
    token = resolve_token(args.token.strip())
    output = args.output.strip()
    if not output:
        safe_name = ticker.replace("/", "_").replace("\\", "_")
        output = os.path.join(os.getcwd(), f"{safe_name}_data.json")

    run(ticker, token, output)


if __name__ == "__main__":
    main()
