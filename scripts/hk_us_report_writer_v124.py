#!/usr/bin/env python3
"""hk_us_report_writer_v124.py - HK/US stock one-pager full generation pipeline.

Usage:
    python hk_us_report_writer_v124.py --ticker 00700.HK --market hk --company-name Tencent --output-dir live/...

Pipeline: collect -> source_trace -> id_audit -> section-wise LLM -> post-repair -> checker -> DOCX.
"""

from __future__ import annotations
import argparse, json, os, sys, subprocess, re, time, shutil, html
from datetime import datetime
from pathlib import Path
from typing import Any

from llm_adapter_v124 import (
    LLMConfig,
    call_llm as adapter_call_llm,
    config_for_diagnostics,
    resolve_llm_config,
)

_PEER_IMPORT_ERROR = ""
try:
    from peer_comparison_v124 import (
        build_peer_comparison_bundle,
        build_peer_comparison_section,
        merge_peer_sources_into_materials,
    )
except Exception as exc:
    _PEER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    build_peer_comparison_bundle = None
    build_peer_comparison_section = None
    merge_peer_sources_into_materials = None

_SCRIPT_DIR = Path(__file__).resolve().parent
_FETCH_MATERIALS = _SCRIPT_DIR / "fetch_materials.py"
_POST_REPAIR    = _SCRIPT_DIR / "hk_us_post_repair_v124.py"
_CHECKER        = _SCRIPT_DIR / "check_report_quality_v124.py"
_BUILD_DOCX     = _SCRIPT_DIR / "build_docx.py"

TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_ISO = datetime.now().isoformat(timespec="seconds")
_LLM_CONFIG = LLMConfig()
_LLM_DIAGNOSTICS: dict[str, Any] = {"config": {}, "calls": []}

SECTION_MATERIAL_MAP = {
    "s12":   {"label": "ch1-2", "h2": ["1 关键要点", "2 近况跟踪"],
              "tags": ["recent", "earnings", "guidance", "rating", "target", "market", "quarterly"],
              "max_sources": 8, "max_tokens": 8000},
    "s34":   {"label": "ch3-4", "h2": ["3 核心投资逻辑", "4 催化事件时间表"],
              "tags": ["catalyst", "investment", "product", "regulation", "buyback"],
              "max_sources": 8, "max_tokens": 8000},
    "s57":   {"label": "ch5-7", "h2": ["5 业务拆分", "6 产销链与生态", "7 财务与盈利质量"],
              "tags": ["business", "financial", "revenue", "profit", "margin", "customer", "supplier", "cashflow", "capex", "ROE"],
              "max_sources": 10, "max_tokens": 10000},
    "s89":   {"label": "ch8-9", "h2": ["8 市场关注", "9 行业对比"],
              "tags": ["market", "peer", "industry", "competition", "comparable", "research", "revenue", "margin", "growth", "outlook", "rating", "target"],
              "max_sources": 12, "max_tokens": 8000},
    "s1011": {"label": "ch10-11", "h2": ["10 市场分歧", "11 估值与预测"]},
    "s12r":  {"label": "ch12", "h2": ["12 风险提示"],
              "tags": ["risk", "warning", "uncertainty"],
              "max_sources": 6, "max_tokens": 4000},
}
SECTION_ORDER = ["s12", "s34", "s57", "s89", "s1011", "s12r"]

_HK_US_REPORT_SYSTEM_CONSTRAINTS = (
    "You are a senior equity research analyst. Write each section in Chinese Markdown. "
    "Follow the exact section structure. All numbers must cite [N] references. "
    "Never use placeholder text like N/A, pending, or blank tables. "
    "Do not invent peer rows or fallback valuation when source-backed peer materials or target prices are unavailable. "
    "Never mention pipeline, checker, quality gate, P0, P1, P2."
)

