"""v1.3 回归测试：三处正文损伤的根因修复。

1. _truncate_risk_title  不再把标题截在词中间
2. _build_operating_fact_cards 的数值分词器不切开千分位、不丢量纲
3. _render_fact_markers  不在同句已陈述该数值时就地贴引
"""
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import a_share_report_writer as W


# ── 1. 风险标题截断 ──────────────────────────────────────────────────────────

def test_truncate_risk_title_no_midword_cut():
    """24 字硬上限内必须以完整短语收尾，不得留下"的收"式残词。"""
    raw = "仿制药收入2026-2028年每年降约15%的收益承压风险"
    out = W._truncate_risk_title(raw)
    assert len(out) <= 24
    assert not out.endswith("的收"), out
    assert not out.endswith("的"), out  # 悬空定语一并去掉，落在完整短语上
    assert out == "仿制药收入2026-2028年每年降约15%"


def test_truncate_risk_title_keeps_short_title():
    assert W._truncate_risk_title("阿帕替尼降价12%等医保续约调价风险") == "阿帕替尼降价12%等医保续约调价风险"


def test_truncate_risk_title_prefers_punctuation():
    raw = "存货1308亿元同比+81%的备货去化风险，若需求不及预期"
    out = W._truncate_risk_title(raw)
    assert len(out) <= 24
    assert out == "存货1308亿元同比+81%的备货去化风险"


def test_truncate_keeps_trailing_risk_noun():
    """超限时优先保住结尾风险名词，而不是硬切掉最后一个字。"""
    raw = "仿制药2026-2028年每年下滑15%的缺口风险"
    assert len(raw) == 25
    out = W._truncate_risk_title(raw)
    assert len(out) <= 24, out
    assert out.endswith("缺口风险"), out
    assert out == "仿制药2026-2028年每年下滑15%缺口风险"


def test_derive_risk_title_does_not_midword_cut():
    """fallback 路径 _derive_a_share_risk_title 也不得切在词中间。"""
    sentence = "仿制药2026-2028年每年下滑15%的缺口风险可能拖累整体增速，需持续跟踪占比变化。"
    out = W._derive_a_share_risk_title(sentence, "恒瑞医药")
    assert len(out) <= 24, out
    assert not out.endswith("风"), out
    # 不得以半截词收尾（缺口风 / 悬空的"的"）。注意 "缺口风" 须锚定结尾，
    # 否则会误匹配到完整词 "缺口风险" 内部。
    assert not re.search(r"(?:缺口风$|的$)", out), out
    assert out == "仿制药2026-2028年每年下滑15%缺口风险"


_SRC = (  # 保留供纯结构判据回归参考
    "公司仿制药2026-2028年收入年降约15%；阿帕替尼降价12%等医保续约调价风险；"
    "四款成熟创新药收入占比由33%降至20%；多款主力产品今年迎来适应症拓展；"
    "生物制品税收优惠调整侵蚀非肿瘤品种利润的风险；核心产品价格降幅"
)


def test_dangling_title_tail_flags_midword_cut():
    """虚拟词尾与来源无从查证的结尾都判为截断。"""
    for bad in ("瑞普泊肽等GLP-1管线2027年上市与临床不确",
                "销售费用率下降与研发资本化率维持在",
                "阿帕替尼降价12%等医保续约调价的风"):
        assert W._has_dangling_title_tail(bad), bad


def test_dangling_title_tail_keeps_valid_titles():
    """合法标题不得被误杀——尤其是不含“风险”二字的标题。"""
    for good in ("阿帕替尼降价12%等医保续约调价风险",
                 "仿制药2026-2028年收入年降约15%",
                 "四款成熟创新药收入占比由33%降至20%",
                 "多款主力产品今年迎来适应症拓展",
                 "生物制品税收优惠调整侵蚀非肿瘤品种利润的风险",
                 "核心产品价格降幅",
                 # 以下两条是真实产出中曾被证据池兜底规则误杀的合法标题
                 "10款新品入院受阻拖累下半年准入兑现",
                 "生物制品税收优惠调整侵蚀部分产品利润率"):
        assert not W._has_dangling_title_tail(good), good


