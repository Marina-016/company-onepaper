#!/usr/bin/env python3
"""v1.2.4 报告质量门禁 —— 分层 Gate 架构（重构自 v123 的平铺 check 1-39）。

8 层 Gate:
  1. Delivery     —— 产物存在性检查
  2. Structure    —— 结构完整性检查
  3. Citation     —— 引用与来源闭环
  4. Data         —— 数据真实性与口径
  5. Section      —— 章节质量
  6. Table        —— 表格质量
  7. Market       —— 港美股差异
  8. Hygiene      —— 输出清洁度

输出格式: Gate | Status | P0 | P1 | P2 | Issues
Gate 状态: PASS(0 P0) / WARN(0 P0 但有 P1/P2) / FAIL(P0>0)

用法: python check_report_quality_v124.py <report.md> [--market A|HK|US] [--json] [--output-dir <dir>]
退出: 0=PASS, 1=FAIL(含P0), 2=WARN(仅P1/P2无P0)

旧 check_id 对照: 每条 issue 括号内保留原 check_id，如 [Citation.C1/check11]
"""

from __future__ import annotations
import argparse, re, sys, json, os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum


# ── 类型定义 ──────────────────────────────────────────────────

class Severity(Enum):
    P0 = "P0"  # blocking
    P1 = "P1"  # should fix
    P2 = "P2"  # nice to fix

class GateStatus(Enum):
    PASS = "✓ PASS"
    WARN = "⚠ WARN"
    FAIL = "✗ FAIL"

@dataclass
class Issue:
    gate: str
    check_id: str
    severity: Severity
    message: str
    old_check_ref: str = ""  # e.g. "check11"

@dataclass
class GateResult:
    name: str
    status: GateStatus = GateStatus.PASS
    p0: int = 0
    p1: int = 0
    p2: int = 0
    issues: List[Issue] = field(default_factory=list)

# ── 常量 ──────────────────────────────────────────────────────

P0, P1, P2 = Severity.P0, Severity.P1, Severity.P2

# 港美股 13 章结构
HK_US_H2_TEMPLATE = [
    (1, "关键要点"),
    (2, "近况跟踪"),
    (3, "核心投资逻辑"),
    (4, "催化事件时间表"),
    (5, "业务拆分"),
    (6, "产销链与生态"),
    (7, "财务与盈利质量"),
    (8, "市场关注"),
    (9, "行业对比"),
    (10, "市场分歧"),
    (11, "估值与预测"),
    (12, "风险提示"),
    (13, "参考资料"),
]

# A 股 11 章结构（旧模板）
A_SHARE_H2_TEMPLATE = [
    (1, "公司近况跟踪"),
    (2, "核心投资逻辑"),
    (3, "催化事件时间表"),
    (4, "业务深度分析"),
    (5, "产销链与生态"),
    (6, "财务与盈利质量"),
    (7, "市场分歧与多空观点"),
    (8, "行业分析及同业对比"),
    (9, "估值与预测"),
    (10, "风险提示"),
    (11, "参考资料"),
]

# 港美股必填 H2
HK_US_REQUIRED_H2 = {
    2: "近况跟踪",
    4: "催化事件时间表",
    5: "业务拆分",
    7: "财务与盈利质量",
    9: "行业对比",
    11: "估值与预测",
    12: "风险提示",
    13: "参考资料",
}

# A 股必填 H2
A_REQUIRED_H2 = {
    3: "催化事件时间表",
    9: "估值与预测",
}

# ── 辅助函数 ──────────────────────────────────────────────────

def _extract_body_and_refs(content: str, market: str) -> Tuple[str, str, int]:
    """拆分正文与参考资料，返回 (body, ref_part, ref_start_index)。"""
    # Try multiple patterns to find reference section
    patterns = [
        r'\n## 13 参考资料',
        r'\n## 参考资料',
        r'\n## 11 参考资料',
    ]
    ref_start = -1
    for pat in patterns:
        m = re.search(pat, content)
        if m:
            ref_start = m.start()
            break
    if ref_start == -1:
        # Fallback: search from end
        for kw in ['参考资料', '## 13 ', '## 11 ']:
            idx = content.rfind(kw)
            if idx > len(content) * 0.7:  # must be in latter 30%
                ref_start = idx
                break

    body = content[:ref_start] if ref_start > 0 else content
    ref_part = content[ref_start:] if ref_start > 0 else ""
    return body, ref_part, ref_start


def _find_section(content: str, patterns: List[str], start_marker: str = None, end_markers: List[str] = None) -> Optional[str]:
    """Find a section by regex patterns or manual index."""
    for pat in patterns:
        m = re.search(pat, content, re.DOTALL)
        if m:
            return m.group(0)
    if start_marker:
        idx = content.find(start_marker)
        if idx >= 0:
            end_idx = len(content)
            for end in (end_markers or []):
                e = content.find(end, idx + 1)
                if e > 0 and e < end_idx:
                    end_idx = e
            return content[idx:end_idx]
    return None


def _extract_tables(content: str) -> List[str]:
    """Extract all Markdown tables from content."""
    return re.findall(r'(\|.+\|.*\n(?:\|.+\|.*\n)+)', content)

def _infer_target(content: str) -> tuple[str, str]:
    first = content.splitlines()[0] if content else ""
    ticker_m = re.search(r'[（(]([A-Za-z0-9.]+)[）)]', first)
    ticker = ticker_m.group(1).upper().replace(".HK", "") if ticker_m else ""
    name = re.sub(r'^#\s*', '', first).split("（")[0].split("(")[0].strip()
    return name, ticker

def _target_semantic_profile(content: str) -> dict:
    name, ticker = _infer_target(content)
    t = ticker.upper()
    if t == "NVDA" or "NVIDIA" in name.upper():
        return {"label": "NVIDIA", "bad_terms": ["MAU", "DAU", "ARPU", "外卖", "本地生活", "即时零售", "支付/云/广告", "内容/游戏/广告", "用户生态", "阿里巴巴", "美团（", "美团("], "bad_peers": ["阿里巴巴", "美团", "腾讯", "快手", "Kuaishou"]}
    if t in {"03690", "3690"} or "美团" in name:
        return {"label": "美团", "bad_terms": ["Microsoft", "NVIDIA", "GPU", "CUDA", "Blackwell", "云与企业服务", "内容/游戏/广告", "MAU/DAU/ARPU"], "bad_peers": ["Microsoft", "NVIDIA"]}
    if t == "META" or "META" in name.upper():
        return {"label": "Meta", "bad_terms": ["Flutter", "FLUT"], "bad_peers": ["Flutter", "FLUT"]}
    if t == "600519" or "茅台" in name:
        return {"label": "贵州茅台", "bad_terms": ["光模块", "800G", "1.6T", "新能源汽车", "动力电池", "GPU", "CUDA"], "bad_peers": ["新易盛", "天孚通信", "光迅科技", "比亚迪"]}
    if t == "002594" or "比亚迪" in name:
        return {"label": "比亚迪", "bad_terms": ["新易盛", "天孚通信", "光迅科技", "光模块", "800G", "1.6T", "AI资本开支波动"], "bad_peers": ["新易盛", "天孚通信", "光迅科技"]}
    return {}

def _load_source_trace(md_dir: str) -> dict:
    for name in ("source_trace.json", "report_source_trace.json"):
        p = os.path.join(md_dir, name)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


# ═══════════════════════════════════════════════════════════════
# GATE 1: Delivery —— 产物存在性检查
# ═══════════════════════════════════════════════════════════════

def check_delivery(md_path: str, market: str) -> GateResult:
    """检查输出产物是否齐全。"""
    gate = GateResult(name="Delivery")
    md_dir = os.path.dirname(os.path.abspath(md_path)) if os.path.dirname(md_path) else "."
    md_basename = os.path.splitext(os.path.basename(md_path))[0]

    # D1: MD 文件存在
    if not os.path.exists(md_path):
        gate.issues.append(Issue("Delivery", "D1", P0,
            f"MD报告文件不存在: {md_path}", "check(产物)"))
        gate.p0 += 1
    else:
        fsize = os.path.getsize(md_path)
        if fsize < 500:
            gate.issues.append(Issue("Delivery", "D1", P0,
                f"MD报告文件过小({fsize}字节)——疑似空壳", "check(产物)"))
            gate.p0 += 1

    # D2: DOCX 存在
    docx_path = os.path.join(md_dir, md_basename + ".docx")
    alt_docx = md_path.replace('.md', '.docx')
    docx_exists = os.path.exists(docx_path) or os.path.exists(alt_docx)
    if not docx_exists:
        gate.issues.append(Issue("Delivery", "D2", P1,
            f"未找到对应DOCX文件(期望: {docx_path})", "check(产物)"))

    # D3: source_trace.json / id_audit.json / materials.json 存在
    # A股: 结构化接口+研报+纪要引用链路为合法来源链, 不强制要求 HK/US 的 source_trace.json
    if market == "A":
        trace_files = []
        # A股: 查找 *_data.json 或 data.json 作为采集产物标识
        for cand in os.listdir(md_dir) if os.path.isdir(md_dir) else []:
            if cand.endswith('_data.json') or cand == 'data.json':
                tp = os.path.join(md_dir, cand)
                if os.path.isfile(tp):
                    trace_files.append(cand)
        # A股: 若报告正文含参考资料章节且引用编号≥3, 视为引用链路完整
        if not trace_files:
            with open(md_path, 'r', encoding='utf-8') as f:
                a_content = f.read()
            ref_entries = re.findall(r'^\[(\d+)\]', a_content, re.M)
            if len(set(ref_entries)) >= 3:
                trace_files.append('inline_references')
    else:
        trace_files = ['source_trace.json', 'id_audit.json', 'materials.json',
                       f'{md_basename}_source_trace.json', f'{md_basename}_id_audit.json']
    found_traces = []
    for tf in trace_files:
        tp = os.path.join(md_dir, tf)
        if os.path.exists(tp):
            found_traces.append(tf)
    if not found_traces:
        gate.issues.append(Issue("Delivery", "D3", P1,
            "未找到 source_trace.json / id_audit.json / materials.json", "check(产物)"))

    # D4: report header metadata. HK/US only keeps market and generation date.
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    header_info = content[:800] if len(content) > 800 else content
    missing_header = []
    if market in ("HK", "US"):
        meta_line = ""
        for line in content.splitlines()[1:6]:
            if line.strip().startswith("市场"):
                meta_line = line.strip()
                break
        expected_market = "港股" if market == "HK" else "美股"
        if not meta_line:
            missing_header.append('市场')
            missing_header.append('生成日期')
        else:
            if not re.fullmatch(rf'市场：{expected_market} \| 生成日期：\d{{4}}-\d{{2}}-\d{{2}}', meta_line):
                gate.issues.append(Issue("Delivery", "D4_HKUS_HEADER_SCHEMA", P1,
                    f"港美股报告头部必须严格为: 市场：{expected_market} | 生成日期：YYYY-MM-DD", "header-schema"))
            if "行业：" in meta_line or "当前价格/市值：" in meta_line:
                gate.issues.append(Issue("Delivery", "D4_HKUS_HEADER_LEGACY", P1,
                    "港美股报告头部不得出现'行业：'或'当前价格/市值：'", "header-schema"))
            if not re.search(rf'市场：{expected_market}', meta_line):
                missing_header.append('市场')
            if not re.search(r'生成日期：\d{4}-\d{2}-\d{2}', meta_line):
                missing_header.append('生成日期')
    else:
        if not re.search(r'市场[：:]', header_info) and market != "A":
            missing_header.append('市场')
        if not re.search(r'(?:ticker|代码|股票代码)', header_info, re.I) and not re.search(r'[\(（]\d{4,6}[\.\w]*[\)）]', header_info):
            missing_header.append('ticker/代码')
        if not re.search(r'生成日期[：:]', header_info) and not (market == "A" and re.search(r'\*\*日期\*\*|日期[：:]', header_info)):
            missing_header.append('生成日期')
        if not re.search(r'当前价格|价格/市值', header_info) and not (market == "A" and re.search(r'PE\(TTM\)|PB', header_info)):
            missing_header.append('价格/市值口径')
    if missing_header:
        gate.issues.append(Issue("Delivery", "D4", P1,
            f"报告头信息缺失: {', '.join(missing_header)}", "check(口径)"))

    # Update gate counters
    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 2: Structure —— 结构完整性检查
# ═══════════════════════════════════════════════════════════════

