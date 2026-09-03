#!/usr/bin/env python3
"""Build an A-share-style DOCX from a one-pager Markdown report."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


BLUE = RGBColor(31, 58, 95)
MID_BLUE = RGBColor(44, 88, 124)
TEXT = RGBColor(31, 31, 31)
TABLE_FILL = "D9EAF7"
GRID = "A6B8C8"


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_width(cell, width_dxa: int) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def set_table_autofit_to_window(table) -> None:
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), "5000")
    tbl_w.set(qn("w:type"), "pct")

    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "autofit")

    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), "0")
    tbl_ind.set(qn("w:type"), "dxa")


def remove_table_grid(table) -> None:
    old_grid = table._tbl.find(qn("w:tblGrid"))
    if old_grid is not None:
        table._tbl.remove(old_grid)
    for tc_pr in table._tbl.iter(qn("w:tcPr")):
        for tc_w in tc_pr.findall(qn("w:tcW")):
            tc_pr.remove(tc_w)


def set_table_borders(table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = "w:" + edge
        el = borders.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            borders.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), GRID)


def clean_inline(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def add_runs(paragraph, text: str, font_size: float = 10.5, bold_default: bool = False) -> None:
    pattern = re.compile(r"(\[[0-9,\]\[]+\]|\*\*.*?\*\*)")
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            run = paragraph.add_run(clean_inline(text[pos:match.start()]))
            style_run(run, font_size, bold_default)
        token = match.group(0)
        if token.startswith("["):
            run = paragraph.add_run(token)
            style_run(run, font_size, False, MID_BLUE)
        elif token.startswith("**"):
            run = paragraph.add_run(clean_inline(token))
            style_run(run, font_size, True)
        pos = match.end()
    if pos < len(text):
        run = paragraph.add_run(clean_inline(text[pos:]))
        style_run(run, font_size, bold_default)


def style_run(run, font_size: float, bold: bool = False, color: RGBColor = TEXT) -> None:
    run.bold = bold
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    run.font.size = Pt(font_size)
    run.font.color.rgb = color


def style_paragraph(paragraph, before: float = 0, after: float = 4, line: float = 1.08) -> None:
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line


def add_table(doc: Document, rows: list[list[str]], merge_first_col: bool = False) -> None:
    if not rows:
        return
    cols = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    set_table_borders(table)
    set_table_autofit_to_window(table)
    # 实际可用宽度：8.5 - 0.83*2 = 6.84 英寸 ≈ 9850 dxa，留 50 dxa 边距
    usable_width = 9800
    if cols <= 3:
        # 3列以内均匀分布
        widths = [usable_width // cols] * cols
    elif cols <= 6:
        # 4-6列：首列略窄（指标名），其余均匀
        widths = [int(usable_width * 0.18)]  # 首列 18%
        middle = (usable_width - widths[0]) // (cols - 1)
        widths += [middle] * (cols - 1)
    else:
        # 7列以上：首列指标名 15%，末列评述类 18%，中间均匀
        first_w = int(usable_width * 0.15)
        last_w  = int(usable_width * 0.18)
        mid_w   = (usable_width - first_w - last_w) // (cols - 2)
        widths = [first_w] + [mid_w] * (cols - 2) + [last_w]

    for r_idx, row in enumerate(rows):
        for c_idx in range(cols):
            cell = table.cell(r_idx, c_idx)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if r_idx == 0:
                set_cell_shading(cell, TABLE_FILL)
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if r_idx == 0 else WD_ALIGN_PARAGRAPH.LEFT
            style_paragraph(paragraph, after=1, line=1.0)
            add_runs(paragraph, clean_inline(row[c_idx]) if c_idx < len(row) else "", 9.5, r_idx == 0)

    # 合并列0中连续相同的单元格（数据行，跳过表头）
    if merge_first_col and len(rows) > 2:
        # 第1步：收集所有待合并区间（从 rows 解析，不受表格操作影响）
        merge_ranges: list[tuple[int, int]] = []  # (start_row, end_row) 在 rows 中的索引
        scan_start = 1  # 数据行起始（跳过表头）
        while scan_start < len(rows):
            scan_end = scan_start
            start_text = clean_inline(rows[scan_start][0]) if len(rows[scan_start]) > 0 else ""
            while scan_end + 1 < len(rows):
                next_text = clean_inline(rows[scan_end + 1][0]) if len(rows[scan_end + 1]) > 0 else ""
                if next_text == start_text and start_text != "":
                    scan_end += 1
                else:
                    break
            if scan_end > scan_start:
                merge_ranges.append((scan_start, scan_end))
            scan_start = scan_end + 1

        # 第2步：直接用 XML 设置 vMerge（避免 python-docx merge() 的索引漂移）
        for ms, me in merge_ranges:
            # 顶部单元格：vMerge restart，承载合并后的文本
            top_cell = table.cell(ms, 0)
            top_tc_pr = top_cell._tc.get_or_add_tcPr()
            for old_vm in top_tc_pr.findall(qn("w:vMerge")):
                top_tc_pr.remove(old_vm)
            vm_restart = OxmlElement("w:vMerge")
            vm_restart.set(qn("w:val"), "restart")
            top_tc_pr.append(vm_restart)

            # 清理顶部单元格：仅保留一个段落写入合并文本
            top_tc = top_cell._tc
            all_ps_top = top_tc.findall(qn("w:p"))
            for extra_p in all_ps_top[1:]:
                p = extra_p.getparent()
                if p is not None:
                    p.remove(extra_p)
            # 使用 python-docx Paragraph 对象操作第一个段落
            first_p = top_cell.paragraphs[0]
            first_p.clear()
            first_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            # 直接操作 pPr 确保左对齐生效
            jc = first_p._element.get_or_add_pPr().find(qn("w:jc"))
            if jc is None:
                jc = OxmlElement("w:jc")
                first_p._element.get_or_add_pPr().append(jc)
            jc.set(qn("w:val"), "left")
            style_paragraph(first_p, after=1, line=1.0)
            clean_text = clean_inline(rows[ms][0]) if len(rows[ms]) > 0 else ""
            add_runs(first_p, clean_text, 9.5, False)

            # 后续单元格：vMerge continue，清空内容避免视觉残留
            for r in range(ms + 1, me + 1):
                cell_below = table.cell(r, 0)
                cell_tc_pr = cell_below._tc.get_or_add_tcPr()
                for old_vm in cell_tc_pr.findall(qn("w:vMerge")):
                    cell_tc_pr.remove(old_vm)
                vm_cont = OxmlElement("w:vMerge")
                cell_tc_pr.append(vm_cont)
                # 清空 CONTINUE 单元格的全部段落
                for bp in cell_below._tc.findall(qn("w:p")):
                    parent = bp.getparent()
                    if parent is not None:
                        parent.remove(bp)

    remove_table_grid(table)
    doc.add_paragraph()


def parse_table(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    rows = []
    i = start
    while i < len(lines) and lines[i].strip().startswith("|"):
        raw = lines[i].strip()
        cells = [c.strip() for c in raw.strip("|").split("|")]
        if not all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
            rows.append(cells)
        i += 1
    return rows, i


def build_docx(md_path: Path, out_path: Path) -> None:
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.71)
    section.bottom_margin = Inches(0.71)
    section.left_margin = Inches(0.83)
    section.right_margin = Inches(0.83)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    normal.font.size = Pt(10.5)

    lines = md_path.read_text(encoding="utf-8").splitlines()
    current_section = 0  # 追踪当前章节号（## N → N）
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("|"):
            rows, i = parse_table(lines, i)
            # 所有表格均启用首列自动合并：§6"类型"列、§11.2"指标"列等
            add_table(doc, rows, merge_first_col=True)
            continue
        if stripped.startswith("# "):
            current_section = 0
            p = doc.add_paragraph(style="Title")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            style_paragraph(p, before=0, after=4, line=1.0)
            add_runs(p, stripped[2:].strip(), 18, True)
            for run in p.runs:
                run.font.color.rgb = BLUE
            i += 1
            continue
        if stripped.startswith("## "):
            m = re.match(r"##\s+(\d+)", stripped)
            current_section = int(m.group(1)) if m else 0
            p = doc.add_paragraph(style="Heading 1")
            style_paragraph(p, before=8, after=4, line=1.0)
            add_runs(p, stripped[3:].strip(), 13.5, True)
            for run in p.runs:
                run.font.color.rgb = BLUE
            i += 1
            continue
        if stripped.startswith("### "):
            p = doc.add_paragraph(style="Heading 2")
            style_paragraph(p, before=5, after=3, line=1.0)
            add_runs(p, stripped[4:].strip(), 12, True)
            for run in p.runs:
                run.font.color.rgb = MID_BLUE
            i += 1
            continue
        if stripped.startswith(("- ", "* ", "• ")):
            p = doc.add_paragraph(style=None)
            p.paragraph_format.left_indent = Inches(0.18)
            p.paragraph_format.first_line_indent = Inches(-0.12)
            style_paragraph(p, after=2)
            add_runs(p, "• " + stripped[2:].strip(), 10.5)
            i += 1
            continue
        p = doc.add_paragraph()
        style_paragraph(p, after=3)
        add_runs(p, stripped, 10.5)
        i += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a styled DOCX from one-pager Markdown.")
    parser.add_argument("markdown", help="Input Markdown report")
    parser.add_argument("--output", help="Output DOCX path")
    args = parser.parse_args()

    md_path = Path(args.markdown)
    out_path = Path(args.output) if args.output else md_path.with_suffix(".docx")
    build_docx(md_path, out_path)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
