# -*- coding: utf-8 -*-
"""
gen_charts.py — 独立图表生成脚本
==================================
从已有的 fetch_data.py 输出 JSON 文件中读取财务数据，
调用 data_txt_to_chart 接口生成四张 ECharts 图表，
并将图片 URL 写回 JSON 的 charts 字段。

用法:
    python -X utf8 gen_charts.py --input ./600030_data.json --token <TOKEN>
    python -X utf8 gen_charts.py --input ./600030_data.json           # 自动读取 ~/token.txt
    python -X utf8 gen_charts.py --input ./600030_data.json --output ./updated.json

图表说明:
    revenue   — 营业收入及同比增速（柱状图+折线图）
    profit    — 扣非净利润及同比增速（柱状图+折线图）
    margin    — 分业务毛利率（分组柱状图，仅年报）
    structure — 历年收入结构（堆叠柱状图，仅年报）

期间选取规则:
    revenue / profit  — 近3年年报 + 最新一期季报/半年报（如有）
    margin / structure — 近3年年报（这两接口本身只有年报数据）
"""

import argparse
import json
import os
import sys
import time

# Windows 控制台 GBK 编码兼容：强制 stdout/stderr 输出 UTF-8
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "-q"], check=True)
    import requests

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
META_BASE = "https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api"

ALLOWED_HOSTS = {"gw.datayes.com", "api.datayes.com", "api.wmcloud.com", "r.datayes.com"}


