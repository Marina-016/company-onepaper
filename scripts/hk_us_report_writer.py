#!/usr/bin/env python3
"""hk_us_report_writer.py - HK/US stock one-pager full generation pipeline.

Pipeline: collect -> source_trace -> id_audit -> section-wise LLM -> DOCX.
"""

from __future__ import annotations
import argparse, json, os, sys, subprocess, re, time, shutil, html
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from llm_adapter import (
    LLMConfig,
    call_llm as adapter_call_llm,
    config_for_diagnostics,
    resolve_llm_config,
)

_PEER_IMPORT_ERROR = ""
# peer_comparison_v124 模块已不再使用，§9 改由 LLM 直接从研报材料中读取同行
build_peer_comparison_bundle = None
build_peer_comparison_section = None
merge_peer_sources_into_materials = None

_SCRIPT_DIR = Path(__file__).resolve().parent
_FETCH_MATERIALS = _SCRIPT_DIR / "fetch_materials.py"
_BUILD_DOCX      = _SCRIPT_DIR / "build_docx.py"

TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_ISO = datetime.now().isoformat(timespec="seconds")
_LLM_CONFIG = LLMConfig()
_LLM_DIAGNOSTICS: dict[str, Any] = {"config": {}, "calls": []}
_LLM_DIAGNOSTIC_LOCK = threading.Lock()
_LLM_THREAD_LOCAL = threading.local()

HKUS_LLM_MAX_WORKERS_DEFAULT = 3
HKUS_LLM_RETRY_MAX_WORKERS_DEFAULT = 2
HKUS_LLM_TOTAL_BUDGET_SECONDS_DEFAULT = 600
HKUS_LLM_TASK_BUDGET_SECONDS_DEFAULT = 180
HKUS_LLM_SLOW_SECTION_TIMEOUT_SECONDS_DEFAULT = 90
TARGET_PRICE_BASIS_MAX_RECORDS = 5
TARGET_PRICE_BASIS_MAX_RETRIES = 2
_LLM_ACTIVE_COUNT = 0
_LLM_RETRY_ACTIVE_COUNT = 0
_LLM_OBSERVED_MAX_ACTIVE_TASKS = 0
_LLM_OBSERVED_MAX_RETRY_TASKS = 0


@dataclass
class LlmTaskResult:
    task_name: str
    ok: bool
    content: Any
    status: str
    elapsed_seconds: float
    attempts: int
    error_type: str = ""
    error_message: str = ""
    diagnostics: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ForecastSample:
    institution: str
    article_id: str
    metric: str
    forecast_year: str
    value: float
    unit: str
    accounting_basis: str
    source_id: str
    raw_value: str
    currency: str = ""
    period_basis: str = "FY"
    raw_unit: str = ""
    standardized_value: float = 0.0
    standardized_unit: str = ""
    conversion_formula: str = ""


@dataclass
class LlmTaskSpec:
    task_name: str
    runner: Callable[[], Any]

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
        str(p) for p in (_FETCH_MATERIALS, _BUILD_DOCX)
        if not p.exists()
    ]
    if missing:
        raise RuntimeError(f"required internal script missing: {missing}")

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


def _set_run_manifest_field(output_dir: str, key: str, value: Any) -> None:
    path = _manifest_path(output_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data[key] = value
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
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


def _clean_repeated_punctuation(text: str) -> str:
    """Clean repetitive Chinese punctuation patterns from rendered text."""
    text = text.replace("。；", "；")
    text = text.replace("；。", "。")
    text = text.replace("。。", "。")
    text = text.replace("；；", "；")
    text = re.sub(r'[。；]{2,}', lambda m: m.group(0)[0], text)
    return text


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

_LLM_LIGHT_RETRY_ERRORS = {"EMPTY_TEXT_RESPONSE", "NETWORK_TEMPORARY"}


def _compact_prompt_for_light_retry(prompt: str, max_chars: int = 5200) -> str:
    prompt = str(prompt or "")
    if len(prompt) <= max_chars:
        return prompt
    head = prompt[:3200].rstrip()
    tail = prompt[-1800:].lstrip()
    return (
        head
        + "\n\n---\n"
        + "Context truncated for lightweight retry; preserve the schema/rules above and use only cited evidence below.\n"
        + "---\n\n"
        + tail
    )


def _record_llm_event(event: dict) -> None:
    local_events = getattr(_LLM_THREAD_LOCAL, "events", None)
    if isinstance(local_events, list):
        local_events.append(event)
    else:
        with _LLM_DIAGNOSTIC_LOCK:
            _LLM_DIAGNOSTICS.setdefault("calls", []).append(event)


def _llm_call_event(result: Any, prompt: str, call_name: str,
                    request_started_at: float, request_finished_at: float,
                    retry_mode: str = "") -> dict:
    event = {
        "call_name": call_name,
        "ok": result.ok,
        "latency_s": result.latency_s,
        "attempt_count": result.attempt_count,
        "response_length": len(result.text or ""),
        "prompt_size_chars": len(prompt or ""),
        "material_size_chars": len(prompt or ""),
        "error_type": result.error_type,
        "error_message": (result.error_message or "")[:500],
        "http_status": result.http_status,
        "model": result.model,
        "api_format": result.api_format,
        "endpoint": result.endpoint,
        "request_started_at": request_started_at,
        "request_finished_at": request_finished_at,
        "preview": _plain_text(result.text, 200) if result.text else "",
    }
    if retry_mode:
        event["retry_mode"] = retry_mode
    return event


def _call_llm(prompt: str, max_tokens: int = 8000, timeout: int = 180, system: str = "",
              call_name: str = "llm_call", max_attempts: int = 2) -> tuple:
    """Call the unified adapter and record structured diagnostics."""
    request_started_at = time.time()
    result = adapter_call_llm(prompt, system=system, max_tokens=max_tokens, timeout=timeout,
                              max_attempts=max_attempts, config=_LLM_CONFIG)
    request_finished_at = time.time()
    _record_llm_event(_llm_call_event(result, prompt, call_name, request_started_at, request_finished_at))
    if (
        not result.ok
        and result.error_type in _LLM_LIGHT_RETRY_ERRORS
        and _bounded_env_int("HKUS_LLM_LIGHT_RETRY", 1, 0, 1) == 1
    ):
        retry_prompt = _compact_prompt_for_light_retry(prompt)
        retry_tokens = max(600, int(max_tokens * 0.6))
        retry_timeout = min(int(timeout or 60), 60)
        retry_started_at = time.time()
        retry_result = adapter_call_llm(retry_prompt, system=system, max_tokens=retry_tokens,
                                        timeout=retry_timeout, max_attempts=1, config=_LLM_CONFIG)
        retry_finished_at = time.time()
        _record_llm_event(_llm_call_event(
            retry_result, retry_prompt, f"{call_name}:light_retry",
            retry_started_at, retry_finished_at, retry_mode="compact_prompt",
        ))
        if retry_result.ok:
            _append_llm_issue("llm_light_retry", f"{call_name}:ok_light_retry_after_{result.error_type}")
            return (retry_result.text, True)
        _append_llm_issue("llm_light_retry", f"{call_name}:failed_light_retry_after_{result.error_type}:{retry_result.error_type}")
    return (result.text, result.ok)


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


def _hkus_llm_max_workers() -> int:
    return _bounded_env_int("HKUS_LLM_MAX_WORKERS", HKUS_LLM_MAX_WORKERS_DEFAULT, 1, 4)


def _hkus_llm_retry_workers() -> int:
    return _bounded_env_int("HKUS_LLM_RETRY_MAX_WORKERS", HKUS_LLM_RETRY_MAX_WORKERS_DEFAULT, 1, 3)


def _hkus_llm_total_budget_seconds() -> int:
    return _bounded_env_int("HKUS_LLM_TOTAL_BUDGET_SECONDS", HKUS_LLM_TOTAL_BUDGET_SECONDS_DEFAULT, 180, 1200)


def _hkus_llm_task_budget_seconds() -> int:
    return _bounded_env_int("HKUS_LLM_TASK_BUDGET_SECONDS", HKUS_LLM_TASK_BUDGET_SECONDS_DEFAULT, 60, 300)


def _hkus_llm_slow_section_timeout_seconds() -> int:
    return _bounded_env_int("HKUS_LLM_SLOW_SECTION_TIMEOUT_SECONDS", HKUS_LLM_SLOW_SECTION_TIMEOUT_SECONDS_DEFAULT, 45, 120)


def _execute_llm_task(spec: LlmTaskSpec) -> LlmTaskResult:
    global _LLM_ACTIVE_COUNT, _LLM_RETRY_ACTIVE_COUNT, _LLM_OBSERVED_MAX_ACTIVE_TASKS, _LLM_OBSERVED_MAX_RETRY_TASKS
    submitted_at = getattr(spec, "submitted_at", None) or time.time()
    worker_started_at = time.time()
    is_retry = spec.task_name.startswith("target_price_basis_retry:")
    with _LLM_DIAGNOSTIC_LOCK:
        if is_retry:
            _LLM_RETRY_ACTIVE_COUNT += 1
            _LLM_OBSERVED_MAX_RETRY_TASKS = max(_LLM_OBSERVED_MAX_RETRY_TASKS, _LLM_RETRY_ACTIVE_COUNT)
        else:
            _LLM_ACTIVE_COUNT += 1
            _LLM_OBSERVED_MAX_ACTIVE_TASKS = max(_LLM_OBSERVED_MAX_ACTIVE_TASKS, _LLM_ACTIVE_COUNT)
    start = worker_started_at
    _LLM_THREAD_LOCAL.events = []
    try:
        content = spec.runner()
        diagnostics = list(getattr(_LLM_THREAD_LOCAL, "events", []) or [])
        attempts = sum(int(d.get("attempt_count") or 0) for d in diagnostics) or len(diagnostics)
        ok = True
        status = "ok"
        error_type = ""
        error_message = ""
        if isinstance(content, dict):
            ok = bool(content.get("ok", True))
            status = str(content.get("status") or ("ok" if ok else "failed"))
            error_type = str(content.get("error_type") or "")
            error_message = str(content.get("error_message") or "")
        return LlmTaskResult(
            task_name=spec.task_name,
            ok=ok,
            content=content,
            status=status,
            elapsed_seconds=time.time() - start,
            attempts=attempts,
            error_type=error_type,
            error_message=error_message,
            diagnostics=diagnostics,
            metadata={
                "submitted_at": submitted_at,
                "worker_started_at": worker_started_at,
                "task_finished_at": time.time(),
                "queue_wait_seconds": max(0.0, worker_started_at - submitted_at),
            },
        )
    except Exception as exc:
        diagnostics = list(getattr(_LLM_THREAD_LOCAL, "events", []) or [])
        return LlmTaskResult(
            task_name=spec.task_name,
            ok=False,
            content=None,
            status="exception",
            elapsed_seconds=time.time() - start,
            attempts=sum(int(d.get("attempt_count") or 0) for d in diagnostics) or len(diagnostics),
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
            diagnostics=diagnostics,
            metadata={
                "submitted_at": submitted_at,
                "worker_started_at": worker_started_at,
                "task_finished_at": time.time(),
                "queue_wait_seconds": max(0.0, worker_started_at - submitted_at),
            },
        )
    finally:
        _LLM_THREAD_LOCAL.events = None
        with _LLM_DIAGNOSTIC_LOCK:
            if is_retry:
                _LLM_RETRY_ACTIVE_COUNT = max(0, _LLM_RETRY_ACTIVE_COUNT - 1)
            else:
                _LLM_ACTIVE_COUNT = max(0, _LLM_ACTIVE_COUNT - 1)


def _append_task_diagnostics(results: list[LlmTaskResult]) -> None:
    events = []
    for result in results:
        for event in result.diagnostics:
            item = dict(event)
            item.setdefault("task_name", result.task_name)
            events.append(item)
    if events:
        with _LLM_DIAGNOSTIC_LOCK:
            _LLM_DIAGNOSTICS.setdefault("calls", []).extend(events)


def _append_llm_issue(key: str, message: str) -> None:
    with _LLM_DIAGNOSTIC_LOCK:
        _LLM_DIAGNOSTICS.setdefault(key, []).append(message)


def run_hkus_llm_tasks(task_specs: list[LlmTaskSpec], max_workers: int | None = None,
                       total_budget_seconds: int | None = None, progress_prefix: str = "[LLM]") -> tuple[list[LlmTaskResult], dict]:
    """Run independent LLM tasks with bounded parallelism and main-thread merging."""
    if not task_specs:
        return [], {"budget_exceeded": False, "elapsed_seconds": 0.0}
    workers = max(1, min(max_workers or _hkus_llm_max_workers(), len(task_specs)))
    budget = total_budget_seconds if total_budget_seconds is not None else _hkus_llm_total_budget_seconds()
    start = time.time()
    results: list[LlmTaskResult] = []
    budget_exceeded = False
    print(f"{progress_prefix} config workers={workers}, tasks={len(task_specs)}, budget={budget}s", flush=True)
    executor = ThreadPoolExecutor(max_workers=workers)
    future_to_name = {}
    try:
        for idx, spec in enumerate(task_specs, 1):
            setattr(spec, "submitted_at", time.time())
            print(f"{progress_prefix} {idx}/{len(task_specs)} {spec.task_name} started", flush=True)
            future_to_name[executor.submit(_execute_llm_task, spec)] = spec.task_name
        try:
            for future in as_completed(future_to_name, timeout=budget):
                name = future_to_name[future]
                result = future.result()
                results.append(result)
                state = "DONE" if result.ok else "FAIL"
                print(f"{progress_prefix} {state} {len(results)}/{len(task_specs)} {name} {result.status}, {result.elapsed_seconds:.1f}s", flush=True)
        except FuturesTimeoutError:
            budget_exceeded = True
            for future, name in future_to_name.items():
                if not future.done():
                    future.cancel()
                    results.append(LlmTaskResult(
                        task_name=name,
                        ok=False,
                        content=None,
                        status="budget_exceeded",
                        elapsed_seconds=time.time() - start,
                        attempts=0,
                        error_type="budget_exceeded",
                        error_message=f"LLM phase exceeded {budget}s",
                        metadata={"budget_exceeded": True},
                    ))
                    print(f"{progress_prefix} FAIL {len(results)}/{len(task_specs)} {name} budget_exceeded", flush=True)
    finally:
        executor.shutdown(wait=not budget_exceeded, cancel_futures=True)
    _append_task_diagnostics(results)
    return results, {
        "budget_exceeded": budget_exceeded,
        "elapsed_seconds": time.time() - start,
        "max_workers": workers,
        "observed_max_active_tasks": _LLM_OBSERVED_MAX_ACTIVE_TASKS,
        "observed_max_retry_tasks": _LLM_OBSERVED_MAX_RETRY_TASKS,
    }


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

def _segment_context_hit(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r'分业务|分版块|分板块|业务板块|增值服务|营销服务|在线广告|金融科技|企业服务|云业务|社交网络', text)
        and re.search(r'收入|营收|占比|毛利率|同比|FY20\d{2}|20\d{2}年', text)
    )

def _extract_annual_segment_context(materials: dict, ref_map: dict, co: str, ticker: str) -> tuple[str, list[int]]:
    """Collect source-backed annual/segment facts for §5.2.

    §5 previously received only the latest target research abstracts. For companies
    where annual segment data sits in older Materials V2 snippets, that pushed the
    LLM into quarterly KPI fallback. This context is intentionally narrow: only
    target-matched snippets with segment/business keywords and numeric facts.
    """
    lines: list[str] = []
    years: set[int] = set()
    latest_completed_year = int(TODAY[:4]) - 1
    seen: set[tuple[int, str]] = set()

    def add_line(ref_no: int, title: str, text: str):
        if ref_no <= 0:
            return
        clean = _plain_text(text, 700)
        if not _segment_context_hit(clean):
            return
        key = (ref_no, clean[:80])
        if key in seen:
            return
        seen.add(key)
        for y in re.findall(r'(?:FY)?(20\d{2})', clean):
            try:
                yi = int(y)
            except ValueError:
                continue
            # Treat already completed years as annual candidates; forward years
            # remain usable evidence but do not count toward required actual FYs.
            if yi <= latest_completed_year:
                years.add(yi)
        lines.append(f"[{ref_no}] {_plain_text(title, 80)}：{clean[:520]}")

    for rd in materials.get("research", {}).get("details", []) or []:
        if not _source_matches_target(rd, co, ticker):
            continue
        ref_no = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        text = "。".join(str(rd.get(k, "") or "") for k in ("textAbstract", "summary", "articleTitle", "title"))
        add_line(ref_no, rd.get("articleTitle") or rd.get("title") or "", text)

    for q in materials.get("materials_v2", {}).get("queries", []) or []:
        for item in q.get("data", []) or []:
            if item.get("company_match") not in ("exact_target", "target", "matched", ""):
                continue
            ref_no = _find_ref_no(ref_map, str(item.get("id", "")))
            text = "。".join(str(item.get(k, "") or "") for k in ("text", "insight", "summary", "title"))
            add_line(ref_no, item.get("title", ""), text)

    def line_score(line: str) -> tuple[int, int]:
        score = 0
        line_years = []
        for y in re.findall(r'(?:FY)?(20\d{2})', line):
            try:
                line_years.append(int(y))
            except ValueError:
                continue
        if re.search(r'年报|年度|全年|FY20\d{2}|20\d{2}年', line):
            score += 5
        if re.search(r'收入占比|占比|分部|分业务|业务板块|segment|Segment', line):
            score += 4
        if len(set(line_years)) >= 2:
            score += 2
        if re.search(r'Q[1-4]|季度|一季度|二季度|三季度|四季度|前瞻|预测|预计|26Q|2026Q', line, re.I):
            score -= 4
        return score, max(line_years) if line_years else 0

    lines.sort(key=line_score, reverse=True)
    return "\n".join(lines[:10]), sorted(years, reverse=True)[:3]

def _recent_consecutive_years(years: list[int]) -> list[int]:
    ordered = sorted({int(y) for y in years if y}, reverse=True)
    if not ordered:
        return []
    keep = [ordered[0]]
    expected = ordered[0] - 1
    for year in ordered[1:]:
        if year == expected:
            keep.append(year)
            expected -= 1
        elif year < expected:
            break
    return keep

