---
name: datayes-company-onepager
version: v1.2.4
description: |
  生成 A股、港股和美股公司的买方视角公司一页纸报告。
  主路径通过 Datayes 数据采集脚本和自动化 writer 生成 MD + DOCX。
  当前版本强调真实数据、引用闭环、目标公司一致性、结构化质量门禁和自动修复。
  当用户要求生成“一页纸”“公司一页纸”“股票研究报告”“公司研究报告”或输入股票名称/代码/公司名称要求分析时触发。
metadata:
  short-description: 生成A股/港股/美股公司一页纸（v1.2.4）
  openclaw:
    requires:
      env: [DATAYES_TOKEN]
      bins: [python3]
---

# 公司一页纸深度研究报告

当前文档只描述 **v1.2.4 生效规则**。历史版本说明统一放在文末 Appendix，正文不再重复版本堆叠。

## 执行要求
运行 hk_us_report_writer_v124.py 时 Bash timeout 必须设为 1200000ms（20分钟），
默认 300s 不足以完成全管线。

## 0. Core Principles
- 真实数据优先，禁止编造、拼接或臆测数值。
- 报告文件是唯一交付物，聊天窗口只用于进度、路径和阻断原因。
- 不输出占位符、模板壳、内部工程话术、"已隐藏原因"或假通过结论。
- P0 和 P1 都是阻断项，必须修复后才能交付；P2 只能在规则允许时修复或保留为明确说明。
- 任何正文事实、数值、结论、比较、推演都必须能回溯到真实来源。
- 同一规则只保留一处权威写法；历史信息只出现在 Appendix。
- 不降低当前质量标准，不因为单一来源缺失就跳过核心门禁。

## 1. Trigger & Input
- 触发条件：用户要求生成“一页纸”“公司一页纸”“股票研究报告”“公司研究报告”或输入股票名称/代码/公司名称要求分析。
- 必填输入：目标公司或股票代码、市场线索、Datayes Token。
- 可选输入：用户特别关注的业务线、事件、时间窗口、比较对象。
- Token 获取顺序：
  - `DATAYES_TOKEN` 环境变量
  - `~/token.txt`
  - 脚本同目录 `token.txt`
  - `~/.datayes_token`
  - Windows 下的 `%USERPROFILE%\token.txt`
- 找到 Token 后，后续所有 API 调用都使用 `Authorization: Bearer {DATAYES_TOKEN}`。
- 所有脚本调用都使用 `python3 -X utf8`，不要改用 `python` 或 `py`。

## 2. Market Routing
- 先按输入特征判断市场，再进入对应子流程。
- A 股识别：
  - 6 位纯数字代码
  - `stock_search` 返回的 A 股结果
  - 中文公司名经 `stock_search` 解析后落到 A 股
- 港股识别：
  - 带 `.HK` / `.hk` 后缀的代码
  - 4-5 位纯数字港股代码，若存在歧义必须先确认
  - 中文或英文公司名经 `stock_search` 解析后落到港股
- 美股识别：
  - 纯英文 ticker
  - 带 `.O` / `.N` / `.US` 后缀的代码
  - 中文或英文公司名经 `stock_search` 解析后落到美股
- `stock_search` 只用于公司名，不用于代码搜索。
- `entity_id` 是后续结构化接口的唯一入参代码：
  - A 股使用 6 位纯数字 `entity_id`
  - 港股和美股使用市场对应的 `entity_id` / ticker 形式
- 如果市场仍然模糊，先问用户确认是 A 股、港股还是美股，再继续。

## 3. A-Share Pipeline

### 3.1 Entry Rules
- A 股主路径是 `fetch_data.py` → `report_writer.py`。
- 必须先通过元信息接口发现 API URL 和参数，不允许自己拼接业务 API URL。
- 每个业务接口只接受元信息接口返回的调用 URL；如果元信息查不到或接口失败，就跳过该接口，不要自行重试构造 URL。
- 数据获取优先级固定为：
  1. 结构化接口
  2. Materials V2
  3. 研报全文
  4. 研报图表与表格说明
  5. 公告、纪要、调研、公司披露
  6. 公开网页补充
- 不得因为前一级没有数据就直接结束，必须逐级降级到最低优先级。

### 3.2 Resolve Before Fetch
- 当 A 股公司名或代码存在歧义时，先运行：

```bash
python3 -X utf8 <skill_root>/scripts/fetch_data.py \
  --ticker "{用户输入}" \
  --token "{DATAYES_TOKEN}" \
  --resolve-only
```