def check_structure(content: str, market: str) -> GateResult:
    """检查13章结构完整性、编号正确性。"""
    gate = GateResult(name="Structure")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)

    # S1: 必需章节完整性
    h2s = re.findall(r'^## (\d+) (.+)$', content, re.M)
    h2_map = {int(n): t.strip() for n, t in h2s}
    h2_count = len(set(h2_map.keys()))

    if market in ("HK", "US"):
        template = HK_US_H2_TEMPLATE
        required = HK_US_REQUIRED_H2
    else:
        template = A_SHARE_H2_TEMPLATE
        required = A_REQUIRED_H2

    missing_required = []
    for sec_num, sec_name in required.items():
        if sec_num not in h2_map:
            missing_required.append(f"§{sec_num} {sec_name}")
    if missing_required:
        gate.issues.append(Issue("Structure", "S1", P1,
            f"缺失必填章节: {', '.join(missing_required)}", "check33/check30"))

    if market in ("HK", "US") and h2_count < 9:
        gate.issues.append(Issue("Structure", "S1", P1,
            f"港美股报告章节过少(仅{h2_count}个H2)——疑似缩略版", "check33"))

    # S2: H2 编号无重复 (check5)
    h2_nums = [int(n) for n, _ in h2s]
    h2_counter = Counter(h2_nums)
    for num, cnt in h2_counter.items():
        if cnt > 1:
            gate.issues.append(Issue("Structure", "S2", P0,
                f"H2重复: ## {num} 出现{cnt}次", "check5"))

    # S3: H3 父子编号一致性 + 近空检测 (check3)
    h3_pattern = re.compile(r'^### (.+)$', re.M)
    h3_matches = list(h3_pattern.finditer(content))
    allowed_a_h3 = {'4.1', '4.2', '4.3', '4.4', '4.5',
                    '6.1', '6.2', '8.1',
                    '9.1', '9.2', '9.3', '9.4',
                    '2.1', '2.2'}
    for i, m in enumerate(h3_matches):
        h3_title = m.group(1).strip()
        # 提取 H3 编号
        h3_num_match = re.match(r'(\d+)\.(\d+)', h3_title)
        h3_parent = None
        if h3_num_match:
            h3_parent = int(h3_num_match.group(1))

        # 查找所属 H2
        before = content[:m.start()]
        h2_before = re.findall(r'^## (\d+) ', before, re.M)
        expected_parent = int(h2_before[-1]) if h2_before else None

        if h3_parent is not None and expected_parent is not None and h3_parent != expected_parent:
            gate.issues.append(Issue("Structure", "S3", P1,
                f"H3编号({h3_title[:40]})与父H2(§{expected_parent})不一致——H3父={h3_parent}", "check3"))

        # A 股豁免检查
        if market == "A" and h3_num_match:
            h3_key = h3_num_match.group(0)
            if h3_key in allowed_a_h3:
                continue

        # 近空 H3 检测
        start = m.end()
        end = h3_matches[i+1].start() if i+1 < len(h3_matches) else len(content)
        section_body = content[start:end]
        if '|' in section_body and re.search(r'^\|.+\|$', section_body, re.M):
            continue
        clean = re.sub(r'\|.*\|', '', section_body, flags=re.DOTALL)
        clean = re.sub(r'^#.*$', '', clean, flags=re.M)
        clean = re.sub(r'^\*[^*].*$', '', clean, flags=re.M)
        clean = re.sub(r'^[-–•].*$', '', clean, flags=re.M)
        clean = re.sub(r'\s+', '', clean)
        if len(clean) < 30:
            gate.issues.append(Issue("Structure", "S3", P1,
                f"近乎空的H3: {h3_title[:50]}", "check3"))

    # S4: §11.2 空预测表省略 (check38)
    if market in ("HK", "US"):
        fcast_m = re.search(r'### 11\.2 盈利预测分析.*?(?=### 11\.\d|## 12|\Z)', content, re.DOTALL)
        if fcast_m:
            fcast_section = fcast_m.group(0)
            has_table = bool(re.search(r'^\|.+\|$', fcast_section, re.M))
            if has_table:
                data_cells = re.findall(r'\|\s*(\d+\.?\d*)\s*(?:\[?\d*\]?)?\s*\|', fcast_section)
                placeholders = ['待提取', '数据未获取', '待从研报', '暂未获取', '尚未获取']
                has_placeholder = any(p in fcast_section for p in placeholders)
                all_empty = not data_cells or len(data_cells) <= 2
                if has_placeholder and all_empty:
                    gate.issues.append(Issue("Structure", "S4", P1,
                        "§11.2机构盈利预测表存在但无有效数值——应整节省略", "check38"))
                elif has_placeholder:
                    gate.issues.append(Issue("Structure", "S4", P2,
                        "§11.2机构盈利预测表含占位符——单行无具体数值应隐藏", "check38"))

    # S5: 无英文残留标题 (NEW)
    english_h2 = re.findall(r'^## \d+ [A-Z]', content, re.M)
    if english_h2:
        gate.issues.append(Issue("Structure", "S5", P1,
            f"英文残留标题: {', '.join(e[:30] for e in english_h2)}", "NEW"))

    # S6: 标题格式正确性 (AUDIT enhanced)
    first_line = content.split('\n')[0] if content else ''
    title_forbidden = ("深度分析", "投资价值分析", "近期研报", "持续关注", "主业韧性：", "核心业务增长")
    title_bad_endings = tuple("、：:的利业，,；;")
    if market in ("HK", "US"):
        # P0: catch "HK市场/US市场公司一页纸" or "深度分析" as conclusion
        bad_market = re.match(r'^# .+?(?:HK|US)市场公司一页纸', first_line)
        if bad_market:
            gate.issues.append(Issue("Structure", "S6", P0,
                f"标题写'{bad_market.group(0)[:40]}'——应为'港股公司一页纸'或'美股公司一页纸'", "AUDIT-P0"))
        expected_market_title = "港股公司一页纸" if market == "HK" else "美股公司一页纸"
        if expected_market_title not in first_line:
            gate.issues.append(Issue("Structure", "S6", P1,
                f"{market}标题缺少'{expected_market_title}'市场标识", "AUDIT-P1"))
        bad_conclusion = re.search(r'公司一页纸[：:]\s*(?:深度分析|跟踪报告|公司概览|投资分析|市场分析)\s*$', first_line)
        if bad_conclusion:
            gate.issues.append(Issue("Structure", "S6", P0,
                "标题结论为'深度分析'/'跟踪报告'等非投资判断标签——需改为15-25字投资结论", "AUDIT-P0"))
    if not first_line.strip() or "一页纸" not in first_line:
        gate.issues.append(Issue("Structure", "S6", P1,
            "报告标题为空或缺少'一页纸'", "check(标题)"))
    if not re.search(r'公司一页纸[：:]', first_line):
        gate.issues.append(Issue("Structure", "S6", P1,
            "报告标题格式不符合规范(需含'公司一页纸：'及市场标识)", "check(标题)"))
    # Check conclusion length (15-25 chars)
    conc_match = re.search(r'公司一页纸[：:]\s*(.+)$', first_line)
    if conc_match:
        conclusion = conc_match.group(1).strip()
        zh_len = len(re.findall(r'[\u4e00-\u9fff]', conclusion))
        if not conclusion:
            gate.issues.append(Issue("Structure", "S6", P1,
                "标题缺少一句话投资结论", "AUDIT-P1"))
        if any(term in first_line for term in title_forbidden):
            gate.issues.append(Issue("Structure", "S6", P1,
                "标题含禁用材料摘要词或泛化标签", "AUDIT-P1"))
        if conclusion.endswith(title_bad_endings) or "：" in conclusion or ":" in conclusion:
            gate.issues.append(Issue("Structure", "S6", P1,
                "标题结论疑似句子截断或含第二个冒号", "AUDIT-P1"))
        judgment_terms = ("驱动", "受益", "稳健", "韧性", "延续", "打开", "修复", "改善", "支撑", "增量", "需求", "利润率", "龙头")
        if not any(term in conclusion for term in judgment_terms):
            gate.issues.append(Issue("Structure", "S6", P1,
                "标题缺少明确投资判断", "AUDIT-P1"))
        if len(conclusion) < 8 and market in ("HK", "US"):
            gate.issues.append(Issue("Structure", "S6", P1,
                f"标题结论过短('{conclusion}'，{len(conclusion)}字)——需15-25字投资判断", "AUDIT-P1"))
        elif zh_len > 30:
            gate.issues.append(Issue("Structure", "S6", P2,
                f"标题结论过长('{conclusion[:20]}…'，{zh_len}个中文字符)——建议≤25字", "AUDIT-P2"))
    else:
        gate.issues.append(Issue("Structure", "S6", P1,
            "标题无一句话投资结论", "AUDIT-P1"))
    # S6b: 标题不得包含"标题生成失败"占位符 (r11b)
    if "标题生成失败" in first_line:
        gate.issues.append(Issue("Structure", "S6", P0,
            "标题包含'标题生成失败'占位符——标题生成链路必须兜底为投资结论", "r11b-P0"))

    # S7: §1 关键要点不能为空 (AUDIT P0)
    sec1 = _find_section(content, [r'## 1 关键要点.*?(?=## 2 |\Z)'])
    if sec1:
        sec1_clean = re.sub(r'^#{1,4} .*$', '', sec1, flags=re.M)
        sec1_clean = re.sub(r'\|.*\|', '', sec1_clean, flags=re.DOTALL)
        sec1_clean = re.sub(r'\s+', '', sec1_clean)
        if len(sec1_clean) < 30:
            gate.issues.append(Issue("Structure", "S7", P0,
                "§1关键要点为空——这是买方一页纸最重要的章节，必须输出4-6条投资结论", "AUDIT-P0"))
        else:
            bullets = re.findall(r'^[\*\-•]\s*(.{10,})', sec1, re.M)
            if len(bullets) < 3:
                gate.issues.append(Issue("Structure", "S7", P1,
                    f"§1关键要点仅{len(bullets)}条(期望4-6条投资结论)", "AUDIT"))

    # S8: §3 核心投资逻辑不能为空 (AUDIT P0)
    sec3 = _find_section(content, [r'## 3 核心投资逻辑.*?(?=## 4 |\Z)'])
    if sec3:
        sec3_clean = re.sub(r'^#{1,4} .*$', '', sec3, flags=re.M)
        sec3_clean = re.sub(r'\|.*\|', '', sec3_clean, flags=re.DOTALL)
        sec3_clean = re.sub(r'\s+', '', sec3_clean)
        if len(sec3_clean) < 30:
            gate.issues.append(Issue("Structure", "S8", P0,
                "§3核心投资逻辑为空——必须输出3.1短期逻辑+3.2长期逻辑", "AUDIT-P0"))

    # S9: §10 市场分歧不能为空 (AUDIT P0)
    sec10 = _find_section(content, [r'## 10 市场分歧.*?(?=## 11 |\Z)'])
    if sec10:
        sec10_clean = re.sub(r'^#{1,4} .*$', '', sec10, flags=re.M)
        sec10_clean = re.sub(r'\|.*\|', '', sec10_clean, flags=re.DOTALL)
        sec10_clean = re.sub(r'\s+', '', sec10_clean)
        # Exempt: valid multi-empty table exists (HEADER is the content)
        tables_10 = _extract_tables(sec10)
        has_bull_bear = any(
            all(kw in tbl.split('\n')[0] for kw in ['多头', '空头'])
            for tbl in tables_10
        ) if tables_10 else False
        if len(sec10_clean) < 30 and not has_bull_bear:
            gate.issues.append(Issue("Structure", "S9", P0,
                "§10市场分歧为空——必须输出多空对照四列表", "AUDIT-P0"))

    # S10: §5/§6/§7/§8 不能是空壳 (AUDIT P0)
    for sec_num, sec_name in [(5, '业务拆分'), (6, '产销链与生态'), (7, '财务与盈利质量'), (8, '市场关注')]:
        sec = _find_section(content, [rf'## {sec_num} {sec_name}.*?(?=## {sec_num+1} |\Z)'])
        if sec:
            # Exempt: sections with substantive tables (table content IS section content)
            tables_in_sec = _extract_tables(sec)
            has_table_content = any(
                len(tbl.split('\n')) >= 4 and any(re.search(r'\d', row) for row in tbl.split('\n'))
                for tbl in tables_in_sec
            ) if tables_in_sec else False
            if has_table_content:
                continue  # Table content counts as substantive
            sec_clean = re.sub(r'^#{1,4} .*$', '', sec, flags=re.M)
            sec_clean = re.sub(r'\|.*\|', '', sec_clean, flags=re.DOTALL)
            sec_clean = re.sub(r'\s+', '', sec_clean)
            if len(sec_clean) < 50:
                gate.issues.append(Issue("Structure", "S10", P0,
                    f"§{sec_num}{sec_name}为空壳——必须有结构化内容", "AUDIT-P0"))

    # S11: §11.4 "待研报补充"占位符 (AUDIT P0)
    scenario = re.search(r'(?:### 11\.[34] 情景推演).*?(?=## 12|\Z)', content, re.DOTALL)
    if scenario:
        if '待研报补充' in scenario.group(0):
            gate.issues.append(Issue("Structure", "S11", P0,
                "§11.4情景推演含'待研报补充'占位符——若无数据应删除本节而非填占位符", "AUDIT-P0"))

    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 3: Citation & Trace —— 引用与来源闭环
# ═══════════════════════════════════════════════════════════════

def check_citation(content: str, market: str) -> GateResult:
    """检查引用闭环、编号连续性、source_trace 一致性。"""
    gate = GateResult(name="Citation & Trace")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)

    # C1: 正文引用全部在参考资料中定义 (check11 orphan)
    body_refs = set(int(x) for x in re.findall(r'\[(\d+)\]', body))
    ref_defs = set(int(x) for x in re.findall(r'^\[(\d+)\]', ref_part, re.M)) if ref_part else set()
    orphan = body_refs - ref_defs
    if orphan:
        gate.issues.append(Issue("Citation", "C1", P0,
            f"孤引用(正文引用但参考资料未定义): {sorted(orphan)}", "check11"))

    # C2: 参考资料全部被正文使用 (check11 unused)
    unused = ref_defs - body_refs
    if unused:
        gate.issues.append(Issue("Citation", "C2", P2,
            f"死引用(参考资料定义但正文未用): {sorted(unused)}", "check11"))

    # C3: 引用编号连续从[1]开始 (check23)
    if body_refs:
        sorted_refs = sorted(body_refs)
        if sorted_refs[0] != 1:
            gate.issues.append(Issue("Citation", "C3", P1,
                f"引用编号未从[1]开始，首个引用为[{sorted_refs[0]}]", "check23"))
        for i in range(len(sorted_refs) - 1):
            if sorted_refs[i+1] - sorted_refs[i] > 1:
                gate.issues.append(Issue("Citation", "C3", P1,
                    f"引用编号不连续: [{sorted_refs[i]}]→[{sorted_refs[i+1]}]", "check23"))
                break

    # C4: 无 SRC-X 残留 (check10)
    if re.search(r'SRC-\d+', body):
        gate.issues.append(Issue("Citation", "C4", P0,
            "报告正文含内部编号 SRC-X", "check10"))

    # C5: 参考资料格式——纯文本列表非表格
    if ref_part:
        ref_tables = re.findall(r'(\|.+\|.*\n(?:\|.+\|.*\n)+)', ref_part)
        if ref_tables:
            gate.issues.append(Issue("Citation", "C5", P0,
                "参考资料使用了Markdown表格——必须用纯文本列表", "check(格式)"))

    # C6: 参考资料 [N] 后禁止有空格——正确格式为 [1]Materials V2研报 | ... (check24)
    if ref_part:
        # Detect whitespace between ] and the type field
        spaced_refs = re.findall(r'^\[(\d+)\]\s+', ref_part, re.M)
        if spaced_refs:
            gate.issues.append(Issue("Citation", "C6", P1,
                f"参考资料[序号]后有空格(应为[N]来源类型): refs={spaced_refs[:5]}", "check24"))

    # C7: source_trace 闭环检测 —— 尝试读取 source_trace.json
    # 通过尝试查找同目录下的 source_trace.json
    md_dir = os.path.dirname(os.path.abspath(__file__))  # placeholder, actual path passed differently
    # 此检查在 CLI 入口统一处理

    # C8: 参考资料日期非空检测 (v1.2.4-R2: 从注释落地为实际检查)
    # 参考资料格式: [N]类型 | 日期 | ID: xxx | 机构 | 标题 | API: xxx
    # 检测 "| |" (空日期) 或 "|  |" 模式
    if ref_part:
        ref_lines = ref_part.strip().split('\n')
        missing_date_refs = []
        for line in ref_lines:
            if not re.match(r'^\[\d+\]', line):
                continue
            # 分割 pipe 字段
            parts = [p.strip() for p in line.split('|')]
            # parts[0]: [N]类型, parts[1]: 日期, parts[2]: ID, ...
            if len(parts) >= 3 and not parts[1]:
                ref_match = re.match(r'^\[(\d+)\]', line)
                if ref_match:
                    missing_date_refs.append(ref_match.group(1))
        if missing_date_refs:
            gate.issues.append(Issue("Citation", "C8", P1,
                f"参考资料缺少日期(共{len(missing_date_refs)}条): refs=[{','.join(missing_date_refs[:8])}]",
                "check(date)"))

    # C8b: Datayes结构化接口参考资料展示格式
    if ref_part:
        structured_ref_lines = [
            line.strip() for line in ref_part.splitlines()
            if re.match(r'^\[\d+\]Datayes结构化接口\b', line.strip())
        ]
        structured_schema = re.compile(
            r'^\[\d+\]Datayes结构化接口 \| \d{4}-\d{2}-\d{2} \| [^|]+ \| [^|]+ \| API：[^|:：\s]+$'
        )
        for line in structured_ref_lines:
            if re.search(r'structured:get|ID:|API_ID|API:', line):
                gate.issues.append(Issue("Citation", "C8b", P1,
                    f"结构化接口参考资料含旧格式字段: {line[:100]}", "AUDIT-P1"))
            if not structured_schema.match(line):
                gate.issues.append(Issue("Citation", "C8b", P1,
                    f"结构化接口参考资料格式错误: {line[:100]}", "AUDIT-P1"))

    # C9: 引用不得只挂在纯表头/机构名/评级/来源名等非事实单元格；
    # 描述性事实、事件、业务判断、关键假设允许带引用。
    if ref_part:
        bad_cells = []
        label_headers = ("机构", "评级", "来源")
        pure_label_terms = ("Datayes", "Research", "材料", "来源", "证券", "公司")
        for tbl in _extract_tables(body):
            lines = [x for x in tbl.splitlines() if x.strip().startswith("|")]
            if len(lines) < 3:
                continue
            headers = [c.strip() for c in lines[0].strip().strip("|").split("|")]
            for line in lines[2:]:
                if re.match(r'^\|\s*:?-+', line):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                for idx, cell in enumerate(cells):
                    if not re.search(r'\[\d+\]', cell):
                        continue
                    header = headers[idx] if idx < len(headers) else ""
                    plain = re.sub(r'\[\d+\]', '', cell).strip()
                    has_fact_signal = bool(re.search(r'\d|增长|下降|提升|改善|验证|发布|披露|收入|利润|现金流|资本开支|产品|业务|客户|毛利率|市占率|订单|销量|出货|回购|分红', plain))
                    if re.search(r'引用|来源|数据来源|Source', header, re.I):
                        continue
                    if any(h in header for h in label_headers) and not has_fact_signal:
                        bad_cells.append(cell[:60])
                    elif len(plain) <= 12 and any(t in plain for t in pure_label_terms) and not has_fact_signal:
                        bad_cells.append(cell[:60])
        if bad_cells:
            gate.issues.append(Issue("Citation", "C9", P1,
                f"引用挂在纯标签/来源/机构名单元格: {bad_cells[0]}", "check(引位)"))

    # C10: §11.1 机构目标价多家共用同一引用 (AUDIT P0)
    # 检测: 同一 [N] 出现在多行机构目标价附近
    target_section = _find_section(body, [r'### 11\.1 机构目标价汇总.*?(?=###|## 12|\Z)',
                                           r'### 11\.1 .*?目标价.*?(?=###|## 12|\Z)'])
    if target_section:
        # Extract all [N] refs from each row of the target price table
        target_rows = re.findall(r'^\|.+\|$', target_section, re.M)
        ref_to_orgs: dict[int, set[str]] = {}
        for row in target_rows:
            if re.match(r'^\|\s*:?-+', row) or re.search(r'机构|目标价', row):
                continue
            refs = [int(x) for x in re.findall(r'\[(\d+)\]', row)]
            org_match = re.match(r'\|\s*([^|\d]+?)(?:\s*\[|\s*\||$)', row)
            org = org_match.group(1).strip() if org_match else row[:30]
            if refs:
                for r in set(refs):
                    ref_to_orgs.setdefault(r, set()).add(org)
        for ref_num, orgs in ref_to_orgs.items():
            if len(orgs) >= 3:
                gate.issues.append(Issue("Citation", "C10", P0,
                    f"引用[{ref_num}]被{len(orgs)}家不同机构目标价共用——每家机构必须绑定独立引用", "AUDIT-P0"))

    # C11: 来源内容与引用支撑的事实类型不匹配 (AUDIT P1)
    # 检测: §11.1 所有目标价只引用 [1] 但 [1] 在参考资料中标注为微信/市场观点/行业研报
    if ref_part and body_refs:
        target_prices_section = _find_section(body, [r'### 11\.1 机构目标价汇总.*?(?=### 11\.[23]|### 11\.[34]|## 12|\Z)'])
        if target_prices_section:
            target_refs = set(int(x) for x in re.findall(r'\[(\d+)\]', target_prices_section))
            # If only 1 ref used for ALL target prices, check what type it is
            if len(target_refs) == 1:
                sole_ref = list(target_refs)[0]
                ref_line_match = re.search(rf'^\[{sole_ref}\](.+?)\|', ref_part, re.M)
                if ref_line_match:
                    ref_desc = ref_line_match.group(0)[:100]
                    suspect_types = ['微信', 'wechat', '市场观点', 'marketView', '公众号']
                    if any(t in ref_desc for t in suspect_types):
                        gate.issues.append(Issue("Citation", "C11", P1,
                            f"§11.1所有机构目标价仅引用[{sole_ref}]但来源类型疑似非研报({ref_desc[:50]}...)", "AUDIT-P1"))

    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 4: Data Integrity —— 数据真实性与口径
# ═══════════════════════════════════════════════════════════════

