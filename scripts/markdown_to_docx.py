#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司一页纸 Markdown → Word 转换脚本
将Markdown格式的报告转换为符合规范的.docx文件

使用方式：
    python markdown_to_docx.py <input.md> <output.docx>
    python markdown_to_docx.py <input.md>   # 自动生成同名.docx

格式规范（参考样例文档）：
- 字体：微软雅黑（Windows）/ PingFang SC（macOS）/ Noto Sans CJK SC（Linux）/ Calibri（英文）
- 标题1：13.5pt，颜色 #1F3A5F，加粗
- 标题2：12pt，颜色 #2C587C
- 标题3：11pt，颜色 #44546A
- 正文：10.5pt，颜色 #1F1F1F，行距1.2倍
- 表格：Table Grid样式，表头填充 #D9EAF7，奇行白色，偶行 #F7FBFF，9.5pt
- 无目录页
"""

import sys
import re
import os
import io
import platform
import urllib.request
import urllib.parse
import urllib.error
import tempfile
import ssl
from pathlib import Path

# Windows 控制台 GBK 编码兼容：强制 stdout/stderr 输出 UTF-8
if sys.platform == "win32":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor, Cm, Twips
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
    from docx.oxml.ns import qn, nsmap
    from docx.oxml import OxmlElement
    import docx
except ImportError:
    import subprocess
    print("python-docx not found, installing...", file=sys.stderr)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "python-docx", "-q"],
            stdout=subprocess.DEVNULL
        )
        from docx import Document
        from docx.shared import Pt, Inches, RGBColor, Cm, Twips
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
        from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
        from docx.oxml.ns import qn, nsmap
        from docx.oxml import OxmlElement
        import docx
        print("python-docx installed successfully.", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Failed to install python-docx: {e}", file=sys.stderr)
        print("Please run manually: pip install python-docx", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 颜色常量
# ─────────────────────────────────────────────────────────────────────────────
COLOR_TITLE      = RGBColor(0x1F, 0x1F, 0x1F)
COLOR_H1         = RGBColor(0x1F, 0x3A, 0x5F)
COLOR_H2         = RGBColor(0x2C, 0x58, 0x7C)
COLOR_H3         = RGBColor(0x44, 0x54, 0x6A)
COLOR_BODY       = RGBColor(0x1F, 0x1F, 0x1F)
COLOR_TBL_HEADER = "D9EAF7"   # 表头填充色
COLOR_TBL_ALT    = "F7FBFF"   # 偶数行填充色
COLOR_TBL_BORDER = "2C587C"   # 表格边框色

FONT_EN  = "Calibri"

# 中文字体：按平台选择（优先使用各平台原生字体）
_os = platform.system()
if _os == "Windows":
    FONT_CN = "微软雅黑"
elif _os == "Darwin":   # macOS
    FONT_CN = "PingFang SC"
else:                    # Linux / 其他
    FONT_CN = "Noto Sans CJK SC"

LINE_SPACING = 1.2   # 全局行距


# ─────────────────────────────────────────────────────────────────────────────
# Word XML 辅助函数
# ─────────────────────────────────────────────────────────────────────────────
def set_cell_background(cell, hex_color: str):
    """设置单元格背景色（十六进制，不含#）"""
    tc = cell._tc
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = OxmlElement("w:tcPr")
        tc.insert(0, tcPr)
    shd = tcPr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tcPr.append(shd)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color.upper())


def set_table_borders(table, color: str = COLOR_TBL_BORDER, sz: str = "4"):
    """设置表格所有边框"""
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblBorders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{side}")
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), sz)
        border.set(qn("w:space"), "0")
        border.set(qn("w:color"), color)
        tblBorders.append(border)
    existing = tblPr.find(qn("w:tblBorders"))
    if existing is not None:
        tblPr.remove(existing)
    tblPr.append(tblBorders)


