#!/usr/bin/env python3
"""Source-driven HK/US peer comparison helpers for v1.2.4.

This module owns §9 peer discovery, peer material retrieval, evidence
validation, and deterministic §9 rendering. It deliberately does not maintain
fixed peer lists or company-specific peer maps.
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

try:
    from fetch_materials import DatayesClient, data_of, dates, safe_call, stock_search
except Exception:  # pragma: no cover - allows direct unit import without cwd tweaks
    DatayesClient = None
    data_of = None
    dates = None
    safe_call = None
    stock_search = None


PEER_TABLE_COLUMNS = [
    "竞争关系", "公司", "可比业务", "行业地位", "可比维度",
    "商业模式", "目标客户群体", "核心产品", "最新业务进展", "进展日期",
]
RELATION_PRIORITY = {"直接竞争": 5, "细分业务可比": 4, "商业模式可比": 3, "上下游生态可比": 2, "A/H映射候选": 1}
PROGRESS_TYPES = ("产品发布", "客户进展", "商业化", "收入增长", "订单", "产能", "交付", "用户增长", "价格", "监管", "业务合作", "其他")
BUSINESS_PROGRESS_RE = re.compile(
    r"产品|发布|客户|商业化|收入|增长|订单|产能|交付|用户|价格|监管|合作|推出|上线|签约|订阅|利润|毛利|market|revenue|order|customer|launch|delivery",
    re.I,
)


def _plain_text(value: Any, limit: int = 0) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit and len(text) > limit else text


def _norm_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").lower())
    for suffix in ("股份有限公司", "有限公司", "集团控股有限公司", "控股有限公司", "inc.", "inc", "corp.", "corp", "ltd.", "ltd"):
        text = text.replace(suffix.lower(), "")
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text)


def _norm_ticker(value: Any, market: str = "") -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    raw = re.sub(r"\s+US$", "", raw)
    if market.upper() == "HK" or raw.endswith(".HK"):
        base = raw.replace(".HK", "")
        return f"{base.zfill(5)}.HK" if base.isdigit() else raw
    if market.upper() == "A" and raw.isdigit():
        return raw.zfill(6)
    return raw.replace(".US", "")


def _source_id(item: dict) -> str:
    meta = item.get("metadata") or {}
    return str(meta.get("id") or item.get("id") or "").strip()


def _source_date(item: dict) -> str:
    meta = item.get("metadata") or {}
    return str(meta.get("publishTime") or item.get("publishTime") or "")[:10]


def _source_title(item: dict) -> str:
    return _plain_text(item.get("title") or item.get("articleTitle") or "", 140)


def _source_text(item: dict) -> str:
    return _plain_text(" ".join(str(item.get(k) or "") for k in ("title", "text", "content", "summary", "abstract")), 1200)


def _article_text(rd: dict) -> str:
    return _plain_text(" ".join(str(rd.get(k) or "") for k in ("articleTitle", "title", "textAbstract", "summary")), 1200)


def _extract_json_payload(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def target_research_inputs(materials: dict, company_name: str, ticker: str, max_reports: int = 8) -> list[dict]:
    rows = []
    target_norm = _norm_name(company_name)
    ticker_norm = _norm_ticker(ticker).replace(".HK", "")
    for rd in materials.get("research", {}).get("details", []) or []:
        aid = str(rd.get("articleId") or "").strip()
        if not aid:
            continue
        text = _article_text(rd)
        company_hit = target_norm and target_norm in _norm_name(text)
        ticker_hit = ticker_norm and re.search(rf"(?<![A-Z0-9]){re.escape(ticker_norm)}(?:\.HK| HK| US)?(?![A-Z0-9])", text.upper())
        if not (company_hit or ticker_hit):
            continue
        rows.append({
            "articleId": aid,
            "title": _plain_text(rd.get("articleTitle") or rd.get("title"), 120),
            "date": str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:10],
            "abstract": _plain_text(rd.get("textAbstract") or rd.get("summary"), 500),
        })
        if len(rows) >= max_reports:
            break
    return rows


def discover_peer_candidates(target_reports: list[dict], company_name: str, ticker: str, market: str,
                             llm_call: Callable[..., tuple[str, bool]] | None) -> tuple[list[dict], str]:
    if not target_reports or llm_call is None:
        return [], "unsupported"
    article_ids = {str(r["articleId"]) for r in target_reports}
    prompt = f"""请仅根据以下目标公司研报，提取研报原文明示的同业公司、竞争公司或可比公司。

