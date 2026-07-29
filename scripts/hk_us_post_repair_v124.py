#!/usr/bin/env python3
"""
hk_us_post_repair_v124.py — 港美股报告生成后修复脚本
====================================================
用法:
    python hk_us_post_repair_v124.py \\
        --input-md <report.md> \\
        --source-trace <source_trace.json> \\
        --materials <materials.json> \\
        --output-md <repaired.md> \\
        --market hk|us

功能:
    1. 内部 pipeline 话术清除
    2. 港美股最小结构检查（只记录日志）
    3. 参考资料时效性检查（只记录日志）

Mechanical-only contract:
    post-repair 不补 peer、同行公司、目标价、估值情景、催化、风险或章节正文；
    不给事实重新挂引用，不修改 source_trace，不删除错误引用后让报告继续通过。
"""

from __future__ import annotations
import argparse, re, sys, json, os, datetime
from pathlib import Path

TODAY = datetime.date.today().isoformat()


# =============================================================================
# 1. 内部 pipeline 话术清除
# =============================================================================

def remove_internal_pipeline_footer(content: str) -> tuple:
    """删除 v1.2.3 HK-US pipeline / fresh_generation / checker 等内部话术。"""
    repair_log = []

    forbidden = [
        r'v\d+\.\d+\.\d+\s+HK-US\s+pipeline',
        r'fresh_generation',
        r'checker',
        r'quality\s+gate',
        r'v1\.2\.\d+\s+HK-US',
        r'数据来源:\s*Datayes\s+getMaterialsV2',
    ]

    lines = content.split('\n')
    new_lines = []
    removed = 0

    for line in lines:
        stripped = line.strip()
        if any(re.search(p, stripped, re.IGNORECASE) for p in forbidden):
            removed += 1
            continue
        new_lines.append(line)

    if removed > 0:
        repair_log.append(f'清除内部话术: {removed}行')

    return '\n'.join(new_lines), repair_log


# =============================================================================
# 8. 港美股最小结构检查
# =============================================================================

def validate_hk_us_minimum_structure(content: str, market: str) -> tuple:
    """检查港美股最小章节结构。"""
    repair_log = []

    required_sections = [
        ('关键要点', '§1'),
        ('近况跟踪', '§2'),
        ('核心投资逻辑', '§3'),
        ('催化事件时间表', '§4'),
        ('业务拆分', '§5'),
        ('产销链', '§6'),
        ('财务与盈利质量', '§7'),
        ('市场关注', '§8'),
        ('行业对比', '§9'),
        ('市场分歧', '§10'),
        ('估值与预测', '§11'),
        ('风险提示', '§12'),
        ('参考资料', '§13'),
    ]

    missing = []
    for name, ref in required_sections:
        if name not in content:
            missing.append(ref)

    if missing:
        repair_log.append(f'❌ 缺章节: {missing}')
    else:
        repair_log.append('✅ 最小结构: 完整')

    return content, repair_log


# =============================================================================
# 9. 参考资料时效性检查
# =============================================================================

def check_reference_recency(content: str) -> tuple:
    """检查参考资料时效性。核心引用超过6个月→P2，估值/预测/催化引用超6个月→P1。"""
    issues = []

    ref_start = content.find('## 13 ')
    if ref_start < 0:
        ref_start = content.find('参考资料')
    if ref_start < 0:
        return content, issues

    ref_section = content[ref_start:]
    body = content[:ref_start]

    # Parse reference dates
    ref_dates = {}
    for line in ref_section.split('\n'):
        m = re.match(r'^\[(\d+)\].*?\|\s*(\d{4}-\d{2}-\d{2})', line.strip())
        if m:
            ref_dates[int(m.group(1))] = m.group(2)

    # Check body citations for recency
    today = datetime.date.today()
    cutoff_6m = today - datetime.timedelta(days=180)

    old_refs = set()
    for ref_num, ref_date in ref_dates.items():
        try:
            d = datetime.date.fromisoformat(ref_date)
            if d < cutoff_6m:
                old_refs.add(ref_num)
        except ValueError:
            pass

    if old_refs:
        # Check if old refs are used in critical sections
        critical_sections = ['估值', '预测', '催化', '情景推演', '目标价', '盈利预测']
        for sec in critical_sections:
            sec_start = body.find(sec)
            if sec_start > 0:
                sec_end = min(sec_start + 2000, len(body))
                sec_content = body[sec_start:sec_end]
                for rn in old_refs:
                    if f'[{rn}]' in sec_content:
                        issues.append(f'P1: [{rn}]({ref_dates[rn]})用于{sec}章节但超过6个月')

        # General old refs
        for rn in old_refs:
            if f'[{rn}]' in body:
                if not any(f'P1: [{rn}]' in i for i in issues):
                    issues.append(f'P2: [{rn}]({ref_dates[rn]})引用超过6个月未标注原因')

    return content, issues


# =============================================================================
# 主入口
# =============================================================================

def repair_report(input_md: str, source_trace_path: str, materials_path: str,
                  output_md: str, market: str) -> None:
    """执行 mechanical-only 修复流程；不得补语义内容或改引用归属。"""
    print(f'hk_us_post_repair_v124: market={market}')
    print(f'  输入: {input_md}')
    print(f'  素材: {materials_path}')

    with open(input_md, 'r', encoding='utf-8') as f:
        content = f.read()

    all_logs = []

    # 1. 清除 internal pipeline 话术
    print('[1/8] 清除内部话术...')
    content, log = remove_internal_pipeline_footer(content)
    all_logs.extend(log)

    # 2. 最小结构只读检查
    print('[2/4] 最小结构检查...')
    _, log = validate_hk_us_minimum_structure(content, market)
    all_logs.extend(log)

    # 3. 参考资料时效性只读检查
    print('[3/4] 参考资料时效性...')
    content, recency_issues = check_reference_recency(content)
    all_logs.extend(recency_issues)

    # 4. 写入 mechanical-only 输出
    print('[4/4] 写入 mechanical-only 输出...')

    # 写入修复后文件
    with open(output_md, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'  输出: {output_md}')

    # 输出日志
    try:
        print(f'\n  修复日志 ({len(all_logs)}条):')
        for log_item in all_logs:
            try: print(f'    {log_item}')
            except UnicodeEncodeError: print(f'    [log item skipped - encoding]')
    except UnicodeEncodeError:
        print(f'  修复日志 ({len(all_logs)}条) - encoding fallback')


def main():
    parser = argparse.ArgumentParser(description='港美股报告生成后修复')
    parser.add_argument('--input-md', required=True, help='输入 MD 路径')
    parser.add_argument('--source-trace', required=True, help='source_trace.json 路径')
    parser.add_argument(
        '--materials', default=None,
        help='materials.json 路径（用于匹配真实材料 ID，与 --id-audit 互斥；至少提供一个）')
    parser.add_argument(
        '--id-audit', default=None,
        help='id_audit.json 路径（用于 ID 审计查找表，与 --materials 互斥；至少提供一个）')
    parser.add_argument('--output-md', required=True, help='输出修复后 MD 路径')
    parser.add_argument('--market', required=True, choices=['hk', 'us', 'HK', 'US'],
                        help='市场类型')
    parser.add_argument(
        '--min-structure-check', action='store_true',
        help='仅执行最小结构检查（跳过 LLM 修复步骤）')
    args = parser.parse_args()

    materials_path = args.materials or args.id_audit
    if not materials_path:
        parser.error('至少提供 --materials 或 --id-audit 中的一个')

    repair_report(args.input_md, args.source_trace, materials_path,
                  args.output_md, args.market.lower())


if __name__ == '__main__':
    main()