- 只读取脚本输出的 `RESOLVED_CODE`、`RESOLVED_NAME`、`RESOLVED_PERIOD`、`RESOLVED_AMBIGUOUS`。
- `RESOLVED_AMBIGUOUS=1` 时，先把候选列表交给用户确认，再继续。
- `RESOLVED_CODE` 为空时立即停止，请用户给出更准确的公司名或 6 位代码。
- 不要自己 curl `stock_search` 后再手动解析嵌套 JSON。

### 3.3 Fetch and Write
- 运行数据采集脚本时，`entity_id` 只允许使用 6 位纯数字，不加 `.SH` / `.SZ`，也不使用其他响应字段替代。

```bash
python3 -X utf8 <skill_root>/scripts/fetch_data.py \
  --ticker "{6位股票代码}" \
  --token "{DATAYES_TOKEN}" \
  --output "{输出目录}/{股票代码}_data.json"
```

- 采集完成后直接进入 `report_writer.py`。

```bash
python3 -X utf8 <skill_root>/scripts/report_writer.py \
  --data "{输出目录}/{股票代码}_data.json" \
  --output "{输出目录}/{公司名}（{股票代码}）公司一页纸.md" \
  --docx "{输出目录}/{公司名}（{股票代码}）公司一页纸.docx"
```

- `report_writer.py` 负责生成正文、自动修复和 DOCX 导出；不要把对话窗口当成正文输出区。

### 3.4 A-Share Adapters
- 特殊行业必须使用各自适配的指标体系，不要强行套用通用消费品模板。
- 保险、银行、科技、平台、资源周期、REITs、生物医药等行业的指标应以对应参考文件为准。
- 材料不足时允许减少表格数量，但不得用不适用指标硬填。

### 3.5 A-Share Fallback
- 若自动 writer 失败，按 `references/a-share-report-structure.md` 手工组织内容，并按 `references/a-share-quality-checklist.md` 自检。
- 手工降级不等于放宽标准，所有质量门禁仍然有效。

## 4. HK/US Pipeline

### 4.1 Entry Rules
- 港美股主路径是 `fetch_materials.py` → `hk_us_report_writer_v124.py`。
- 入参至少包含：
  - `market=HK` 或 `market=US`
  - 公司名或 ticker
- 港股 ticker 传 API 时使用 5 位纯数字，不带 `.HK`。
- 美股 ticker 使用标准 ticker，例如 `NVDA`、`AAPL`。
- 港美股结构化能力仍然以 `stock_search` 和解析后的 `entity_id` 为准。

### 4.2 Materials Collection
- 港美股材料采集优先使用脚本自动获取 `DATAYES_TOKEN`，不要额外要求用户手动传 token 参数。
- 输出目录应为可写路径，避免写到容易被 sandbox 拦截的位置。

```bash
python3 -X utf8 <skill_root>/scripts/fetch_materials.py \
  --company "{公司名}" \
  --ticker "{ticker}" \
  --market "{HK|US}" \
  --output "{输出目录}/{ticker}_materials.json"
```

- 若只有公司名没有 ticker，可省略 ticker；若只有 ticker，可省略 company。
- 脚本内部会组合多次 query，覆盖催化、投资逻辑、业务财务、产销生态、估值分歧、市场关注等材料。

### 4.3 Source Trace and Citation
- 写报告前必须先从 materials JSON 建立 `source_id -> {title, organization, publishTime, type, url, text}` 的索引。
- 正文里的每个 `[N]` 只能来自这个索引或结构化接口映射，不允许自增、猜测或复用不存在的编号。
- 公告、研报、纪要和公开网页都必须进入 `materials JSON` 或其 `external_sources`，然后才能被引用。
- 参考资料元数据必须逐字复制，不能截断标题，不能删前后缀，不能把来源类型换成别的机构名。
- 正文中的事件、指标和结论都必须标注属性：`actual`、`forecast`、`guidance`、`estimate`。
- 一旦原文包含“预计 / 预期 / 有望 / 将 / 目标 / forecast”等语义，正文必须保留预测属性，不能写成已发生事实。

### 4.4 HK/US Specific Rules
- HK 财务数据优先使用 PIT 三表结构化接口补齐。
- US 报告必须显式区分：
  - GAAP / non-GAAP
  - segment actual
  - company guidance
  - forecast / estimate
  - fiscal year 与 calendar year
  - 人民币 / 美元币种
  - 普通股 / ADS 口径
