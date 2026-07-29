#!/usr/bin/env python3
"""
v1.2.2 来源自动补充与冻结模块

执行顺序：
  fetch_structured_sources()      — fdmtNew / HK PIT
  fetch_materials_v2_sources()    — 研报/纪要/公告/微信/新闻
  fetch_report_fulltext()         — 研报正文
  fetch_minutes()                 — 纪要详情
  fetch_public_sources()          — 公开权威来源（当内部数据不足时）
  normalize_sources()             — 统一字段映射
  deduplicate_sources()           — 按ID去重
  freeze_materials()              — 锁定JSON并计算SHA-256

所有来源写入统一字段：
  internal_source_key, material_id|url, source_type, date,
  organization, title, api, content/text, fetched_at
"""
import hashlib, json, os, re, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


API_INFO_URL = "https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api"
ALLOWED_HOSTS = {"gw.datayes.com", "api.datayes.com", "api.wmcloud.com", "r.datayes.com"}
MATERIALS_SEARCH_URL = "https://gw.datayes.com/gptMaterials/v2/gpt/search"


def find_token() -> str:
    t = os.environ.get("DATAYES_TOKEN", "").strip()
    if t:
        return t
    for p in [Path.cwd() / "token.txt", Path.home() / "token.txt", Path.home() / ".datayes_token"]:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    raise SystemExit("DATAYES_TOKEN not found.")


class SourceClient:
    def __init__(self, token: str):
        self.token = token
        self.ctx = ssl._create_unverified_context()

    def _req(self, method: str, url: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=90, context=self.ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def api_info(self, name_en: str) -> dict:
        url = API_INFO_URL + "?" + urllib.parse.urlencode({"nameEn": name_en})
        resp = self._req("GET", url)
        if resp.get("code") != 1:
            raise RuntimeError(f"api_info failed: {resp.get('message')}")
        return resp.get("data") or {}

    def call_api(self, name_en: str, url_params: dict = None, body: dict = None) -> Any:
        info = self.api_info(name_en)
        http_url = info.get("httpUrl", "")
        http_method = info.get("httpMethod", "GET")
        params = info.get("parametersInput", [])

        # Replace path params
        for p in params:
            if p.get("location") == "Path" and url_params and p.get("name") in url_params:
                http_url = http_url.replace("{" + p["name"] + "}", str(url_params[p["name"]]))

        # Add query params
        if url_params:
            qp = {p["name"]: str(url_params[p["name"]])
                  for p in params if p.get("location") == "Query" and p["name"] in url_params}
            if qp:
                http_url += "?" + urllib.parse.urlencode(qp)

        req_body = None
        if http_method.upper() == "POST" and body:
            req_body = {p["name"]: body[p["name"]]
                        for p in params if p.get("location") == "Body" and p["name"] in body}

        resp = self._req(http_method, http_url, req_body)
        if resp.get("code") != 1:
            raise RuntimeError(f"API {name_en} failed: {resp.get('message')}")
        return resp.get("data") or {}

    def search_materials(self, query: str, data_types: list[str], days: int = 365, size: int = 20) -> list[dict]:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        all_results = []
        for dtype in data_types:
            body = {
                "query": query,
                "queryScope": dtype,
                "size": size,
                "startTime": start,
                "endTime": end,
            }
            try:
                resp = self._req("POST", MATERIALS_SEARCH_URL, body)
                items = resp.get("data", {}).get("data", []) if isinstance(resp.get("data"), dict) else []
                all_results.extend(items)
            except Exception:
                pass
        return all_results


def _key_from_source(s: dict) -> str:
    sid = str(s.get("id") or s.get("material_id") or s.get("url") or "")
    title = (s.get("title") or "")[:40]
    return f"{sid}|{title}"


def normalize_source(raw: dict, dtype: str, api: str, key_prefix: str = "SRC") -> dict:
    """Normalize a raw source into unified format."""
    sid = str(raw.get("id", ""))
    meta = raw.get("metadata") or {}
    pub_time = meta.get("publishTime") or raw.get("publishTime") or raw.get("date") or ""
    title = raw.get("title") or meta.get("title") or ""
    org = meta.get("organization") or raw.get("organization") or raw.get("source") or ""
    text = raw.get("text") or raw.get("content") or raw.get("snippet") or ""

    if pub_time:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(pub_time))
        pub_time = m.group(1) if m else ""

    return {
        "internal_source_key": f"{key_prefix}-{sid}" if sid else "",
        "material_id": sid,
        "source_type": dtype,
        "date": pub_time,
        "organization": org,
        "title": re.sub(r"<[^>]+>", "", title),
        "api": api,
        "text": text,
        "fetched_at": datetime.now().isoformat(),
    }