def check_data_integrity(content: str, market: str) -> GateResult:
    """检查数据真实性、口径一致性、禁止估算。"""
    gate = GateResult(name="Data Integrity")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)

    # D1: 核心章节无引用 (check8)
    core_patterns = {
        "估值与预测": r'## (?:9|11) .*?(?=## (?:10|12)|\Z)',
    }
    for sec_name, pattern in core_patterns.items():
        m = re.search(pattern, content, re.DOTALL)
        if m and not re.findall(r'\[(\d+)\]', m.group(0)):
            gate.issues.append(Issue("Data", "D1", P0,
                f"核心章节'{sec_name}'无任何引用", "check8"))

    # D2: 估值分析小节无引用 (check12)
    for vs in [r'### 9\.[34] 估值分析.*?(?=###|## 10|\Z)',
               r'### 11\.[23] 估值分析.*?(?=###|## 12|\Z)',
               r'### 11\.[34] 情景推演.*?(?=###|## 12|\Z)']:
        m = re.search(vs, content, re.DOTALL)
        if m and not re.findall(r'\[(\d+)\]', m.group(0)):
            gate.issues.append(Issue("Data", "D2", P0,
                "估值分析/情景推演小节无引用", "check12"))

    # D3: 经营指标必须有引用 (check17)
    op_metrics = ['MAU', 'DAU', 'ARPU', 'GMV', 'take rate', '用户数', '付费用户',
                  '流水', '市场份额', '云收入', '广告收入', '月活', '日活', '装机量',
                  '出货量', '渗透率', '市占率']
    body_only = re.sub(r'\|.*\|', '', content, flags=re.DOTALL)
    body_only = re.sub(r'^#{1,4} .*$', '', body_only, flags=re.M)
    if ref_start > 0:
        body_only = body_only[:ref_start]
    for metric in op_metrics:
        for m in re.finditer(re.escape(metric), body_only):
            nearby = body_only[max(0, m.start()-200):m.end()+200]
            if re.search(r'\[(\d+)\]', nearby):
                continue
            if any(kw in nearby for kw in ('测算', '推算', '验证', '问题', '关注点', '?')):
                continue
            gate.issues.append(Issue("Data", "D3", P1,
                f"经营指标'{metric}'附近无引用[N]", "check17"))

    # D4: 会计准则混用 (check29)
    has_ifrs = bool(re.search(r'IFRS|Non-IFRS|经调整', body[:2000]))
    has_gaap = bool(re.search(r'GAAP', body[:2000]))
    if has_ifrs and has_gaap:
        gate.issues.append(Issue("Data", "D4", P1,
            "会计准则混用: IFRS与GAAP不应同时出现", "check29"))

    # D5: 币种混用 (check29)
    if re.search(r'当前价格.*USD.*港元|当前价格.*HKD.*人民币|价格.*\$.*HK\$', content[:2000]):
        gate.issues.append(Issue("Data", "D5", P1,
            "币种混用: 价格/市值单位应统一", "check29"))

    # D6: 估值口径一致性 (check29/34)
    if re.search(r'PE\(TTM\)[：:].*\d{4}E', content):
        gate.issues.append(Issue("Data", "D6", P1,
            "估值口径混用: PE(TTM)与预测年度(如2026E)不应同时出现", "check29"))
    valuation_section = re.search(r'(?:### 9\.[34] 估值分析|### 11\.[23] 估值分析).*?(?=###|\Z)', content, re.DOTALL)
    if valuation_section:
        v_text = valuation_section.group(0)
        has_pe_ttm = bool(re.search(r'PE.*TTM|TTM.*PE', v_text))
        has_pe_forward = bool(re.search(r'PE.*\d{4}E|预测PE|forward.*PE', v_text))
        if has_pe_ttm and has_pe_forward:
            gate.issues.append(Issue("Data", "D6", P2,
                "估值分析同时引用PE(TTM)和预测PE——建议明确标注口径差异", "check34"))

    # D7: 禁止历史财务数据使用"约/大约/估计约"等估算表述
    # v1.2.4-R2: 仅限历史财务章节，不得误伤预测/估值/前瞻性/内部测算语境
    # 历史财务真实值必须精确；预测/内部测算/目标价/估值可以标注"预测"/"内部测算"/"约"
    financial_contexts = [
        r'(?:收入|营收|净利润|毛利|总资产|净资产|现金流|经营现金流|自由现金流)[^。\n]{0,30}?约\s*\d+',
    ]
    fuzzy_found = False
    # 识别历史财务章节位置 (A股§6.x, HK/US§7)
    hist_sec_range = None
    for pat_h in [r'## 6\s', r'## 7\s']:
        m_h = re.search(pat_h, content)
        if m_h:
            # 下一个同级标题到此为止
            next_h2 = re.search(r'## \d+\s', content[m_h.end():])
            end = m_h.end() + next_h2.start() if next_h2 else len(content)
            hist_sec_range = (m_h.start(), end)
            # 也匹配同级H3子章节
            for m_h3 in re.finditer(r'### \d+\.\d+', content):
                if m_h3.start() > m_h.start() and m_h3.start() < end:
                    hist_sec_range = (m_h.start(), max(end, m_h3.end()))
            break
    for pat in financial_contexts:
        for m in re.finditer(pat, body):
            ctx = body[max(0, m.start()-60):m.end()]
            # 豁免: 目标价/普通股/评级/机构/预测/指引/前瞻性/风险语境
            if any(kw in ctx for kw in ('目标价', '普通股', '评级', '机构', '指引', '一致预期',
                                         '市场一致', '分析师', '预测', '预计', '估值', '情景',
                                         '假设', '内部测算', '模型', '前瞻', 'forward')):
                continue
            # 豁免: 风险/条件/可能性语境
            if any(kw in ctx for kw in ('影响', '若', '如果', '可能导致', '或将', '回购', 'ppt', 'bps', '百分点')):
                continue
            # 豁免: 后续是百分比/比率单位而非金额
            end_part = body[m.end():m.end()+10]
            if re.match(r'\s*(?:ppt|bps|百分点|bp|%|\d)', end_part):
                continue
            # 豁免: 匹配在非历史财务章节 (估值/预测/情景推演/风险/近况跟踪等)
            if hist_sec_range and not (hist_sec_range[0] <= m.start() <= hist_sec_range[1]):
                continue
            gate.issues.append(Issue("Data", "D7", P1,
                f"历史财务数据使用模糊估算({ctx[:50].strip()}...)——财务数据必须使用精确数字", "check(估算)"))
            fuzzy_found = True
            break
        if fuzzy_found:
            break

    # D8: 情景推演质量检查
    # v1.2.4-R2: 不再强制要求EPS×PE公式; 机构目标价为合法锚点, PE倍数可作为内部假设
    # 规则: 1)不允许反推EPS; 2)情景排序必须乐观≥中性≥悲观;
    # 3)目标价必须绑定真实机构引用; 4)经营含义必须含可验证变量
    if '情景推演' in content:
        # D8c: 禁止反推EPS表述 → P1
        if re.search(r'反推\s*EPS|反推\s*每股|EPS\s*假设|EPS假设', content, re.I):
            gate.issues.append(Issue("Data", "D8c", P1,
                "情景推演出现目标价/估值倍数反推EPS的表述", "AUDIT-P1"))
        has_eps_pe = re.search(r'EPS\s*[×x\*·→⇒]\s*PE|每股\s*[×x\*·→⇒]\s*PE|基于\s*EPS\s*[×x\*·→⇒]\s*PE', content)
        has_pev = re.search(r'P/EV\s*[×x\*·→⇒]\s*EV|EV\s*[×x\*·→⇒]\s*P/EV|内含价值\s*[×x\*·→⇒]|P/EV\s*\d', content)
        has_target_x = re.search(r'目标价\s*[=＝≈~→⇒]?\s*[\d.]+\s*[×x\*→⇒]\s*[\d.]+', content)
        has_arrow_price = re.search(r'PE\s*[\d.~-]+[x×倍]?\s*[→⇒]\s*目标价', content)
        has_internal = re.search(r'内部测算', content)
        # 检查情景推演是否包含机构目标价锚点
        has_tp_anchor = bool(re.search(r'目标价\s*\d+|\d+(?:\.\d+)?\s*(?:港元|美元)/(?:普通股|股)\[\d+\]', content))
        has_inst_tp = bool(re.search(r'以.*目标价.*锚|以可回溯.*目标价|机构目标价区间', content))
        # 仅当 (1)无反推EPS且(2)有机构目标价锚点或明确含内部测算语义时, 不因缺EPS×PE公式判P1
        anti_push_ok = not re.search(r'反推\s*EPS|反推\s*每股', content, re.I)
        if not has_eps_pe and not has_pev and not has_target_x and not has_arrow_price:
            if not (anti_push_ok and (has_tp_anchor or has_inst_tp or has_internal)):
                gate.issues.append(Issue("Data", "D8", P1,
                    "情景推演未含机构目标价锚点或可复核公式——需至少包含机构目标价引用或EPS×PE/P/EV×EV",
                    "check14"))

        scenario = re.search(r'(?:### 9\.4 情景推演|### 11\.[34] 情景推演).*?(?=###|##|\Z)', content, re.DOTALL)
        if scenario:
            sce_text = scenario.group(0)
            eps_by_case = {}
            for row in re.findall(r'^\|.*(?:乐观|中性|悲观).*$', sce_text, re.M):
                label = "乐观" if "乐观" in row else ("中性" if "中性" in row else ("悲观" if "悲观" in row else ""))
                eps_match = re.search(r'(?:EPS|每股盈利|每股收益)[^0-9]{0,16}(\d+(?:\.\d+)?)', row, re.I)
                if label and eps_match:
                    eps_by_case[label] = float(eps_match.group(1))
            if all(k in eps_by_case for k in ("乐观", "中性", "悲观")):
                if not (eps_by_case["乐观"] >= eps_by_case["中性"] >= eps_by_case["悲观"]):
                    gate.issues.append(Issue("Data", "D8d", P1,
                        f"情景推演EPS排序倒挂: 乐观{eps_by_case['乐观']:g}/中性{eps_by_case['中性']:g}/悲观{eps_by_case['悲观']:g}",
                        "AUDIT-P1"))
            for m in re.finditer(r'收入[^|。\n]{0,12}?(\d+(?:\.\d+)?)[^|。\n]{0,20}?净利[^|。\n]{0,12}?(\d+(?:\.\d+)?)', sce_text):
                rev = float(m.group(1)); profit = float(m.group(2))
                if profit > rev:
                    gate.issues.append(Issue("Data", "D8", P0,
                        f"情景推演净利({profit:g})大于收入({rev:g})，数值口径明显错误", "AUDIT-P0"))
            for m in re.finditer(r'(净利率|毛利率)[^0-9]{0,8}(\d+(?:\.\d+)?)\s*%', sce_text):
                pct = float(m.group(2))
                if pct > 100:
                    gate.issues.append(Issue("Data", "D8", P0,
                        f"情景推演{m.group(1)}超过100%({pct:g}%)", "AUDIT-P0"))

    # D8b: target price currency/share basis must be explicit
    target_sec = _find_section(content, [r'### 11\.1 机构目标价汇总.*?(?=### 11\.2|### 11\.3|## 12|\Z)',
                                         r'### 11\.[23] 估值分析.*?(?=### 11\.4|## 12|\Z)'])
    if market in ("HK", "US") and target_sec and re.search(r'目标价.*\d', target_sec):
        has_currency = bool(re.search(r'港元|HKD|美元|USD|\$', target_sec))
        has_share_basis = bool(re.search(r'普通股|ADS|ADR|每股|/股|/普通股', target_sec))
        if not has_currency or not has_share_basis:
            gate.issues.append(Issue("Data", "D8b", P1,
                "目标价未同时标明币种与每股/ADS/普通股口径", "AUDIT-P1"))

    # D9: 港股 §7 无 PIT 三表数据 (AUDIT P1)
    if market == "HK":
        sec7 = _find_section(content, [r'## 7 财务与盈利质量.*?(?=## 8 |## 参考资料|\Z)'])
        if sec7:
            has_pit = bool(re.search(r'getHkFdmt|PIT|Point.in.Time|PIT三表', sec7, re.I))
            has_table = bool(re.search(r'^\|.+\|$', sec7, re.M))
            has_numbers = bool(re.search(r'\|\s*\d+\.?\d*\s*\|', sec7))
            if not has_pit and not (has_table and has_numbers):
                gate.issues.append(Issue("Data", "D9", P1,
                    "港股§7财务未使用PIT三表(getHkFdmtIsPit/BsPit/CfPit)——需标注skipped_with_reason或填入数据", "AUDIT-P1"))
            uses_hk_fin_table = has_pit or bool(re.search(r'\|\s*指标\s*\|\s*FY20\d{2}\s*\|', sec7))
            if uses_hk_fin_table:
                required_refs = ["Datayes结构化接口", "getHkFdmtIsPit", "getHkFdmtBsPit", "getHkFdmtCfPit"]
                missing_refs = [x for x in required_refs if x not in ref_part]
                if missing_refs:
                    gate.issues.append(Issue("Data", "D9b", P1,
                        f"港股§7出现PIT财务数据但参考资料缺少结构化接口来源: {', '.join(missing_refs)}", "AUDIT-P1"))
                source_line = next((line for line in sec7.splitlines() if "数据来源" in line), "")
                source_refs = {int(x) for x in re.findall(r'\[(\d+)\]', source_line)}
                if len(source_refs) < 3:
                    gate.issues.append(Issue("Data", "D9d", P1,
                        "港股§7使用PIT财务数据但表前未以'[N][N][N]'简洁标明三表来源", "AUDIT-P1"))
                long_source_terms = re.findall(r'财务数据使用港股 PIT 三表口径，表格覆盖|港股 PIT 利润表\[\d+\]|港股 PIT 资产负债表\[\d+\]|港股 PIT 现金流量表\[\d+\]', sec7)
                if long_source_terms:
                    gate.issues.append(Issue("Data", "D9g", P2,
                        f"港股§7数据来源表述过长，应只写引用编号: {long_source_terms[0]}", "AUDIT-P2"))
                for tbl in _extract_tables(sec7):
                    header = tbl.splitlines()[0] if tbl.splitlines() else ""
                    if "口径/来源" in header:
                        gate.issues.append(Issue("Data", "D9e", P1,
                            "港股§7财务表仍包含'口径/来源'列，需改为表前统一数据来源", "AUDIT-P1"))
                    if re.search(r'getHkFdmtIsPit|getHkFdmtBsPit|getHkFdmtCfPit', tbl):
                        gate.issues.append(Issue("Data", "D9f", P1,
                            "港股§7财务表内不应出现PIT接口名，接口名仅保留在参考资料", "AUDIT-P1"))
            if re.search(r'三个 PIT 期间|三期|三年|近三年|最近三', sec7):
                period_cols = 0
                for tbl in _extract_tables(sec7):
                    header = tbl.splitlines()[0] if tbl.splitlines() else ""
                    cells = [c.strip() for c in header.split("|")[1:-1]]
                    period_cols = max(period_cols, sum(1 for c in cells if re.search(r'(?:FY)?20\d{2}(?:-\d{2}-\d{2})?|20\d{2}A', c)))
                if period_cols < 3 and "当前仅取得" not in sec7:
                    gate.issues.append(Issue("Data", "D9c", P1,
                        "港股§7声称三期/三年趋势，但财务表不足3个期间列", "AUDIT-P1"))

    # D10: 美股 §7 无 GAAP/non-GAAP/FY/CY 口径标注 (AUDIT P1)
    if market == "US":
        sec7 = _find_section(content, [r'## 7 财务与盈利质量.*?(?=## 8 |## 参考资料|\Z)'])
        if sec7:
            has_gaap = bool(re.search(r'GAAP|Non-GAAP|non-GAAP', sec7, re.I))
            has_fy = bool(re.search(r'FY\d|CY\d|财年|自然年', sec7))
            if not has_gaap:
                gate.issues.append(Issue("Data", "D10", P1,
                    "美股§7财务未区分GAAP/non-GAAP口径——必须显式标注", "AUDIT-P1"))
            if not has_fy:
                gate.issues.append(Issue("Data", "D10", P2,
                    "美股§7财务未标注FY/CY/财年口径", "AUDIT-P2"))

    # D11: §7 财务章节无标准指标表格 (AUDIT P1)
    if sec7_capture := _find_section(content, [r'## 7 财务与盈利质量.*?(?=## 8 |## 参考资料|\Z)', r'## 6 财务与盈利质量.*?(?=## 7 |\Z)']):
        tables_in_sec7 = _extract_tables(sec7_capture)
        has_financial_table = False
        key_metrics = ['收入', '净利', '毛利', 'ROE', '现金流']
        for tbl in tables_in_sec7:
            # Check entire table body (not just header row) for financial metric names
            full_table_text = tbl[:500]  # first 500 chars covers header + first few rows
            first_data_rows = '\n'.join(tbl.split('\n')[:4])  # header + sep + 2 data rows
            if any(k in first_data_rows for k in key_metrics):
                has_financial_table = True
                break
        if not has_financial_table and market in ("HK", "US"):
            gate.issues.append(Issue("Data", "D11", P1,
                "§7财务章节无标准财务指标表格——需包含收入/净利/毛利率/ROE/现金流等关键行", "AUDIT-P1"))

    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 5: Section —— 章节质量
# ═══════════════════════════════════════════════════════════════

def _check_placeholder_financials(content: str, market: str) -> GateResult:
    gate = GateResult(name="Semantic Data Guards")
    sec7 = _find_section(content, [r'## 7 .*?(?=## 8 |\Z)'])
    if sec7 and market in ("HK", "US"):
        bad_placeholder = []
        if re.search(r'\|\s*(?:营业总收入|Revenue|营收)[^|\n]*\|\s*100\s*\|\s*112\s*\|\s*124\s*\|', sec7, re.I):
            bad_placeholder.append("100/112/124")
        if re.search(r'示例数据|占位|待替换|placeholder', sec7, re.I):
            bad_placeholder.append("placeholder")
        if bad_placeholder:
            gate.issues.append(Issue("Data", "D12", P1,
                f"§7财务章节含占位财务数据: {', '.join(sorted(set(bad_placeholder)))}", "AUDIT-P1"))
    _update_gate(gate)
    return gate