def set_cell_width(cell, width_dxa: int):
    """设置单元格列宽（dxa单位，1 inch = 1440 dxa）"""
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def add_superscript_run(para, text: str, size_pt: float = 7.0, color: RGBColor = COLOR_H3):
    """在段落中添加上标文字（用于角标引用）"""
    run = para.add_run(text)
    run.font.size = Pt(size_pt)
    run.font.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    # 设置上标
    vertAlign = OxmlElement("w:vertAlign")
    vertAlign.set(qn("w:val"), "superscript")
    rPr.append(vertAlign)
    return run


def set_run_font(run, size_pt=None, bold=None, color=None, italic=None):
    """统一设置run的字体属性"""
    run.font.name = FONT_EN
    # 设置东亚字体
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), FONT_EN)
    rFonts.set(qn("w:eastAsia"), FONT_CN)
    rFonts.set(qn("w:hAnsi"), FONT_EN)
    rFonts.set(qn("w:cs"), FONT_CN)

    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    if italic is not None:
        run.italic = italic


def set_para_spacing(para, before_pt=None, after_pt=None, line_spacing=LINE_SPACING):
    """设置段落间距"""
    fmt = para.paragraph_format
    if before_pt is not None:
        fmt.space_before = Pt(before_pt)
    if after_pt is not None:
        fmt.space_after = Pt(after_pt)
    fmt.line_spacing = line_spacing
    fmt.line_spacing_rule = WD_LINE_SPACING.MULTIPLE


# ─────────────────────────────────────────────────────────────────────────────
# 文档样式配置
# ─────────────────────────────────────────────────────────────────────────────
def setup_styles(doc: Document):
    """配置文档基础样式"""
    styles = doc.styles

    # Normal 正文
    normal = styles["Normal"]
    normal.font.name = FONT_EN
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = COLOR_BODY
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = LINE_SPACING
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    nf = normal.element.get_or_add_rPr()
    rFonts = nf.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        nf.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), FONT_CN)

    # Heading 1
    try:
        h1 = styles["Heading 1"]
    except KeyError:
        h1 = styles.add_style("Heading 1", docx.enum.style.WD_STYLE_TYPE.PARAGRAPH)
    h1.font.name = FONT_EN
    h1.font.size = Pt(13.5)
    h1.font.bold = True
    h1.font.color.rgb = COLOR_H1
    h1.paragraph_format.space_before = Pt(10)
    h1.paragraph_format.space_after = Pt(6)
    h1.paragraph_format.line_spacing = LINE_SPACING
    h1.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf = h1.element.get_or_add_rPr()
    rf = pf.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        pf.insert(0, rf)
    rf.set(qn("w:eastAsia"), FONT_CN)

    # Heading 2
    try:
        h2 = styles["Heading 2"]
    except KeyError:
        h2 = styles.add_style("Heading 2", docx.enum.style.WD_STYLE_TYPE.PARAGRAPH)
    h2.font.name = FONT_EN
    h2.font.size = Pt(12)
    h2.font.bold = True
    h2.font.color.rgb = COLOR_H2
    h2.paragraph_format.space_before = Pt(6)
    h2.paragraph_format.space_after = Pt(4)
    h2.paragraph_format.line_spacing = LINE_SPACING
    h2.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf2 = h2.element.get_or_add_rPr()
    rf2 = pf2.find(qn("w:rFonts"))
    if rf2 is None:
        rf2 = OxmlElement("w:rFonts")
        pf2.insert(0, rf2)
    rf2.set(qn("w:eastAsia"), FONT_CN)

    # Heading 3
    try:
        h3 = styles["Heading 3"]
    except KeyError:
        h3 = styles.add_style("Heading 3", docx.enum.style.WD_STYLE_TYPE.PARAGRAPH)
    h3.font.name = FONT_EN
    h3.font.size = Pt(11)
    h3.font.bold = True
    h3.font.color.rgb = COLOR_H3
    h3.paragraph_format.space_before = Pt(4)
    h3.paragraph_format.space_after = Pt(3)
    h3.paragraph_format.line_spacing = LINE_SPACING
    h3.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf3 = h3.element.get_or_add_rPr()
    rf3 = pf3.find(qn("w:rFonts"))
    if rf3 is None:
        rf3 = OxmlElement("w:rFonts")
        pf3.insert(0, rf3)
    rf3.set(qn("w:eastAsia"), FONT_CN)


