"""Frozen-material claim judge pilot for v1.3.0.

The v1.3.0 judge is experimental and non-enforcing by default. It prepares
LLM-ready evidence packets, but the offline scorer below remains deterministic
so regression can run without network access or an LLM provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


VERDICTS = {"supported", "partially_supported", "unsupported", "unverifiable"}
LOW_CONFIDENCE_THRESHOLD = 0.6
BENCHMARK_PATH = Path(__file__).with_name("benchmarks") / "v1.3.0_claim_benchmark.json"


@dataclass
class AtomicClaim:
    claim_id: str
    line: int
    parent_text: str
    claim: str
    references: list[str]
    claim_type: str
    split_from: str
    deterministic_overlap: bool = False


@dataclass
class SkippedClaim:
    claim_id: str
    line: int
    parent_text: str
    claim: str
    references: list[str]
    claim_type: str
    skip_reason: str
    deterministic_overlap: bool


@dataclass
class JudgeResult:
    claim_id: str
    line: int
    parent_text: str
    claim: str
    references: list[str]
    evidence: str
    evidence_source_id: str
    verdict: str
    confidence: float
    reason: str
    risk_level: str | None
    manual_review: bool
    critical_fact: bool
    claim_type: str
    split_from: str
    deterministic_overlap: bool


def strip_citations(text: str) -> str:
    return re.sub(r"\[([0-9]+|A[0-9]+)\]", "", text or "")


def sort_refs(refs: list[str]) -> list[str]:
    def key(ref: str) -> tuple[bool, int]:
        if ref.startswith("A"):
            return True, int(ref[1:] or 0)
        return False, int(ref or 0)

    return sorted(set(refs), key=key)


def normalize_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.lower()
    return re.sub(r"[\s,.;:(){}\[\]<>|/_%+\-\u3000-\u303f\uff00-\uffef]+", "", text)


def numeric_tokens(text: str) -> list[str]:
    clean = strip_citations(text)
    raw = re.findall(r"-?\d+(?:\.\d+)?%?", clean)
    tokens: list[str] = []
    for token in raw:
        if token in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            continue
        tokens.append(token)
        if token.endswith(".0"):
            tokens.append(token[:-2])
    return sorted(set(tokens), key=len, reverse=True)


def keyword_tokens(text: str) -> set[str]:
    clean = strip_citations(text)
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_\-/]{2,}", clean.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", clean)
    stop = {
        "company", "report", "source", "同比", "环比", "收入", "利润", "预计", "预测",
        "业务", "市场", "参考", "资料", "亿元", "美元", "港元", "人民币", "公司",
    }
    return {x for x in latin + chinese if x not in stop and len(x) >= 2}


def collect_evidence_text(source_objs: list[dict[str, Any]], max_chars: int = 30000) -> str:
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("title", "text", "content", "summary", "snapshot_text", "definition"):
                if isinstance(value.get(key), str) and value[key].strip():
                    parts.append(value[key].strip())
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for obj in source_objs:
        walk(obj)

    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)
    return "\n".join(unique_parts)[:max_chars]


def select_evidence_excerpt(claim: str, evidence: str, max_chars: int = 900) -> str:
    if len(evidence) <= max_chars:
        return evidence
    needles: list[str] = numeric_tokens(claim)
    if not needles:
        needles = sorted(keyword_tokens(claim), key=len, reverse=True)
    evidence_norm = evidence.lower()
    best_pos = -1
    for needle in needles:
        pos = evidence_norm.find(str(needle).lower())
        if pos >= 0:
            best_pos = pos
            break
    if best_pos < 0:
        return evidence[:max_chars]
    start = max(0, best_pos - max_chars // 3)
    end = min(len(evidence), start + max_chars)
    return evidence[start:end]


def is_critical_fact(claim: str) -> bool:
    text = strip_citations(claim).lower()
    if numeric_tokens(text):
        return True
    critical_terms = [
        "收入", "营收", "净利润", "eps", "ebita", "ebitda", "毛利率", "roe", "capex",
        "目标价", "估值", "pe", "ps", "dcf", "sotp", "guidance", "指引", "segment",
        "non-gaap", "gaap", "同比", "环比", "free cash flow", "fcf",
    ]
    return any(term in text for term in critical_terms)


def claim_type_for(text: str, parent_text: str) -> str:
    lower = f"{text} {parent_text}".lower()
    if "|" in parent_text:
        return "table_cell"
    if any(term in lower for term in ["guidance", "指引"]):
        return "guidance"
    if any(term in lower for term in ["pe", "ps", "dcf", "sotp", "ev/s", "估值", "目标价"]):
        return "valuation"
    if any(term in lower for term in ["gaap", "non-gaap", "eps", "ebita", "收入", "利润", "毛利率"]):
        return "financial"
    if any(term in lower for term in ["壁垒", "竞争", "优势", "风险", "催化", "逻辑"]):
        return "qualitative"
    return "body"


def is_table_separator(line: str) -> bool:
    return bool(line.strip()) and set(line.strip()) <= {"|", "-", ":", " "}


def is_probable_table_header(cells: list[str]) -> bool:
    header_terms = {"项目", "指标", "公司", "来源", "字段", "口径", "估值方法", "市场", "维度"}
    return bool(cells) and sum(any(term in cell for term in header_terms) for cell in cells) >= 2


def deterministic_overlap(text: str, parent_text: str) -> bool:
    combined = f"{text} {parent_text}".lower()
    terms = [
        "引用", "参考资料", "source_id", "materials", "metadata", "元数据", "id:",
        "pit", "三表", "同业对比", "peer", "可比公司", "roe", "capex",
    ]
    return any(term in combined for term in terms)


def split_text_segments(text: str) -> list[str]:
    body = re.sub(r"^\s*(?:[-*+]|\d+[.)]|#+)\s*", "", text).strip()
    body = re.sub(r"^\*\*(.+?)\*\*[:：]?\s*", r"\1：", body)
    pieces = re.split(r"(?<=[。！？!?；;])\s*|\s+[/-]\s+(?=[^\[])", body)
    segments: list[str] = []
    comma_split_terms = [
        "同比", "环比", "增长", "下降", "收入", "利润", "PE", "DCF", "SOTP",
        "guidance", "GAAP", "non-GAAP", "Non-GAAP",
    ]
    for piece in pieces:
        piece = piece.strip(" \t;-；。")
        if not piece:
            continue
        comma_parts = [piece]
        if any(term in piece for term in comma_split_terms):
            comma_parts = re.split(r"[，,]\s*", piece)
        for part in comma_parts:
            part = part.strip(" \t;-；。")
            if part:
                segments.append(part)
    return segments or [body]


def split_claims(claims: list[dict[str, Any]]) -> tuple[list[AtomicClaim], list[dict[str, Any]]]:
    atomic: list[AtomicClaim] = []
    per_line: list[dict[str, Any]] = []
    for claim in claims:
        line = int(claim.get("line") or 0)
        parent = str(claim.get("text") or "").strip()
        parent_refs = sort_refs([str(r) for r in claim.get("references", [])])
        line_parts: list[tuple[str, str]] = []

        if "|" in parent and not is_table_separator(parent):
            cells = [cell.strip() for cell in parent.strip("|").split("|")]
            if is_probable_table_header(cells):
                line_parts = []
            else:
                for idx, cell in enumerate(cells, 1):
                    if not cell or set(cell) <= {"-", ":", " "}:
                        continue
                    if len(strip_citations(cell).strip()) < 4:
                        continue
                    line_parts.append((f"table_cell:{idx}", cell))
        else:
            line_parts = [(f"text:{idx}", part) for idx, part in enumerate(split_text_segments(parent), 1)]

        start_count = len(atomic)
        for split_from, text in line_parts:
            refs = sort_refs(re.findall(r"\[([0-9]+|A[0-9]+)\]", text) or parent_refs)
            claim_id = f"L{line}.{len(atomic) - start_count + 1}"
            atomic.append(AtomicClaim(
                claim_id=claim_id,
                line=line,
                parent_text=parent,
                claim=text,
                references=refs,
                claim_type=claim_type_for(text, parent),
                split_from=split_from,
                deterministic_overlap=deterministic_overlap(text, parent),
            ))
        per_line.append({
            "line": line,
            "original_text": parent,
            "original_refs": parent_refs,
            "atomic_count": len(atomic) - start_count,
        })
    return atomic, per_line


def skip_reason(claim: AtomicClaim) -> str | None:
    text = claim.claim.strip()
    bare = strip_citations(text).strip()
    lower = text.lower()
    if not claim.references:
        return "no_citation"
    if not bare:
        return "citation_only"
    if len(bare) < 4:
        return "too_short"
    if is_table_separator(text):
        return "pure_table_structure"
    metadata_terms = ["source_id", "materials", "metadata", "元数据", "参考资料", "来源格式", "逐字复制"]
    if any(term in lower for term in metadata_terms):
        return "deterministic_metadata_or_citation_check"
    citation_format_terms = ["引用闭环", "引用编号", "citation", "reference id"]
    if any(term in lower for term in citation_format_terms):
        return "deterministic_citation_check"
    if claim.claim_type == "table_cell" and not numeric_tokens(text) and len(keyword_tokens(text)) <= 1:
        return "pure_table_label"
    return None


def build_evidence_by_reference(
    references: dict[str, dict[str, Any]],
    source_index: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    evidence_by_ref: dict[str, str] = {}
    for ref_no, ref in references.items():
        source_id = ref.get("source_id")
        if source_id and source_id in source_index:
            evidence_by_ref[ref_no] = collect_evidence_text(source_index[source_id])
        else:
            evidence_by_ref[ref_no] = str(ref.get("definition") or "")
    return evidence_by_ref


def judge_one(claim: AtomicClaim, evidence_by_source: dict[str, str]) -> JudgeResult:
    refs = [r for r in claim.references if r in evidence_by_source]
    evidence_source_id = ",".join(refs)
    evidence = "\n".join(evidence_by_source[r] for r in refs if evidence_by_source[r])
    critical = is_critical_fact(claim.claim)

    if not refs or not evidence.strip():
        verdict = "unverifiable"
        confidence = 0.35
        reason = "No frozen-material evidence text is available for the cited source id."
    else:
        claim_numbers = numeric_tokens(claim.claim)
        evidence_norm = normalize_text(evidence)
        matched_numbers = [n for n in claim_numbers if normalize_text(n) in evidence_norm]
        claim_keywords = keyword_tokens(claim.claim)
        evidence_keywords = keyword_tokens(evidence[:5000])
        overlap = len(claim_keywords & evidence_keywords) / max(1, len(claim_keywords))

        if claim_numbers and len(matched_numbers) == len(claim_numbers) and overlap >= 0.06:
            verdict = "supported"
            confidence = min(0.93, 0.75 + 0.18 * overlap)
            reason = "All extracted numeric tokens are present in frozen evidence and keyword overlap is sufficient."
        elif claim_numbers and matched_numbers:
            verdict = "partially_supported"
            confidence = min(0.78, 0.52 + 0.18 * overlap + 0.06 * len(matched_numbers))
            reason = "Some, but not all, extracted numeric tokens are present in frozen evidence."
        elif not claim_numbers and overlap >= 0.30:
            verdict = "supported"
            confidence = min(0.85, 0.64 + 0.20 * overlap)
            reason = "Non-numeric claim has sufficient keyword overlap with frozen evidence."
        elif overlap >= 0.14:
            verdict = "partially_supported"
            confidence = min(0.67, 0.46 + 0.20 * overlap)
            reason = "Frozen evidence is topically related, but exact support is incomplete."
        elif claim_numbers:
            verdict = "unsupported"
            confidence = 0.72
            reason = "Claim contains numeric facts, but none of the extracted numeric tokens appear in frozen evidence."
        else:
            verdict = "unverifiable"
            confidence = 0.42
            reason = "Frozen evidence is insufficient for a semantic support judgment."

    if verdict == "supported":
        risk_level: str | None = None
    elif verdict == "unsupported":
        risk_level = "P1" if critical else "P2"
    elif verdict == "partially_supported":
        risk_level = "P2" if critical else "P3"
    else:
        risk_level = "P2" if critical else "P3"

    return JudgeResult(
        claim_id=claim.claim_id,
        line=claim.line,
        parent_text=claim.parent_text,
        claim=claim.claim,
        references=claim.references,
        evidence=select_evidence_excerpt(claim.claim, evidence),
        evidence_source_id=evidence_source_id,
        verdict=verdict,
        confidence=round(float(confidence), 3),
        reason=reason,
        risk_level=risk_level,
        manual_review=confidence < LOW_CONFIDENCE_THRESHOLD,
        critical_fact=critical,
        claim_type=claim.claim_type,
        split_from=claim.split_from,
        deterministic_overlap=claim.deterministic_overlap,
    )


def load_benchmark() -> list[dict[str, Any]]:
    if not BENCHMARK_PATH.exists():
        return []
    with BENCHMARK_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return list(data.get("items", []))
    if isinstance(data, list):
        return data
    return []


def evaluate_benchmark(case_id: str | None, results: list[JudgeResult]) -> dict[str, Any]:
    items = [
        row for row in load_benchmark()
        if (not case_id or row.get("case_id") == case_id) and not row.get("claim")
    ]
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for item in items:
        needle = str(item.get("match_text") or "").strip()
        expected = str(item.get("expected_verdict") or "")
        hit: JudgeResult | None = None
        for result in results:
            if result.claim_id in used_ids:
                continue
            haystack = f"{result.claim}\n{result.parent_text}"
            if needle and needle in haystack:
                hit = result
                break
        if hit is None:
            unmatched.append(item)
            continue
        used_ids.add(hit.claim_id)
        matched.append({
            "case_id": item.get("case_id"),
            "category": item.get("category"),
            "match_text": needle,
            "expected_verdict": expected,
            "actual_verdict": hit.verdict,
            "claim_id": hit.claim_id,
            "line": hit.line,
            "correct": hit.verdict == expected,
        })

    def precision(label: str) -> float | None:
        predicted = [row for row in matched if row["actual_verdict"] == label]
        if not predicted:
            return None
        correct = sum(row["expected_verdict"] == label for row in predicted)
        return round(correct / len(predicted), 4)

    accuracy = None
    if matched:
        accuracy = round(sum(row["correct"] for row in matched) / len(matched), 4)

    return {
        "case_id": case_id,
        "benchmark_item_count": len(items),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "overall_accuracy": accuracy,
        "supported_precision": precision("supported"),
        "unsupported_precision": precision("unsupported"),
        "partial_precision": precision("partially_supported"),
        "matched_items": matched,
        "unmatched_items": unmatched,
    }


def run_llm_judge(
    claims: list[dict[str, Any]],
    references: dict[str, dict[str, Any]],
    source_index: dict[str, list[dict[str, Any]]],
    case_id: str | None = None,
) -> dict[str, Any]:
    evidence_by_ref = build_evidence_by_reference(references, source_index)
    atomic_claims, per_line_split = split_claims(claims)

    skipped: list[SkippedClaim] = []
    judgeable: list[AtomicClaim] = []
    for claim in atomic_claims:
        reason = skip_reason(claim)
        if reason:
            skipped.append(SkippedClaim(
                claim_id=claim.claim_id,
                line=claim.line,
                parent_text=claim.parent_text,
                claim=claim.claim,
                references=claim.references,
                claim_type=claim.claim_type,
                skip_reason=reason,
                deterministic_overlap=claim.deterministic_overlap,
            ))
        else:
            judgeable.append(claim)

    results = [judge_one(claim, evidence_by_ref) for claim in judgeable]
    counts = {verdict: 0 for verdict in sorted(VERDICTS)}
    risk_counts = {"P1": 0, "P2": 0, "P3": 0}
    manual_review_count = 0
    deterministic_overlap_count = 0
    for result in results:
        counts[result.verdict] += 1
        if result.risk_level:
            risk_counts[result.risk_level] += 1
        if result.manual_review:
            manual_review_count += 1
        if result.deterministic_overlap:
            deterministic_overlap_count += 1
    deterministic_overlap_count += sum(1 for item in skipped if item.deterministic_overlap)

    benchmark_metrics = evaluate_benchmark(case_id, results)
    judgeable_count = len(results)
    atomic_count = len(atomic_claims)
    return {
        "mode": "offline_frozen_material_pilot",
        "schema_version": "v1.3.0-experimental",
        "status": "experimental_non_enforcing",
        "policy": {
            "allowed_evidence": "materials_json_only",
            "network": "disabled",
            "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
            "release_gate": "disabled",
        },
        "summary": {
            "original_claim_count": len(claims),
            "atomic_claim_count": atomic_count,
            "judgeable_claim_count": judgeable_count,
            "skipped_claim_count": len(skipped),
            "claim_count": judgeable_count,
            "verdict_counts": counts,
            "risk_counts": risk_counts,
            "manual_review_count": manual_review_count,
            "human_review_rate": round(manual_review_count / judgeable_count, 4) if judgeable_count else 0.0,
            "deterministic_overlap_count": deterministic_overlap_count,
            "deterministic_overlap_rate": round(deterministic_overlap_count / atomic_count, 4) if atomic_count else 0.0,
        },
        "benchmark_metrics": benchmark_metrics,
        "split_summary": per_line_split,
        "skipped_claims": [asdict(item) for item in skipped],
        "results": [asdict(result) for result in results],
    }


def judge_findings(judge_output: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for row in judge_output.get("results", []):
        risk = row.get("risk_level")
        if not risk:
            continue
        findings.append({
            "severity": risk,
            "category": "LLM Judge",
            "location": f"body line {row.get('line')}",
            "issue": f"Claim judged {row.get('verdict')}",
            "evidence": str(row.get("evidence") or "")[:500],
            "suggestion": "Manual review required; judge evidence is limited to frozen materials JSON.",
        })
    return findings
