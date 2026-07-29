#!/usr/bin/env python3
"""
v1.2.2 报告后处理管线
1. 小节编号自动修复：确保H3编号第一位与所属H2一致
2. 稀疏行列自动清理：删除有效数据≤1的行/列
3. 参考资料自动生成：从materials JSON逐字读取元数据，按首次引用顺序重新编号
"""
import re, json, sys
from pathlib import Path
from datetime import datetime
from typing import Any
from table_classifier import classify_table, should_apply_sparse_rules, get_checkable_columns, TableType

_HEADING = re.compile(r'^#{1,3}\s+(.*)$')
_H_NUM = re.compile(r'^(\d+)')
_H3_NUM = re.compile(r'^(\d+)\.(\d+)')
_REF_BODY = re.compile(r'\[(\d+|A\d+)\]')
_REF_DEF = re.compile(r'^\[(\d+|A\d+)\]\s*(.*)')
_ID_EXTRACT = re.compile(r'ID[：:]\s*(\S+)')
_TABLE_LINE = re.compile(r'^\|.+\|$')
_TABLE_SEP = re.compile(r'^\|[:\-\s|]+\|$')
_IS_NUM = re.compile(r'\d')
_EMPTY_VALS = {'—', '-', 'N/A', 'NA', '', '待补充', '...'}

# ─── Data type mapping ───
TYPE_MAP = {
    'research': 'Materials V2研报',
    'meetingSummary': 'Materials V2纪要',
    'meeting_summary': 'Materials V2纪要',
    'marketView': 'Materials V2市场观点',
    'market_view': 'Materials V2市场观点',
    'wechat': 'Materials V2微信',
    'news': 'Materials V2新闻',
    'announcement': 'Materials V2公告',
}

API_MAP = {
    'research': 'getMaterialsV2',
    'meetingSummary': 'getMaterialsV2',
    'marketView': 'getMaterialsV2',
    'wechat': 'getMaterialsV2',
    'news': 'getMaterialsV2',
    'announcement': 'getMaterialsV2',
}


