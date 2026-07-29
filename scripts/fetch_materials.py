#!/usr/bin/env python3
"""Fetch Datayes materials for HK/US company one-pager reports."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import ssl
import sys
import time
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


def collect_research(client: DatayesClient, company: str, ticker: str, market: str, errors: list[dict[str, str]], max_reports: int) -> dict[str, Any]:
    exchange_map = {"HK": "XHKG", "US": "AMXO,XNAS,XNYS", "A": "SZSE,SSE"}
    exchange = exchange_map.get(market, "XHKG")
    items: dict[str, dict[str, Any]] = {}

    for days_back in (180, 365):
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
                "pageSize": 20,
                "sortOrder": "desc",
            }
            body.update(base)
            resp = safe_call(client, errors, "research_search", body)
            for row in ((data_of(resp) or {}).get("list") or []):
                datum = row.get("data") or row
                rid = str(datum.get("id") or "")
                if rid:
                    items[rid] = datum
        if len(items) >= 5:
            break

    selected = list(items.values())[:max_reports]
    ids = [x.get("id") for x in selected if x.get("id")]
    details = []
    graphs = []
    viewpoints = []

    for rid in ids[:max_reports]:
        details.append(data_of(safe_call(client, errors, "getReportDetail", {"reportId": rid})))
        graphs.append({"reportId": rid, "data": data_of(safe_call(client, errors, "report_graph", {"reportId": rid}))})
        viewpoints.append({"reportId": rid, "data": data_of(safe_call(client, errors, "core_viewpoint/", {"rrId": rid}))})
        time.sleep(1.05)

    # Split report IDs by orgType: domestic (Chinese org name) → batchGetReportContentDomestic,
    # foreign (non-Chinese org name) → batchGetReportContentForeign
    domestic_ids = []
    foreign_ids = []
    for rid, detail in zip(ids, details):
        if not isinstance(detail, dict):
            continue
        org_name = str(detail.get("orgName") or "")
        if any('一' <= ch <= '鿿' for ch in org_name):
            domestic_ids.append(str(rid))
        else:
            foreign_ids.append(str(rid))

    contents = {}
    if domestic_ids:
        dom_resp = safe_call(client, errors, "batchGetReportContentDomestic", {"reportIds": domestic_ids[:10]})
        dom_data = data_of(dom_resp)
        if isinstance(dom_data, dict):
            contents.update(dom_data)
    if foreign_ids:
        for_resp = safe_call(client, errors, "batchGetReportContentForeign", {"reportIds": foreign_ids[:10]})
        for_data = data_of(for_resp)
        if isinstance(for_data, dict):
            contents.update(for_data)
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
    for mid in list(found)[:max_meetings]:
        details.append({"id": mid, "data": data_of(safe_call(client, errors, "getMeetingSummaryDetail", {"id": mid}))})
        time.sleep(1.05)
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
        {"topic": "recent_updates", "question": f"{name} {market_name} 近况 业绩 指引 催化 资本市场 事件"},
        {"topic": "investment_logic", "question": f"{name} 投资逻辑 增长驱动 商业模式 竞争优势 风险"},
        {"topic": "business_financials", "question": f"{name} 业务拆分 收入 毛利率 利润率 ARR 客户 订单 财务预测"},
        {"topic": "supply_chain_ecosystem", "question": f"{name} 客户 供应商 渠道 生态伙伴 产业链 合作"},
        {"topic": "valuation_consensus", "question": f"{name} 估值 目标价 盈利预测 市场分歧 多空观点 同业对比"},
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
    results = []

    for item in build_material_questions(company, ticker, market):
        body = {
            "queryScope": query_scope,
            "question": item["question"],
            "rewriteQuestion": False,
            "size": size,
            "startTime": start,
            "endTime": end,
        }
        data = data_of(safe_call(client, errors, "getMaterialsV2", body))
        results.append({"topic": item["topic"], "question": item["question"], "data": data})
        time.sleep(0.8)

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

    filtered_count = 0
    for query in results:
        items = query.get("data") or []
        if not isinstance(items, list):
            continue
        before = len(items)
        query["data"] = [it for it in items if isinstance(it, dict) and _is_relevant(it)]
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
    target = choose_company(args, client, errors)
    ticker = target["ticker"]
    market = target["market"]
    company = target["company"]

    if not ticker:
        raise SystemExit("No ticker resolved. Provide --ticker for US stocks or ambiguous HK stocks.")

    exchange = target.get("exchange", "")
    full_ticker = target.get("full_ticker", ticker)
    output = {
        "__meta__": {
            "company": company,
            "ticker": ticker,
            "market": market,
            "exchange": exchange,
            "full_ticker": full_ticker,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
        "materials_v2": collect_materials_v2(client, company, ticker, market, errors, args.materials_days, args.materials_size),
        "structured": collect_structured(client, ticker, market, errors),
        "research": collect_research(client, company, ticker, market, errors, args.max_reports),
        "meetings": collect_meetings(client, company, ticker, market, errors, args.max_meetings),
        "announcements": collect_announcements(client, company, ticker, errors, args.max_announcements) if market in ("HK", "A") else {"list": [], "details": []},
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