def check_source_company_mismatch(content: str, market: str, md_dir: str = ".") -> GateResult:
    gate = GateResult(name="Source Company Match")
    body, _, _ = _extract_body_and_refs(content, market)
    trace = _load_source_trace(md_dir)
    sources = trace.get("sources", []) if isinstance(trace, dict) else []
    if not sources:
        _update_gate(gate)
        return gate
    used = sorted(set(int(x) for x in re.findall(r'\[(\d+)\]', body)))
    profile = _target_semantic_profile(content)
    bad_terms = (profile or {}).get("bad_peers", []) + (profile or {}).get("bad_terms", [])
    for rn in used:
        if rn < 1 or rn > len(sources):
            continue
        src = sources[rn - 1]
        status = src.get("company_match", "")
        title = src.get("title", "") or ""
        if status == "unrelated":
            gate.issues.append(Issue("Citation", "C12", P1,
                f"正文引用[{rn}]来源标记为unrelated: {title[:80]}", "AUDIT-P1"))
        elif profile and any(term and term in title for term in bad_terms):
            sev = P0 if profile.get("label") == "NVIDIA" else P1
            gate.issues.append(Issue("Citation", "C12", sev,
                f"{profile.get('label')}正文引用[{rn}]疑似非目标公司来源: {title[:80]}", "AUDIT-P1"))
    _update_gate(gate)
    return gate