def _extract_peer_context(materials: dict, ref_map: dict, company_name: str, ticker: str) -> str:
    lines: list[str] = []
    seen: set[tuple[int, str]] = set()
    peer_signal = re.compile(
        r'同业|可比|竞争|竞品|市场份额|提及的公司|peer|comparable|competitor|'
        r'competition|rival|versus|vs\.?|market share|benchmark',
        re.I,
    )
    target_aliases, ticker_aliases = _target_aliases(company_name, ticker)
    target_terms = {str(x).lower() for x in target_aliases | ticker_aliases if x}
    for q in materials.get("materials_v2", {}).get("queries", []) or []:
        if q.get("topic") not in ("valuation_consensus", "peer_discovery", "market_focus", "business_financials"):
            continue
        for item in q.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            text = _plain_text("。".join(str(item.get(k, "") or "") for k in ("text", "insight", "summary", "title")), 900)
            if not text or not peer_signal.search(text):
                continue
            lowered = text.lower()
            if target_terms and not any(term and term in lowered for term in target_terms):
                # Peer evidence may come from a peer-specific report, but the
                # retrieval snippet should still mention why it is comparable
                # to the target. Otherwise it is too easy to import unrelated
                # industry material.
                continue
            ref_no = _find_ref_no(ref_map, str(item.get("id", "")))
            if ref_no <= 0:
                continue
            key = (ref_no, text[:80])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"[{ref_no}] {_plain_text(item.get('title',''), 80)}：{text[:520]}")
            if len(lines) >= 8:
                return "\n".join(lines)
    return "\n".join(lines)

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
    annual_segment_text, annual_segment_years = _extract_annual_segment_context(materials, ref_map, co, ticker)
    annual_segment_years = _recent_consecutive_years(annual_segment_years)
    annual_segment_preferred_years = annual_segment_years[:3] if len(annual_segment_years) >= 3 else annual_segment_years[:1]
    annual_segment_preferred_periods = [f"FY{y}" for y in annual_segment_preferred_years]
    peer_context_text = _extract_peer_context(materials, ref_map, co, ticker)
    return {
        "company_name": co,
        "ticker": ticker,
        "market": mkt,
        "target_matched_reports": target_reports,
        "target_prices": target_prices[:10],
        "recent_reports_text": recent_reports_text,
        "annual_segment_text": annual_segment_text,
        "annual_segment_years": annual_segment_years,
        "annual_segment_preferred_periods": annual_segment_preferred_periods,
        "annual_segment_required_periods": len(annual_segment_preferred_periods),
        "peer_context_text": peer_context_text,
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
    if key_data.get("annual_segment_text"):
        years = ", ".join(f"FY{y}" for y in key_data.get("annual_segment_years", []) or [])
        preferred = ", ".join(str(x) for x in key_data.get("annual_segment_preferred_periods", []) or [])
        suffix = f" Candidate annual periods found: {years}. Use exactly these periods for §5.2: {preferred}." if preferred else f" Candidate annual periods found: {years}." if years else ""
        lines.extend([
            "Annual / segment business evidence for §5.2:",
            str(key_data.get("annual_segment_text") or ""),
            suffix.strip(),
        ])
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

def _extract_json_parse_issue(error: str) -> str:
    msg = _plain_text(error, 180)
    return msg or "json_parse_error"

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

def _cite(refs: list[int]) -> str:
    return "".join(f"[{r}]" for r in refs)

def _clean_source_fragment(text: str, limit: int = 0) -> str:
    """Clean copied source snippets before putting them into report cells."""
    clean = _plain_text(text, limit)
    clean = re.sub(r'^[\s\uf06e\uf06c•●○◆◇▪▫➢>》]+', '', clean)
    clean = re.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩一二三四五六七八九十]+[、.)）]\s*', '', clean)
    prefix_re = re.compile(
        r'^(核心观点|核心财务数据前瞻预期|主要观点|简评|摘要|要点|投资要点|正文|点评|事件)[:：.\s-]*'
    )
    for _ in range(3):
        new_clean = prefix_re.sub('', clean).strip()
        if new_clean == clean:
            break
        clean = new_clean
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def normalize_refs(text: str, refs: list[int]) -> str:
    """Put merged, de-duplicated refs at the end of a single cell."""
    base = _clean_source_fragment(re.sub(r'\[\d+\]', '', str(text or ""))).strip()
    merged: list[int] = []
    for n in re.findall(r'\[(\d+)\]', str(text or "")):
        v = int(n)
        if v not in merged:
            merged.append(v)
    for ref in refs or []:
        try:
            v = int(ref)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in merged:
            merged.append(v)
    return f"{base}{_cite(merged)}" if merged else base

def _section_1_2_json_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""Return ONLY JSON for HK/US company one-pager sections 1 and 2.
Schema:
{{"title_conclusion": "...", "section_1": {{"key_points": [{{"keyword": "...", "text": "...", "source_ids": [1]}}]}}, "section_2": {{"recent_updates": [{{"keyword": "...", "date": "YYYY-MM-DD or YYYY-Qx", "fact": "...", "implication": "...", "source_ids": [1]}}]}}}}
Rules: section_1 has 4-6 investment conclusions; section_2 has 3-5 recent concrete updates and stays within 400 Chinese characters; every item cites valid source_ids; no Markdown.
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _validate_render_sections_1_2(payload: dict, ref_map: dict, company_name: str = "", ticker: str = "") -> tuple[dict[str, str], list[str]]:
    issues: list[str] = []
    title_conclusion = _sanitize_title_conclusion(str(payload.get("title_conclusion") or ""), company_name, ticker, "")
    if not title_conclusion:
        issues.append("title_conclusion.invalid_or_missing")
    s1 = payload.get("section_1") if isinstance(payload.get("section_1"), dict) else {}
    points = s1.get("key_points") if isinstance(s1.get("key_points"), list) else []
    s1_issue_start = len(issues)
    if not (4 <= len(points) <= 6):
        issues.append(f"section_1.key_points_count:{len(points)}")
    lines = ["## 1 关键要点", ""]
    valid_points = 0
    for i, item in enumerate(points[:6]):
        if not isinstance(item, dict):
            issues.append(f"section_1.key_points[{i}].not_object")
            continue
        text = _text_ok(item.get("text") or item.get("statement"), issues, f"section_1.key_points[{i}].text", 12)
        refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, issues, f"section_1.key_points[{i}]")
        keyword = _plain_text(item.get("keyword") or re.split(r'[，；。:：]', text)[0], 18)
        if text and refs:
            lines.append(f"- **{keyword or '投资判断'}**：{text}{_cite(refs)}")
            valid_points += 1
    section_1_valid = 4 <= valid_points <= 6 and len(issues) == s1_issue_start
    s2 = payload.get("section_2") if isinstance(payload.get("section_2"), dict) else {}
    updates = s2.get("recent_updates") if isinstance(s2.get("recent_updates"), list) else []
    s2_issue_start = len(issues)
    if not (3 <= len(updates) <= 5):
        issues.append(f"section_2.recent_updates_count:{len(updates)}")
    lines2 = ["## 2 近况跟踪", ""]
    valid_updates = 0
    for i, item in enumerate(updates[:5]):
        if not isinstance(item, dict):
            issues.append(f"section_2.recent_updates[{i}].not_object")
            continue
        keyword = _plain_text(item.get("keyword") or item.get("date") or "", 18)
        fact = _text_ok(item.get("fact") or item.get("text"), issues, f"section_2.recent_updates[{i}].fact", 10)
        implication = _text_ok(item.get("implication") or "", issues, f"section_2.recent_updates[{i}].implication", 6)
        refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, issues, f"section_2.recent_updates[{i}]")
        if keyword and fact and implication and refs:
            lines2.append(f"- **{keyword}**：{fact}；{implication}{_cite(refs)}")
            valid_updates += 1
    section_2_valid = 3 <= valid_updates <= 5 and len(issues) == s2_issue_start
    rendered: dict[str, str] = {"_title_conclusion": title_conclusion}
    parts = []
    if section_1_valid:
        parts.append("\n".join(lines))
    if section_2_valid:
        parts.append("\n".join(lines2))
    if parts:
        rendered["s12"] = "\n\n".join(parts)
    rendered["_section_1_valid"] = "1" if section_1_valid else ""
    rendered["_section_2_valid"] = "1" if section_2_valid else ""
    return (rendered, issues)