def fetch_structured_sources(client: SourceClient, ticker: str, market: str) -> dict:
    """Fetch structured financial data."""
    result = {}
    if market == "A":
        api_ticker = ticker if "." in ticker else f"{ticker}.SZ"
        try:
            data = client.call_api("fdmtNew", url_params={
                "ticker": api_ticker,
                "reportType": "ACCUMULATE",
            })
            result["fdmtNew"] = data
        except Exception as e:
            result["fdmtNew_error"] = str(e)
    elif market == "HK":
        api_ticker = ticker if "." in ticker else f"{ticker}.HK"
        for api_name, field in [
            ("getHkFdmtIsPit", "getHkFdmtIsPit"),
            ("getHkFdmtBsPit", "getHkFdmtBsPit"),
            ("getHkFdmtCfPit", "getHkFdmtCfPit"),
        ]:
            try:
                data = client.call_api(api_name, url_params={"ticker": api_ticker})
                result[field] = data
            except Exception as e:
                result[f"{field}_error"] = str(e)
    return result


def fetch_materials_v2_sources(client: SourceClient, company: str, ticker: str, market: str) -> list[dict]:
    """Fetch research, meetings, announcements via Materials V2."""
    data_types = ["research", "meetingSummary", "announcement", "news", "wechat", "marketView"]
    queries = [company, ticker]
    if market == "A":
        queries.append(f"{ticker}.SZ")
    elif market == "HK":
        queries.append(f"{ticker}.HK")

    raw_sources: list[dict] = []
    seen = set()
    for q in queries:
        for dtype in data_types:
            results = client.search_materials(q, [dtype], days=365, size=10)
            for r in results:
                key = _key_from_source(r)
                if key not in seen:
                    seen.add(key)
                    raw_sources.append(r)
            time.sleep(0.3)
    return raw_sources


def fetch_report_fulltext(client: SourceClient, sources: list[dict], max_reports: int = 8) -> list[dict]:
    """Fetch full text for research reports via batchGetReportContent."""
    research_sources = [s for s in sources if s.get("dataType") in ("research", "marketView")]
    research_sources = research_sources[:max_reports]

    enriched = []
    for s in research_sources:
        sid = s.get("id")
        if not sid:
            enriched.append(s)
            continue
        try:
            data = client.call_api("batchGetReportContent", body={"reportIds": [str(sid)]})
            contents = data.get("contents") or data.get("data") or []
            if contents and isinstance(contents, list) and len(contents) > 0:
                c = contents[0]
                if isinstance(c, dict):
                    s = dict(s)
                    s["text"] = c.get("text") or c.get("content") or s.get("text", "")
                    s["report_title"] = c.get("title") or s.get("title", "")
            enriched.append(s)
        except Exception:
            enriched.append(s)
        time.sleep(0.3)
    return enriched


