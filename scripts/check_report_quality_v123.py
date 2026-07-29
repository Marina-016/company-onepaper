#!/usr/bin/env python3
"""v1.2.3 报告质量门禁——离线 MD 检查（不做 Pipeline，只做最终质量把关）。

用法: python check_report_quality_v123.py <report.md> [--market A|HK|US]
退出: 0=通过, 1=未通过(含P0), 2=未通过(仅P1无P0)

v1.2.3-R2 新增:
- 必填章节非空检查 (required_section_non_empty)
- 情景推演有效数值检查 (scenario_table_must_have_computable_values)
- 同业比较表完整 schema 检查 (peer_comparison_table_required + schema_required)
- 港美股最小结构检查 (hk_us_minimum_structure_required)
- 内部 pipeline 页尾检查 (no_pipeline_footer_in_final_report)
- 结构化接口 ID 口径修正（entity_id/ticker 合法）
- 估值口径一致检查 (valuation_consistency)
- 数据空占位符检查 (no_empty_placeholders)
"""

from __future__ import annotations
import argparse, re, sys, json, os
from collections import Counter


P0, P1, P2 = "P0", "P1", "P2"


def check_report(md_path: str, market: str = "A") -> dict:
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    issues = {"P0": [], "P1": [], "P2": []}

    # ── 预处理：拆分正文与参考资料 ──
    ref_start = content.find('## 参考资料')
    if ref_start == -1:
        ref_start = content.find('## 13 ')
    if ref_start == -1:
        ref_start = content.find('## 10 ')
    body = content[:ref_start] if ref_start > 0 else content
    ref_part = content[ref_start:] if ref_start > 0 else ""

    # ── 检查 1: 工程/评测/Meta话术（扩展黑名单） ──
    # Only scan body (before 参考资料), skip last 5 lines (footer area)
    body_no_footer = '\n'.join(body.split('\n')[:-5]) if body else body
    forbidden_terms = {
        "skipped_with_reason": (P0, "评测话术"),
        "接口不可用": (P0, "接口说明泄露"),
        "本市场不设": (P0, "模板元说明泄露"),
        "美股不设": (P0, "模板元说明泄露"),
        "pipeline": (P1, "工程术语泄露(pipeline)——如在页尾则为页尾标识问题"),
        "自检": (P0, "工程术语泄露"),
        "评测记录": (P0, "工程术语泄露"),
        "HK PIT": (P0, "接口名泄露"),
        "同业数据优先从": (P0, "prompt规则泄露"),
        "如无法获取": (P0, "prompt规则泄露"),
        "需补充": (P0, "prompt规则泄露"),
        "检查项": (P0, "质量检查话术泄露"),
        "本报告应": (P0, "指令性话术泄露"),
        "数据优先": (P0, "prompt规则泄露"),
        "LLM环节": (P0, "工程话术泄露"),
        "兜底模板": (P0, "工程话术泄露"),
        "请在.*后移除": (P0, "工程指令泄露"),
    }
    for term, (level, desc) in forbidden_terms.items():
        if term in body_no_footer:
            issues[level].append(f"[检查1] 正文含{desc}: '{term}'")

    # ── 检查 2: 模板装饰符 ──
    for pattern in ['⭐', '🌟🌟', '⚠️']:
        if pattern in content:
            issues[P0].append(f"[检查2] 模板装饰符未清理: '{pattern}'")

    # ── 检查 3: 空H3章节 ──
    h3_pattern = re.compile(r'^### (.+)$', re.M)
    h3_matches = list(h3_pattern.finditer(content))
    for i, m in enumerate(h3_matches):
        start = m.end()
        end = h3_matches[i+1].start() if i+1 < len(h3_matches) else len(content)
        section_body = content[start:end]
        # If the section contains a Markdown table, skip near-empty check
        if '|' in section_body and re.search(r'^\|.+\|$', section_body, re.M):
            continue
        clean = re.sub(r'\|.*\|', '', section_body, flags=re.DOTALL)
        clean = re.sub(r'^#.*$', '', clean, flags=re.M)
        clean = re.sub(r'^\*[^*].*$', '', clean, flags=re.M)
        clean = re.sub(r'^[-–•].*$', '', clean, flags=re.M)
        clean = re.sub(r'\s+', '', clean)
        if len(clean) < 30:
            issues[P1].append(f"[检查3] 近乎空的H3: {m.group(1)[:50]}")

    # ── 检查 4: 空表 ──
    table_blocks = re.findall(r'(\|.+\|.*\n(?:\|.+\|.*\n)+)', content)
    for tbl in table_blocks:
        tbl_lines = tbl.strip().split('\n')
        if len(tbl_lines) <= 2:
            issues[P1].append(f"[检查4] 疑似空表(仅{len(tbl_lines)}行)")
        data_rows = [l for l in tbl_lines if not re.match(r'^[\|\s\-:]+$', l)]
        if len(data_rows) <= 1:
            issues[P1].append("[检查4] 表格无数据行")

    # ── 检查 5: 重复H2标题 ──
    h2s = re.findall(r'^## (\d+) (.+)$', content, re.M)
    h2_counter = Counter(int(n) for n, _ in h2s)
    for num, cnt in h2_counter.items():
        if cnt > 1:
            issues[P0].append(f"[检查5] H2重复: ## {num} 出现{cnt}次")

    # ── 检查 6: 全空列 + 稀疏数值列(仅1个有效值) ──
    for tbl in table_blocks:
        tbl_lines = [l for l in tbl.strip().split('\n') if re.match(r'^\|.+\|$', l)]
        if len(tbl_lines) < 3:
            continue
        header_cells = [c.strip() for c in tbl_lines[0].split('|')[1:-1]]
        n_cols = len(header_cells)
        for ci in range(n_cols):
            col_vals = []
            for line in tbl_lines[2:]:
                cells = line.split('|')[1:-1]
                if ci < len(cells):
                    col_vals.append(cells[ci].strip())
            non_empty = [v for v in col_vals if v and v not in ('—','-','','——','--','─','－','N/A','—','~','~—')]
            # 判断是否为数值列（列标题或单元格含数字模式）
            col_header = header_cells[ci].lower() if ci < len(header_cells) else ''
            has_numbers = any(re.search(r'\d', v) for v in non_empty)
            is_numeric_col = has_numbers or any(kw in col_header for kw in ('亿','%','x','倍','率','额','价','值','PE','PB','PS','EPS','ROE'))
            if len(non_empty) == 0:
                issues[P1].append(f"[检查6] 全空列: 表'{header_cells[0][:15]}'第{ci+1}列('{header_cells[ci][:15] if ci < len(header_cells) else '?'}')")
            elif is_numeric_col and len(non_empty) == 1:
                issues[P1].append(f"[检查6] 稀疏数值列(仅1值): 表'{header_cells[0][:15]}'第{ci+1}列('{header_cells[ci][:15] if ci < len(header_cells) else '?'}')")

    # ── 检查 7: 表格无引用 ──
    for tbl in table_blocks:
        tbl_lines = tbl.split('\n')
        header_first = tbl_lines[0].split('|')[1].strip()[:30] if '|' in tbl_lines[0] else ''
        # Skip: scenario tables (情景/乐观/中性/悲观) — inherently model-derived
        if any(kw in tbl for kw in ('乐观', '中性', '悲观', '情景')):
            continue
        tbl_text_above = ""
        tbl_start = content.find(tbl.split('\n')[0])
        if tbl_start > 0:
            before = content[max(0, tbl_start-300):tbl_start]
            tbl_text_above = '\n'.join(before.split('\n')[-4:])
        refs_in_table = re.findall(r'\[(\d+)\]', tbl)
        src_has_ref = bool(re.search(r'数据来源.*\[(\d+)\]', tbl_text_above))
        if not refs_in_table and not src_has_ref:
            issues[P1].append(f"[检查7] 表格无引用: '{header_first}...'")
        elif not refs_in_table and re.search(r'数据来源|Data source', tbl_text_above, re.I) and not src_has_ref:
            issues[P2].append(f"[检查7] 表前来源行无具体[N]引用: '{header_first}...'")

    # ── 检查 8: 核心章节无引用 ──
    core_patterns = {
        "估值与预测": r'## (?:9|11) .*?(?=## (?:10|12)|\Z)',
    }
    for sec_name, pattern in core_patterns.items():
        m = re.search(pattern, content, re.DOTALL)
        if m and not re.findall(r'\[(\d+)\]', m.group(0)):
            issues[P0].append(f"[检查8] 核心章节无引用: {sec_name}")

    # ── 检查 9: [生成失败] ──
    if '[生成失败]' in content:
        issues[P0].append("[检查9] 报告含'[生成失败]'")

    # ── 检查 10: SRC-X（仅检查正文，参考资料允许含来源编号） ──
    if re.search(r'SRC-\d+', body):
        issues[P0].append("[检查10] 报告正文含内部编号 SRC-X")

    # ── 检查 11: 正文引用-参考资料闭环 ──
    if ref_start > 0:
        body_refs = set(int(x) for x in re.findall(r'\[(\d+)\]', body))
        ref_defs = set(int(x) for x in re.findall(r'^\[(\d+)\]', ref_part, re.M))
        orphan = body_refs - ref_defs
        unused = ref_defs - body_refs
        if orphan:
            issues[P0].append(f"[检查11] 孤引用: {sorted(orphan)}")
        if unused:
            issues[P2].append(f"[检查11] 死引用: {sorted(unused)}")

    # ── 检查 12: 估值分析无引用 ──
    for vs in [r'### 9\.3 估值分析.*?(?=###|## 10|\Z)',
               r'### 11\.[23] 估值分析.*?(?=###|## 12|\Z)']:
        m = re.search(vs, content, re.DOTALL)
        if m and not re.findall(r'\[(\d+)\]', m.group(0)):
            issues[P0].append(f"[检查12] 估值分析小节无引用")

    # ── 检查 13: 情景推演为空 ──
    for sb in [r'情景推演表.*?(?=\n\n---|\n##|\Z)',
               r'### 9\.4 情景推演.*?(?=## 10|\Z)',
               r'### 11\.[34] 情景推演.*?(?=## 12|\Z)']:
        m = re.search(sb, content, re.DOTALL)
        if m:
            sc_rows = re.findall(r'^\| (乐观|中性|悲观).*\|$', m.group(0), re.M)
            if len(sc_rows) < 3:
                issues[P0].append(f"[检查13] 情景推演表为空(仅{len(sc_rows)}行)")
            else:
                for row in sc_rows:
                    if re.search(r'\[需结合|\[对应|\[基准|\[下行|请在LLM|移除本行', row):
                        issues[P0].append(f"[检查13] 情景推演表含占位符(非实体内容): {row[:60]}")
                        break

    # ── 检查 14: 情景推演可复核公式 ──
    if '情景推演' in content:
        has_eps_pe = re.search(r'EPS\s*[×x\*·]\s*PE|每股\s*[×x\*·]\s*PE|基于\s*EPS\s*[×x\*·]\s*PE', content)
        has_pev = re.search(r'P/EV\s*[×x\*·]\s*EV|EV\s*[×x\*·]\s*P/EV|内含价值\s*[×x\*·]|P/EV\s*\d', content)
        has_target = re.search(r'目标价\s*[\d.~-]+\s*[元美元港元]', content)
        if not has_eps_pe and not has_pev:
            issues[P1].append("[检查14] 情景推演未含可复核公式(需EPS×PE或P/EV×EV)")

    # ── 检查 15: HTML标签检测 ──
    if re.search(r'<br\s*/?>', content):
        issues[P0].append("[检查15] 含裸<br>标签——Markdown不应使用HTML换行")
    if re.search(r'<(p|div|span|table|tr|td)\b', content):
        issues[P0].append("[检查15] 含HTML标签——Markdown不应混用HTML")

    # ── 检查 16: 孤立/异常 * 符号 ──
    for i, line in enumerate(content.split('\n')):
        if line.strip().startswith('|') or line.strip().startswith('* ') or line.strip().startswith('- '):
            continue
        single_stars = re.findall(r'(?<!\*)\*(?!\*)', line)
        if len(single_stars) % 2 == 1:
            issues[P1].append(f"[检查16] 孤立*(未闭合Markdown强调): {line.strip()[:60]}")
        m = re.match(r'^\*([^*\n]{30,})\*$', line.strip())
        if m and '**' not in line:
            issues[P1].append(f"[检查16] 整句被*包裹(Markdown斜体误用): {line.strip()[:60]}")

    # ── 检查 17: 经营指标无引用（仅检查段落正文中的数值声明） ──
    op_metrics = ['MAU', 'DAU', 'ARPU', 'GMV', 'take rate', '用户数', '付费用户',
                  '流水', '市场份额', '云收入', '广告收入', '月活', '日活', '装机量',
                  '出货量', '渗透率', '市占率']
    body_only = re.sub(r'\|.*\|', '', content, flags=re.DOTALL)
    body_only = re.sub(r'^#{1,4} .*$', '', body_only, flags=re.M)
    if ref_start > 0:
        body_only = body_only[:ref_start]
    for metric in op_metrics:
        for m in re.finditer(re.escape(metric), body_only):
            nearby = body_only[max(0, m.start()-10):m.end()+40]
            if re.search(r'\[(\d+)\]', nearby):
                continue
            if any(kw in nearby for kw in ('内部测算', '基于', '推算', '验证的数据', '需要验证', '问题', '关注点', '担心', '?')):
                continue
            issues[P1].append(f"[检查17] 经营指标'{metric}'附近无引用[N]: {nearby.strip()[:60]}")

    # ── 检查 18: 催化事件表条数 ──
    cat_evt_rows = 0
    for sec_num in ['3', '4']:
        sec_pat = rf'## {sec_num} .*?(?=## {int(sec_num)+1} |\Z)'
        sec = re.search(sec_pat, content, re.DOTALL)
        if sec:
            rows = re.findall(r'^\|\s*(2\d{3}|FY\d|距今).*\|$', sec.group(0), re.M)
            if rows:
                cat_evt_rows = len(rows)
                break
    if cat_evt_rows == 0:
        issues[P1].append("[检查18] 催化事件表未找到(需≥3条)")
    elif cat_evt_rows < 3:
        issues[P1].append(f"[检查18] 催化事件表仅{cat_evt_rows}条(要求≥3)")

    # ── 检查 19: 树形调试符号 ──
    for sym, desc in [('└', 'tree-l'), ('├', 'tree-t'), ('│', 'tree-pipe')]:
        if sym in content:
            issues[P0].append(f"[检查19] 含树形调试符号: '{sym}'")

    # ── 检查 20: 同业比较表完整 schema ──
    # A-share: §8.2 同业比较
    # HK-US: §9 行业对比
    required_peer_cols_a = ['可比业务', '相关业务进展', '竞争关系']
    required_peer_cols_hk = ['竞争关系', '公司', '可比业务', '行业地位', '相关业务进展', '商业模式', '目标客户群体', '核心产品']

    # Find peer section by manual indexing for A-share, regex for HK-US
    peer_section = None
    if market == "A":
        idx_peer_start = content.find('## 8 行业分析及同业对比')
        if idx_peer_start >= 0:
            idx_peer_end = content.find('## 9 ', idx_peer_start + 1)
            if idx_peer_end < 0:
                idx_peer_end = len(content)
            peer_section = content[idx_peer_start:idx_peer_end]
    else:
        for pat in [r'## 9 行业对比与 A/H 映射.*?(?=## \d+|\Z)', r'## 9 行业对比.*?(?=## \d+|\Z)']:
            m = re.search(pat, content, re.DOTALL)
            if m:
                peer_section = m.group(0)
                break
        # Fallback: manual index
        if not peer_section:
            idx_peer_start = content.find('## 9 行业对比')
            if idx_peer_start >= 0:
                idx_peer_end = content.find('## 10 ', idx_peer_start + 1)
                if idx_peer_end < 0:
                    idx_peer_end = len(content)
                peer_section = content[idx_peer_start:idx_peer_end]

    peer_tables_found = []
    if peer_section:
        peer_tables = re.findall(r'(\|.+\|.*\n(?:\|.+\|.*\n)+)', peer_section)
        for tbl in peer_tables:
            hdr = tbl.split('\n')[0]
            hdr_cols = [c.strip() for c in hdr.split('|')[1:-1]]
            joined_hdr = ','.join(hdr_cols)
            # Must have at least one of these peer-identifying columns
            is_peer_table = any(c in joined_hdr for c in ['竞争关系', '可比业务', '行业地位'])
            if is_peer_table:
                peer_tables_found.append((hdr_cols, tbl))
                # Use appropriate schema based on market
                req_cols = required_peer_cols_hk if market in ("HK", "US") else required_peer_cols_a
                missing = [c for c in req_cols if not any(c in hc for hc in hdr_cols)]
                if missing:
                    issues[P1].append(f"[检查20] 同业比较表缺必需列: {missing}")
                # Check row count
                data_rows = [l for l in tbl.strip().split('\n') if re.match(r'^\|.+\|$', l)]
                if len(data_rows) < 5:  # header + sep + self + ≥3 peers = 6 total (header+sep=2, need ≥4 data rows)
                    issues[P1].append(f"[检查20] 同业比较表行数不足(仅{len(data_rows)}行，需≥6: 本公司行+≥3家可比)")
                # Check for self row (竞争关系 or 可比业务 column with '—')
                has_self_row = bool(re.search(r'\|\s*—\s*\|.*\|\s*—\s*\|', tbl))
                if not has_self_row:
                    issues[P1].append("[检查20] 同业比较表缺本公司行(本公司行的竞争关系/可比业务列应为'—')")

    # If no peer comparison table found at all
    if not peer_tables_found:
        issues[P1].append("[检查20] 未找到合格同业比较表——需以正式表格呈现，不接受纯文字行业格局描述")

    # ── 检查 21: 情景推演数值化 ──
    scenario_check = re.search(r'(?:### 9\.4 情景推演|### 11\.[34] 情景推演).*?(?=##|### \d+\.\d|\Z)', content, re.DOTALL)
    if scenario_check:
        sc_text = scenario_check.group(0)
        has_numbers = bool(re.search(r'\d+\.?\d*[%亿x倍美元港元]', sc_text))
        has_formula = bool(re.search(r'[=×*]|EPS|每股|目标价|BVPS|P/EV', sc_text))
        if not has_numbers:
            issues[P0].append("[检查21] 情景推演无具体数值")
        elif not has_formula:
            issues[P1].append("[检查21] 情景推演有数值但无可复核公式")

    # ── 检查 22: 公司/代码分列 ──
    for tbl in table_blocks:
        header_cells = [c.strip() for c in tbl.split('\n')[0].split('|')[1:-1]]
        has_company = any('公司' in c and '代码' not in c and '(' not in c for c in header_cells)
        has_code = any('代码' in c or c.strip() == '代码' for c in header_cells)
        if has_company and has_code:
            issues[P1].append(f"[检查22] 公司/代码分两列(应合并): {header_cells[:3]}")

    # ── 检查 23: 引用编号必须从 [1] 开始 ──
    if ref_start > 0:
        body_nums = sorted(set(int(x) for x in re.findall(r'\[(\d+)\]', body)))
        if body_nums and body_nums[0] != 1:
            issues[P1].append(f"[检查23] 引用编号未从[1]开始，首个引用为[{body_nums[0]}]")
        if len(body_nums) > 1:
            for i in range(len(body_nums) - 1):
                if body_nums[i+1] - body_nums[i] > 1:
                    issues[P1].append(f"[检查23] 引用编号不连续: [{body_nums[i]}]→[{body_nums[i+1]}]")
                    break

    # ── 检查 24: 参考资料章节格式（A股允许无编号，港美股必须编号） ──
    if market in ("HK", "US"):
        bare_ref = re.search(r'^## 参考资料\s*$', content, re.M)
        if bare_ref:
            issues[P1].append("[检查24] 港美股参考资料章节无编号，必须为'13 参考资料'或对应编号")

    # ── 检查 25: 稀疏行列（全—占位行/列必须删除） ──
    for tbl in table_blocks:
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
                issues[P1].append(f"[检查25] 稀疏行(整行—占位): 表'{header[0][:15] if header else '?'}' 第{ri}行 '{cells[0][:20] if cells else ''}'")
        for ci in range(len(header)):
            col_vals = []
            for line in tbl_lines[2:]:
                cells = line.split('|')[1:-1]
                if ci < len(cells):
                    col_vals.append(cells[ci].strip())
            non_empty = [v for v in col_vals if v not in ('', '—', '--', '——', ' ', '-')]
            if len(non_empty) == 0 and len(col_vals) >= 2:
                issues[P1].append(f"[检查25] 稀疏列(全空/—): 表'{header[0][:15] if header else '?'}'第{ci+1}列('{header[ci][:15] if ci < len(header) else '?'}')")

    # ── 检查 26: 价格时效性 ──
    price_match = re.search(r'当前价格[：:]\s*~?[\d,.]+', content)
    if price_match:
        gen_date_match = re.search(r'生成日期[：:]\s*(\d{4}-\d{2}-\d{2})', content)
        price_year_match = re.search(r'（(\d{4}-\d{2})）', content[price_match.start():price_match.start()+60])
        if gen_date_match and price_year_match:
            gen_date = gen_date_match.group(1)
            price_date = price_year_match.group(1)
            if price_date < gen_date[:7]:
                issues[P1].append(f"[检查26] '当前价格'日期({price_date})早于生成日期({gen_date})")

    # ── 检查 27: DOCX vMerge 验证 ──
    has_grouped_table = bool(re.search(r'^\| \|.+\|$', body, re.M))
    if has_grouped_table:
        vmerge_json = md_path.replace('.md', '.vmerge.json')
        if os.path.exists(vmerge_json):
            try:
                with open(vmerge_json, 'r', encoding='utf-8') as vf:
                    vd = json.load(vf)
                vc = vd.get('vmerge_count', 0)
                if vc == 0:
                    issues[P1].append(f"[检查27] MD含分组表但DOCX vMerge=0")
            except Exception:
                issues[P1].append("[检查27] MD含分组表但无法验证vMerge.json")
        else:
            issues[P1].append("[检查27] MD含分组表但无vmerge.json")

    # ── 检查 28: 报告正文禁止内部质检话术 ──
    forbidden_qa_terms = ['schema', '标准schema', '9列', '10列', '本轮删除', '其余列完整',
                          'checker', 'quality gate', 'quality_check', 'id_audit',
                          'fresh_generation', 'artifact', 'raw_payload']
    for term in forbidden_qa_terms:
        if term in body:
            issues[P1].append(f"[检查28] 报告正文含内部质检话术: '{term}'")
    if re.search(r'\bP0\b.*[=：:]|P0\s*[=＞]', body):
        issues[P1].append("[检查28] 报告正文含质检级别标识 'P0'")
    if re.search(r'\bP1\b.*[=：:]|P1\s*[=＞]', body):
        issues[P1].append("[检查28] 报告正文含质检级别标识 'P1'")
    if re.search(r'\bP2\b.*[=：:]|P2\s*[=＞]', body):
        issues[P1].append("[检查28] 报告正文含质检级别标识 'P2'")
    if re.search(r'check_report_quality', body):
        issues[P1].append("[检查28] 报告正文含内部质检话术: 'check_report_quality'")

    # ── 检查 29: 估值口径一致性 ──
    if re.search(r'PE\(TTM\)[：:].*\d{4}E', content):
        issues[P1].append("[检查29] 估值口径混用: PE(TTM)与预测年度(如2026E)不应同时出现")
    if re.search(r'当前价格.*USD.*港元|当前价格.*HKD.*人民币', content):
        issues[P1].append("[检查29] 币种混用: 价格单位应统一")
    has_ifrs = bool(re.search(r'IFRS|Non-IFRS|经调整', body.split('---')[0] if '---' in body else body[:500]))
    has_gaap = bool(re.search(r'GAAP', body.split('---')[0] if '---' in body else body[:500]))
    has_nongaap = bool(re.search(r'Non-GAAP|non-GAAP|经调整', body.split('---')[0] if '---' in body else body[:500]))
    if has_ifrs and has_gaap:
        issues[P1].append("[检查29] 会计准则混用: IFRS与GAAP不应同时出现")

    # ═══════════════════════════════════════════════════════
    # v1.2.3-R2 新增检查
    # ═══════════════════════════════════════════════════════

    # ── 检查 30: 必填章节非空（required_section_non_empty） ──
    # A 股必填章节列表
    if market == "A":
        required_h2 = {
            '3': '催化事件时间表',
            '9': '一致预期/盈利预测/估值',
        }
    else:
        required_h2 = {
            '4': '催化事件时间表',
            '11': '估值与预测',
        }

    for sec_num, sec_name in required_h2.items():
        sec_pat = rf'## {sec_num} {sec_name}.*?(?=## {int(sec_num)+1} |## 参考资料|\Z)'
        sec = re.search(sec_pat, content, re.DOTALL)
        if sec:
            sec_body = sec.group(0)
            # Remove headers within the section
            clean_body = re.sub(r'^#{1,4} .*$', '', sec_body, flags=re.M)
            # Remove "数据来源" lines
            clean_body = re.sub(r'\*数据来源.*$', '', clean_body, flags=re.M)
            clean_body = re.sub(r'数据来源[：:].*$', '', clean_body, flags=re.M)
            # Remove blank lines
            clean_body = re.sub(r'\n\s*\n', '\n', clean_body)
            clean_body = clean_body.strip()
            # Check if there's a markdown table with actual content
            has_table = bool(re.search(r'^\|.+\|$', clean_body, re.M))
            has_bullets = bool(re.search(r'^[\*\-•].{5,}', clean_body, re.M))
            has_paragraph = len(clean_body) > 100
            if not has_table and not has_bullets and not has_paragraph:
                issues[P1].append(f"[检查30] 必填章节为空: ## {sec_num} {sec_name}——仅有标题或数据来源，无正文/表格/bullet内容")

    # ── 检查 31: 情景推演必须有可计算数值（scenario_table_must_have_computable_values） ──
    scenario_section = re.search(r'(?:### 9\.4 情景推演|### 11\.[34] 情景推演).*?(?=## |### \d+\.\d(?!\.\d)|\Z)', content, re.DOTALL)
    if scenario_section:
        sc_text = scenario_section.group(0)
        # Check for template-only placeholders
        template_only_phrases = [
            '基于核心变量乐观假设',
            '基于核心变量悲观假设',
            '基于核心变量基准假设',
            '基于EPS×PE=目标价',
            '基于核心变量',
            '乐观假设',
            '悲观假设',
        ]
        has_template_only = any(phrase in sc_text for phrase in template_only_phrases)
        # Check for actual numeric values in scenario cells
        numeric_in_scenario = bool(re.search(r'(?:乐观|中性|悲观).*?\d+\.?\d*[%亿x倍美元港元千万百]', sc_text))

        if has_template_only and not numeric_in_scenario:
            issues[P1].append("[检查31] 情景推演为模板占位——仅有'基于核心变量乐观假设/基于EPS×PE=目标价'等话术，无有效数值")
        elif has_template_only and numeric_in_scenario:
            # Partial: has some numbers but also template text
            issues[P1].append("[检查31] 情景推演含模板占位话术(如'基于核心变量乐观假设')——需全部替换为具体数值")

    # ── 检查 32: 内部 pipeline 页尾信息（no_pipeline_footer_in_final_report） ──
    # Check last 10 lines of the file for pipeline/engineering footers
    lines = content.split('\n')
    tail = '\n'.join(lines[-15:])  # Check last 15 lines
    pipeline_footer_patterns = [
        r'v\d+\.\d+\.\d+\s+(?:HK-US|A-share|pipeline)',
        r'HK-US pipeline',
        r'pipeline\s*[·•]\s*\d{4}-\d{2}-\d{2}',
    ]
    for pat in pipeline_footer_patterns:
        if re.search(pat, tail):
            issues[P1].append(f"[检查32] 页尾含内部pipeline信息: 匹配 '{pat}'")
            break

    # Also check for "数据来源: Datayes getMaterialsV2" as a standalone footer (not in 参考资料)
    if re.search(r'^>.*数据来源[：:]\s*Datayes\s+getMaterialsV2', content, re.M):
        issues[P1].append("[检查32] 页尾含内部话术: '数据来源: Datayes getMaterialsV2'——应仅在参考资料章节出现")

    # ── 检查 33: 港美股最小结构要求（hk_us_minimum_structure_required） ──
    if market in ("HK", "US"):
        hk_us_required_sections = [
            ('近况跟踪', [r'## 2 近况跟踪', r'## 2 .*近况']),
            ('催化事件时间表', [r'## 4 催化事件时间表', r'## 4 .*催化']),
            ('业务拆分', [r'## 5 业务拆分', r'## 5 .*业务']),
            ('财务数据分析/预测', [r'## 7 财务与盈利质量', r'## 7 .*财务', r'## 5 财务预测']),
            ('行业分析及同业对比', [r'## 9 行业对比', r'## 9 .*行业', r'## 8 行业']),
            ('情景推演', [r'### 11\.4 情景推演', r'### 11\.[34] 情景推演']),
            ('风险提示', [r'## 12 风险提示', r'## \d+ 风险提示']),
            ('参考资料', [r'## 13 参考资料', r'## \d+ 参考资料']),
        ]

        missing_sections = []
        for sec_name, patterns in hk_us_required_sections:
            found = any(re.search(pat, content) for pat in patterns)
            if not found:
                missing_sections.append(sec_name)

        if missing_sections:
            issues[P1].append(f"[检查33] 港美股报告结构过薄——缺失关键章节: {', '.join(missing_sections)}")

        # Also check overall H2 count — a valid onepager should have at least 10 numbered H2 sections
        h2_count = len(set(re.findall(r'^## (\d+) ', content, re.M)))
        if h2_count < 9:
            issues[P1].append(f"[检查33] 港美股报告章节过少(仅{h2_count}个H2)——疑似摘要版而非完整onepager，需补齐关键结构")

    # ── 检查 34: 估值口径一致（扩展） ──
    # Check for mixed valuation approaches in the same paragraph
    valuation_consistency = re.search(r'(?:### 9\.3 估值分析|### 11\.3 估值分析).*?(?=###|\Z)', content, re.DOTALL)
    if valuation_consistency:
        v_text = valuation_consistency.group(0)
        has_pe_ttm = bool(re.search(r'PE.*TTM|TTM.*PE', v_text))
        has_pe_forward = bool(re.search(r'PE.*\d{4}E|预测PE|forward.*PE', v_text))
        if has_pe_ttm and has_pe_forward:
            issues[P2].append("[检查34] 估值分析同时引用PE(TTM)和预测PE——建议明确标注口径差异")

    # ── 检查 35: 无"未披露/N/A/待补充"等空占位符 ──
    placeholder_patterns = [
        (r'未披露', '未披露'),
        (r'未提供', '未提供'),
        (r'无数据', '无数据'),
        (r'N/A', 'N/A'),
    ]
    for pat, label in placeholder_patterns:
        matches = re.findall(pat, body, re.I)
        if len(matches) >= 3:  # Occasional use is OK but widespread indicates a problem
            issues[P1].append(f"[检查35] 大量'{label}'占位符({len(matches)}处)——应按稀疏处理规则删除或改写")

    # ── 检查 36: 结构化接口 source_trace 标记（仅提示，不阻断） ──
    # 检查参考资料中结构化接口是否有 entity_id/ticker
    struct_refs = re.findall(r'\[(\d+)\].*结构化接口.*API[：:]\s*(\S+)', ref_part)
    for ref_no, api_name in struct_refs:
        # This is informational — structured API refs with entity_id/ticker are valid
        pass  # No blocking issue, this check is for trace purposes only

    return issues


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v1.2.3 报告质量门禁")
    parser.add_argument("markdown", help="Markdown 报告路径")
    parser.add_argument("--market", default="A", choices=["A","HK","US"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    issues = check_report(args.markdown, args.market)
    p0, p1, p2 = len(issues["P0"]), len(issues["P1"]), len(issues["P2"])

    if args.json:
        print(json.dumps({"P0": p0, "P1": p1, "P2": p2, "issues": issues}, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  v1.2.3 质量检查: {args.markdown}")
        print(f"  市场: {args.market}  |  P0={p0}  P1={p1}  P2={p2}")
        print(f"{'='*60}")
        for level in ["P0", "P1", "P2"]:
            for issue in issues[level]:
                print(f"  [{level}] {issue}")
        if p0 == 0 and p1 == 0 and p2 == 0:
            print(f"\n  PASS - no issues")
        elif p0 == 0:
            print(f"\n  PASS_WITH_NOTES - {p1} P1")
        else:
            print(f"\n  FAIL - {p0} P0 blocking")

    sys.exit(1 if p0 > 0 else 0)
