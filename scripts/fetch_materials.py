#!/usr/bin/env python3
"""Fetch Datayes materials for HK/US company one-pager reports."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
from pathlib import Path
import ssl
import sys
import time
import threading
import urllib.parse
import urllib.request
from typing import Any


API_INFO_URL = "https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api"
ALLOWED_HOSTS = {"gw.datayes.com", "api.datayes.com", "api.wmcloud.com", "r.datayes.com"}


def find_token() -> str:
    if os.environ.get("DATAYES_TOKEN"):
        return os.environ["DATAYES_TOKEN"].strip()

    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.extend([cwd / "token.txt", cwd.parent / "token.txt"])
    candidates.extend([Path.home() / "token.txt", Path.home() / ".datayes_token"])

    for path in candidates:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token

    raise SystemExit("DATAYES_TOKEN not found. Set DATAYES_TOKEN or place token.txt in the workspace.")


def normalize_ticker(ticker: str | None) -> str:
    if not ticker:
        return ""
    value = ticker.strip().upper()
    for suffix in (".HK", ".US", ".O", ".N", ".XHKG", ".XNAS", ".XNYS", ".AMXO", ".SZ", ".SS"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if value.isdigit() and len(value) < 5:
        value = value.zfill(5)
    return value


def infer_market(ticker: str, market: str | None) -> str:
    if market and market.lower() != "auto":
        return market.upper()
    raw = ticker.strip().upper()
    code = normalize_ticker(raw)
    # A股：6位纯数字 或 .SZ/.SS 后缀
    if raw.endswith((".SZ", ".SS")) or (code.isdigit() and len(code) == 6):
        return "A"
    if raw.endswith(".HK") or (code.isdigit() and len(code) == 5):
        return "HK"
    if raw.endswith((".O", ".N", ".US", ".XNAS", ".XNYS", ".AMXO")) or code.isalpha():
        return "US"
    return "auto"


def dates(days_back: int) -> tuple[str, str]:
    end = dt.date.today()
    start = end - dt.timedelta(days=days_back)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _len_or_zero(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict, str)) else 0


def _branch_summary(name: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    if name == "materials_v2":
        return {
            "queries": _len_or_zero(data.get("queries")),
            "unique_sources": _len_or_zero(data.get("unique_sources")),
            "filtered_out": data.get("filtered_out", 0),
        }
    if name == "research":
        return {
            "list": _len_or_zero(data.get("list")),
            "details": _len_or_zero(data.get("details")),
            "contents": _len_or_zero(data.get("contents")),
            "graphs": _len_or_zero(data.get("graphs")),
            "viewpoints": _len_or_zero(data.get("viewpoints")),
        }
    if name in ("meetings", "announcements"):
        return {
            "list": _len_or_zero(data.get("list")),
            "details": _len_or_zero(data.get("details")),
        }
    if name == "structured":
        summary: dict[str, Any] = {"keys": sorted(data.keys())}
        hk_financials = data.get("hk_financials")
        if isinstance(hk_financials, dict):
            summary["hk_financials"] = {k: _len_or_zero(v) for k, v in hk_financials.items()}
        return summary
    return {"keys": sorted(data.keys())}


def _format_branch_summary(summary: dict[str, Any]) -> str:
    parts = []
    for key, value in summary.items():
        if isinstance(value, dict):
            inner = ",".join(f"{k}:{v}" for k, v in value.items())
            parts.append(f"{key}={{{inner}}}")
        elif isinstance(value, list):
            parts.append(f"{key}={','.join(str(v) for v in value)}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


class DatayesClient:
    def __init__(self, token: str, insecure_ssl: bool = True) -> None:
        self.token = token
        self.context = ssl._create_unverified_context() if insecure_ssl else None
        self.info_cache: dict[str, dict[str, Any]] = {}

    def request(self, method: str, url: str, body: Any | None = None) -> Any:
        hostname = urllib.parse.urlparse(url).hostname or ""
        if hostname not in ALLOWED_HOSTS:
            raise ValueError(f"域名不在白名单: {hostname}")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=60, context=self.context) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def api_info(self, name_en: str) -> dict[str, Any]:
        if name_en not in self.info_cache:
            url = API_INFO_URL + "?" + urllib.parse.urlencode({"nameEn": name_en})
            resp = self.request("GET", url)
            if resp.get("code") != 1:
                raise RuntimeError(f"api_info failed for {name_en}: {resp.get('message')}")
            self.info_cache[name_en] = resp.get("data") or {}
        return self.info_cache[name_en]

    def call_api(self, name_en: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        info = self.api_info(name_en)
        method = (info.get("httpMethod") or "GET").upper()
        url = info.get("httpUrl") or ""
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}

        for p in info.get("parametersInput") or []:
            key = p.get("nameEn")
            if key not in params:
                continue
            location = (p.get("location") or "Query").lower()
            if location == "path":
                url = replace_path_param(url, key, str(params[key]), p.get("defaultValue"))
            elif location == "body":
                body[key] = params[key]
            else:
                query[key] = params[key]

        if method == "GET" and query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(query, doseq=True)
            return self.request("GET", url)
        if method == "POST":
            return self.request("POST", url, body)
        return self.request(method, url)


def replace_path_param(url: str, key: str, value: str, default: Any | None) -> str:
    if "{" + key + "}" in url:
        return url.replace("{" + key + "}", urllib.parse.quote(value))
    if default:
        return url.replace(str(default), urllib.parse.quote(value))
    return url.rstrip("/") + "/" + urllib.parse.quote(value)


def ok(resp: Any) -> bool:
    return isinstance(resp, dict) and (resp.get("code") in (1, 200) or resp.get("retCode") in (1, 200))


def data_of(resp: Any) -> Any:
    if not isinstance(resp, dict):
        return None
    return resp.get("data")


def safe_call(client: DatayesClient, errors: list[dict[str, str]], name: str, params: dict[str, Any] | None = None) -> Any:
    try:
        resp = client.call_api(name, params or {})
        if not ok(resp):
            errors.append({"api": name, "message": str(resp.get("message") if isinstance(resp, dict) else resp)[:500]})
        return resp
    except Exception as exc:  # Keep the fetch pipeline moving.
        errors.append({"api": name, "message": str(exc)[:500]})
        return None


def stock_search(client: DatayesClient, query: str, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not query:
        return []
    resp = safe_call(client, errors, "stock_search", {"dataType": "1", "query": query, "topK": "5"})
    data = data_of(resp) or {}
    return data.get("hits") or []


def choose_company(args: argparse.Namespace, client: DatayesClient, errors: list[dict[str, str]]) -> dict[str, str]:
    ticker = normalize_ticker(args.ticker)
    market = infer_market(args.ticker or "", args.market)
    company = args.company or ""

    if company and (not ticker or market == "auto"):
        hits = stock_search(client, company, errors)
        if hits and not ticker:
            first = hits[0]
            entity_id = str(first.get("entity_id") or "")
            if entity_id.isdigit() and len(entity_id) == 5:
                ticker = entity_id
                market = "HK"
                company = company or str(first.get("name") or "")
            elif entity_id.isdigit() and len(entity_id) == 6:
                ticker = entity_id
                market = "A"
                company = company or str(first.get("name") or "")

    if ticker and market == "auto":
        market = infer_market(ticker, "auto")
    if market == "auto":
        if ticker and ticker.isdigit() and len(ticker) == 6:
            market = "A"
        elif ticker and ticker.isalpha():
            market = "US"
        else:
            market = "HK"
    if not company:
        company = ticker

    # A股交易所推断
    exchange = ""
    full_ticker = ticker
    if market == "A":
        raw = (args.ticker or "").strip().upper()
        if raw.endswith(".SZ") or ticker.startswith(("0", "3")):
            exchange = "SZSE"
            full_ticker = ticker + ".SZ"
        elif raw.endswith(".SS") or ticker.startswith("6"):
            exchange = "SSE"
            full_ticker = ticker + ".SH"
        else:
            exchange = "SZSE"
            full_ticker = ticker + ".SZ"
    elif market == "HK":
        exchange = "XHKG"
        full_ticker = ticker + ".HK"

    return {"ticker": ticker, "market": market, "company": company, "exchange": exchange, "full_ticker": full_ticker}


def _research_matches_target(row: dict, company: str, ticker: str) -> bool:
    return _company_match_status(row, company, ticker) == "exact_target"

def _company_match_status(row: dict, company: str, ticker: str) -> str:
    t = (ticker or "").upper().replace(".HK", "")
    aliases = {t, (ticker or "").upper()}
    if t.isdigit():
        aliases.add(t.lstrip("0") or t)
    names = {company, company.lower(), company.upper()} if company else set()
    if "META" in t or "META" in (company or "").upper():
        names.update({"Meta", "META", "Meta Platforms", "METAPLATFORMS", "元平台", "元"})
        aliases.add("META")
    if "01024" in t or "1024" == t.lstrip("0") or "快手" in (company or ""):
        names.update({"快手", "快手-W", "Kuaishou", "KUAISHOU"})
        aliases.update({"01024", "1024"})
    fields = [row.get("title", ""), row.get("articleTitle", ""), row.get("companyName", ""),
              row.get("stockId", ""), row.get("secCode", ""), row.get("rrTitle", "")]
    text = " ".join(str(x or "") for x in fields)
    text_upper = text.upper()
    explicit = set(re.findall(r'\(([A-Z]{1,6}|\d{3,5})(?:\.[A-Z]{1,4})?\)', text_upper))
    allowed = {a.upper().replace(".HK", "") for a in aliases if a}
    allowed.update(a.lstrip("0") for a in list(allowed) if a.isdigit())
    if explicit and not any(x in allowed for x in explicit):
        return "unrelated"
    if any(str(a).lower() in text.lower() for a in names if a):
        return "exact_target"
    if any(str(a).upper() in text_upper for a in aliases if a):
        return "exact_target"
    peer_terms = {
        "NVDA": ["AMD", "INTC", "Intel", "Broadcom", "AVGO", "Marvell", "MRVL", "TSMC", "TSM"],
        "00700": ["Alibaba", "BABA", "9988", "网易", "NTES", "美团", "3690", "快手", "01024", "百度", "BIDU", "京东", "JD"],
        "700": ["Alibaba", "BABA", "9988", "网易", "NTES", "美团", "3690", "快手", "01024", "百度", "BIDU", "京东", "JD"],
        "03690": ["Alibaba", "BABA", "9988", "JD", "9618", "PDD", "Kuaishou", "01024"],
        "META": ["Alphabet", "GOOGL", "SNAP", "Pinterest", "PINS", "TikTok", "ByteDance"],
    }
    for key, terms in peer_terms.items():
        if key in allowed and any(term.upper() in text_upper for term in terms):
            return "related_peer"
    if any(k in text_upper for k in ["INDUSTRY", "SECTOR", "AI", "CLOUD", "SEMICONDUCTOR"]):
        return "industry_background"
    return "unrelated"


def collect_research(client: DatayesClient, company: str, ticker: str, market: str, errors: list[dict[str, str]], max_reports: int) -> dict[str, Any]:
    exchange_map = {"HK": "XHKG", "US": "AMXO,XNAS,XNYS", "A": "SZSE,SSE"}
    exchange = exchange_map.get(market, "XHKG")
    items: dict[str, dict[str, Any]] = {}

    for days_back in (180, 365, 730, 1095):
        start, end = dates(days_back)
        bodies: list[dict[str, Any]] = []
        if ticker:
            bodies.append({"ticker": ticker})
        if company:
            bodies.append({"query": company})
        for base in bodies:
            body = {
                "type": "EXTERNAL_REPORT",
                "reportType": "COMPANY",
                "exchangeCode": exchange,
                "pubTimeStart": start,
                "pubTimeEnd": end,
                "pageNow": 1,
                "pageSize": 50,
                "sortOrder": "desc",
            }
            body.update(base)
            resp = safe_call(client, errors, "research_search", body)
            for row in ((data_of(resp) or {}).get("list") or []):
                datum = row.get("data") or row
                rid = str(datum.get("id") or "")
                if rid:
                    items[rid] = datum
        if len(items) >= max(max_reports, 20):
            break

    selected = []
    for x in items.values():
        status = _company_match_status(x, company, ticker)
        x["company_match"] = status
        if status == "exact_target":
            selected.append(x)
    selected = selected[:max_reports]
    ids = [x.get("id") for x in selected if x.get("id")]
    selected_by_id = {str(x.get("id")): x for x in selected}

    # ── 并发拉取每篇报告的 detail / graph / viewpoint ──
    def _fetch_report_trio(rid: str) -> tuple[str, Any, Any, Any]:
        detail = data_of(safe_call(client, errors, "getReportDetail", {"reportId": rid}))
        graph   = data_of(safe_call(client, errors, "report_graph",   {"reportId": rid}))
        vp      = data_of(safe_call(client, errors, "core_viewpoint/", {"rrId": rid}))
        return rid, detail, graph, vp

    details = []
    detail_ids = []
    graphs = []
    viewpoints = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut_map = {ex.submit(_fetch_report_trio, rid): rid for rid in ids[:max_reports]}
        trio_results: dict[str, tuple] = {}
        for fut in concurrent.futures.as_completed(fut_map):
            try:
                rid, detail, graph, vp = fut.result()
                trio_results[str(rid)] = (detail, graph, vp)
            except Exception as exc:
                errors.append({"api": "report_trio", "message": str(exc)[:300]})

    # 按原始顺序整理结果，过滤非目标公司
    for rid in ids[:max_reports]:
        trio = trio_results.get(str(rid))
        if trio is None:
            continue
        detail, graph, vp = trio
        if isinstance(detail, dict):
            detail["company_match"] = _company_match_status(detail, company, ticker)
            if detail["company_match"] != "exact_target":
                src = selected_by_id.get(str(rid), {})
                detail["company_match"] = _company_match_status(src, company, ticker)
            if detail["company_match"] != "exact_target":
                continue
        details.append(detail)
        detail_ids.append(rid)
        graphs.append({"reportId": rid, "data": graph})
        viewpoints.append({"reportId": rid, "data": vp})

    # 研报全文按机构属性拆成内资/外资两个接口，各自对「不属于自己」的 reportId
    # 返回 code=1 + 空 data（不是错误）——分错边就是正文静默消失。
    # 这里不做分类：先全打内资，再把没拿到正文的 id 交给外资接口回捞。
    # （曾按 orgName 含不含中文猜，实测 160 篇样本里 24 篇外资错判 15 篇——
    #   高盛集团 / 花旗集团 / 美国银行 / 巴克莱银行 / 里昂证券 都是中文机构名。）
    content_ids = [str(rid) for rid, detail in zip(detail_ids, details) if isinstance(detail, dict)]

    def _fetch_contents(api_name: str, ids: list[str]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for start in range(0, len(ids), 10):  # 接口硬上限 10 个/次
            chunk = ids[start:start + 10]
            if not chunk:
                continue
            data = data_of(safe_call(client, errors, api_name, {"reportIds": chunk}))
            if isinstance(data, dict):
                merged.update(data)
        return merged

    contents = _fetch_contents("batchGetReportContentDomestic", content_ids)
    missing = [rid for rid in content_ids if not str(contents.get(rid) or "").strip()]
    if missing:
        for key, value in _fetch_contents("batchGetReportContentForeign", missing).items():
            if str(value or "").strip() or key not in contents:
                contents[key] = value
    if not contents:
        contents = None

    return {"list": selected, "details": details, "contents": contents, "graphs": graphs, "viewpoints": viewpoints}


def collect_meetings(client: DatayesClient, company: str, ticker: str, market: str, errors: list[dict[str, str]], max_meetings: int) -> dict[str, Any]:
    start, end = dates(180)
    market_type_map = {"HK": "港股", "US": "美股", "A": "A股"}
    market_type = market_type_map.get(market, "港股")
    found: dict[str, dict[str, Any]] = {}

    for page in range(1, 4):
        body = {
            "ticker": ticker,
            "input": company or ticker,
            "marketType": market_type,
            "meetingType": "业绩说明会,机构调研,电话会议",
            "pubTimeStart": start + "000000",
            "pubTimeEnd": end + "235959",
            "pageNo": page,
            "pageSize": 20,
        }
        resp = safe_call(client, errors, "meeting_search", body)
        for row in ((data_of(resp) or {}).get("list") or []):
            mid = str(row.get("id") or (row.get("data") or {}).get("id") or "")
            if mid:
                found[mid] = row.get("data") or row

    details = []
    mids = list(found)[:max_meetings]

    def _fetch_meeting(mid: str) -> dict:
        return {"id": mid, "data": data_of(safe_call(client, errors, "getMeetingSummaryDetail", {"id": mid}))}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_meeting, mid): mid for mid in mids}
        mid_results: dict[str, dict] = {}
        for fut in concurrent.futures.as_completed(futs):
            try:
                res = fut.result()
                mid_results[res["id"]] = res
            except Exception as exc:
                errors.append({"api": "getMeetingSummaryDetail", "message": str(exc)[:300]})
    # 保持顺序
    details = [mid_results[mid] for mid in mids if mid in mid_results]
    return {"list": list(found.values())[:max_meetings], "details": details}


def collect_announcements(client: DatayesClient, company: str, ticker: str, errors: list[dict[str, str]], max_announcements: int) -> dict[str, Any]:
    start, end = dates(120)
    safe_call(client, errors, "announcement_type", {})
    resp = safe_call(client, errors, "announcement", {
        "ticker": ticker,
        "input": company or ticker,
        "beginDate": start,
        "endDate": end,
        "pageNow": 1,
        "pageSize": 20,
    })
    rows = ((data_of(resp) or {}).get("list") or [])
    details = []
    for row in rows[:max_announcements]:
        datum = row.get("data") or row
        aid = datum.get("id") or datum.get("announcementId")
        if aid:
            details.append({"id": aid, "data": data_of(safe_call(client, errors, "getAnnouncementDetail", {"id": aid}))})
    return {"list": rows, "details": details}


def collect_structured(client: DatayesClient, ticker: str, market: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if market == "HK" and ticker:
        begin, end = dates(1100)
        result["hk_financials"] = {
            "getHkFdmtIsPit": data_of(safe_call(client, errors, "getHkFdmtIsPit", {
                "ticker": ticker,
                "beginDate": begin,
                "endDate": end,
                "pagenum": 1,
                "pagesize": 50,
            })),
            "getHkFdmtBsPit": data_of(safe_call(client, errors, "getHkFdmtBsPit", {
                "ticker": ticker,
                "beginDate": begin,
                "endDate": end,
                "pagenum": 1,
                "pagesize": 50,
            })),
            "getHkFdmtCfPit": data_of(safe_call(client, errors, "getHkFdmtCfPit", {
                "ticker": ticker,
                "beginDate": begin,
                "endDate": end,
                "pagenum": 1,
                "pagesize": 50,
            })),
        }
    elif market == "A" and ticker:
        result["fdmtNew"] = data_of(safe_call(client, errors, "fdmtNew", {
            "ticker": ticker,
            "mergedFlag": 1,
            "reportType": "SUMMARY",
            "displaySort": "left",
            "duration": "ACCUMULATE",
            "includeLatest": True,
            "period": 4,
            "reportPeriodType": "A,Q1",
        }))

    return result


def build_material_questions(company: str, ticker: str, market: str) -> list[dict[str, str]]:
    name = " ".join(x for x in (company, ticker) if x).strip()
    market_map = {"HK": "港股", "US": "美股", "A": "A股"}
    market_name = market_map.get(market, "港股")
    return [
        {"topic": "business_segments_history", "question": f"{name} 2023 annual report 2024 annual report 2025 annual report segment revenue revenue mix revenue share value-added services online advertising fintech business services"},
        {"topic": "recent_updates", "question": f"{name} {market_name} 近况 业绩 指引 催化 资本市场 事件"},
        {"topic": "investment_logic", "question": f"{name} 投资逻辑 增长驱动 商业模式 竞争优势 风险"},
        {"topic": "business_financials", "question": f"{name} 业务拆分 收入 毛利率 利润率 ARR 客户 订单 财务预测"},
        {"topic": "business_segments_annual", "question": f"{name} 近三年 年报 分业务收入 占比 毛利率 FY2023 FY2024 FY2025 segment revenue"},
        {"topic": "supply_chain_ecosystem", "question": f"{name} 客户 供应商 渠道 生态伙伴 产业链 合作"},
        {"topic": "valuation_consensus", "question": f"{name} 估值 目标价 盈利预测 市场分歧 多空观点 同业对比"},
        {"topic": "peer_discovery", "question": f"{name} 同业 可比公司 竞争对手 上市公司 最新业务进展 peer comparable competitor"},
        {"topic": "market_focus", "question": f"{name} 市场关注 调研问题 风险 解禁 监管 竞争 下一次验证点"},
    ]


def collect_materials_v2(
    client: DatayesClient,
    company: str,
    ticker: str,
    market: str,
    errors: list[dict[str, str]],
    days_back: int,
    size: int,
) -> dict[str, Any]:
    start, end = dates(days_back)
    query_scope = "research,meetingSummary,marketView,wechat"
    questions = build_material_questions(company, ticker, market)

    def _fetch_one_query(item: dict[str, str]) -> dict[str, Any]:
        body = {
            "queryScope": query_scope,
            "question": item["question"],
            "rewriteQuestion": False,
            "size": size,
            "startTime": start,
            "endTime": end,
        }
        data = data_of(safe_call(client, errors, "getMaterialsV2", body))
        return {"topic": item["topic"], "question": item["question"], "data": data}

    # 多条 query 并发，限制 3 个并发避免限流
    results: list[dict[str, Any]] = [{}] * len(questions)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        fut_map = {ex.submit(_fetch_one_query, item): i for i, item in enumerate(questions)}
        for fut in concurrent.futures.as_completed(fut_map):
            idx = fut_map[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                errors.append({"api": "getMaterialsV2", "message": str(exc)[:300]})
                results[idx] = {"topic": questions[idx]["topic"], "question": questions[idx]["question"], "data": None}

    # ── 相关性过滤：去除不涉及目标公司的无关条目 ──
    keywords = []
    if company:
        keywords.append(company)
    if ticker:
        keywords.append(ticker)
        keywords.append(ticker.upper())
        keywords.append(ticker.lower())

    def _is_relevant(item: dict) -> bool:
        if not keywords:
            return True
        text = (item.get("text") or item.get("content") or item.get("title") or "")
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                return True
        # 标题不命中视为不相关
        title = (item.get("title") or "").lower()
        for kw in keywords:
            if kw.lower() in title:
                return True
        return False

    def _mark_material(item: dict) -> str:
        meta = item.get("metadata") or {}
        probe = {
            "title": item.get("title", ""),
            "articleTitle": item.get("title", ""),
            "companyName": meta.get("companyName", ""),
            "stockId": meta.get("stockId", ""),
            "secCode": meta.get("secCode", ""),
            "rrTitle": item.get("title", ""),
        }
        return _company_match_status(probe, company, ticker)

    filtered_count = 0
    for query in results:
        items = query.get("data") or []
        if not isinstance(items, list):
            continue
        before = len(items)
        kept = []
        topic = query.get("topic", "")
        for it in items:
            if not isinstance(it, dict):
                continue
            match_status = _mark_material(it)
            if topic != "peer_discovery" and not _is_relevant(it):
                continue
            if topic == "peer_discovery" and match_status == "unrelated":
                continue
            it["company_match"] = match_status
            if it["company_match"] in ("exact_target", "industry_background", "related_peer"):
                kept.append(it)
        query["data"] = kept
        filtered_count += before - len(query["data"])

    # ── 从所有 query 结果中提取去重后的独立来源 ──
    sources: dict[str, dict[str, Any]] = {}
    for query in results:
        items = query.get("data") or []
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            meta = it.get("metadata") or {}
            rid = str(meta.get("id") or it.get("id") or "")
            if not rid:
                continue
            if rid not in sources:
                dtype = it.get("dataType", "unknown")
                type_map = {
                    "research": "Materials V2研报",
                    "meetingSummary": "Materials V2纪要",
                    "marketView": "Materials V2市场观点",
                    "wechat": "Materials V2微信",
                }
                sources[rid] = {
                    "id": rid,
                    "type": type_map.get(dtype, f"Materials V2/{dtype}"),
                    "company_match": it.get("company_match", _mark_material(it)),
                    "dataType": dtype,
                    "title": it.get("title", ""),
                    "organization": meta.get("organization", ""),
                    "analyst": meta.get("analyst", ""),
                    "source": meta.get("source", ""),
                    "publishTime": meta.get("publishTime", ""),
                    "url": it.get("url", ""),
                }

    return {
        "query_scope": query_scope,
        "start_time": start,
        "end_time": end,
        "queries": results,
        "unique_sources": list(sources.values()),
        "filtered_out": filtered_count,
    }


def main() -> int:
    total_t0 = time.time()
    parser = argparse.ArgumentParser(description="Fetch HK/US company one-pager materials from Datayes.")
    parser.add_argument("--company", default="", help="Company name, e.g. 腾讯控股 or NVIDIA")
    parser.add_argument("--ticker", default="", help="Ticker, e.g. 00700.HK or NVDA")
    parser.add_argument("--market", default="auto", choices=["auto", "HK", "US", "A", "hk", "us", "a"], help="Market")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--max-reports", type=int, default=8)
    parser.add_argument("--max-meetings", type=int, default=5)
    parser.add_argument("--max-announcements", type=int, default=5)
    parser.add_argument("--materials-days", type=int, default=365)
    parser.add_argument("--materials-size", type=int, default=10)
    args = parser.parse_args()

    token = find_token()
    client = DatayesClient(token)
    errors: list[dict[str, str]] = []
    target_t0 = time.time()
    target = choose_company(args, client, errors)
    target_elapsed = time.time() - target_t0
    ticker = target["ticker"]
    market = target["market"]
    company = target["company"]

    if not ticker:
        raise SystemExit("No ticker resolved. Provide --ticker for US stocks or ambiguous HK stocks.")

    exchange = target.get("exchange", "")
    full_ticker = target.get("full_ticker", ticker)
    timings: dict[str, Any] = {
        "target_resolution": {
            "elapsed_seconds": round(target_elapsed, 3),
            "status": "ok",
        },
        "branches": {},
    }
    timings_lock = threading.Lock()

    def timed_branch(name: str, func: Any, *branch_args: Any) -> Any:
        started_at = dt.datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        print(f"[fetch][start] {name}", flush=True)
        try:
            data = func(*branch_args)
            elapsed = time.time() - t0
            summary = _branch_summary(name, data)
            with timings_lock:
                timings["branches"][name] = {
                    "status": "ok",
                    "started_at": started_at,
                    "elapsed_seconds": round(elapsed, 3),
                    "summary": summary,
                }
            print(f"[fetch][done] {name} {elapsed:.1f}s {_format_branch_summary(summary)}", flush=True)
            return data
        except Exception as exc:
            elapsed = time.time() - t0
            with timings_lock:
                timings["branches"][name] = {
                    "status": "failed",
                    "started_at": started_at,
                    "elapsed_seconds": round(elapsed, 3),
                    "error": str(exc)[:300],
                }
            print(f"[fetch][fail] {name} {elapsed:.1f}s {str(exc)[:120]}", flush=True)
            raise

    # ── 四大采集函数并发执行 ──
    print("[fetch] 并发采集：materials_v2 / structured / research / meetings", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut_mv2   = ex.submit(timed_branch, "materials_v2", collect_materials_v2, client, company, ticker, market,
                              errors, args.materials_days, args.materials_size)
        fut_struct = ex.submit(timed_branch, "structured", collect_structured, client, ticker, market, errors)
        fut_res   = ex.submit(timed_branch, "research", collect_research, client, company, ticker, market, errors, args.max_reports)
        fut_meet  = ex.submit(timed_branch, "meetings", collect_meetings, client, company, ticker, market, errors, args.max_meetings)
        fut_ann   = ex.submit(
            timed_branch, "announcements", collect_announcements, client, company, ticker, errors, args.max_announcements
        ) if market in ("HK", "A") else None

        materials_v2_data  = fut_mv2.result()
        structured_data    = fut_struct.result()
        research_data      = fut_res.result()
        meetings_data      = fut_meet.result()
        announcements_data = fut_ann.result() if fut_ann else {"list": [], "details": []}
    timings["total_elapsed_seconds"] = round(time.time() - total_t0, 3)

    output = {
        "__meta__": {
            "company": company,
            "ticker": ticker,
            "market": market,
            "exchange": exchange,
            "full_ticker": full_ticker,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
        "__timings__": timings,
        "materials_v2":  materials_v2_data,
        "structured":    structured_data,
        "research":      research_data,
        "meetings":      meetings_data,
        "announcements": announcements_data,
        "errors": errors,
    }

    out_path = Path(args.output)
    if out_path.exists():
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = out_path.stem
        suffix = out_path.suffix
        out_path = out_path.with_name(f"{stem}_{ts}{suffix}")
        print(f"[WARN] 目标文件已存在，输出重命名为: {out_path.name}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