def setup_page(doc: Document):
    """配置页面设置"""
    section = doc.sections[0]
    section.page_width  = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin   = Inches(0.83)
    section.right_margin  = Inches(0.83)
    section.top_margin    = Inches(0.71)
    section.bottom_margin = Inches(0.71)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown 解析与内容添加
# ─────────────────────────────────────────────────────────────────────────────
def add_inline_text(run_adder, text: str, base_size_pt: float, base_color: RGBColor,
                    base_bold: bool = False, para=None):
    """
    解析行内的 **bold**、*italic*、`code` 以及 [^N] 角标标记，添加多个run。
    run_adder: 函数，接收 (text, bold, italic) → 返回一个run
    para: 若提供，用于添加上标run
    """
    pattern = re.compile(r'\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|\[\^(\d+)\]')
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            r = run_adder(text[last:m.start()], base_bold, False)
            set_run_font(r, base_size_pt, base_bold, base_color)
        if m.group(1):  # **bold**
            r = run_adder(m.group(1), True, False)
            set_run_font(r, base_size_pt, True, base_color)
        elif m.group(2):  # *italic*
            r = run_adder(m.group(2), base_bold, True)
            set_run_font(r, base_size_pt, base_bold, base_color, italic=True)
        elif m.group(3):  # `code`
            r = run_adder(m.group(3), False, False)
            set_run_font(r, base_size_pt - 0.5, False, RGBColor(0x44, 0x54, 0x6A))
        elif m.group(4) and para is not None:  # [^N] 角标
            add_superscript_run(para, f"[{m.group(4)}]")
        last = m.end()
    if last < len(text):
        r = run_adder(text[last:], base_bold, False)
        set_run_font(r, base_size_pt, base_bold, base_color)


def add_paragraph_with_inline(doc: Document, text: str, style_name: str = "Normal",
                               size_pt: float = 10.5, color: RGBColor = COLOR_BODY,
                               bold: bool = False, before_pt=None, after_pt=None):
    """添加带有行内格式的段落"""
    para = doc.add_paragraph(style=style_name)
    set_para_spacing(para, before_pt, after_pt if after_pt is not None else 4)

    def run_adder(t, b, i):
        r = para.add_run(t)
        return r

    add_inline_text(run_adder, text, size_pt, color, bold, para=para)
    return para


def parse_md_table(lines: list) -> tuple:
    """解析Markdown表格，返回 (headers, rows)"""
    headers = []
    rows = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if i == 0:
            headers = cells
        elif re.match(r'^[\s|:-]+$', line):
            continue  # 分隔行
        else:
            rows.append(cells)
    return headers, rows