def load_materials(materials_path: str) -> dict[str, Any]:
    with open(materials_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_source_index(materials: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从materials JSON构建source_id索引。递归收集所有含id字段的对象。"""
    index: dict[str, dict[str, Any]] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if 'id' in obj and obj.get('id') is not None:
                sid = str(obj['id'])
                if sid not in index:
                    index[sid] = obj
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(materials)
    return index


def collect_used_refs(body_lines: list[str]) -> dict[str, int]:
    """收集正文中实际使用的引用编号及其首次出现行号。"""
    used: dict[str, int] = {}
    for lineno, line in enumerate(body_lines, 1):
        refs = _REF_BODY.findall(line)
        for r in refs:
            if r not in used:
                used[r] = lineno
    return used


def parse_existing_refs(ref_lines: list[str]) -> dict[str, str]:
    """解析已有参考资料章节。"""
    refs: dict[str, str] = {}
    for line in ref_lines:
        m = _REF_DEF.match(line.strip())
        if m:
            refs[m.group(1)] = m.group(2)
    return refs


def build_ref_entry(source: dict[str, Any], materials: dict[str, Any]) -> str | None:
    """从单个source对象构建标准参考资料条目。"""
    sid = str(source.get('id', ''))
    dtype = source.get('dataType', source.get('type', ''))
    meta = source.get('metadata', {}) or {}
    title = source.get('title', '') or meta.get('title', '')
    org = meta.get('organization', '') or source.get('organization', '')
    pub_time = meta.get('publishTime', '') or source.get('publishTime', '')

    if not title:
        return None

    src_type = TYPE_MAP.get(dtype, f'Materials V2{dtype}')
    api_name = API_MAP.get(dtype, 'getMaterialsV2')

    # Normalize date format
    date_str = ''
    if pub_time:
        m = re.match(r'(\d{4}-\d{2}-\d{2})', str(pub_time))
        if m:
            date_str = m.group(1)

    if not org:
        org = '-'

    return f'[{sid}]' + src_type + f' | {date_str} | ID：{sid} | {org} | {title} | API：{api_name}'


def fix_section_numbering(lines: list[str]) -> tuple[list[str], list[dict]]:
    """修复H3小节编号使其第一位与所属H2一致。返回(修复后行列表, 修复报告)。"""
    fixed = list(lines)
    fixes = []
    parent_h2_num = None
    h2_title = None

    for i, line in enumerate(lines):
        m = _HEADING.match(line.strip())
        if not m:
            continue
        title = m.group(1).strip()
        level = line.strip().count('#', 0, 3)

        if level == 2:
            num_m = _H_NUM.match(title)
            if num_m:
                parent_h2_num = num_m.group(1)
                h2_title = title
        elif level == 3 and parent_h2_num:
            num_m = _H3_NUM.match(title)
            if num_m and num_m.group(1) != parent_h2_num:
                old_title = title
                new_num = f'{parent_h2_num}.{num_m.group(2)}'
                new_title = re.sub(r'^\d+\.\d+', new_num, title)
                old_prefix = '#' * level
                fixed[i] = f'{old_prefix} {new_title}'
                fixes.append({
                    'line': i + 1,
                    'old': old_title,
                    'new': new_title,
                    'parent_h2': h2_title,
                    'parent_num': parent_h2_num,
                })

    return fixed, fixes


def clean_sparse_tables(lines: list[str]) -> tuple[list[str], list[dict]]:
    """清理稀疏表格行列。使用 table_classifier 区分表格类型。返回(清理后行列表, 清理报告)。"""
    cleaned = list(lines)
    deletions: list[dict] = []

    # 按表格收集
    tables: list[dict] = []
    in_table = False
    current_table: dict = {'start': 0, 'rows': []}

    for i, line in enumerate(lines):
        is_table_line = bool(_TABLE_LINE.match(line.strip()))
        is_sep = bool(_TABLE_SEP.match(line.strip().replace(' ', '') or ''))

        if is_table_line and not is_sep:
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if not in_table:
                current_table = {'start': i, 'rows': [(i, cells)]}
                in_table = True
            else:
                current_table['rows'].append((i, cells))
        elif in_table and not is_table_line:
            if len(current_table['rows']) >= 2:
                current_table['end'] = i
                tables.append(current_table)
            in_table = False
        elif is_table_line and is_sep:
            if in_table and len(current_table['rows']) >= 1:
                current_table['rows'].append((i, ['__SEP__']))  # placeholder for seps

    # Handle last table
    if in_table and len(current_table['rows']) >= 2:
        current_table['end'] = len(lines)
        tables.append(current_table)

    # Analyze each table
    for t in tables:
        data_rows = [(ln, cells) for ln, cells in t['rows'] if '__SEP__' not in cells]
        if len(data_rows) < 2:  # need header + at least 1 data row
            continue

        header = data_rows[0][1]
        body = data_rows[1:]

        # Classify table type
        classification = classify_table(header)
        if not should_apply_sparse_rules(classification):
            continue  # Skip qualitative tables for sparse checking

        checkable_cols = get_checkable_columns(classification)

        # Check each data row (only checkable columns)
        rows_to_delete: set[int] = set()
        for ln, cells in body:
            valid = sum(1 for c_idx in checkable_cols
                       if c_idx < len(cells) and cells[c_idx]
                       and cells[c_idx] not in _EMPTY_VALS and _IS_NUM.search(cells[c_idx]))
            total_checkable = len([c_idx for c_idx in checkable_cols if c_idx < len(cells)])
            if valid <= 1 and total_checkable > 1:
                rows_to_delete.add(ln)
                deletions.append({
                    'line': ln + 1,
                    'type': 'row',
                    'content': cells[0][:40] if cells else '?',
                    'valid_cells': valid,
                    'total_cells': total_checkable,
                    'table_type': classification.table_type.value,
                })

        # Check each column (only checkable columns)
        cols_to_delete: set[int] = set()
        for c_idx in checkable_cols:
            if c_idx >= len(header):
                continue
            valid = sum(1 for _, cells in body
                       if c_idx < len(cells) and cells[c_idx]
                       and cells[c_idx] not in _EMPTY_VALS and _IS_NUM.search(cells[c_idx]))
            if valid <= 1 and len(body) > 1:
                cols_to_delete.add(c_idx)
                col_header = header[c_idx]
                populated = [ln for ln, cells in body
                           if c_idx < len(cells) and cells[c_idx] and cells[c_idx] not in _EMPTY_VALS]
                deletions.append({
                    'type': 'column',
                    'header': col_header,
                    'valid_cells': valid,
                    'total_rows': len(body),
                    'populated_in_rows': populated,
                    'table_type': classification.table_type.value,
                })

        # Apply deletions
        if rows_to_delete:
            for i in range(len(cleaned)):
                if i in rows_to_delete:
                    cleaned[i] = ''  # mark for deletion
        if cols_to_delete:
            for i in range(len(cleaned)):
                if _TABLE_LINE.match(cleaned[i].strip()) and not _TABLE_SEP.match(cleaned[i].strip().replace(' ', '') or ''):
                    cells = cleaned[i].strip().strip('|').split('|')
                    new_cells = [cells[0]] if cells else []
                    for c_idx in range(1, len(cells)):
                        if c_idx not in cols_to_delete:
                            new_cells.append(cells[c_idx])
                    cleaned[i] = '|'.join(new_cells)
                    if not cleaned[i].startswith('|'):
                        cleaned[i] = '|' + cleaned[i]
                    if not cleaned[i].endswith('|'):
                        cleaned[i] = cleaned[i] + '|'

    # Filter out empty lines
    cleaned = [l for l in cleaned if l != '']

    return cleaned, deletions


def rebuild_references(
    lines: list[str],
    materials: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
) -> tuple[list[str], dict]:
    """重建参考资料章节：自动收集来源、按首次引用重新编号、替换正文引用。"""
    # Split report body from reference section
    ref_heading_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^#+\s*(?:13\s*)?参考资料', line.strip()):
            ref_heading_idx = i
            break

    if ref_heading_idx is None:
        return lines, {'error': 'no_reference_section_found'}

    body_lines = lines[:ref_heading_idx]
    old_ref_text = '\n'.join(lines[ref_heading_idx:])

    # Collect used references from body
    used = collect_used_refs(body_lines)

    # Parse old reference definitions to find which source_ids map to which ref numbers
    old_refs = parse_existing_refs(lines[ref_heading_idx + 1:])

    # Build new reference entries
    # Map old number -> new number
    remap: dict[str, str] = {}

    # Special handling for A-prefix refs (structured data)
    new_refs: list[str] = []
    new_num = 1
    ref_report: dict = {
        'total_used': len(used),
        'sources_available': len(source_index),
        'mapped': 0,
        'unmapped': [],
        'structured_refs': [],
    }

    # Process A-prefix refs first (structured data)
    for ref_id in sorted(used.keys()):
        if ref_id.startswith('A'):
            old_def = old_refs.get(ref_id, '')
            new_ref_id = f'A{ref_id[1:]}'
            new_refs.append(f'[{new_ref_id}] {old_def}')
            remap[ref_id] = new_ref_id
            ref_report['structured_refs'].append(ref_id)

    # Process numeric refs
    ordered_numeric = sorted(
        [(r, used[r]) for r in used if not r.startswith('A')],
        key=lambda x: x[1]  # sort by first occurrence line
    )

    for ref_id, first_line in ordered_numeric:
        # Try to find source in index
        matched = False
        source = None

        # Step 1: Extract source ID from old reference definition
        old_def = old_refs.get(ref_id, '')
        id_m = _ID_EXTRACT.search(old_def)
        if id_m:
            extracted_id = id_m.group(1)
            source = source_index.get(extracted_id)

        # Step 2: Try direct ID lookup
        if not source:
            source = source_index.get(ref_id)

        # Step 3: Fallback to title matching
        if not source:
            for sid, s in source_index.items():
                if isinstance(s, dict):
                    title = str(s.get('title', ''))
                    if title and len(title) >= 20 and title[:30] in old_def:
                        source = s
                        break

        if source and isinstance(source, dict):
            entry = build_ref_entry(source, materials)
            if entry:
                entry = re.sub(r'^\[[^\]]+\]', f'[{new_num}]', entry)
                new_refs.append(entry)
                remap[ref_id] = str(new_num)
                ref_report['mapped'] += 1
                new_num += 1
                matched = True

        if not matched:
            ref_report['unmapped'].append(ref_id)
            # Keep the old reference as-is but warn
            old_def = old_refs.get(ref_id, ref_id)
            new_refs.append(f'[{new_num}] {old_def}')
            remap[ref_id] = str(new_num)
            new_num += 1

    # Replace reference numbers in body
    rebuilt_body: list[str] = []
    for line in body_lines:
        def replace_ref(m):
            r = m.group(1)
            if r in remap:
                return f'[{remap[r]}]'
            return m.group(0)
        rebuilt_body.append(_REF_BODY.sub(replace_ref, line))

    # Build new reference section
    ref_heading = lines[ref_heading_idx].strip()
    new_ref_section = [ref_heading, '']
    new_ref_section.extend(new_refs)

    result = rebuilt_body + [''] + new_ref_section

    return result, ref_report


def post_process(
    report_path: str,
    materials_path: str,
    output_path: str | None = None,
) -> dict:
    """完整的v1.2.2后处理管线。"""
    with open(report_path, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    materials = load_materials(materials_path)
    source_index = build_source_index(materials)

    results: dict = {
        'report_path': report_path,
        'materials_path': materials_path,
        'section_fixes': [],
        'sparse_deletions': [],
        'reference_report': {},
        'total_source_count': len(source_index),
    }

    # Step 1: Fix section numbering
    lines, section_fixes = fix_section_numbering(lines)
    results['section_fixes'] = section_fixes

    # Step 2: Rebuild references (must happen before sparse cleanup since refs might change)
    lines, ref_report = rebuild_references(lines, materials, source_index)
    results['reference_report'] = ref_report

    # Step 3: Clean sparse tables
    lines, sparse_dels = clean_sparse_tables(lines)
    results['sparse_deletions'] = sparse_dels

    # Write output
    out = output_path or report_path
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    results['output_path'] = out
    return results


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('Usage: python post_process.py <report.md> <materials.json> [output.md]')
        sys.exit(1)

    result = post_process(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
