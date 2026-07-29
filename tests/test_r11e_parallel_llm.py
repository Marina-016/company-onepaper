#!/usr/bin/env python3
"""r11e mock-only tests for bounded HK/US LLM parallelism."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hk_us_report_writer_v124 as writer  # noqa: E402
from llm_adapter_v124 import LLMResult  # noqa: E402


class EnvGuard:
    def __enter__(self):
        self.old = dict(os.environ)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.old)


def _sleep_spec(name: str, delay: float, value: str = "ok") -> writer.LlmTaskSpec:
    return writer.LlmTaskSpec(name, lambda: (time.sleep(delay), value)[1])


class TestR11EParallelScheduler(unittest.TestCase):
    def setUp(self):
        self.old_diag = writer._LLM_DIAGNOSTICS
        writer._LLM_DIAGNOSTICS = {"config": {}, "calls": []}

    def tearDown(self):
        writer._LLM_DIAGNOSTICS = self.old_diag

    def test_01_max_concurrent_main_tasks_not_above_three(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def run():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            return "ok"

        specs = [writer.LlmTaskSpec(f"t{i}", run) for i in range(6)]
        results, _ = writer.run_hkus_llm_tasks(specs, max_workers=3, total_budget_seconds=5, progress_prefix="[T]")
        self.assertEqual(len(results), 6)
        self.assertLessEqual(peak, 3)

    def test_02_tasks_overlap_in_time(self):
        starts = []
        lock = threading.Lock()

        def run():
            with lock:
                starts.append(time.time())
            time.sleep(0.12)
            return "ok"

        t0 = time.time()
        writer.run_hkus_llm_tasks([writer.LlmTaskSpec(f"t{i}", run) for i in range(3)], max_workers=3, total_budget_seconds=5, progress_prefix="[T]")
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.60)
        self.assertLess(max(starts) - min(starts), 0.08)

    def test_03_as_completed_fast_task_returns_before_slow(self):
        specs = [_sleep_spec("slow", 0.18), _sleep_spec("fast", 0.02)]
        results, _ = writer.run_hkus_llm_tasks(specs, max_workers=2, total_budget_seconds=5, progress_prefix="[T]")
        self.assertEqual(results[0].task_name, "fast")

    def test_04_one_exception_does_not_cancel_others(self):
        def bad():
            raise RuntimeError("boom")

        specs = [writer.LlmTaskSpec("bad", bad), _sleep_spec("good", 0.02)]
        results, _ = writer.run_hkus_llm_tasks(specs, max_workers=2, total_budget_seconds=5, progress_prefix="[T]")
        by_name = {r.task_name: r for r in results}
        self.assertFalse(by_name["bad"].ok)
        self.assertTrue(by_name["good"].ok)

    def test_05_budget_timeout_returns_budget_result(self):
        specs = [_sleep_spec("slow", 0.4), _sleep_spec("fast", 0.02)]
        t0 = time.time()
        results, meta = writer.run_hkus_llm_tasks(specs, max_workers=2, total_budget_seconds=0.08, progress_prefix="[T]")
        elapsed = time.time() - t0
        self.assertTrue(meta["budget_exceeded"])
        self.assertLess(elapsed, 0.25)
        self.assertTrue(any(r.status == "budget_exceeded" for r in results))

    def test_06_task_results_are_merged_outside_workers(self):
        shared = {"texts": {}, "status": {}, "failed": []}

        def run():
            return {"ok": True, "status": "ok", "text": "body"}

        results, _ = writer.run_hkus_llm_tasks([writer.LlmTaskSpec("section_8_json", run)], max_workers=1, total_budget_seconds=5, progress_prefix="[T]")
        self.assertEqual(shared, {"texts": {}, "status": {}, "failed": []})
        shared["texts"]["s89"] = results[0].content["text"]
        shared["status"]["8"] = results[0].content["status"]
        self.assertEqual(shared["texts"]["s89"], "body")

    def test_07_same_task_final_status_once(self):
        results, _ = writer.run_hkus_llm_tasks([writer.LlmTaskSpec("x", lambda: {"ok": True, "status": "recovered"})], max_workers=1, total_budget_seconds=5, progress_prefix="[T]")
        self.assertEqual(len([r for r in results if r.task_name == "x"]), 1)
        self.assertEqual(results[0].status, "recovered")

    def test_08_retry_success_not_in_failed_sections_merge(self):
        failed = ["10"]
        result = writer.LlmTaskResult("section_10", True, {"ok": True, "status": "ok_json"}, "ok_json", 0.1, 1)
        if result.ok and "10" in failed:
            failed.remove("10")
        self.assertNotIn("10", failed)

    def test_09_batch_success_items_not_retried(self):
        calls = []
        old = writer._call_llm

        def fake(prompt, **kw):
            calls.append(kw.get("call_name"))
            if kw.get("call_name") == "target_price_basis":
                return ('[{"articleId":"1","target_price_basis":"PE","key_assumptions":["A"]}]', True)
            return ('[{"articleId":"2","target_price_basis":"DCF","key_assumptions":["B"]}]', True)

        writer._call_llm = fake
        try:
            out = writer._extract_target_price_basis([
                {"article_id": "1", "org": "A", "target": 1, "date": "d"},
                {"article_id": "2", "org": "B", "target": 2, "date": "d"},
            ])
        finally:
            writer._call_llm = old
        self.assertIn("1", out)
        self.assertIn("target_price_basis_retry:2", calls)
        self.assertNotIn("target_price_basis_retry:1", calls)

    def test_10_only_missing_article_ids_retry_concurrently(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        old = writer._call_llm

        def fake(prompt, **kw):
            nonlocal active, peak
            name = kw.get("call_name")
            if name == "target_price_basis":
                return ("[]", True)
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            aid = str(name).split(":")[-1]
            return (f'[{{"articleId":"{aid}","target_price_basis":"PE","key_assumptions":["A"]}}]', True)

        writer._call_llm = fake
        try:
            out = writer._extract_target_price_basis([
                {"article_id": "1", "org": "A", "target": 1, "date": "d"},
                {"article_id": "2", "org": "B", "target": 2, "date": "d"},
                {"article_id": "3", "org": "C", "target": 3, "date": "d"},
            ])
        finally:
            writer._call_llm = old
        self.assertEqual(set(out), {"1", "2", "3"})
        self.assertLessEqual(peak, writer._hkus_llm_retry_workers())
        self.assertGreaterEqual(peak, 2)

    def test_11_retry_worker_env_boundaries(self):
        with EnvGuard():
            os.environ["HKUS_LLM_RETRY_MAX_WORKERS"] = "9"
            self.assertEqual(writer._hkus_llm_retry_workers(), writer.HKUS_LLM_RETRY_MAX_WORKERS_DEFAULT)
            os.environ["HKUS_LLM_RETRY_MAX_WORKERS"] = "3"
            self.assertEqual(writer._hkus_llm_retry_workers(), 3)

    def test_12_article_id_results_bind_out_of_order(self):
        payload = [
            {"articleId": "3", "target_price_basis": "SOTP", "key_assumptions": ["C"]},
            {"articleId": "1", "target_price_basis": "PE", "key_assumptions": ["A"]},
        ]
        out, _ = writer._normalize_target_basis_payload(payload, {"1", "3"})
        self.assertEqual(out["1"]["target_price_basis"], "PE")
        self.assertEqual(out["3"]["target_price_basis"], "SOTP")

    def test_13_diagnostics_are_scoped_to_task(self):
        old = writer.adapter_call_llm

        def fake(prompt, **kw):
            return LLMResult(True, "hello", "", "", 200, 0.01, 1, "m", "fmt", "endpoint")

        writer.adapter_call_llm = fake
        try:
            specs = [writer.LlmTaskSpec("a", lambda: writer._call_llm("p", call_name="a_call")),
                     writer.LlmTaskSpec("b", lambda: writer._call_llm("p", call_name="b_call"))]
            results, _ = writer.run_hkus_llm_tasks(specs, max_workers=2, total_budget_seconds=5, progress_prefix="[T]")
        finally:
            writer.adapter_call_llm = old
        self.assertEqual(len(writer._LLM_DIAGNOSTICS["calls"]), 2)
        by_task = {r.task_name: [d["call_name"] for d in r.diagnostics] for r in results}
        self.assertEqual(by_task["a"], ["a_call"])
        self.assertEqual(by_task["b"], ["b_call"])

    def test_14_scheduler_does_not_write_json_files(self):
        with tempfile.TemporaryDirectory() as d:
            writer.run_hkus_llm_tasks([_sleep_spec("x", 0.01)], max_workers=1, total_budget_seconds=5, progress_prefix="[T]")
            self.assertEqual(list(Path(d).glob("*.json")), [])

    def test_15_source_trace_can_be_merged_after_tasks(self):
        results, _ = writer.run_hkus_llm_tasks([writer.LlmTaskSpec("peer", lambda: {"local_sources": [{"id": "A"}]})], max_workers=1, total_budget_seconds=5, progress_prefix="[T]")
        merged = []
        for r in results:
            merged.extend(r.content.get("local_sources", []))
        self.assertEqual(merged, [{"id": "A"}])

    def test_16_budget_exhaustion_marks_not_started_retries(self):
        results, meta = writer.run_hkus_llm_tasks([_sleep_spec("slow1", 0.3), _sleep_spec("slow2", 0.3)], max_workers=1, total_budget_seconds=0.05, progress_prefix="[T]")
        self.assertTrue(meta["budget_exceeded"])
        self.assertTrue(any(r.status == "budget_exceeded" for r in results))

    def test_17_main_worker_env_boundaries(self):
        with EnvGuard():
            os.environ["HKUS_LLM_MAX_WORKERS"] = "12"
            self.assertEqual(writer._hkus_llm_max_workers(), writer.HKUS_LLM_MAX_WORKERS_DEFAULT)
            os.environ["HKUS_LLM_MAX_WORKERS"] = "4"
            self.assertEqual(writer._hkus_llm_max_workers(), 4)
            os.environ["HKUS_LLM_TOTAL_BUDGET_SECONDS"] = "60"
            self.assertEqual(writer._hkus_llm_total_budget_seconds(), writer.HKUS_LLM_TOTAL_BUDGET_SECONDS_DEFAULT)

    def test_18_max_workers_one_serial_mode(self):
        t0 = time.time()
        writer.run_hkus_llm_tasks([_sleep_spec("a", 0.08), _sleep_spec("b", 0.08)], max_workers=1, total_budget_seconds=5, progress_prefix="[T]")
        self.assertGreaterEqual(time.time() - t0, 0.15)

    def test_19_no_llm_config_section_task_fail_closed(self):
        out = writer._run_section_5_7_task("s57", {"research": {"details": []}}, {}, {}, {}, "Co", "0001.HK", "HK", "", False, "")
        self.assertIn(out["status"], {"failed_sparse", "degraded_partial_hk_pit_only"})

    def test_20_final_section_order_stays_fixed(self):
        title = "# T"
        meta = "\n市场：港股 | 生成日期：2026-07-16\n"
        texts = {"s1011": "## 10 市场分歧\nx\n\n## 11 估值与预测\ny", "s12": "a", "s34": "b", "s57": "c", "s89": "d", "s12r": "e"}
        report, _ = writer.assemble_fixed_hk_us_sections(title, meta, texts, "## 13 参考资料\n")
        positions = [report.find(f"## {i} ") for i in range(1, 13)]
        self.assertEqual(positions, sorted(positions))

    def test_21_parallel_mock_faster_than_serial_theory(self):
        t0 = time.time()
        writer.run_hkus_llm_tasks([_sleep_spec("a", 0.2), _sleep_spec("b", 0.2), _sleep_spec("c", 0.2)], max_workers=3, total_budget_seconds=5, progress_prefix="[T]")
        self.assertLess(time.time() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