def check_section_quality(content: str, market: str) -> GateResult:
    """检查各章节内容质量。"""
    gate = GateResult(name="Section Quality")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)
    profile = _target_semantic_profile(content)
    if profile:
        hits = [kw for kw in profile.get("bad_terms", []) if kw and kw in body]
        if hits:
            sev = P0 if profile.get("label") == "NVIDIA" and any(x in hits for x in ["MAU", "DAU", "ARPU", "阿里巴巴", "美团（", "美团("]) else P1
            gate.issues.append(Issue("Section", "E13", sev,
                f"{profile.get('label')}报告疑似套用非目标公司模板，出现不相关业务关键词: {', '.join(hits[:8])}", "AUDIT-P1"))

    # E1: §1 关键要点——必须有3-4条投资判断
    sec1 = _find_section(content, [r'## 1 关键要点.*?(?=## 2 |\Z)'])
    if sec1:
        bullets = re.findall(r'^[\*\-•]\s*(.{10,})', sec1, re.M)
        if not (3 <= len(bullets) <= 4):
            gate.issues.append(Issue("Section", "E1", P1,
                f"§1关键要点为{len(bullets)}条(期望3-4条投资判断)", "check(§1)"))
        missing_ref = [b for b in bullets if not re.search(r'\[\d+\]', b)]
        if missing_ref:
            gate.issues.append(Issue("Section", "E1b", P1,
                f"§1关键要点存在无[N]引用的投资判断: {missing_ref[0][:60]}", "check(§1)"))

    # E2: §2 近况跟踪——句首加粗规范 (check39)
    near_term = _find_section(content, [r'## 2 近况跟踪.*?(?=## 3 |\Z)',
                                         r'## 2 .*近况.*?(?=## 3 |\Z)'])
    if near_term:
        bullets = re.findall(r'^[\*\-•]\s*(.+)$', near_term, re.M)
        missing_bold = 0
        missing_ref = 0
        for b in bullets:
            b = b.strip()
            if not b or re.match(r'数据来源|Data source', b):
                continue
            if not re.match(r'\*\*[^*]+\*\*', b):
                missing_bold += 1
            if not re.search(r'\[\d+\]', b):
                missing_ref += 1
        if bullets and not (4 <= len(bullets) <= 6):
            gate.issues.append(Issue("Section", "E2a", P1,
                f"近况跟踪§2为{len(bullets)}条(期望4-6条)", "check39"))
        if missing_bold > 0:
            gate.issues.append(Issue("Section", "E2", P1,
                f"近况跟踪§2中{missing_bold}条bullet缺少句首**加粗关键词**", "check39"))
        if missing_ref > 0:
            gate.issues.append(Issue("Section", "E2", P1,
                f"近况跟踪§2中{missing_ref}条bullet无[N]引用", "check39"))

    sec3 = _find_section(content, [r'## 3 核心投资逻辑.*?(?=## 4 |\Z)'])
    if sec3 and market in ("HK", "US"):
        if "### 3.1 短期逻辑" not in sec3 or "### 3.2 长期逻辑" not in sec3:
            gate.issues.append(Issue("Section", "E2b", P1,
                "§3核心投资逻辑必须拆分为3.1短期逻辑和3.2长期逻辑", "AUDIT-P1"))
        if len(re.findall(r'验证变量', sec3)) < 2:
            gate.issues.append(Issue("Section", "E2c", P1,
                "§3短期/长期逻辑需包含可跟踪验证变量", "AUDIT-P1"))

    # E3: 催化事件——必须有合法三列表且至少4条公司事件 (check18)
    cat_rows = 0
    for sec_num in ['4', '3']:
        sec_pat = rf'## {sec_num} .*?(?=## {int(sec_num)+1} |\Z)'
        sec = re.search(sec_pat, content, re.DOTALL)
        if sec:
            for tbl in _extract_tables(sec.group(0)):
                lines = [l.strip() for l in tbl.strip().splitlines() if l.strip().startswith("|")]
                if len(lines) < 3 or not all(c in lines[0] for c in ["时间", "事件", "影响"]):
                    continue
                rows = []
                for line in lines[2:]:
                    if re.match(r'^\|\s*:?-+', line):
                        continue
                    cells = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cells) >= 3 and all(cells[:3]) and not re.search(r'研报|报告发布|上调目标价|下调目标价|券商', cells[1]):
                        rows.append(line)
                cat_rows = max(cat_rows, len(rows))
            if cat_rows:
                break
    if cat_rows == 0:
        gate.issues.append(Issue("Section", "E3", P1,
            "催化事件表未找到有效公司事件(需≥4条有效事件)", "check18"))
    elif cat_rows < 4:
        gate.issues.append(Issue("Section", "E3", P1,
            f"催化事件表仅{cat_rows}条有效公司事件(需≥4条有效事件)", "check18"))
    if market in ("HK", "US"):
        cat_section = _find_section(content, [r'## 4 催化事件时间表.*?(?=## 5 |\Z)',
                                               r'## 3 .*?催化.*?(?=## 4 |\Z)']) or ""
        generic_events = [
            "季度业绩与经营 KPI 更新", "季度业绩更新", "季度业绩与经营数据更新",
            "季度业绩与经营KPI更新", "核心产品/业务进展披露", "核心业务进展",
            "下一轮业绩电话会", "新一轮业绩电话会",
        ]
        generic_count = sum(1 for kw in generic_events if kw in cat_section)
        if generic_count > 1:
            gate.issues.append(Issue("Section", "E3b", P1,
                f"§4催化事件含{generic_count}条泛化日历事件，需替换为具体产品/财报/监管/资本配置动作", "AUDIT-P1"))
        strict_valid_rows = 0
        malformed_rows = []
        missing_ref_rows = []
        for tbl in _extract_tables(cat_section):
            lines = [l.strip() for l in tbl.strip().splitlines() if l.strip().startswith("|")]
            if len(lines) < 3 or not all(c in lines[0] for c in ["时间", "事件", "影响"]):
                continue
            for line in lines[2:]:
                if re.match(r'^\|\s*:?-+', line):
                    continue
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if len(cells) != 3 or any(not c for c in cells[:3]):
                    malformed_rows.append(line[:100])
                    continue
                if re.search(r'研报|报告发布|上调目标价|下调目标价|券商', cells[1]):
                    continue
                strict_valid_rows += 1
                if not re.search(r'\[\d+\]', cells[1]) or not re.search(r'\[\d+\]', cells[2]):
                    missing_ref_rows.append(line[:100])
        if strict_valid_rows < 4:
            gate.issues.append(Issue("Section", "E3c", P1,
                f"§4催化事件有效完整行仅{strict_valid_rows}条，需≥4条", "AUDIT-P1"))
        if malformed_rows:
            gate.issues.append(Issue("Section", "E3d", P1,
                f"§4催化事件表存在残缺行或缺列: {malformed_rows[0]}", "AUDIT-P1"))
        if missing_ref_rows:
            gate.issues.append(Issue("Section", "E3e", P1,
                f"§4催化事件事件列和影响列必须同时含引用: {missing_ref_rows[0]}", "AUDIT-P1"))

    # E4: §5 业务拆分——分业务表完整 (NEW)
    sec5 = _find_section(content, [r'## 5 业务拆分.*?(?=## 6 |\Z)'])
    if sec5:
        tables = _extract_tables(sec5)
        has_biz_table = any('业务' in t or '收入' in t or '占比' in t for t in tables)
        if not has_biz_table and not re.search(r'产品矩阵|商业模式|收费方式', sec5):
            gate.issues.append(Issue("Section", "E4", P1,
                "§5业务拆分为看到分业务数据表或产品矩阵/KPI表", "check(§5)"))

    # E5: §7 财务质量——关键指标表完整 (NEW)
    sec7 = _find_section(content, [r'## 7 财务与盈利质量.*?(?=## 8 |\Z)',
                                    r'## 6 财务与盈利质量.*?(?=## 7 |\Z)'])
    if sec7:
        key_metrics = ['营业总收入', '归母净利润', '毛利率', '净利率', 'ROE']
        missing_metrics = [m for m in key_metrics if m not in sec7]
        if len(missing_metrics) >= 3:
            gate.issues.append(Issue("Section", "E5", P2,
                f"§7财务表缺失关键指标: {', '.join(missing_metrics[:3])}", "check(§7)"))

    if market in ("HK", "US"):
        sec6 = _find_section(content, [r'## 6 产销链与生态.*?(?=## 7 |\Z)']) or ""
        if sec6:
            zh_count = len(re.findall(r'[\u4e00-\u9fff]', sec6))
            bullets = re.findall(r'^[\*\-•]\s*(.+)$', sec6, re.M)
            thin_bullets = [
                b for b in bullets
                if len(re.findall(r'[\u4e00-\u9fff]', b)) < 55 or len(re.findall(r'[。；;]', b)) < 2
            ]
            vague_chain = re.findall(r'影响成本、交付和产品节奏|决定收入弹性和客户留存|决定规模化效率与差异化能力', sec6)
            concrete_objects = re.findall(r'内容供给|研发人才|算力资源|支付清算|云基础设施|AI训练|微信生态|游戏|视频号|小程序|广告系统|企业客户|开发者|商户', sec6)
            validation_vars = re.findall(r'资本开支|内容成本|云毛利率|游戏流水|广告收入|eCPM|填充率|商户交易频次|企业客户续约率|付费率|云收入', sec6, re.I)
            if zh_count < 180:
                gate.issues.append(Issue("Section", "E5b", P1,
                    f"§6产销链信息密度不足({zh_count}个中文字符，需≥180)", "AUDIT-P1"))
            elif len(bullets) >= 3 and len(thin_bullets) >= 2:
                gate.issues.append(Issue("Section", "E5b", P2,
                    "§6产销链多个小点少于2句或信息量过低", "AUDIT-P2"))
            if len(vague_chain) >= 2:
                gate.issues.append(Issue("Section", "E5c", P1,
                    "§6产销链仍以泛化传导话术为主，缺少具体资源/产品/客户变量", "AUDIT-P1"))
            if len(concrete_objects) < 4 or len(validation_vars) < 4:
                gate.issues.append(Issue("Section", "E5d", P2,
                    "§6产销链缺少足够具体业务对象或验证变量", "AUDIT-P2"))

    # E6: §9 同业比较——完整 schema (check20)
    peer_section = None
    if market == "A":
        idx = content.find('## 8 行业分析及同业对比')
        if idx >= 0:
            end_idx = content.find('## 9 ', idx + 1)
            peer_section = content[idx:end_idx if end_idx > 0 else len(content)]
    else:
        for pat in [r'## 9 行业对比与 A/H 映射.*?(?=## \d+|\Z)',
                     r'## 9 行业对比.*?(?=## \d+|\Z)']:
            m = re.search(pat, content, re.DOTALL)
            if m:
                peer_section = m.group(0)
                break
        if not peer_section:
            idx = content.find('## 9 行业对比')
            if idx >= 0:
                e = content.find('## 10 ', idx + 1)
                peer_section = content[idx:e if e > 0 else len(content)]

    if market in ("HK", "US"):
        required_peer_cols = ['竞争关系', '公司', '可比业务', '行业地位', '可比维度', '商业模式', '目标客户群体', '核心产品', '最新业务进展', '进展日期']
    else:
        required_peer_cols = ['竞争关系', '公司', '可比业务', '行业地位', '相关业务进展', '商业模式', '目标客户群体', '核心产品']
    peer_tables_found = []
    if peer_section:
        if market in ("HK", "US") and re.search(r'等待补齐|peer-specific|专门同行材料|仅基于目标公司材料', peer_section, re.I):
            gate.issues.append(Issue("Section", "E6i", P1,
                "§9含内部流程话术，不能出现在正式正文", "AUDIT-P1"))
        peer_tables = _extract_tables(peer_section)
        for tbl in peer_tables:
            hdr = tbl.split('\n')[0]
            hdr_cols = [c.strip() for c in hdr.split('|')[1:-1]]
            joined = ','.join(hdr_cols)
            is_peer = any(c in joined for c in ['竞争关系', '可比业务', '行业地位'])
            if is_peer:
                peer_tables_found.append((hdr_cols, tbl))
                missing = [c for c in required_peer_cols if not any(c in hc for hc in hdr_cols)]
                if missing:
                    gate.issues.append(Issue("Section", "E6", P1,
                        f"同业比较表缺必需列: {missing}", "check20"))
                data_rows = [l for l in tbl.strip().split('\n') if re.match(r'^\|.+\|$', l)]
                min_peer_rows = 5 if market in ("HK", "US") else 4
                if len(data_rows) < min_peer_rows:
                    gate.issues.append(Issue("Section", "E6", P1,
                        f"同业比较表行数不足(仅{len(data_rows)}行，需≥{min_peer_rows})", "check20"))
                target_name, target_ticker = _infer_target(content)
                data_only_rows = [r for r in data_rows[2:] if not re.match(r'^\|\s*:?-+', r)]
                first_cells = [c.strip() for c in data_only_rows[0].split("|")[1:-1]] if data_only_rows else []
                has_self = bool(first_cells and first_cells[0] in ("基准公司", "目标公司（基准）") and len(first_cells) > 1 and ((target_name and target_name in first_cells[1]) or (target_ticker and target_ticker.upper().replace(".HK", "") in first_cells[1].upper().replace(".HK", ""))))
                if not has_self:
                    gate.issues.append(Issue("Section", "E6", P1,
                        "同业比较表第一条数据行必须为目标公司基准行", "check20"))
                if market in ("HK", "US"):
                    progress_idx = next((idx for idx, col in enumerate(hdr_cols) if "最新业务进展" in col), -1)
                    if progress_idx >= 0:
                        weak_progress = []
                        for line in data_rows[2:]:
                            if re.match(r'^\|\s*:?-+', line):
                                continue
                            cells = [c.strip() for c in line.split("|")[1:-1]]
                            if len(cells) <= progress_idx:
                                continue
                            company_cell = cells[1] if len(cells) > 1 else ""
                            progress_cell = re.sub(r'\[\d+\]', '', cells[progress_idx]).strip()
                            zh_len = len(re.findall(r'[\u4e00-\u9fff]', progress_cell))
                            generic_only = bool(re.fullmatch(r'.{0,12}(?:商业化|变现|持续验证|持续跟踪|增长|加码)', progress_cell))
                            bad_phrase = bool(re.search(r'相关产品与服务|同类客户群体|本报告分析对象', progress_cell))
                            if zh_len < 12 or generic_only or bad_phrase:
                                weak_progress.append(f"{company_cell}:{progress_cell}"[:80])
                        if weak_progress:
                            gate.issues.append(Issue("Section", "E6e", P1,
                                f"§9最新业务进展过短或泛化: {weak_progress[0]}", "AUDIT-P1"))
                    non_self_peer_rows = []
                    for line in data_rows[2:]:
                        if re.match(r'^\|\s*:?-+', line):
                            continue
                        cells = [c.strip() for c in line.split("|")[1:-1]]
                        if len(cells) < 2:
                            continue
                        rel_cell, company_cell = cells[0], cells[1]
                        is_self_row = rel_cell in ("基准公司", "目标公司（基准）") or (target_name and target_name in company_cell) or (target_ticker and target_ticker in company_cell.upper())
                        if is_self_row:
                            continue
                        non_self_peer_rows.append(line)
                    if len(non_self_peer_rows) < 2:
                        gate.issues.append(Issue("Section", "E6j", P1,
                            "HK/US §9缺少至少2家peer-specific来源支持的非自身peer行", "AUDIT-P1"))

    if peer_section:
        profile = _target_semantic_profile(content)
        if profile:
            bad_peers = [kw for kw in profile.get("bad_peers", []) if kw and kw in peer_section]
            if bad_peers:
                sev = P0 if profile.get("label") == "NVIDIA" else P1
                gate.issues.append(Issue("Section", "E6c", sev,
                    f"{profile.get('label')}同业比较出现明显不相关peer: {', '.join(bad_peers[:8])}", "AUDIT-P1"))
            name, ticker = _infer_target(content)
            if name and re.search(r'\|\s*直接竞争\s*\|\s*' + re.escape(name), peer_section):
                gate.issues.append(Issue("Section", "E6c", P1,
                    "同业比较将目标公司自身列为直接竞争对手", "AUDIT-P1"))

    if not peer_tables_found and peer_section:
        if market == "A":
            has_peer_text = bool(re.search(r'(?:竞争|对手|同行|可比)', peer_section))
            if not has_peer_text:
                gate.issues.append(Issue("Section", "E6", P1,
                    "未找到合格同业比较表——不接受纯文字行业格局描述", "check20"))
        else:
            gate.issues.append(Issue("Section", "E6", P1,
                "未找到合格同业比较表——不接受纯文字行业格局描述", "check20"))

    # E6b: A股行业一致性检查，防止跨公司/跨行业模板污染
    if market == "A":
        first_line = content.splitlines()[0] if content else ""
        mismatch_rules = []
        if "比亚迪" in first_line or "002594" in first_line:
            mismatch_rules.append(("比亚迪", ["新易盛", "天孚通信", "光迅科技", "光模块", "800G", "1.6T", "AI资本开支波动"]))
        for label, bad_terms in mismatch_rules:
            hits = [kw for kw in bad_terms if kw in content]
            if hits:
                gate.issues.append(Issue("Section", "E6b", P1,
                    f"{label}报告疑似跨行业污染，出现不相关关键词: {', '.join(hits[:6])}", "AUDIT-P1"))

    # E10: 港美股§9 禁止模板占位符 (NEW v1.2.4-R2)
    if market in ("HK", "US"):
        forbidden_placeholders = {
            'target company': 'target company',
            'comparable model': 'comparable model',
            'comparable customers': 'comparable customers',
            'comparable product/KPI': 'comparable product/KPI',
            'comparable product': 'comparable product',
        }
        found_forbidden = []
        for kw, label in forbidden_placeholders.items():
            if re.search(re.escape(kw), content, re.I):
                found_forbidden.append(label)
        if found_forbidden:
            gate.issues.append(Issue("Section", "E10", P1,
                f"§9同业比较含模板占位符: {', '.join(found_forbidden)}", "AUDIT-P1"))

        soft_shell_sections = "\n".join([
            _find_section(content, [r'## 1 .*?(?=## 2 |\Z)']) or "",
            _find_section(content, [r'## 2 .*?(?=## 3 |\Z)']) or "",
            _find_section(content, [r'## 3 .*?(?=## 4 |\Z)']) or "",
            _find_section(content, [r'## 8 .*?(?=## 9 |\Z)']) or "",
            _find_section(content, [r'## 9 .*?(?=## 10 |\Z)']) or "",
        ])
        soft_shell_phrases = [
            "\u4e3b\u4e1a\u5224\u65ad\uff1a\u6295\u8d44\u4e3b\u7ebf\u5e94\u56f4\u7ed5",
            "\u589e\u957f\u53d8\u91cf\uff1a\u9700\u8981\u8ddf\u8e2a",
            "\u5229\u6da6\u53d8\u91cf\uff1a\u5e02\u573a\u5206\u6b67\u96c6\u4e2d\u5728",
            "\u4f30\u503c\u53d8\u91cf\uff1a\u76ee\u6807\u4ef7\u548c\u8bc4\u7ea7\u5206\u6b67\u5e94\u56de\u5230",
            "\u4f18\u5148\u6838\u9a8c\u76ee\u6807\u516c\u53f8\u7814\u62a5\u4e2d\u76843\u9879\u6570\u636e",
            "\u672c\u62a5\u544a\u5206\u6790\u5bf9\u8c61",
            "\u540c\u7c7b\u5ba2\u6237\u7fa4\u4f53",
            "\u76f8\u5173\u4ea7\u54c1\u4e0e\u670d\u52a1",
            "\u76ee\u6807\u516c\u53f8\u4ea7\u54c1/\u5e73\u53f0",
            "近期材料显示，市场真正关心的是",
            "而不是单纯给公司贴成长标签",
            "近期材料应重点观察",
            "若目标价与评级出现分歧，需要回到",
            "短期判断需要同时观察收入增速、利润率和现金流3项指标",
            "若其中2项以上改善",
            "target company",
            "comparable model",
            "comparable customers",
            "comparable product/KPI",
        ]
        soft_hits = [p for p in soft_shell_phrases if re.search(re.escape(p), soft_shell_sections, re.I)]
        if soft_hits:
            gate.issues.append(Issue("Section", "E10b", P1,
                f"港美股§1/§2/§3/§8/§9含软模板壳: {', '.join(soft_hits[:6])}", "AUDIT-P1"))
        if peer_section:
            stitched_model_cells = re.findall(r'\|\s*([^|\n]{1,28}\u6a21\u5f0f)\s*\|', peer_section)
            stitched_model_cells = [x for x in stitched_model_cells if len(x) > 4 and x not in ("\u5546\u4e1a\u6a21\u5f0f",)]
            if stitched_model_cells:
                gate.issues.append(Issue("Section", "E10c", P1,
                    f"§9同业比较疑似拼接式商业模式单元格: {', '.join(stitched_model_cells[:5])}", "AUDIT-P1"))
        chain_break_section = _find_section(content, [r'## 3 .*?(?=## 4 |\Z)']) or ""
        if re.search(r'“\s*—|—\s*”|产业链位置是[^。\n]{0,80}—', chain_break_section):
            gate.issues.append(Issue("Section", "E10e", P1,
                "§3.2长期逻辑含产业链硬拼或破折号断裂句", "AUDIT-P1"))
        if re.search(r'&(?:nbsp|amp|lt|gt|quot);', content, re.I):
            gate.issues.append(Issue("Section", "E10f", P1,
                "正文含HTML entity残留(&nbsp;/&amp;等)", "AUDIT-P1"))

    # E7: §10 市场分歧——多空对照表 (NEW)
    sec10 = _find_section(content, [r'## 10 市场分歧.*?(?=## 11 |\Z)',
                                     r'## 7 市场分歧.*?(?=## 8 |\Z)'])
    if sec10:
        has_bull_bear_table = bool(re.search(r'(?:多头|空头|看多|看空).*\|', sec10))
        if not has_bull_bear_table and not re.search(r'多空对照', sec10):
            gate.issues.append(Issue("Section", "E7", P2,
                "§10市场分歧未使用多空对照表格式", "check(§10)"))

    # E8: §11 情景推演——三档+数值+公式 (check13/21/31)
    scenario = _find_section(content,
        [r'(?:### 9\.4 情景推演|### 11\.[34] 情景推演).*?(?=## |### \d+\.\d(?!\.\d)|\Z)'])
    if scenario:
        sc_text = scenario
        # 情景行检测
        sc_rows = re.findall(r'\|\s*(?:乐观|中性|悲观)[^|]*\|[^|]*\|[^|]*\|[^|]*\|', sc_text, re.DOTALL)
        if len(sc_rows) < 3:
            sc_rows = re.findall(r'\|\s*\*\*(?:乐观|中性|悲观)\*\*[^|]*\|[^|]*\|[^|]*\|[^|]*\|', sc_text, re.DOTALL)
        if len(sc_rows) < 3:
            gate.issues.append(Issue("Section", "E8", P0,
                f"情景推演表为空(仅{len(sc_rows)}行，需乐观/中性/悲观三档)", "check13"))
        else:
            for row in sc_rows:
                if re.search(r'\[需结合|\[对应|\[基准|\[下行|请在LLM|移除本行', row):
                    gate.issues.append(Issue("Section", "E8", P0,
                        f"情景推演表含占位符: {row[:60]}", "check13"))
                    break

        # 模板话术检测
        template_phrases = ['基于核心变量乐观假设', '基于核心变量悲观假设',
                           '基于核心变量基准假设', '基于EPS×PE=目标价']
        has_template = any(p in sc_text for p in template_phrases)
        has_numbers = bool(re.search(r'(?:乐观|中性|悲观).*?\d+\.?\d*[%亿x倍美元港元千万百]', sc_text))
        if has_template and not has_numbers:
            gate.issues.append(Issue("Section", "E8", P1,
                "情景推演为模板占位——仅有话术无有效数值", "check31"))
        elif has_template and has_numbers:
            gate.issues.append(Issue("Section", "E8", P1,
                "情景推演含模板占位话术——需全部替换为具体数值", "check31"))

        # 纯数值检查
        if not has_numbers and not has_template:
            gate.issues.append(Issue("Section", "E8", P0,
                "情景推演无具体数值", "check21"))

        has_formula = bool(re.search(r'[=×*]|EPS|每股|目标价|BVPS|P/EV', sc_text))
        if has_numbers and not has_formula:
            gate.issues.append(Issue("Section", "E8", P1,
                "情景推演有数值但无可复核公式", "check21"))

    # E9: §12 风险提示——只要求风险标题清单，每条标题加粗并有引用
    sec12 = _find_section(content, [r'## 12 风险提示.*?(?=## 13 |## 参考资料|\Z)',
                                     r'## 10 风险提示.*?(?=## 11 |\Z)'])
    if sec12:
        risk_bullets = re.findall(r'^[\*\-•]\s+(.+)$', sec12, re.M)
        if not (4 <= len(risk_bullets) <= 6):
            gate.issues.append(Issue("Section", "E9", P1,
                f"§12风险提示仅{len(risk_bullets)}条(期望4-6条)", "check(§12)"))
        for bullet in risk_bullets:
            if not re.search(r'\*\*[^*]{2,24}\*\*', bullet) or not re.search(r'\[\d+\]', bullet):
                gate.issues.append(Issue("Section", "E9b", P1,
                    f"§12风险提示需为加粗标题并带引用: {bullet[:60]}", "check(§12)"))
                break
            plain = re.sub(r'\[\d+\]|\*\*', '', bullet).strip()
            if len(re.findall(r'[\u4e00-\u9fff]', plain)) > 28 or re.search(r'[，。；;].{4,}', plain):
                gate.issues.append(Issue("Section", "E9c", P2,
                    f"§12风险提示正文过长，建议只保留标题: {bullet[:60]}", "check(§12)"))
                break

    # E10: §8 市场关注必须是三列表格 (AUDIT P1)
    sec8 = _find_section(content, [r'## 8 市场关注.*?(?=## 9 |\Z)'])
    if sec8 and market in ("HK", "US"):
        tables_in_sec8 = _extract_tables(sec8)
        has_focus_table = any(
            all(kw in tbl.split('\n')[0] for kw in ['关注点', '担心', '验证'])
            for tbl in tables_in_sec8
        ) if tables_in_sec8 else False
        if not has_focus_table:
            gate.issues.append(Issue("Section", "E10", P1,
                "§8市场关注未使用标准三列表(关注点|市场在担心什么|需要验证的数据)——当前为散文化描述", "AUDIT-P1"))

    # E11: §10 市场分歧必须是多空对照四列表 (AUDIT P1)
    sec10 = _find_section(content, [r'## 10 市场分歧.*?(?=## 11 |\Z)'])
    if sec10 and market in ("HK", "US"):
        tables_in_sec10 = _extract_tables(sec10)
        has_bull_bear = any(
            all(kw in tbl.split('\n')[0] for kw in ['多头', '空头'])
            for tbl in tables_in_sec10
        ) if tables_in_sec10 else False
        if not has_bull_bear:
            gate.issues.append(Issue("Section", "E11", P1,
                "§10市场分歧未使用多空对照四列表(多头观点|证据|空头观点|验证点)——当前为空或散文化描述", "AUDIT-P1"))
        formula_hits = re.findall(r'若兑现[^|。\n]{0,35}上修空间|若兑现|若延续|若转化|可能压制利润率、现金流或估值倍数|收入和利润率有上修空间', sec10)
        if len(formula_hits) >= 2:
            gate.issues.append(Issue("Section", "E11c", P1,
                f"§10市场分歧含重复公式化句式: {formula_hits[0][:60]}", "AUDIT-P1"))
        for tbl in tables_in_sec10:
            header_cells = [c.strip() for c in tbl.splitlines()[0].split("|")[1:-1]] if tbl.splitlines() else []
            evidence_idx = next((i for i, h in enumerate(header_cells) if "证据" in h), -1)
            rows = []
            for line in tbl.splitlines():
                if not line.strip().startswith("|") or re.search(r'^\|\s*:?-+', line):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 4 and not any(h in cells[0] for h in ["\u591a\u5934", "\u7a7a\u5934"]):
                    rows.append(cells)
                    if evidence_idx >= 0 and len(cells) > evidence_idx:
                        evidence = cells[evidence_idx]
                        if re.search(r'腾讯控股[（(]00700(?:\.HK)?[）)]', evidence, re.I):
                            gate.issues.append(Issue("Section", "E11d", P1,
                                f"§10证据列不应出现公司名: {evidence[:80]}", "AUDIT-P1"))
                        if re.search(r'：', evidence) and len(re.sub(r'\[\d+\]', '', evidence)) > 34:
                            gate.issues.append(Issue("Section", "E11e", P2,
                                f"§10证据列疑似搬运研报标题: {evidence[:80]}", "AUDIT-P2"))
            if 0 < len(rows) < 3:
                gate.issues.append(Issue("Section", "E11f", P1,
                    f"§10市场分歧仅{len(rows)}个主题，需≥3个真实分歧主题", "AUDIT-P1"))
            elif len(rows) == 3:
                gate.issues.append(Issue("Section", "E11f", P2,
                    "§10市场分歧为3个主题，可生成但建议补足至4个以上", "AUDIT-P2"))
            if len(rows) >= 3:
                for col_idx, label in [(2, "\u7a7a\u5934\u89c2\u70b9"), (3, "\u9a8c\u8bc1\u70b9")]:
                    values = [r[col_idx] for r in rows if len(r) > col_idx and r[col_idx]]
                    repeated = [v for v, cnt in Counter(values).items() if cnt >= 3]
                    if repeated:
                        gate.issues.append(Issue("Section", "E11b", P1,
                            f"§10市场分歧{label}三行以上重复: {repeated[0][:60]}", "AUDIT-P1"))
                        break

    # E12: §11.1 机构目标价表格式与引用 (AUDIT P1)
    sec11_1 = _find_section(content, [r'### 11\.1 机构目标价汇总.*?(?=### 11\.[23]|## 12|\Z)'])
    if sec11_1 and market in ("HK", "US"):
        # Check for r7 6-column format
        has_correct_format = bool(re.search(r'机构.*\|.*日期.*\|.*评级.*\|.*目标价.*\|.*目标价口径.*\|.*关键假设/关注点', sec11_1))
        has_source_col = bool(re.search(r'机构.*\|.*日期.*\|.*评级.*\|.*目标价.*\|.*目标价口径.*\|.*关键假设/关注点.*\|.*来源', sec11_1))
        has_bad_format = bool(re.search(r'观点方向|主要依据', sec11_1))
        if has_bad_format and not has_correct_format:
            gate.issues.append(Issue("Section", "E12", P1,
                "§11.1目标价表格式偏离规范(应为:机构|日期|评级|目标价|目标价口径|关键假设/关注点)", "AUDIT-P1"))
        if not has_correct_format:
            gate.issues.append(Issue("Section", "E12", P1,
                "§11.1目标价表未使用r7六列表头: 机构|日期|评级|目标价|目标价口径|关键假设/关注点", "AUDIT-P1"))
        if has_source_col:
            gate.issues.append(Issue("Section", "E12", P1,
                "§11.1目标价表不应再包含'来源'列；目标价列已逐机构带引用", "AUDIT-P1"))
        if re.search(r'研报目标价字段', sec11_1):
            gate.issues.append(Issue("Section", "E12b", P1,
                "§11.1不应把'研报目标价字段'写成估值方法，应使用目标价口径说明", "AUDIT-P1"))
        for tbl in _extract_tables(sec11_1):
            header = [c.strip() for c in tbl.splitlines()[0].split("|")[1:-1]] if tbl.splitlines() else []
            assumption_idx = next((i for i, h in enumerate(header) if "关键假设" in h), -1)
            if assumption_idx >= 0:
                for line in tbl.splitlines()[2:]:
                    if not line.strip().startswith("|") or re.search(r'^\|\s*:?-+', line):
                        continue
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if len(cells) <= assumption_idx:
                        continue
                    assumption = re.sub(r'\[\d+\]', '', cells[assumption_idx]).strip()
                    if re.search(r'腾讯控股[（(]00700(?:\.HK)?[）)]', assumption, re.I):
                        gate.issues.append(Issue("Section", "E12c", P1,
                            f"§11.1关键假设含完整公司标题前缀: {assumption[:70]}", "AUDIT-P1"))
                    if re.search(r'：', assumption) and len(assumption) > 32:
                        gate.issues.append(Issue("Section", "E12d", P2,
                            f"§11.1关键假设疑似研报标题截断: {assumption[:70]}", "AUDIT-P2"))
        # Check for dedup: same org appearing multiple times
        orgs = re.findall(r'\|\s*([^|]*?(?:Morgan|Goldman|JPMorgan|花旗|高盛|摩根|中信|招商|国信|天风|广发|华通|Jefferies|UBS|BMO|Barclays|Citi|Deutsche)[^|]*)\s*\|', sec11_1)
        org_counts = Counter(o.strip() for o in orgs)
        duplicates = {k: v for k, v in org_counts.items() if v > 1}
        if duplicates:
            gate.issues.append(Issue("Section", "E12", P1,
                f"§11.1目标价表机构重复: {dict(duplicates)}——需去重", "AUDIT-P1"))

    sec11_4 = _find_section(content, [r'### 11\.4 情景推演.*?(?=## 12|\Z)'])
    if sec11_4 and market in ("HK", "US"):
        if re.search(r'内部估值倍数假设|28x|22x|16x', sec11_4):
            gate.issues.append(Issue("Section", "E12e", P1,
                "§11.4不应出现无来源内部估值倍数假设", "AUDIT-P1"))
        if re.search(r'目标价\s*/\s*PE|倒推每股盈利|反推EPS', sec11_4, re.I):
            gate.issues.append(Issue("Section", "E12f", P1,
                "§11.4不应以目标价/PE倒推每股盈利作为核心逻辑", "AUDIT-P1"))

        def _price_ref_pairs(section: str) -> set[tuple[float, int]]:
            pairs = set()
            for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:港元|美元)/(?:普通股|股)\[(\d+)\]', section):
                pairs.add((float(m.group(1)), int(m.group(2))))
            return pairs

        anchors_11_1 = _price_ref_pairs(sec11_1 or "")
        scenario_pairs = _price_ref_pairs(sec11_4)
        bad_pairs = [p for p in scenario_pairs if p not in anchors_11_1]
        if bad_pairs and anchors_11_1:
            gate.issues.append(Issue("Section", "E12g", P1,
                f"§11.4目标价锚未能在11.1找到同一目标价和引用: {bad_pairs[0]}", "AUDIT-P1"))

        scenario_prices = {}
        for line in sec11_4.splitlines():
            if not line.strip().startswith("|") or re.search(r'^\|\s*:?-+', line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            label = cells[0]
            m = re.search(r'(\d+(?:\.\d+)?)\s*(?:港元|美元)/(?:普通股|股)\[\d+\]', cells[1])
            if m and any(x in label for x in ["乐观", "中性", "悲观"]):
                scenario_prices[label[:2]] = float(m.group(1))
        if all(k in scenario_prices for k in ["乐观", "中性", "悲观"]):
            if not (scenario_prices["乐观"] >= scenario_prices["中性"] >= scenario_prices["悲观"]):
                gate.issues.append(Issue("Section", "E12h", P1,
                    "§11.4乐观/中性/悲观目标价排序错误", "AUDIT-P1"))

    # ── r11b: §9 §10 §11 enhanced checks ──
    _r11b_section_quality_checks(gate, content)
    _update_gate(gate)
    return gate


def _strip_md(text: str) -> str:
    """Strip markdown formatting: headers, bold, links, citation refs. KEEPS table content."""
    if not text:
        return ""
    text = re.sub(r'^#{1,4}\s+.*$', '', text, flags=re.M)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\s+', '', text)
    return text


def _cell_refs(cell: str) -> list[int]:
    return [int(x) for x in re.findall(r'\[(\d+)\]', cell or "")]


def _norm_topic_for_quality(text: str) -> set[str]:
    base = re.sub(r'\[\d+\]|[^\w\u4e00-\u9fff]+', ' ', str(text or "").lower())
    return {w for w in base.split() if len(w) >= 2} | set(re.findall(r'[\u4e00-\u9fff]{2,6}', base))


def _topic_jaccard(a: str, b: str) -> float:
    sa, sb = _norm_topic_for_quality(a), _norm_topic_for_quality(b)
    return len(sa & sb) / max(1, len(sa | sb)) if sa and sb else 0.0


def _r11b_section_quality_checks(gate, content):
    """r11b: enhanced §9 §10 §11 quality checks."""
    sec9_body = _find_section(content, [r'## 9 ' + '行业对比.*?(?=## 10 |\\Z)'])
    sec10_body = _find_section(content, [r'## 10 ' + '市场分歧.*?(?=## 11 |\\Z)'])
    # §9: check for substantive content (real table rows vs explanation-only)
    sec9_table_rows = re.findall(r'^\|.*\|$', sec9_body, re.M) if sec9_body else []
    sec9_data_rows = [r for r in sec9_table_rows
                      if not re.match(r'^\|[\s:\-]+\|$', r)
                      and "竞争关系" not in r
                      and "可比业务" not in r]
    sec9_clean = _strip_md(sec9_body) if sec9_body else ""
    has_peer_table = len(sec9_data_rows) >= 1
    if not has_peer_table and (not sec9_clean or len(sec9_clean) < 35):
        gate.issues.append(Issue("Section", "E9a", P1,
            "§9行业对比为空——必须输出同行表或'未取得有效同行'说明", "r11b-P1"))
    # §10: check for substantive debate rows
    sec10_table_rows = [r for r in (re.findall(r'^\|.*\|$', sec10_body, re.M) if sec10_body else [])
                        if not re.match(r'^\|[\s:\-]+\|$', r)
                        and "多头观点" not in r]
    if not sec10_table_rows:
        gate.issues.append(Issue("Section", "E10a", P1,
            "§10市场分歧为空——必须输出4-6行多空对照四列表", "r11b-P1"))
    valid_db = [r for r in sec10_table_rows]
    if len(valid_db) < 4:
        gate.issues.append(Issue("Section", "E10b", P1,
            f"§10仅{len(valid_db)}行有效分歧(期望4-6行)", "r11b-P1"))
    elif len(valid_db) > 6:
        gate.issues.append(Issue("Section", "E10d", P1,
            f"§10有效分歧超过6行(当前{len(valid_db)}行)", "r11d-P1"))
    if sec10_body:
        for tbl in _extract_tables(sec10_body):
            lines = [l for l in tbl.splitlines() if l.strip().startswith("|")]
            if not lines:
                continue
            header = [c.strip() for c in lines[0].split("|")[1:-1]]
            if not any("多头" in h for h in header):
                continue
            if len(header) != 4:
                gate.issues.append(Issue("Section", "E10_SCHEMA", P1,
                    "§10市场分歧必须是4列表格: 多头观点|证据|空头观点|验证点", "r11d-P1"))
            data = []
            for line in lines[2:]:
                if re.match(r'^\|\s*:?-+', line):
                    continue
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if len(cells) < 4:
                    continue
                data.append(cells)
                if any(not re.sub(r'\[\d+\]', '', c).strip() for c in cells[:4]):
                    gate.issues.append(Issue("Section", "E10_EMPTY_CELL", P1,
                        "§10市场分歧存在空单元格", "r11d-P1"))
                if not _cell_refs(cells[1]):
                    gate.issues.append(Issue("Section", "E10_EVIDENCE_REF", P1,
                        "§10证据列缺少引用", "r11d-P1"))
                if not _cell_refs(cells[3]):
                    gate.issues.append(Issue("Section", "E10_VALIDATION_REF", P1,
                        "§10验证点缺少引用", "r11d-P1"))
                for cell in cells:
                    refs = _cell_refs(cell)
                    if len(refs) != len(set(refs)):
                        gate.issues.append(Issue("Section", "E10_REF_DUP", P1,
                            "§10单元格存在重复引用", "r11d-P1"))
                        break
                if _topic_jaccard(cells[0], cells[2]) >= 0.75:
                    gate.issues.append(Issue("Section", "E10_NOT_OPPOSING", P1,
                        "§10多头和空头疑似同义改写，未形成真实对立", "r11d-P1"))
            topic_cells = [re.sub(r'\[\d+\]', '', r[0] + r[2]) for r in data]
            for i in range(len(topic_cells)):
                for j in range(i + 1, len(topic_cells)):
                    if _topic_jaccard(topic_cells[i], topic_cells[j]) >= 0.62:
                        gate.issues.append(Issue("Section", "E10_TOPIC_DUP", P1,
                            "§10市场分歧主题高度重复，应去重保留质量最高行", "r11d-P1"))
                        break
                else:
                    continue
                break
    sec11_1 = _find_section(content, [r'### 11\.1 ' + '.*?(?=### 11\\.[23]|\\Z)'])
    if sec11_1:
        tp_rows = re.findall(r'^\|.*\|$', sec11_1, re.M)
        data_rows = [r for r in tp_rows if r.strip("| :-\n") and "目标价口径" not in r and "机构" not in r]
        bases = []; assumptions = []; orgs = []
        banned = ["收入增长与需求兑现","产品迭代与客户转化","业务发展","盈利改善","估值修复","基本面改善","关注后续进展"]
        allowlisted_bases = {"研报披露目标价,正文未披露估值方法", "研报披露目标价，正文未披露估值方法"}
        for row in data_rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) >= 5: bases.append(cells[4])
            if len(cells) >= 6: assumptions.append(cells[5])
            if len(cells) >= 1: orgs.append(cells[0])
        # E11a: 仅对旧模板/非缺失口径相同/ID不匹配触发
        old_template = "结构化目标价字段；方法未明示"
        if bases and all(old_template in b for b in bases):
            gate.issues.append(Issue("Section", "E11a", P1,
                "§11.1目标价口径全部为旧模板'结构化目标价字段；方法未明示'——每家机构应独立提取", "r11b-P1"))
        elif len(bases) > 1 and len(set(bases)) == 1 and not all(b in allowlisted_bases for b in bases):
            gate.issues.append(Issue("Section", "E11a", P1,
                "§11.1目标价口径全部相同且非统一缺失口径——每家机构应独立提取", "r11b-P1"))
        # E11b: banned phrases in key assumptions
        all_text = " ".join(assumptions)
        for phrase in banned:
            if phrase in all_text:
                gate.issues.append(Issue("Section", "E11b", P1,
                    f"§11.1关键假设含禁用泛化短语: '{phrase}'", "r11b-P1"))
                break
        # E11d: cross-institution assumption duplication
        fail_closed_marker = "正文未披露可验证的关键假设"
        norm_assumptions = []
        for a in assumptions:
            na = re.sub(r'\[\d+\]', '', a)
            na = re.sub(r'[\s,，、；;。.]', '', na).lower()
            norm_assumptions.append(na)
        non_fail_closed = [na for i, na in enumerate(norm_assumptions)
                           if fail_closed_marker not in assumptions[i]]
        if len(non_fail_closed) >= 3:
            uniq = list(set(non_fail_closed))
            dup_ratio = 1 - len(uniq) / len(non_fail_closed) if non_fail_closed else 0
            if dup_ratio >= 0.5:
                gate.issues.append(Issue("Section", "E11d", P1,
                    f"§11.1关键假设跨机构重复率过高({dup_ratio:.0%}，{len(non_fail_closed)}家中{len(uniq)}个不同)——应逐机构独立提取", "r11b-P1"))
        concrete_assumptions = []
        for a in assumptions:
            plain = re.sub(r'\[\d+\]', '', a)
            if "正文未披露可验证的关键假设" in plain:
                continue
            parts = [p.strip() for p in re.split(r'[；;、]', plain) if p.strip()]
            if len(parts) >= 2 and any(re.search(r'\d|20\d{2}|客户|订单|出货|ASP|毛利|收入|利润|量产|认证|份额|渗透', p, re.I) for p in parts):
                concrete_assumptions.append(plain)
        if len(data_rows) >= 3 and len(concrete_assumptions) < 3:
            gate.issues.append(Issue("Section", "E11_ASSUMPTION_COVERAGE", P1,
                f"§11.1具备2条以上具体关键假设的机构不足3家(当前{len(concrete_assumptions)}家)", "r11d-P1"))
        method_like = [b for b in bases if re.search(r'PE|P/E|DCF|SOTP|EV/S|P/S|PB|P/B|202\dE|FY202\d|盈利预测', re.sub(r'\[\d+\]', '', b), re.I)]
        if bases and not method_like:
            gate.issues.append(Issue("Section", "E11_METHOD_DISCLOSURE", P2,
                "§11.1未发现明确估值方法或预测年份；允许缺失但需保留证据不足口径", "r11d-P2"))
        tp_values = []
        for row in data_rows:
            m = re.search(r'(\d+(?:\.\d+)?)(?:港元|美元)/', row)
            if m: tp_values.append(float(m.group(1)))
        if len(tp_values) >= 4 and len(tp_values) % 2 == 0:
            sv = sorted(tp_values)
            correct = round((sv[len(sv)//2 - 1] + sv[len(sv)//2]) / 2, 2)
            sec11_2 = _find_section(content, [r'### 11\.2 ' + '.*?(?=### 11\\.[34]|\\Z)'])
            if sec11_2:
                rep = re.search(r'中位数为\s*(\d+(?:\.\d+)?)', sec11_2)
                if rep and abs(float(rep.group(1)) - correct) > 0.1:
                    gate.issues.append(Issue("Section", "E11c", P1,
                        f"§11.2中位数错误: 报告写{float(rep.group(1)):g}，应为{correct:g}", "r11b-P1"))
    sec11_3 = _find_section(content, [r'### 11\.3 ' + '.*?(?=### 11\\.[4]|## 12|\\Z)'])
    if sec11_3:
        required_terms = ["目标价分布", "估值分歧来源", "验证框架"]
        missing_terms = [t for t in required_terms if t not in sec11_3]
        if missing_terms:
            gate.issues.append(Issue("Section", "E11_3_STRUCTURE", P1,
                f"§11.3缺少必要分析段: {missing_terms}", "r11d-P1"))
        if not re.search(r'\[\d+\]', sec11_3):
            gate.issues.append(Issue("Section", "E11_3_REFS", P1,
                "§11.3缺少来自§11.1机构研报的引用", "r11d-P1"))
        if re.search(r'中位附近机构[^。；\n]{0,12}中位数目标价|称为中位数目标价', sec11_3):
            gate.issues.append(Issue("Section", "E11_3_MEDIAN_LABEL", P1,
                "§11.3不得把中位附近机构目标价称为中位数目标价", "r11d-P1"))


# ═══════════════════════════════════════════════════════════════
# GATE 6: Table —— 表格质量
# ═══════════════════════════════════════════════════════════════

def check_table_quality(content: str, market: str) -> GateResult:
    """检查表格格式、稀疏行列、占位符。"""
    gate = GateResult(name="Table Quality")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)
    tables = _extract_tables(content)
    body_tables = _extract_tables(body)

    # T1: 列数上限——普通表≤8列，同业对比≤10列 (NEW)
    for tbl in body_tables:
        hdr = [c.strip() for c in tbl.split('\n')[0].split('|')[1:-1]]
        n_cols = len(hdr)
        is_peer_table = any(kw in ','.join(hdr) for kw in ['竞争关系', '可比业务', '行业地位'])
        limit = 10 if is_peer_table else 8
        if n_cols > limit:
            gate.issues.append(Issue("Table", "T1", P1,
                f"表格列数超限({n_cols}列>{limit}列): '{hdr[0][:20]}'", "check(列限)"))

    # T1b: Markdown table schema must be well formed (header + separator + equal cells)
    for tbl in body_tables:
        lines = [l.rstrip() for l in tbl.strip().split('\n') if l.strip()]
        if not lines:
            continue
        header_cells = [c.strip() for c in lines[0].split('|')[1:-1]]
        header_n = len(header_cells)
        if header_n == 0:
            continue
        if len(lines) < 2 or not re.match(r'^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$', lines[1]):
            gate.issues.append(Issue("Table", "T1b", P1,
                f"表格缺少Markdown分隔行: '{header_cells[0][:20]}'", "AUDIT-P1"))
        for row_no, line in enumerate(lines[1:], 2):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            is_sep = re.match(r'^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$', line)
            if is_sep:
                continue
            if cells == header_cells:
                gate.issues.append(Issue("Table", "T1b", P1,
                    f"表格重复表头: '{header_cells[0][:20]}'第{row_no}行", "AUDIT-P1"))
            if len(cells) != header_n:
                gate.issues.append(Issue("Table", "T1b", P1,
                    f"表格列数不一致: '{header_cells[0][:20]}'第{row_no}行 {len(cells)}列≠表头{header_n}列", "AUDIT-P1"))

    # T2: 公司+代码合并 (check22)
    for tbl in body_tables:
        hdr_cells = [c.strip() for c in tbl.split('\n')[0].split('|')[1:-1]]
        has_company = any('公司' in c and '代码' not in c and '(' not in c for c in hdr_cells)
        has_code = any('代码' in c for c in hdr_cells)
        if has_company and has_code:
            gate.issues.append(Issue("Table", "T2", P1,
                f"公司/代码分两列(应合并为'公司(代码)'): {hdr_cells[:3]}", "check22"))

    # T3: 全空列 + 稀疏数值列 (check6)
    for tbl in body_tables:
        tbl_lines = [l for l in tbl.strip().split('\n') if re.match(r'^\|.+\|$', l)]
        if len(tbl_lines) < 3:
            continue
        header_cells = [c.strip() for c in tbl_lines[0].split('|')[1:-1]]
        # 豁免地区/区域表
        if any(kw in header_cells[0] for kw in ('地区', '区域', 'Region')):
            continue
        n_cols = len(header_cells)
        for ci in range(n_cols):
            col_vals = []
            for line in tbl_lines[2:]:
                cells = line.split('|')[1:-1]
                if ci < len(cells):
                    col_vals.append(cells[ci].strip())
            non_empty = [v for v in col_vals if v and v not in ('—','-','','——','--','─','－','~','~—')]
            col_header = header_cells[ci].lower() if ci < len(header_cells) else ''
            has_numbers = any(re.search(r'\d', v) for v in non_empty)
            is_numeric_col = has_numbers or any(kw in col_header for kw in ('亿','%','x','倍','率','额','价','值','PE','PB','PS','EPS','ROE'))
            if len(non_empty) == 0:
                gate.issues.append(Issue("Table", "T3", P1,
                    f"全空列: 表'{header_cells[0][:15]}'第{ci+1}列('{header_cells[ci][:15] if ci < len(header_cells) else '?'}')", "check6"))
            elif is_numeric_col and len(non_empty) == 1:
                gate.issues.append(Issue("Table", "T3", P1,
                    f"稀疏数值列(仅1值): 表'{header_cells[0][:15]}'第{ci+1}列", "check6"))

    # T4: 稀疏行列(全—占位) (check25)
    for tbl in body_tables:
        tbl_lines = [l for l in tbl.strip().split('\n') if re.match(r'^\|.+\|$', l)]
        if len(tbl_lines) < 3:
            continue
        header = [c.strip() for c in tbl_lines[0].split('|')[1:-1]]
        for ri, line in enumerate(tbl_lines[2:], 2):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if len(cells) < 2:
                continue
            non_first = cells[1:] if len(cells) > 1 else cells
            if all(c in ('—', '--', '——', '', ' ', '-') for c in non_first) and len(non_first) >= 2:
                gate.issues.append(Issue("Table", "T4", P1,
                    f"稀疏行(整行—占位): 表'{header[0][:15] if header else '?'}'第{ri}行", "check25"))

    # T5: 空表检测 (check4)
    for tbl in body_tables:
        tbl_lines = tbl.strip().split('\n')
        if len(tbl_lines) <= 2:
            gate.issues.append(Issue("Table", "T5", P1,
                f"疑似空表(仅{len(tbl_lines)}行)", "check4"))
        data_rows = [l for l in tbl_lines if not re.match(r'^[\|\s\-:]+$', l)]
        if len(data_rows) <= 1:
            gate.issues.append(Issue("Table", "T5", P1,
                "表格无数据行", "check4"))

    # T6: 表格无引用 (check7)
    for tbl in body_tables:
        tbl_lines = tbl.split('\n')
        header_first = tbl_lines[0].split('|')[1].strip()[:30] if '|' in tbl_lines[0] else ''
        # 豁免催化/事件/地区表
        if any(kw in header_first for kw in ('时间', '事件', '催化', 'Catalyst', '地区', '区域', 'Region')):
            continue
        # 豁免情景推演表
        if any(kw in tbl for kw in ('乐观', '中性', '悲观', '情景')):
            continue
        refs_in_table = re.findall(r'\[(\d+)\]', tbl)
        tbl_start = content.find(tbl.split('\n')[0])
        tbl_text_above = ""
        if tbl_start > 0:
            before = content[max(0, tbl_start-300):tbl_start]
            tbl_text_above = '\n'.join(before.split('\n')[-4:])
        src_has_ref = bool(re.search(r'数据来源.*\[(\d+)\]', tbl_text_above))
        if not refs_in_table and not src_has_ref:
            gate.issues.append(Issue("Table", "T6", P1,
                f"表格无引用: '{header_first}...'", "check7"))

    # T7: 情景推演三档概率 (check13 extended)
    scenario = re.search(r'(?:### 9\.4 情景推演|### 11\.[34] 情景推演).*?(?=##|\Z)', content, re.DOTALL)
    if scenario:
        probs = re.findall(r'概率\s*[~≈]?\s*(\d+)\s*%', scenario.group(0))
        if len(probs) >= 3:
            try:
                total = sum(int(p) for p in probs)
                if total != 100:
                    gate.issues.append(Issue("Table", "T7", P2,
                        f"情景推演三档概率之和={total}%(期望100%)", "check13"))
            except ValueError:
                pass

    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 7: Market-Specific —— 港美股差异
# ═══════════════════════════════════════════════════════════════

def check_market_specific(content: str, market: str, md_dir: str = ".") -> GateResult:
    """检查港美股特有约束。"""
    gate = GateResult(name="Market-Specific")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)

    if market not in ("HK", "US"):
        gate.issues.append(Issue("Market", "M0", Severity("INFO") if hasattr(Severity, "INFO") else P2,
            "A股报告，跳过港美股专属检查"))
        _update_gate(gate)
        return gate

    # M1: 港股调研大纲检查
    if market == "HK":
        has_survey = bool(re.search(r'调研大纲|调研问题', content))
        survey_section = _find_section(content, [r'## 8 .*?(?=## 9 |\Z)'])
        if survey_section:
            topics = re.findall(r'议题\s*\d+', survey_section)
            if len(topics) < 3 and has_survey:
                gate.issues.append(Issue("Market", "M1", P1,
                    f"港股调研大纲议题不足(仅{len(topics)}个，需3-4个)", "check(调研)"))

    # M2: 美股市场关注检查
    if market == "US":
        has_survey = bool(re.search(r'调研大纲|调研问题|议题\s*\d+', content))
        if has_survey:
            gate.issues.append(Issue("Market", "M2", P1,
                "美股报告出现了调研大纲——美股只应写'市场关注'", "check(美股调研)"))
        has_market_focus = bool(re.search(r'市场关注|market focus', content, re.I))
        if not has_market_focus:
            gate.issues.append(Issue("Market", "M2", P2,
                "美股报告未找到'市场关注'章节", "check(美股市场关注)"))

    # M3: 港美股最小结构 (check33)
    h2_map = {int(n): t.strip() for n, t in re.findall(r'^## (\d+) (.+)$', content, re.M)}
    missing = []
    for sec_num, sec_name in HK_US_REQUIRED_H2.items():
        if sec_num not in h2_map:
            missing.append(f"§{sec_num} {sec_name}")
    if missing:
        gate.issues.append(Issue("Market", "M3", P1,
            f"港美股结构过薄——缺失关键章节: {', '.join(missing)}", "check33"))

    h2_count = len(set(h2_map.keys()))
    if h2_count < 9:
        gate.issues.append(Issue("Market", "M3", P1,
            f"港美股章节过少(仅{h2_count}个H2)——疑似缩略版", "check33"))

    # M4: 禁用接口检查——报告不应出现停用接口名
    deprecated_apis = ['research_sec_foredata', 'stock_evaluationAnalysis',
                       'diagnosis_valuation_rank', 'Stock_OnePage',
                       'market_snapshot', 'market_HK']
    for api in deprecated_apis:
        if api in body:
            gate.issues.append(Issue("Market", "M4", P1,
                f"报告中出现停用接口: {api}", "check(禁用接口)"))

    # M5: 美股 GAAP/non-GAAP 口径标注
    if market == "US":
        has_financial = bool(re.search(r'(?:Non-GAAP|non-GAAP|GAAP)', body))
        if not has_financial:
            gate.issues.append(Issue("Market", "M5", P2,
                "美股财务章节未明确标注GAAP/non-GAAP口径", "check(GAAP)"))

    # M6: 美股/ADR FY/CY 标注
    if market == "US":
        fy_refs = re.findall(r'FY\d{4}', body)
        cy_refs = re.findall(r'CY\d{4}', body)
        if fy_refs and not cy_refs:
            # 有FY无CY标注 → 检查是否只写了FY没解释财年截止月
            if not re.search(r'财年.*截至|FY\d{4}[（(]截至', body):
                gate.issues.append(Issue("Market", "M6", P2,
                    "美股使用FY但未标注财年截止月份(FY≠CY)", "check(FY/CY)"))

    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# GATE 8: Hygiene —— 输出清洁度
# ═══════════════════════════════════════════════════════════════

def check_hygiene(content: str, market: str, md_path: str = "") -> GateResult:
    """检查输出清洁度：无内部话术、无占位符、无临时文件。"""
    gate = GateResult(name="Hygiene")
    body, ref_part, ref_start = _extract_body_and_refs(content, market)
    body_no_footer = '\n'.join(body.split('\n')[:-5]) if body else body

    # H1: 工程/评测/Meta话术 (check1)
    forbidden_terms = {
        "skipped_with_reason": (P0, "评测话术"),
        "接口不可用": (P0, "接口说明泄露"),
        "本市场不设": (P0, "模板元说明泄露"),
        "美股不设": (P0, "模板元说明泄露"),
        "pipeline": (P1, "工程术语泄露"),
        "自检": (P0, "工程术语泄露"),
        "评测记录": (P0, "工程术语泄露"),
        "HK PIT": (P0, "接口名泄露" if "HK PIT" not in (ref_part or "") else (P2, "正文接口名")),
        "同业数据优先从": (P0, "prompt规则泄露"),
        "如无法获取": (P0, "prompt规则泄露"),
        "需补充": (P0, "prompt规则泄露"),
        "检查项": (P0, "质量检查话术泄露"),
        "本报告应": (P0, "指令性话术泄露"),
        "数据优先": (P0, "prompt规则泄露"),
        "LLM环节": (P0, "工程话术泄露"),
        "兜底模板": (P0, "工程话术泄露"),
        "生成失败": (P0, "生成失败占位符"),
        "LLM不可用": (P0, "生成失败占位符"),
        "degraded": (P0, "降级状态泄露"),
        "fallback failed": (P0, "降级状态泄露"),
        "请在.*后移除": (P0, "工程指令泄露"),
    }
    for term, (level, desc) in forbidden_terms.items():
        if term in body_no_footer:
            # 豁免: HK PIT 在参考资料中可保留
            if term == "HK PIT" and ref_part and term in ref_part and term not in body_no_footer:
                continue
            gate.issues.append(Issue("Hygiene", "H1", level,
                f"正文含{desc}: '{term}'", "check1"))

    # H2: 内部质检话术 (check28)
    forbidden_qa = ['schema', '标准schema', '9列', '10列', '本轮删除', '其余列完整',
                    'checker', 'quality gate', 'quality_check', 'id_audit',
                    'fresh_generation', 'artifact', 'raw_payload']
    for term in forbidden_qa:
        if term in body:
            gate.issues.append(Issue("Hygiene", "H2", P1,
                f"正文含内部质检话术: '{term}'", "check28"))
    for level_label in ['P0', 'P1', 'P2']:
        if re.search(rf'\b{level_label}\b.*[=：:]|{level_label}\s*[=＞]', body):
            gate.issues.append(Issue("Hygiene", "H2", P1,
                f"正文含质检级别标识 '{level_label}'", "check28"))

    # H3: 页尾 pipeline 信息 (check32)
    lines = content.split('\n')
    tail = '\n'.join(lines[-15:])
    pipeline_patterns = [
        r'v\d+\.\d+\.\d+\s+(?:HK-US|A-share|pipeline)',
        r'HK-US pipeline',
        r'pipeline\s*[·•]\s*\d{4}-\d{2}-\d{2}',
    ]
    for pat in pipeline_patterns:
        if re.search(pat, tail):
            gate.issues.append(Issue("Hygiene", "H3", P1,
                f"页尾含内部pipeline信息: 匹配'{pat}'", "check32"))
            break
    if re.search(r'^>.*数据来源[：:]\s*Datayes\s+getMaterialsV2', content, re.M):
        gate.issues.append(Issue("Hygiene", "H3", P1,
            "页尾含内部话术: '数据来源: Datayes getMaterialsV2'", "check32"))

    # H4: 模板装饰符 (check2)
    for pattern in ['⭐', '🌟🌟', '⚠️']:
        if pattern in content:
            gate.issues.append(Issue("Hygiene", "H4", P0,
                f"模板装饰符未清理: '{pattern}'", "check2"))

    # H5: [生成失败] (check9)
    if '[生成失败]' in content:
        gate.issues.append(Issue("Hygiene", "H5", P0,
            "报告含'[生成失败]'", "check9"))

    # H6: HTML标签 (check15)
    if re.search(r'<br\s*/?>', content):
        gate.issues.append(Issue("Hygiene", "H6", P0,
            "含裸<br>标签——Markdown不应使用HTML换行", "check15"))
    if re.search(r'<(p|div|span|table|tr|td)\b', content):
        gate.issues.append(Issue("Hygiene", "H6", P0,
            "含HTML标签——Markdown不应混用HTML", "check15"))

    # H7: 树形调试符号 (check19)
    for sym, desc in [('└', 'tree-l'), ('├', 'tree-t'), ('│', 'tree-pipe')]:
        if sym in content:
            gate.issues.append(Issue("Hygiene", "H7", P0,
                f"含树形调试符号: '{sym}'", "check19"))

    # H8: 孤立 * 符号 (check16)
    for i, line in enumerate(content.split('\n')):
        if line.strip().startswith('|') or line.strip().startswith('* ') or line.strip().startswith('- '):
            continue
        single_stars = re.findall(r'(?<!\*)\*(?!\*)', line)
        if len(single_stars) % 2 == 1:
            gate.issues.append(Issue("Hygiene", "H8", P1,
                f"孤立*(未闭合Markdown强调): {line.strip()[:60]}", "check16"))
    if re.search(r'(^|[\s>|-])\*\*\s*[：:]|\*\*\s*\*\*', content, re.M):
        gate.issues.append(Issue("Hygiene", "H8b", P1,
            "正文含空加粗标签或空bold残留", "AUDIT-P1"))

    # H10: 空bullet标签 "• ：" / "•:" (NEW v1.2.4-R2)
    # 检测 bullet 后直接跟冒号且无有效加粗文本的模式
    bullet_colon = re.findall(r'^[  \t]*[•·●►-]\s*(?::|：)\s', content, re.M)
    if bullet_colon:
        gate.issues.append(Issue("Hygiene", "H10", P1,
            f"正文含空bullet标签'•：'(共{len(bullet_colon)}处)——缺少加粗小标题", "AUDIT-P1"))

    # H9: 大量空占位符 (check35)
    placeholder_counts = {}
    for pat, label in [(r'未披露', '未披露'), (r'未提供', '未提供'),
                        (r'无数据', '无数据'), (r'N/A', 'N/A')]:
        matches = re.findall(pat, body, re.I)
        if len(matches) >= 3:
            placeholder_counts[label] = len(matches)
    if placeholder_counts:
        detail = ', '.join(f"'{k}'×{v}" for k, v in placeholder_counts.items())
        gate.issues.append(Issue("Hygiene", "H9", P1,
            f"大量空占位符({detail})——应按稀疏处理规则删除或改写", "check35"))

    _update_gate(gate)
    return gate


def check_target_price_binding(content: str, market: str, md_dir: str = ".") -> GateResult:
    """Verify §11.1 org-target-ref rows bind to the same source record."""
    gate = GateResult(name="Target Binding")
    if market not in ("HK", "US"):
        _update_gate(gate)
        return gate

    sec11_1 = _find_section(content, [r'### 11\.1 机构目标价汇总.*?(?=### 11\.2|### 11\.3|## 12|\Z)'])
    if not sec11_1:
        gate.issues.append(Issue("Target", "TP0", P1,
            "缺少§11.1机构目标价汇总，无法逐机构绑定目标价来源", "AUDIT-P1"))
        _update_gate(gate)
        return gate

    materials_path = os.path.join(md_dir, "raw_retrieval_payloads", "materials.json")
    if not os.path.exists(materials_path):
        alt_path = os.path.join(md_dir, "materials.json")
        materials_path = alt_path if os.path.exists(alt_path) else materials_path
    if not os.path.exists(materials_path):
        gate.issues.append(Issue("Target", "TP0", P1,
            "缺少materials.json，无法核验目标价字段绑定", "AUDIT-P1"))
        _update_gate(gate)
        return gate

    try:
        with open(materials_path, "r", encoding="utf-8") as f:
            materials = json.load(f)
    except Exception as exc:
        gate.issues.append(Issue("Target", "TP0", P1,
            f"materials.json读取失败: {exc}", "AUDIT-P1"))
        _update_gate(gate)
        return gate

    details = {str(rd.get("articleId", "")): rd for rd in materials.get("research", {}).get("details", [])}
    _, ref_part, _ = _extract_body_and_refs(content, market)
    refs = {}
    for m in re.finditer(r'^\[(\d+)\].*?ID[:：]\s*([^|]+)\|\s*([^|]+)\|', ref_part, re.M):
        refs[int(m.group(1))] = {"id": m.group(2).strip(), "org": m.group(3).strip(), "line": m.group(0)}

    def _num(s: str) -> Optional[float]:
        m = re.search(r'(\d+(?:,\d{3})*(?:\.\d+)?)', s)
        if not m:
            return None
        return float(m.group(1).replace(",", ""))

    def _norm_org(s: str) -> str:
        s = re.sub(r'[\s证券股份有限公司控股集团研究所资本国际]', '', s or '')
        return re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]', '', s).lower()

    first_line = content.splitlines()[0] if content else ""
    ticker_m = re.search(r'[（(]([A-Z]{1,6}|\d{3,5}(?:\.HK)?)[）)]', first_line)
    target_ticker = ticker_m.group(1).upper().replace(".HK", "") if ticker_m else ""
    target_name = re.sub(r'^#\s*', '', first_line).split("（")[0].split("(")[0].strip()

    def _target_aliases() -> tuple[set[str], set[str]]:
        names = {target_name, target_name.lower(), target_name.upper()} if target_name else set()
        tickers = {target_ticker}
        if target_ticker.isdigit():
            tickers.add(target_ticker.lstrip("0") or target_ticker)
        if target_ticker == "META" or "Meta" in target_name:
            names.update({"Meta", "META", "Meta Platforms", "METAPLATFORMS", "元平台", "元"})
            tickers.add("META")
        if target_ticker in {"01024", "1024"} or "快手" in target_name:
            names.update({"快手", "快手-W", "Kuaishou", "KUAISHOU"})
            tickers.update({"01024", "1024"})
        if target_ticker == "MSFT" or "Microsoft" in target_name:
            names.update({"Microsoft", "MICROSOFT", "微软"})
            tickers.add("MSFT")
        return names, tickers

    def _source_matches_target(rd: dict) -> bool:
        names, tickers = _target_aliases()
        fields = [rd.get("articleTitle", ""), rd.get("articleTitleEN", ""), rd.get("title", ""),
                  rd.get("companyName", ""), rd.get("stockId", ""), rd.get("secCode", "")]
        text = " ".join(str(x or "") for x in fields)
        text_upper = text.upper()
        explicit = set(re.findall(r'\(([A-Z]{1,6}|\d{3,5})(?:\.[A-Z]{1,4})?\)', text_upper))
        allowed = {t.upper().replace(".HK", "") for t in tickers if t}
        allowed.update(t.lstrip("0") for t in list(allowed) if t.isdigit())
        if explicit and not any(x in allowed for x in explicit):
            return False
        return any(str(a).lower() in text.lower() for a in names if a) or any(str(t).upper() in text_upper for t in tickers if t)

    header_line = next((line for line in sec11_1.splitlines() if line.strip().startswith("|") and "机构" in line and "目标价" in line), "")
    header_cells = [c.strip() for c in header_line.split("|")[1:-1]] if header_line else []
    org_idx = next((i for i, h in enumerate(header_cells) if h == "机构" or "机构" in h), 0)
    target_idx = next((i for i, h in enumerate(header_cells) if h == "目标价"), 1)
    if target_idx == 1 and len(header_cells) > 3 and header_cells[1] == "日期":
        target_idx = next((i for i, h in enumerate(header_cells) if h == "目标价"), 3)

    rows_checked = 0
    for line in sec11_1.splitlines():
        if not line.strip().startswith("|") or re.match(r'^\|\s*:?-+', line):
            continue
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if len(cells) <= max(org_idx, target_idx) or "机构" in cells[org_idx]:
            continue
        org, target_cell = cells[org_idx], cells[target_idx]
        target_val = _num(target_cell)
        ref_nums = [int(x) for x in re.findall(r'\[(\d+)\]', target_cell)] or [int(x) for x in re.findall(r'\[(\d+)\]', line)]
        if target_val is None or not ref_nums:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1目标价行缺少数值或引用: {line[:80]}", "AUDIT-P0"))
            continue
        rows_checked += 1
        rn = ref_nums[0]
        ref = refs.get(rn)
        if not ref:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1引用[{rn}]未在参考资料中找到", "AUDIT-P0"))
            continue
        rd = details.get(str(ref.get("id", "")))
        if not rd:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1引用[{rn}]的ID {ref.get('id')} 不在materials research.details中", "AUDIT-P0"))
            continue
        raw_tp = rd.get("targetPrice", "")
        src_tp = _num(str(raw_tp))
        if src_tp is None:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1引用[{rn}]源记录无targetPrice字段，不能支撑目标价", "AUDIT-P0"))
        elif abs(src_tp - target_val) > 0.01:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1目标价错配: {org}写{target_val:g}，引用[{rn}]源targetPrice={src_tp:g}", "AUDIT-P0"))
        src_org = rd.get("orgName", "") or ref.get("org", "")
        no, ns = _norm_org(org), _norm_org(src_org)
        if no and ns and no not in ns and ns not in no:
            gate.issues.append(Issue("Target", "TP1", P0,
                f"§11.1机构错配: 行内机构'{org}'，引用[{rn}]源机构'{src_org}'", "AUDIT-P0"))
        if not _source_matches_target(rd):
            src_title = rd.get("articleTitle", "") or rd.get("title", "")
            gate.issues.append(Issue("Target", "TP3", P1,
                f"§11.1目标价来源非当前标的: [{rn}] {src_title[:80]}", "AUDIT-P1"))

    if rows_checked < 3:
        gate.issues.append(Issue("Target", "TP2", P1,
            f"§11.1可核验机构目标价不足3行(当前{rows_checked}行)", "AUDIT-P1"))

    _update_gate(gate)
    return gate


