#!/usr/bin/env python3
"""v1.2.3 最终 Markdown 清理——A股/港股/美股统一执行。不依赖 LLM，纯后处理。
用法: python clean_final_markdown_v123.py <input.md> [--output <output.md>]
"""

import argparse, re, os

FORBIDDEN_TERMS = [
    "skipped_with_reason", "pipeline", "自检", "评测记录", "接口不可用",
    "HK PIT", "本市场不设", "美股不设", "同业数据优先从", "如无法获取",
    "需补充", "检查项", "本报告应", "数据优先", "LLM环节", "兜底模板",
    "请在.*后移除",
]


def clean_markdown(content: str) -> str:
    # 1. Strip tree symbols
    content = content.replace("└", "").replace("├", "").replace("│", "")
    content = re.sub(r'─{2,}', '', content)  # long dash sequences from tree display

    # 2. Strip template decorators
    content = re.sub(r'[⭐🌟🔥✅❌]+', '', content)

    # 3. Strip HTML tags
    content = re.sub(r'<br\s*/?>', '\n', content)
    content = re.sub(r'<[^>]+>', '', content)

    # 4. Remove engineering language lines
    for term in FORBIDDEN_TERMS:
        # Remove entire lines containing forbidden terms
        lines = content.split('\n')
        lines = [l for l in lines if not re.search(term, l)]
        content = '\n'.join(lines)

    # 5. Fix isolated single-star wrapping (converts *whole sentence* to plain text)
    # Only affects lines where the entire content (not starting with |) is wrapped in single *
    lines = content.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Skip table rows, bullets, headers
        if stripped.startswith('|') or stripped.startswith('* ') or stripped.startswith('- ') or stripped.startswith('#'):
            cleaned.append(line)
            continue
        # Detect whole-line single-* wrapping (not **bold**)
        m = re.match(r'^\*([^*\n]{20,})\*$', stripped)
        if m and '**' not in m.group(1):
            # Convert to plain text (remove the * wrapper)
            cleaned.append(m.group(1).strip())
        else:
            cleaned.append(line)
    content = '\n'.join(cleaned)

    # 6. Remove consecutive blank lines (max 2)
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v1.2.3 final MD cleaner")
    parser.add_argument("input", help="Input Markdown path")
    parser.add_argument("--output", "-o", help="Output path (overwrites input if omitted)")
    args = parser.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        content = f.read()

    cleaned = clean_markdown(content)

    out_path = args.output or args.input
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(cleaned)

    print(f"Cleaned: {args.input} -> {out_path}")