def test_section10_main_path_rejects_truncated_title():
    """主路径校验此前完全不做标题结构检查，须拒绝词中截断标题。"""
    payload = {"risks": [
        {"title": "瑞普泊肽等GLP-1管线2027年上市与临床不确", "trigger": "若临床读出延后",
         "impact": "上市节奏推迟将拖累创新药放量", "monitor": "III期读数与NDA受理", "source_refs": [1]},
        {"title": "阿帕替尼降价12%等医保续约调价风险", "trigger": "若续约降价延续",
         "impact": "成熟品种收入承压拖累创新药增速", "monitor": "医保续约价格", "source_refs": [1]},
        {"title": "仿制药2026-2028年收入年降约15%", "trigger": "若集采继续扩围",
         "impact": "仿制药收入收缩压制整体增速", "monitor": "仿制药季度收入", "source_refs": [1]},
        {"title": "对外授权收入确认依赖BMS等研发进度的风险", "trigger": "若里程碑延后",
         "impact": "授权收入递延拖累当期利润", "monitor": "首付款确认比例", "source_refs": [1]},
        {"title": "10款新进医保产品入院恢复慢于预期的风险", "trigger": "若入院受阻",
         "impact": "新品放量递延影响全年目标", "monitor": "医院准入数量", "source_refs": [1]},
        {"title": "研发资本化率约20%与费用率下降假设偏离风险", "trigger": "若III期加速",
         "impact": "资本化率上行增加减值压力", "monitor": "资本化率与费用率", "source_refs": [1]},
    ]}
    rendered, _issues = W._validate_render_section_10(payload, {1: {"n": 1}})
    # 有效行 >=5 时函数只返回渲染结果、不上抛逐条拒因，因此断言落在产物上。
    assert rendered, "6 条风险剔除 1 条后仍有 5 条，应正常渲染"
    assert "上市与临床不确" not in rendered, rendered
    assert "阿帕替尼降价12%等医保续约调价风险" in rendered, rendered
    assert len([l for l in rendered.splitlines() if l.startswith("•")]) == 5, rendered


def test_truncated_title_passes_downstream_validator():
    """修复后的标题必须仍能通过 §10 的 24 字硬约束校验。"""
    raw = "仿制药收入2026-2028年每年降约15%的收益承压风险"
    title = W._truncate_risk_title(raw)
    body = f"• **{title}**：若仿制药集采续约持续，收入端承压；重点跟踪仿制药季度收入。[1]"
    assert title in body
    assert len(title) <= 24


# ── 2. 事实卡数值分词 ────────────────────────────────────────────────────────

def _fact_values(text):
    """在给定原文上跑真实的事实卡提取，返回 (value, label) 列表。"""
    key_data = {
        "name": "江苏恒瑞医药股份有限公司",
        "short_name": "恒瑞医药",
        "ticker": "600276",
        "ref_map": {"mgmt_discussion": {"n": 11}},
        "_raw_data": {
            "mgmt_discussion": [{"content": text}],
        },
    }
    cards, fact_map = W._build_operating_fact_cards(key_data)
    return cards, fact_map


def test_thousand_separator_not_split():
    """'超过200,000家线下零售药店' 不得被切成 '000家' 残片。"""
    from unittest.mock import patch
    text = "公司商业化网络覆盖中国30多个省级行政区的超过25,000家医院及超过200,000家线下零售药店。"
    with patch.object(W, "_build_reference_evidence",
                      return_value={11: {"text": text, "api": "management_discussion"}}):
        cards, fact_map = _fact_values(text)
    values = [f["value"] for f in fact_map.values()]
    assert "000家" not in values, values
    assert not any(v.startswith("000") for v in values), values


def test_unit_not_dropped_for_wan_jia():
    """'25万家' 必须保留 '家' 量纲，不能退化成 '25万'。"""
    from unittest.mock import patch
    text = "公司已覆盖超过25万家零售药店，并成立DTP专职团队整合及拓展DTP药房等渠道。"
    with patch.object(W, "_build_reference_evidence",
                      return_value={11: {"text": text, "api": "management_discussion"}}):
        cards, fact_map = _fact_values(text)
    values = [f["value"] for f in fact_map.values()]
    assert "25家万" not in values
    assert "25万" not in values, f"量纲丢失: {values}"
    assert any(v.endswith("家") for v in values), values


