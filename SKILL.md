---
name: datayes-company-onepaper
version: v1.2.34
license: MIT
compatibility: network
description: |
  生成 A 股、港股和美股公司的买方视角公司一页纸报告（Markdown + DOCX）。
  当用户要求“公司一页纸”“股票研究报告”“公司研究报告”，或给出上市公司名称/代码要求分析时使用。
  使用 DATAYES_TOKEN、Datayes 数据采集脚本和 writer 生成带引用的报告；没有可验证证据时不得编造。
metadata:
  short-description: 生成上市公司一页纸投研报告
  openclaw:
    requires:
      env: [DATAYES_TOKEN]
      bins: [python3]
    network:
      allow:
        - gw.datayes.com
        - api.datayes.com
        - ai.datayes.com
        - api.wmcloud.com
        - llm-proxy.datayes.com
---

# 公司一页纸深度研究报告

## 1. 核心原则

- 真实数据与可追溯来源优先。正文事实、数字、结论、比较和推演都必须能回溯到真实来源。
- 报告文件是唯一交付物；对话只说明进度、路径和阻塞原因。
- 不输出占位符、模板壳、内部工程话术、虚构来源或“已通过”的假结论。
- 缺少证据时按章节降级或 fail closed，不以编造内容补齐。
- 所有脚本使用 `python3 -X utf8`，文件统一 UTF-8（无 BOM）。

## 2. 输入与市场路由

必填：目标公司或股票代码、市场、可用的 `DATAYES_TOKEN`。公司名或市场不明确时先确认。

- A 股：6 位纯数字代码，走 A 股双阶段流程。
- 港股：5 位数字或 `.HK`，走港美股 writer。
- 美股：标准 ticker 或 `.US`，走港美股 writer。

Token 查找顺序：`DATAYES_TOKEN` 环境变量、`~/token.txt`、脚本同目录 `token.txt`、`~/.datayes_token`、Windows `%USERPROFILE%\token.txt`。所有 API 请求使用 `Authorization: Bearer {DATAYES_TOKEN}`。

## 3. A 股流程

### 3.1 采集

先通过 Datayes 元信息网关发现接口 URL 与参数，不能自行拼接业务接口。执行：

```bash
python3 -X utf8 <skill_root>/scripts/a_share_fetch_data.py \
  --ticker "{6位股票代码}" \
  --token "{DATAYES_TOKEN}" \
  --output "{输出目录}/{股票代码}_data.json"
```

采集优先级：结构化接口、Materials V2、研报全文、研报图表/表格、公告/纪要/调研/公司披露。单个接口失败不阻塞；`ticker_period` 返回 `data: null` 时使用年度口径，不作为失败接口。

### 3.2 写作与导出

```bash
python3 -X utf8 <skill_root>/scripts/a_share_report_writer.py \
  --data "{输出目录}/{股票代码}_data.json" \
  --output "{输出目录}/{公司名}（{股票代码}）公司一页纸.md" \
  --docx "{输出目录}/{公司名}（{股票代码}）公司一页纸.docx"
```

writer 负责正文生成、来源绑定、结构修复、质量门禁和 DOCX 导出；上述处理均在内置主链路完成，不依赖外部质量检查脚本。不要将聊天窗口作为报告正文输出。

### 3.3 A 股同业比较

同业表只使用经股票检索精确验证的直接可比上市公司；客户、供应商、合作方、投资方和未上市主体不能作为 peer。

固定 schema 为 9 列：`竞争关系 / 公司（代码） / 市场 / 可比业务 / 行业地位 / 相关业务进展 / 商业模式 / 目标客户群体 / 核心产品`。不得出现“市值”列。

- 标的公司必须是第一行且只出现一次；至少需要两家有效 peer，否则省略 §8.2。
- 除“相关业务进展”外，允许写简洁定性画像，无需逐格引用；不能编造精确财务数字、排名或客户名单。
- “相关业务进展”是唯一要求引用的列：标的行使用标的自身材料，peer 行使用其自身 `getMaterialsV2` 定向材料。
- 每格只保留一条最新经营事件，约 80 个汉字以内；优先新品、产品结构、渠道、价格、产能、组织改革和市占率。财务数据仅可作背景，不得成为主要内容。
- 标的行没有可审计进展时，整体删除“相关业务进展”列，保留其他研究维度。

### 3.4 A 股估值与情景推演

§9.3 由 writer 基于一致预期 EPS × 对应 PE 的统一价格锚确定性生成；估值排名接口仅用于同口径横向比较。接口口径与统一锚偏离明显时，不能将其视为当前定价或情景目标价依据。

§9.4 只使用来源绑定的经营事实卡：

- 至少两项不同经营维度的当前基准值，必须带真实 `[N]` 引用。
- 营收、净利润、EPS 和券商预测不是核心变量；不得编造销量、价格、收入、利润、EPS、PE 或股价数字。
- 三档只描述相对当前基准的经营方向及收入、利润、现金流传导；估值含义只写相对统一锚的敏感性，并明确“不提供目标价”。
- 模型输出出现结构或来源问题时，writer 用同一批事实卡确定性重建；仍不满足两项来源变量时才省略 §9.4。

## 4. 港股和美股流程

港美股由单一 writer 负责采集、来源索引、写作和导出：

```bash
python3 -X utf8 <skill_root>/scripts/hk_us_report_writer.py \
  --ticker "{ticker}" \
  --market "{HK|US}" \
  --company-name "{公司名}" \
  --output-dir "{输出目录}"
```

港股 ticker 使用 5 位纯数字，美股使用标准 ticker。美国公司必须明确区分 GAAP/non-GAAP、实际/指引/预测、财年/自然年、币种和普通股/ADS 口径。

## 5. 引用与质量门禁

- 正文 `[N]` 只能来自采集到的结构化接口或材料索引，参考资料与正文必须双向闭环。
- 预测、指引和估算必须分别标注 `forecast`、`guidance`、`estimate`，不能写成实际事实。
- 数字引用要与来源字段或原文相容；来源不支持时删除该事实或该单元格。
- 空章节、空表、残缺情景表和孤儿表格行不得进入 MD 或 DOCX。
- 风险提示只使用目标公司的真实材料，避免通用模板化风险。

## 6. 输出要求

最终交付必须包含 MD 和 DOCX。DOCX 采用纵向 A4、微软雅黑中文字体、Calibri 英文字体、无目录页，并保持表头、表格网格和必要的纵向合并。

## 7. 参考文件

按需读取以下文件，不重复在本文件维护细节：

- `references/a-share-api-interfaces.md`：A 股接口参数
- `references/a-share-report-structure.md`：A 股章节、同业 schema 与估值写法
- `references/a-share-quality-checklist.md`：A 股质量清单
- `references/hk-us-api-playbook.md`：港美股接口
- `references/hk-us-report-structure.md`：港美股章节与写法
- `references/hk-us-quality-checklist.md`：港美股质量清单