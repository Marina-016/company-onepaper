#!/usr/bin/env python3
"""
v1.2.2 一键主流程管线

用法：
  python pipeline.py --ticker 002352.SZ --output <new_dir>

流程：
  1. 识别市场
  2. source_enrichment：采集→归一化→去重→冻结 materials JSON（SHA-256）
  3. generate_report：从冻结 materials 自动生成报告草稿
  4. post_process：fix_section_numbering → clean_sparse_tables → rebuild_references
  5. evaluator：自动评测
  6. run_manifest：记录全过程

任一步失败 → 整次运行失败。
Materials 生成后哈希变化 → 自动判定失败。
"""
import argparse, hashlib, json, os, subprocess, sys, traceback
from datetime import datetime
from pathlib import Path

# Resolve package paths
_PKG = Path(__file__).resolve().parent.parent
_REPO = _PKG.parents[2]

_SCRIPTS = _PKG / "scripts"
_EVALUATOR = _PKG / "evaluate_report.py"


def detect_market(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith((".SZ", ".SS")) or (t.isdigit() and len(t) == 6):
        return "A"
    if t.endswith(".HK") or (t.isdigit() and len(t) == 5):
        return "HK"
    if t.endswith((".US", ".O", ".N")):
        return "US"
    if t.isalpha():
        return "US"
    raise ValueError(f"Cannot detect market for ticker: {ticker}")


def compute_file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_step(cmd: list[str], step_name: str, cwd: str | None = None) -> dict:
    """Run a subprocess step. Returns {'ok': True/False, 'stdout': ..., 'stderr': ...}."""
    print(f"  [{step_name}] Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            cwd=cwd or str(_SCRIPTS),
        )
        if result.returncode != 0:
            return {"ok": False, "stdout": result.stdout, "stderr": result.stderr, "step": step_name}
        return {"ok": True, "stdout": result.stdout, "stderr": result.stderr, "step": step_name}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "Timeout after 300s", "step": step_name}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "step": step_name}