- US 若缺少结构化三表，只能记为 `skipped_with_reason` 或 `N/A`，不能写成“检查通过”。
- 港美股特殊行业必须使用适配的指标体系，不得套用不适用的通用消费模板。

### 4.5 HK/US Writer
- `hk_us_report_writer_v124.py` 是首选自动 writer，负责：
  1. 采集材料
  2. 生成 `source_trace.json` / `id_audit.json`
  3. 生成章节正文
  4. 执行 post-repair
  5. 运行质量检查
  6. 导出 DOCX
- writer 失败时，按 `references/hk-us-report-structure.md` 手工降级，并按 `references/hk-us-quality-checklist.md` 自检。
- 不允许把失败包装成成功，也不允许跳过质量检查直接交付。

### 4.6 HK Financials
- 港股 PIT 三表补齐由 `hk_financials.py` 负责。
- 只保留可验证字段，缺失项保留 `None`，不要硬造数。

## 5. Quality Gates

### 5.1 Blocking Levels
- `P0`：立即阻断，必须修复。
- `P1`：立即阻断，必须修复。
- `P2`：允许保留为明确说明，但不能伪装成通过项。

### 5.2 Required Content
- 所有必填章节必须非空。
- 章节编号必须和对应模板一致，H3 必须继承父级 H2 编号，同级编号不能重复或倒序。
- 必需章节缺失时，优先修复或补齐，不要直接留空。
- 不能输出模板壳、占位符、`N/A` 大面积充数、无来源空表。
- 不能出现“已隐藏原因”“内部 pipeline”“checker 通过”等内部工程话术。

### 5.3 Citation Closure
- 正文中的关键事实、财务数据、经营指标、行业格局、估值、催化与风险都必须带行内 `[N]` 引用。
- 参考资料与正文必须双向闭环：
  - 正文引用集合必须能在参考资料中找到
  - 参考资料中被列出的条目必须至少被正文使用一次
- 不得出现正文引用的 `[N]` 在参考资料中不存在。
- 不得出现参考资料列出但正文从未使用的死引用。
- `source_trace.json` 必须与正文引用集合一致，`refs_missing=0`、`refs_synthetic=0`、`raw_payload_file` 全部存在。

### 5.4 Reference Metadata
- 参考资料必须逐字复制：
  - `title`
  - `organization`
  - `publishTime`
  - `url`
  - 结构化接口回来的真实 ID
- 仅限 Datayes 研报的 `organization` 字段可以使用脚本里已有的标准简称映射，且只能使用标准简称；除此之外所有来源的 `organization` 仍必须逐字复制，不得自行改写、猜测、补写或删除机构信息。
- `title` 必须逐字复制，不得截断、改写、删除前后缀。
- 公开网页引用必须保留完整 URL。
- 若某来源只能通过降级链路拿到，则要记录其来源原因，不能伪装成原始 material_id。

### 5.5 Actual / Forecast / Guidance / Estimate
- `actual` 只表示已发生或已披露事实。
- `forecast` 只表示研报预测。
- `guidance` 只表示公司指引。
- `estimate` 只表示模型估算。
- 不得把预测写成已发生，不得把指引写成事实，不得把模型估算写成公司披露。

### 5.6 Tables, Sparse Data and Peer Comparison
- 同业比较表必须是正式 Markdown 表格，不能只用纯文字描述行业格局。
- 表头必须中文。
- 统一同业比较表 schema 为 10 列：
  - 竞争关系
  - 公司（代码）
  - 市场
  - 可比业务
  - 行业地位
  - 相关业务进展
  - 市值
  - 商业模式
  - 目标客户群体
  - 核心产品
- Markdown 表头必须保持可正常渲染，示例：

```markdown
| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 市值 | 商业模式 | 目标客户群体 | 核心产品 |
|---|---|---|---|---|---|---|---|---|---|
```

- 如果市值数据不全，可以省略“市值”列；但省略后其余 9 列必须完整，且仍然必须是中文表头。
- 不能只留下口语描述。
- 不能用 `Comparable peer A/B/C`。
- 不能用英文表头。
- 不能大面积用 `—` / `N/A` / `未披露` 填充。
- 同业比较最低要求：
  1. 必须有本公司行；
  2. 至少 3 家真实可比公司；
  3. 每个可比公司必须属于同一行业或相邻可比行业；
  4. 目标公司自身不能被列为竞争对手；
  5. 相关业务进展必须具体可验证，不能只写“行业领先”“持续增长”；
  6. 表格必须能被 Markdown 正常渲染；
  7. 表头必须中文。