def add_table_to_doc(doc: Document, headers: list, rows: list):
    """添加格式化表格（样例文档风格）"""
    col_count = max(len(headers), max((len(r) for r in rows), default=0))
    if col_count == 0:
        return

    # 补齐列数
    headers = headers + [''] * (col_count - len(headers))
    rows = [r + [''] * (col_count - len(r)) for r in rows]

    table = doc.add_table(rows=1 + len(rows), cols=col_count)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True

    # 设置边框
    set_table_borders(table, "2C587C", "4")

    # ── 列宽分配（避免 Word 自动挤压窄列）──────────────────────────────
    # 实际可用宽度：8.5 - 0.83*2 = 6.84 英寸 ≈ 9850 dxa，留 50 dxa
    usable_w = 9800
    if col_count <= 3:
        col_widths = [usable_w // col_count] * col_count
    elif col_count <= 6:
        col_widths = [int(usable_w * 0.18)]  # 首列 18%
        mid = (usable_w - col_widths[0]) // (col_count - 1)
        col_widths += [mid] * (col_count - 1)
    else:
        first_w = int(usable_w * 0.15)
        last_w  = int(usable_w * 0.18)
        mid_w   = (usable_w - first_w - last_w) // (col_count - 2)
        col_widths = [first_w] + [mid_w] * (col_count - 2) + [last_w]

    # 表头行
    header_row = table.rows[0]
    for j, hdr in enumerate(headers):
        cell = header_row.cells[j]
        set_cell_width(cell, col_widths[j])
        set_cell_background(cell, COLOR_TBL_HEADER)
        para = cell.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_para_spacing(para, 2, 2, 1.2)
        run = para.add_run(hdr.strip())
        set_run_font(run, 10, bold=True, color=COLOR_BODY)

    # 数据行（奇偶行交替背景）
    for ri, row in enumerate(rows):
        tr = table.rows[ri + 1]
        fill = "FFFFFF" if ri % 2 == 0 else COLOR_TBL_ALT
        for j, cell_text in enumerate(row):
            cell = tr.cells[j]
            set_cell_width(cell, col_widths[j])
            if fill != "FFFFFF":
                set_cell_background(cell, fill)
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.LEFT
            set_para_spacing(para, 2, 2, 1.2)
            def run_adder(t, b, i):
                r = para.add_run(t)
                return r
            add_inline_text(run_adder, cell_text.strip(), 10, COLOR_BODY, False, para=para)

    # 在表格后添加空行
    doc.add_paragraph().paragraph_format.space_after = Pt(6)


def try_insert_image(doc: Document, src: str, alt: str = ""):
    """
    尝试插入图片。src 可以是：
    - 本地文件路径
    - http/https URL（自动下载到临时文件）
    - base64 data URI（data:image/png;base64,xxx）
    插入失败时回退为文字说明。
    """
    tmp_path = None
    try:
        if src.startswith("data:image"):
            # Base64 data URI
            import base64
            header, b64data = src.split(",", 1)
            img_bytes = base64.b64decode(b64data)
            suffix = ".png"
            if "jpeg" in header or "jpg" in header:
                suffix = ".jpg"
            elif "gif" in header:
                suffix = ".gif"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(img_bytes)
            tmp.close()
            tmp_path = tmp.name
            img_path = tmp_path
        elif src.startswith("http://") or src.startswith("https://"):
            # 远程 URL — 域名白名单校验
            _ALLOWED_IMG_HOSTS = {"gw.datayes.com", "datayes.com", "api.datayes.com", "static.wmcloud.com", "api.wmcloud.com"}
            if urllib.parse.urlparse(src).hostname not in _ALLOWED_IMG_HOSTS:
                _insert_image_placeholder(doc, alt or src)
                return
            parsed = urllib.parse.urlparse(src)
            suffix = os.path.splitext(parsed.path)[1].lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                suffix = ".png"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.close()
            tmp_path = tmp.name
            req = urllib.request.Request(
                src,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; datayes-onepager-docx/1.0)",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    img_bytes = resp.read()
            except (ssl.SSLCertVerificationError, urllib.error.URLError) as e:
                reason = getattr(e, "reason", e)
                if not isinstance(reason, ssl.SSLCertVerificationError):
                    raise
                # Some corporate/internal chart hosts present a chain that the
                # bundled Python cert store cannot validate, while browser/curl
                # access succeeds through the system trust store. Retry image
                # download only with a relaxed context so the report still embeds
                # generated charts instead of silently falling back to text.
                print(f"WARNING: retrying image download without certificate verification: {src}", file=sys.stderr)
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    img_bytes = resp.read()
            if not img_bytes:
                raise RuntimeError(f"empty image response: {src}")
            with open(tmp_path, "wb") as f:
                f.write(img_bytes)
            img_path = tmp_path
        else:
            img_path = src

        if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            # 插入图片，宽度限制在页面内容宽度以内（约6英寸）
            para = doc.add_paragraph(style="Normal")
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_para_spacing(para, 4, 4)
            run = para.add_run()
            run.add_picture(img_path, width=Inches(5.5))
            # 图注
            if alt:
                cap_para = doc.add_paragraph(style="Normal")
                cap_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                set_para_spacing(cap_para, 0, 8)
                cap_run = cap_para.add_run(alt)
                set_run_font(cap_run, 9.0, False, COLOR_H3, italic=True)
        else:
            _insert_image_placeholder(doc, alt or src)
    except Exception as e:
        print(f"WARNING: image insert failed: {src} ({e})", file=sys.stderr)
        _insert_image_placeholder(doc, alt or src)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _insert_image_placeholder(doc: Document, label: str):
    """图片插入失败时的占位文字"""
    para = doc.add_paragraph(style="Normal")
    set_para_spacing(para, 2, 4)
    run = para.add_run(f"[图表：{label}]")
    set_run_font(run, 9.5, False, COLOR_H3, italic=True)


# ─────────────────────────────────────────────────────────────────────────────
# 主转换函数
# ─────────────────────────────────────────────────────────────────────────────
def convert_markdown_to_docx(md_content: str, output_path: str):
    doc = Document()
    setup_page(doc)
    setup_styles(doc)

    lines = md_content.split('\n')
    i = 0

    # 不插入目录，直接处理正文
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # ── 空行 ──
        if not stripped:
            i += 1
            continue

        # ── 水平线 ──
        if re.match(r'^-{3,}$|^\*{3,}$|^_{3,}$', stripped):
            i += 1
            continue

        # ── 标题行 ──
        heading_match = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if heading_match:
            level = len(heading_match.group(1))
            title_text = heading_match.group(2).strip()
            # 去掉末尾的⭐符号
            title_text = re.sub(r'\s*⭐+\s*.*$', '', title_text).strip()
            title_text = re.sub(r'\s*（最重要[^）]*）', '', title_text).strip()

            if level == 1:
                para = doc.add_paragraph(style="Heading 1")
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                set_para_spacing(para, 0, 12)
                run = para.add_run(title_text)
                set_run_font(run, 16, bold=True, color=COLOR_TITLE)
            elif level == 2:
                para = doc.add_paragraph(style="Heading 1")
                set_para_spacing(para, 10, 6)
                run = para.add_run(title_text)
                set_run_font(run, 13.5, bold=True, color=COLOR_H1)
            elif level == 3:
                para = doc.add_paragraph(style="Heading 2")
                set_para_spacing(para, 6, 4)
                run = para.add_run(title_text)
                set_run_font(run, 12, bold=True, color=COLOR_H2)
            else:
                para = doc.add_paragraph(style="Heading 3")
                set_para_spacing(para, 4, 3)
                run = para.add_run(title_text)
                set_run_font(run, 11, bold=True, color=COLOR_H3)
            i += 1
            continue

        # ── Markdown 表格 ──
        if '|' in stripped and stripped.strip().startswith('|'):
            table_lines = []
            while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
                table_lines.append(lines[i])
                i += 1
            if table_lines:
                headers, rows = parse_md_table(table_lines)
                add_table_to_doc(doc, headers, rows)
            continue

        # ── 图片 ![alt](src) ──
        img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$', stripped)
        if img_match:
            alt_text = img_match.group(1)
            src = img_match.group(2).strip()
            try_insert_image(doc, src, alt_text)
            i += 1
            continue

        # ── 引用块 (>) ──
        if stripped.startswith('>'):
            quote_text = stripped.lstrip('> ').strip()
            para = doc.add_paragraph(style="Normal")
            para.paragraph_format.left_indent = Inches(0.3)
            set_para_spacing(para, 2, 2)
            pPr = para._p.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            left_bdr = OxmlElement("w:left")
            left_bdr.set(qn("w:val"), "single")
            left_bdr.set(qn("w:sz"), "8")
            left_bdr.set(qn("w:space"), "4")
            left_bdr.set(qn("w:color"), "2C587C")
            pBdr.append(left_bdr)
            pPr.append(pBdr)
            def run_adder(t, b, itl):
                r = para.add_run(t)
                return r
            add_inline_text(run_adder, quote_text, 10.5, COLOR_H3, False, para=para)
            i += 1
            continue

        # ── 脚注定义行 [^N]: 文字 ──
        fn_def_match = re.match(r'^\[\^(\d+)\]:\s*(.*)', stripped)
        if fn_def_match:
            fn_num = fn_def_match.group(1)
            fn_text = fn_def_match.group(2)
            para = doc.add_paragraph(style="Normal")
            para.paragraph_format.left_indent = Inches(0.25)
            set_para_spacing(para, 0, 2)
            add_superscript_run(para, f"[{fn_num}]")
            r = para.add_run(f" {fn_text}")
            set_run_font(r, 9.0, False, COLOR_H3)
            i += 1
            continue

        # ── 1) 或 1） 风格有序列表 ──
        paren_ol_match = re.match(r'^(\s*)(\d+)[)）]\s+(.*)', stripped)
        if paren_ol_match:
            indent_level = len(paren_ol_match.group(1)) // 2
            num = paren_ol_match.group(2)
            text = paren_ol_match.group(3)
            para = doc.add_paragraph(style="Normal")
            para.paragraph_format.left_indent = Inches(0.2 + indent_level * 0.25)
            set_para_spacing(para, 1, 2)
            def run_adder(t, b, itl):
                r = para.add_run(t)
                return r
            add_inline_text(run_adder, f"{num}) {text}", 10.5, COLOR_BODY, False, para=para)
            i += 1
            continue

        # ── 有序列表 1. ──
        ol_match = re.match(r'^(\s*)(\d+)\.\s+(.*)', stripped)
        if ol_match:
            indent_level = len(ol_match.group(1)) // 2
            num = ol_match.group(2)
            text = ol_match.group(3)
            para = doc.add_paragraph(style="Normal")
            para.paragraph_format.left_indent = Inches(0.2 + indent_level * 0.25)
            set_para_spacing(para, 1, 2)
            def run_adder(t, b, itl):
                r = para.add_run(t)
                return r
            add_inline_text(run_adder, f"{num}. {text}", 10.5, COLOR_BODY, False, para=para)
            i += 1
            continue

        # ── 无序列表 ──
        ul_match = re.match(r'^(\s*)[-*+]\s+(.*)', stripped)
        if ul_match:
            indent_level = len(ul_match.group(1)) // 2
            text = ul_match.group(2)
            para = doc.add_paragraph(style="Normal")
            para.paragraph_format.left_indent = Inches(0.2 + indent_level * 0.25)
            set_para_spacing(para, 1, 2)
            def run_adder(t, b, itl):
                r = para.add_run(t)
                return r
            bullet_text = "• " + text
            add_inline_text(run_adder, bullet_text, 10.5, COLOR_BODY, False, para=para)
            i += 1
            continue

        # ── 普通段落 ──
        para = doc.add_paragraph(style="Normal")
        set_para_spacing(para, 2, 4)
        def run_adder(t, b, itl):
            r = para.add_run(t)
            return r
        add_inline_text(run_adder, stripped, 10.5, COLOR_BODY, False, para=para)
        i += 1

    # 保存
    doc.save(output_path)
    print(f"Word文档已生成：{output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python markdown_to_docx.py <input.md> [output.docx]")
        sys.exit(1)

    input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        output_path = str(Path(input_path).with_suffix('.docx'))

    with open(input_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    convert_markdown_to_docx(md_content, output_path)
