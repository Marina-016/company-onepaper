#!/usr/bin/env python3
"""Mock-only tests for HK/US peer comparison r10."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from peer_comparison_v124 import (  # noqa: E402
    PEER_TABLE_COLUMNS,
    build_peer_comparison_bundle,
    build_peer_comparison_section,
    merge_peer_sources_into_materials,
)
from check_report_quality_v124 import check_peer_comparison_source_trace  # noqa: E402


def _materials() -> dict:
    return {
        "research": {
            "details": [
                {
                    "articleId": "T1",
                    "articleTitle": "目标公司云服务同业比较",
                    "publishTimeReadable": "2026-07-12",
                    "textAbstract": (
                        "目标公司在云服务、企业客户和AI平台上与PeerA、PeerB、PeerC存在可比关系。"
                        "PeerA在云服务收入增长方面与目标公司直接竞争。"
                        "PeerB在企业软件订阅业务上可比。"
                        "PeerC在AI平台产品和客户拓展上可比。"
                    ),
                }
            ]
        },
        "materials_v2": {"unique_sources": []},
    }


def _llm_call(prompt: str, **_: object) -> tuple[str, bool]:
    if "latest_progress_summary" in prompt:
        peer = "PeerA" if "PeerA" in prompt else ("PeerB" if "PeerB" in prompt else "PeerC")
        return json.dumps({
            "latest_progress_summary": f"{peer}发布云服务产品并披露客户增长",
            "progress_date": "2026-07-10",
            "progress_type": "产品发布",
            "quantitative_metrics": ["20%"],
            "source_ids": [f"{peer}_M1"],
            "supporting_sentence": f"{peer}发布云服务产品，企业客户增长20%，订阅收入继续提升",
            "status": "supported",
        }, ensure_ascii=False), True
    rows = []
    for peer, business, relation in [
        ("PeerA", "云服务", "直接竞争"),
        ("PeerB", "企业软件订阅", "细分业务可比"),
        ("PeerC", "AI平台", "商业模式可比"),
    ]:
        rows.append({
            "peer_name": peer,
            "ticker_hint": peer.upper(),
            "relation_type": relation,
            "comparable_business": business,
            "evidence_sentence": f"{peer}在{business}方面与目标公司可比",
            "discovery_article_id": "T1",
            "confidence": 0.8,
        })
    return json.dumps(rows, ensure_ascii=False), True


def _resolver(candidate: dict) -> list[dict]:
    name = candidate["peer_name"]
    return [{"name": name, "ticker": name.upper(), "market": "US", "entity_id": name.upper()}]


def _material_fetcher(peer: dict, question: str, days_back: int, size: int) -> list[dict]:
    name = peer["peer_name"]
    return [{
        "id": f"{name}_M1",
        "title": f"{name}云服务产品进展",
        "text": f"{name} {peer['comparable_business']} 发布云服务产品，企业客户增长20%，订阅收入继续提升。",
        "metadata": {
            "id": f"{name}_M1",
            "publishTime": "2026-07-10",
            "organization": "Mock Research",
        },
        "type": "Materials V2",
    }]


def _source_trace(peer_count: int = 3, wrong_peer_role: bool = False) -> dict:
    sources = [{
        "id": "T1",
        "type": "Datayes Research",
        "publishTime": "2026-07-12",
        "source_role": "target_research",
        "company_match": "exact_target",
    }]
    for i, peer in enumerate(["PEERA", "PEERB", "PEERC"][:peer_count], start=2):
        sources.append({
            "id": f"Peer{chr(63 + i)}_M1",
            "type": "Materials V2",
            "publishTime": "2026-07-10",
            "source_role": "target_research" if wrong_peer_role and i == 2 else "peer_business_progress",
            "company_match": "exact_target" if wrong_peer_role and i == 2 else "exact_peer",
            "peer_ticker": peer,
        })
    return {"sources": sources}


def _report(peer_count: int = 3, duplicate: bool = False, target_not_first: bool = False,
            bad_progress_ref: bool = False) -> str:
    peers = [
        ("PeerA", "PEERA", 2),
        ("PeerB", "PEERB", 3),
        ("PeerC", "PEERC", 4),
    ][:peer_count]
    if duplicate and peers:
        peers[-1] = peers[0]
    rows = [
        "| 基准公司 | 目标公司（00001.HK） | 云服务[1] | 云服务领先[1] | 云服务[1] | 平台订阅[1] | 企业客户[1] | AI平台[1] | 云服务客户增长[1] | 2026-07-12 |"
    ]
    for name, ticker, ref in peers:
        progress_ref = 1 if bad_progress_ref and name == "PeerA" else ref
        rows.append(
            f"| 直接竞争[1] | {name}（{ticker}） | 云服务[1] | {name}客户增长[{ref}] | 云服务[1] | "
            f"{name}平台订阅[{ref}] | {name}企业客户[{ref}] | {name}云服务产品[{ref}] | "
            f"{name}发布云服务产品[{progress_ref}] | 2026-07-10 |"
        )
    if target_not_first and len(rows) > 1:
        rows = rows[1:] + rows[:1]
    return "\n".join([
        "# 目标公司（00001.HK）港股公司一页纸",
        "",
        "## 9 行业对比与 A/H 映射",
        "",
        "### 9.1 同业业务对比",
        "",
        "| " + " | ".join(PEER_TABLE_COLUMNS) + " |",
        "|" + "|".join(":---" for _ in PEER_TABLE_COLUMNS) + "|",
        *rows,
        "",
        "## 13 参考资料",
        "",
        "[1]Datayes Research | 2026-07-12 | ID: T1 | Mock | 目标公司云服务同业比较 | API: batchGetReportContent",
        "[2]Materials V2 | 2026-07-10 | ID: PeerA_M1 | Mock | PeerA云服务产品进展 | API: getMaterialsV2",
        "[3]Materials V2 | 2026-07-10 | ID: PeerB_M1 | Mock | PeerB云服务产品进展 | API: getMaterialsV2",
        "[4]Materials V2 | 2026-07-10 | ID: PeerC_M1 | Mock | PeerC云服务产品进展 | API: getMaterialsV2",
    ])


def _check(content: str, trace: dict):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        json.dump(trace, f, ensure_ascii=False)
        path = f.name
    return check_peer_comparison_source_trace(content, "HK", path)


class PeerComparisonR10Tests(unittest.TestCase):
    def test_bundle_discovers_resolves_fetches_and_renders_target_plus_three_peers(self):
        bundle = build_peer_comparison_bundle(
            _materials(), "目标公司", "00001.HK", "HK",
            llm_call=_llm_call, resolver=_resolver, material_fetcher=_material_fetcher,
        )
        self.assertEqual(bundle["valid_peer_rows"], 3)
        self.assertEqual(bundle["status"], "supported")
        merged = merge_peer_sources_into_materials(_materials(), bundle)
        peer_sources = merged["materials_v2"]["unique_sources"]
        self.assertEqual(len(peer_sources), 3)
        self.assertTrue(all(s["company_match"] == "exact_peer" for s in peer_sources))
        ref_map = {
            1: {"id": "T1"},
            2: {"id": "PeerA_M1"},
            3: {"id": "PeerB_M1"},
            4: {"id": "PeerC_M1"},
        }
        section = build_peer_comparison_section(bundle, ref_map)
        self.assertIn("| " + " | ".join(PEER_TABLE_COLUMNS) + " |", section)
        self.assertIn("| 基准公司 | 目标公司（00001.HK）", section)
        self.assertIn("PeerA（PEERA）", section)
        self.assertNotIn("### 9.2", section)

    def test_checker_accepts_target_first_and_three_peer_specific_rows(self):
        gate = _check(_report(3), _source_trace(3))
        self.assertEqual(gate.p1, 0, [i.message for i in gate.issues])
        self.assertEqual(gate.p2, 0, [i.message for i in gate.issues])

    def test_checker_warns_for_two_peers_and_blocks_one_peer(self):
        two = _check(_report(2), _source_trace(2))
        self.assertEqual(two.p1, 0, [i.message for i in two.issues])
        self.assertGreaterEqual(two.p2, 1)
        one = _check(_report(1), _source_trace(1))
        self.assertGreaterEqual(one.p1, 1)

    def test_checker_blocks_self_only_target_not_first_duplicate_and_wrong_peer_ref(self):
        self.assertGreaterEqual(_check(_report(0), _source_trace(0)).p1, 1)
        self.assertGreaterEqual(_check(_report(3, target_not_first=True), _source_trace(3)).p1, 1)
        self.assertGreaterEqual(_check(_report(3, duplicate=True), _source_trace(3)).p1, 1)
        self.assertGreaterEqual(_check(_report(3, bad_progress_ref=True), _source_trace(3)).p1, 1)
        self.assertGreaterEqual(_check(_report(3), _source_trace(3, wrong_peer_role=True)).p1, 1)


if __name__ == "__main__":
    unittest.main()