def run_pipeline(ticker: str, output_dir: str, company: str | None = None) -> dict:
    """
    一键执行完整 v1.2.2 管线。

    Args:
        ticker: 股票代码，如 002352.SZ, 01299.HK, XOM
        output_dir: 输出目录（自动创建子目录结构）
        company: 公司名（可选，自动从 ticker 推断）
    """
    start_time = datetime.now()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    market = detect_market(ticker)
    ticker_clean = ticker.replace(".SZ", "").replace(".SS", "").replace(".HK", "").replace(".US", "")
    if company is None:
        company = ticker_clean

    case_name = f"{ticker_clean}_{company.lower().replace(' ', '')}"
    manifest_path = out / "run_manifest.json"

    manifest = {
        "case": case_name,
        "ticker": ticker,
        "company": company,
        "market": market,
        "version": "v1.2.2",
        "run_type": "clean_replay",
        "pipeline": "source_enrichment → generate_report → post_process → evaluator → manifest",
        "started_at": start_time.isoformat(),
        "manual_intervention": False,
        "clean_end_to_end": True,
        "eligible_for_regression": True,
        "materials_modified_after_generation": False,
        "steps": {},
    }

    # ═══════════════ Step 1: Source Enrichment ═══════════════
    print(f"\n{'='*60}")
    print(f"[1/5] SOURCE ENRICHMENT: {ticker} ({market})")
    print(f"{'='*60}")
    step_result = run_step(
        [sys.executable, str(_SCRIPTS / "source_enrichment.py"), company, ticker_clean, market, str(out)],
        "source_enrichment",
        cwd=str(_SCRIPTS),
    )
    if not step_result["ok"]:
        manifest["steps"]["source_enrichment"] = step_result
        manifest["pipeline_status"] = "FAILED_at_source_enrichment"
        _write_manifest(manifest_path, manifest)
        raise SystemExit(f"Source enrichment failed: {step_result['stderr'][:500]}")

    # Parse freeze metadata
    materials_path = str(out / "input" / f"{ticker_clean}_materials.json")
    if not Path(materials_path).exists():
        raise SystemExit(f"Materials JSON not found: {materials_path}")

    materials_sha_before = compute_file_sha256(materials_path)
    manifest["materials_finalized_at"] = datetime.now().isoformat()
    manifest["materials_sha256_before_generation"] = materials_sha_before
    manifest["materials_path"] = materials_path
    manifest["steps"]["source_enrichment"] = {"status": "ok", "materials_path": materials_path,
                                                "sha256": materials_sha_before}
    print(f"  Materials frozen: {materials_sha_before[:16]}...")

    # ═══════════════ Step 2: Generate Report ═══════════════
    print(f"\n[2/5] GENERATE REPORT")
    draft_path = str(out / f"{case_name}_draft.md")
    step_result = run_step(
        [sys.executable, str(_SCRIPTS / "generate_report.py"), materials_path, draft_path],
        "generate_report",
        cwd=str(_SCRIPTS),
    )
    if not step_result["ok"]:
        manifest["steps"]["generate_report"] = step_result
        manifest["pipeline_status"] = "FAILED_at_generate"
        _write_manifest(manifest_path, manifest)
        raise SystemExit(f"Report generation failed: {step_result['stderr'][:500]}")

    manifest["report_generated_at"] = datetime.now().isoformat()
    manifest["draft_path"] = draft_path

    # Verify materials not modified
    materials_sha_after = compute_file_sha256(materials_path)
    materials_modified = materials_sha_before != materials_sha_after
    manifest["materials_sha256_after_generation"] = materials_sha_after
    manifest["materials_modified_after_generation"] = materials_modified
    if materials_modified:
        manifest["pipeline_status"] = "FAILED_materials_modified_after_generation"
        _write_manifest(manifest_path, manifest)
        raise SystemExit("Materials JSON modified after generation — run FAILED")
    manifest["steps"]["generate_report"] = {"status": "ok", "draft_path": draft_path}
    print(f"  Draft: {draft_path}")

    # ═══════════════ Step 3: Post-Process ═══════════════
    print(f"\n[3/5] POST-PROCESS")
    processed_path = str(out / f"{case_name}_report.md")
    step_result = run_step(
        [sys.executable, str(_SCRIPTS / "post_process.py"), draft_path, materials_path, processed_path],
        "post_process",
        cwd=str(_SCRIPTS),
    )
    if not step_result["ok"]:
        manifest["steps"]["post_process"] = step_result
        manifest["pipeline_status"] = "FAILED_at_post_process"
        _write_manifest(manifest_path, manifest)
        raise SystemExit(f"Post-process failed: {step_result['stderr'][:500]}")

    try:
        pp_output = json.loads(step_result["stdout"])
    except json.JSONDecodeError:
        pp_output = {}

    manifest["post_process_applied"] = True
    manifest["section_numbering_fixed"] = len(pp_output.get("section_fixes", []))
    manifest["sparse_rows_removed"] = sum(1 for d in pp_output.get("sparse_deletions", []) if d.get("type") == "row")
    manifest["sparse_columns_removed"] = sum(1 for d in pp_output.get("sparse_deletions", []) if d.get("type") == "column")
    manifest["references_rebuilt"] = True
    manifest["reference_count"] = pp_output.get("reference_report", {}).get("total_used", 0)
    manifest["reference_mapped"] = pp_output.get("reference_report", {}).get("mapped", 0)
    manifest["reference_unmapped"] = pp_output.get("reference_report", {}).get("unmapped", [])
    manifest["steps"]["post_process"] = {
        "status": "ok",
        "section_fixes": manifest["section_numbering_fixed"],
        "sparse_rows_removed": manifest["sparse_rows_removed"],
        "sparse_columns_removed": manifest["sparse_columns_removed"],
        "ref_mapped": manifest["reference_mapped"],
        "ref_unmapped": manifest["reference_unmapped"],
    }
    print(f"  Section fixes: {manifest['section_numbering_fixed']}")
    print(f"  Sparse: {manifest['sparse_rows_removed']} rows, {manifest['sparse_columns_removed']} cols")
    print(f"  Refs: {manifest['reference_count']} total, {manifest['reference_mapped']} mapped")

    # ═══════════════ Step 4: Evaluator ═══════════════
    print(f"\n[4/5] EVALUATOR")
    eval_dir = str(out / "evaluation")
    step_result = run_step(
        [sys.executable, str(_EVALUATOR), "--report", processed_path, "--materials", materials_path,
         "--output-dir", eval_dir],
        "evaluator",
        cwd=str(_PKG),
    )
    if not step_result["ok"]:
        manifest["steps"]["evaluator"] = step_result
        manifest["pipeline_status"] = "FAILED_at_evaluator"
        _write_manifest(manifest_path, manifest)
        raise SystemExit(f"Evaluator failed: {step_result['stderr'][:500]}")

    # Parse audit
    audit_files = list(Path(eval_dir).glob("*_audit.json"))
    if not audit_files:
        raise SystemExit(f"No audit JSON found in {eval_dir}")
    with open(audit_files[0], "r", encoding="utf-8") as f:
        audit = json.load(f)
    summary = audit.get("summary", {})
    manifest["evaluation"] = {
        "P0": summary.get("P0", "N/A"),
        "P1": summary.get("P1", "N/A"),
        "P2": summary.get("P2", "N/A"),
        "P3": summary.get("P3", "N/A"),
        "release_decision": summary.get("release_decision", "N/A"),
        "financial_checks_passed": summary.get("financial_checks_passed", 0),
        "financial_checks_failed": summary.get("financial_checks_failed", 0),
        "financial_checks_skipped": summary.get("financial_checks_skipped", 0),
        "references_used": summary.get("used_reference_count", 0),
        "references_defined": summary.get("defined_reference_count", 0),
        "missing_reference_definitions": summary.get("missing_reference_definitions", []),
    }
    manifest["steps"]["evaluator"] = {"status": "ok", "audit_path": str(audit_files[0])}
    print(f"  P0={summary.get('P0')} P1={summary.get('P1')} P2={summary.get('P2')}")
    print(f"  Decision: {summary.get('release_decision')}")

    # ═══════════════ Step 5: Manifest ═══════════════
    manifest["finished_at"] = datetime.now().isoformat()
    manifest["pipeline_status"] = "OK"
    _write_manifest(manifest_path, manifest)
    print(f"\n[5/5] PIPELINE COMPLETE")
    print(f"  Manifest: {manifest_path}")
    return manifest


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(description="v1.2.2 一键公司一页纸管线")
    parser.add_argument("--ticker", required=True, help="股票代码，如 002352.SZ, 01299.HK, XOM")
    parser.add_argument("--output", required=True, help="输出目录（自动创建）")
    parser.add_argument("--company", default=None, help="公司名（可选）")
    args = parser.parse_args()

    try:
        run_pipeline(args.ticker, args.output, args.company)
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