HK_PIT_API_SOURCES = {
    "getHkFdmtIsPit": {
        "api_id": "923",
        "title": "港股 PIT 利润表",
        "url": "https://r.datayes.com/mdpt/dashboard/apis/923",
    },
    "getHkFdmtBsPit": {
        "api_id": "924",
        "title": "港股 PIT 资产负债表",
        "url": "https://r.datayes.com/mdpt/dashboard/apis/924",
    },
    "getHkFdmtCfPit": {
        "api_id": "925",
        "title": "港股 PIT 现金流量表",
        "url": "https://r.datayes.com/mdpt/dashboard/apis/925",
    },
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return "".join(c for c in re.sub(r"<[^>]+>", "", s) if c.isalnum()).lower()

def _find_token() -> str:
    for p in ["DATAYES_TOKEN"]:
        v = os.environ.get(p, "")
        if v: return v
    for p in [Path.home() / "token.txt", Path.home() / ".datayes_token"]:
        if p.exists(): return p.read_text().strip()
    return ""

def _find_llm_creds() -> tuple:
    cfg = _LLM_CONFIG if _LLM_CONFIG else resolve_llm_config()
    return cfg.api_key, cfg.base_url, cfg.model

def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

def _write_llm_diagnostics(output_dir: str):
    _save_json(str(Path(output_dir) / "llm_diagnostics.json"), _LLM_DIAGNOSTICS)

def _diagnostics_payload(config: LLMConfig) -> dict[str, Any]:
    data = config_for_diagnostics(config)
    return {
        "config": data,
        "config_resolution": data.get("config_resolution", {}),
        "calls": [],
    }

def _startup_check() -> None:
    missing = [
        str(p) for p in (_FETCH_MATERIALS, _POST_REPAIR, _CHECKER, _BUILD_DOCX)
        if not p.exists()
    ]
    if missing:
        raise RuntimeError(f"required internal script missing: {missing}")
    if _PEER_IMPORT_ERROR or not (build_peer_comparison_bundle and merge_peer_sources_into_materials):
        raise RuntimeError(f"peer_comparison_v124 import failed: {_PEER_IMPORT_ERROR or 'unknown error'}")

def _manifest_path(output_dir: str) -> Path:
    return Path(output_dir) / "run_manifest.json"

def _update_run_manifest(output_dir: str, *, stage: str, status: str = "running",
                         started_at: str | None = None, duration_s: float | None = None):
    path = _manifest_path(output_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {"started_at": started_at or datetime.now().isoformat(timespec="seconds"),
                "completed_stages": [], "stage_durations_s": {}}
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    data["current_stage"] = stage
    data["status"] = status
    if duration_s is not None:
        data.setdefault("stage_durations_s", {})[stage] = round(duration_s, 3)
        if stage not in data.setdefault("completed_stages", []):
            data["completed_stages"].append(stage)
    _save_json(str(path), data)

def _finalize_failed_run(output_dir: str, *, stage: str, error_type: str, message: str):
    path = _manifest_path(output_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {"started_at": datetime.now().isoformat(timespec="seconds"), "completed_stages": [], "stage_durations_s": {}}
    else:
        data = {"started_at": datetime.now().isoformat(timespec="seconds"), "completed_stages": [], "stage_durations_s": {}}
    now = datetime.now().isoformat(timespec="seconds")
    data.update({
        "updated_at": now,
        "finished_at": now,
        "current_stage": stage,
        "status": "failed",
        "failure": {
            "error_type": error_type,
            "message": message,
        },
    })
    _save_json(str(path), data)

def _print_llm_config_error(config: LLMConfig):
    if config.error_type == "LLM_CONFIG_MISSING":
        missing = config.missing_fields or []
        print("\nLLM_CONFIG_MISSING\n")
        print("已检查:")
        for source in (config.searched_sources or ["cli", "environment", ".env", "platform_models_json"]):
            print(f"- {source}")
        print("\n缺失:")
        for field in missing:
            print(f"- {field}")
        print("\n当前 Agent 未向 Python 子进程暴露可用 LLM 配置。")
        print("请由运行平台注入 LLM_* 环境变量或提供受支持的 models.json。")
    elif config.error_type == "CONFIG_AMBIGUOUS":
        print("\nCONFIG_AMBIGUOUS\n")
        print("无法在多个候选 LLM 配置中安全选择。候选摘要:")
        for cand in (config.candidate_summaries or [])[:8]:
            print(
                f"- provider_id={cand.get('provider_id','')} model_id={cand.get('model_id','')} "
                f"hostname={cand.get('hostname','')} api_format={cand.get('api_format','')} source={cand.get('source','')}"
            )
    else:
        print(f"\n{config.error_type or 'LLM_CONFIG_ERROR'}\n{config.error_message}")

def _clean_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()

def _normalize_table_separators(text: str) -> str:
    def repl(m):
        cells = [c for c in m.group(0).strip().strip('|').split('|')]
        return "|" + "|".join(":---" for _ in cells) + "|"
    return re.sub(r'^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$', repl, text, flags=re.M)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, max_tokens: int = 8000, timeout: int = 180, system: str = "", call_name: str = "llm_call") -> tuple:
    """Call the unified adapter and record structured diagnostics."""
    result = adapter_call_llm(prompt, system=system, max_tokens=max_tokens, timeout=timeout, config=_LLM_CONFIG)
    _LLM_DIAGNOSTICS.setdefault("calls", []).append({
        "call_name": call_name,
        "ok": result.ok,
        "latency_s": result.latency_s,
        "attempt_count": result.attempt_count,
        "response_length": len(result.text or ""),
        "error_type": result.error_type,
        "error_message": (result.error_message or "")[:500],
        "http_status": result.http_status,
        "model": result.model,
        "api_format": result.api_format,
        "endpoint": result.endpoint,
        "preview": _plain_text(result.text, 200) if result.text else "",
    })
    return (result.text, result.ok)


# ---------------------------------------------------------------------------
# material compression & section assignment
# ---------------------------------------------------------------------------

def compress_materials(materials: dict, source_trace: dict) -> list:
    """Compress materials to key-facts summaries."""
    compressed = []
    seen = set()
    for i, s in enumerate(source_trace.get("sources", [])):
        sid = s.get("id", "")
        if sid in seen or not sid: continue
        seen.add(sid)
        entry = {
            "ref_no": i + 1,
            "title": (s.get("title", "") or "")[:120],
            "publisher": s.get("organization", "--"),
            "date": s.get("publishTime", ""),
            "id": sid,
            "type": s.get("type", ""),
            "facts": [],
        }
        for rd in materials.get("research", {}).get("details", []):
            if str(rd.get("articleId", "")) == str(sid):
                ab = re.sub(r"<[^>]+>", "", rd.get("textAbstract", ""))
                ab = re.sub(r"\s+", " ", ab).strip()
                for sent in re.split(r"[.;;]", ab):
                    sent = sent.strip()
                    if len(sent) > 15 and re.search(r"\d+", sent):
                        entry["facts"].append(sent[:150])
                    if len(entry["facts"]) >= 5: break
                break
        if not entry["facts"]:
            for mv in materials.get("materials_v2", {}).get("unique_sources", []):
                if str(mv.get("id", "")) == str(sid):
                    entry["facts"].append((mv.get("title", "") or "")[:200])
                    break
        compressed.append(entry)
    return compressed

def _assign_sources(compressed: list) -> dict:
    """Assign compressed sources to sections using keyword matches only."""
    assigned = {k: [] for k in SECTION_ORDER}

    # Phase 1: keyword-based matching
    matched = set()
    for e in compressed:
        tl = (_norm(e.get("title", "")) + " " + _norm(" ".join(e.get("facts", []))))
        best, best_score = None, 0
        for sk, si in SECTION_MATERIAL_MAP.items():
            if not si.get("tags"):
                continue
            sc = sum(3 for t in si["tags"] if _norm(t) in tl)
            if sc > best_score: best_score = sc; best = sk
        if best and best_score > 0:
            assigned[best].append(e)
            matched.add(id(e))

    # Cap per section
    for k in assigned:
        lim = SECTION_MATERIAL_MAP[k].get("max_sources", len(assigned[k]))
        if len(assigned[k]) > lim: assigned[k] = assigned[k][:lim]
    assigned["_unmatched_source_count"] = len([e for e in compressed if id(e) not in matched])
    return assigned

def _fmt_materials(entries: list) -> str:
    if not entries: return ""
    lines = []
    for e in entries:
        f = " | ".join(e.get("facts", [])[:3]) or "(see original)"
        lines.append(f"[{e['ref_no']}] {e['type']} | {e['date']} | {e['publisher']} | {e['title'][:80]} | facts: {f[:200]}")
    return "\n".join(lines)

def _normalize_security_code(code: Any, market: str = "") -> str:
    raw = str(code or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    market_u = (market or "").upper()
    if market_u == "HK" or upper.endswith(".HK"):
        base = re.sub(r'\.HK$', '', upper)
        if base.isdigit():
            return f"{base.zfill(5)}.HK"
        return upper
    if market_u == "US":
        return re.sub(r'\s+US$', '', upper).strip()
    if raw.isdigit() and len(raw) <= 6:
        return raw.zfill(6)
    return raw

def _reference_code(source: dict) -> str:
    market = source.get("market") or source.get("exchange") or ""
    code = source.get("secCode") or source.get("ticker") or source.get("stockCode")
    return _normalize_security_code(code, market) or "--"

def _format_reference_line(ref_no: int, source: dict) -> str:
    """Format one public reference line without exposing structured-source internals."""
    st = source.get("type", "")
    sd = source.get("publishTime", "")
    if st == "Datayes结构化接口":
        code = _reference_code(source)
        title = (source.get("title", "") or "")[:100]
        api_name = source.get("api_nameEn", "")
        return f"[{ref_no}]Datayes结构化接口 | {sd} | {code} | {title} | API：{api_name}"

    sid = source.get("id", "")
    so = source.get("organization", "--")
    sti = (source.get("title", "") or "")[:100]
    sa = source.get("api_nameEn", "")
    api_part = f" | API: {sa}" if sa else ""
    return f"[{ref_no}]{st} | {sd} | ID: {sid} | {so} | {sti}{api_part}"

def _build_ref_text(source_trace: dict) -> tuple:
    """Build deterministic reference section from source_trace. Returns (text, ref_map)."""
    lines = ["\n## 13 参考资料\n"]
    ref_map = {}
    for i, s in enumerate(source_trace.get("sources", [])):
        rn = i + 1
        line = _format_reference_line(rn, s)
        lines.append(line)
        ref_map[rn] = {
            "id": s.get("id", ""),
            "date": s.get("publishTime", ""),
            "title": (s.get("title", "") or "")[:100],
            "line": line,
            "type": s.get("type", ""),
            "organization": s.get("organization", "--"),
            "api_nameEn": s.get("api_nameEn", ""),
            "api_id": s.get("api_id", "") or s.get("apiId", ""),
        }
    return ("\n".join(lines), ref_map)

def _ref_guide(ref_map: dict) -> str:
    lines = []
    for rn in sorted(ref_map.keys()):
        r = ref_map[rn]
        lines.append(f"  [{rn}] {r.get('title','')[:80]} ({r.get('date','')})")
    return "\n".join(lines)

def _plain_text(s: Any, limit: int = 0) -> str:
    text = html.unescape(str(s or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit and len(text) > limit else text

def _target_research_details(materials: dict, co: str, ticker: str, ref_map: dict | None = None,
                             limit: int = 12) -> tuple[list[dict], dict]:
    details = []
    seen = set()
    stats = {"target_reports_total": 0, "target_reports_referenceable": 0, "target_reports_dropped_no_ref": 0}
    for rd in materials.get("research", {}).get("details", []) or []:
        if not _source_matches_target(rd, co, ticker):
            continue
        stats["target_reports_total"] += 1
        aid = str(rd.get("articleId", "") or "").strip()
        rn = _find_ref_no(ref_map or {}, aid) if ref_map is not None else (1 if aid else -1)
        if not aid or rn <= 0:
            stats["target_reports_dropped_no_ref"] += 1
            continue
        if aid in seen:
            continue
        seen.add(aid)
        details.append(rd)
        stats["target_reports_referenceable"] += 1
        if len(details) >= limit:
            break
    return details, stats

def _research_line(rd: dict, ref_map: dict, max_text: int = 360) -> str:
    aid = str(rd.get("articleId", ""))
    rn = _find_ref_no(ref_map, aid)
    if rn <= 0:
        return ""
    cite = f"[{rn}]"
    title = _plain_text(rd.get("articleTitle") or rd.get("title"), 90)
    org = rd.get("orgName", "") or rd.get("organization", "") or "--"
    date = str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:10]
    rating = _plain_text(rd.get("rating"), 30)
    target = _plain_text(rd.get("targetPrice"), 30)
    abstract = _plain_text(rd.get("textAbstract") or rd.get("summary") or "", max_text)
    extras = []
    if rating:
        extras.append(f"评级:{rating}")
    if target:
        extras.append(f"目标价:{target}")
    extra = " | " + " | ".join(extras) if extras else ""
    return f"{cite} {date} {org}《{title}》{extra}：{abstract}".strip()

def _build_hkus_key_data(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str) -> dict:
    target_reports, target_stats = _target_research_details(materials, co, ticker, ref_map, limit=16)
    target_prices = []
    for rd in target_reports:
        tp = rd.get("targetPrice")
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if tp and rn > 0:
            target_prices.append({
                "org": rd.get("orgName", "") or "--",
                "targetPrice": tp,
                "rating": rd.get("rating", "") or "",
                "ref": rn,
                "title": _plain_text(rd.get("articleTitle", ""), 80),
            })
    recent_reports_text = "\n".join(line for line in (_research_line(rd, ref_map) for rd in target_reports[:8]) if line)
    return {
        "company_name": co,
        "ticker": ticker,
        "market": mkt,
        "target_matched_reports": target_reports,
        "target_prices": target_prices[:10],
        "recent_reports_text": recent_reports_text,
        **target_stats,
    }

def _has_target_materials(key_data: dict) -> bool:
    reports = key_data.get("target_matched_reports", []) or []
    if len(reports) >= 2:
        return True
    return len(key_data.get("recent_reports_text", "")) >= 600

def _format_hkus_key_context(key_data: dict) -> str:
    lines = [
        f"Company: {key_data.get('company_name')} ({key_data.get('ticker')}) | Market: {key_data.get('market')}",
        "Target-company research excerpts (only these may support target price, financials, conclusions and catalysts):",
        key_data.get("recent_reports_text") or "(no target-company research excerpt)",
    ]
    if key_data.get("target_prices"):
        lines.append("Target prices / ratings:")
        for tp in key_data["target_prices"][:6]:
            lines.append(f"[{tp['ref']}] {tp['org']} | {tp['targetPrice']} | {tp.get('rating','')} | {tp.get('title','')}")
    return "\n".join(lines)

def _build_section_context(sec_key: str, key_data: dict, assigned: dict, ref_map: dict) -> str:
    """Build non-duplicated source context for one section."""
    lines = [_format_hkus_key_context(key_data)]
    target_ids = {str(rd.get("articleId", "") or "") for rd in key_data.get("target_matched_reports", [])}
    assigned_lines = []
    for e in assigned.get(sec_key, []) or []:
        ref_no = int(e.get("ref_no") or 0)
        source_id = str(e.get("id", "") or "")
        if ref_no <= 0 or ref_no not in ref_map or not source_id or source_id in target_ids:
            continue
        facts = " | ".join(e.get("facts", [])[:3]).strip()
        if not facts:
            facts = e.get("title", "")[:160]
        assigned_lines.append(
            f"[{ref_no}] {e.get('type','')} | {e.get('date','')} | {e.get('publisher','--')} | "
            f"{e.get('title','')[:80]} | facts: {facts[:200]}"
        )
    if assigned_lines:
        lines.append("Section-specific supporting materials:")
        lines.extend(assigned_lines)
    return "\n".join(x for x in lines if x).strip()

_GENERIC_FALLBACK_TERMS = (
    "主业表现" + "待验证",
    "持续" + "关注",
    "估值锚" + "需回溯",
    "长期" + "值得关注",
    "N/A",
    "待补齐",
    "暂无",
    "pipeline",
    "checker",
    "P0",
    "P1",
)
_PLACEHOLDER_RE = re.compile("|".join(re.escape(x) for x in _GENERIC_FALLBACK_TERMS), re.I)

def _parse_json_object(text: str) -> tuple[dict, str]:
    raw = _clean_fence(text or "")
    if raw.startswith("```"):
        raw = _clean_fence(raw)
    try:
        obj = json.loads(raw)
        return (obj, "") if isinstance(obj, dict) else ({}, "top_level_not_object")
    except Exception:
        pass
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return {}, "json_object_not_found"
    try:
        obj = json.loads(m.group(0))
        return (obj, "") if isinstance(obj, dict) else ({}, "top_level_not_object")
    except Exception as exc:
        return {}, f"json_parse_error:{exc}"

def _refs_ok(refs: Any, ref_map: dict, issues: list[str], path: str) -> list[int]:
    if not isinstance(refs, list) or not refs:
        issues.append(f"{path}.source_refs_missing")
        return []
    out = []
    for ref in refs:
        if not isinstance(ref, int):
            issues.append(f"{path}.source_refs_not_int")
            continue
        if ref <= 0 or ref not in ref_map:
            issues.append(f"{path}.source_refs_out_of_range:{ref}")
            continue
        out.append(ref)
    if not out:
        issues.append(f"{path}.source_refs_empty_after_validation")
    return out

def _text_ok(value: Any, issues: list[str], path: str, min_len: int = 2) -> str:
    text = _plain_text(value, 260)
    if len(text) < min_len:
        issues.append(f"{path}.empty")
    if _PLACEHOLDER_RE.search(text):
        issues.append(f"{path}.placeholder")
    return text

def _validate_sections_1_4_payload(payload: dict, ref_map: dict) -> tuple[dict, list[str]]:
    issues: list[str] = []
    clean: dict[str, Any] = {}
    s1 = payload.get("section_1") if isinstance(payload.get("section_1"), dict) else {}
    points = s1.get("key_points") if isinstance(s1.get("key_points"), list) else []
    if not (3 <= len(points) <= 4):
        issues.append(f"section_1.key_points_count:{len(points)}")
    clean_points = []
    for i, item in enumerate(points[:4]):
        if not isinstance(item, dict):
            issues.append(f"section_1.key_points[{i}].not_object")
            continue
        statement = _text_ok(item.get("statement"), issues, f"section_1.key_points[{i}].statement", 12)
        refs = _refs_ok(item.get("source_refs"), ref_map, issues, f"section_1.key_points[{i}]")
        keyword = _plain_text(item.get("keyword") or re.split(r'[，；。,:：]', statement)[0], 18)
        if statement and refs:
            clean_points.append({"keyword": keyword or "投资判断", "statement": statement, "source_refs": refs})
    clean["section_1"] = {"key_points": clean_points}

    s2 = payload.get("section_2") if isinstance(payload.get("section_2"), dict) else {}
    updates = s2.get("recent_updates") if isinstance(s2.get("recent_updates"), list) else []
    if not (4 <= len(updates) <= 6):
        issues.append(f"section_2.recent_updates_count:{len(updates)}")
    clean_updates = []
    s1_statements = {p["statement"] for p in clean_points}
    for i, item in enumerate(updates[:6]):
        if not isinstance(item, dict):
            issues.append(f"section_2.recent_updates[{i}].not_object")
            continue
        keyword = _text_ok(item.get("keyword"), issues, f"section_2.recent_updates[{i}].keyword", 2)
        fact = _text_ok(item.get("fact"), issues, f"section_2.recent_updates[{i}].fact", 8)
        implication = _text_ok(item.get("implication"), issues, f"section_2.recent_updates[{i}].implication", 8)
        refs = _refs_ok(item.get("source_refs"), ref_map, issues, f"section_2.recent_updates[{i}]")
        if fact in s1_statements:
            issues.append(f"section_2.recent_updates[{i}].duplicates_section_1")
        if keyword and fact and implication and refs:
            clean_updates.append({"keyword": keyword, "fact": fact, "implication": implication, "source_refs": refs})
    clean["section_2"] = {"recent_updates": clean_updates}

    s3 = payload.get("section_3") if isinstance(payload.get("section_3"), dict) else {}
    for key in ("near_term_logic", "long_term_logic"):
        rows = s3.get(key) if isinstance(s3.get(key), list) else []
        if not (2 <= len(rows) <= 3):
            issues.append(f"section_3.{key}_count:{len(rows)}")
        clean_rows = []
        for i, item in enumerate(rows[:3]):
            if not isinstance(item, dict):
                issues.append(f"section_3.{key}[{i}].not_object")
                continue
            title = _text_ok(item.get("title"), issues, f"section_3.{key}[{i}].title", 3)
            mechanism = _text_ok(item.get("mechanism"), issues, f"section_3.{key}[{i}].mechanism", 12)
            metrics = item.get("verification_metrics")
            if not isinstance(metrics, list) or not metrics:
                issues.append(f"section_3.{key}[{i}].verification_metrics_missing")
                metrics = []
            metrics = [_plain_text(x, 24) for x in metrics if _plain_text(x, 24)]
            refs = _refs_ok(item.get("source_refs"), ref_map, issues, f"section_3.{key}[{i}]")
            if title and mechanism and metrics and refs:
                clean_rows.append({"title": title, "mechanism": mechanism, "verification_metrics": metrics[:5], "source_refs": refs})
        clean.setdefault("section_3", {})[key] = clean_rows

    s4 = payload.get("section_4") if isinstance(payload.get("section_4"), dict) else {}
    cats = s4.get("catalysts") if isinstance(s4.get("catalysts"), list) else []
    if not (4 <= len(cats) <= 7):
        issues.append(f"section_4.catalysts_count:{len(cats)}")
    clean_cats = []
    for i, item in enumerate(cats[:7]):
        if not isinstance(item, dict):
            issues.append(f"section_4.catalysts[{i}].not_object")
            continue
        date = _text_ok(item.get("date"), issues, f"section_4.catalysts[{i}].date", 4)
        event = _text_ok(item.get("event"), issues, f"section_4.catalysts[{i}].event", 8)
        impact = _text_ok(item.get("impact"), issues, f"section_4.catalysts[{i}].impact", 8)
        refs = _refs_ok(item.get("source_refs"), ref_map, issues, f"section_4.catalysts[{i}]")
        if re.search(r'研报发布|评级|目标价更新', event):
            issues.append(f"section_4.catalysts[{i}].broker_event")
        if date and event and impact and refs:
            clean_cats.append({"date": date, "event": event, "impact": impact, "source_refs": refs})
    clean["section_4"] = {"catalysts": clean_cats}
    return clean, issues

def _cite(refs: list[int]) -> str:
    return "".join(f"[{r}]" for r in refs)

def _render_sections_1_4(payload: dict) -> dict[str, str]:
    lines12 = ["## 1 关键要点", ""]
    for item in payload["section_1"]["key_points"]:
        lines12.append(f"- **{item['keyword']}**：{item['statement']}{_cite(item['source_refs'])}")
    lines12.extend(["", "## 2 近况跟踪", ""])
    for item in payload["section_2"]["recent_updates"]:
        lines12.append(f"- **{item['keyword']}**：{item['fact']}；{item['implication']}{_cite(item['source_refs'])}。")

    lines34 = ["## 3 核心投资逻辑", "", "### 3.1 短期逻辑", ""]
    for item in payload["section_3"]["near_term_logic"]:
        lines34.append(f"- **{item['title']}**：{item['mechanism']}{_cite(item['source_refs'])}。")
        lines34.append(f"  验证变量：{'、'.join(item['verification_metrics'])}。")
    lines34.extend(["", "### 3.2 长期逻辑", ""])
    for item in payload["section_3"]["long_term_logic"]:
        lines34.append(f"- **{item['title']}**：{item['mechanism']}{_cite(item['source_refs'])}。")
        lines34.append(f"  验证变量：{'、'.join(item['verification_metrics'])}。")
    lines34.extend(["", "## 4 催化事件时间表", "", "| 时间 | 事件 | 影响 |", "|:---|:---|:---|"])
    for item in payload["section_4"]["catalysts"]:
        refs = _cite(item["source_refs"])
        lines34.append(f"| {item['date']} | {item['event']}{refs} | {item['impact']}{refs} |")
    return {"s12": "\n".join(lines12), "s34": "\n".join(lines34)}

def _validate_render_section_8(payload: dict, ref_map: dict) -> tuple[str, list[str]]:
    issues: list[str] = []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not (3 <= len(rows) <= 5):
        return "", [f"section_8.rows_count:{0 if not isinstance(rows, list) else len(rows)}"]
    lines = ["## 8 市场关注", "", "| 关注点 | 市场在担心什么 | 需要验证的数据 |", "|:---|:---|:---|"]
    for i, row in enumerate(rows[:5]):
        if not isinstance(row, dict):
            issues.append(f"section_8.rows[{i}].not_object")
            continue
        topic = _text_ok(row.get("topic"), issues, f"section_8.rows[{i}].topic", 3)
        concern = _text_ok(row.get("market_concern"), issues, f"section_8.rows[{i}].market_concern", 8)
        metrics = row.get("verification_metrics")
        if not isinstance(metrics, list) or not metrics:
            issues.append(f"section_8.rows[{i}].verification_metrics_missing")
            metrics = []
        refs = _refs_ok(row.get("source_refs"), ref_map, issues, f"section_8.rows[{i}]")
        if topic and concern and metrics and refs:
            lines.append(f"| {topic}{_cite(refs)} | {concern}{_cite(refs)} | {'、'.join(_plain_text(x, 24) for x in metrics if _plain_text(x, 24))} |")
    return ("\n".join(lines), issues) if len(lines) >= 6 and not issues else ("", issues)

def gen_hkus_section_8(key_data: dict, ref_map: dict) -> tuple[str, bool, list[str]]:
    prompt = f"""Return ONLY JSON for HK/US report section_8 market concerns.
Schema: {{"rows": [{{"topic": "...", "market_concern": "...", "verification_metrics": ["..."], "source_refs": [1]}}]}}
Rules: 3-5 rows; use only target-company context refs; no Markdown.
Context:
{_format_hkus_key_context(key_data)}
"""
    text, ok = _call_llm(prompt, max_tokens=3500, timeout=120, system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name="section_8_json")
    if not ok:
        return "", False, ["section_8:llm_failed"]
    payload, parse_error = _parse_json_object(text)
    if parse_error:
        return "", False, [parse_error]
    rendered, issues = _validate_render_section_8(payload, ref_map)
    return rendered, bool(rendered and not issues), issues

def _validate_render_section_12(payload: dict, ref_map: dict) -> tuple[str, list[str]]:
    issues: list[str] = []
    risks = payload.get("risks") if isinstance(payload, dict) else None
    if not isinstance(risks, list) or not (4 <= len(risks) <= 6):
        return "", [f"section_12.risks_count:{0 if not isinstance(risks, list) else len(risks)}"]
    lines = ["## 12 风险提示", ""]
    for i, risk in enumerate(risks[:6]):
        if not isinstance(risk, dict):
            issues.append(f"section_12.risks[{i}].not_object")
            continue
        title = _text_ok(risk.get("title"), issues, f"section_12.risks[{i}].title", 6)
        refs = _refs_ok(risk.get("source_refs"), ref_map, issues, f"section_12.risks[{i}]")
        if len(re.findall(r'[\u4e00-\u9fff]', title)) > 20:
            issues.append(f"section_12.risks[{i}].title_too_long")
        if title and refs:
            lines.append(f"- **{title}**{_cite(refs)}")
    return ("\n".join(lines), issues) if len(lines) >= 6 and not issues else ("", issues)

def gen_hkus_section_12(key_data: dict, ref_map: dict) -> tuple[str, bool, list[str]]:
    prompt = f"""Return ONLY JSON for HK/US report risk titles.
Schema: {{"risks": [{{"title": "不超过20个中文字的风险小标题", "source_refs": [1]}}]}}
Rules: 4-6 risks; title only, no mechanism/body; use only target-company context refs; no Markdown.
Context:
{_format_hkus_key_context(key_data)}
"""
    text, ok = _call_llm(prompt, max_tokens=2500, timeout=120, system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name="section_12_json")
    if not ok:
        return "", False, ["section_12:llm_failed"]
    payload, parse_error = _parse_json_object(text)
    if parse_error:
        return "", False, [parse_error]
    rendered, issues = _validate_render_section_12(payload, ref_map)
    return rendered, bool(rendered and not issues), issues

def _sections_1_4_json_prompt(key_data: dict, schema_issues: list[str] | None = None,
                              section_scope: str = "sections_1_4") -> str:
    issue_text = "\nPrevious schema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""You are a senior HK/US equity research analyst. Return ONLY one valid JSON object for {section_scope}.
Do not output Markdown headings, Markdown tables, code fences, reference section, explanations, or prose outside JSON.

Use only target-company evidence in the context below. Every source_refs value must be an integer [N] from the context.

Context:
{_format_hkus_key_context(key_data)}
{issue_text}

Required JSON schema:
{{
  "section_1": {{"key_points": [{{"keyword": "短语", "statement": "一句明确投资判断", "source_refs": [1]}}]}},
  "section_2": {{"recent_updates": [{{"keyword": "近期进展关键词", "fact": "近期具体事实", "implication": "对经营变量的直接影响", "source_refs": [1]}}]}},
  "section_3": {{
    "near_term_logic": [{{"title": "短期逻辑标题", "mechanism": "业务机制", "verification_metrics": ["可跟踪变量"], "source_refs": [1]}}],
    "long_term_logic": [{{"title": "长期逻辑标题", "mechanism": "长期竞争力或增长机制", "verification_metrics": ["可跟踪变量"], "source_refs": [1]}}]
  }},
  "section_4": {{"catalysts": [{{"date": "YYYY-MM or YYYY-Qx", "event": "具体事件", "impact": "具体影响", "source_refs": [1]}}]}}
}}

Content rules:
- section_1: 3-4 key_points; each statement includes an operating fact/variable and a judgment on growth, profit, cash flow or valuation.
- section_2: 4-6 recent_updates; do not duplicate section_1 full sentences.
- section_3: near_term_logic and long_term_logic each has 2-3 rows; include trackable verification_metrics.
- section_4: 4-7 catalysts; no broker report publication, rating change, or target price update as catalyst.
- Never use generic fallback phrases from the forbidden generic fallback list.
"""

def gen_hkus_sections_1_2_4(key_data: dict, ref_map: dict | None = None) -> tuple[dict[str, str], bool, dict]:
    """Generate sections 1-4 as JSON, validate schema, then render Markdown."""
    if not _has_target_materials(key_data):
        return ({}, False, {"call_mode": "skipped_no_target_materials", "parse_attempts": 0, "schema_issues": ["no_target_materials"]})
    ref_map = ref_map or {}
    schema_issues: list[str] = []
    parse_attempts = 0
    for scope in ("sections_1_4", "sections_1_4_retry"):
        prompt = _sections_1_4_json_prompt(key_data, schema_issues if scope.endswith("retry") else None)
        text, ok = _call_llm(prompt, max_tokens=9000, timeout=180, system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=scope)
        if not ok:
            schema_issues.append(f"{scope}:llm_failed")
            continue
        parse_attempts += 1
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            schema_issues.append(f"{scope}:{parse_error}")
            continue
        clean, issues = _validate_sections_1_4_payload(payload, ref_map)
        if not issues:
            return _render_sections_1_4(clean), True, {"call_mode": "ok_json_combined", "parse_attempts": parse_attempts, "schema_issues": []}
        schema_issues.extend(f"{scope}:{x}" for x in issues)

    split_payload: dict[str, Any] = {}
    for scope in ("sections_1_2", "sections_3_4"):
        prompt = _sections_1_4_json_prompt(key_data, schema_issues, section_scope=scope)
        text, ok = _call_llm(prompt, max_tokens=7000, timeout=180, system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=scope)
        if not ok:
            schema_issues.append(f"{scope}:llm_failed")
            continue
        parse_attempts += 1
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            schema_issues.append(f"{scope}:{parse_error}")
            continue
        split_payload.update(payload)
    if split_payload:
        clean, issues = _validate_sections_1_4_payload(split_payload, ref_map)
        if not issues:
            return _render_sections_1_4(clean), True, {"call_mode": "ok_json_split", "parse_attempts": parse_attempts, "schema_issues": []}
        schema_issues.extend(f"split:{x}" for x in issues)
    return ({}, False, {"call_mode": "failed_schema", "parse_attempts": parse_attempts, "schema_issues": schema_issues[:80]})


# ---------------------------------------------------------------------------
# section prompt builders
# ---------------------------------------------------------------------------

def _section_specific_requirements(sec_key: str) -> list[str]:
    by_section = {
        "s12": [
            "§1 has only 3-4 high-conviction conclusions with real [N] citations.",
            "§2 bullets start with **bold keyword**; use specific numbers only when disclosed, otherwise write concrete event, operating implication and [N].",
        ],
        "s34": [
            "§3 separates near-term and long-term logic; include business mechanism and verification variables supported by [N].",
            "§4 catalyst table has at least 4 dated rows; each row must be 时间 | 事件 | 影响, and both event and impact must cite [N].",
        ],
        "s57": [
            "§5 explains business composition using only cited source facts.",
            "§6 keeps upstream / middle platform / downstream ecology, with concrete business objects and verification variables.",
            "§7 financials may be generated only when the requested H2 list contains §7; HK PIT financials are handled deterministically outside the LLM.",
        ],
        "s89": [
            "§8 summarizes source-backed market concerns.",
            "Do not output §9, peer rows, peer tables, or A/H mapping; §9 is built deterministically from peer evidence.",
        ],
        "s12r": [
            "§12 must have 4-6 risk title bullets with **bold names** and [N] citations.",
            "Do not write risk body, mechanism paragraphs, or tracking-signal prose.",
        ],
    }
    return by_section.get(sec_key, [])

def _build_section_prompt(sec_key: str, mats_text: str, co: str, ticker: str, mkt: str,
                          h2_override: list[str] | None = None) -> str:
    """Build section-specific generation prompt."""
    h2s = h2_override or SECTION_MATERIAL_MAP[sec_key]["h2"]
    h2_text = "\n".join(f"## {h}" for h in h2s)
    section_rules = "\n".join(f"- {x}" for x in _section_specific_requirements(sec_key))

    return f"""You are a senior equity research analyst covering HK/US stocks.

Company: {co} ({ticker}) | Market: {mkt}

Write the following sections:
{h2_text}

Available materials (use [N] for citation numbers matching the ref guide below):
{mats_text}

Requirements:
1. Use EXACTLY the H2 titles listed above; do not invent English alternatives.
2. All numbers and facts MUST cite [N] from the ref guide.
3. Never use placeholder text like N/A, pending, data-unavailable.
4. If source-backed data is unavailable, fail closed by omitting unsupported rows rather than filling placeholders.
5. Never mention pipeline, checker, quality gate, P0, P1, P2.
{section_rules}

Output ONLY the Markdown sections above, no explanation."""


# ---------------------------------------------------------------------------
# section-wise report generation
# ---------------------------------------------------------------------------

def write_report(materials: dict, source_trace: dict, ticker: str, market: str,
                 company_name: str, output_dir: str, peer_bundle: dict | None = None) -> tuple:
    """Generate report via section-wise LLM calls. Returns (content, gen_status)."""
    mkt = "HK" if market.lower() == "hk" else "US"
    key, _, model = _find_llm_creds()

    industry = materials.get("industry", "") or (materials.get("tags", [None])[0] or "未披露行业")
    price = _extract_price(materials)
    print(f"[4/12] Section-wise generation ({model or 'no-llm'})")

    # 1. compress
    compressed = compress_materials(materials, source_trace)
    print(f"  compressed: {len(source_trace.get('sources',[]))} sources -> {len(compressed)} entries")

    # 2. assign
    assigned = _assign_sources(compressed)

    # 3. build reference section (deterministic)
    ref_text, ref_map = _build_ref_text(source_trace)
    guide = _ref_guide(ref_map)
    print(f"  refs: {len(ref_map)} deterministic entries")
    key_data = _build_hkus_key_data(materials, ref_map, company_name, ticker, mkt)
    target_materials_ok = _has_target_materials(key_data)
    _, target_price_meta = _collect_target_price_records(materials, ref_map, company_name, ticker, mkt, return_meta=True)
    print(f"  target matched reports: {len(key_data.get('target_matched_reports', []))}")

    # 4. section-wise LLM generation (body only, LLM must NOT output H2/H3)
    texts = {}
    status = {}
    failed = []

    sections_1_4_meta = {"call_mode": "not_called", "parse_attempts": 0, "schema_issues": []}
    if key and target_materials_ok:
        combined_14, ok_14, sections_1_4_meta = gen_hkus_sections_1_2_4(key_data, ref_map)
        if ok_14:
            texts.update(combined_14)
            status["s12"] = "ok_json_sections_1_4"
            status["s34"] = "ok_json_sections_1_4"
            status["1"] = status["2"] = status["3"] = status["4"] = sections_1_4_meta.get("call_mode", "ok_json_combined")
            print(f"    [ch1-4] LLM combined, {len(texts.get('s12','')) + len(texts.get('s34',''))} chars")
        else:
            status["1"] = status["2"] = status["3"] = status["4"] = sections_1_4_meta.get("call_mode", "failed_schema")
            print("    [ch1-4] LLM combined failed; continue section flow")

    for sk in SECTION_ORDER:
        si = SECTION_MATERIAL_MAP[sk]
        if sk in texts:
            continue
        if sk in ("s12", "s34"):
            status[sk] = status.get(sk) or "failed_json_required"
            if sk == "s12":
                failed.extend(["1", "2"])
            else:
                failed.extend(["3", "4"])
            print(f"    [{si['label']}] FAIL (sections 1-4 require validated JSON; no Markdown fallback)")
            continue

        # ch10-11: deterministic (no LLM for valuation section)
        if sk == "s1011":
            sec10 = _build_market_debate_section(materials, ref_map, company_name, ticker)
            sec11 = _build_valuation_section_group(materials, ref_map, company_name, ticker, mkt)
            parts = []
            if sec10:
                parts.append(sec10)
                status["10"] = "ok_deterministic"
            else:
                status["10"] = "failed_no_concrete_debate_source"
                failed.append("10")
            if sec11:
                parts.append(sec11)
                status["11"] = "ok_deterministic"
            else:
                status["11"] = "failed_no_traceable_target_price"
                failed.append("11")
            if parts:
                texts[sk] = "\n\n".join(parts)
                status[sk] = "degraded_partial" if len(parts) < 2 else "ok_deterministic"
                print(f"    [{si['label']}] deterministic split, {len(texts[sk])} chars")
            else:
                status[sk] = "failed"
                failed.append(sk)
                print(f"    [{si['label']}] FAIL (no source-backed debate/valuation)")
            continue

        if sk == "s89":
            peer_section = build_peer_comparison_section(peer_bundle or {}, ref_map) if build_peer_comparison_section else ""
            print(f"    [{si['label']}] generating §8 JSON + deterministic §9...")
            sec8_text = ""
            if key and target_materials_ok:
                sec8_text, ok8, issues8 = gen_hkus_section_8(key_data, ref_map)
                if ok8:
                    status["8"] = "ok_json"
                else:
                    status["8"] = "failed_json_schema"
                    status["8_schema_issues"] = issues8[:30]
                    failed.append("8")
            else:
                status["8"] = "failed_sparse_or_no_llm"
                failed.append("8")
            valid_peer_rows = int((peer_bundle or {}).get("valid_peer_rows") or 0)
            if peer_section:
                status["9"] = "ok_peer_deterministic" if valid_peer_rows >= 2 else "failed_peer_count"
                if valid_peer_rows < 2:
                    failed.append("9")
            else:
                status["9"] = "failed_no_peer_evidence"
                failed.append("9")
            texts[sk] = "\n\n".join(x for x in [sec8_text, peer_section] if x)
            status[sk] = "ok" if sec8_text and peer_section and valid_peer_rows >= 2 else "degraded_partial"
            continue

        if sk == "s12r":
            print(f"    [{si['label']}] generating §12 risk-title JSON...")
            if key and target_materials_ok:
                sec12_text, ok12, issues12 = gen_hkus_section_12(key_data, ref_map)
                if ok12:
                    texts[sk] = sec12_text
                    status["12"] = "ok_json"
                    status[sk] = "ok_json"
                else:
                    status["12"] = "failed_json_schema"
                    status["12_schema_issues"] = issues12[:30]
                    status[sk] = "failed_json_schema"
                    failed.append("12")
            else:
                status["12"] = "failed_sparse_or_no_llm"
                status[sk] = "failed_sparse_or_no_llm"
                failed.append("12")
            continue

        hk_s57 = sk == "s57" and mkt == "HK"
        hk_financial_section = _build_hk_financial_section(materials, ref_map) if hk_s57 else ""
        prompt_h2 = ["5 业务拆分", "6 产销链与生态"] if hk_s57 else None
        mats = _build_section_context(sk, key_data, assigned, ref_map)
        prompt = _build_section_prompt(sk, mats, company_name, ticker, mkt, h2_override=prompt_h2)
        prompt += f"\n\n---\nREFERENCE GUIDE (only use these [N] numbers, 1-{len(ref_map)}):\n{guide}\n\nDo NOT invent new [N] numbers beyond range 1-{len(ref_map)}."

        print(f"    [{si['label']}] generating ({len(mats)} chars)...")

        # Sparse materials are a generation failure in RC mode. Do not output
        # placeholders as if the section succeeded.
        if len(mats) < 100 or (sk != "s12r" and not target_materials_ok):
            if hk_s57 and hk_financial_section:
                texts[sk] = hk_financial_section
                status["7"] = "ok_pit_deterministic"
                status[sk] = "degraded_partial_hk_pit_only"
                failed.extend(["5", "6"])
                print(f"      PARTIAL deterministic HK §7, {len(hk_financial_section)} chars")
            else:
                status[sk] = "failed_sparse"; failed.append(sk)
                print(f"      FAIL (sparse/untargeted materials, {len(mats)} chars)")
            continue

        if not key:
            if hk_s57 and hk_financial_section:
                texts[sk] = hk_financial_section
                status["7"] = "ok_pit_deterministic"
                status[sk] = "degraded_partial_hk_pit_only"
                failed.extend(["5", "6"])
                print(f"      OK deterministic HK §7 no-llm, {len(hk_financial_section)} chars")
                continue
            status[sk] = "skipped"; failed.append(sk); continue

        t0 = time.time()
        text, ok = _call_llm(prompt, max_tokens=si.get("max_tokens", 8000),
                             timeout=si.get("timeout", 180),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS)
        dt = time.time() - t0
        if ok and text and len(text.strip()) > 100:
            body = _clean_fence(text)
            if hk_s57:
                if hk_financial_section:
                    body = "\n\n".join([body, hk_financial_section])
                    status["7"] = "ok_pit_deterministic"
                else:
                    status["7"] = "failed_missing_pit"
                    failed.append("7")
            texts[sk] = body
            status[sk] = "ok" if not hk_s57 or hk_financial_section else "degraded_missing_hk_pit"
            print(f"      OK {dt:.0f}s, {len(body)} chars")
        else:
            if hk_s57 and hk_financial_section:
                texts[sk] = hk_financial_section
                status["7"] = "ok_pit_deterministic"
                status[sk] = "degraded_partial_hk_pit_only"
                failed.extend(["5", "6"])
                print(f"      LLM FAIL {dt:.0f}s; deterministic HK §7, {len(hk_financial_section)} chars")
            else:
                status[sk] = "failed"; failed.append(sk)
                print(f"      FAIL {dt:.0f}s")

    # 5. Extract title conclusion from §1 content
    mkt_cn = "港股" if mkt == "HK" else "美股"
    conclusion = _derive_title_conclusion(texts.get("s12", ""), company_name, mkt_cn, ticker)
    title_status = "ok" if conclusion else "failed_no_investment_conclusion"
    title_issues = [] if conclusion else ["title_status=failed_no_investment_conclusion"]
    title = _build_report_title(company_name, ticker, mkt_cn, conclusion)
    ticker_meta = f" | ticker：{ticker}" if mkt == "US" else ""
    meta = f"\n市场：{mkt_cn}{ticker_meta} | 行业：{industry} | 当前价格/市值：{price} | 生成日期：{TODAY}\n"
    report, assembly_issues = assemble_fixed_hk_us_sections(title, meta, texts, ref_text)
    assembly_issues.extend(title_issues)

    # 6. normalize references (keep only body-used refs, renumber 1..N)
    report, ref_map, reference_issues = normalize_used_references(report, source_trace)
    assembly_issues.extend(reference_issues)
    report = _normalize_table_separators(report)

    # 7. status
    h2_matches = list(re.finditer(r'^##\s*(\d{1,2})\s+[^\n]+$', report, re.M))
    section_h2_status = {}
    for sec_no in range(1, 13):
        body = ""
        match = next((m for m in h2_matches if int(m.group(1)) == sec_no), None)
        if match:
            idx = h2_matches.index(match)
            end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else report.find("## 13 参考资料")
            if end < 0:
                end = len(report)
            body = report[match.end():end].strip()
        section_h2_status[str(sec_no)] = "ok" if body and _is_substantive_section(body) else "failed"
    ok_n = sum(1 for v in section_h2_status.values() if v == "ok")
    failed_h2 = [k for k, v in section_h2_status.items() if v != "ok"]
    is_skel = ok_n == 0
    is_deg = bool(failed_h2) or len(failed) > 0 or bool(assembly_issues)
    gs = {
        "total_sections": 12, "ok_sections": ok_n,
        "failed_sections": failed_h2,
        "failed_groups": failed,
        "section_status": status, "is_degraded": is_deg, "is_skeleton": is_skel,
        "mode": "skeleton" if is_skel else ("degraded" if is_deg else "full"),
        "generated_at": TODAY_ISO, "ref_count": len(ref_map),
        "assembly_issues": assembly_issues,
        "section_h2_status": section_h2_status,
        "title_status": title_status,
        "references_status": "issue" if reference_issues else "ok",
        "reference_issues": reference_issues,
        "unmatched_source_count": assigned.get("_unmatched_source_count", 0),
        "target_reports_total": key_data.get("target_reports_total", 0),
        "target_reports_referenceable": key_data.get("target_reports_referenceable", 0),
        "target_reports_dropped_no_ref": key_data.get("target_reports_dropped_no_ref", 0),
        "dropped_mixed_unit_count": target_price_meta.get("dropped_mixed_unit_count", 0),
        "target_price_unit": target_price_meta.get("target_price_unit", ""),
        "peer_discovery_status": (peer_bundle or {}).get("status", "unsupported"),
        "peer_candidates_total": (peer_bundle or {}).get("peer_candidates_total", 0),
        "peer_entities_resolved": (peer_bundle or {}).get("peer_entities_resolved", 0),
        "peer_valid_rows": (peer_bundle or {}).get("valid_peer_rows", 0),
        "peer_query_count": (peer_bundle or {}).get("query_count", 0),
        "peer_dropped_unresolved": sum(1 for x in (peer_bundle or {}).get("dropped_candidates", []) if x.get("reason") in ("unresolved", "ambiguous")),
        "peer_dropped_no_material": sum(1 for x in (peer_bundle or {}).get("dropped_candidates", []) if x.get("reason") == "no_material"),
        "peer_dropped_no_progress": sum(1 for x in (peer_bundle or {}).get("dropped_candidates", []) if x.get("reason") == "no_progress"),
        "peer_latest_progress_date": max([p.get("latest_progress", {}).get("date", "") for p in (peer_bundle or {}).get("peers", [])] or [""]),
        "ah_mapping_status": (peer_bundle or {}).get("ah_mapping", {}).get("status", "none"),
        "ah_mapping_row_count": len((peer_bundle or {}).get("ah_mapping", {}).get("rows", [])),
    }

    rp = Path(output_dir) / "report.md"
    rp.write_text(report, encoding="utf-8")
    tag = "SKELETON" if is_skel else ("DEGRADED" if is_deg else "FULL")
    print(f"  [{tag}] {ok_n}/12 sections OK, {len(ref_map)} refs -> {rp}")
    return (report, gs)

# ── Fixed section shell & normalization ──

FIXED_H2_TITLES = [
    "1 关键要点", "2 近况跟踪", "3 核心投资逻辑",
    "4 催化事件时间表", "5 业务拆分", "6 产销链与生态",
    "7 财务与盈利质量", "8 市场关注", "9 行业对比与 A/H 映射",
    "10 市场分歧", "11 估值与预测", "12 风险提示", "13 参考资料",
]

def _strip_section_headers(text: str) -> str:
    """Remove H2/H3 headers from LLM output (writer injects fixed headers)."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if re.match(r'^#{2,4}\s+', stripped):
            continue  # skip H2/H3/H4 headers
        lines.append(line)
    return "\n".join(lines).strip()

def _extract_numbered_h2_sections(text: str) -> dict[int, str]:
    """Return {section_number: body} from a generated markdown chunk."""
    text = _clean_fence(text)
    matches = list(re.finditer(r'^##\s*(\d{1,2})\s+[^\n]*$', text, re.M))
    if not matches:
        return {}
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[num] = body
    return sections

def _chunk_without_h2(text: str) -> str:
    return _strip_section_headers(_clean_fence(text)).strip()

def _legacy_chunk_split_group(sec_key: str, text: str) -> dict[int, str]:
    """Best-effort split for legacy chunks. Kept strict: missing sections are reported."""
    body = _chunk_without_h2(text)
    if not body:
        return {}
    expected = [int(h.split()[0]) for h in SECTION_MATERIAL_MAP[sec_key]["h2"]]
    if len(expected) == 1:
        return {expected[0]: body}
    # Avoid the old failure mode of appending all content to the last heading.
    # Put the legacy chunk under the first expected H2 and let validation block
    # the remaining empty sections.
    return {expected[0]: body}

def _is_substantive_section(body: str) -> bool:
    body = re.sub(r'^\s*>.*$', '', body, flags=re.M)
    body = re.sub(r'^\|[-:\s|]+\|$', '', body, flags=re.M)
    plain = re.sub(r'[#*`_\[\]\(\)\|\-:：,\s，。；;、]+', '', body)
    if re.search(r'生成失败|LLM不可用|degraded|fallback failed', body, re.I):
        return False
    return len(plain) >= 20 or len(re.findall(r'^\|.+\|$', body, re.M)) >= 3

def _validate_final_hk_us_sections(report: str) -> list[str]:
    issues = []
    h2_matches = list(re.finditer(r'^##\s*(\d{1,2})\s+([^\n]+)$', report, re.M))
    h2_map = {int(m.group(1)): m for m in h2_matches}
    for sec_no in range(1, 13):
        m = h2_map.get(sec_no)
        if not m:
            issues.append(f"missing_h2:§{sec_no}")
            continue
        idx = h2_matches.index(m)
        end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else report.find("## 13 参考资料")
        if end < 0:
            end = len(report)
        body = report[m.end():end].strip()
        if not _is_substantive_section(body):
            issues.append(f"empty_or_placeholder_h2:§{sec_no}")
    if re.search(r'生成失败|LLM不可用|degraded|fallback failed', report, re.I):
        issues.append("forbidden_degraded_marker_in_report")
    return issues

def assemble_fixed_hk_us_sections(title: str, meta: str, section_bodies: dict,
                                  ref_text: str) -> tuple[str, list[str]]:
    """Assemble report with one fixed H2 shell and routed content per H2."""
    routed: dict[int, str] = {}
    for sk in SECTION_ORDER:
        if sk not in section_bodies:
            continue
        extracted = _extract_numbered_h2_sections(section_bodies[sk])
        if not extracted:
            extracted = _legacy_chunk_split_group(sk, section_bodies[sk])
        expected = {int(h.split()[0]) for h in SECTION_MATERIAL_MAP[sk]["h2"]}
        for num, body in extracted.items():
            if num in expected and body.strip():
                routed[num] = body.strip()

    parts = [title, meta]
    for h in FIXED_H2_TITLES[:-1]:
        sec_no = int(h.split()[0])
        parts.append(f"\n## {h}\n")
        body = routed.get(sec_no, "").strip()
        if body:
            parts.append(body)
    parts.append(ref_text)
    report = "\n".join(parts)
    return report, _validate_final_hk_us_sections(report)


# ── Deterministic section builders (§10-11) ──

def _build_market_debate_section(materials: dict, ref_map: dict,
                                 co: str, ticker: str) -> str:
    """Build §10 only from concrete target-company debate evidence."""
    debate = _build_market_debate(materials, ref_map, co, ticker)
    return f"## 10 {FIXED_H2_TITLES[9].split(' ', 1)[1]}\n\n{debate}" if debate else ""

def _extract_theme_evidence(rd: dict, keywords: list[str]) -> str:
    """Extract one concise source sentence for a debate theme."""
    title = _plain_text(rd.get("articleTitle") or rd.get("title") or "", 140)
    abstract = _plain_text(rd.get("textAbstract") or rd.get("summary") or "", 900)
    text = f"{abstract}。{title}"
    sentences = [s.strip() for s in re.split(r'[。；;.!?\n]', text) if s.strip()]
    for sent in sentences:
        clean = _plain_text(sent, 120)
        if not any(k and k.lower() in clean.lower() for k in keywords):
            continue
        clean = re.sub(r'^\s*[\w\u4e00-\u9fff]{1,12}[：:丨｜-]\s*', '', clean).strip()
        if title and (clean == title or title in clean):
            continue
        zh_len = len(re.findall(r'[\u4e00-\u9fff]', clean))
        if 25 <= zh_len <= 80:
            return clean
    return ""

def _extract_verification_metrics(evidence: str, keywords: list[str] | None = None) -> list[str]:
    metric_patterns = [
        ("营收增速", r"营收增速|收入增速|revenue growth"),
        ("收入", r"收入|营收|revenue"),
        ("毛利率", r"毛利率|gross margin"),
        ("经营利润率", r"经营利润率|operating margin"),
        ("订单", r"订单|order"),
        ("用户数", r"用户数|用户|MAU|DAU|user"),
        ("交付量", r"交付量|交付|deliver"),
        ("价格", r"价格|定价|price|pricing"),
        ("资本开支", r"资本开支|capex"),
        ("经营现金流", r"经营现金流|operating cash flow"),
        ("自由现金流", r"自由现金流|free cash flow|FCF"),
        ("回购", r"回购|buyback"),
        ("分红", r"分红|dividend"),
        ("市场份额", r"市场份额|份额|share"),
        ("利润率", r"利润率|margin"),
    ]
    found = []
    for label, pattern in metric_patterns:
        if re.search(pattern, evidence or "", re.I) and label not in found:
            found.append(label)
    return found

def _build_market_debate(materials: dict, ref_map: dict, co: str, ticker: str) -> str:
    """Aggregate market debate by theme; evidence is summarized instead of copying report titles."""
    target_reports, _ = _target_research_details(materials, co, ticker, ref_map, limit=12)
    if not target_reports:
        return ""

    theme_specs = [
        ("收入增长与主业兑现", ["收入", "增长", "主业", "订单", "需求", "营收", "revenue", "growth", "demand"],
         "若收入增长和主业兑现持续，市场会更愿意上修业绩可见度",
         "若需求或订单转弱，收入和利润弹性会同步承压",
         "收入增速、订单/用户/交付量、利润率"),
        ("利润率与现金流质量", ["利润率", "毛利率", "经营利润", "现金流", "free cash flow", "margin", "cash flow"],
         "若利润率和现金流同步改善，估值支撑更扎实",
         "若费用、成本或营运资本拖累现金流，盈利质量会被折价",
         "毛利率、经营利润率、经营现金流、自由现金流"),
        ("产品迭代与商业化", ["产品", "商业化", "客户", "订阅", "交付", "product", "customer", "subscription"],
         "若产品迭代能转化为客户付费或交付增长，增长持续性更强",
         "若产品落地慢于预期，收入确认和客户留存会受影响",
         "客户数、付费率、交付量、续约率"),
        ("投入强度与资本配置", ["资本开支", "回购", "分红", "资本配置", "capex", "buyback", "dividend"],
         "若投入强度与现金回报匹配，市场对中期回报的容忍度更高",
         "若投入先于回报或股东回报弱化，估值中枢会承压",
         "资本开支、回购/分红、自由现金流"),
        ("监管、竞争与宏观风险", ["监管", "竞争", "价格", "政策", "宏观", "regulation", "competition", "pricing", "macro"],
         "若外部风险边际缓和，估值风险溢价有下降空间",
         "若竞争、价格或监管压力升温，盈利预测会面临下修",
         "价格、份额、政策进展、费用率"),
    ]

    rows = []
    used_refs = set()
    for theme, keywords, bull, bear, variables in theme_specs:
        selected = None
        for rd in target_reports:
            rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
            if rn <= 0 or rn in used_refs:
                continue
            evidence = _extract_theme_evidence(rd, keywords)
            if evidence:
                selected = (rn, evidence)
                break
        if not selected:
            continue
        rn, evidence = selected
        metrics = _extract_verification_metrics(evidence, keywords)
        if not metrics:
            continue
        used_refs.add(rn)
        rows.append((bull, f"{evidence}[{rn}]", bear, f"{'、'.join(metrics)}[{rn}]"))
    if len(rows) < 3:
        return ""
    header = "| \u591a\u5934\u89c2\u70b9 | \u8bc1\u636e | \u7a7a\u5934\u89c2\u70b9 | \u9a8c\u8bc1\u70b9 |\n|:---|:---|:---|:---|"
    return header + "\n" + "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)


def _build_valuation_section_group(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str) -> str:
    valuation = _build_valuation_section(materials, ref_map, co, ticker, mkt)
    if not valuation:
        return ""
    parts = [f"## 11 {FIXED_H2_TITLES[10].split(' ', 1)[1]}", valuation]
    scenario = _build_scenario_section(materials, ref_map, co, mkt, ticker)
    if scenario:
        parts.append(scenario)
    return "\n\n".join(parts)


def _target_aliases(co: str, ticker: str) -> tuple[set[str], set[str]]:
    co = str(co or "")
    t = (ticker or "").upper().replace(".HK", "")
    ticker_aliases = {t, ticker.upper()} if ticker else set()
    if t.isdigit():
        ticker_aliases.add(t.lstrip("0") or t)
    name_aliases = {co, co.lower(), co.upper()} if co else set()
    if "META" in t or "META" in co.upper():
        name_aliases.update({"Meta", "META", "Meta Platforms", "METAPLATFORMS"})
        ticker_aliases.add("META")
    if "01024" in t or "1024" == t.lstrip("0") or "快手" in co:
        name_aliases.update({"快手", "快手-W", "Kuaishou", "KUAISHOU"})
        ticker_aliases.update({"01024", "1024", "01024.HK", "1024.HK"})
    if "00700" in t or "腾讯" in co:
        name_aliases.update({"腾讯", "腾讯控股", "Tencent", "TENCENT"})
        ticker_aliases.update({"00700", "700", "00700.HK", "700.HK"})
    if "MSFT" in t or "MICROSOFT" in co.upper():
        name_aliases.update({"Microsoft", "MICROSOFT", "微软"})
        ticker_aliases.add("MSFT")
    if "TSLA" in t or "TESLA" in co.upper() or "特斯拉" in co:
        name_aliases.update({"Tesla", "TESLA", "Tesla, Inc.", "特斯拉"})
        ticker_aliases.add("TSLA")
    name_aliases = {a for a in name_aliases if not re.fullmatch(r'[\u4e00-\u9fff]', str(a or ""))}
    return name_aliases, ticker_aliases

def _normalize_company_name(name: Any) -> str:
    text = re.sub(r'\s+', '', str(name or "").lower())
    for suffix in ("股份有限公司", "有限公司", "集团控股有限公司", "控股有限公司", "inc.", "inc", "corp.", "corp", "ltd.", "ltd"):
        text = text.replace(suffix.lower(), "")
    return text

def _contains_english_alias_with_boundary(text: str, alias: str) -> bool:
    alias = str(alias or "").strip()
    if not alias or not re.search(r'[A-Za-z]', alias):
        return False
    return bool(re.search(rf'(?<![A-Z0-9]){re.escape(alias.upper())}(?![A-Z0-9])', text.upper()))

def _extract_explicit_security_codes(title: str) -> set[str]:
    text = str(title or "")
    upper = text.upper()
    codes = set()
    for m in re.finditer(r'\b(\d{3,5})\s*(?:\.HK|HK)\b', upper):
        codes.add((m.group(1).lstrip("0") or m.group(1)))
    for m in re.finditer(r'(?:股票代码|证券代码|代码)[:：\s]*([0-9]{3,5})(?:\.HK)?', text, re.I):
        codes.add((m.group(1).lstrip("0") or m.group(1)))
    for m in re.finditer(r'[\(（]([0-9]{5})(?:\.HK)?[\)）]', upper):
        codes.add((m.group(1).lstrip("0") or m.group(1)))
    for m in re.finditer(r'\b(?:NASDAQ|NYSE|AMEX)[:：\s]+([A-Z]{1,6})\b', upper):
        codes.add(m.group(1))
    for m in re.finditer(r'\b([A-Z]{2,6})\s+US\b', upper):
        if m.group(1) not in {"FY", "Q", "AI"}:
            codes.add(m.group(1))
    for m in re.finditer(r'[\(（]([A-Z]{2,6})[\)）]', upper):
        token = m.group(1)
        if token not in {"AI", "FY", "Q", "Q1", "Q2", "Q3", "Q4"} and not re.match(r'FY\d+', token):
            codes.add(token)
    return codes


def _source_matches_target(rd: dict, co: str, ticker: str) -> bool:
    name_aliases, ticker_aliases = _target_aliases(co, ticker)
    ticker_norm_hk = _normalize_security_code(ticker, "HK")
    ticker_norm_us = _normalize_security_code(ticker, "US")
    target_codes = {a.upper().replace(".HK", "") for a in ticker_aliases if a}
    for c in (ticker_norm_hk, ticker_norm_us):
        if c:
            target_codes.add(c.upper().replace(".HK", ""))
            if c.upper().replace(".HK", "").isdigit():
                target_codes.add(c.upper().replace(".HK", "").lstrip("0") or c.upper().replace(".HK", ""))

    structured_codes = []
    for field in ("stockId", "secCode", "ticker", "securityCode", "stockCode"):
        val = str(rd.get(field, "") or "").strip()
        if val:
            structured_codes.append(_normalize_security_code(val, "HK").upper().replace(".HK", ""))
            structured_codes.append(_normalize_security_code(val, "US").upper().replace(".HK", ""))
            structured_codes.append(val.upper().replace(".HK", ""))
    if any(c and c in target_codes for c in structured_codes):
        return True
    nonempty_structured = {c for c in structured_codes if c}
    if nonempty_structured and not (nonempty_structured & target_codes):
        return False

    title = " ".join(str(rd.get(k, "") or "") for k in ("articleTitle", "articleTitleEN", "title"))
    company_name = str(rd.get("companyName", "") or "")
    title_upper = title.upper()
    title_codes = _extract_explicit_security_codes(title)
    if title_codes:
        normalized_title_codes = {c.upper().replace(".HK", "") for c in title_codes}
        normalized_title_codes.update(c.lstrip("0") for c in list(normalized_title_codes) if c.isdigit())
        if not (normalized_title_codes & target_codes):
            return False
        return True

    company_norm = _normalize_company_name(company_name)
    alias_norms = {_normalize_company_name(a) for a in name_aliases if a}
    if company_norm and company_norm in alias_norms:
        return True
    for a in name_aliases:
        alias = str(a or "")
        if not alias:
            continue
        if re.search(r'[\u4e00-\u9fff]', alias) and alias in title:
            return True
        if _contains_english_alias_with_boundary(title, alias):
            return True
    for a in ticker_aliases:
        au = str(a or "").upper()
        if not au or au.replace(".HK", "").isdigit():
            continue
        if re.search(rf'(?<![A-Z0-9]){re.escape(au)}(?![A-Z0-9])', title_upper):
            return True
    return False

def _fmt_metric_value(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        fv = float(v)
        if abs(fv) >= 1e8:
            return f"{fv / 1e8:.1f}亿"
        if abs(fv) >= 1e4:
            return f"{fv / 1e4:.1f}万"
        return f"{fv:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)

def _first_value(row: dict, keys: list[str]) -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None

def _hk_pit_ref_numbers(ref_map: dict) -> dict:
    refs = {}
    for rn, meta in ref_map.items():
        api = meta.get("api_nameEn", "")
        if api in HK_PIT_API_SOURCES:
            refs[api] = rn
    return refs

def _period_row_map(rows: list[dict]) -> dict:
    mapped = {}
    for row in rows or []:
        period = str(row.get("endDate", "") or row.get("reportDate", ""))[:10]
        if not period:
            continue
        current = mapped.get(period)
        if current is None:
            mapped[period] = row
            continue
        try:
            if int(row.get("fiscalPeriod") or 0) >= int(current.get("fiscalPeriod") or 0):
                mapped[period] = row
        except Exception:
            mapped[period] = row
    return mapped

def _pct(num: Any, den: Any) -> str:
    try:
        if float(den) == 0:
            return ""
        return f"{float(num) / float(den) * 100:.1f}%"
    except Exception:
        return ""

def _hk_pit_period_context(materials: dict) -> tuple[list[str], dict, dict, dict]:
    hk = materials.get("structured", {}).get("hk_financials", {})
    is_by = _period_row_map(hk.get("getHkFdmtIsPit", []) or [])
    bs_by = _period_row_map(hk.get("getHkFdmtBsPit", []) or [])
    cf_by = _period_row_map(hk.get("getHkFdmtCfPit", []) or [])
    annual_candidates = [
        p for p in (set(is_by.keys()) & set(bs_by.keys()) & set(cf_by.keys()))
        if p.endswith("-12-31")
    ]
    annual_periods = sorted(annual_candidates, reverse=True)[:3]
    periods = annual_periods if len(annual_periods) >= 3 else sorted(set(is_by.keys()) | set(bs_by.keys()) | set(cf_by.keys()), reverse=True)[:3]
    return periods, is_by, bs_by, cf_by

def _hk_display_periods(periods: list[str]) -> list[str]:
    return [f"FY{p[:4]}" if p.endswith("-12-31") else p for p in periods]

def _hk_financial_quality_text(materials: dict, pit_refs: dict, periods: list[str]) -> str:
    hk = materials.get("structured", {}).get("hk_financials", {})
    if not hk or not periods:
        return ""
    periods, is_by, bs_by, cf_by = _hk_pit_period_context(materials)
    latest = periods[0]
    previous = periods[1] if len(periods) > 1 else ""
    latest_is = is_by.get(latest, {})
    latest_bs = bs_by.get(latest, {})
    latest_cf = cf_by.get(latest, {})
    previous_is = is_by.get(previous, {}) if previous else {}
    rev = _first_value(latest_is, ["revenue", "tRevenue", "totalRevenue", "operRevenue"])
    prev_rev = _first_value(previous_is, ["revenue", "tRevenue", "totalRevenue", "operRevenue"])
    gp = _first_value(latest_is, ["grossProf", "grossProfit"])
    prev_gp = _first_value(previous_is, ["grossProf", "grossProfit"])
    op = _first_value(latest_is, ["operProfit", "operateProfit", "operatingProfit", "opProfit"])
    net = _first_value(latest_is, ["nIncomeAttrP", "netProfitAttrP", "nIncome", "netIncome"])
    cfo = _first_value(latest_cf, ["nCfOperateA", "netCFO", "netCashFlowsOperate", "cashFlowOperate"])
    assets = _first_value(latest_bs, ["tAssets", "totalAssets"])
    liabilities = _first_value(latest_bs, ["tLiab", "totalLiab", "totalLiabilities"])
    equity = _first_value(latest_bs, ["tEquityAttrP", "equityAttrP", "totalEquity"])
    is_ref = pit_refs.get("getHkFdmtIsPit", -1)
    bs_ref = pit_refs.get("getHkFdmtBsPit", -1)
    cf_ref = pit_refs.get("getHkFdmtCfPit", -1)
    if min(is_ref, bs_ref, cf_ref) <= 0:
        return ""

    bullets = []
    if len(periods) < 3:
        bullets.append(f"- **趋势口径**：当前仅取得最新期间（{periods[0] if periods else 'latest'}），不把单期数据写成三期趋势[{is_ref}]。")
    else:
        bullets.append(f"- **趋势口径**：当前覆盖{', '.join(_hk_display_periods(periods[:3]))}三个年度期间，收入、利润、现金流和资产负债表口径均可逐期对照[{is_ref}][{bs_ref}][{cf_ref}]。")
    try:
        if rev and prev_rev:
            direction = "提升" if float(rev) >= float(prev_rev) else "回落"
            latest_label = _hk_display_periods([latest])[0]
            previous_label = _hk_display_periods([previous])[0]
            bullets.append(f"- **收入趋势**：{latest_label}营业总收入为{_fmt_metric_value(rev)}，较{previous_label}{direction}，对应表格中收入列的连续变化[{is_ref}]。")
    except Exception:
        pass
    try:
        gm = _pct(gp, rev)
        prev_gm = _pct(prev_gp, prev_rev)
        if gm:
            suffix = f"，前一期为{prev_gm}" if prev_gm else ""
            latest_label = _hk_display_periods([latest])[0]
            bullets.append(f"- **盈利能力**：{latest_label}毛利率约{gm}{suffix}，需结合经营利润率判断业务组合和成本结构变化[{is_ref}]。")
    except Exception:
        pass
    try:
        if cfo and net:
            latest_label = _hk_display_periods([latest])[0]
            bullets.append(f"- **现金流质量**：{latest_label}经营现金流为{_fmt_metric_value(cfo)}，与归母净利润{_fmt_metric_value(net)}对照，可观察盈利兑现和营运资本压力[{is_ref}][{cf_ref}]。")
    except Exception:
        pass
    try:
        if assets and liabilities:
            latest_label = _hk_display_periods([latest])[0]
            bullets.append(f"- **资产负债与资本配置**：{latest_label}资产负债率约{float(liabilities) / float(assets) * 100:.1f}%，需结合资本开支、回购和分红评估资产负债表弹性[{bs_ref}]。")
        elif equity and net:
            latest_label = _hk_display_periods([latest])[0]
            bullets.append(f"- **权益回报**：{latest_label}ROE约{float(net) / float(equity) * 100:.1f}%，需结合股东回报和再投资强度观察资本效率[{is_ref}][{bs_ref}]。")
    except Exception:
        pass
    return "\n".join(bullets[:5])

def _hk_financial_rows(materials: dict, pit_refs: dict) -> tuple[list[str], list[str]]:
    hk = materials.get("structured", {}).get("hk_financials", {})
    if not hk:
        return [], []
    if any(pit_refs.get(api, -1) <= 0 for api in ("getHkFdmtIsPit", "getHkFdmtBsPit", "getHkFdmtCfPit")):
        return [], []
    periods, is_by, bs_by, cf_by = _hk_pit_period_context(materials)
    if not periods:
        return [], []
    def is_val(period: str, keys: list[str]) -> Any:
        return _first_value(is_by.get(period, {}), keys)

    def bs_val(period: str, keys: list[str]) -> Any:
        return _first_value(bs_by.get(period, {}), keys)

    def cf_val(period: str, keys: list[str]) -> Any:
        return _first_value(cf_by.get(period, {}), keys)

    metric_specs = [
        ("营业总收入", lambda p: _fmt_metric_value(is_val(p, ["revenue", "tRevenue", "totalRevenue", "operRevenue"]))),
        ("毛利", lambda p: _fmt_metric_value(is_val(p, ["grossProf", "grossProfit"]))),
        ("经营利润", lambda p: _fmt_metric_value(is_val(p, ["operProfit", "operateProfit", "operatingProfit", "opProfit"]))),
        ("归母净利润", lambda p: _fmt_metric_value(is_val(p, ["nIncomeAttrP", "netProfitAttrP", "nIncome", "netIncome"]))),
        ("EPS", lambda p: _fmt_metric_value(is_val(p, ["basicEPS", "eps", "basicEps"]))),
        ("经营现金流", lambda p: _fmt_metric_value(cf_val(p, ["nCfOperateA", "netCFO", "netCashFlowsOperate", "cashFlowOperate"]))),
        ("资产负债率", lambda p: _pct(bs_val(p, ["tLiab", "totalLiab", "totalLiabilities"]), bs_val(p, ["tAssets", "totalAssets"]))),
        ("ROE", lambda p: _pct(is_val(p, ["nIncomeAttrP", "netProfitAttrP", "nIncome", "netIncome"]), bs_val(p, ["tEquityAttrP", "equityAttrP", "totalEquity"]))),
        ("毛利率", lambda p: _pct(is_val(p, ["grossProf", "grossProfit"]), is_val(p, ["revenue", "tRevenue", "totalRevenue", "operRevenue"]))),
    ]
    rows = []
    for label, getter in metric_specs:
        vals = [getter(p) for p in periods]
        if not vals or any(v in (None, "") for v in vals):
            continue
        rows.append(f"| {label} | {' | '.join(vals)} |")
    return periods, rows

def _build_hk_financial_section(materials: dict, ref_map: dict) -> str:
    pit_refs = _hk_pit_ref_numbers(ref_map)
    required = ("getHkFdmtIsPit", "getHkFdmtBsPit", "getHkFdmtCfPit")
    if any(pit_refs.get(api, -1) <= 0 for api in required):
        return ""
    fin_periods, fin_rows = _hk_financial_rows(materials, pit_refs)
    if not fin_rows:
        return ""
    fin_period_labels = _hk_display_periods(fin_periods)
    is_ref = pit_refs["getHkFdmtIsPit"]
    bs_ref = pit_refs["getHkFdmtBsPit"]
    cf_ref = pit_refs["getHkFdmtCfPit"]
    if len(fin_periods) >= 3:
        note = f"数据来源：港股 PIT 利润表[{is_ref}]、港股 PIT 资产负债表[{bs_ref}]、港股 PIT 现金流量表[{cf_ref}]。"
    else:
        note = (
            f"当前仅取得{', '.join(fin_period_labels)}，不把单期数据写成三期趋势。\n\n"
            f"数据来源：港股 PIT 利润表[{is_ref}]、港股 PIT 资产负债表[{bs_ref}]、港股 PIT 现金流量表[{cf_ref}]。"
        )
    fin_analysis = _hk_financial_quality_text(materials, pit_refs, fin_periods)
    fin_header = f"| 指标 | {' | '.join(fin_period_labels)} |\n|:---|{''.join('---:|' for _ in fin_period_labels)}"
    return f"""## 7 财务与盈利质量

{note}

{fin_header}
{chr(10).join(fin_rows)}

{fin_analysis}"""


def _target_price_float(raw: Any) -> float:
    try:
        return float(str(raw).replace(",", "").replace("HK$", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return 0.0

def _detect_target_price_unit(rd: dict, company_name: str, ticker: str, mkt: str) -> str:
    text = " ".join(str(rd.get(k, "") or "") for k in (
        "targetPriceUnit", "priceUnit", "articleTitle", "title", "textAbstract", "summary"
    ))
    if mkt == "HK":
        if re.search(r'\bADS\b|美国存托股', text, re.I):
            return "港元/ADS"
        if re.search(r'\bADR\b|美国存托凭证', text, re.I):
            return "港元/ADR"
        return "港元/普通股"
    if re.search(r'\bADS\b|美国存托股', text, re.I):
        return "美元/ADS"
    if re.search(r'\bADR\b|美国存托凭证', text, re.I):
        return "美元/ADR"
    if re.search(r'普通股|common share', text, re.I):
        return "美元/普通股"
    if re.search(r'每股|per share', text, re.I):
        return "美元/每股"
    return "美元/每股"

def _target_price_evidence_excerpt(rd: dict) -> str:
    text = _plain_text((rd.get("textAbstract", "") or "") + "。" + (rd.get("articleTitle", "") or rd.get("title", "")), 900)
    for sent in [s.strip() for s in re.split(r'[。；;.!?\n]', text) if s.strip()]:
        if re.search(r'目标价|评级|收入|营收|利润率|现金流|资本开支|回购|分红|订单|用户|交付|target price|rating|revenue|margin|cash flow|capex', sent, re.I):
            return _plain_text(sent, 120)
    return _plain_text(text, 120)

def _clean_assumption_text(rd: dict, company_name: str = "", ticker: str = "") -> str:
    text = _plain_text((rd.get("articleTitle", "") or "") + " " + (rd.get("textAbstract", "") or ""), 360)
    for alias in sorted(_target_aliases(company_name, ticker)[0] | _target_aliases(company_name, ticker)[1], key=len, reverse=True):
        if alias:
            text = re.sub(re.escape(alias), "", text, flags=re.I)
    text = re.sub(r'^\s*[:：\-—]+', '', text).strip()
    phrases = []
    if re.search(r'收入|营收|增长|订单|交付|需求|revenue|growth|demand', text, re.I):
        phrases.append("收入增长与需求兑现")
    if re.search(r'产品|客户|商业化|订阅|交付|product|customer|subscription', text, re.I):
        phrases.append("产品迭代与客户转化")
    if re.search(r'投入|资本开支|研发|capex|investment|R&D', text, re.I):
        phrases.append("投入强度与回报周期")
    if re.search(r'韧性|利润率|毛利率|现金流', text, re.I):
        phrases.append("利润率与现金流质量")
    if re.search(r'回购|分红|股东回报|资本配置', text, re.I):
        phrases.append("股东回报和资本配置纪律")
    if phrases:
        deduped = []
        for p in phrases:
            if p not in deduped:
                deduped.append(p)
        return "；".join(deduped[:2])
    title = _plain_text(rd.get("articleTitle", "") or rd.get("title", ""), 80)
    for alias in sorted(_target_aliases(company_name, ticker)[0] | _target_aliases(company_name, ticker)[1], key=len, reverse=True):
        if alias:
            title = re.sub(re.escape(alias), "", title, flags=re.I)
    title = re.sub(r'.*?[:：]', '', title).strip()
    return title[:28] if title else "目标价来源材料关注点"

def _collect_target_price_records(materials: dict, ref_map: dict, co: str, ticker: str,
                                  mkt: str = "US", return_meta: bool = False):
    records = []
    for rd in materials.get("research", {}).get("details", []):
        if not _source_matches_target(rd, co, ticker):
            continue
        tp = rd.get("targetPrice", "")
        tp_val = _target_price_float(tp)
        if tp_val <= 0:
            continue
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        evidence = _target_price_evidence_excerpt(rd)
        metrics = _extract_verification_metrics(evidence)
        records.append({
            "target": tp_val,
            "org": rd.get("orgName", "") or "--",
            "rn": rn,
            "rating": rd.get("rating", "") or "未明示",
            "date": str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:10],
            "article_id": str(rd.get("articleId", "")),
            "source_order": len(records),
            "assumption": _clean_assumption_text(rd, co, ticker),
            "evidence": evidence,
            "metrics": metrics,
            "unit": _detect_target_price_unit(rd, co, ticker, mkt),
        })
    latest_by_org = {}
    for rec in records:
        org_key = re.sub(r'\W+', '', (rec.get("org") or "").lower())
        current = latest_by_org.get(org_key)
        rec_key = (rec.get("date", ""), rec.get("article_id", ""), -rec.get("source_order", 0))
        cur_key = (current.get("date", ""), current.get("article_id", ""), -current.get("source_order", 0)) if current else None
        if current is None or rec_key > cur_key:
            latest_by_org[org_key] = rec
    unique = sorted(latest_by_org.values(), key=lambda x: (x["target"], x.get("date", ""), x.get("article_id", "")))
    by_unit = {}
    for rec in unique:
        by_unit.setdefault(rec.get("unit", ""), []).append(rec)
    if by_unit:
        selected_unit, selected_records = max(by_unit.items(), key=lambda item: (len(item[1]), item[1][-1].get("date", "")))
    else:
        selected_unit, selected_records = "", []
    dropped = len(unique) - len(selected_records)
    result = selected_records[:8]
    meta = {"target_price_unit": selected_unit, "dropped_mixed_unit_count": dropped}
    return (result, meta) if return_meta else result

def _target_anchor(records: list[dict], which: str) -> dict:
    if not records:
        return {}
    if which == "low":
        return records[0]
    if which == "high":
        return records[-1]
    return records[len(records) // 2]

def _format_target_anchor(rec: dict) -> str:
    return f"{rec['target']:g}{rec.get('unit', '')}[{rec['rn']}]"

def _build_valuation_section(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str) -> str:
    """Build valuation analysis from verifiable target prices and extracted assumptions."""
    targets_display = _collect_target_price_records(materials, ref_map, co, ticker, mkt)
    if not targets_display:
        return ""  # Fail closed: no verifiable targetPrice field

    lines = ["### 11.1 机构目标价汇总", ""]
    lines.append("| 机构 | 日期 | 评级 | 目标价 | 目标价口径 | 关键假设/关注点 |")
    lines.append("|:---|:---|:---|:---|:---|:---|")

    for rec in targets_display:
        lines.append(
            f"| {rec['org']} | {rec['date']} | {rec['rating']} | {rec['target']:g}{rec.get('unit','')}[{rec['rn']}] | "
            f"结构化目标价字段；方法未明示 | {rec['assumption']} |"
        )

    lines.append("")
    lines.append("### 11.2 目标价区间与市场隐含预期")
    lines.append("")
    if len(targets_display) == 1:
        only = targets_display[0]
        lines.append(
            f"当前仅取得一个可回溯机构目标价锚：{_format_target_anchor(only)}，"
            f"对应{only.get('assumption', '来源材料关注点')}。"
        )
    elif len(targets_display) == 2:
        low, high = _target_anchor(targets_display, "low"), _target_anchor(targets_display, "high")
        lines.append(
            f"当前取得两个可回溯目标价锚，区间为{_format_target_anchor(low)}至"
            f"{_format_target_anchor(high)}；低端对应{low.get('assumption', '来源材料关注点')}，"
            f"高端对应{high.get('assumption', '来源材料关注点')}。"
        )
    else:
        low, mid, high = _target_anchor(targets_display, "low"), _target_anchor(targets_display, "mid"), _target_anchor(targets_display, "high")
        lines.append(
            f"低目标价锚为{_format_target_anchor(low)}，对应{low.get('assumption', '来源材料关注点')}；"
            f"中位目标价锚为{_format_target_anchor(mid)}，对应{mid.get('assumption', '来源材料关注点')}；"
            f"高目标价锚为{_format_target_anchor(high)}，对应{high.get('assumption', '来源材料关注点')}。"
        )
    lines.append("")
    lines.append("### 11.3 估值分析")
    lines.append("")
    lines.append(
        "本节仅采用目标价字段可回溯到 articleId 的机构研报；如 11.1 表所示，目标价已逐机构绑定来源，"
        "估值方法缺失时只标注目标价口径，不硬猜PE、SOTP、DCF或EV/S方法。"
    )

    return "\n".join(lines)


def _build_scenario_section(materials: dict, ref_map: dict, co: str, mkt: str, ticker: str = "") -> str:
    """Build scenario analysis from verifiable target prices."""
    targets = _collect_target_price_records(materials, ref_map, co, ticker, mkt)
    if len(targets) < 3:
        return ""

    low, mid, high = _target_anchor(targets, "low"), _target_anchor(targets, "mid"), _target_anchor(targets, "high")
    rows = []
    for name, rec, risk in [
        ("乐观", high, "来源关注点未兑现"),
        ("中性", mid, "关键经营变量弱于机构假设"),
        ("悲观", low, "估值中枢下移"),
    ]:
        metrics = rec.get("metrics") or []
        if not metrics:
            return ""
        rows.append((name, rec, rec.get("assumption", "来源材料关注点"), "、".join(metrics), risk))

    lines = ["### 11.4 情景推演"]
    lines.append("")
    lines.append("情景推演以11.1中可回溯机构目标价为锚，不写无来源内部估值倍数。")
    lines.append("")

    lines.append("| 情景 | 目标价锚 | 核心假设 | 需要验证的变量 | 下修风险 |")
    lines.append("|:---|:---|:---|:---|:---|")
    for name, rec, assumption, variables, risk in rows:
        lines.append(f"| {name} | {_format_target_anchor(rec)} | {assumption}[{rec['rn']}] | {variables}[{rec['rn']}] | {risk} |")

    return "\n".join(lines)


def _find_ref_no(ref_map: dict, target_id: str) -> int:
    """Find reference number by source ID. Returns -1 if not found (never default to 1)."""
    for rn, v in ref_map.items():
        if str(v.get("id", "")) == str(target_id):
            return rn
    return -1  # MUST NOT default to ref [1] — was root cause of citation compression


# ── Reference renumbering ──

def normalize_used_references(report: str, source_trace: dict) -> tuple:
    """Keep only body-used refs, renumber [1][2][3]... sequentially."""
    body_end = report.find("## 13 参考资料")
    if body_end < 0: body_end = report.find("## 13 References")
    if body_end < 0: return (report, {}, ["missing_reference_section"])

    body = report[:body_end]
    all_sources = source_trace.get("sources", []) or []
    reference_issues = []
    raw_nums = [int(m) for m in re.findall(r'\[(\d+)\]', body)]
    used_nums = sorted(set(raw_nums))
    for n in used_nums:
        if n <= 0:
            reference_issues.append(f"invalid_reference_number:{n}")
        elif n > len(all_sources):
            reference_issues.append(f"reference_out_of_range:{n}>source_count:{len(all_sources)}")
        elif not all_sources[n - 1].get("id"):
            reference_issues.append(f"reference_missing_source_id:{n}")

    if not used_nums:
        return (report, {}, reference_issues)

    valid_old_nums = [n for n in used_nums if 0 < n <= len(all_sources) and all_sources[n - 1].get("id")]
    old_to_new = {old: new for new, old in enumerate(valid_old_nums, 1)}
    new_to_old = {new: old for old, new in old_to_new.items()}

    def renum_body(m):
        n = int(m.group(1))
        return f"[{old_to_new[n]}]" if n in old_to_new else f"[{n}]"
    body = re.sub(r'\[(\d+)\]', renum_body, body)

    new_ref_lines = ["## 13 参考资料\n"]
    new_ref_map = {}
    for new_n in sorted(new_to_old):
        old_n = new_to_old[new_n]
        s = all_sources[old_n - 1]
        line = _format_reference_line(new_n, s)
        new_ref_lines.append(line)
        new_ref_map[new_n] = {
            "id": s.get("id", ""),
            "date": s.get("publishTime", ""),
            "title": (s.get("title", "") or "")[:100],
            "line": line,
            "type": s.get("type", ""),
            "organization": s.get("organization", "--"),
            "api_nameEn": s.get("api_nameEn", ""),
            "api_id": s.get("api_id", "") or s.get("apiId", ""),
        }

    report = body + "\n" + "\n".join(new_ref_lines)
    return (report, new_ref_map, reference_issues)

def _validate_reference_integrity(report: str, source_trace: dict) -> list[str]:
    body_end = report.find("## 13 参考资料")
    body = report[:body_end] if body_end >= 0 else report
    all_sources = source_trace.get("sources", []) or []
    issues = []
    for n in sorted(set(int(m) for m in re.findall(r'\[(\d+)\]', body))):
        if n <= 0:
            issues.append(f"invalid_reference_number:{n}")
        elif n > len(all_sources):
            issues.append(f"reference_out_of_range:{n}>source_count:{len(all_sources)}")
        elif not all_sources[n - 1].get("id"):
            issues.append(f"reference_missing_source_id:{n}")
    return issues

def _post_repair_static_validation(report: str, source_trace: dict) -> list[str]:
    return _validate_final_hk_us_sections(report) + _validate_reference_integrity(report, source_trace)


_TITLE_FORBIDDEN_TERMS = (
    "近期研报", "持续关注", "主业韧性：", "深度分析", "投资价值分析",
    "核心业务增长", "股份有限公司",
)
_TITLE_BAD_ENDINGS = tuple("、：:的利业，,；;")


def _title_zh_len(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fff]', text or ""))


def _fallback_title_conclusion(market_cn: str) -> str:
    return ""


def _sanitize_title_conclusion(raw: str, company_name: str, ticker: str, market_cn: str) -> str:
    text = re.sub(r'\[\d+\]', '', raw or "")
    text = re.sub(r'[#*_`"“”‘’]', '', text)
    text = text.strip().strip("：:，,。.、；;")
    for term in _TITLE_FORBIDDEN_TERMS:
        text = text.replace(term, "")
    for suffix in ("股份有限公司", "有限公司", "集团控股有限公司", "控股有限公司"):
        text = text.replace(suffix, "")
    if company_name:
        text = text.replace(company_name, "")
    text = re.sub(r'\s+', '', text).strip("：:，,。.、；;")
    if not _valid_title_conclusion(text, company_name):
        return ""
    return text


def _valid_title_conclusion(text: str, company_name: str = "") -> bool:
    if not text:
        return False
    if any(term in text for term in _TITLE_FORBIDDEN_TERMS):
        return False
    if "：" in text or ":" in text:
        return False
    if text.endswith(_TITLE_BAD_ENDINGS):
        return False
    if company_name and company_name in text:
        return False
    zh_len = _title_zh_len(text)
    if zh_len < 10 or zh_len > 30:
        return False
    judgment_terms = ("驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量", "需求", "利润率")
    return any(term in text for term in judgment_terms)


def _build_report_title(company_name: str, ticker: str, market_cn: str, conclusion: str) -> str:
    safe_conclusion = _sanitize_title_conclusion(conclusion, company_name, ticker, market_cn)
    if not safe_conclusion:
        safe_conclusion = "标题生成失败"
    return f"# {company_name}（{ticker}）{market_cn}公司一页纸：{safe_conclusion}"


def _derive_title_conclusion(section12_text: str, company_name: str, market_cn: str, ticker: str = "") -> str:
    """Extract a 15-25 char investment conclusion from §1 content bullet points.

    Returns an empty string if no usable conclusion is found; callers must fail closed.
    """
    if not section12_text:
        return ""

    # Extract bullet points from §1 area (key takeaways, before §2 近况跟踪)
    sec1_end = section12_text.find("近况跟踪")
    sec1_text = section12_text[:sec1_end] if sec1_end > 0 else section12_text[:500]

    bullets = [x.strip() for x in re.findall(r'^[\*\-•]\s*(.+)$', sec1_text, re.M) if x.strip()]

    if bullets:
        # Take first bullet, strip bold markers, and condense to conclusion
        first = bullets[0].strip()
        first = re.sub(r'\[\d+\]', '', first).strip()
        first = re.sub(r'^\*\*[^*]+\*\*\s*[：:]\s*', '', first).strip()
        first = re.sub(r'\*\*', '', first).strip()
        # Remove leading/trailing punctuation
        first = first.strip('：:，,。.、')
        # Try to condense: take first clause before ：or，
        core = re.split(r'[：:，,。]', first)[0]
        # Remove common prefixes that aren't investment conclusions
        core = re.sub(r'^(?:FY\d{4}Q\d|CY\d{4}|FY\d{4})\s*', '', core)
        core = core.strip()
        if 10 <= len(core) <= 28:
            return _sanitize_title_conclusion(core, company_name, ticker, market_cn)
        elif len(core) > 28:
            return _sanitize_title_conclusion(core[:25], company_name, ticker, market_cn)
        # If too brief, try next bullet or use longer form
        if len(core) < 10 and len(first) >= 10:
            trimmed = first[:25].rstrip('，,。.')
            return _sanitize_title_conclusion(trimmed, company_name, ticker, market_cn)

    # Fallback: try to find a meaningful sentence
    sentences = re.split(r'[。；\n]', section12_text[:600])
    for s in sentences:
        s = re.sub(r'\*\*|#|\[|\]', '', s).strip()
        if 15 <= len(s) <= 28 and any(kw in s for kw in ['增长', '驱动', '估值', '修复', '超预期', '改善', '加速', '提升', '放量', '领先']):
            return _sanitize_title_conclusion(s[:25], company_name, ticker, market_cn)

    return ""


def _extract_price(materials: dict) -> str:
    st = materials.get("structured", {})
    if st:
        sn = st.get("snapshot", st.get("market_data", {}))
        if isinstance(sn, dict):
            p = sn.get("lastPrice", sn.get("price", sn.get("close", "")))
            m = sn.get("marketCap", sn.get("marketValue", ""))
            ps = []
            if p: ps.append(f"price {p}")
            if m: ps.append(f"mkt cap {m}")
            if ps: return ", ".join(ps)
    return "未取得实时价格/市值"


# ---------------------------------------------------------------------------
# pipeline steps
# ---------------------------------------------------------------------------

def _collect(ticker: str, market: str, co: str, out: str, token: str) -> dict:
    raw = Path(out) / "raw_retrieval_payloads"
    raw.mkdir(parents=True, exist_ok=True)
    mp = str(raw / "materials.json")
    cmd = [sys.executable, "-X", "utf8", str(_FETCH_MATERIALS), "--output", mp, "--market", market,
           "--materials-days", "365", "--materials-size", "15", "--max-reports", "20", "--max-meetings", "5"]
    if co: cmd.extend(["--company", co])
    if ticker: cmd.extend(["--ticker", ticker])
    env = os.environ.copy(); env["DATAYES_TOKEN"] = token
    print(f"[1/12] Collecting materials: {ticker or co} ({market})")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"fetch_materials failed (exit {r.returncode})")
    print(f"  OK: {mp}")
    return _load_json(mp)

def _build_trace(materials: dict, out: str) -> dict:
    trace = {"generated_at": TODAY_ISO, "version": "v1.2.4",
             "input_source_count": 0, "referenceable_source_count": 0,
             "refs_total": 0, "real_id_count": 0, "missing_id_count": 0,
             "duplicate_id_count": 0,
             "synthetic_id_count": 0, "real_id_coverage_pct": 0.0, "sources": []}
    seen_ids = set()
    for rd in materials.get("research", {}).get("details", []):
        sid = str(rd.get("articleId","") or "").strip()
        if not sid:
            trace["missing_id_count"] += 1
            continue
        if sid in seen_ids:
            trace["duplicate_id_count"] += 1
            continue
        seen_ids.add(sid)
        trace["sources"].append({"id": sid, "type": "Datayes Research",
            "title": (rd.get("articleTitle","") or "").strip(), "organization": rd.get("orgName","") or "--",
            "publishTime": str(rd.get("publishTimeReadable", rd.get("publishTime","")))[:10], "api_nameEn": "batchGetReportContent",
            "source_role": "target_research",
            "company_match": rd.get("company_match", "exact_target"),
            "id_field": "articleId", "id_value": sid})
    for s in materials.get("materials_v2", {}).get("unique_sources", []):
        sid = str(s.get("id","") or "").strip()
        if not sid:
            trace["missing_id_count"] += 1
            continue
        if sid in seen_ids:
            trace["duplicate_id_count"] += 1
            continue
        seen_ids.add(sid)
        trace["sources"].append({"id": sid, "type": s.get("type",""),
            "title": (s.get("title","") or "").strip(), "organization": s.get("organization","") or "--",
            "publishTime": str(s.get("publishTime",""))[:10], "api_nameEn": "getMaterialsV2",
            "source_role": s.get("source_role", "industry_background"),
            "peer_name": s.get("peer_name", ""),
            "peer_ticker": s.get("peer_ticker", ""),
            "peer_market": s.get("peer_market", ""),
            "comparable_business": s.get("comparable_business", ""),
            "discovery_article_ids": s.get("discovery_article_ids", []),
            "company_match": s.get("company_match", "industry_background"),
            "id_field": "id", "id_value": sid})
    hk_fin = materials.get("structured", {}).get("hk_financials", {}) or {}
    for api_name, meta in HK_PIT_API_SOURCES.items():
        rows = hk_fin.get(api_name, []) or []
        if not rows:
            continue
        first = rows[0] if isinstance(rows[0], dict) else {}
        ticker = _normalize_security_code(first.get("ticker") or first.get("secID") or materials.get("ticker") or "", "HK")
        api_id = str(meta.get("api_id") or "").strip()
        has_payload = any(isinstance(row, dict) and row for row in rows)
        if not api_id or not ticker or not has_payload:
            trace["missing_id_count"] += 1
            continue
        sid = f"structured:{api_name}:{api_id}:{ticker}"
        if sid in seen_ids:
            trace["duplicate_id_count"] += 1
            continue
        seen_ids.add(sid)
        trace["sources"].append({
            "id": sid,
            "type": "Datayes结构化接口",
            "title": meta["title"],
            "organization": "Datayes",
            "ticker": ticker,
            "secCode": ticker,
            "market": "HK",
            "publishTime": TODAY,
            "api_nameEn": api_name,
            "api_id": api_id,
            "api_url": meta["url"],
            "company_match": "structured_target",
            "source_role": "target_structured_financial",
            "id_field": "api_id+ticker",
            "id_value": f"{api_id}:{ticker}",
        })
    trace["referenceable_source_count"] = len(trace["sources"])
    trace["refs_total"] = trace["referenceable_source_count"]
    trace["synthetic_id_count"] = sum(1 for s in trace["sources"] if str(s.get("id", "")).startswith("synthetic:"))
    trace["real_id_count"] = sum(1 for s in trace["sources"] if s.get("id") and not str(s.get("id")).startswith("synthetic:"))
    trace["input_source_count"] = trace["real_id_count"] + trace["missing_id_count"] + trace["synthetic_id_count"]
    trace["real_id_coverage_pct"] = round(trace["real_id_count"] / trace["input_source_count"] * 100, 2) if trace["input_source_count"] else 0.0
    _save_json(str(Path(out)/"source_trace.json"), trace)
    print(f"[2/12] source_trace: {trace['refs_total']} sources")
    return trace

def _build_audit(trace: dict, out: str) -> dict:
    a = {"generated_at": TODAY_ISO, "refs_total": trace.get("refs_total",0),
         "input_source_count": trace.get("input_source_count", 0),
         "referenceable_source_count": trace.get("referenceable_source_count", trace.get("refs_total", 0)),
         "real_id_count": trace.get("real_id_count",0), "missing_id_count": trace.get("missing_id_count", 0),
         "duplicate_id_count": trace.get("duplicate_id_count", 0),
         "synthetic_id_count": trace.get("synthetic_id_count", 0), "real_id_coverage_pct": trace.get("real_id_coverage_pct",0.0),
         "all_ids": [s.get("id") for s in trace.get("sources",[]) if s.get("id")]}
    _save_json(str(Path(out)/"id_audit.json"), a)
    print(f"[3/12] id_audit: {a['real_id_count']} real, {a['missing_id_count']} missing, {a['synthetic_id_count']} synthetic")
    return a

def _run_repair(md: str, st: str, ia: str, out_md: str, mkt: str):
    # Post-repair is limited to mechanical format hygiene. It must not add
    # peer rows, valuation content, target prices, or other semantic section
    # content; missing source-backed content should remain blocked/degraded.
    print("[5/12] Running post-repair...")
    cmd = [sys.executable, "-X", "utf8", str(_POST_REPAIR), "--input-md", md,
           "--source-trace", st, "--id-audit", ia, "--output-md", out_md, "--market", mkt]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.stdout:
        for line in r.stdout.strip().split('\n'):
            if any(kw in line for kw in ['repair','warning','OK','FAIL','clear','remove','omit','delete']):
                print(f"    {line.strip()[:100]}")
    if r.stderr:
        stderr_summary = r.stderr.strip()[:200]
        if stderr_summary:
            print(f"    repair stderr: {stderr_summary}")
    if r.returncode != 0:
        return {"ok": False, "returncode": r.returncode, "output": out_md}
    out_path = Path(out_md)
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return {"ok": False, "returncode": r.returncode, "output": out_md}
    try:
        out_path.read_text(encoding="utf-8")
    except Exception:
        return {"ok": False, "returncode": r.returncode, "output": out_md}
    return {"ok": True, "returncode": r.returncode, "output": out_md}

def _run_checker(md: str, mkt: str, source_trace_path: str = "") -> dict:
    print("[6/12] Running quality checker...")
    cmd = [sys.executable, "-X", "utf8", str(_CHECKER), md, "--market", mkt.upper(), "--json"]
    if source_trace_path:
        cmd.extend(["--source-trace", source_trace_path])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    def failed_qc(reason: str) -> dict:
        return {
            "overall": "FAIL", "P0": 1, "P1": 0, "P2": 0,
            "issues": {"P0": [reason], "P1": [], "P2": []},
            "gates": [{
                "gate": "Checker Execution", "status": "FAIL", "P0": 1, "P1": 0, "P2": 0,
                "issues": [{"check_id": "CHECKER_EXECUTION_FAILED", "severity": "P0", "message": reason}],
            }],
        }
    if not r.stdout or not r.stdout.strip():
        qc = failed_qc("checker stdout is empty")
        print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
        return qc
    try:
        qc = json.loads(r.stdout.strip())
    except (json.JSONDecodeError, AttributeError) as exc:
        qc = failed_qc(f"checker JSON parse failed: {exc}")
        print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
        return qc
    if not isinstance(qc, dict):
        qc = failed_qc("checker JSON payload is not an object")
        print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
        return qc
    if r.returncode not in {0, 1, 2}:
        qc = failed_qc(f"checker returncode={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
        return qc
    print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
    if qc.get("P0",0) == 0 and qc.get("P1",0) == 0: print("  PASS")
    elif qc.get("P0",0) == 0: print(f"  CHECKER_ISSUES ({qc.get('P1',0)} P1)")
    else: print(f"  FAIL ({qc.get('P0',0)} P0)")
    return qc

def _append_qc_issue(qc: dict, check_id: str, severity: str, message: str):
    """Add writer-side blocking issues to checker JSON without losing gate detail."""
    sev_key = severity.upper()
    qc[sev_key] = int(qc.get(sev_key, 0) or 0) + 1
    if sev_key == "P0":
        qc["overall"] = "FAIL"
    elif qc.get("overall") != "FAIL":
        qc["overall"] = "WARN"

    gate = None
    for g in qc.setdefault("gates", []):
        if g.get("gate") == "Generation Status":
            gate = g
            break
    if gate is None:
        gate = {"gate": "Generation Status", "status": "PASS", "P0": 0, "P1": 0, "P2": 0, "issues": []}
        qc["gates"].append(gate)
    gate[sev_key] = int(gate.get(sev_key, 0) or 0) + 1
    gate["status"] = "FAIL" if gate.get("P0", 0) else "WARN"
    gate.setdefault("issues", []).append({
        "check_id": check_id,
        "severity": sev_key,
        "message": message,
        "old_check_ref": "RC-v124"
    })

def _run_docx(md: str, out_docx: str):
    print("[7/12] Generating DOCX...")
    cmd = [sys.executable, "-X", "utf8", str(_BUILD_DOCX), md, "--output", out_docx]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    out_path = Path(out_docx)
    ok = r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    if ok:
        print(f"  OK: {out_docx}")
    else:
        print(f"  DOCX failed: returncode={r.returncode}")
    return {"ok": ok, "returncode": r.returncode, "output": out_docx}


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------

def run(ticker: str, market: str, company: str, output_dir: str, llm_args: Any = None) -> dict:
    global _LLM_CONFIG, _LLM_DIAGNOSTICS
    os.makedirs(output_dir, exist_ok=True)
    run_started_at = datetime.now().isoformat(timespec="seconds")
    _update_run_manifest(output_dir, stage="startup", status="running", started_at=run_started_at)
    _startup_check()
    _LLM_CONFIG = resolve_llm_config(llm_args)
    _LLM_DIAGNOSTICS = _diagnostics_payload(_LLM_CONFIG)
    _write_llm_diagnostics(output_dir)
    print(
        "[0/12] LLM config: "
        f"format={_LLM_CONFIG.api_format or 'missing'} model={_LLM_CONFIG.model or 'missing'} "
        f"endpoint={_LLM_CONFIG.endpoint or 'missing'} credential_source={_LLM_CONFIG.source or 'missing'} "
        f"timeout={_LLM_CONFIG.timeout}"
    )
    if _LLM_CONFIG.error_type:
        _print_llm_config_error(_LLM_CONFIG)
        _finalize_failed_run(
            output_dir,
            stage="startup",
            error_type=_LLM_CONFIG.error_type,
            message=_LLM_CONFIG.error_message,
        )
        _write_llm_diagnostics(output_dir)
        raise RuntimeError(f"{_LLM_CONFIG.error_type}: {_LLM_CONFIG.error_message}")
    token = _find_token()
    if not token: raise RuntimeError("No DATAYES_TOKEN found")
    mkt = market.lower()
    t0 = time.time()

    stage_t0 = time.time()
    materials = _collect(ticker, mkt, company, output_dir, token)
    _update_run_manifest(output_dir, stage="collect_materials", duration_s=time.time() - stage_t0)
    peer_bundle = {}
    if build_peer_comparison_bundle and merge_peer_sources_into_materials:
        stage_t0 = time.time()
        peer_bundle = build_peer_comparison_bundle(
            materials, company, ticker, mkt.upper(), token=token, llm_call=_call_llm,
            output_dir=output_dir
        )
        materials = merge_peer_sources_into_materials(materials, peer_bundle)
        _update_run_manifest(output_dir, stage="peer_comparison", duration_s=time.time() - stage_t0)
    stage_t0 = time.time()
    trace = _build_trace(materials, output_dir)
    audit = _build_audit(trace, output_dir)
    _update_run_manifest(output_dir, stage="source_trace", duration_s=time.time() - stage_t0)

    mdp = str(Path(output_dir) / "report.md")
    gs = None
    stage_t0 = time.time()
    try:
        report, gs = write_report(materials, trace, ticker, mkt, company, output_dir, peer_bundle=peer_bundle)
    except Exception as e:
        print(f"  Generation error: {e}")
        Path(mdp).write_text(f"# {company}（{ticker}）生成失败\n\n生成阶段失败，未形成可发布报告。\n", encoding="utf-8")
        gs = {"is_skeleton": True, "is_degraded": True, "mode": "exception", "error": str(e),
              "ok_sections": 0, "total_sections": 12,
              "failed_sections": [str(i) for i in range(1, 13)]}
    _update_run_manifest(output_dir, stage="write_report", duration_s=time.time() - stage_t0)
    _write_llm_diagnostics(output_dir)

    is_skel = gs.get("is_skeleton", False) if gs else True
    is_deg = gs.get("is_degraded", False) if gs else True
    blocking = is_skel or is_deg

    stp = str(Path(output_dir) / "source_trace.json")
    iap = str(Path(output_dir) / "id_audit.json")
    if gs: _save_json(str(Path(output_dir) / "generation_status.json"), gs)
    repair_status = {"ok": False, "skipped": True}
    if gs.get("mode") != "exception":
        stage_t0 = time.time()
        repair_status = _run_repair(mdp, stp, iap, str(Path(output_dir) / "report_repaired.md"), mkt)
        _update_run_manifest(output_dir, stage="post_repair", duration_s=time.time() - stage_t0)
    rp2 = str(Path(output_dir) / "report_repaired.md")
    if repair_status.get("ok") and Path(rp2).exists():
        shutil.move(rp2, mdp)
        repaired_report = Path(mdp).read_text(encoding="utf-8")
        post_repair_issues = _post_repair_static_validation(repaired_report, trace)
        if post_repair_issues:
            gs["is_degraded"] = True
            gs["mode"] = "degraded"
            is_deg = True
            blocking = True
            gs.setdefault("assembly_issues", []).extend(f"post_repair:{x}" for x in post_repair_issues)
            gs["post_repair_validation_issues"] = post_repair_issues
    elif not repair_status.get("skipped"):
        gs.setdefault("pipeline_warnings", []).append(f"repair_failed:returncode={repair_status.get('returncode')}")

    stage_t0 = time.time()
    qc = _run_checker(mdp, mkt, stp)
    _update_run_manifest(output_dir, stage="checker", duration_s=time.time() - stage_t0)
    docx_status = {"ok": False, "skipped": True}
    can_build_docx = (
        qc.get("P0", 0) == 0 and qc.get("P1", 0) == 0
        and not is_skel and not is_deg
        and not gs.get("failed_sections") and not gs.get("assembly_issues")
    )
    if can_build_docx:
        stage_t0 = time.time()
        docx_status = _run_docx(mdp, str(Path(output_dir) / "report.docx"))
        _update_run_manifest(output_dir, stage="docx", duration_s=time.time() - stage_t0)
        if not docx_status.get("ok"):
            _append_qc_issue(qc, "DOCX_GENERATION_FAILED", "P0",
                             f"DOCX generation failed: returncode={docx_status.get('returncode')}")
    else:
        print("[7/12] Skipping DOCX because checker or generation_status is blocking.")
    if is_skel or is_deg or gs.get("failed_sections") or gs.get("assembly_issues"):
        _append_qc_issue(
            qc, "G0", "P0",
            f"generation_status阻断: mode={gs.get('mode','?')}, "
            f"failed_sections={gs.get('failed_sections', [])}, "
            f"assembly_issues={gs.get('assembly_issues', [])}"
        )
    # Force blocking when any quality issue exists (P0>0 or P1>0)
    if qc.get("P0", 0) > 0 or qc.get("P1", 0) > 0:
        blocking = True
    if blocking:
        reason_parts = []
        if is_skel or is_deg:
            reason_parts.append(f"generation_status={gs.get('mode','?') if gs else '?'}")
        if qc.get("P0", 0) > 0:
            reason_parts.append(f"P0={qc['P0']}")
        if qc.get("P1", 0) > 0:
            reason_parts.append(f"P1={qc['P1']}")
        qc["blocking"] = True
        qc["blocking_reason"] = "; ".join(reason_parts)
    _save_json(str(Path(output_dir) / "quality_check.json"), qc)
    gs["repair_status"] = repair_status
    gs["docx_status"] = docx_status
    if gs: _save_json(str(Path(output_dir) / "generation_status.json"), gs)
    _write_llm_diagnostics(output_dir)

    elapsed = time.time() - t0
    _update_run_manifest(output_dir, stage="complete", status="complete", duration_s=elapsed)
    mode = gs.get("mode", "?") if gs else "?"
    label = "SKELETON" if is_skel else ("DEGRADED" if is_deg else "FULL")
    print(f"\n{'='*60}")
    print(f"  Pipeline complete ({elapsed:.0f}s)  mode={label}")
    if blocking: print(f"  BLOCKED - cannot enter candidate validation")
    print(f"  out: {output_dir}")
    print(f"  P0={qc.get('P0','?')} P1={qc.get('P1','?')} P2={qc.get('P2','?')}")
    print(f"  refs={audit.get('refs_total','?')} real={audit.get('real_id_count','?')} coverage={audit.get('real_id_coverage_pct','?')}%")
    print(f"{'='*60}")
    return {"output_dir": output_dir, "blocking": blocking, "quality_check": qc,
            "id_audit": audit, "source_trace": trace, "generation_status": gs, "elapsed_s": elapsed}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="HK/US one-pager report writer v1.2.4")
    p.add_argument("--ticker", required=True)
    p.add_argument("--market", required=True, choices=["hk", "us", "HK", "US"])
    p.add_argument("--company-name", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--llm-api-key")
    p.add_argument("--llm-base-url")
    p.add_argument("--llm-model")
    p.add_argument("--llm-format", choices=["auto", "openai", "anthropic"])
    p.add_argument("--llm-timeout", type=int)
    args = p.parse_args()

    try:
        result = run(args.ticker, args.market.lower(), args.company_name, args.output_dir, llm_args=args)
    except Exception as exc:
        print(f"\nStartup failed: {str(exc)[:300]}")
        sys.exit(1)
    blocking = result.get("blocking", False)
    if blocking:
        print("\nCANNOT enter candidate validation: report is degraded/skeleton.")
        sys.exit(3)
    qc = result["quality_check"]
    sys.exit(1 if qc.get("P0", 0) > 0 or qc.get("P1", 0) > 0 else 0)

if __name__ == "__main__":
    main()