def normalize_all_sources(
    raw_sources: list[dict],
    structured: dict,
    company: str,
    ticker: str,
    market: str,
) -> list[dict]:
    """Normalize all raw sources into unified format."""
    normalized = []
    counter = 1

    # Add structured data as A-prefix sources
    if market == "A" and "fdmtNew" in structured:
        normalized.append({
            "internal_source_key": "A1",
            "material_id": "",
            "source_type": "structured",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "organization": "Datayes",
            "title": f"fdmtNew A股财务摘要（ACCUMULATE口径，合并报表）| 证券代码：{ticker}",
            "api": "fdmtNew",
            "text": "",
            "fetched_at": datetime.now().isoformat(),
            "is_structured": True,
        })
        counter = 1
    elif market == "HK" and any(k.startswith("getHkFdmt") for k in structured):
        for label, api_name in [("A1", "getHkFdmtIsPit"), ("A2", "getHkFdmtBsPit"), ("A3", "getHkFdmtCfPit")]:
            if api_name in structured:
                normalized.append({
                    "internal_source_key": label,
                    "material_id": "",
                    "source_type": "structured",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "organization": "Datayes",
                    "title": f"HK PIT {api_name} | 证券代码：{ticker}",
                    "api": api_name,
                    "text": "",
                    "fetched_at": datetime.now().isoformat(),
                    "is_structured": True,
                })

    # Normalize material sources
    for s in raw_sources:
        sid = s.get("id", "")
        dtype = s.get("dataType", "research")
        if not sid:
            continue
        key = f"SRC-{sid}"
        norm = normalize_source(s, dtype, "getMaterialsV2", key_prefix="SRC")
        norm["internal_source_key"] = key
        normalized.append(norm)
        counter += 1

    return normalized


def deduplicate_sources(sources: list[dict]) -> list[dict]:
    """Deduplicate by internal_source_key."""
    seen = set()
    result = []
    for s in sources:
        key = s.get("internal_source_key", "")
        title_key = (s.get("title") or "")[:60]
        dedup_key = f"{key}|{title_key}"
        if dedup_key not in seen:
            seen.add(dedup_key)
            result.append(s)
    return result


def freeze_materials(
    normalized_sources: list[dict],
    structured: dict,
    company: str,
    ticker: str,
    market: str,
    output_path: str,
) -> dict:
    """Freeze materials JSON and return SHA-256 + metadata."""
    materials = {
        "__meta__": {
            "company": company,
            "ticker": ticker,
            "market": market,
            "materials_finalized_at": datetime.now().isoformat(),
            "pipeline": "v1.2.2 source_enrichment",
        },
        "structured": structured,
        "normalized_sources": normalized_sources,
        "source_count": len(normalized_sources),
    }

    json_str = json.dumps(materials, ensure_ascii=False, indent=2, sort_keys=True)
    sha256 = hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    materials["__meta__"]["materials_sha256"] = sha256

    # Write with proper formatting
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(materials, f, ensure_ascii=False, indent=2)

    return {
        "path": str(out),
        "sha256": sha256,
        "source_count": len(normalized_sources),
        "frozen_at": materials["__meta__"]["materials_finalized_at"],
    }


def enrich_and_freeze(
    company: str,
    ticker: str,
    market: str,
    output_dir: str,
    token: str | None = None,
) -> dict:
    """完整的来源采集→归一化→冻结流程。返回冻结元数据。"""
    client = SourceClient(token or find_token())

    print(f"[enrich] Fetching structured data for {ticker} ({market})...")
    structured = fetch_structured_sources(client, ticker, market)

    print(f"[enrich] Searching Materials V2 for {company}/{ticker}...")
    raw_sources = fetch_materials_v2_sources(client, company, ticker, market)

    print(f"[enrich] Fetching full text for {min(8, len(raw_sources))} research reports...")
    raw_sources = fetch_report_fulltext(client, raw_sources)

    print(f"[enrich] Normalizing {len(raw_sources)} raw sources...")
    normalized = normalize_all_sources(raw_sources, structured, company, ticker, market)

    print(f"[enrich] Deduplicating...")
    normalized = deduplicate_sources(normalized)

    output_path = str(Path(output_dir) / "input" / f"{ticker}_materials.json")
    print(f"[enrich] Freezing materials to {output_path}...")
    freeze_meta = freeze_materials(normalized, structured, company, ticker, market, output_path)

    print(f"[enrich] Done. {freeze_meta['source_count']} sources frozen.")
    print(f"[enrich] SHA-256: {freeze_meta['sha256'][:16]}...")
    return freeze_meta


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python source_enrichment.py <company> <ticker> <market> <output_dir>")
        sys.exit(1)
    meta = enrich_and_freeze(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(json.dumps(meta, ensure_ascii=False, indent=2))