- 稀疏数据规则：
  - 结构化表格若只剩 0 或 1 个有效值，删除整行。
  - 若整列只剩 0 或 1 个有效值，删除整列。
  - 删除后若仍不足以支撑 2 个有效指标或 2 个比较维度，删除整张表。
  - 不能用空白、`N/A`、`未披露`、`--`、`待补充` 大面积充数。

### 5.7 Scenario Analysis
- 情景推演必须写出可验证公式、基础数据、核心假设和单位。
- 不能只写“乐观 / 中性 / 悲观”三档而没有具体数值。
- 每个情景至少给出 2-3 个核心变量，必须能够算出目标值或估值区间。
- 若使用模型推导，正文必须保留推导公式和 sanity check。
- 无法完整复核时，只保留定性判断，不要报出不可验证的数字。

### 5.8 Post-Repair and Checker
- 生成正文后先做 post-repair，再做 checker，最后导出 DOCX。
- A 股和港美股的自动修复脚本都属于当前生效规则的一部分，不能跳过。
- 质量检查只允许把问题标成阻断、修复中或明确跳过，不允许把未验证项记为通过。
- 检查发现的空章节、空表、空预测、空催化、空情景、空比较表都应优先修复或删除，不可硬留。

## 6. Fallback Policy
- 自动 writer 失败时，优先调用脚本内置修复；仍失败时才进入手工降级。
- 手工降级只能使用对应的结构参考文件和质量清单，不得引入额外模板壳。
- 缺少关键证据时，必须阻断并向用户说明需要补什么。
- 不得用“已省略”“已隐藏”替代真实来源说明。
- 不能假通过，不能把不可验证的内容包装成完成态。

## 7. Output Requirements
- 最终交付物必须包含：
  - `MD`
  - `DOCX`
- 对话窗口只输出进度、阻断原因和最终路径，不输出完整报告正文。
- Word 输出样式必须保持以下详细规则：
  - 页面：纵向 A4
  - 左右边距约 0.83 inch
  - 上下边距约 0.71 inch
  - 英文字体 Calibri
  - 中文字体 微软雅黑
  - 正文 10.5pt
  - 正文颜色 `#1F1F1F`
  - 主标题居中 18pt 深蓝 `#1F3A5F`
  - 一级标题 13.5pt
  - 二级标题 12pt
  - 三级标题 11pt
  - 表格表头浅蓝底 `#D9EAF7`
  - 表格细网格线
  - 表内文字港美股 10pt / A 股 9.5pt
  - 表头加粗
  - 引用 `[N]` 保持正文可读性，不做过小上标
  - 无目录页
- 必须保持表格纵向合并、重复表头处理和必要的 `w:vMerge` 结果。
- 不要生成目录页。
- 输出文件名按脚本当前约定生成，不要自己另起一套命名规则。

## 8. Reference Files
以下文件是当前规则的权威补充，正文只保留流程和门禁，细节以这些文件为准：

- `references/a-share-api-interfaces.md`：A 股 API 接口参数说明
- `references/a-share-report-structure.md`：A 股章节结构、写作规范、同业比较 schema
- `references/a-share-quality-checklist.md`：A 股质量清单与检查项
- `references/hk-us-api-playbook.md`：港美股 API 使用手册
- `references/hk-us-report-structure.md`：港美股章节结构、写作规范、同业比较 schema
- `references/hk-us-quality-checklist.md`：港美股质量清单与检查项
- `references/report-verification-prompt.md`：报告验证提示词与 ID 审计规则

## Appendix A. Version History

### v1.2.3
- 收敛章节编号规则，强制 H2/H3 编号一致。
- 固化数据获取优先级，要求逐级降级。
- 强化稀疏行列处理，禁止空表、空占位符和伪造数据。
- 强化正文行内引用闭环和参考资料逐字复制。
- 补充派生测算、特殊行业适配和输出前自检。

### v1.2.3-R2
- 增加必填章节非空检查。
- 增加情景推演有效性检查。
- 增加同业比较表正式 schema 检查。
- 增加港美股最小结构检查、结构化接口 ID 口径修正、空占位符检查。
- 增加内部 checker 术语泄漏检查和 pipeline 结尾内容清理。

### v1.2.4
- 增加生成后自动修复能力。
- 增加港美股 post-repair 流程。
- 增加港股 PIT 三表聚合。
- 增加近况跟踪句首加粗规则。
- 增加参考资料时效性约束和空预测节省略规则。
- 增加港美股特殊行业 GAAP / non-GAAP 等适配。