def gen_hkus_sections_1_2(key_data: dict, ref_map: dict | None = None) -> tuple[dict[str, str], bool, dict]:
    if not _has_target_materials(key_data):
        return {}, False, {"call_mode": "skipped_no_target_materials", "schema_issues": ["no_target_materials"]}
    ref_map = ref_map or {}
    issues: list[str] = []
    parse_attempts = 0
    for call_name in ("sections_1_2", "sections_1_2_json_repair"):
        prompt = _section_1_2_json_prompt(key_data, issues if call_name.endswith("repair") else None)
        text, ok = _call_llm(prompt, max_tokens=4500, timeout=min(120, _hkus_llm_task_budget_seconds()), system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        parse_attempts += 1
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_sections_1_2(payload, ref_map)
        section_1_valid = bool(rendered.get("_section_1_valid"))
        section_2_valid = bool(rendered.get("_section_2_valid"))
        if rendered.get("s12") and (section_1_valid or section_2_valid):
            call_mode = "ok_json" if section_1_valid and section_2_valid and not render_issues else "partial_json"
            return rendered, True, {
                "call_mode": call_mode,
                "parse_attempts": parse_attempts,
                "schema_issues": render_issues[:40],
                "section_1_valid": section_1_valid,
                "section_2_valid": section_2_valid,
                "title_valid": bool(rendered.get("_title_conclusion")),
            }
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    return {}, False, {"call_mode": "failed_schema", "parse_attempts": parse_attempts, "schema_issues": issues[:40]}


def _section_3_json_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""Return ONLY JSON for HK/US company one-pager section 3.
Schema:
{{"short_term_logic": [{{"title": "...", "text": "...", "source_ids": [1]}}], "long_term_logic": [{{"title": "...", "text": "...", "source_ids": [1]}}]}}
Rules: each list has 2-3 source-backed rows; do not output catalysts, tables, extra tracking fields, or a 3.3 subsection.
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _section_3_group_prompt(key_data: dict, group_key: str, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    group_name = "short-term investment logic for §3.1" if group_key == "short_term_logic" else "long-term investment logic for §3.2"
    return f"""Return ONLY JSON for HK/US company one-pager section 3 subtask: {group_name}.
Schema:
{{"rows": [{{"title": "...", "text": "...", "source_ids": [1]}}]}}
Rules: produce 2-3 source-backed rows; each text must explain one investment mechanism, not a catalyst list; no Markdown.
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _normalize_section_3_group_rows(payload: dict, ref_map: dict, group_key: str) -> tuple[list[dict], list[str]]:
    issues: list[str] = []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get(group_key) if isinstance(payload, dict) and isinstance(payload.get(group_key), list) else []
    if not (2 <= len(rows) <= 3):
        issues.append(f"section_3.{group_key}_count:{len(rows)}")
    clean_rows: list[dict] = []
    for i, item in enumerate(rows[:3]):
        if not isinstance(item, dict):
            issues.append(f"section_3.{group_key}[{i}].not_object")
            continue
        title = _plain_text(item.get("title") or "", 24)
        text = _text_ok(item.get("text") or item.get("mechanism"), issues, f"section_3.{group_key}[{i}].text", 12)
        refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, issues, f"section_3.{group_key}[{i}]")
        if text and refs:
            clean_rows.append({"title": title, "text": text, "refs": refs})
    if len(clean_rows) < 2:
        issues.append(f"section_3.{group_key}_valid_rows:{len(clean_rows)}<2")
    return clean_rows, issues


def _render_section_3_groups(short_rows: list[dict], long_rows: list[dict]) -> str:
    lines = ["## 3 核心投资逻辑", ""]
    for heading, rows in (("### 3.1 短期逻辑", short_rows), ("### 3.2 长期逻辑", long_rows)):
        if not rows:
            continue
        lines.extend([heading, ""])
        for item in rows[:3]:
            prefix = f"**{item.get('title', '')}**：" if item.get("title") else ""
            lines.append(f"- {prefix}{item['text']}{_cite(item['refs'])}")
        lines.append("")
    return "\n".join(lines).strip()


def _validate_render_section_3(payload: dict, ref_map: dict) -> tuple[str, list[str]]:
    issues: list[str] = []
    groups = [
        ("short_term_logic", "### 3.1 短期逻辑"),
        ("long_term_logic", "### 3.2 长期逻辑"),
    ]
    lines = ["## 3 核心投资逻辑", ""]
    for key, heading in groups:
        rows = payload.get(key) if isinstance(payload.get(key), list) else []
        if not (2 <= len(rows) <= 3):
            issues.append(f"section_3.{key}_count:{len(rows)}")
        lines.extend([heading, ""])
        for i, item in enumerate(rows[:3]):
            if not isinstance(item, dict):
                issues.append(f"section_3.{key}[{i}].not_object")
                continue
            title = _plain_text(item.get("title") or "", 24)
            text = _text_ok(item.get("text") or item.get("mechanism"), issues, f"section_3.{key}[{i}].text", 12)
            refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, issues, f"section_3.{key}[{i}]")
            if text and refs:
                prefix = f"**{title}**：" if title else ""
                lines.append(f"- {prefix}{text}{_cite(refs)}")
        lines.append("")
    return ("\n".join(lines).strip(), issues)


def gen_hkus_section_3(key_data: dict, ref_map: dict | None = None) -> tuple[str, bool, list[str]]:
    if not _has_target_materials(key_data):
        return "", False, ["no_target_materials"]
    ref_map = ref_map or {}
    issues: list[str] = []
    for call_name in ("section_3", "section_3_json_repair"):
        text, ok = _call_llm(_section_3_json_prompt(key_data, issues if call_name.endswith("repair") else None),
                             max_tokens=3500, timeout=min(120, _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_3(payload, ref_map)
        if rendered and not render_issues:
            return rendered, True, []
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    return "", False, issues[:40]


def gen_hkus_section_3_group(key_data: dict, ref_map: dict, group_key: str) -> tuple[list[dict], bool, list[str]]:
    if not _has_target_materials(key_data):
        return [], False, ["no_target_materials"]
    issues: list[str] = []
    for call_name in (f"section_3_{group_key}",):
        text, ok = _call_llm(_section_3_group_prompt(key_data, group_key, issues),
                             max_tokens=1800, timeout=min(90, _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name,
                             max_attempts=1)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rows, row_issues = _normalize_section_3_group_rows(payload, ref_map, group_key)
        if len(rows) >= 2:
            return rows, True, row_issues
        issues.extend(f"{call_name}:{x}" for x in row_issues)
    return [], False, issues[:30]


def merge_hkus_section_3_groups(short_rows: list[dict], long_rows: list[dict]) -> tuple[str, bool, list[str]]:
    issues: list[str] = []
    if len(short_rows) < 2:
        issues.append(f"section_3.short_term_logic_valid_rows:{len(short_rows)}<2")
    if len(long_rows) < 2:
        issues.append(f"section_3.long_term_logic_valid_rows:{len(long_rows)}<2")
    if len(short_rows) < 2 and len(long_rows) < 2:
        return "", False, issues
    rendered = _render_section_3_groups(short_rows if len(short_rows) >= 2 else [], long_rows if len(long_rows) >= 2 else [])
    if len(short_rows) < 2 or len(long_rows) < 2:
        issues.append("section_3.partial_groups")
    return rendered, True, issues


def _section_4_json_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""Return ONLY JSON for HK/US company one-pager section 4 catalysts.
Schema: {{"catalysts": [{{"time": "YYYY-MM or YYYY-Qx", "event": "...", "impact": "...", "source_ids": [1]}}]}}
Rules:
- 4-7 concrete company/industry events; include both recent completed validation events and forward-looking catalysts when source-backed.
- Prefer 2-3 past/recent events that validate the thesis plus 2-4 upcoming events or expected milestones.
- event and impact must be source-backed; no broker report/target-price updates as catalysts.
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _validate_render_section_4(payload: dict, ref_map: dict) -> tuple[str, list[str]]:
    issues: list[str] = []
    rows = payload.get("catalysts") if isinstance(payload.get("catalysts"), list) else []
    if not (4 <= len(rows) <= 7):
        issues.append(f"section_4.catalysts_count:{len(rows)}")
    lines = ["## 4 催化事件时间表", "", "| 时间 | 事件 | 影响 |", "|:---|:---|:---|"]
    for i, item in enumerate(rows[:7]):
        if not isinstance(item, dict):
            issues.append(f"section_4.catalysts[{i}].not_object")
            continue
        when = _text_ok(item.get("time") or item.get("date"), issues, f"section_4.catalysts[{i}].time", 4)
        event = _text_ok(item.get("event"), issues, f"section_4.catalysts[{i}].event", 8)
        impact = _text_ok(item.get("impact"), issues, f"section_4.catalysts[{i}].impact", 8)
        refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, issues, f"section_4.catalysts[{i}]")
        if re.search(r'研报发布|评级|目标价更新|broker report|target price', event, re.I):
            issues.append(f"section_4.catalysts[{i}].broker_event")
        if when and event and impact and refs:
            cite = _cite(refs)
            lines.append(f"| {when} | {event}{cite} | {impact}{cite} |")
    return ("\n".join(lines), issues) if len(lines) >= 8 and not issues else ("", issues)


def _build_section_4_deterministic(key_data: dict, ref_map: dict) -> tuple[str, list[str]]:
    rows: list[tuple[str, str, str, int]] = []
    issues: list[str] = []
    seen_events: list[str] = []
    event_signal = re.compile(
        r'发布|上线|接入|打通|推出|预计|实现|增长|提升|加大|投入|回升|商业化|业绩|收入|利润|游戏|广告|云|AI|Agent',
        re.I,
    )
    banned = re.compile(r'研报发布|评级|目标价|维持.*买入|上调|下调.*评级|broker report|target price', re.I)
    future_signal = re.compile(r'预计|将|有望|计划|年内|下半年|未来|指引|待|发布|推出|上线|扩产|开店|量产|商业化|milestone|launch|guidance', re.I)
    past_rows: list[tuple[str, str, str, int]] = []
    future_rows: list[tuple[str, str, str, int]] = []

    for rd in key_data.get("target_matched_reports", []) or []:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        when = str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:7]
        text = _plain_text("。".join(str(rd.get(k, "") or "") for k in ("textAbstract", "summary", "articleTitle", "title")), 1400)
        sentences = [s.strip() for s in re.split(r'[。；;\n]', text) if s.strip()]
        for sent in sentences:
            if not event_signal.search(sent) or banned.search(sent):
                continue
            if not _has_fact_signal(sent):
                continue
            event = _plain_text(sent, 62)
            if any(_topic_similar(event, prior) for prior in seen_events):
                continue
            impact = _plain_text(sent, 96)
            row = (when or "近期", event, impact, rn)
            if future_signal.search(sent):
                future_rows.append(row)
            else:
                past_rows.append(row)
            seen_events.append(event)
            break
        if len(past_rows) + len(future_rows) >= 8:
            break

    rows = (past_rows[:3] + future_rows[:4])[:6]
    if len(rows) < 4:
        rows = (future_rows[:4] + past_rows[:4])[:6]
    if len(rows) < 4:
        issues.append(f"section_4.deterministic_rows:{len(rows)}<4")
        return "", issues
    if not past_rows:
        issues.append("section_4.deterministic_no_past_events")
    if not future_rows:
        issues.append("section_4.deterministic_no_future_events")
    lines = ["## 4 催化事件时间表", "", "| 时间 | 事件 | 影响 |", "|:---|:---|:---|"]
    for when, event, impact, rn in rows[:6]:
        cite = _cite([rn])
        lines.append(f"| {when} | {event}{cite} | {impact}{cite} |")
    return "\n".join(lines), issues


def gen_hkus_section_4(key_data: dict, ref_map: dict | None = None) -> tuple[str, bool, list[str]]:
    if not _has_target_materials(key_data):
        return "", False, ["no_target_materials"]
    ref_map = ref_map or {}
    issues: list[str] = []
    for call_name in ("section_4",):
        text, ok = _call_llm(_section_4_json_prompt(key_data, issues if call_name.endswith("repair") else None),
                             max_tokens=2200,
                             timeout=min(_hkus_llm_slow_section_timeout_seconds(), _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name,
                             max_attempts=1)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_4(payload, ref_map)
        if rendered and not render_issues:
            return rendered, True, []
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    fallback, fallback_issues = _build_section_4_deterministic(key_data, ref_map)
    if fallback:
        return fallback, True, issues + ["section_4.ok_deterministic_fallback"] + fallback_issues
    return "", False, issues[:40]

def _validate_render_section_8(payload: dict, ref_map: dict, mkt: str = "US") -> tuple[str, list[str]]:
    issues: list[str] = []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not (3 <= len(rows) <= 5):
        return "", [f"section_8.rows_count:{0 if not isinstance(rows, list) else len(rows)}"]
    title = "## 8 市场关注/调研大纲" if mkt == "HK" else "## 8 市场关注"
    lines = [title, "", "| 关注点 | 市场在担心什么 | 需要验证的数据 |", "|:---|:---|:---|"]
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
    if mkt == "HK":
        agenda = payload.get("research_agenda") if isinstance(payload.get("research_agenda"), list) else []
        if len(agenda) < 3:
            issues.append(f"section_8.research_agenda_count:{len(agenda)}")
        for i, item in enumerate(agenda[:4], 1):
            if not isinstance(item, dict):
                issues.append(f"section_8.research_agenda[{i}].not_object")
                continue
            topic = _text_ok(item.get("topic"), issues, f"section_8.research_agenda[{i}].topic", 4)
            background = _text_ok(item.get("background"), issues, f"section_8.research_agenda[{i}].background", 12)
            questions = item.get("questions") if isinstance(item.get("questions"), list) else []
            refs = _refs_ok(item.get("source_refs"), ref_map, issues, f"section_8.research_agenda[{i}]")
            if len(questions) < 2:
                issues.append(f"section_8.research_agenda[{i}].questions_count:{len(questions)}")
            if topic and background and refs and len(questions) >= 2:
                lines.extend(["", f"**议题{i}：{topic}**", f"背景：{background}{_cite(refs)}"])
                for qn, q in enumerate(questions[:3], 1):
                    qtext = _plain_text(q, 80)
                    if qtext:
                        lines.append(f"- 问题{qn}：{qtext}")
    return ("\n".join(lines), issues) if len(lines) >= 6 and not issues else ("", issues)


def gen_hkus_section_8(key_data: dict, ref_map: dict, mkt: str = "HK") -> tuple[str, bool, list[str]]:
    extra_schema = ', "research_agenda": [{"topic": "...", "background": "...", "questions": ["..."], "source_refs": [1]}]' if mkt == "HK" else ""
    extra_rules = "For HK include 3-4 research_agenda items; for US do not output research_agenda."
    issues: list[str] = []
    for call_name in ("section_8", "section_8_json_repair"):
        issue_text = ""
        if call_name.endswith("repair") and issues:
            issue_text = "\nSchema/parse issues to fix:\n" + "\n".join(_extract_json_parse_issue(x) for x in issues[-8:])
        prompt = f"""Return ONLY valid JSON for HK/US report section_8 market concerns.
Schema: {{"rows": [{{"topic": "...", "market_concern": "...", "verification_metrics": ["..."], "source_refs": [1]}}]{extra_schema}}}
Rules: 3-5 rows; use only target-company context refs; no Markdown; no code fence; double quotes only; no trailing commas. {extra_rules}
Context:
{_format_hkus_key_context(key_data)}
{issue_text}
"""
        text, ok = _call_llm(prompt, max_tokens=3500, timeout=min(120, _hkus_llm_task_budget_seconds()), system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_8(payload, ref_map, mkt)
        if rendered and not render_issues:
            return rendered, True, render_issues
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    return "", False, issues[:30]

def gen_hkus_section_12(key_data: dict, ref_map: dict) -> tuple[str, bool, list[str]]:
    """生成 §12 风险提示（active path）。
    合同：prompt / schema / validator / renderer 均要求 title + explanation + source_refs。
    """
    issues: list[str] = []
    for call_name in ("section_12", "section_12_json_repair"):
        prompt = f"""Return ONLY JSON for HK/US report section_12 risks.
Schema: {{"risks": [{{"title": "不超过20个中文字的风险小标题", "explanation": "一句话说明触发条件及对收入/利润/现金流/估值/执行节奏的影响，不加粗", "source_refs": [1]}}]}}
Rules: 4-6 company-specific risks; title is short (≤20 Chinese chars); explanation is exactly one sentence; no generic macro/market-competition-only risk title; use only target-company context refs; no Markdown.
{("Schema issues to fix:\\n" + chr(10).join(issues)) if call_name.endswith("repair") and issues else ""}
Context:
{_format_hkus_key_context(key_data)}
"""
        text, ok = _call_llm(prompt, max_tokens=2800, timeout=min(120, _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_12(payload, ref_map)
        if rendered and not render_issues:
            return rendered, True, []
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    return "", False, issues[:40]

# ── §10 market debate (JSON-based LLM) ──

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
        title = _text_ok(risk.get("title"), issues, f"section_12.risks[{i}].title", 4)
        explanation = _text_ok(risk.get("explanation") or risk.get("body"), issues, f"section_12.risks[{i}].explanation", 12)
        refs = _refs_ok(risk.get("source_refs"), ref_map, issues, f"section_12.risks[{i}]")
        if re.search(r'宏观经济风险|市场竞争风险$', title):
            issues.append(f"section_12.risks[{i}].generic_title")
        if title and explanation and refs:
            clean_exp = re.sub(r'\[\d+\]', '', explanation).strip()
            lines.append(f"- **{title}**：{clean_exp}{_cite(refs)}")
    return ("\n".join(lines), issues) if len(lines) >= 6 and not issues else ("", issues)


_MARKET_DEBATE_BANNED_PHRASES = [
    "收入增长与需求兑现", "产品迭代与客户转化", "业务发展", "盈利改善", "估值修复",
    "基本面改善", "关注后续进展", "关注业务进展", "有待观察", "需持续跟踪",
    "保持关注", "静待验证", "进一步确认",
]

def _topic_key(text: str) -> set[str]:
    base = re.sub(r'\[\d+\]|[^\w\u4e00-\u9fff]+', ' ', str(text or "").lower())
    words = {w for w in base.split() if len(w) >= 2}
    zh = re.findall(r'[\u4e00-\u9fff]{2,6}', base)
    return words | set(zh)


def _topic_similar(a: str, b: str) -> bool:
    sa, sb = _topic_key(a), _topic_key(b)
    if not sa or not sb:
        return False
    return len(sa & sb) / max(1, len(sa | sb)) >= 0.55


def _has_fact_signal(text: str) -> bool:
    return bool(re.search(r'\d|20\d{2}|Q[1-4]|H[12]|FY|同比|环比|bps|%|客户|订单|出货|收入|利润|毛利|目标价|评级|量产|认证|capex|margin|revenue|order', text or "", re.I))


def _refs_from_row(row: dict, *keys: str) -> list[int]:
    refs: list[int] = []
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                try:
                    ref = int(item)
                except (TypeError, ValueError):
                    continue
                if ref not in refs:
                    refs.append(ref)
    return refs


def _section_10_json_prompt(key_data: dict, schema_issues: list[str] | None = None, scope: str = "section_10_a") -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    focus = "core business cycle, revenue/profit/cost, valuation or target-price divergence" if scope.endswith("_a") else "new business commercialization, product/customer validation, competition and medium-term growth"
    return f"""Return ONLY JSON for HK/US one-pager section 10 market debate subtask {scope}.
Focus: {focus}.
Schema:
{{"market_debates": [{{"theme": "...", "bull_view": "...", "bull_evidence": "...", "bull_source_ids": [1], "bear_view": "...", "bear_evidence": "...", "bear_source_ids": [2], "validation_metric": "...", "validation_window": "..."}}]}}
Rules: produce 2-4 candidate rows; bull and bear must oppose each other around the same theme; evidence cells contain facts/numbers/times and refs; validation is future observable; no Markdown.
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _format_section_10_retry_context(key_data: dict) -> str:
    lines = [
        f"Company: {key_data.get('company_name')} ({key_data.get('ticker')}) | Market: {key_data.get('market')}",
        "Target-company research excerpts:",
        str(key_data.get("recent_reports_text") or "")[:1500],
    ]
    if key_data.get("target_prices"):
        lines.append("Target prices / ratings:")
        for tp in key_data["target_prices"][:3]:
            lines.append(f"[{tp['ref']}] {tp['org']} | {tp['targetPrice']} | {tp.get('rating','')} | {tp.get('title','')}")
    return "\n".join(x for x in lines if x)


def _section_10_short_retry_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nPrevious issues:\n" + "\n".join((schema_issues or [])[:6]) if schema_issues else ""
    return f"""Return ONLY compact JSON for HK/US one-pager §10 market debates.
Schema:
{{"market_debates": [{{"theme": "...", "bull_view": "...", "bull_evidence": "...", "bull_source_ids": [1], "bear_view": "...", "bear_evidence": "...", "bear_source_ids": [2], "validation_metric": "...", "validation_window": "..."}}]}}
Rules:
- Produce 2-3 rows.
- Use only cited target-company research excerpts below.
- Bull and bear must be opposing interpretations of the same theme.
- Evidence must be summarized in your own words with concrete facts/numbers/times.
- No Markdown and no source text prefixes.
Context:
{_format_section_10_retry_context(key_data)}
{issue_text}"""


def _normalize_section_10_rows(payload: dict, ref_map: dict) -> tuple[list[dict], list[str]]:
    issues: list[str] = []
    debates = payload.get("market_debates") if isinstance(payload, dict) else None
    if not isinstance(debates, list):
        return [], ["section_10.rows_not_list"]
    rows: list[dict] = []
    accepted_themes: list[str] = []
    for i, row in enumerate(debates[:6]):
        if not isinstance(row, dict):
            issues.append(f"section_10.rows[{i}].not_object")
            continue
        theme = _text_ok(row.get("theme") or row.get("debate_theme") or row.get("bull_view"), issues, f"section_10.rows[{i}].theme", 4)
        bull = _text_ok(row.get("bull_view"), issues, f"section_10.rows[{i}].bull_view", 8)
        bear = _text_ok(row.get("bear_view"), issues, f"section_10.rows[{i}].bear_view", 8)
        bull_evidence = _text_ok(row.get("bull_evidence") or row.get("evidence"), issues, f"section_10.rows[{i}].bull_evidence", 8)
        bear_evidence = _text_ok(row.get("bear_evidence") or row.get("evidence"), issues, f"section_10.rows[{i}].bear_evidence", 8)
        validation_metric = _text_ok(row.get("validation_metric") or row.get("validation"), issues, f"section_10.rows[{i}].validation_metric", 4)
        validation_window = _plain_text(row.get("validation_window") or "", 24)
        validation = f"{validation_metric}（{validation_window}）" if validation_window else validation_metric
        bull_refs = _refs_ok(_refs_from_row(row, "bull_source_ids", "source_refs"), ref_map, issues, f"section_10.rows[{i}].bull")
        bear_refs = _refs_ok(_refs_from_row(row, "bear_source_ids", "source_refs"), ref_map, issues, f"section_10.rows[{i}].bear")
        if not (theme and bull and bear and bull_evidence and bear_evidence and validation and bull_refs and bear_refs):
            continue
        if any(_topic_similar(theme, prior) for prior in accepted_themes):
            issues.append(f"section_10.rows[{i}].duplicate_theme:{theme}")
            continue
        if bull == bear or _topic_similar(bull, bear):
            issues.append(f"section_10.rows[{i}].bull_equals_bear")
            continue
        if not _has_fact_signal(bull_evidence):
            issues.append(f"section_10.rows[{i}].bull_evidence_no_fact")
            continue
        if not _has_fact_signal(bear_evidence):
            issues.append(f"section_10.rows[{i}].bear_evidence_no_fact")
            continue
        if not _has_fact_signal(validation):
            issues.append(f"section_10.rows[{i}].validation_not_observable")
            continue
        rows.append({
            "theme": theme,
            "bull_view": bull,
            "bull_evidence": bull_evidence,
            "bull_refs": bull_refs,
            "bear_view": bear,
            "bear_evidence": bear_evidence,
            "bear_refs": bear_refs,
            "validation": validation,
            "validation_refs": list(dict.fromkeys(bull_refs + bear_refs)),
        })
        accepted_themes.append(theme)
    return rows, issues


def _render_section_10_rows(rows: list[dict]) -> str:
    lines = [
        "## 10 市场分歧", "",
        "| 多头观点 | 多头证据 | 空头观点 | 空头证据 | 需要观察的验证点 |",
        "|:---|:---|:---|:---|:---|",
    ]
    for row in rows:
        # 证据收紧到 2句/100字（v1.2.4）
        bull_evidence = _compress_evidence_sentences(row['bull_evidence'], max_sentences=2, max_chars=100)
        bear_evidence = _compress_evidence_sentences(row['bear_evidence'], max_sentences=2, max_chars=100)
        # validation_metric + validation_window 不走 compress，直接保留原始组合字符串
        validation = str(row.get('validation') or '').strip()
        lines.append(
            f"| {normalize_refs(row['bull_view'], [])} | {normalize_refs(bull_evidence, row['bull_refs'])} | "
            f"{normalize_refs(row['bear_view'], [])} | {normalize_refs(bear_evidence, row['bear_refs'])} | "
            f"{normalize_refs(validation, row['validation_refs'])} |"
        )
    return "\n".join(lines)


def _validate_render_section_10(payload: dict, ref_map: dict, min_rows: int = 3) -> tuple[str, list[str]]:
    rows, issues = _normalize_section_10_rows(payload, ref_map)
    fatal_ref_issues = (
        ".source_refs_missing",
        ".source_refs_empty_after_validation",
        ".source_refs_out_of_range",
        ".bull.source_refs_missing",
        ".bull.source_refs_empty_after_validation",
        ".bull.source_refs_out_of_range",
        ".bear.source_refs_missing",
        ".bear.source_refs_empty_after_validation",
        ".bear.source_refs_out_of_range",
    )
    if any(any(marker in issue for marker in fatal_ref_issues) for issue in issues):
        return "", issues + [f"section_10.rows_count:{len(rows)}"]
    if len(rows) < min_rows:
        return "", issues + [f"section_10.rows_count:{len(rows)}", f"section_10.valid_rows:{len(rows)}<{min_rows}"]
    if len(rows) < 3:
        issues.append(f"section_10.partial_rows:{len(rows)}<3")
    return _render_section_10_rows(rows[:5]), issues


def _compress_evidence_sentences(text: str, max_sentences: int = 3, max_chars: int = 140) -> str:
    clean = _clean_source_fragment(re.sub(r'^(多|空)[:：]\s*', '', str(text or "").strip()))
    parts = [p.strip() for p in re.split(r'(?<=[。；;])|\n+', clean) if p.strip()]
    if not parts:
        parts = [clean]
    scored = []
    for idx, sent in enumerate(parts):
        score = len(re.findall(r'\d|%|FY|Q[1-4]|H[12]|收入|利润|毛利|订单|用户|现金流|capex|revenue|margin', sent, re.I))
        scored.append((score, -idx, sent))
    picked = [x[2] for x in sorted(scored, reverse=True)[:max_sentences]]
    picked = [p for _, p in sorted((parts.index(p), p) for p in picked if p in parts)]
    out = "".join(picked)
    if len(out) <= max_chars:
        return out
    bounded = ""
    for sent in picked:
        if len(bounded) + len(sent) > max_chars:
            break
        bounded += sent
    return bounded or out[:max_chars].rsplit("，", 1)[0].rstrip("，；;")


def gen_hkus_section_10_part(key_data: dict, ref_map: dict, scope: str) -> tuple[list[dict], bool, list[str]]:
    if not _has_target_materials(key_data):
        return [], False, ["no_target_materials"]
    issues: list[str] = []
    for call_name in (scope,):
        prompt = _section_10_json_prompt(key_data, issues if call_name.endswith("repair") else None, scope)
        text, ok = _call_llm(prompt, max_tokens=2000,
                             timeout=min(_hkus_llm_slow_section_timeout_seconds(), _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name,
                             max_attempts=1)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rows, row_issues = _normalize_section_10_rows(payload, ref_map)
        if rows:
            return rows, True, row_issues
        issues.extend(f"{call_name}:{x}" for x in row_issues)
    return [], False, issues[:40]


def merge_hkus_section_10_parts(parts: list[list[dict]]) -> tuple[str, bool, list[str]]:
    merged: list[dict] = []
    issues: list[str] = []
    for rows in parts:
        for row in rows:
            if any(_topic_similar(row.get("theme", ""), prior.get("theme", "")) for prior in merged):
                issues.append(f"section_10.merge_duplicate_theme:{row.get('theme','')}")
                continue
            merged.append(row)
    if len(merged) < 3:
        return "", False, issues + [f"section_10.merged_rows:{len(merged)}<3"]
    return _render_section_10_rows(merged[:3]), True, issues


def gen_hkus_section_10_short_retry(key_data: dict, ref_map: dict, prior_issues: list[str] | None = None) -> tuple[str, bool, list[str]]:
    if not _has_target_materials(key_data):
        return "", False, ["no_target_materials"]
    text, ok = _call_llm(_section_10_short_retry_prompt(key_data, prior_issues),
                         max_tokens=1200,
                         timeout=min(50, _hkus_llm_task_budget_seconds()),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                         call_name="section_10_short_retry",
                         max_attempts=1)
    if not ok:
        return "", False, ["section_10_short_retry:llm_failed"]
    payload, parse_error = _parse_json_object(text)
    if parse_error:
        return "", False, [f"section_10_short_retry:{parse_error}"]
    rendered, issues = _validate_render_section_10(payload, ref_map, min_rows=2)
    if rendered and not any("rows_count" in x or "valid_rows" in x for x in issues):
        return rendered, True, issues
    return "", False, [f"section_10_short_retry:{x}" for x in issues[:30]]

def _build_target_price_basis_prompt(records: list[dict]) -> str:
    articles = []
    for rec in records:
        articles.append({
            "articleId": rec["article_id"],
            "orgName": rec["org"],
            "targetPrice": rec["target"],
            "date": rec["date"],
            "rating": rec.get("rating", ""),
            "title": rec.get("title", ""),
            "abstract": rec.get("abstract", ""),
            "report_text": rec.get("report_text", rec.get("evidence", ""))[:1200],
            "evidence": rec.get("evidence", "")[:600],
        })
    return f"""你是分析师,对以下机构研报逐篇提取估值依据和关键假设。只返回JSON数组。

Input:
{json.dumps(articles, ensure_ascii=False)}

返回格式:
[{{"articleId": "xxx", "target_price_basis": "估值口径", "basis_evidence": "同一篇研报中支持估值口径的原文短句", "key_assumptions": [{{"text": "假设1", "evidence": "同一篇研报中支持假设1的原文短句"}}]}}]

估值口径提取(三级优先级):
1. 明确方法: "2027E PE 18x" / "SOTP(光学+汽车+光互连)" / "DCF" / "EV/S 4x"
2. 仅提预测年份: "基于2027E盈利预测,正文未披露具体PE倍数"
3. 无方法: "研报披露目标价,正文未披露估值方法"

关键假设: 2-3条。必须含具体业务/数字/年份/客户/出货量/ASP/毛利率, 每条必须给 evidence。
禁止: {"、".join(_MARKET_DEBATE_BANNED_PHRASES[:6])}。无假设则写"估值方法未披露"。"""


def _parse_json_value(text: str) -> Any:
    raw = _clean_fence(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    for pattern in (r'(\[.*\])', r'(\{.*\})'):
        m = re.search(pattern, raw, re.S)
        if not m:
            continue
        try:
            return json.loads(m.group(1))
        except Exception:
            continue
    return None


def _normalize_target_basis_payload(payload: Any, expected_ids: set[str]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    if isinstance(payload, dict):
        payload = payload.get("articles") or payload.get("results") or [payload]
    if not isinstance(payload, list):
        return {}, {"unknown": [], "duplicates": [], "missing": sorted(expected_ids)}
    result: dict[str, dict] = {}
    unknown_ids: list[str] = []
    duplicate_ids: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("articleId") or item.get("article_id") or "").strip()
        if not aid:
            continue
        if aid not in expected_ids:
            unknown_ids.append(aid)
            continue
        if aid in result:
            duplicate_ids.append(aid)
            continue
        basis = _plain_text(item.get("target_price_basis") or item.get("basis") or "", 100)
        if not basis:
            basis = "研报披露目标价,正文未披露估值方法"
        basis_evidence = _plain_text(item.get("basis_evidence") or item.get("evidence") or "", 180)
        assumptions = item.get("key_assumptions")
        assumption_evidence: list[str] = []
        if isinstance(assumptions, list):
            parts = []
            for x in assumptions[:3]:
                if isinstance(x, dict):
                    text = _plain_text(x.get("text"), 80)
                    ev = _plain_text(x.get("evidence"), 160)
                    if text:
                        parts.append(text)
                        if ev:
                            assumption_evidence.append(ev)
                else:
                    text = _plain_text(x, 80)
                    if text:
                        parts.append(text)
            parts = [x for x in parts if not any(b in x for b in _MARKET_DEBATE_BANNED_PHRASES[:6])]
            key_assumptions = "；".join(parts) if parts else "估值方法未披露"
        elif isinstance(assumptions, str) and assumptions.strip():
            key_assumptions = _plain_text(assumptions, 200)
        else:
            key_assumptions = "估值方法未披露"
        result[aid] = {
            "target_price_basis": basis,
            "basis_evidence": basis_evidence,
            "key_assumptions": key_assumptions,
            "assumption_evidence": assumption_evidence,
        }
    return result, {
        "unknown": unknown_ids,
        "duplicates": duplicate_ids,
        "missing": sorted(expected_ids - set(result)),
    }


def _extract_target_price_basis(records: list[dict]) -> dict[str, dict]:
    """LLM-based per-article extraction of valuation basis and key assumptions.

    Strict articleId binding: every returned articleId must exist in input;
    unknown/dropped/duplicate IDs are discarded with diagnostics.
    """
    if not records:
        return {}
    task_started = time.time()
    task_budget = min(_hkus_llm_task_budget_seconds(), _hkus_llm_slow_section_timeout_seconds())
    expected_ids = {str(r["article_id"]) for r in records}
    by_id = {str(r["article_id"]): r for r in records}
    prompt = _build_target_price_basis_prompt(records)
    text, ok = _call_llm(prompt, max_tokens=2600, timeout=task_budget,
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                         call_name="target_price_basis",
                         max_attempts=1)
    result, problems = _normalize_target_basis_payload(_parse_json_value(text) if ok else None, expected_ids)
    retry_specs: list[LlmTaskSpec] = []
    missing_for_retry = sorted(expected_ids - set(result))
    if len(result) >= 3:
        missing_for_retry = []
    else:
        missing_for_retry = missing_for_retry[:TARGET_PRICE_BASIS_MAX_RETRIES]
    for aid in missing_for_retry:
        if time.time() - task_started >= task_budget:
            _append_llm_issue("target_price_basis_issues", "target_price_basis_task_budget_exceeded")
            break
        rec = by_id.get(aid)
        if not rec:
            continue
        def _retry_runner(aid=aid, rec=rec):
            retry_text, retry_ok = _call_llm(
                _build_target_price_basis_prompt([rec]),
                max_tokens=1000,
                timeout=min(30, task_budget),
                system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                call_name=f"target_price_basis_retry:{aid}",
                max_attempts=1,
            )
            retry_result, retry_problems = _normalize_target_basis_payload(_parse_json_value(retry_text) if retry_ok else None, {aid})
            return {"ok": retry_ok and aid in retry_result, "status": "ok_retry" if aid in retry_result else "failed_retry",
                    "aid": aid, "result": retry_result, "problems": retry_problems}
        retry_specs.append(LlmTaskSpec(task_name=f"target_price_basis_retry:{aid}", runner=_retry_runner))
    if retry_specs:
        retry_results, retry_meta = run_hkus_llm_tasks(
            retry_specs,
            max_workers=_hkus_llm_retry_workers(),
            total_budget_seconds=max(1, int(task_budget - (time.time() - task_started))),
            progress_prefix="[LLM RETRY]",
        )
        if retry_meta.get("budget_exceeded"):
            _append_llm_issue("target_price_basis_issues", "target_price_basis_retry_budget_exceeded")
        for task_result in retry_results:
            content = task_result.content if isinstance(task_result.content, dict) else {}
            aid = str(content.get("aid") or "")
            retry_result = content.get("result") if isinstance(content.get("result"), dict) else {}
            retry_problems = content.get("problems") if isinstance(content.get("problems"), dict) else {}
            if aid and aid in retry_result:
                result[aid] = retry_result[aid]
            problems.setdefault("unknown", []).extend(retry_problems.get("unknown", []))
            problems.setdefault("duplicates", []).extend(retry_problems.get("duplicates", []))
    missing = sorted(expected_ids - set(result))
    for aid in missing:
        result[aid] = {
            "target_price_basis": "研报披露目标价,正文未披露估值方法",
            "basis_evidence": "",
            "key_assumptions": "估值方法未披露",
            "assumption_evidence": [],
        }
    unknown_ids = problems.get("unknown", [])
    dropped_dup = problems.get("duplicates", [])
    if unknown_ids:
        _append_llm_issue("target_price_basis_issues", f"unknown_article_ids_dropped: {unknown_ids}")
    if dropped_dup:
        _append_llm_issue("target_price_basis_issues", f"duplicate_article_ids_dropped: {dropped_dup}")
    if missing:
        _append_llm_issue("target_price_basis_issues", f"missing_article_ids_fail_closed: {list(missing)}")
    return result


def calculate_target_price_stats(values: list[float]) -> dict:
    """Deterministic target price statistics with correct even-count median."""
    clean = []
    for v in values:
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            # Try parsing string with currency symbols
            if isinstance(v, str):
                cleaned = re.sub(r'[^\d.]', '', str(v))
                try:
                    fv = float(cleaned)
                except (TypeError, ValueError):
                    continue
            else:
                continue
        if fv > 0:
            clean.append(fv)
    clean.sort()
    if not clean:
        return {"low": None, "median": None, "high": None, "count": 0}
    n = len(clean)
    if n % 2 == 1:
        median = clean[n // 2]
    else:
        median = round((clean[n // 2 - 1] + clean[n // 2]) / 2, 2)
    return {"low": clean[0], "median": median, "high": clean[-1], "count": n}


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


def _run_sections_1_2_task(key_data: dict, ref_map: dict) -> dict:
    sections, ok, meta = gen_hkus_sections_1_2(key_data, ref_map)
    return {"ok": ok, "status": meta.get("call_mode", "ok_json" if ok else "failed_schema"),
            "sections": sections if ok else {}, "meta": meta}


def _run_section_3_task(key_data: dict, ref_map: dict) -> dict:
    text, ok, issues = gen_hkus_section_3(key_data, ref_map)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "text": text, "issues": issues}


def _run_section_3_group_task(key_data: dict, ref_map: dict, group_key: str) -> dict:
    rows, ok, issues = gen_hkus_section_3_group(key_data, ref_map, group_key)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "rows": rows, "issues": issues}


def _run_section_4_task(key_data: dict, ref_map: dict) -> dict:
    text, ok, issues = gen_hkus_section_4(key_data, ref_map)
    status = "ok_deterministic_fallback" if ok and "section_4.ok_deterministic_fallback" in issues else ("ok_json" if ok else "failed_json_schema")
    return {"ok": ok, "status": status, "text": text, "issues": issues}


def _run_section_8_task(key_data: dict, ref_map: dict, mkt: str = "HK") -> dict:
    text, ok, issues = gen_hkus_section_8(key_data, ref_map, mkt)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "text": text, "issues": issues}


def _run_section_10_part_task(key_data: dict, ref_map: dict, scope: str) -> dict:
    rows, ok, issues = gen_hkus_section_10_part(key_data, ref_map, scope)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "rows": rows, "issues": issues}


def _run_section_12_task(key_data: dict, ref_map: dict) -> dict:
    text, ok, issues = gen_hkus_section_12(key_data, ref_map)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "text": text, "issues": issues}


def _repair_title_from_verified_sections(texts: dict, company_name: str, ticker: str, market_cn: str) -> str:
    verified = "\n\n".join(x for x in (texts.get("s12", ""), texts.get("s34", "")) if x).strip()
    if not verified:
        return ""
    prompt = (
        "请根据以下研报内容，生成一句10-25个中文字的投资结论，作为报告标题后半部分。"
        "要求：不含公司名称、股票代码、冒号、书名号；必须包含明确的投资判断词（如：驱动/受益/加速/商业化/增长/布局/验证/落地/变现等）；直接输出结论文字，不加任何前缀或解释。\n\n"
        f"{verified[:1500]}"
    )
    text, ok = _call_llm(prompt, max_tokens=120, timeout=min(45, _hkus_llm_task_budget_seconds()),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name="title_repair")
    if not ok:
        return ""
    return _sanitize_title_conclusion(text, company_name, ticker, market_cn)


def _run_target_price_basis_task(records: list[dict]) -> dict:
    if not records:
        return {"ok": False, "status": "failed_no_traceable_target_price", "basis_map": {}, "record_count": 0}
    basis_map = _extract_target_price_basis(records)
    return {"ok": bool(basis_map), "status": "ok" if basis_map else "failed", "basis_map": basis_map, "record_count": len(records)}


def _run_forecast_sample_extraction_task(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str) -> dict:
    samples = _extract_forecast_samples(materials)
    payload = _forecast_samples_payload(samples)
    aggregate = _aggregate_forecast_samples(samples)
    return {
        "ok": bool(samples),
        "status": "ok" if samples else "failed_no_forecast_samples",
        "forecast_samples": payload,
        "consensus_forecast": aggregate,
        "sample_count": len(samples),
    }


def _section_5_json_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""Return ONLY JSON for HK/US one-pager section 5 business breakdown.
Schema:
{{"business_model": {{"text": "...", "source_ids": [1]}}, "performance_mode": "segment_table|product_matrix|kpi_table", "periods": [{{"label": "FY2025", "period_type": "annual_actual", "currency": "RMB", "unit": "亿元"}}], "segment_rows": [{{"business": "...", "values": [{{"period": "FY2025", "revenue": "100", "share": "30%", "gross_margin": "20%", "data_basis": "actual", "source_ids": [1]}}]}}], "kpi_rows": [{{"business": "...", "metric": "...", "period": "...", "value": "...", "source_ids": [1]}}], "deep_dives": [{{"business": "...", "conclusion": "...", "text": "...", "source_ids": [1]}}]}}
Rules:
- business_model.text: write approximately 200 Chinese characters (aim for 150-250 chars). Explain clearly how the company makes money: main revenue streams, key profit drivers, and business model logic. Must cite [N]. No bullet points — write as flowing prose.
- no Markdown tables in business_model.text; never invent segment revenue or gross margin; do not output N/A or validation-variable fields.
- FORBIDDEN in business/metric/deep_dive: 融资金额, 投后估值, 减持, 回购, 股东, 评级, 目标价, 投资者数量, 可灵估值. These are capital events, not operating business segments.
- deep_dives: only real operating business units; text must be 2-3 sentences, ≤160 Chinese characters per business.
- If §5.2 cannot use an annual segment table, deep_dives will be rendered as §5.2 narrative bullets; each business must have a concise conclusion that works as a bold subheading.
- data_basis must be one of: annual_actual, quarterly_actual, forecast, estimate.
- SECTION 5.2 DATA PRIORITY: output segment_table with each business segment's annual revenue and revenue share. Prefer latest 3 annual_actual FY periods when all 3 are available; if 3 years are not available, output the latest 1 annual_actual FY period only; if even 1 annual revenue/share period is unavailable, leave segment_rows empty so the writer hides §5.2.
- SECTION 5.2 FORBIDDEN FALLBACK: do NOT use quarterly KPI, forecast KPI, YoY-only rows, profit, valuation, target price, or shareholder/capital event data as §5.2.
- TABLE HEADER RULE: periods array must reflect what the table actually covers. For annual data use labels like "FY2023", "FY2024", "FY2025". Do NOT label annual data as "2026Q2E".
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


def _validate_render_section_5(payload: dict, ref_map: dict, annual_required_periods: Any = 0) -> tuple[str, list[str]]:
    """验证并渲染 §5。
    hard_issues：真正导致章节无法输出的错误（performance_missing / deep_dives_missing 等）。
    soft_issues：过滤日志（capital_event_skipped 等），只记录不阻断输出。
    只有 hard_issues 非空时才返回空字符串。
    """
    hard_issues: list[str] = []
    soft_issues: list[str] = []

    if not isinstance(payload, dict):
        return "", ["section_5.payload_not_object"]

    bm = payload.get("business_model") if isinstance(payload.get("business_model"), dict) else {}
    bm_text = _text_ok(bm.get("text"), soft_issues, "section_5.business_model.text", 80)
    bm_refs = _refs_ok(bm.get("source_ids") or bm.get("source_refs"), ref_map, soft_issues, "section_5.business_model")
    periods = payload.get("periods") if isinstance(payload.get("periods"), list) else []
    period_labels = [_plain_text(p.get("label"), 20) for p in periods if isinstance(p, dict) and _plain_text(p.get("label"), 20)]
    period_labels = period_labels[:4]
    lines = ["## 5 业务拆分", "", "### 5.1 公司如何赚钱", ""]
    annual_issues: list[str] = []
    required_labels: list[str] = []
    if isinstance(annual_required_periods, list):
        for p in annual_required_periods:
            label = _plain_text(p, 20)
            if re.fullmatch(r'FY20\d{2}', label):
                required_labels.append(label)
    else:
        try:
            required_count = int(annual_required_periods or 0)
        except (TypeError, ValueError):
            required_count = 0
        annual_labels_for_requirement = [p for p in period_labels if re.fullmatch(r'FY20\d{2}', p)]
        if required_count >= 3 and len(annual_labels_for_requirement) >= 3:
            required_labels = annual_labels_for_requirement[:3]
        elif required_count >= 1 and annual_labels_for_requirement:
            required_labels = annual_labels_for_requirement[:1]
    if bm_text and bm_refs:
        lines.append(normalize_refs(bm_text, bm_refs))
    perf_start_idx = len(lines)
    lines.extend(["", "### 5.2 分业务表现", ""])
    segment_rows = payload.get("segment_rows") if isinstance(payload.get("segment_rows"), list) else []
    kpi_rows = payload.get("kpi_rows") if isinstance(payload.get("kpi_rows"), list) else []
    # mode 声明放在这里，表格渲染逻辑下方会用到
    mode = _plain_text(payload.get("performance_mode") or "segment_table", 24)
    annual_labels = [p for p in period_labels if re.fullmatch(r'FY20\d{2}', p)]
    completed_annual_labels = [p for p in annual_labels if int(p[2:]) <= int(TODAY[:4]) - 1]
    if required_labels:
        if mode != "segment_table":
            annual_issues.append(f"section_5.annual_segment_required_but_mode:{mode}")
        missing = [p for p in required_labels if p not in completed_annual_labels]
        if missing:
            annual_issues.append(f"section_5.annual_periods_missing:{','.join(missing)}")
        period_labels = [p for p in period_labels if p in required_labels]
    else:
        period_labels = completed_annual_labels[:3]

    # 资本事件关键词（函数级常量）
    _CAPITAL_EVENT_RE = re.compile(
        r'融资|投后估值|减持|回购|增持|股东|评级|目标价|投资者|可灵.*估值|轮融资|持股|股份|分红', re.I
    )

    rendered_perf = False
    if mode == "segment_table" and period_labels and segment_rows and not annual_issues:
        rows = []
        for row in segment_rows[:8]:
            if not isinstance(row, dict):
                continue
            business = _plain_text(row.get("business"), 30)
            if not business or _CAPITAL_EVENT_RE.search(business):
                soft_issues.append(f"section_5.segment.{business}.capital_event_skipped")
                continue
            values = row.get("values") if isinstance(row.get("values"), list) else []
            by_period = {}
            refs = []
            for value in values:
                if not isinstance(value, dict):
                    continue
                period = _plain_text(value.get("period"), 20)
                if period not in period_labels:
                    continue
                data_basis = _plain_text(value.get("data_basis"), 24).lower()
                if data_basis and data_basis not in ("annual_actual", "actual"):
                    continue
                rev = _plain_text(value.get("revenue"), 32)
                share = _plain_text(value.get("share"), 32)
                gm = _plain_text(value.get("gross_margin"), 32)
                if rev and share and not _PLACEHOLDER_RE.search(rev + share):
                    parts = [f"收入{rev}", f"占比{share}"]
                    if gm and not _PLACEHOLDER_RE.search(gm):
                        parts.append(f"毛利率{gm}")
                    by_period[period] = "；".join(parts)
                    # ref 问题记到 soft，不阻断
                    refs.extend(_refs_ok(value.get("source_ids") or value.get("source_refs"), ref_map, soft_issues, f"section_5.segment.{business}.{period}"))
            if business and by_period:
                rows.append((business, by_period, list(dict.fromkeys(refs))))
        nonempty_periods = [p for p in period_labels if any(p in row[1] for row in rows)]
        if rows and nonempty_periods:
            # 指定表头格式：业务板块 | FYxxxx 收入（亿元）| FYxxxx 占比 | ...（每期两列：收入+占比）
            header_cols = ["业务板块"]
            for p in nonempty_periods:
                header_cols.append(f"{p} 收入（亿元）")
                header_cols.append(f"{p} 占比")
            lines.append("| " + " | ".join(header_cols) + " |")
            lines.append("|:--|" + "|".join(":--" for _ in header_cols[1:]) + "|")
            for business, by_period, refs in rows:
                cell_raw = by_period.get
                row_cells = [business]
                for p in nonempty_periods:
                    raw_cell = by_period.get(p, "")
                    # 从 "收入100；占比30%；毛利率20%" 中分别提取收入和占比
                    rev_match = re.search(r'收入([^；;，\s]+)', raw_cell)
                    share_match = re.search(r'占比([^；;，\s]+)', raw_cell)
                    rev_val = rev_match.group(1) if rev_match else ""
                    share_val = share_match.group(1) if share_match else ""
                    # 若没有精细拆分，则用原始 cell 填收入列，占比列留空
                    if not rev_val and not share_val:
                        rev_val = raw_cell
                    row_cells.append(normalize_refs(rev_val, refs))
                    row_cells.append(normalize_refs(share_val, refs) if share_val else "")
                lines.append("| " + " | ".join(row_cells) + " |")
            rendered_perf = True
    if not rendered_perf and kpi_rows:
        soft_issues.append("section_5.kpi_rows_ignored_for_annual_revenue_share")
    if not rendered_perf:
        soft_issues.extend(annual_issues)
        soft_issues.append("section_5.performance_missing")
        del lines[perf_start_idx:]

    deep_section_no = "5.3" if rendered_perf else "5.2"
    lines.extend(["", f"### {deep_section_no} \u4e1a\u52a1\u6df1\u5ea6", ""])
    dives = payload.get("deep_dives") if isinstance(payload.get("deep_dives"), list) else []
    dive_count = 0
    for i, item in enumerate(dives[:2]):
        if not isinstance(item, dict):
            soft_issues.append(f"section_5.deep_dives[{i}].not_object")
            continue
        business = _text_ok(item.get("business"), soft_issues, f"section_5.deep_dives[{i}].business", 2)
        conclusion = _text_ok(item.get("conclusion"), soft_issues, f"section_5.deep_dives[{i}].conclusion", 6)
        text = _text_ok(item.get("text"), soft_issues, f"section_5.deep_dives[{i}].text", 30)
        refs = _refs_ok(item.get("source_ids") or item.get("source_refs"), ref_map, soft_issues, f"section_5.deep_dives[{i}]")
        # 资本事件业务名 → soft
        if business and _CAPITAL_EVENT_RE.search(business):
            soft_issues.append(f"section_5.deep_dives[{i}].capital_event_business:{business}")
            continue
        if business and conclusion and text and refs:
            # §5.3 字数上限：160字，按完整句子截断
            zh_count = len(re.findall(r'[一-鿿]', text))
            if zh_count > 160:
                sentences = re.split(r'(?<=[。；;])', text)
                truncated, total = [], 0
                for sent in sentences:
                    sc = len(re.findall(r'[一-鿿]', sent))
                    if total + sc > 160:
                        break
                    truncated.append(sent)
                    total += sc
                text = "".join(truncated).strip() or text[:160]
            lines.append(f"- **{business}——{conclusion}：** {normalize_refs(text, refs)}")
            dive_count += 1
    if dive_count < 1:
        # 5.3 完全没有业务深度：hard issue
        hard_issues.append("section_5.deep_dives_missing")

    if not bm_text or not bm_refs:
        hard_issues.append("section_5.business_model_missing")

    # 只有 5.1/5.3 缺失才阻断整章；5.2 不合格保留章节并记录 soft issue。
    if hard_issues:
        return "", hard_issues + soft_issues
    return "\n".join(lines), soft_issues


def gen_hkus_section_5(key_data: dict, ref_map: dict) -> tuple[str, bool, list[str]]:
    issues: list[str] = []
    annual_required_periods = key_data.get("annual_segment_preferred_periods") or int(key_data.get("annual_segment_required_periods") or 0)
    for call_name in ("section_5", "section_5_json_repair"):
        text, ok = _call_llm(_section_5_json_prompt(key_data, issues if call_name.endswith("repair") else None),
                             max_tokens=4500, timeout=min(120, _hkus_llm_task_budget_seconds()),
                             system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_5(payload, ref_map, annual_required_periods)
        # 只要 rendered 非空就算成功（render_issues 可能含 soft_issues，不阻断）
        if rendered:
            return rendered, True, render_issues
        issues.extend(f"{call_name}:{x}" for x in render_issues)
    return "", False, issues[:40]


def _section_6_json_prompt(key_data: dict, schema_issues: list[str] | None = None) -> str:
    issue_text = "\nSchema issues to fix:\n" + "\n".join(schema_issues or []) if schema_issues else ""
    return f"""Return ONLY JSON for HK/US one-pager section 6 supply/customer chain and ecosystem.
Schema:
{{"rows": [{{"type": "主要客户|主要供应商|核心资源|渠道|生态伙伴", "name": "...", "relationship": "...", "source_ids": [1]}}], "fallback_description": {{"text": "...", "source_ids": [1]}}}}
Rules:
- Always prefer outputting rows over fallback_description; fallback_description is only used when truly no concrete entity/resource can be found.
- do not output upstream/middle/downstream prose or validation-variable fields.
- EXCLUDE entities that appear only as shareholders, one-time investors, or capital-market participants with no ongoing operating relationship with the company.
- relationship field MUST describe specific business relationship, scale, proportion, or cooperation mode. Generic phrases like "战略合作", "长期支持", "重要合作伙伴" are NOT acceptable — must include concrete detail (e.g. 占采购额约30%, 月活用户超1亿, 独家内容供应商).
Context:
{_format_hkus_key_context(key_data)}
{issue_text}"""


# 空泛关系描述正则：这些词组单独出现时视为无实质内容
_VAGUE_RELATIONSHIP_RE = re.compile(
    r'^(战略合作|长期支持|重要合作伙伴|深度合作|持续合作|合作关系|生态合作|友好合作|密切合作|相互合作)$'
)
# 纯资本方关键词：type 或 name 含这些词且 relationship 无实质内容时跳过
_CAPITAL_PARTY_RE = re.compile(r'股东|投资方|投资人|基金|持股|融资方|一次性.*投资|LP|GP')


def _extract_section_6_rows(payload: dict, ref_map: dict) -> tuple[list[tuple], list[str]]:
    """从 payload 中提取有效行，返回 (rendered_rows, soft_issues)。
    soft_issues 只记录过滤日志，不影响章节是否输出。
    """
    soft: list[str] = []
    rows = payload.get("rows") if isinstance(payload, dict) and isinstance(payload.get("rows"), list) else []
    rendered: list[tuple] = []
    hard_issues: list[str] = []
    for i, row in enumerate(rows[:10]):
        if not isinstance(row, dict):
            soft.append(f"section_6.rows[{i}].not_object")
            continue
        typ = _plain_text(row.get("type"), 24)
        name = _plain_text(row.get("name"), 40)
        rel = _plain_text(row.get("relationship"), 120)
        refs = _refs_ok(row.get("source_ids") or row.get("source_refs"), ref_map, hard_issues, f"section_6.rows[{i}]")
        # 过滤：纯资本方
        if _CAPITAL_PARTY_RE.search(typ + name) and _VAGUE_RELATIONSHIP_RE.match(rel.strip()):
            soft.append(f"section_6.rows[{i}].capital_party_skipped:{name}")
            continue
        # 过滤：relationship 完全空泛
        if _VAGUE_RELATIONSHIP_RE.match(rel.strip()):
            soft.append(f"section_6.rows[{i}].vague_relationship_skipped:{name}")
            continue
        if typ and name and rel and refs:
            rendered.append((typ, name, normalize_refs(rel, refs)))
    return rendered, soft


def _validate_render_section_6(payload: dict, ref_map: dict) -> tuple[str, list[str]]:
    """验证并渲染 §6。>=1 行有效数据即可出表；0行走 fallback_description。"""
    issues: list[str] = []
    rendered_rows, soft = _extract_section_6_rows(payload, ref_map)
    lines = ["## 6 产销链与生态", ""]
    if rendered_rows:
        lines.extend(["| 类型 | 名称 | 合作情况/规模/占比 |", "|:--|:--|:--|"])
        for row in rendered_rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines), []   # 有行就成功，soft issues 不阻断
    # 无行：尝试 fallback_description
    fallback = payload.get("fallback_description") if isinstance(payload, dict) and isinstance(payload.get("fallback_description"), dict) else {}
    text = _text_ok(fallback.get("text"), issues, "section_6.fallback_description.text", 40)
    refs = _refs_ok(fallback.get("source_ids") or fallback.get("source_refs"), ref_map, issues, "section_6.fallback_description")
    if text and refs:
        lines.append(normalize_refs(text, refs))
        return "\n".join(lines), []
    return "", soft + issues + ["section_6.no_valid_rows_and_no_fallback"]


def _section_6_supplement_prompt(key_data: dict, existing_rows: list[tuple], ref_map: dict) -> str:
    """已有 existing_rows 行但不足4行时，让 LLM 从研报里最多补充1行。"""
    existing_text = "\n".join(f"- {r[0]} | {r[1]} | {r[2]}" for r in existing_rows)
    return f"""Already generated §6 rows:
{existing_text}

Return ONLY JSON with additional rows to supplement the table above.
Schema: {{"rows": [{{"type": "主要客户|主要供应商|核心资源|渠道|生态伙伴", "name": "...", "relationship": "...", "source_ids": [1]}}]}}
Rules:
- Output 0-1 NEW row not already covered above; use only types: 主要客户, 主要供应商, 核心资源, 渠道, 生态伙伴.
- relationship MUST be specific (scale, proportion, cooperation detail); no "战略合作" or generic phrases.
- EXCLUDE shareholders, one-time investors, capital-market participants.
- If no new source-backed rows can be found, return {{"rows": []}}.
Context:
{_format_hkus_key_context(key_data)}"""

def _context_ref_excerpt(key_data: dict, keywords: list[str]) -> tuple[int, str]:
    contexts = "\n".join(str(key_data.get(k) or "") for k in (
        "recent_reports_text", "annual_segment_text", "peer_context_text"
    ))
    for line in contexts.splitlines():
        if not any(k and k.lower() in line.lower() for k in keywords):
            continue
        m = re.search(r'\[(\d+)\]', line)
        if not m:
            continue
        try:
            ref = int(m.group(1))
        except ValueError:
            continue
        return ref, _plain_text(line, 220)
    return -1, ""

def _section_6_deterministic_rows(key_data: dict, existing_rows: list[tuple]) -> tuple[list[tuple], list[str]]:
    rows = list(existing_rows)
    issues: list[str] = []
    if len(rows) < 4:
        issues.append("section_6.deterministic_no_generic_rows")
    return rows, issues


def _render_section_6_rows(rows: list[tuple]) -> str:
    lines = ["## 6 产销链与生态", "",
             "| 类型 | 名称 | 合作情况/规模/占比 |", "|:--|:--|:--|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def gen_hkus_section_6(key_data: dict, ref_map: dict) -> tuple[str, bool, list[str]]:
    issues: list[str] = []
    best_rows: list[tuple] = []

    for call_name in ("section_6", "section_6_json_repair"):
        text, ok = _call_llm(
            _section_6_json_prompt(key_data, issues if call_name.endswith("repair") else None),
            max_tokens=3500, timeout=min(120, _hkus_llm_task_budget_seconds()),
            system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=call_name)
        if not ok:
            issues.append(f"{call_name}:llm_failed")
            continue
        payload, parse_error = _parse_json_object(text)
        if parse_error:
            issues.append(f"{call_name}:{parse_error}")
            continue
        rendered, render_issues = _validate_render_section_6(payload, ref_map)
        if rendered and not render_issues:
            # 有输出了——但如果行数 < 4，尝试补写
            rows, _ = _extract_section_6_rows(payload, ref_map)
            if len(rows) < 4:
                best_rows = rows  # 先存下来，下面补写
                break
            return rendered, True, []
        # 完全没输出，记录 issues 继续 retry
        issues.extend(f"{call_name}:{x}" for x in render_issues)

    # 如果有 >=1 行但 <4 行，追加一次补写
    if best_rows:
        supp_text, supp_ok = _call_llm(
            _section_6_supplement_prompt(key_data, best_rows, ref_map),
            max_tokens=2000, timeout=min(90, _hkus_llm_task_budget_seconds()),
            system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name="section_6_supplement")
        if supp_ok:
            supp_payload, supp_err = _parse_json_object(supp_text)
            if not supp_err:
                supp_rows, _ = _extract_section_6_rows(supp_payload, ref_map)
                # 合并：去重（按 name 去重）
                seen_names = {r[1] for r in best_rows}
                for r in supp_rows[:1]:
                    if r[1] not in seen_names:
                        best_rows.append(r)
                        seen_names.add(r[1])
            else:
                issues.append(f"section_6_supplement:{supp_err}")
        else:
            issues.append("section_6_supplement:llm_failed")
        if len(best_rows) < 4:
            best_rows, det_issues = _section_6_deterministic_rows(key_data, best_rows)
            issues.extend(det_issues)
        # 用合并后的行组装最终输出
        if len(best_rows) >= 3:
            if len(best_rows) < 4:
                issues.append(f"section_6.partial_rows:{len(best_rows)}<4")
            return _render_section_6_rows(best_rows), True, issues[:40]
        else:
            issues.append(f"section_6.rows_count:{len(best_rows)}<4")
            return "", False, issues[:40]

    return "", False, issues[:40]


def _run_section_5_task(key_data: dict, ref_map: dict) -> dict:
    text, ok, issues = gen_hkus_section_5(key_data, ref_map)
    return {"ok": ok, "status": "ok_json" if ok else "failed_json_schema", "text": text, "issues": issues}


def _run_section_6_task(key_data: dict, ref_map: dict) -> dict:
    text, ok, issues = gen_hkus_section_6(key_data, ref_map)
    status = "partial_json" if ok and any("section_6.partial_rows" in x for x in issues) else ("ok_json" if ok else "failed_json_schema")
    return {"ok": ok, "status": status, "text": text, "issues": issues}


def _run_section_5_7_task(sk: str, materials: dict, ref_map: dict, key_data: dict, assigned: dict,
                          company_name: str, ticker: str, mkt: str, guide: str,
                          target_materials_ok: bool, key: str) -> dict:
    hk_s57 = sk == "s57" and mkt == "HK"
    hk_financial_section = _build_hk_financial_section(materials, ref_map) if hk_s57 else ""
    prompt_h2 = ["5 业务拆分", "6 产销链与生态"] if hk_s57 else None
    mats = _build_section_context(sk, key_data, assigned, ref_map)
    if len(mats) < 100 or (sk != "s12r" and not target_materials_ok):
        if hk_s57 and hk_financial_section:
            return {"ok": True, "status": "degraded_partial_hk_pit_only", "text": hk_financial_section,
                    "failed_sections": ["5", "6"], "section_status": {"7": "ok_pit_deterministic"}, "chars": len(hk_financial_section)}
        return {"ok": False, "status": "failed_sparse", "text": "", "failed_sections": [sk], "section_status": {}, "chars": 0}
    if not key:
        if hk_s57 and hk_financial_section:
            return {"ok": True, "status": "degraded_partial_hk_pit_only", "text": hk_financial_section,
                    "failed_sections": ["5", "6"], "section_status": {"7": "ok_pit_deterministic"}, "chars": len(hk_financial_section)}
        return {"ok": False, "status": "skipped", "text": "", "failed_sections": [sk], "section_status": {}, "chars": 0}
    si = SECTION_MATERIAL_MAP[sk]
    prompt = _build_section_prompt(sk, mats, company_name, ticker, mkt, h2_override=prompt_h2)
    prompt += f"\n\n---\nREFERENCE GUIDE (only use these [N] numbers, 1-{len(ref_map)}):\n{guide}\n\nDo NOT invent new [N] numbers beyond range 1-{len(ref_map)}."
    text, ok = _call_llm(prompt, max_tokens=si.get("max_tokens", 8000),
                         timeout=si.get("timeout", 180),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                         call_name=f"{sk}_markdown")
    if ok and text and len(text.strip()) > 100:
        body = _clean_fence(text)
        section_status = {}
        failed_sections = []
        if hk_s57:
            if hk_financial_section:
                body = "\n\n".join([body, hk_financial_section])
                section_status["7"] = "ok_pit_deterministic"
            else:
                section_status["7"] = "failed_missing_pit"
                failed_sections.append("7")
        return {"ok": True, "status": "ok" if not failed_sections else "degraded_missing_hk_pit",
                "text": body, "failed_sections": failed_sections, "section_status": section_status, "chars": len(body)}
    if hk_s57 and hk_financial_section:
        return {"ok": True, "status": "degraded_partial_hk_pit_only", "text": hk_financial_section,
                "failed_sections": ["5", "6"], "section_status": {"7": "ok_pit_deterministic"}, "chars": len(hk_financial_section)}
    return {"ok": False, "status": "failed", "text": "", "failed_sections": [sk], "section_status": {}, "chars": 0}


def _run_markdown_section_task(section_no: str, materials: dict, ref_map: dict, key_data: dict, assigned: dict,
                               company_name: str, ticker: str, mkt: str, guide: str,
                               target_materials_ok: bool, key: str) -> dict:
    h2_map = {
        "5": ["5 业务拆分"],
        "6": ["6 产销链与生态"],
        "7": ["7 财务与盈利质量"],
    }
    if section_no == "7" and mkt == "HK":
        text = _build_hk_financial_section(materials, ref_map)
        return {
            "ok": bool(text),
            "status": "ok_pit_deterministic" if text else "failed_missing_pit",
            "text": text,
            "failed_sections": [] if text else ["7"],
            "section_status": {"7": "ok_pit_deterministic" if text else "failed_missing_pit"},
            "chars": len(text or ""),
        }
    mats = _build_section_context("s57", key_data, assigned, ref_map)
    if len(mats) < 100 or not target_materials_ok:
        return {"ok": False, "status": "failed_sparse", "text": "", "failed_sections": [section_no], "section_status": {section_no: "failed_sparse"}, "chars": 0}
    if not key:
        return {"ok": False, "status": "skipped", "text": "", "failed_sections": [section_no], "section_status": {section_no: "skipped"}, "chars": 0}
    prompt = _build_section_prompt("s57", mats, company_name, ticker, mkt, h2_override=h2_map[section_no])
    prompt += f"\n\n---\nREFERENCE GUIDE (only use these [N] numbers, 1-{len(ref_map)}):\n{guide}\n\nDo NOT invent new [N] numbers beyond range 1-{len(ref_map)}."
    text, ok = _call_llm(prompt, max_tokens=4500, timeout=min(120, _hkus_llm_task_budget_seconds()),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS, call_name=f"section_{section_no}")
    body = _clean_fence(text) if ok and text else ""
    if ok and len(body.strip()) > 80 and f"## {section_no}" in body:
        return {"ok": True, "status": "ok", "text": body, "failed_sections": [], "section_status": {section_no: "ok"}, "chars": len(body)}
    return {"ok": False, "status": "failed", "text": "", "failed_sections": [section_no], "section_status": {section_no: "failed"}, "chars": 0}


# ---------------------------------------------------------------------------
# section-wise report generation
# ---------------------------------------------------------------------------
# §9 peer LLM fallback
# ---------------------------------------------------------------------------

_PEER_TABLE_COLS_CN = "竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品"


def _build_manual_report_from_materials(materials: dict, ref_map: dict, key_data: dict,
                                         company_name: str, ticker: str, mkt: str) -> dict:
    """无 LLM 时从采集材料手动拼装各章节，标注引用来源。"""
    texts: dict[str, str] = {}
    target_reports = key_data.get("target_matched_reports", []) or []

    # §1 关键要点：取前4篇研报标题+摘要作为要点
    s1_lines = ["## 1 关键要点", ""]
    for i, rd in enumerate(target_reports[:4]):
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        org = rd.get("orgName", "") or "--"
        title = _plain_text(rd.get("articleTitle") or rd.get("title") or "", 80)
        rating = rd.get("rating", "") or ""
        tp = rd.get("targetPrice", "") or ""
        point = f"**{org}**：{title}"
        if rating:
            point += f"；评级{rating}"
        if tp:
            point += f"；目标价{tp}"
        s1_lines.append(f"- {point}[{rn}]")
    if len(s1_lines) > 2:
        texts["s12"] = "\n".join(s1_lines)

    # §2 近况跟踪：取最新研报摘要拼装
    s2_lines = ["## 2 近况跟踪", ""]
    for rd in target_reports[:5]:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        date = str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:10]
        abstract = _plain_text(rd.get("textAbstract") or rd.get("summary") or "", 150)
        if abstract:
            s2_lines.append(f"- **{date}**：{abstract}[{rn}]")
    if len(s2_lines) > 2:
        s2_text = "\n".join(s2_lines)
        texts["s12"] = texts.get("s12", "") + "\n\n" + s2_text if texts.get("s12") else s2_text

    # §3 核心投资逻辑：从摘要提炼
    s3_lines = ["## 3 核心投资逻辑", "", "### 3.1 短期逻辑", ""]
    for rd in target_reports[:3]:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        abstract = _plain_text(rd.get("textAbstract") or "", 200)
        if abstract:
            s3_lines.append(f"- {abstract}[{rn}]")
    s3_lines.extend(["", "### 3.2 长期逻辑", ""])
    for rd in target_reports[3:6]:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        abstract = _plain_text(rd.get("textAbstract") or "", 200)
        if abstract:
            s3_lines.append(f"- {abstract}[{rn}]")

    # §4 催化事件：直接用研报标题
    s4_lines = ["## 4 催化事件时间表", "", "| 时间 | 事件 | 影响 |", "|:---|:---|:---|"]
    for rd in target_reports[:6]:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        date = str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:7]
        title = _plain_text(rd.get("articleTitle") or rd.get("title") or "", 60)
        if date and title:
            s4_lines.append(f"| {date} | {title}[{rn}] | 参见研报[{rn}] |")

    texts["s34"] = "\n".join(s3_lines) + "\n\n" + "\n".join(s4_lines)

    # §12 风险提示：从摘要提炼
    s12r_lines = ["## 12 风险提示", ""]
    for rd in target_reports[:4]:
        rn = _find_ref_no(ref_map, str(rd.get("articleId", "")))
        if rn <= 0:
            continue
        abstract = _plain_text(rd.get("textAbstract") or "", 100)
        sentences = [s.strip() for s in re.split(r'[。；;]', abstract) if s.strip()]
        risk_sent = next((s for s in sentences if re.search(r'风险|不确定|压力|挑战|下行|监管', s)), "")
        if risk_sent:
            s12r_lines.append(f"- **风险关注**：{risk_sent}[{rn}]")
    if len(s12r_lines) > 2:
        texts["s12r"] = "\n".join(s12r_lines)

    return texts


def _build_peer_llm_fallback(key_data: dict, ref_map: dict,
                              company_name: str, ticker: str, mkt: str) -> str:
    """直接让 LLM 从研报材料中读取同行公司，生成 §9 行业对比表。
    要求：目标公司为第一行 + 至少3家来自研报的同行 = 共至少4行数据。
    """
    guide = "\n".join(f"  [{rn}] {v.get('title','')[:80]}" for rn, v in sorted(ref_map.items())[:20])
    peer_context = key_data.get("peer_context_text") or "(no separate peer context extracted)"
    prompt = f"""你是资深股票分析师。请为 {company_name}（{ticker}，{mkt} 市场）生成 §9 行业对比表。

任务：从下方研报材料中提取同行/可比公司，生成对比表。

规则：
1. 第一行必须是目标公司（竞争关系填"基准公司"）
2. 从研报中至少找出 3 家真实同行或可比公司（合计至少 4 行）
3. 同行公司必须来自研报中明确提及的竞争对手或可比公司，不要编造
4. 每个非目标 peer 行的引用 [N] 对应证据中必须出现该 peer 的公司名或 ticker；不要把目标公司研报内容填到 peer 行
5. 如果研报中提到的同行不足3家，尽量列出所有能找到的；不要在表格外输出任何解释性文字或数据来源说明
6. 相关业务进展：填写研报中提到该 peer 的最新具体进展，并引用 [N]
7. 不要输出"未生成"或"暂无"——始终输出最佳可用表格

输出格式（仅输出 Markdown，不要解释）：

## 9 行业对比与 A/H 映射

| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品 |
|:--|:--|:--|:--|:--|:--|:--|:--|:--|
| 基准公司 | {company_name}（{ticker}） | {mkt} | ... | ... | ...[N] | ... | ... | ... |
| 直接竞争 | 公司名（代码） | 市场 | ... | ... | ...[N] | ... | ... | ... |

参考资料索引（引用时使用 [N]）：
{guide}

目标公司研报摘录（从中提取同行信息）：
{key_data.get('recent_reports_text', '')[:4000]}

同业/可比公司证据摘录（优先用于同行行，必须引用 [N]）：
{peer_context[:3500]}
"""
    text, ok = _call_llm(prompt, max_tokens=2500,
                         timeout=min(120, _hkus_llm_task_budget_seconds()),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                         call_name="section_9_peer_llm")
    if not ok or not text:
        return ""
    body = _clean_fence(text)
    if "|" not in body or "## 9" not in body:
        return ""
    return _sanitize_markdown_table_cells(body)

def _sanitize_markdown_table_cells(markdown: str) -> str:
    lines: list[str] = []
    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "|" not in stripped[1:]:
            lines.append(line)
            continue
        if re.match(r'^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$', stripped):
            lines.append(line)
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and cells[0] == "竞争关系":
            lines.append("| " + " | ".join(cells) + " |")
            continue
        lines.append("| " + " | ".join(normalize_refs(cell, []) for cell in cells) + " |")
    return "\n".join(lines)

def _peer_table_data_rows(peer_section: str) -> list[str]:
    rows: list[str] = []
    for line in (peer_section or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "|" not in stripped[1:]:
            continue
        if re.match(r'^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$', stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if any(c in ("竞争关系", "公司", "公司（代码）") for c in cells[:2]):
            continue
        if len(cells) >= 5:
            rows.append(stripped)
    if rows:
        return rows
    # Some models return a whole Markdown table on one physical line. Rebuild
    # data rows by chunking pipe cells after the header.
    text = _clean_fence(peer_section or "")
    if text.count("|") < 12:
        return rows
    cells = [c.strip() for c in text.split("|") if c.strip()]
    header_idx = -1
    for i, cell in enumerate(cells):
        if cell in ("竞争关系", "公司（代码）", "公司"):
            header_idx = i
            break
    if header_idx < 0:
        return rows
    header = []
    i = header_idx
    while i < len(cells) and not re.fullmatch(r':?-{2,}:?', cells[i]):
        header.append(cells[i])
        i += 1
    width = len(header)
    if width < 5:
        return rows
    while i < len(cells) and re.fullmatch(r':?-{2,}:?', cells[i]):
        i += 1
    for start in range(i, len(cells), width):
        chunk = cells[start:start + width]
        if len(chunk) == width and chunk[0] != "竞争关系":
            rows.append("| " + " | ".join(chunk) + " |")
    return rows

def _peer_ref_evidence_map(key_data: dict | None) -> dict[int, str]:
    evidence: dict[int, list[str]] = {}
    if not isinstance(key_data, dict):
        return {}
    for source_key in ("peer_context_text", "recent_reports_text"):
        for line in str(key_data.get(source_key) or "").splitlines():
            refs = []
            for raw in re.findall(r'\[(\d+)\]', line):
                try:
                    refs.append(int(raw))
                except ValueError:
                    continue
            for rn in refs:
                evidence.setdefault(rn, []).append(_plain_text(line, 600))
    return {rn: "\n".join(lines) for rn, lines in evidence.items()}


def _peer_company_terms(company_cell: str) -> list[str]:
    cell = re.sub(r'\[[0-9]+\]', '', str(company_cell or ""))
    terms: list[str] = []
    paren_terms = re.findall(r'[（(]([^）)]+)[）)]', cell)
    base = re.sub(r'[（(].*?[）)]', '', cell)
    candidates = [base] + paren_terms + re.split(r'[/,，、\s]+', cell)
    for term in candidates:
        t = _plain_text(term, 40).strip()
        t = re.sub(r'\b(HK|US|NASDAQ|NYSE|SEHK|SZ|SH)\b', '', t, flags=re.I).strip()
        if not t:
            continue
        if len(t) < 2 and not re.fullmatch(r'[A-Z]{1,5}', t):
            continue
        if t not in terms:
            terms.append(t)
    return terms[:6]


def _row_refs(row: str) -> list[int]:
    refs: list[int] = []
    for raw in re.findall(r'\[(\d+)\]', row or ""):
        try:
            refs.append(int(raw))
        except ValueError:
            continue
    return list(dict.fromkeys(refs))


def _validate_peer_llm_section(peer_section: str, company_name: str, ticker: str,
                               key_data: dict | None = None) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not peer_section:
        return False, ["section_9.empty"]
    rows = _peer_table_data_rows(peer_section)
    if len(rows) < 4:
        issues.append(f"section_9.peer_rows:{len(rows)}<4")
    target_hits = 0
    target_code = (ticker or "").upper().replace(".HK", "")
    ref_evidence = _peer_ref_evidence_map(key_data)
    for row in rows:
        company_cell = _peer_row_company_cell(row)
        is_target = (company_name and company_name in company_cell) or (target_code and target_code in company_cell.upper().replace(".HK", ""))
        if is_target:
            target_hits += 1
            continue
        refs = _row_refs(row)
        if not refs:
            issues.append(f"section_9.peer_row_no_refs:{company_cell}")
            continue
        evidence_text = "\n".join(ref_evidence.get(rn, "") for rn in refs)
        if not evidence_text:
            issues.append(f"section_9.peer_row_no_ref_evidence:{company_cell}")
            continue
        terms = _peer_company_terms(company_cell)
        if terms and not any(term.lower() in evidence_text.lower() for term in terms):
            issues.append(f"section_9.peer_row_company_not_in_evidence:{company_cell}")
    if target_hits != 1:
        issues.append(f"section_9.target_rows:{target_hits}!=1")
    non_target_rows = max(0, len(rows) - target_hits)
    if non_target_rows < 3:
        issues.append(f"section_9.peer_non_target_rows:{non_target_rows}<3")
    if re.search(r'Comparable peer|peer A|peer B|暂无|未生成|N/A|待补充', peer_section, re.I):
        issues.append("section_9.placeholder_or_generic_peer")
    return not issues, issues

def _peer_row_company_cell(row: str) -> str:
    cells = [c.strip() for c in str(row or "").strip().strip("|").split("|")]
    return cells[1] if len(cells) >= 2 else ""


def write_report(materials: dict, source_trace: dict, ticker: str, market: str,
                 company_name: str, output_dir: str, peer_bundle: dict | None = None) -> tuple:
    """Generate report via section-wise LLM calls. Returns (content, gen_status)."""
    mkt = "HK" if market.lower() == "hk" else "US"
    key, _, model = _find_llm_creds()

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

    # 4. bounded parallel LLM generation. Worker tasks return local results only;
    # texts/status/failed/generation_status are merged below on the main thread.
    texts = {}
    status = {}
    failed = []
    target_price_records = _collect_target_price_records(materials, ref_map, company_name, ticker, mkt)
    target_price_basis_records = _select_target_price_basis_records(target_price_records)
    task_specs: list[LlmTaskSpec] = []
    if key and target_materials_ok:
        task_specs.extend([
            LlmTaskSpec("sections_1_2", lambda: _run_sections_1_2_task(key_data, ref_map)),
            LlmTaskSpec("section_3_short", lambda: _run_section_3_group_task(key_data, ref_map, "short_term_logic")),
            LlmTaskSpec("section_3_long", lambda: _run_section_3_group_task(key_data, ref_map, "long_term_logic")),
            LlmTaskSpec("section_4", lambda: _run_section_4_task(key_data, ref_map)),
            LlmTaskSpec("section_5", lambda: _run_section_5_task(key_data, ref_map)),
            LlmTaskSpec("section_6", lambda: _run_section_6_task(key_data, ref_map)),
            LlmTaskSpec("section_7", lambda: _run_markdown_section_task("7", materials, ref_map, key_data, assigned, company_name, ticker, mkt, guide, target_materials_ok, key)),
            LlmTaskSpec("section_8", lambda: _run_section_8_task(key_data, ref_map, mkt)),
            LlmTaskSpec("section_10_a", lambda: _run_section_10_part_task(key_data, ref_map, "section_10_a")),
            LlmTaskSpec("section_10_b", lambda: _run_section_10_part_task(key_data, ref_map, "section_10_b")),
            LlmTaskSpec("section_12", lambda: _run_section_12_task(key_data, ref_map)),
            LlmTaskSpec("forecast_sample_extraction", lambda: _run_forecast_sample_extraction_task(materials, ref_map, company_name, ticker, mkt)),
        ])
        if target_price_basis_records:
            task_specs.append(LlmTaskSpec("target_price_basis", lambda: _run_target_price_basis_task(target_price_basis_records)))
    else:
        # 无 LLM API Key：用采集到的材料手动生成所有章节，保留引用来源
        print("  [NO-LLM] LLM API Key 不可用，切换到材料直写降级模式（手动撰写+引用标注）")
        manual_texts = _build_manual_report_from_materials(materials, ref_map, key_data, company_name, ticker, mkt)
        texts.update(manual_texts)
        for sec in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"):
            status[sec] = "ok_manual_fallback" if texts.get(f"s{sec}") or texts.get("s12") else "skipped_no_llm"
        task_specs.append(LlmTaskSpec("section_7", lambda: _run_markdown_section_task("7", materials, ref_map, key_data, assigned, company_name, ticker, mkt, guide, target_materials_ok, key)))

    phase2_results, phase2_meta = run_hkus_llm_tasks(
        task_specs,
        max_workers=_hkus_llm_max_workers(),
        total_budget_seconds=_hkus_llm_total_budget_seconds(),
        progress_prefix="[LLM]",
    )
    status["_llm_parallel"] = {
        "max_workers": phase2_meta.get("max_workers"),
        "elapsed_seconds": round(float(phase2_meta.get("elapsed_seconds", 0)), 3),
        "budget_exceeded": bool(phase2_meta.get("budget_exceeded")),
        "observed_max_active_tasks": phase2_meta.get("observed_max_active_tasks"),
        "observed_max_retry_tasks": phase2_meta.get("observed_max_retry_tasks"),
    }
    result_map = {r.task_name: r for r in phase2_results}

    r12 = result_map.get("sections_1_2")
    if r12 and r12.ok and isinstance(r12.content, dict) and r12.content.get("sections"):
        texts.update(r12.content["sections"])
        call_mode = r12.content.get("meta", {}).get("call_mode", "ok_json")
        status["s12"] = call_mode
        status["1"] = status["2"] = call_mode
    else:
        mode = (r12.content or {}).get("status") if r12 and isinstance(r12.content, dict) else ("failed_sparse_or_no_llm" if not key or not target_materials_ok else "failed_schema")
        status["1"] = status["2"] = status["s12"] = mode
        failed.extend(["1", "2"])

    r3_short = result_map.get("section_3_short")
    r3_long = result_map.get("section_3_long")
    r4 = result_map.get("section_4")
    s34_parts = []
    short_rows = r3_short.content.get("rows", []) if r3_short and isinstance(r3_short.content, dict) else []
    long_rows = r3_long.content.get("rows", []) if r3_long and isinstance(r3_long.content, dict) else []
    sec3_text, sec3_ok, sec3_issues = merge_hkus_section_3_groups(short_rows, long_rows)
    sec3_prior_issues = []
    for result in (r3_short, r3_long):
        if result and isinstance(result.content, dict):
            sec3_prior_issues.extend(result.content.get("issues", []) or [])
    if sec3_text and sec3_ok:
        s34_parts.append(sec3_text)
        status["3"] = "partial_json_split" if any("partial_groups" in x for x in sec3_issues) else "ok_json_split"
        if sec3_prior_issues or sec3_issues:
            status["3_schema_issues"] = (sec3_prior_issues + sec3_issues)[:30]
    else:
        combined_text, combined_ok, combined_issues = gen_hkus_section_3(key_data, ref_map) if key and target_materials_ok else ("", False, [])
        if combined_text and combined_ok:
            s34_parts.append(combined_text)
            status["3"] = "ok_json_combined_retry"
            status["3_schema_issues"] = (sec3_prior_issues + sec3_issues + combined_issues)[:30]
        else:
            status["3"] = "failed_json_schema" if key and target_materials_ok else "failed_sparse_or_no_llm"
            status["3_schema_issues"] = (sec3_prior_issues + sec3_issues + combined_issues)[:30]
            failed.append("3")
    if r4 and r4.ok and isinstance(r4.content, dict) and r4.content.get("text"):
        s34_parts.append(r4.content["text"])
        status["4"] = r4.content.get("status", r4.status)
    else:
        status["4"] = "failed_json_schema" if key and target_materials_ok else "failed_sparse_or_no_llm"
        if r4 and isinstance(r4.content, dict):
            status["4_schema_issues"] = r4.content.get("issues", [])[:30]
        failed.append("4")
    texts["s34"] = "\n\n".join(s34_parts)
    status["s34"] = "ok" if len(s34_parts) == 2 else "degraded_partial"
    print(f"    [ch1-4] s12={status.get('s12','?')} s34={status.get('s34','?')}, {len(texts.get('s12','')) + len(texts.get('s34',''))} chars", flush=True)

    s57_parts = []
    for section_no in ("5", "6", "7"):
        result = result_map.get(f"section_{section_no}")
        if result and isinstance(result.content, dict) and result.content.get("text"):
            s57_parts.append(result.content["text"])
            status[section_no] = result.content.get("status", result.status)
            status.update(result.content.get("section_status", {}))
            failed.extend(result.content.get("failed_sections", []))
        else:
            status[section_no] = result.status if result else "failed"
            failed.append(section_no)
    if s57_parts:
        texts["s57"] = "\n\n".join(s57_parts)
        status["s57"] = "ok" if all(status.get(x, "").startswith("ok") for x in ("5", "6", "7")) else "degraded_partial"
        print(f"    [ch5-7] {status['s57']}, {len(texts['s57'])} chars", flush=True)
    else:
        status["s57"] = "failed"
        failed.append("s57")

    sec8_text = ""
    r8 = result_map.get("section_8")
    if r8 and r8.ok and isinstance(r8.content, dict) and r8.content.get("text"):
        sec8_text = r8.content["text"]
        status["8"] = "ok_json"
    else:
        status["8"] = "failed_json_schema" if key and target_materials_ok else "failed_sparse_or_no_llm"
        if r8 and isinstance(r8.content, dict):
            status["8_schema_issues"] = r8.content.get("issues", [])[:30]
        failed.append("8")
    peer_section = ""
    peer_is_fallback = False
    if key and target_materials_ok:
        peer_section = _build_peer_llm_fallback(key_data, ref_map, company_name, ticker, mkt)
        peer_ok, peer_issues = _validate_peer_llm_section(peer_section, company_name, ticker, key_data)
        if peer_ok:
            status["9"] = "ok_peer_llm"
        else:
            peer_section = ""
            status["9"] = "failed_peer_llm_schema"
            status["9_schema_issues"] = (peer_issues + ["section_9.fail_closed_no_peer_context_fallback"])[:20]
            failed.append("9")
    else:
        status["9"] = "failed_no_peer_evidence"
        failed.append("9")
    texts["s89"] = "\n\n".join(x for x in [sec8_text, peer_section] if x)
    status["s89"] = "ok" if sec8_text and peer_section else "degraded_partial"

    sec10_text = ""
    rows_10 = []
    issues_10 = []
    for name in ("section_10_a", "section_10_b"):
        r10p = result_map.get(name)
        if r10p and isinstance(r10p.content, dict):
            if r10p.content.get("rows"):
                rows_10.append(r10p.content["rows"])
            issues_10.extend(r10p.content.get("issues", []) or [])
    sec10_text, sec10_ok, sec10_issues = merge_hkus_section_10_parts(rows_10)
    if sec10_text and sec10_ok:
        status["10"] = "ok_json_split" if not any("partial_rows" in x for x in sec10_issues) else "partial_json_split"
    else:
        retry_text, retry_ok, retry_issues = gen_hkus_section_10_short_retry(key_data, ref_map, issues_10 + sec10_issues)
        if retry_text and retry_ok:
            sec10_text = retry_text
            status["10"] = "partial_json_short_retry" if any("partial_rows" in x for x in retry_issues) else "ok_json_short_retry"
            status["10_schema_issues"] = (issues_10 + sec10_issues + retry_issues)[:30]
        else:
            status["10"] = "failed_json_schema" if key and target_materials_ok else "failed_sparse_or_no_llm"
            status["10_schema_issues"] = (issues_10 + sec10_issues + retry_issues + ["section_10.fail_closed_no_deterministic_fallback"])[:30]
            failed.append("10")
    basis_map = {}
    rbasis = result_map.get("target_price_basis")
    if rbasis and rbasis.ok and isinstance(rbasis.content, dict):
        basis_map = rbasis.content.get("basis_map") or {}
    rforecast = result_map.get("forecast_sample_extraction")
    forecast_samples_payload = {"sample_count": 0, "samples": []}
    if rforecast and isinstance(rforecast.content, dict):
        forecast_samples_payload = rforecast.content.get("forecast_samples") or forecast_samples_payload
    sec11 = _build_valuation_section_group(materials, ref_map, company_name, ticker, mkt, basis_map=basis_map if basis_map else None)
    _save_json(str(Path(output_dir) / "forecast_samples.json"), forecast_samples_payload)
    _save_json(str(Path(output_dir) / "consensus_forecast.json"), materials.get("_consensus_forecast_output") or {"forecast_years": [], "rows": [], "sample_count": 0})
    parts = [sec10_text] if sec10_text else []
    if sec11:
        parts.append(sec11)
        status["11"] = "ok_deterministic"
    else:
        status["11"] = "failed_no_traceable_target_price"
        failed.append("11")
    texts["s1011"] = "\n\n".join(parts)
    status["s1011"] = "degraded_partial" if len(parts) < 2 else "ok_deterministic"
    print(f"    [ch10-11] s10={status.get('10','?')} s11={status.get('11','?')}, {len(texts['s1011'])} chars", flush=True)

    r12 = result_map.get("section_12")
    if r12 and r12.ok and isinstance(r12.content, dict) and r12.content.get("text"):
        texts["s12r"] = r12.content["text"]
        status["12"] = "ok_json"
        status["s12r"] = "ok_json"
    else:
        status["12"] = "failed_json_schema" if key and target_materials_ok else "failed_sparse_or_no_llm"
        status["s12r"] = status["12"]
        if r12 and isinstance(r12.content, dict):
            status["12_schema_issues"] = r12.content.get("issues", [])[:30]
        failed.append("12")

    # 5. Extract title conclusion: JSON _title_conclusion first, then §1 text fallback
    mkt_cn = "港股" if mkt == "HK" else "美股"
    conclusion = texts.get("_title_conclusion", "")
    if not conclusion:
        conclusion = _derive_title_conclusion(texts.get("s12", ""), company_name, mkt_cn, ticker)
    if not conclusion and key:
        conclusion = _repair_title_from_verified_sections(texts, company_name, ticker, mkt_cn)
    if not conclusion:
        conclusion = _build_deterministic_fallback_title(texts, company_name, ticker)
    title = _build_report_title(company_name, ticker, mkt_cn, conclusion)
    meta = _build_hkus_meta_line(mkt_cn)
    report, assembly_issues, assembly_meta = assemble_fixed_hk_us_sections(title, meta, texts, ref_text, mkt=mkt, return_meta=True)

    # title_status 以最终写入 report.md 的标题为准（_build_report_title 内部的
    # _sanitize_title_conclusion 可能二次清空 conclusion，需在 assemble 后重新判断）
    _title_in_report = re.match(r'^#\s+.+：(.+)$', title)
    title_status = "ok" if _title_in_report else "failed_no_investment_conclusion"
    if not _title_in_report:
        assembly_issues.append("title_status=failed_no_investment_conclusion")

    # 6. normalize references (keep only body-used refs, renumber 1..N)
    report, ref_map, reference_issues = normalize_used_references(report, source_trace)
    assembly_issues.extend(reference_issues)
    report = _normalize_table_separators(report)
    report = _clean_repeated_punctuation(report)

    # 7. status
    h2_matches = list(re.finditer(r'^##\s*(\d{1,2})\s+[^\n]+$', report, re.M))
    ref_match = _find_reference_heading_match(report)
    body_h2_matches = [m for m in h2_matches if not re.search(r'参考资料|References', m.group(0), re.I)]
    section_h2_status = {}
    for match in body_h2_matches:
        idx = h2_matches.index(match)
        end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else (ref_match.start() if ref_match else len(report))
        body = report[match.end():end].strip()
        section_h2_status[str(match.group(1))] = "ok" if body and _is_substantive_section(body) else "failed"
    ok_n = sum(1 for v in section_h2_status.values() if v == "ok")
    failed_h2 = [k for k, v in section_h2_status.items() if v != "ok"]
    is_skel = ok_n == 0
    is_deg = bool(failed_h2) or len(failed) > 0 or bool(assembly_issues)
    gs = {
        "total_sections": len(section_h2_status), "ok_sections": ok_n,
        "failed_sections": failed_h2,
        "failed_groups": failed,
        "section_status": status, "is_degraded": is_deg, "is_skeleton": is_skel,
        "mode": "skeleton" if is_skel else ("degraded" if is_deg else "full"),
        "generated_at": TODAY_ISO, "ref_count": len(ref_map),
        "assembly_issues": assembly_issues,
        "section_number_mapping": assembly_meta.get("section_number_mapping", {}),
        "omitted_original_sections": assembly_meta.get("omitted_original_sections", []),
        "display_sections_total": assembly_meta.get("display_sections_total", len(section_h2_status)),
        "reference_display_section": assembly_meta.get("reference_display_section", ""),
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
        "peer_discovery_status": "llm_from_research",
    }

    rp = Path(output_dir) / "report.md"
    rp.write_text(report, encoding="utf-8")
    tag = "SKELETON" if is_skel else ("DEGRADED" if is_deg else "FULL")
    print(f"  [{tag}] {ok_n}/12 sections OK, {len(ref_map)} refs -> {rp}")
    return (report, gs)


def _build_hkus_meta_line(market_cn: str, generation_date: str = TODAY) -> str:
    return f"\n市场：{market_cn} | 生成日期：{generation_date}\n"

# ── Fixed section shell & normalization ──

FIXED_H2_TITLES = [
    "1 关键要点", "2 近况跟踪", "3 核心投资逻辑",
    "4 催化事件时间表", "5 业务拆分", "6 产销链与生态",
    "7 财务与盈利质量", "8 市场关注", "9 行业对比与 A/H 映射",
    "10 市场分歧", "11 估值与预测", "12 风险提示", "13 参考资料",
]

# 必填章节（失败时在 generation_status 中记录，不写空 H2）
_REQUIRED_SECTIONS = {1, 2, 5, 7, 12}


def _fixed_h2_titles_for_market(mkt: str) -> list[str]:
    """返回按市场调整后的 H2 标题列表（HK §8 含调研大纲）。"""
    titles = list(FIXED_H2_TITLES)
    if (mkt or "").upper() == "HK":
        titles[7] = "8 市场关注/调研大纲"
    return titles

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
    """Return {section_number: full H2 section} from a generated markdown chunk."""
    text = _clean_fence(text)
    matches = list(re.finditer(r'^##\s*(\d{1,2})\s+[^\n]*$', text, re.M))
    if not matches:
        return {}
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[num] = body
    return sections

def _chunk_without_h2(text: str) -> str:
    return _strip_section_headers(_clean_fence(text)).strip()

def _fallback_chunk_split_group(sec_key: str, text: str) -> dict[int, str]:
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


def _renumber_markdown_section(body: str, original_no: int, display_no: int) -> str:
    if original_no == display_no:
        return body
    body = re.sub(r'^(##\s*)%d(\s+)' % original_no,
                  lambda m: f"{m.group(1)}{display_no}{m.group(2)}",
                  body, count=1, flags=re.M)
    body = re.sub(r'^(#{3,6}\s*)%d\.' % original_no,
                  lambda m: f"{m.group(1)}{display_no}.",
                  body, flags=re.M)
    return body


def _find_reference_heading_match(report: str):
    return re.search(r'^##\s*(\d{1,2})\s+(参考资料|References)\s*$', report, re.M | re.I)


def _validate_final_hk_us_sections(report: str, sec11_dropped: bool = False) -> list[str]:
    issues = []
    h2_matches = list(re.finditer(r'^##\s*(\d{1,2})\s+([^\n]+)$', report, re.M))
    ref_match = next((m for m in h2_matches if re.search(r'参考资料|References', m.group(2), re.I)), None)
    body_matches = [m for m in h2_matches if not re.search(r'参考资料|References', m.group(2), re.I)]
    expected_nums = list(range(1, len(body_matches) + 1))
    actual_nums = [int(m.group(1)) for m in body_matches]
    if actual_nums != expected_nums:
        issues.append(f"non_continuous_h2_numbers:{actual_nums}!={expected_nums}")
    if ref_match:
        expected_ref_no = len(body_matches) + 1
        if int(ref_match.group(1)) != expected_ref_no:
            issues.append(f"reference_h2_number:{ref_match.group(1)}!={expected_ref_no}")
    else:
        issues.append("missing_reference_section")
    for m in body_matches:
        idx = h2_matches.index(m)
        end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else len(report)
        body = report[m.end():end].strip()
        if not _is_substantive_section(body):
            issues.append(f"empty_or_placeholder_h2:§{m.group(1)}")
    if re.search(r'生成失败|LLM不可用|degraded|fallback failed', report, re.I):
        issues.append("forbidden_degraded_marker_in_report")
    return issues

def _sec11_has_content(section_bodies: dict) -> bool:
    """§11（估值与预测）是否有实质内容。s1011 包含 §10+§11，需单独检测 §11 部分。"""
    s1011 = section_bodies.get("s1011", "")
    if not s1011:
        return False
    # 提取 §11 区块（## 11 ... 到下一个 ## 或末尾）
    m = re.search(r'^##\s*11\s+[^\n]*$(.*?)(?=^##\s*\d+\s+|\Z)', s1011, re.M | re.S)
    if not m:
        return False
    return _is_substantive_section(m.group(1))


def assemble_fixed_hk_us_sections(title: str, meta: str, section_bodies: dict,
                                  ref_text: str, mkt: str = "US",
                                  return_meta: bool = False) -> tuple:
    """Assemble report in fixed order.

    规则：
    - 有实质 body 的章节：保留 body 中已有的 ## H2（renderer 已生成正确标题则直接用），
      否则插入 fixed H2 标题。
    - 无 body 的章节：不写空 H2；必填章节（_REQUIRED_SECTIONS）将在 assembly_issues 中记录。
    - §8 标题按 mkt 参数选择正确版本（HK="市场关注/调研大纲"，US="市场关注"）。
    - 任一章节缺失时：后续章节连续编号，参考资料跟随最后一个正文编号。
    """
    h2_titles = _fixed_h2_titles_for_market(mkt)

    routed: dict[int, str] = {}
    for sk in SECTION_ORDER:
        if sk not in section_bodies:
            continue
        extracted = _extract_numbered_h2_sections(section_bodies[sk])
        if not extracted:
            extracted = _fallback_chunk_split_group(sk, section_bodies[sk])
        expected = {int(h.split()[0]) for h in SECTION_MATERIAL_MAP[sk]["h2"]}
        for num, body in extracted.items():
            if num in expected and body.strip():
                routed[num] = body.strip()

    assembly_issues: list[str] = []
    section_number_mapping: dict[str, str | None] = {}
    omitted_original_sections: list[str] = []

    parts = [title, meta]
    display_no = 1
    for h in h2_titles[:-1]:  # 不含"13 参考资料"，参考资料单独追加
        sec_no = int(h.split()[0])
        body = routed.get(sec_no, "").strip()
        if body:
            rest = h.split(" ", 1)[1] if " " in h else h
            disp_h = f"{display_no} {rest}"
            body = _renumber_markdown_section(body, sec_no, display_no)
            # 如果 body 里已有正确编号的 H2，替换编号后直接使用；否则插入 fixed H2
            if re.match(r'^##\s*%d\s+' % display_no, body):
                parts.append("\n" + body + "\n")
            else:
                parts.append(f"\n## {disp_h}\n")
                parts.append(body)
            if display_no != sec_no:
                assembly_issues.append(f"renumbered_section:§{sec_no}->§{display_no}")
            section_number_mapping[str(sec_no)] = str(display_no)
            display_no += 1
        else:
            section_number_mapping[str(sec_no)] = None
            omitted_original_sections.append(str(sec_no))
            if sec_no in _REQUIRED_SECTIONS:
                assembly_issues.append(f"required_section_missing:§{sec_no}")
            else:
                assembly_issues.append(f"optional_section_missing:§{sec_no}")

    # 参考资料：跟随最后一个实际正文编号。
    ref_disp_no = display_no
    section_number_mapping["13"] = str(ref_disp_no)
    ref_text_out = re.sub(r'^##\s*\d+\s+', f"## {ref_disp_no} ", ref_text, count=1, flags=re.M)
    parts.append(ref_text_out)
    report = "\n".join(parts)
    assembly_issues.extend(_validate_final_hk_us_sections(report))
    assembly_meta = {
        "section_number_mapping": section_number_mapping,
        "omitted_original_sections": omitted_original_sections,
        "display_sections_total": max(0, display_no - 1),
        "reference_display_section": str(ref_disp_no),
    }
    if return_meta:
        return report, assembly_issues, assembly_meta
    return report, assembly_issues


# ── Valuation and financial section builders ──

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


def _build_valuation_section_group(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str,
                                   basis_map: dict[str, dict] | None = None) -> str:
    return _build_valuation_section(materials, ref_map, co, ticker, mkt, basis_map=basis_map)


def _target_aliases(co: str, ticker: str) -> tuple[set[str], set[str]]:
    co = str(co or "")
    t = (ticker or "").upper().replace(".HK", "")
    ticker_aliases = {t, ticker.upper()} if ticker else set()
    if t.isdigit():
        ticker_aliases.add(t.lstrip("0") or t)
    name_aliases = {co, co.lower(), co.upper()} if co else set()
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
        note = f"数据来源：港股 PIT 利润表、资产负债表及现金流量表[{is_ref}][{bs_ref}][{cf_ref}]。"
    else:
        note = (
            f"当前仅取得{', '.join(fin_period_labels)}，不把单期数据写成三期趋势。\n\n"
            f"数据来源：港股 PIT 利润表、资产负债表及现金流量表[{is_ref}][{bs_ref}][{cf_ref}]。"
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
    """Extract concrete assumption data from a report. Used as fallback when LLM extraction is unavailable."""
    evidence = _target_price_evidence_excerpt(rd)[:300]
    if not evidence:
        return "估值方法未披露"
    # Extract concrete factual phrases from evidence
    concrete = []
    patterns = [
        (r'(\d{4}年[^，。；\n]{4,25}?(?:出货|量产|增长|提升|渗透|份额|收入|毛利率|ASP))', 1),
        (r'(光互连[^，。；\n]{3,30})', 1),
        (r'((?:手机|车载|光|AR|机器人)[^，。；\n]{4,25}?(?:占比|增速|出货|订单|客户|认证|定点))', 1),
    ]
    seen = set()
    for pattern, group in patterns:
        for match in re.finditer(pattern, evidence):
            txt = match.group(group).strip()
            if txt not in seen and len(txt) >= 6:
                seen.add(txt)
                concrete.append(txt)
    if concrete:
        return "；".join(concrete[:3])
    return "估值方法未披露"

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
        report_text = _plain_text(" ".join(str(rd.get(k, "") or "") for k in ("content", "text", "reportText", "textAbstract", "summary", "articleTitle", "title")), 2000)
        records.append({
            "target": tp_val,
            "org": rd.get("orgName", "") or "--",
            "rn": rn,
            "rating": rd.get("rating", "") or "未明示",
            "date": str(rd.get("publishTimeReadable") or rd.get("publishTime") or "")[:10],
            "article_id": str(rd.get("articleId", "")),
            "title": _plain_text(rd.get("articleTitle") or rd.get("title") or "", 160),
            "abstract": _plain_text(rd.get("textAbstract") or rd.get("summary") or "", 800),
            "report_text": report_text,
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


def _select_target_price_basis_records(records: list[dict], max_records: int = TARGET_PRICE_BASIS_MAX_RECORDS) -> list[dict]:
    """Pick representative target-price reports for §11 basis extraction."""
    if len(records) < 3:
        return []
    ordered = list(records)
    picks: list[dict] = []

    def _add(rec: dict) -> None:
        aid = str(rec.get("article_id") or "")
        if aid and all(str(x.get("article_id") or "") != aid for x in picks):
            picks.append(rec)

    _add(ordered[0])
    _add(ordered[len(ordered) // 2])
    _add(ordered[-1])
    evidence_ranked = sorted(
        ordered,
        key=lambda r: (
            bool(_plain_text(r.get("assumption") or "", 80)),
            bool(_plain_text(r.get("evidence") or "", 80)),
            str(r.get("date") or ""),
        ),
        reverse=True,
    )
    for rec in evidence_ranked:
        if len(picks) >= max_records:
            break
        _add(rec)
    return picks[:max_records]


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

def _basis_core_text(basis_map: dict[str, dict], rec: dict) -> str:
    info = basis_map.get(str(rec.get("article_id")), {})
    text = info.get("key_assumptions") or rec.get("assumption") or "估值方法未披露"
    return _plain_text(re.sub(r'\[\d+\]', '', text), 90)


def _nearest_median_record(records: list[dict], median_value: float) -> dict:
    return min(records, key=lambda r: (abs(float(r.get("target") or 0) - median_value), str(r.get("date") or ""))) if records else {}


def _rating_distribution(records: list[dict]) -> str:
    counts = {}
    for rec in records:
        rating = rec.get("rating") or "未明示"
        counts[rating] = counts.get(rating, 0) + 1
    return "、".join(f"{k}{v}家" for k, v in counts.items())


def _normalize_forecast_metric(metric: str) -> str:
    text = _plain_text(metric, 40)
    compact = re.sub(r'\s+', '', text).lower()
    if re.search(r'revenue|sales|营业收入|收入', compact, re.I):
        return "营业收入"
    if re.search(r'netprofit|净利润|归母净利润|non-gaap净利润|nongaap净利润', compact, re.I):
        return "净利润"
    if re.search(r'eps|每股收益', compact, re.I):
        return "EPS"
    return text


def _normalize_forecast_year(year: str) -> str:
    text = _plain_text(year, 20).upper().replace(" ", "")
    m = re.search(r'(FY|CY)?(20\d{2})E?', text)
    if not m:
        return text
    prefix = m.group(1) or "FY"
    return f"{prefix}{m.group(2)}E"


def _forecast_value_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "")
    m = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(m.group(0)) if m else None


def _standardize_forecast_unit(value: float, unit: str, currency: str) -> tuple[float, str, str]:
    unit_text = _plain_text(unit, 30)
    cur = _plain_text(currency, 12)
    if not cur:
        m = re.search(r'\b(HKD|USD|RMB|CNY|EUR|GBP)\b', unit_text, re.I)
        cur = m.group(1).upper() if m else unit_text
    if re.search(r'百万|mn|million', unit_text, re.I):
        return value / 100.0, f"{cur}亿元", f"{value:g}{unit_text}/100"
    if re.search(r'十亿|bn|billion', unit_text, re.I):
        return value * 10.0, f"{cur}亿元", f"{value:g}{unit_text}*10"
    return value, unit_text or cur, ""


def _forecast_samples_from_research(materials: dict) -> list[dict]:
    rows: list[dict] = []
    for rd in materials.get("research", {}).get("details", []) or []:
        if not isinstance(rd, dict):
            continue
        article_id = str(rd.get("articleId") or rd.get("article_id") or "").strip()
        institution = rd.get("orgName") or rd.get("institution") or rd.get("org")
        explicit = rd.get("forecast_samples") or rd.get("forecastSamples") or rd.get("earningForecasts")
        if isinstance(explicit, list):
            for item in explicit:
                if isinstance(item, dict):
                    merged = dict(item)
                    merged.setdefault("article_id", article_id)
                    merged.setdefault("institution", institution)
                    rows.append(merged)
    return rows


def _median(values: list[float]) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _extract_forecast_samples(materials: dict) -> list[ForecastSample]:
    pools = []
    cf = materials.get("consensus_forecast") if isinstance(materials.get("consensus_forecast"), dict) else {}
    for key in ("samples", "forecast_samples"):
        if isinstance(cf.get(key), list):
            pools.extend(cf.get(key) or [])
    if isinstance(materials.get("forecast_samples"), list):
        pools.extend(materials.get("forecast_samples") or [])
    pools.extend(_forecast_samples_from_research(materials))
    samples: list[ForecastSample] = []
    seen = set()
    for item in pools:
        if not isinstance(item, dict):
            continue
        institution = _plain_text(item.get("institution") or item.get("org") or item.get("orgName"), 60)
        article_id = _plain_text(item.get("article_id") or item.get("articleId") or item.get("id"), 80)
        metric = _normalize_forecast_metric(item.get("metric") or item.get("name"))
        forecast_year = _normalize_forecast_year(item.get("forecast_year") or item.get("year") or item.get("period"))
        currency = _plain_text(item.get("currency") or item.get("ccy") or "", 12)
        raw_unit = _plain_text(item.get("unit") or item.get("raw_unit") or currency or "", 30)
        unit = _plain_text(item.get("standardized_unit") or raw_unit, 30)
        accounting_basis = _plain_text(item.get("accounting_basis") or item.get("basis") or item.get("gaap_basis") or "reported", 40)
        period_basis = "CY" if forecast_year.startswith("CY") else "FY"
        source_id = _plain_text(item.get("source_id") or article_id, 80)
        raw_value = _plain_text(item.get("raw_value") if item.get("raw_value") is not None else item.get("value"), 40)
        value = _forecast_value_float(item.get("value") if item.get("value") is not None else raw_value)
        if not (institution and article_id and metric and forecast_year and unit and accounting_basis and value is not None):
            continue
        standardized_value, standardized_unit, conversion_formula = _standardize_forecast_unit(value, unit, currency)
        dedupe_key = (
            re.sub(r'\W+', '', institution.lower()),
            article_id,
            metric,
            forecast_year,
            standardized_unit,
            accounting_basis.lower(),
            period_basis,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        samples.append(ForecastSample(
            institution=institution,
            article_id=article_id,
            metric=metric,
            forecast_year=forecast_year,
            value=standardized_value,
            unit=standardized_unit,
            accounting_basis=accounting_basis,
            source_id=source_id,
            raw_value=raw_value,
            currency=currency,
            period_basis=period_basis,
            raw_unit=raw_unit,
            standardized_value=standardized_value,
            standardized_unit=standardized_unit,
            conversion_formula=conversion_formula,
        ))
    return samples


def _aggregate_forecast_samples(samples: list[ForecastSample]) -> dict:
    grouped: dict[tuple[str, str, str, str, str, str], list[ForecastSample]] = {}
    for sample in samples:
        key = (sample.metric, sample.forecast_year, sample.currency, sample.unit, sample.accounting_basis, sample.period_basis)
        grouped.setdefault(key, []).append(sample)
    rows = []
    for (metric, forecast_year, currency, unit, accounting_basis, period_basis), items in sorted(grouped.items()):
        institutions = sorted({s.institution for s in items})
        if len(institutions) < 3:
            continue
        values = [s.value for s in items]
        rows.append({
            "metric": metric,
            "forecast_year": forecast_year,
            "currency": currency,
            "unit": unit,
            "accounting_basis": accounting_basis,
            "period_basis": period_basis,
            "aggregation_method": "institution_forecast_median",
            "sample_count": len(institutions),
            "consensus_value": _median(values),
            "institution": institutions,
            "article_id": sorted({s.article_id for s in items}),
            "source_id": sorted({s.source_id for s in items}),
            "samples": [
                {
                    "institution": s.institution,
                    "article_id": s.article_id,
                    "source_id": s.source_id,
                    "raw_value": s.raw_value,
                    "standardized_value": s.standardized_value,
                    "raw_unit": s.raw_unit,
                    "unit": s.unit,
                    "currency": s.currency,
                    "accounting_basis": s.accounting_basis,
                    "period_basis": s.period_basis,
                    "conversion_formula": s.conversion_formula,
                }
                for s in items
            ],
        })
    forecast_years = sorted({row["forecast_year"] for row in rows})[:3]
    return {
        "forecast_years": forecast_years,
        "aggregation_method": "institution_forecast_median" if rows else "",
        "sample_count": sum(row["sample_count"] for row in rows),
        "article_ids": sorted({aid for row in rows for aid in row["article_id"]}),
        "rows": rows,
    }


def _forecast_samples_payload(samples: list[ForecastSample]) -> dict:
    return {
        "sample_count": len(samples),
        "samples": [
            {
                "institution": s.institution,
                "article_id": s.article_id,
                "source_id": s.source_id,
                "metric": s.metric,
                "forecast_year": s.forecast_year,
                "currency": s.currency,
                "unit": s.unit,
                "accounting_basis": s.accounting_basis,
                "period_basis": s.period_basis,
                "raw_value": s.raw_value,
                "standardized_value": s.standardized_value,
                "conversion_formula": s.conversion_formula,
            }
            for s in samples
        ],
    }


def _forecast_year_labels(materials: dict) -> list[str]:
    samples = _extract_forecast_samples(materials)
    years = sorted({_normalize_forecast_year(s.forecast_year) for s in samples})
    if years:
        while len(years) < 3:
            m = re.search(r'(FY|CY)(20\d{2})E', years[-1])
            if not m:
                break
            years.append(f"{m.group(1)}{int(m.group(2)) + 1}E")
        return years[:3]
    base = max(datetime.now().year + 1, 2027)
    return [f"FY{base+i}E" for i in range(3)]


def _build_consensus_forecast_section(materials: dict, targets: list[dict] | None = None,
                                      stats: dict | None = None, unit: str = "") -> tuple[str, dict]:
    machine = _aggregate_forecast_samples(_extract_forecast_samples(materials))
    labels = machine.get("forecast_years") or _forecast_year_labels(materials)
    while len(labels) < 3:
        m = re.search(r'(FY|CY)(20\d{2})E', labels[-1] if labels else "")
        labels.append(f"{m.group(1)}{int(m.group(2)) + 1}E" if m else f"FY{datetime.now().year + len(labels) + 1}E")
    if not machine.get("rows"):
        return "", machine
    by_metric: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in machine["rows"]:
        key = (row["metric"], row["unit"], row["accounting_basis"])
        by_metric.setdefault(key, {})
        value = row["consensus_value"]
        by_metric[key][row["forecast_year"]] = f"{value:g}{row['unit']}"
    by_metric = {k: v for k, v in by_metric.items() if sum(1 for year in labels if v.get(year)) >= 2}
    active_years = [year for year in labels if any(values.get(year) for values in by_metric.values())]
    if len(by_metric) < 2 or len(active_years) < 2:
        return "", machine
    lines = ["### 11.1 盈利预测", "", f"| 预测指标 | {' | '.join(active_years)} |", "|:--|" + "|".join(":--" for _ in active_years) + "|"]
    for (metric, row_unit, accounting_basis), values in by_metric.items():
        label = f"{metric}（{row_unit}，{accounting_basis}）"
        lines.append("| " + " | ".join([label] + [values.get(label_year, "") for label_year in active_years]) + " |")
    lines.extend(["", f"盈利预测表按机构样本中位数聚合，未混合不同币种、FY/CY 或 GAAP/non-GAAP 口径。收入、利润与 EPS 的三年变化用于观察盈利弹性是否扩大，下一次财报需重点验证预测最高的业务变量能否兑现。"])
    return "\n".join(lines), machine


def _drop_empty_forecast_columns_in_section_11(text: str) -> str:
    lines = str(text or "").splitlines()
    h_idx = next((i for i, line in enumerate(lines) if re.match(r'^\s*###\s+11\.1\b', line)), -1)
    if h_idx < 0:
        return text
    table_start = -1
    for i in range(h_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_start = i
            break
        if stripped.startswith("### "):
            return text
    if table_start < 0:
        return text
    table_end = table_start
    while table_end < len(lines) and lines[table_end].strip().startswith("|"):
        table_end += 1
    table_lines = lines[table_start:table_end]
    if len(table_lines) < 3:
        return text

    parsed = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in table_lines]
    width = len(parsed[0])
    if width < 4 or any(len(row) != width for row in parsed[:2]):
        return text
    data_rows = [row for row in parsed[2:] if len(row) == width]

    def is_forecast_header(cell: str) -> bool:
        return bool(re.search(r'(?:FY|CY)?20\d{2}\s*E|20\d{2}E', cell, re.I))

    def has_forecast_value(cell: str) -> bool:
        clean = re.sub(r'\[[0-9]+\]', '', str(cell or "")).strip()
        if not clean:
            return False
        if re.fullmatch(r'[-—–~\s]+|N/?A|NA|nan|null|None|鈥?', clean, re.I):
            return False
        return bool(re.search(r'\d', clean))

    keep_indexes: list[int] = []
    dropped_forecast_cols = 0
    for idx, header in enumerate(parsed[0]):
        if is_forecast_header(header):
            if any(has_forecast_value(row[idx]) for row in data_rows):
                keep_indexes.append(idx)
            else:
                dropped_forecast_cols += 1
        else:
            keep_indexes.append(idx)
    if dropped_forecast_cols <= 0 or len(keep_indexes) == width:
        return text

    rebuilt = []
    for row_idx, row in enumerate(parsed):
        if len(row) != width:
            continue
        kept = [row[i] for i in keep_indexes]
        if row_idx == 1:
            rebuilt.append("|" + "|".join(":---" for _ in kept) + "|")
        else:
            rebuilt.append("| " + " | ".join(kept) + " |")
    return "\n".join(lines[:table_start] + rebuilt + lines[table_end:])


def _build_valuation_section(materials: dict, ref_map: dict, co: str, ticker: str, mkt: str,
                             basis_map: dict[str, dict] | None = None) -> str:
    """Build §11 via LLM from research materials.

    Structure (per reference v1.2.4):
    11.1 盈利预测分析 — per-institution forecast table + 1-para analysis
    11.2 估值分析     — 2-3 sentence overview + valuation dimensions table
    11.3 情景推演     — core variables + 3 differentiated scenario rows
    """
    targets = _collect_target_price_records(materials, ref_map, co, ticker, mkt)
    values = [rec["target"] for rec in targets]
    stats = calculate_target_price_stats(values)
    unit = targets[0].get("unit", "") if targets else ""
    materials["_consensus_forecast_output"] = {}

    # collect all research excerpts for the prompt
    target_research = "\n".join(
        line for line in (
            _research_line(rd, ref_map, max_text=600)
            for rd in materials.get("research", {}).get("details", [])[:12]
            if _source_matches_target(rd, co, ticker)
        ) if line
    )
    # 目标公司材料不足时退回全量研报（最多12条），不提前 return
    if not target_research:
        target_research = "\n".join(
            line for line in (
                _research_line(rd, ref_map, max_text=600)
                for rd in materials.get("research", {}).get("details", [])[:12]
            ) if line
        )
    if not target_research:
        return ""

    refs_guide = "\n".join(
        f"  [{rn}] {v.get('title','')[:80]}"
        for rn, v in sorted(ref_map.items())[:12]
    )

    # target price summary for 11.2
    tp_summary = ""
    if stats.get("count", 0) >= 1:
        cites = _cite([int(r.get("rn") or 0) for r in targets[:6] if r.get("rn")])
        tp_summary = (
            f"机构目标价区间 {stats['low']:g}-{stats['high']:g}{unit}，样本{stats['count']}家"
            f"{'，中位数' + str(stats['median']) + unit if stats.get('count', 0) >= 3 else ''}{cites}"
        )

    prompt = f"""You are a senior equity analyst. Generate §11 for {co} ({ticker}, {mkt}) using ONLY the research excerpts below.
Keep it concise: no more than 4 forecast rows, 4 valuation rows, 3 core-variable bullets, and 3 scenario rows.

Output EXACTLY this structure in Chinese Markdown (no extra text):

## 11 估值与预测

### 11.1 盈利预测分析
Table with columns: 指标 | 来源 | [Year1]E | [Year2]E | [Year3]E
- Use 2-3 forecast years from the materials (e.g. 2026E 2027E 2028E)
- One row per institution per metric (归母净利润/Non-IFRS归母净利润/营业收入/EPS — only if data found)
- Cite [N] next to each number; leave cell blank if no data
- After the table: 1 paragraph (50-120 Chinese chars) summarizing the forecast range, growth rate implied, and key divergence driver

### 11.2 估值分析
2-3 sentence overview (mention current PE/PB level and what it implies vs history/peers), then:
| 估值维度 | 当前水平 | 解读 |
|:--|:--|:--|
- 3-5 rows: PE(TTM), PE(forward), PB, EV/EBITDA, SOTP — only rows with source-backed numbers; cite [N] next to numbers
- After table: 1 sentence on key valuation conclusion

### 11.3 情景推演
核心变量 (3-5 bullet points with specific base values from materials):
• **[variable name]**: [specific number/rate][N], sensitivity: [quantified impact if available]

Then:
| 情景 | 核心假设 | 经营含义 | 估值含义 |
|:--|:--|:--|:--|
| 乐观（概率约X%） | [different/higher assumptions with specific numbers][N] | [better revenue/profit outcome] | [higher PE/target price range][N] |
| 中性（概率约X%） | [base-case assumptions with specific numbers][N] | [baseline outcome] | [base PE/target price][N] |
| 悲观（概率约X%） | [different/lower assumptions with specific numbers][N] | [worse outcome] | [lower PE/target price range][N] |

CRITICAL: The three scenario rows MUST have DIFFERENT assumptions and DIFFERENT target price ranges. Do NOT copy the same text into all three rows.
Probabilities must sum to 100%.

Target price context: {tp_summary or "（无可回溯目标价数据）"}

Reference guide:
{refs_guide}

Research excerpts (cite [N] from above):
{target_research[:2500]}
"""
    text, ok = _call_llm(prompt, max_tokens=2800,
                         timeout=min(_hkus_llm_slow_section_timeout_seconds(), _hkus_llm_task_budget_seconds()),
                         system=_HK_US_REPORT_SYSTEM_CONSTRAINTS,
                         call_name="section_11_llm",
                         max_attempts=1)
    if not ok or not text:
        return ""
    body = _clean_fence(text)
    if "## 11" not in body and "### 11" not in body:
        return ""
    return _drop_empty_forecast_columns_in_section_11(body)


def _find_ref_no(ref_map: dict, target_id: str) -> int:
    """Find reference number by source ID. Returns -1 if not found (never default to 1)."""
    for rn, v in ref_map.items():
        if str(v.get("id", "")) == str(target_id):
            return rn
    return -1  # MUST NOT default to ref [1] — was root cause of citation compression


# ── Reference renumbering ──

def normalize_used_references(report: str, source_trace: dict) -> tuple:
    """Keep only body-used refs, renumber [1][2][3]... sequentially."""
    ref_match = _find_reference_heading_match(report)
    if not ref_match:
        return (report, {}, ["missing_reference_section"])
    body_end = ref_match.start()
    ref_heading = ref_match.group(0)

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

    new_ref_lines = [f"{ref_heading}\n"]
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
    ref_match = _find_reference_heading_match(report)
    body_end = ref_match.start() if ref_match else -1
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
    # 交接文档 v1.2.4 新增：禁止通用模板结论
    "核心主业稳健", "基本面稳健", "估值有望修复", "新业务打开成长空间",
    "深度分析", "基本面", "估值修复",
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
    # 扩充判断词表：覆盖互联网/消费/科技/周期等行业常见表述
    judgment_terms = (
        "驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量", "需求", "利润率",
        # 互联网/平台常见
        "商业化", "变现", "渗透", "用户", "增速", "加速", "提升", "放量", "超预期",
        "广告", "收入", "利润", "现金流", "回购", "分红", "扩张", "拓展",
        # 空头/分歧视角也算有效结论
        "承压", "压力", "风险", "不确定", "下行", "收缩",
        # 增长/回报类
        "增长", "成长", "领先", "优势", "布局", "落地", "兑现", "验证",
    )
    return any(term in text for term in judgment_terms)


def _build_deterministic_fallback_title(texts: dict, company_name: str, ticker: str) -> str:
    """Build a title conclusion from already-generated §1 key points and §3 logic titles."""
    s12 = texts.get("s12", "")
    s34 = texts.get("s34", "")
    judgment_terms = (
        "驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量",
        "商业化", "变现", "渗透", "用户", "增速", "加速", "提升", "放量", "超预期",
        "广告", "收入", "利润", "回购", "扩张", "拓展", "增长", "成长", "领先",
        "优势", "布局", "落地", "兑现", "验证", "承压", "风险",
    )
    # 从 §3 逻辑标题（H3 冒号前短语）中提取
    logic_h3 = re.findall(r'###\s+\d+\.\d+\s+(.+)', s34[:2000] if s34 else "")
    for h in logic_h3:
        h = re.sub(r'\*\*|[：:，,。.、\[\d+\]]', '', h).strip()
        h = re.sub(r'，.*', '', h).strip()  # 取逗号前
        zh_len = len(re.findall(r'[一-鿿]', h))
        if 8 <= zh_len <= 25 and any(t in h for t in judgment_terms):
            return h
    # 从 §1 bullet point 正文中提取（取第一个子句）
    sec1_end = s12.find("近况跟踪")
    sec1_text = s12[:sec1_end] if sec1_end > 0 else s12[:800]
    bullets = re.findall(r'^[\*\-•]\s*\*\*[^*]+\*\*[：:]\s*(.+)$', sec1_text, re.M)
    for b in bullets:
        b = re.sub(r'\[\d+\]', '', b).strip()
        core = re.split(r'[，,。；;]', b)[0].strip()
        zh_len = len(re.findall(r'[一-鿿]', core))
        if 8 <= zh_len <= 25 and any(t in core for t in judgment_terms):
            return core
    # 最后兜底：拼接 §3 中两个最短的 bold 关键词
    kp_matches = re.findall(r'\*\*([^*]{4,15})\*\*', (s34 or "") + s12)
    kws = [m.strip() for m in kp_matches if any(t in m for t in judgment_terms)][:2]
    if len(kws) >= 2:
        return "与".join(kws[:2]) + "双轮驱动"
    if kws:
        return kws[0]
    return ""


def _build_report_title(company_name: str, ticker: str, market_cn: str, conclusion: str) -> str:
    safe_conclusion = _sanitize_title_conclusion(conclusion, company_name, ticker, market_cn)
    if not safe_conclusion:
        return f"# {company_name}（{ticker}）{market_cn}公司一页纸"
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


def _walk_values(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_values(v)


def _first_field(materials: dict, names: tuple[str, ...]) -> Any:
    lowered = {n.lower() for n in names}
    for d in _walk_values(materials):
        for k, v in d.items():
            if str(k).lower() in lowered and v not in ("", None, "N/A"):
                return v
    return ""


def _extract_industry(materials: dict) -> str:
    val = _first_field(materials, ("industry", "industry_name", "gics", "sw_industry", "sector"))
    if val:
        return _plain_text(val, 40)
    tags = materials.get("tags")
    if isinstance(tags, list) and tags:
        return _plain_text(tags[0], 40)
    return "未取得具有来源闭环的行业分类"


def _num_or_none(value: Any) -> float | None:
    if value in ("", None):
        return None
    text = re.sub(r'[,，]', '', str(value))
    m = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(m.group(0)) if m else None


def _extract_price(materials: dict) -> str:
    price = _first_field(materials, ("price", "current_price", "close_price", "last_price", "lastPrice", "trade_price", "close"))
    market_cap = _first_field(materials, ("market_cap", "marketCap", "market_value", "marketValue", "total_market_value"))
    shares = _first_field(materials, ("shares_outstanding", "total_shares", "shareCapital", "totalShare"))
    date = _first_field(materials, ("quote_date", "trade_date", "publishTime", "date", "asOfDate"))
    currency = _first_field(materials, ("currency", "ccy")) or ""
    if not currency:
        market = str(materials.get("market") or "").lower()
        currency = "港元" if market == "hk" else ("美元" if market == "us" else "")
    if not date:
        return "未取得具有日期和来源闭环的有效行情数据"
    parts = []
    if price:
        parts.append(f"{price}{currency}")
    if market_cap:
        parts.append(f"{market_cap}{currency}")
    else:
        p = _num_or_none(price)
        sh = _num_or_none(shares)
        if p is not None and sh is not None:
            parts.append(f"约{p * sh / 1e8:.1f}亿{currency}（按价格×总股本内部测算）")
    if parts:
        return " / ".join(parts) + f"（截至{str(date)[:10]}）"
    return "未取得具有日期和来源闭环的有效行情数据"


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

def run(ticker: str, market: str, company: str, output_dir: str, llm_args: Any = None, run_checker: bool = False) -> dict:
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
        print("  [WARNING] LLM config error — 将切换到材料直写模式继续生成")
    token = _find_token()
    if not token: raise RuntimeError("No DATAYES_TOKEN found")
    mkt = market.lower()
    t0 = time.time()

    # Step 1: 采集材料
    stage_t0 = time.time()
    materials = _collect(ticker, mkt, company, output_dir, token)
    _update_run_manifest(output_dir, stage="collect_materials", duration_s=time.time() - stage_t0)

    # Step 2: 构建 source_trace 和 id_audit
    stage_t0 = time.time()
    trace = _build_trace(materials, output_dir)
    audit = _build_audit(trace, output_dir)
    _update_run_manifest(output_dir, stage="source_trace", duration_s=time.time() - stage_t0)

    # Step 3: 生成报告 MD
    mdp = str(Path(output_dir) / "report.md")
    gs = None
    stage_t0 = time.time()
    try:
        report, gs = write_report(materials, trace, ticker, mkt, company, output_dir, peer_bundle={})
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  Generation error: {e}")
        Path(mdp).write_text(f"# {company}（{ticker}）生成失败\n\n生成阶段失败，未形成可发布报告。\n", encoding="utf-8")
        gs = {"is_skeleton": True, "is_degraded": True, "mode": "exception", "error": str(e),
              "ok_sections": 0, "total_sections": 12,
              "failed_sections": [str(i) for i in range(1, 13)]}
    _update_run_manifest(output_dir, stage="write_report", duration_s=time.time() - stage_t0)
    _write_llm_diagnostics(output_dir)
    if gs:
        _save_json(str(Path(output_dir) / "generation_status.json"), gs)

    # Step 4: 直接转 DOCX（不做质检，不做 post-repair 阻断）
    stage_t0 = time.time()
    company_cn = company
    docx_name = f"{company_cn}（{ticker}）公司一页纸.docx"
    docx_path = str(Path(output_dir) / docx_name)
    docx_status = _run_docx(mdp, docx_path)
    _update_run_manifest(output_dir, stage="docx", duration_s=time.time() - stage_t0)

    elapsed = time.time() - t0
    _update_run_manifest(output_dir, stage="complete", status="complete", duration_s=elapsed)
    ok_n = (gs or {}).get("ok_sections", 0)
    ref_count = (gs or {}).get("ref_count", audit.get("refs_total", "?"))
    print(f"\n{'='*60}")
    print(f"  Pipeline complete ({elapsed:.0f}s)")
    print(f"  MD:   {mdp}")
    print(f"  DOCX: {docx_path if docx_status.get('ok') else '(failed)'}")
    print(f"  refs={audit.get('refs_total','?')} real={audit.get('real_id_count','?')} coverage={audit.get('real_id_coverage_pct','?')}%")
    print(f"{'='*60}")
    return {"output_dir": output_dir, "blocking": False, "docx_status": docx_status,
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
    p.add_argument("--run-checker", action="store_true", help="(已废弃) 保留参数兼容性，质检已移除")
    args = p.parse_args()

    try:
        result = run(args.ticker, args.market.lower(), args.company_name, args.output_dir, llm_args=args)
    except Exception as exc:
        print(f"\nStartup failed: {str(exc)[:300]}")
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