# ─────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────
def load_token(token_arg):
    """优先：环境变量 DATAYES_TOKEN → 命令行参数 → ~/token.txt → .datayes_token"""
    if os.environ.get("DATAYES_TOKEN"):
        return os.environ["DATAYES_TOKEN"].strip()
    if token_arg:
        return token_arg.strip()
    candidates = [
        os.path.expanduser("~/token.txt"),
        os.path.join(os.environ.get("USERPROFILE", ""), "token.txt"),
        os.path.expanduser("~/.datayes_token"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                tok = f.read().strip()
                if tok:
                    return tok
    return None


def make_headers(token, json_body=False):
    h = {"Authorization": f"Bearer {token}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def get_chart_url(token):
    """从元信息接口获取 data_txt_to_chart 的 URL；失败则返回 None"""
    try:
        r = requests.get(META_BASE, params={"nameEn": "data_txt_to_chart"},
                         headers=make_headers(token), timeout=15)
        rj = r.json()
        url = (rj.get("data") or {}).get("url") or ""
        if url:
            from urllib.parse import urlparse
            if urlparse(url).hostname not in ALLOWED_HOSTS:
                print(f"  ⚠️ 元信息返回非白名单域名: {urlparse(url).hostname}", flush=True)
                return None
            return url
    except Exception as e:
        print(f"  ⚠️ 获取 data_txt_to_chart URL 失败: {e}", flush=True)
    return None


# ─────────────────────────────────────────────
# 期间选取 & 标签
# ─────────────────────────────────────────────
def _select_periods(lst, n=3, include_latest_quarter=False):
    """
    取最近 n 个年报 (reportType==A)。
    先按 year 升序排列，确保不受接口返回顺序影响。
    若 include_latest_quarter=True 且排序后末尾有更新的非年报期，则追加该期。
    """
    all_rows = sorted(lst or [], key=lambda x: str(x.get("year", "")))
    annual = [x for x in all_rows if x.get("reportType") == "A"]
    selected = annual[-n:]
    if include_latest_quarter and all_rows:
        last = all_rows[-1]
        if last.get("reportType") != "A":
            selected = selected + [last]
    return selected


def _period_label(x):
    rt = x.get("reportType", "")
    yr = x.get("year", "")
    if rt == "A":
        return f"{yr}年报"
    if rt in ("Q1", "CQ1"):
        return f"{yr}Q1"
    if rt in ("Q3", "CQ3"):
        return f"{yr}前三季"
    if rt in ("S1", "H1"):
        return f"{yr}中报"
    return f"{yr}{rt}"


# ─────────────────────────────────────────────
# 四张图文本构建
# ─────────────────────────────────────────────
def _text_revenue(d, title_prefix):
    rows = _select_periods(
        (d.get("fin_chart_revenue") or {}).get("data"),
        n=3, include_latest_quarter=True)
    if not rows:
        return None
    lines = "\n".join(
        f"  {_period_label(x)}: 营业收入={round(x['revenue']/1e8, 2)}亿元, "
        f"同比增速={round(x['revenueYOY'], 2)}%"
        for x in rows if x.get("revenue") is not None
    )
    return (f"图表标题: {title_prefix}:营业收入及增速\n"
            f"图表类型: 柱状图+折线图（左轴蓝色柱状图=营业收入单位亿元；右轴橙色折线图=同比增速单位%）\n"
            f"数据如下:\n{lines}\n"
            f"样式要求: 柱状图每柱顶部显示营业收入数值（保留1位小数，单位亿元）；折线每点标注增速数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
            f"数据来源: Datayes!")


def _text_profit(d, title_prefix):
    rows = _select_periods(
        (d.get("fin_chart_profit") or {}).get("data"),
        n=3, include_latest_quarter=True)
    if not rows:
        return None
    lines = "\n".join(
        f"  {_period_label(x)}: 扣非净利润={round(x['niAttrPCut']/1e8, 2)}亿元, "
        f"同比增速={round(x['niAttrPCutYOY'], 2)}%"
        for x in rows if x.get("niAttrPCut") is not None
    )
    return (f"图表标题: {title_prefix}:扣非净利润及增速\n"
            f"图表类型: 柱状图+折线图（左轴蓝色柱状图=扣非净利润单位亿元；右轴橙色折线图=同比增速单位%）\n"
            f"数据如下:\n{lines}\n"
            f"样式要求: 柱状图每柱顶部显示扣非净利润数值（保留1位小数，单位亿元）；折线每点标注增速数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
            f"数据来源: Datayes!")


def _text_margin(d, title_prefix):
    mg = d.get("fin_chart_margin") or {}
    datas = (mg.get("data") or {}).get("datas") or []
    rows = _select_periods(datas, n=3, include_latest_quarter=False)
    if not rows:
        return None
    lines = []
    for row in rows:
        lbl = _period_label(row)
        for item in (row.get("items") or []):
            name = item.get("itemName") or item.get("name") or "?"
            val = item.get("itemValue")
            if name and val is not None:
                lines.append(f"  {lbl} {name}: {round(val, 2)}%")
    if not lines:
        return None
    return (f"图表标题: {title_prefix}:历年毛利率（业务板块）\n"
            f"图表类型: 分组柱状图（X轴=年份，Y轴=毛利率%，按业务板块分色分组）\n"
            f"数据如下:\n" + "\n".join(lines) + "\n"
            f"样式要求: 每个柱子顶部显示毛利率数值（保留1位小数，加%）；标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
            f"数据来源: Datayes!")


def _text_structure(d, title_prefix):
    st = d.get("fin_chart_structure") or {}
    all_rows = st.get("data") if isinstance(st, dict) else st
    rows = _select_periods(all_rows, n=3, include_latest_quarter=False)
    if not rows:
        return None
    lines = []
    for row in sorted(rows, key=lambda x: x.get("year", "")):
        lbl = _period_label(row)
        for item in (row.get("items") or []):
            name = item.get("name") or "?"
            rev = item.get("revenue")
            if name and rev is not None:
                lines.append(f"{lbl},{name},{round(rev/1e8, 2)}亿元")
    if not lines:
        return None
    return (f"图表标题: {title_prefix}:历年收入结构\n"
            f"图表类型: 堆叠柱状图（X轴=年份，Y轴=亿元，各业务收入堆叠）\n"
            f"数据(格式:期间,业务,金额):\n" + "\n".join(lines) + "\n"
            f"样式要求: 堆叠柱中每层显示对应业务收入数值（保留1位小数，单位亿元）；占比过小或标签重叠时自动隐藏（hideOverlap: true），确保可见标签清晰可读。\n"
            f"数据来源: Datayes!")


# ─────────────────────────────────────────────
# 单张图生成（带重试）
# ─────────────────────────────────────────────
def gen_one(chart_url, token, key, text, max_retry=3):
    """调用 data_txt_to_chart，返回图片 URL 或 None"""
    if not text:
        print(f"  △ {key}: 无数据，跳过")
        return None
    from urllib.parse import urlparse
    if urlparse(chart_url).hostname not in ALLOWED_HOSTS:
        print(f"  ✗ {key}: 域名不在白名单: {urlparse(chart_url).hostname}")
        return None
    for attempt in range(1, max_retry + 1):
        try:
            r = requests.post(
                chart_url,
                json={"text": text},
                headers=make_headers(token, json_body=True),
                timeout=90,
            )
            rj = r.json()
            if rj.get("code") == 1 and rj.get("data"):
                img = rj["data"]
                url = str(img) if not isinstance(img, str) else img
                print(f"  ✓ {key}: {url}")
                return url
            print(f"  attempt {attempt}/{max_retry} failed: code={rj.get('code')} msg={rj.get('message')}")
        except Exception as e:
            print(f"  attempt {attempt}/{max_retry} exception: {e}")
        if attempt < max_retry:
            time.sleep(1)   # 仅失败时短暂等待，不在成功路径上sleep
    print(f"  ✗ {key}: 全部重试失败")
    return None


# ─────────────────────────────────────────────
# 主逻辑
# ─────────────────────────────────────────────
def generate_charts(data, token, chart_url):
    meta = data.get("__meta__") or {}
    ticker = meta.get("ticker", "")
    company_name = meta.get("name", "")
    title_prefix = f"{company_name}({ticker})" if company_name and ticker else (company_name or ticker or "股票")

    tasks = {
        "revenue":   _text_revenue(data, title_prefix),
        "profit":    _text_profit(data, title_prefix),
        "margin":    _text_margin(data, title_prefix),
        "structure": _text_structure(data, title_prefix),
    }

    # 检查哪些图有数据
    skipped = [k for k, v in tasks.items() if v is None]
    if skipped:
        print(f"  以下图表因原始数据为空，跳过: {skipped}")

    charts = {}
    # 四张图并发生成，互不依赖
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(gen_one, chart_url, token, k, t): k for k, t in tasks.items()}
        for fut in concurrent.futures.as_completed(futs):
            k = futs[fut]
            charts[k] = fut.result()

    return charts


def main():
    parser = argparse.ArgumentParser(description="gen_charts.py — 从 fetch_data.py 输出 JSON 生成四张图表")
    parser.add_argument("--input",  required=True, help="fetch_data.py 输出的 JSON 文件路径")
    parser.add_argument("--token",  default=None,  help="Datayes token（省略则自动读取 ~/token.txt）")
    parser.add_argument("--output", default=None,  help="写回路径（省略则覆盖 --input 文件）")
    args = parser.parse_args()

    # 读取 token
    token = load_token(args.token)
    if not token:
        print("错误: 未找到 token，请通过 --token 传入或在 ~/token.txt 中保存")
        sys.exit(1)

    # 读取 JSON
    input_path = args.input
    if not os.path.isfile(input_path):
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("__meta__") or {}
    print(f"\n{'='*60}")
    print(f"图表生成 | ticker={meta.get('ticker','')}  name={meta.get('name','')}")
    print(f"{'='*60}")

    # 获取图表接口 URL
    print("\n[1/2] 获取 data_txt_to_chart 接口 URL...")
    chart_url = get_chart_url(token)
    if not chart_url:
        print("错误: 无法获取 data_txt_to_chart 接口 URL，请检查 token 和网络")
        sys.exit(1)
    print(f"  URL: {chart_url}")

    # 生成图表
    print("\n[2/2] 并发生成图表（4张同时发起，失败自动重试3次）...")
    charts = generate_charts(data, token, chart_url)

    # 写回 JSON
    data["charts"] = charts
    output_path = args.output or input_path
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 汇总
    print(f"\n{'='*60}")
    print("生成结果:")
    labels = {"revenue": "营业收入及增速", "profit": "扣非净利润及增速",
              "margin": "分业务毛利率", "structure": "历年收入结构"}
    ok = 0
    for k, url in charts.items():
        status = "✅" if url else "❌"
        print(f"  {status} {labels.get(k, k)}: {url or '失败'}")
        if url:
            ok += 1
    print(f"\n成功 {ok}/4 张，JSON 已写回: {output_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