def check_generation_status_file(md_dir: str = ".") -> GateResult:
    """Block release when generation_status records degraded or failed sections."""
    gate = GateResult(name="Generation Status")
    path = os.path.join(md_dir, "generation_status.json")
    if not os.path.exists(path):
        _update_gate(gate)
        return gate
    try:
        with open(path, "r", encoding="utf-8") as f:
            gs = json.load(f)
    except Exception as exc:
        gate.issues.append(Issue("Generation", "G0", P0,
            f"generation_status.json读取失败: {exc}", "AUDIT-P0"))
        _update_gate(gate)
        return gate
    failed = gs.get("failed_sections") or []
    assembly = gs.get("assembly_issues") or []
    if gs.get("is_degraded") or gs.get("is_skeleton") or failed or assembly:
        gate.issues.append(Issue("Generation", "G0", P0,
            f"生成状态阻断: mode={gs.get('mode')}, failed_sections={failed}, assembly_issues={assembly}", "AUDIT-P0"))
    _update_gate(gate)
    return gate


# ═══════════════════════════════════════════════════════════════
# 汇总与输出
# ═══════════════════════════════════════════════════════════════

def _update_gate(gate: GateResult):
    """更新 gate 的计数器。"""
    gate.p0 = sum(1 for i in gate.issues if i.severity == P0)
    gate.p1 = sum(1 for i in gate.issues if i.severity == P1)
    gate.p2 = sum(1 for i in gate.issues if i.severity == P2)
    if gate.p0 > 0:
        gate.status = GateStatus.FAIL
    elif gate.p1 > 0 or gate.p2 > 0:
        gate.status = GateStatus.WARN
    else:
        gate.status = GateStatus.PASS