def test_fact_card_carries_indicator_label():
    """事实卡必须显式下发指标名，否则模型会在同句多指标时标错基准值。"""
    from unittest.mock import patch
    text = "运营指标：毛利率持平于85%，与2024年第三季度相同，而销售与营销费用率从33%降至32%。"
    with patch.object(W, "_build_reference_evidence",
                      return_value={16: {"text": text, "api": "management_discussion"}}):
        cards, fact_map = _fact_values(text)
    assert cards, "应至少产出一张事实卡"
    assert "指标:" in cards, cards
    for line in cards.splitlines():
        assert "指标:" in line, line
        assert "来源:" in line, line
    # 85% 的卡必须标成毛利率，不能被同句的费用率抢走
    pct_cards = [f for f in fact_map.values() if "85" in str(f.get("value"))]
    assert pct_cards, fact_map
    assert any(f.get("label") == "毛利率" for f in pct_cards), pct_cards


def test_fin_revenue_specs_cover_rendered_rows():
    """抽取规格与表格展示行必须共用同一组取值键，防止指标增删时两处不同步。"""
    spec_keys = {key for key, _v, _yk, _yr in W._FIN_REVENUE_METRIC_SPECS}
    # gen_maincomp_fallback 实际渲染的四个行取值键
    rendered_keys = {"net_int", "fee", "other_nonint", "revenue"}
    assert rendered_keys <= spec_keys, rendered_keys - spec_keys
    # 同比键须与取值键同源，避免手写 "net_int_yoy" 这类字符串漂移
    yoy_pairs = [(k, yk) for k, _v, yk, _yr in W._FIN_REVENUE_METRIC_SPECS if yk]
    for key, yoy_key in yoy_pairs:
        assert yoy_key == f"{key}_yoy", (key, yoy_key)


def test_gen_maincomp_fallback_extracts_and_renders():
    """金融股营收构成兜底表：抽取 + 渲染均须正常。"""
    items = [
        {"endDate": f"{y}-12-31",
         "reviewDesc": f"利息净收入{1000+y}亿元，同比增长5.5%。"
                       f"手续费及佣金净收入{300+y}亿元，同比增长3.2%。"
                       f"其他非利息收益{80+y}亿元。营业收入{2000+y}亿元。"}
        for y in (2023, 2024, 2025)
    ]
    out = W.gen_maincomp_fallback({"_raw_data": {"mgmt_discussion": {"data": items}}})
    assert out, "三年完整数据应产出表格"
    assert "利息净收入" in out and "营业收入合计" in out
    assert "2025" in out and "2023" in out
    assert out.count("\n") >= 5


def test_gen_maincomp_fallback_first_match_wins():
    """同一指标在一行内多次出现时只取第一次，不得被后值覆盖。"""
    items = [
        {"endDate": f"{y}-12-31", "reviewDesc": f"营业收入{500+y}亿元。营业收入{999+y}亿元。"}
        for y in (2023, 2024, 2025)
    ]
    out = W.gen_maincomp_fallback({"_raw_data": {"mgmt_discussion": {"data": items}}})
    # 2025 行：首值 500+2025=2525 应被采用，后值 999+2025=3024 必须丢弃
    assert "2525" in out, out
    assert "3024" not in out and "3023" not in out, out


def test_quantitative_token_regex_handles_separators():
    """溯源校验用的分词器同样不能切开千分位。"""
    found = W._QUANTITATIVE_TOKEN_RE.findall("超过200,000家线下零售药店，覆盖25,000家医院")
    joined = [f"{v}{u}" for v, u in found]
    assert "000家" not in joined, joined
    assert "25,000家" in joined, joined


# ── 3. 同句重复数值就地贴引 ──────────────────────────────────────────────────

def test_render_fact_markers_drops_duplicate_in_clause():
    """标记落在小句末尾、该数值已在小句中出现时，应丢弃标记而非就地贴引。"""
    md = "公司已建立专业化处方药零售推广团队，覆盖超过25万家零售药店，同时成立DTP专职团队整合及拓展DTP药房等渠道{{FACT:F1}}。"
    fact_map = {"F1": {"value": "25万家", "ref": 11}}
    out = W._render_fact_markers(md, fact_map)
    assert "渠道25万家[11]" not in out, out
    assert "{{FACT" not in out, out
    assert "25万家零售药店" in out, out


def test_render_fact_markers_still_renders_fresh_number():
    """同一数值若尚未在句中出现，仍须正常落引，不能被误杀。"""
    md = "公司渠道覆盖持续扩大，期末覆盖零售药店{{FACT:F1}}，同比提升。"
    fact_map = {"F1": {"value": "25万家", "ref": 11}}
    out = W._render_fact_markers(md, fact_map)
    assert "零售药店25万家[11]" in out, out