目标公司：
公司名：{company_name}
证券代码：{ticker}
市场：{market}

对每个候选输出：
- peer_name
- ticker_hint
- relation_type
- comparable_business
- evidence_sentence
- discovery_article_id
- confidence

relation_type 只能是：
- 直接竞争
- 细分业务可比
- 商业模式可比
- 上下游生态可比
- A/H映射候选

要求：
1. 只提取研报原文明确出现的公司；
2. 不使用模型常识补充同行；
3. 必须说明在哪项具体业务上可比；
4. discovery_article_id 必须来自输入；
5. 目标公司自身不输出；
6. 产品名、品牌名、行业名不能当作上市公司；
7. 只输出 JSON 数组，最多8个候选。

研报：
{json.dumps(target_reports, ensure_ascii=False)}
"""
    text, ok = llm_call(prompt, max_tokens=2500, timeout=90)
    if not ok:
        return [], "discovery_llm_failed"
    payload = _extract_json_payload(text)
    if isinstance(payload, dict):
        payload = payload.get("peers") or payload.get("candidates") or []
    if not isinstance(payload, list):
        return [], "discovery_json_invalid"
    rows = []
    target_norm = _norm_name(company_name)
    for item in payload[:8]:
        if not isinstance(item, dict):
            continue
        peer_name = _plain_text(item.get("peer_name"), 80)
        evidence = _plain_text(item.get("evidence_sentence"), 240)
        business = _plain_text(item.get("comparable_business"), 80)
        aid = str(item.get("discovery_article_id") or "").strip()
        if not peer_name or _norm_name(peer_name) == target_norm or aid not in article_ids:
            continue
        if not business or len(evidence) < 8 or peer_name not in evidence:
            continue
        rows.append({
            "peer_name": peer_name,
            "ticker_hint": _plain_text(item.get("ticker_hint"), 30),
            "relation_type": item.get("relation_type") if item.get("relation_type") in RELATION_PRIORITY else "细分业务可比",
            "comparable_business": business,
            "evidence_sentence": evidence,
            "discovery_article_id": aid,
            "confidence": item.get("confidence", 0),
        })
    return dedupe_and_rank_candidates(rows)[:5], "supported" if rows else "empty"


def dedupe_and_rank_candidates(candidates: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for c in candidates:
        key = _norm_name(c.get("peer_name"))
        if not key:
            continue
        existing = grouped.get(key)
        if not existing:
            item = copy.deepcopy(c)
            item["discovery_article_ids"] = [c["discovery_article_id"]]
            item["evidence_sentences"] = [c["evidence_sentence"]]
            item["comparable_businesses"] = [c["comparable_business"]]
            grouped[key] = item
            continue
        if c["discovery_article_id"] not in existing["discovery_article_ids"]:
            existing["discovery_article_ids"].append(c["discovery_article_id"])
        if c["evidence_sentence"] not in existing["evidence_sentences"]:
            existing["evidence_sentences"].append(c["evidence_sentence"])
        if c["comparable_business"] not in existing["comparable_businesses"]:
            existing["comparable_businesses"].append(c["comparable_business"])
        if RELATION_PRIORITY.get(c["relation_type"], 0) > RELATION_PRIORITY.get(existing["relation_type"], 0):
            existing["relation_type"] = c["relation_type"]
        if not existing.get("ticker_hint") and c.get("ticker_hint"):
            existing["ticker_hint"] = c["ticker_hint"]
        existing["comparable_business"] = "、".join(existing["comparable_businesses"][:2])
    def score(c: dict) -> tuple:
        evidence = " ".join(c.get("evidence_sentences", []))
        return (
            1 if c.get("ticker_hint") else 0,
            1 if re.search(r"竞争|可比|同业|对标", evidence) else 0,
            len(c.get("comparable_business", "")),
            len(c.get("discovery_article_ids", [])),
            RELATION_PRIORITY.get(c.get("relation_type"), 0),
        )
    return sorted(grouped.values(), key=score, reverse=True)


def resolve_peer_entities(candidates: list[dict], target_ticker: str, target_name: str,
                          client: Any = None, errors: list | None = None,
                          resolver: Callable[[dict], list[dict]] | None = None) -> tuple[list[dict], list[dict]]:
    resolved, dropped = [], []
    seen = set()
    target_codes = {_norm_ticker(target_ticker).replace(".HK", ""), _norm_name(target_name)}
    for c in candidates[:5]:
        hits = resolver(c) if resolver else []
        if not hits and client is not None and stock_search is not None:
            query = c.get("ticker_hint") or c.get("peer_name")
            hits = stock_search(client, query, errors if errors is not None else [])
        exact_hits = []
        for h in hits or []:
            ticker = _norm_ticker(h.get("ticker") or h.get("entity_id") or h.get("secCode") or c.get("ticker_hint"), h.get("market") or "")
            name = h.get("name") or h.get("secShortName") or h.get("companyName") or c.get("peer_name")
            market = (h.get("market") or h.get("exchange") or "").upper()
            if not market:
                market = "HK" if ticker.endswith(".HK") else ("A" if ticker.isdigit() and len(ticker) == 6 else "US")
            if not ticker:
                continue
            if _norm_name(name) != _norm_name(c.get("peer_name")) and c.get("ticker_hint") and _norm_ticker(c.get("ticker_hint")).replace(".HK", "") != ticker.replace(".HK", ""):
                continue
            exact_hits.append({"peer_name": name, "ticker": ticker, "market": market, "entity_id": str(h.get("entity_id") or ticker)})
        if len(exact_hits) != 1:
            dropped.append({"peer_name": c.get("peer_name"), "reason": "unresolved" if not exact_hits else "ambiguous"})
            continue
        ent = exact_hits[0]
        entity_key = ent["ticker"].replace(".HK", "")
        if entity_key in target_codes or _norm_name(ent["peer_name"]) in target_codes:
            dropped.append({"peer_name": c.get("peer_name"), "reason": "self_target"})
            continue
        if entity_key in seen:
            dropped.append({"peer_name": c.get("peer_name"), "reason": "duplicate"})
            continue
        seen.add(entity_key)
        merged = {**c, **ent}
        resolved.append(merged)
    return resolved, dropped


def _material_matches_peer(item: dict, peer: dict) -> bool:
    text = f"{_source_title(item)} {_source_text(item)}"
    ticker = _norm_ticker(peer.get("ticker"), peer.get("market")).replace(".HK", "")
    name = str(peer.get("peer_name") or "")
    ticker_hit = bool(ticker and not ticker.isdigit() and re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text.upper()))
    name_hit = bool(name and re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I))
    if not (ticker_hit or name_hit):
        return False
    if peer.get("comparable_business") and _norm_name(peer["comparable_business"]) not in _norm_name(text):
        if not BUSINESS_PROGRESS_RE.search(text):
            return False
    return bool(BUSINESS_PROGRESS_RE.search(text))


def _query_get_materials(client: Any, question: str, days_back: int, size: int, errors: list) -> list[dict]:
    if client is None or safe_call is None or data_of is None or dates is None:
        return []
    start, end = dates(days_back)
    body = {
        "queryScope": "research,meetingSummary,marketView,wechat",
        "question": question,
        "rewriteQuestion": False,
        "size": size,
        "startTime": start,
        "endTime": end,
    }
    data = data_of(safe_call(client, errors, "getMaterialsV2", body))
    return data if isinstance(data, list) else []


def fetch_peer_materials_for_entity(peer: dict, client: Any = None, errors: list | None = None,
                                    material_fetcher: Callable[[dict, str, int, int], list[dict]] | None = None) -> tuple[list[dict], int]:
    errors = errors if errors is not None else []
    queries = [
        (f"{peer['peer_name']} {peer['comparable_business']} 最新业务进展 产品 客户 收入 订单 商业化", 180),
        (f"{peer['peer_name']} {peer['comparable_business']} 财报 公告 研报 业务进展", 365),
    ]
    kept, seen, query_count = [], set(), 0
    for idx, (question, days_back) in enumerate(queries):
        if idx == 1 and len(kept) >= 2:
            break
        query_count += 1
        items = material_fetcher(peer, question, days_back, 8) if material_fetcher else _query_get_materials(client, question, days_back, 8, errors)
        for it in items or []:
            if not isinstance(it, dict):
                continue
            sid = _source_id(it)
            if not sid or sid in seen or not _source_date(it):
                continue
            if not _material_matches_peer(it, peer):
                continue
            seen.add(sid)
            kept.append(it)
    kept = sorted(kept, key=_source_date, reverse=True)[:5]
    return kept, query_count


def extract_latest_progress(peer: dict, materials: list[dict], llm_call: Callable[..., tuple[str, bool]] | None = None) -> dict:
    if not materials:
        return {"status": "unsupported"}
    source_ids = {_source_id(m) for m in materials}
    if llm_call is not None:
        prompt = f"""请仅根据以下 peer-specific 材料，提取该公司在指定可比业务上的最新业务进展。
