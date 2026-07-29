#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构核验器 — 检查章节完整性、顺序、表格位置，基于市场 Profile 驱动。
"""

from __future__ import annotations

import re
from typing import Any
from dataclasses import dataclass


@dataclass
class StructureFinding:
    severity: str
    category: str
    location: str
    issue: str
    evidence: str = ""
    suggestion: str = ""


def detect_market(materials: dict[str, Any]) -> str:
    """从 materials JSON 推断市场（ticker格式优先于meta.market，防止误标）。"""
    meta = materials.get("__meta__") or {}
    ticker = str(meta.get("ticker") or "")

    # ticker格式优先推断（防止fetch_materials误标）
    if ticker:
        if len(ticker) == 6 and ticker.isdigit():
            return "A"
        if ticker.endswith(".HK") or (len(ticker) == 5 and ticker.isdigit()):
            return "HK"

    market = str(meta.get("market") or "").upper()
    if market in {"A", "HK", "US"}:
        return market

    # 最后 fallback
    if ticker.isalpha() or "." in ticker:
        return "US"
    return "HK"


def load_profile(market: str) -> dict[str, Any]:
    """加载对应市场的验证Profile。"""
    import json
    from pathlib import Path
    # v1.2.2: check both local dir and repo root
    candidates = [
        Path(__file__).resolve().parent.parent / "validation_profiles",
        Path(__file__).resolve().parent.parent.parent.parent / "validation_profiles",  # repo root
    ]
    profile_dir = None
    for d in candidates:
        if d.exists():
            profile_dir = d
            break
    if profile_dir is None:
        profile_dir = candidates[0]
    filename = {"HK": "hk_share.json", "US": "us_share.json", "A": "a_share.json"}.get(market, "hk_share.json")
    with open(profile_dir / filename, "r", encoding="utf-8") as f:
        return json.load(f)


_HEADING_PATTERN = re.compile(r"^#{1,3}\s+(.*)$")


def parse_headings(md: str) -> list[dict[str, Any]]:
    """解析文中所有标题行。"""
    headings = []
    for lineno, raw in enumerate(md.splitlines(), 1):
        m = _HEADING_PATTERN.match(raw.strip())
        if not m:
            continue
        title = m.group(1).strip()
        level = raw.strip().count("#", 0, 3)
        headings.append({"line": lineno, "level": level, "title": title, "raw": raw.strip()})
    return headings


def match_section(title: str, section_def: dict[str, Any]) -> bool:
    """判断标题是否匹配某个section定义。"""
    if not section_def.get("title_pattern"):
        return False
    pattern = section_def["title_pattern"]
    return bool(re.search(pattern, title))


def find_section_heading(headings: list[dict[str, Any]], section_def: dict[str, Any]) -> dict[str, Any] | None:
    """在标题列表中查找匹配的section。"""
    for h in headings:
        if h["level"] == 2 and match_section(h["title"], section_def):
            return h
    return None


def validate_structure(
    md: str,
    materials: dict[str, Any],
) -> tuple[dict[str, Any], list[StructureFinding]]:
    """执行结构核验，返回汇总和问题列表。"""
    market = detect_market(materials)
    profile = load_profile(market)
    findings: list[StructureFinding] = []
    headings = parse_headings(md)
    body, ref_text = split_report(md)

    required_sections = profile["sections"]["required"]
    optional_sections = profile["sections"].get("optional", [])

    # 1. 检测必选章节存在性
    found_sections = {}
    for sec in required_sections:
        if sec.get("is_reference_section"):
            # 参考资料用独立逻辑
            if ref_text.strip():
                found_sections[sec["id"]] = {"heading": None, "line": None}
            continue
        h = find_section_heading(headings, sec)
        if h:
            found_sections[sec["id"]] = h
        else:
            findings.append(StructureFinding(
                "P1", "结构完整性", f"§{sec['id']}",
                f"缺少必选章节「{sec['aliases'][0] if sec['aliases'] else sec['id']}」。",
                suggestion=f"按{market}市场模板补充该章节。",
            ))

    # 2. 检测章节顺序
    ordered_ids = [s["id"] for s in required_sections if not s.get("is_reference_section")]
    found_order = [
        (sid, found_sections[sid]["line"])
        for sid in ordered_ids
        if sid in found_sections and found_sections[sid]["line"] is not None
    ]
    for i in range(1, len(found_order)):
        prev_id, prev_line = found_order[i - 1]
        curr_id, curr_line = found_order[i]
        if curr_line < prev_line:
            findings.append(StructureFinding(
                "P2", "章节顺序", f"§{prev_id} → §{curr_id}",
                f"章节§{curr_id}（行{curr_line}）出现在§{prev_id}（行{prev_line}）之前，不符合{market}市场模板顺序。",
            ))

    # 3. 检测重复章节
    section_titles = {}
    for h in headings:
        if h["level"] != 2:
            continue
        for sec in required_sections + [{"id": s, "aliases": [s], "title_pattern": re.escape(s)} for s in optional_sections]:
            if sec.get("is_reference_section"):
                continue
            if match_section(h["title"], sec):
                if sec["id"] in section_titles:
                    findings.append(StructureFinding(
                        "P2", "章节重复", f"行{h['line']}",
                        f"章节「{h['title']}」疑似重复（首次出现在行{section_titles[sec['id']]}）。",
                    ))
                else:
                    section_titles[sec["id"]] = h["line"]
                break

    # 4. 检测空章节
    for h in headings:
        if h["level"] != 2:
            continue
        next_h = _next_heading(headings, h)
        next_line = next_h["line"] if next_h else len(md.splitlines()) + 1
        sec_content = "\n".join(md.splitlines()[h["line"]:next_line - 1])
        # 去掉标题行后是否为空
        content_without_heading = "\n".join(
            l for l in sec_content.splitlines()
            if not l.strip().startswith(f"{'#' * h['level']} ")
        ).strip()
        if not content_without_heading or content_without_heading in {"—", "-", "N/A"}:
            for sec in required_sections:
                if match_section(h["title"], sec):
                    findings.append(StructureFinding(
                        "P1", "章节内容", f"行{h['line']}",
                        f"必选章节「{h['title']}」内容为空。",
                        suggestion="补充内容或标注数据缺失原因。",
                    ))
                    break

    # 5. 必选表格检查
    required_tables = profile.get("required_tables", [])
    for table_req in required_tables:
        sec_id = table_req["section_id"]
        h = found_sections.get(sec_id)
        if not h:
            continue
        # 取该章节内容
        all_h2 = [head for head in headings if head["level"] == 2]
        sec_idx = next((i for i, head in enumerate(all_h2) if head["line"] == h["line"]), None)
        if sec_idx is None:
            continue
        next_sec = all_h2[sec_idx + 1] if sec_idx + 1 < len(all_h2) else None
        start = h["line"]
        end = next_sec["line"] - 1 if next_sec else len(md.splitlines())
        sec_content = "\n".join(md.splitlines()[start:end])
        rows = _parse_table_rows(sec_content)
        if len(rows) < table_req["min_rows"]:
            findings.append(StructureFinding(
                "P2", "表格缺失", f"§{sec_id}",
                f"缺少必选表格「{table_req['name']}」或行数不足（要求≥{table_req['min_rows']}行，实际{len(rows)}行）。",
            ))

    # 6. 参考资料位置：必须在最后
    if ref_text.strip():
        last_content_line = max(
            (h["line"] for h in headings if h["level"] == 2 and "参考资料" not in h["title"]),
            default=0,
        )
        ref_headings = [h for h in headings if "参考资料" in h["title"]]
        if ref_headings and last_content_line > 0:
            ref_line = ref_headings[0]["line"]
            if last_content_line > ref_line:
                findings.append(StructureFinding(
                    "P1", "章节顺序", f"行{ref_line}",
                    "参考资料章节不在报告末尾，其后还有正文内容。",
                ))

    # 7. 标题层级检查：不应跳级（## 后直接 ####）
    prev_level = 0
    for h in headings:
        if h["level"] > prev_level + 1 and prev_level > 0:
            findings.append(StructureFinding(
                "P2", "标题层级", f"行{h['line']}",
                f"标题「{h['title']}」层级为H{h['level']}，上一级为H{prev_level}，疑似跳级。",
            ))
        prev_level = h["level"]

    # 8. 报告标题格式检查（由市场Profile驱动）
    lines = md.splitlines()
    first_line = lines[0].strip() if lines else ""
    title_config = profile.get("title_format", {})
    title_pattern = title_config.get("pattern", r'#\s+.+（.+）.*公司一页纸')
    conclusion_required = title_config.get("conclusion_required", True)

    title_match = re.match(title_pattern, first_line)

    if not title_match and first_line.startswith('#'):
        # 标题格式完全不匹配
        findings.append(StructureFinding(
            "P1", "标题格式", "行1",
            f"报告标题不符合格式规范。要求包含「股票简称（代码）公司一页纸」，当前标题：{first_line[:80]}",
            suggestion=f"按{market}市场模板规范修正标题格式。",
        ))
    elif title_match and conclusion_required:
        # 检查是否包含结论部分（：后内容）
        if not re.search(r'[：:]\s*.+', first_line):
            findings.append(StructureFinding(
                "P2", "标题格式", "行1",
                f"报告标题缺少「：{{一句话结论}}」部分，当前标题：{first_line[:80]}",
                suggestion="补充约20字的一句话结论，体现核心投资判断。",
            ))

    # 9. 财务章节数据完整性检查
    fin_sec_id = profile.get("financial_section_id", "7")
    fin_heading = None
    for h in headings:
        if h["level"] == 2:
            for sec in required_sections:
                if sec.get("id") == fin_sec_id and match_section(h["title"], sec):
                    fin_heading = h
                    break
    if fin_heading:
        next_h = _next_heading(headings, fin_heading)
        end_line = next_h["line"] - 1 if next_h else len(lines)
        fin_content = "\n".join(lines[fin_heading["line"]:end_line])
        # Check table has actual numbers (not all "—")
        table_rows = []
        in_table = False
        for line in fin_content.splitlines():
            if line.strip().startswith("|") and not line.strip().startswith("|:"):
                if not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in [x.strip() for x in line.strip().strip("|").split("|")]):
                    table_rows.append(line.strip())
        if table_rows:
            has_real_data = False
            for row in table_rows[1:]:  # skip header
                cells = [c.strip() for c in row.strip("|").split("|")]
                for c in cells[1:]:
                    if re.search(r'\d', c) and c not in {"—", "-", "N/A", ""}:
                        has_real_data = True
                        break
            if not has_real_data:
                findings.append(StructureFinding(
                    "P1", "财务数据", f"行{fin_heading['line']}",
                    f"财务章节表格全部为占位符（无实际数字）。",
                    suggestion="从结构化接口（fdmtNew/HK PIT）或研报中提取真实财务数据填充表格。",
                ))

    # 10. H3小节编号一致性检查（v1.2.2新增）
    # 友邦保险扩展测试中「3 核心投资逻辑」下出现「2.1 短期逻辑」，原评测器仅检查H2级别章节顺序，
    # 未检查H3小节编号与父H2的一致性，导致父子编号错配未被检出。
    _H_NUM = re.compile(r'^(\d+)')
    _H3_NUM = re.compile(r'^(\d+)\.(\d+)')
    parent_h2 = None
    h2_num = None
    h2_title = None
    seen_h3_nums: dict[str, set[int]] = {}

    for h in headings:
        if h["level"] == 2:
            m = _H_NUM.match(h["title"])
            if m:
                h2_num = m.group(1)
                h2_title = h["title"]
                parent_h2 = h
                if h2_num not in seen_h3_nums:
                    seen_h3_nums[h2_num] = set()
        elif h["level"] == 3 and h2_num is not None:
            m = _H3_NUM.match(h["title"])
            if m:
                h3_parent = m.group(1)
                h3_child = int(m.group(2))
                if h3_parent != h2_num:
                    findings.append(StructureFinding(
                        "P1", "小节编号", f"行{h['line']}",
                        f"H3小节「{h['title']}」的父编号({h3_parent})与所属H2「{h2_title}」的章节编号({h2_num})不一致。",
                        evidence=f"实际标题：{h['title']}，预期父编号：{h2_num}，所属H2：{h2_title}",
                        suggestion=f"将小节编号改为 {h2_num}.{h3_child} 以匹配父章节。",
                    ))
                if h3_child in seen_h3_nums.get(h2_num, set()):
                    findings.append(StructureFinding(
                        "P1", "小节编号", f"行{h['line']}",
                        f"H3小节「{h['title']}」的编号在§{h2_num}内重复。",
                        suggestion="修复重复的小节编号。",
                    ))
                seen_h3_nums[h2_num].add(h3_child)

    # 汇聚
    passed = sum(1 for f in findings)
    summary = {
        "market": market,
        "profile": f"{market}_share",
        "sections_found": len(found_sections),
        "sections_required": len([s for s in required_sections if not s.get("is_reference_section")]),
        "structure_checks_passed": len(required_sections) - passed,
        "structure_checks_failed": passed,
        "structure_checks_skipped": 0,
    }

    return summary, findings


def _next_heading(headings: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any] | None:
    """找到当前标题后下一个同级或上级标题（跳过子标题）。"""
    for h in headings:
        if h["level"] <= current["level"] and h["line"] > current["line"]:
            return h
    return None


def _parse_table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
            continue
        rows.append(cells)
    return rows


def split_report(md: str) -> tuple[str, str]:
    """分割报告正文和参考资料。"""
    m = re.search(r"^(?:#{1,3}\s+)?\d*\s*参考资料\s*$", md, flags=re.M)
    if m:
        return md[:m.start()], md[m.end():]
    marker = "## 13 参考资料"
    if marker in md:
        return md.split(marker, 1)
    return md, ""
