# 公司一页纸 Skill v1.3

生成 A 股 / 港股 / 美股上市公司的买方视角公司一页纸研究报告（Markdown + DOCX）。

核心约束是**可溯源**：正文中的事实、数字、比较和推演都必须绑定到真实数据源，并输出
`[N]` 行内引用与文末参考资料双向闭环。缺少可验证证据时按章节降级或 fail closed，
不以编造内容补齐。

## 环境依赖

- Python 3，依赖见 `scripts/requirements.txt`（`requests`、`python-docx`）
- 必填 `DATAYES_TOKEN`。查找顺序：环境变量 → `~/token.txt` → 脚本同目录 `token.txt`
  → `~/.datayes_token` → Windows `%USERPROFILE%\token.txt`

## 快速开始

A 股走两阶段（采集 → 写作），6 位纯数字代码：

```bash
python3 -X utf8 scripts/a_share_fetch_data.py \
  --ticker "600276" --output "out/600276_data.json"

python3 -X utf8 scripts/a_share_report_writer.py \
  --data "out/600276_data.json" \
  --output "out/恒瑞医药（600276）公司一页纸.md" \
  --docx "out/恒瑞医药（600276）公司一页纸.docx"
```

港股 / 美股由单一 writer 负责采集、写作与导出（港股 5 位数字，美股标准 ticker）：

```bash
python3 -X utf8 scripts/hk_us_report_writer.py \
  --ticker "00700" --market "HK" \
  --company-name "腾讯控股" --output-dir "out"
```

## 文件结构

```
.
├── SKILL.md                      # 主 Skill 定义（入口薄，细则在 references）
├── README.md                     # 本文件
├── CHANGELOG.md                  # 版本变更记录（含完整历史）
├── agents/openai.yaml            # Agent 配置
├── references/
│   ├── a-share-report-structure.md      # A 股章节结构、同业 schema、估值写法
│   ├── a-share-quality-checklist.md     # A 股质量清单
│   ├── a-share-api-interfaces.md        # A 股接口参数
│   ├── hk-us-report-structure.md        # 港美股章节与写法
│   ├── hk-us-quality-checklist.md       # 港美股质量清单
│   └── hk-us-api-playbook.md            # 港美股接口编排
├── scripts/
│   ├── a_share_fetch_data.py     # A 股数据采集（结构化接口 + 材料 + 资讯 + 同业）
│   ├── a_share_report_writer.py  # A 股正文生成、来源绑定、质量门禁、DOCX 导出
│   ├── hk_us_report_writer.py    # 港美股采集 + 写作 + 导出全流程
│   ├── fetch_materials.py        # 港美股素材采集
│   ├── fetch_materials_v2.py     # getMaterialsV2 多查询独立工具
│   ├── llm_adapter.py            # LLM 适配层（端点 / 格式 / 模型）
│   ├── markdown_to_docx.py       # Markdown → DOCX 底层转换
│   ├── build_docx.py             # A 股 DOCX 构建
│   ├── gen_charts.py             # 独立图表生成
│   └── check_api_registry.py     # 接口注册表体检（可挂 CI / 发布前检查）
└── tests/
    ├── test_a_share_writer_regressions.py  # A 股 writer 回归
    └── test_v13_provenance_fixes.py        # v1.3 溯源与文本保真修复回归
```

## 质量门禁

三个市场共用同一套 fail-closed 思路，由 writer 内置校验执行，不依赖外部 checker：

- **引用闭环**：正文 `[N]` 与参考资料双向校验，孤儿引用与未引用来源都会被清理
- **溯源拦截**：无法回溯到来源字段或原文的数字行会被删除，不进入交付物
- **预测口径**：`forecast` / `guidance` / `estimate` 分别标注，不写成实际事实
- **结构兜底**：空章节、空表、残缺情景表与孤儿表格行不进入 MD / DOCX
- **风险提示**：以目标公司真实材料为准，输出「触发条件 → 影响路径 → 跟踪项」

## 验证状态

- `tests/test_v13_provenance_fixes.py`：18 例，覆盖 v1.3 修复的根因及其真阳性 / 真阴性边界
  （千分位分词、单位量纲、同句重复贴引、风险标题词中截断、事实卡指标名下发、
  `gen_maincomp_fallback` 抽取与首 match 语义）
- `tests/test_a_share_writer_regressions.py`：A 股 writer 既有回归

## 注意事项

- 港美股没有独立的 post-repair / checker 阻断链路，质量控制集中在 writer 内置校验。
- A 股采集单个接口失败不阻塞整体流程；`ticker_period` 返回 `data: null` 时使用年度口径，
  不计为失败接口。
- 历史版本变更详见 `CHANGELOG.md`，本文件不再重复维护变更明细。