Peer：公司：{peer['peer_name']} 代码：{peer['ticker']} 市场：{peer['market']} 可比业务：{peer['comparable_business']}
输出 JSON：latest_progress_summary, progress_date, progress_type, quantitative_metrics, source_ids, supporting_sentence, status。
progress_type 只能是：{','.join(PROGRESS_TYPES)}
要求：只使用输入材料；source_ids 必须来自输入；无有效进展返回 status=unsupported。
材料：{json.dumps([{'id': _source_id(m), 'date': _source_date(m), 'title': _source_title(m), 'text': _source_text(m)} for m in materials], ensure_ascii=False)}
"""
        text, ok = llm_call(prompt, max_tokens=1800, timeout=90)
        payload = _extract_json_payload(text) if ok else None
        if isinstance(payload, dict) and payload.get("status") != "unsupported":
            ids = [str(x) for x in payload.get("source_ids", []) if str(x) in source_ids]
            sentence = _plain_text(payload.get("supporting_sentence"), 240)
            summary = _plain_text(payload.get("latest_progress_summary"), 120)
            if ids and sentence and summary and not re.fullmatch(r".{0,12}(持续推进|保持领先|稳步推进).{0,12}", summary):
                date = str(payload.get("progress_date") or "")[:10]
                src_date = max(_source_date(m) for m in materials if _source_id(m) in ids)
                if date and date <= src_date:
                    return {
                        "status": "supported",
                        "summary": summary,
                        "date": date,
                        "type": payload.get("progress_type") if payload.get("progress_type") in PROGRESS_TYPES else "其他",
                        "quantitative_metrics": payload.get("quantitative_metrics") or [],
                        "source_ids": ids,
                        "supporting_sentence": sentence,
                    }
    for item in sorted(materials, key=_source_date, reverse=True):
        text = _source_text(item)
        sentences = [s.strip() for s in re.split(r"[。；;.!?\n]", text) if s.strip()]
        for sent in sentences:
            if BUSINESS_PROGRESS_RE.search(sent):
                return {
                    "status": "supported",
                    "summary": _plain_text(sent, 90),
                    "date": _source_date(item),
                    "type": "其他",
                    "quantitative_metrics": re.findall(r"\d+(?:\.\d+)?%?", sent)[:5],
                    "source_ids": [_source_id(item)],
                    "supporting_sentence": _plain_text(sent, 160),
                }
    return {"status": "unsupported"}


def _field(value: str, source_ids: list[str]) -> dict:
    return {"value": _plain_text(value, 80), "source_ids": [str(x) for x in source_ids if x]}


def _peer_fact_fields(peer: dict, progress: dict) -> dict:
    sid = progress.get("source_ids", [])
    sentence = progress.get("supporting_sentence") or progress.get("summary") or ""
    business = peer.get("comparable_business", "")
    peer_fact = sentence or business
    return {
        "industry_position": _field(peer_fact, sid),
        "comparison_dimensions": _field(business, peer.get("discovery_article_ids") or []),
        "business_model": _field(sentence if re.search(r"模式|平台|订阅|广告|交易|服务|software|platform|subscription", sentence, re.I) else peer_fact, sid),
        "target_customers": _field(sentence if re.search(r"客户|用户|商户|企业|开发者|customer|user|merchant|enterprise", sentence, re.I) else peer_fact, sid),
        "core_products": _field(sentence if re.search(r"产品|服务|平台|应用|云|模型|product|service|cloud|app", sentence, re.I) else peer_fact, sid),
    }


def build_target_comparison_row(materials: dict, company_name: str, ticker: str, market: str, peer_businesses: list[str]) -> dict:
    reports = target_research_inputs(materials, company_name, ticker, max_reports=3)
    source_id = reports[0]["articleId"] if reports else ""
    text = " ".join([r.get("abstract", "") + " " + r.get("title", "") for r in reports])
    business = "、".join([b for b in peer_businesses if b][:2]) or _plain_text(text, 40)
    latest = next((s for s in re.split(r"[。；;.!?\n]", text) if BUSINESS_PROGRESS_RE.search(s)), "")
    date = reports[0].get("date", "") if reports else ""
    return {
        "comparable_business": _field(business, [source_id]),
        "industry_position": _field("", []),
        "comparison_dimensions": _field(business, [source_id]),
        "business_model": _field(_plain_text(latest, 80), [source_id] if latest else []),
        "target_customers": _field(_plain_text(latest, 80) if re.search(r"客户|用户|商户|企业|开发者", latest) else "", [source_id] if latest else []),
        "core_products": _field(_plain_text(latest, 80) if re.search(r"产品|服务|平台|应用|云|模型", latest) else business, [source_id] if source_id else []),
        "latest_progress": {"summary": _plain_text(latest, 90), "date": date, "source_ids": [source_id] if source_id and latest else []},
    }


def build_peer_comparison_bundle(materials: dict, company_name: str, ticker: str, market: str,
                                 token: str = "", llm_call: Callable[..., tuple[str, bool]] | None = None,
                                 resolver: Callable[[dict], list[dict]] | None = None,
                                 material_fetcher: Callable[[dict, str, int, int], list[dict]] | None = None,
                                 output_dir: str | None = None) -> dict:
    target_reports = target_research_inputs(materials, company_name, ticker, 8)
    candidates, discovery_status = discover_peer_candidates(target_reports, company_name, ticker, market, llm_call)
    errors: list[dict[str, str]] = []
    client = DatayesClient(token) if token and DatayesClient is not None else None
    entities, dropped = resolve_peer_entities(candidates, ticker, company_name, client=client, errors=errors, resolver=resolver)
    peers, peer_materials_dump, query_count = [], [], 0
    for peer in entities[:5]:
        mats, qc = fetch_peer_materials_for_entity(peer, client=client, errors=errors, material_fetcher=material_fetcher)
        query_count += qc
        peer_materials_dump.append({"peer": peer, "materials": mats})
        progress = extract_latest_progress(peer, mats, llm_call)
        if progress.get("status") != "supported":
            dropped.append({"peer_name": peer.get("peer_name"), "reason": "no_progress"})
            continue
        fact_fields = _peer_fact_fields(peer, progress)
        peers.append({
            **peer,
            "discovery": {
                "article_ids": peer.get("discovery_article_ids") or [peer.get("discovery_article_id")],
                "evidence_sentences": peer.get("evidence_sentences") or [peer.get("evidence_sentence")],
            },
            "latest_progress": progress,
            "peer_materials": mats,
            **fact_fields,
        })
    peers = sorted(peers, key=lambda p: p.get("latest_progress", {}).get("date", ""), reverse=True)[:3]
    target_row = build_target_comparison_row(materials, company_name, ticker, market, [p.get("comparable_business", "") for p in peers])
    bundle = {
        "target": {"company_name": company_name, "ticker": ticker, "market": market, "comparison_row": target_row},
        "status": "supported" if len(peers) >= 2 else "unsupported",
        "peer_candidates_total": len(candidates),
        "peer_entities_resolved": len(entities),
        "valid_peer_rows": len(peers),
        "query_count": query_count,
        "peers": peers,
        "dropped_candidates": dropped,
        "errors": errors,
        "ah_mapping": {"status": "none", "rows": []},
    }
    if output_dir:
        raw = Path(output_dir) / "raw_retrieval_payloads"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "peer_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        (raw / "peer_materials.json").write_text(json.dumps(peer_materials_dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (Path(output_dir) / "peer_evidence.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (Path(output_dir) / "ah_mapping.json").write_text(json.dumps(bundle["ah_mapping"], ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def merge_peer_sources_into_materials(materials: dict, peer_bundle: dict) -> dict:
    out = copy.deepcopy(materials)
    mv2 = out.setdefault("materials_v2", {})
    sources = mv2.setdefault("unique_sources", [])
    seen = {str(s.get("id") or "") for s in sources}
    for peer in peer_bundle.get("peers", []):
        progress_ids = set(peer.get("latest_progress", {}).get("source_ids") or [])
        for item in peer.get("peer_materials", []) or []:
            sid = _source_id(item)
            if not sid or sid in seen or sid not in progress_ids:
                continue
            meta = item.get("metadata") or {}
            sources.append({
                "id": sid,
                "type": item.get("type") or item.get("dataType") or "Materials V2",
                "title": _source_title(item),
                "organization": meta.get("organization", "") or item.get("organization", ""),
                "publishTime": _source_date(item),
                "url": item.get("url", ""),
                "company_match": "exact_peer",
                "source_role": "peer_business_progress",
                "peer_name": peer.get("peer_name"),
                "peer_ticker": peer.get("ticker"),
                "peer_market": peer.get("market"),
                "comparable_business": peer.get("comparable_business"),
                "discovery_article_ids": peer.get("discovery", {}).get("article_ids", []),
                "api_nameEn": "getMaterialsV2",
            })
            seen.add(sid)
    return out


def _ref_for_source(source_ids: list[str], ref_map: dict) -> int:
    wanted = {str(x) for x in source_ids if x}
    for rn, meta in ref_map.items():
        if str(meta.get("id", "")) in wanted:
            return rn
    return -1


def _fmt_cell(value: str, ref: int = -1) -> str:
    value = _plain_text(value, 90)
    return f"{value}[{ref}]" if value and ref > 0 else value


def build_peer_comparison_section(peer_bundle: dict, ref_map: dict) -> str:
    peers = peer_bundle.get("peers", [])[:3]
    if len(peers) < 2:
        return ""
    target = peer_bundle.get("target", {})
    target_row = target.get("comparison_row", {})
    lines = [
        "## 9 行业对比与 A/H 映射",
        "",
        "### 9.1 同业业务对比",
        "",
        "| " + " | ".join(PEER_TABLE_COLUMNS) + " |",
        "|" + "|".join(":---" for _ in PEER_TABLE_COLUMNS) + "|",
    ]
    def target_field(name: str) -> str:
        field = target_row.get(name, {}) or {}
        return _fmt_cell(field.get("value", ""), _ref_for_source(field.get("source_ids", []), ref_map))
    latest = target_row.get("latest_progress", {}) or {}
    latest_ref = _ref_for_source(latest.get("source_ids", []), ref_map)
    lines.append("| " + " | ".join([
        "基准公司",
        f"{target.get('company_name')}（{target.get('ticker')}）",
        target_field("comparable_business"),
        target_field("industry_position"),
        target_field("comparison_dimensions"),
        target_field("business_model"),
        target_field("target_customers"),
        target_field("core_products"),
        _fmt_cell(latest.get("summary", ""), latest_ref),
        latest.get("date", ""),
    ]) + " |")
    for peer in peers:
        discovery_ref = _ref_for_source(peer.get("discovery", {}).get("article_ids", []), ref_map)
        progress = peer.get("latest_progress", {})
        progress_ref = _ref_for_source(progress.get("source_ids", []), ref_map)
        lines.append("| " + " | ".join([
            _fmt_cell(peer.get("relation_type", ""), discovery_ref),
            f"{peer.get('peer_name')}（{peer.get('ticker')}）",
            _fmt_cell(peer.get("comparable_business", ""), discovery_ref),
            _fmt_cell(peer.get("industry_position", {}).get("value", ""), _ref_for_source(peer.get("industry_position", {}).get("source_ids", []), ref_map)),
            _fmt_cell(peer.get("comparison_dimensions", {}).get("value", ""), discovery_ref),
            _fmt_cell(peer.get("business_model", {}).get("value", ""), _ref_for_source(peer.get("business_model", {}).get("source_ids", []), ref_map)),
            _fmt_cell(peer.get("target_customers", {}).get("value", ""), _ref_for_source(peer.get("target_customers", {}).get("source_ids", []), ref_map)),
            _fmt_cell(peer.get("core_products", {}).get("value", ""), _ref_for_source(peer.get("core_products", {}).get("source_ids", []), ref_map)),
            _fmt_cell(progress.get("summary", ""), progress_ref),
            progress.get("date", ""),
        ]) + " |")
    ah_rows = peer_bundle.get("ah_mapping", {}).get("rows", [])
    if ah_rows:
        lines.extend(["", "### 9.2 A/H 映射", "", "| A股主体（代码） | H股主体（代码） | 映射关系 | 依据 |", "|:---|:---|:---|:---|"])
        for row in ah_rows:
            rn = _ref_for_source(row.get("source_ids", []), ref_map)
            lines.append(f"| {row.get('a_company_name')}（{row.get('a_ticker')}） | {row.get('h_company_name')}（{row.get('h_ticker')}） | {row.get('relation')} | {_fmt_cell('来源验证', rn)} |")
    return "\n".join(lines)