def _load_source_trace_path(source_trace_path: str = "") -> dict:
    if not source_trace_path or not os.path.exists(source_trace_path):
        return {}
    try:
        with open(source_trace_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _reference_source_map(content: str, source_trace: dict, market: str) -> dict[int, dict]:
    _, ref_part, _ = _extract_body_and_refs(content, market)
    by_id = {str(s.get("id", "")): s for s in source_trace.get("sources", []) if s.get("id")}
    out = {}
    for m in re.finditer(r'^\[(\d+)\].*?ID[:：]\s*([^|]+)', ref_part, re.M):
        rn = int(m.group(1))
        sid = m.group(2).strip()
        out[rn] = by_id.get(sid, {"id": sid, "line": m.group(0)})
    return out

def _extract_peer_table(content: str) -> tuple[list[str], list[list[str]], str]:
    sec9 = _find_section(content, [r'## 9 行业对比与 A/H 映射.*?(?=## 10 |\Z)', r'## 9 行业对比.*?(?=## 10 |\Z)']) or ""
    sec91 = _find_section(sec9, [r'### 9\.1 .*?(?=### 9\.2|## 10 |\Z)']) or sec9
    tables = _extract_tables(sec91)
    for tbl in tables:
        lines = [l for l in tbl.strip().splitlines() if l.strip().startswith("|")]
        if not lines:
            continue
        header = [c.strip() for c in lines[0].split("|")[1:-1]]
        if "竞争关系" in header and "公司" in header and "最新业务进展" in header:
            rows = []
            for line in lines[2:]:
                if re.match(r'^\|\s*:?-+', line):
                    continue
                rows.append([c.strip() for c in line.split("|")[1:-1]])
            return header, rows, sec9
    return [], [], sec9

def check_peer_comparison_source_trace(content: str, market: str, source_trace_path: str = "") -> GateResult:
    gate = GateResult(name="Peer Source")
    if market not in ("HK", "US"):
        _update_gate(gate)
        return gate
    header, rows, sec9 = _extract_peer_table(content)
    if not sec9 or not header:
        gate.issues.append(Issue("Peer", "P9_1", P1, "§9.1同业业务对比表缺失", "AUDIT-P1"))
        _update_gate(gate)
        return gate
    required = ["竞争关系", "公司", "可比业务", "行业地位", "可比维度", "商业模式", "目标客户群体", "核心产品", "最新业务进展", "进展日期"]
    missing = [c for c in required if c not in header]
    if missing:
        gate.issues.append(Issue("Peer", "P9_2", P1, f"§9.1同业表缺列: {missing}", "AUDIT-P1"))
    if len(header) == 6:
        gate.issues.append(Issue("Peer", "P9_3", P1, "不得生成另一套六列peer表", "AUDIT-P1"))
    if not rows:
        gate.issues.append(Issue("Peer", "P9_4", P1, "§9.1无数据行", "AUDIT-P1"))
        _update_gate(gate)
        return gate
    target_name, target_ticker = _infer_target(content)
    idx = {c: header.index(c) for c in header if c in required}
    first = rows[0]
    rel0 = first[idx["竞争关系"]] if len(first) > idx["竞争关系"] else ""
    comp0 = first[idx["公司"]] if len(first) > idx["公司"] else ""
    if rel0 not in ("基准公司", "目标公司（基准）"):
        gate.issues.append(Issue("Peer", "P9_5", P1, "§9.1第一行竞争关系必须为基准公司", "AUDIT-P1"))
    if (target_name and target_name not in comp0) or (target_ticker and target_ticker.upper().replace(".HK", "") not in comp0.upper().replace(".HK", "")):
        gate.issues.append(Issue("Peer", "P9_6", P1, "§9.1第一行不是报告目标公司", "AUDIT-P1"))
    target_rows = [r for r in rows if len(r) > idx["公司"] and ((target_name and target_name in r[idx["公司"]]) or (target_ticker and target_ticker.upper().replace(".HK", "") in r[idx["公司"]].upper().replace(".HK", "")))]
    if len(target_rows) > 1:
        gate.issues.append(Issue("Peer", "P9_7", P1, "目标公司在§9.1出现超过一次", "AUDIT-P1"))
    peer_rows = rows[1:]
    if len(peer_rows) < 2:
        gate.issues.append(Issue("Peer", "P9_8", P1, f"非自身有效peer少于2家(当前{len(peer_rows)})", "AUDIT-P1"))
    elif len(peer_rows) == 2:
        gate.issues.append(Issue("Peer", "P9_9", P2, "非自身有效peer为2家，样本偏少", "AUDIT-P2"))
    seen = set()
    ref_sources = _reference_source_map(content, _load_source_trace_path(source_trace_path), market)
    for row in peer_rows:
        if len(row) <= max(idx.values()):
            continue
        company_cell = row[idx["公司"]]
        row_text = " ".join(row)
        if re.search(r'未上市|华为', row_text) or (re.search(r'客户|供应商|合作方|投资方', row_text) and not re.search(r'竞争|同业|可比|对标', row_text)):
            gate.issues.append(Issue("Peer", "P9_18", P1,
                f"§9疑似将客户、供应商、合作方或未上市主体作为同行: {company_cell}", "AUDIT-P1"))
        if target_name and target_name in company_cell:
            gate.issues.append(Issue("Peer", "P9_10", P1, f"peer行再次出现目标公司: {company_cell}", "AUDIT-P1"))
        key = re.sub(r'\[\d+\]|[（）()].*', '', company_cell).strip()
        if key in seen:
            gate.issues.append(Issue("Peer", "P9_11", P1, f"同一peer重复出现: {company_cell}", "AUDIT-P1"))
        seen.add(key)
        refs_by_col = {col: [int(x) for x in re.findall(r'\[(\d+)\]', row[idx[col]])] for col in required if col in idx and len(row) > idx[col]}
        for col in ("可比业务",):
            if not refs_by_col.get(col):
                gate.issues.append(Issue("Peer", "P9_12", P1, f"{company_cell}{col}无引用", "AUDIT-P1"))
            for rn in refs_by_col.get(col, []):
                if ref_sources and ref_sources.get(rn, {}).get("source_role") not in ("target_research", "peer_discovery"):
                    gate.issues.append(Issue("Peer", "P9_13", P1, f"{company_cell}{col}引用[{rn}]不是target_research或peer_discovery", "AUDIT-P1"))
        for col in ("行业地位", "商业模式", "目标客户群体", "核心产品"):
            refs = refs_by_col.get(col, [])
            if not refs:
                gate.issues.append(Issue("Peer", "P9_14", P1, f"{company_cell}{col}无peer-specific引用", "AUDIT-P1"))
            for rn in refs:
                src = ref_sources.get(rn, {})
                if ref_sources and not (src.get("company_match") == "exact_peer" and src.get("source_role") == "peer_business_progress"):
                    gate.issues.append(Issue("Peer", "P9_15", P1, f"{company_cell}{col}引用[{rn}]不是peer-specific来源", "AUDIT-P1"))
        for rn in refs_by_col.get("最新业务进展", []):
            src = ref_sources.get(rn, {})
            ticker_match = str(src.get("peer_ticker", "")).upper().replace(".HK", "") in company_cell.upper().replace(".HK", "")
            if ref_sources and not (src.get("company_match") == "exact_peer" and src.get("source_role") == "peer_business_progress" and ticker_match):
                gate.issues.append(Issue("Peer", "P9_16", P1, f"{company_cell}最新业务进展引用[{rn}]角色错误", "AUDIT-P1"))
            date_cell = row[idx["进展日期"]]
            if src.get("publishTime") and date_cell and date_cell > str(src.get("publishTime"))[:10]:
                gate.issues.append(Issue("Peer", "P9_17", P1, f"{company_cell}进展日期晚于来源日期", "AUDIT-P1"))
    _update_gate(gate)
    return gate

def run_all_checks(md_path: str, market: str = "A", md_dir: str = None, source_trace_path: str = "") -> List[GateResult]:
    """运行全部 8 个 Gate 检查。"""
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if md_dir is None:
        md_dir = os.path.dirname(os.path.abspath(md_path)) or "."

    results = [
        check_delivery(md_path, market),
        check_structure(content, market),
        check_citation(content, market),
        check_data_integrity(content, market),
        _check_placeholder_financials(content, market),
        check_source_company_mismatch(content, market, md_dir),
        check_section_quality(content, market),
        check_peer_comparison_source_trace(content, market, source_trace_path),
        check_table_quality(content, market),
        check_market_specific(content, market, md_dir),
        check_target_price_binding(content, market, md_dir),
        check_hygiene(content, market, md_path),
        check_generation_status_file(md_dir),
    ]
    return results


def format_output(results: List[GateResult], md_path: str, market: str) -> str:
    """格式化输出为 Gate 表格 + 详情。"""
    total_p0 = sum(r.p0 for r in results)
    total_p1 = sum(r.p1 for r in results)
    total_p2 = sum(r.p2 for r in results)

    if total_p0 > 0:
        overall = "✗ FAIL"
    elif total_p1 > 0 or total_p2 > 0:
        overall = "⚠ WARN"
    else:
        overall = "✓ PASS"

    lines = []
    lines.append(f"\n{'='*72}")
    lines.append(f"  v1.2.4 质量门禁报告: {os.path.basename(md_path)}")
    lines.append(f"  市场: {market}  |  P0={total_p0}  P1={total_p1}  P2={total_p2}")
    lines.append(f"{'='*72}\n")

    # Gate summary table
    lines.append(f"{'Gate':<22} | {'Status':<8} | {'P0':>3} | {'P1':>3} | {'P2':>3} | Issues")
    lines.append(f"{'-'*22}-+-{'-'*8}-+-{'-'*3}-+-{'-'*3}-+-{'-'*3}-+-{'-'*50}")
    for r in results:
        issues_summary = ""
        if r.issues:
            # Summarize by check_id prefixes
            id_parts = []
            for i in r.issues:
                parts = i.check_id.split('.')
                if len(parts) >= 2:
                    short = parts[0] + '.' + parts[1].split('/')[0]
                else:
                    short = parts[0]
                id_parts.append(short)
            check_ids = sorted(set(id_parts))
            issues_summary = ', '.join(check_ids[:3])
            if len(check_ids) > 3:
                issues_summary += f" +{len(check_ids)-3}"
        else:
            issues_summary = "—"
        lines.append(f"{r.name:<22} | {r.status.value:<8} | {r.p0:>3} | {r.p1:>3} | {r.p2:>3} | {issues_summary}")

    lines.append(f"\n{'-'*72}")
    lines.append(f"  TOTAL: {overall}  |  P0={total_p0}  P1={total_p1}  P2={total_p2}")
    lines.append(f"{'-'*72}\n")

    # Detail section
    if total_p0 + total_p1 + total_p2 > 0:
        lines.append("Details:")
        for r in results:
            for issue in r.issues:
                sev = issue.severity.value
                ref = f"/{issue.old_check_ref}" if issue.old_check_ref else ""
                lines.append(f"  [{sev}] [{issue.check_id}{ref}] {issue.message}")
    else:
        lines.append("  No issues found — all gates passed.")

    return '\n'.join(lines)


def to_json(results: List[GateResult], md_path: str, market: str) -> dict:
    """转换为 JSON 格式。"""
    total_p0 = sum(r.p0 for r in results)
    total_p1 = sum(r.p1 for r in results)
    total_p2 = sum(r.p2 for r in results)

    gates_json = []
    for r in results:
        gates_json.append({
            "gate": r.name,
            "status": r.status.value,
            "P0": r.p0,
            "P1": r.p1,
            "P2": r.p2,
            "issues": [
                {"check_id": i.check_id, "severity": i.severity.value,
                 "message": i.message, "old_check_ref": i.old_check_ref}
                for i in r.issues
            ]
        })

    return {
        "report": os.path.basename(md_path),
        "version": "v1.2.4",
        "market": market,
        "overall": "FAIL" if total_p0 > 0 else ("WARN" if total_p1 + total_p2 > 0 else "PASS"),
        "P0": total_p0, "P1": total_p1, "P2": total_p2,
        "gates": gates_json
    }


# ═══════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v1.2.4 报告质量门禁（分层 Gate 架构）")
    parser.add_argument("markdown", help="Markdown 报告路径")
    parser.add_argument("--market", default="A", choices=["A", "HK", "US"])
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--output-dir", default=None, help="产物目录(用于检测 source_trace 等)")
    parser.add_argument("--source-trace", default=None, help="source_trace.json 路径(用于peer引用角色检查)")
    parser.add_argument("--summary-only", action="store_true", help="仅输出汇总表，不输出详情")
    args = parser.parse_args()

    md_dir = args.output_dir or os.path.dirname(os.path.abspath(args.markdown)) or "."
    source_trace_path = args.source_trace or os.path.join(md_dir, "source_trace.json")
    results = run_all_checks(args.markdown, args.market, md_dir, source_trace_path)

    total_p0 = sum(r.p0 for r in results)
    total_p1 = sum(r.p1 for r in results)
    total_p2 = sum(r.p2 for r in results)

    if args.json:
        print(json.dumps(to_json(results, args.markdown, args.market), ensure_ascii=False, indent=2))
    else:
        print(format_output(results, args.markdown, args.market))

    sys.exit(1 if total_p0 > 0 else (2 if total_p1 > 0 else 0))
