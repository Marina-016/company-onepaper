---
name: datayes-company-onepaper
version: v1.2.12
license: MIT
compatibility: network
description: |
  生成 A股、港股和美股公司的买方视角公司一页纸报告。
  主路径依赖 DATAYES_TOKEN、python3、Datayes 数据采集脚本和自动化 writer 生成 MD + DOCX。
  当前版本强调真实数据、引用闭环、目标公司一致性、结构化质量门禁和自动修复。
  当用户要求生成”一页纸””公司一页纸””股票研究报告””公司研究报告”或输入上市公司名称/代码要求分析时触发。
  不处理非上市主体、非金融研究任务或无法取得可验证来源的公司分析，不在证据不足时编造报告。
metadata:
  short-description: 生成A股/港股/美股公司一页纸（v1.2.12）
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

## 0. Core Principles
- 真实数据优先，禁止编造、拼接或臆测数值。
- 报告文件是唯一交付物，聊天窗口只用于进度、路径和阻断原因。
- 不输出占位符、模板壳、内部工程话术、"已隐藏原因"或假通过结论。
- 严重质量问题必须在 writer 内修复、fail closed 或明确记录降级原因，不能假通过。
- 任何正文事实、数值、结论、比较、推演都必须能回溯到真实来源。
- 同一规则只保留一处权威写法。
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

### 1.1 获取与配置 Datayes Token

访问 https://ai.datayes.com 获取可撤销的 API token。

macOS / Linux：

```bash
export DATAYES_TOKEN='your-token'
```

Windows CMD：

```cmd
set DATAYES_TOKEN=your-token
```

Windows PowerShell：

```powershell
$env:DATAYES_TOKEN = "your-token"
```

### 1.2 执行边界与跨平台约束

- **禁止对话侧网页搜索**：主流程不使用 WebSearch/WebFetch；公开网页补充只能通过已配置的数据采集接口进入 materials/source trace 后使用。
- **减少探索**：优先使用 `references/` 中已知接口与元信息网关，不重复猜测接口 URL 或参数。
- **明确边界**：不处理非上市主体、非金融查询；目标公司或市场不明确时先确认；可验证证据不足时 fail closed。
- **UTF-8**：所有平台统一使用 `python3 -X utf8`；Windows 终端若仍出现 GBK 乱码，先将终端切换为 UTF-8。
- **路径**：输出路径由调用方传入，脚本使用 `pathlib` / `os.path`，不得硬编码平台路径分隔符。
- **字体**：DOCX 中文字体依赖微软雅黑，英文字体依赖 Calibri；运行环境缺少字体时允许字体替代，但不得改变数据内容。
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
- A 股主路径是 `a_share_fetch_data.py` → `a_share_report_writer.py`。
- 必须先通过中台网关 `https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api` 发现 API URL 和参数，不允许自己拼接业务 API URL。
- 每个业务接口只接受元信息接口返回的调用 URL；如果元信息查不到或接口失败，就跳过该接口，不要自行重试构造 URL。
- 数据获取优先级固定为：
  1. 结构化接口
  2. Materials V2
  3. 研报全文
  4. 研报图表与表格说明
  5. 公告、纪要、调研、公司披露
  6. 公开网页补充
- 不得因为前一级没有数据就直接结束，必须逐级降级到最低优先级。

### 3.2 Fetch and Write
- 本节命令仅限 A 股；港股/美股不要套用这里的 `--data` / `--output` / `--docx` 参数。
- 运行数据采集脚本时，`entity_id` 只允许使用 6 位纯数字，不加 `.SH` / `.SZ`，也不使用其他响应字段替代。

```bash
python3 -X utf8 <skill_root>/scripts/a_share_fetch_data.py \
  --ticker "{6位股票代码}" \
  --token "{DATAYES_TOKEN}" \
  --output "{输出目录}/{股票代码}_data.json"
```

- 采集完成后直接进入 `a_share_report_writer.py`。

```bash
python3 -X utf8 <skill_root>/scripts/a_share_report_writer.py \
  --data "{输出目录}/{股票代码}_data.json" \
  --output "{输出目录}/{公司名}（{股票代码}）公司一页纸.md" \
  --docx "{输出目录}/{公司名}（{股票代码}）公司一页纸.docx"
```

- `a_share_report_writer.py` 负责生成正文、自动修复和 DOCX 导出；不要把对话窗口当成正文输出区。

### 3.3 A-Share Adapters
- 特殊行业必须使用各自适配的指标体系，不要强行套用通用消费品模板。
- 保险、银行、科技、平台、资源周期、REITs、生物医药等行业的指标应以对应参考文件为准。
- 材料不足时允许减少表格数量，但不得用不适用指标硬填。

### 3.4 A-Share Fallback
- 若自动 writer 失败，按 `references/a-share-report-structure.md` 手工组织内容，并按 `references/a-share-quality-checklist.md` 自检。
- 手工降级不等于放宽标准，所有质量门禁仍然有效。

### 3.5 A-Share Risk Guard
- §10 风险提示固定输出 3-4 条，每条使用 `• **公司特有风险标题**：触发条件/影响[N]`，必须有真实行内引用。
- 风险上下文从目标公司研报的 `title/detail_text/abstract/text`、会议纪要、机构调研及最新 `fdmtNew` 财务数据构建；不得读取不存在的 `content/summary` 字段，也不得依赖并行章节尚未生成的 `catalyst_table_ctx`。
- 后处理统一识别 `•`、`-`、`*` 三种项目符号；合规的 `•` 输出不得再被误判为 0 条。
- LLM 输出不合格时，只允许从带引用的目标公司风险证据重建；禁止使用“数据缺失风险”“模型不确定性风险”“不构成投资建议”等静态模板凑数。
- 重建后仍不足 3 条、存在无引用条目或命中通用模板时，最终自检必须 fail closed，不输出可发布报告。

## 4. HK/US Pipeline

### 4.1 Entry Rules
- 港美股主路径是直接运行 `hk_us_report_writer.py`；writer 内部会调用 `fetch_materials.py` 采集材料、构建溯源并导出报告。
- 正常生成只能使用下面这一种命令形态，不要复制 A 股 writer 的 `--data` / `--output` / `--docx` 参数：

```bash
python3 -X utf8 <skill_root>/scripts/hk_us_report_writer.py \
  --ticker "{ticker}" \
  --market "{HK|US}" \
  --company-name "{公司名}" \
  --output-dir "{输出目录}"
```

- `fetch_materials.py` 只作为单独排查材料采集时使用；正常生成不要先手动采集后再把材料 JSON 传给 writer。
- 入参至少包含：
  - `market=HK` 或 `market=US`
  - 公司名或 ticker
- 港股 ticker 传 API 时使用 5 位纯数字，不带 `.HK`。
- 美股 ticker 使用标准 ticker，例如 `NVDA`、`AAPL`。
- 港美股结构化能力仍然以 `stock_search` 和解析后的 `entity_id` 为准。

### 4.2 Materials Collection
- 港美股材料采集由 `hk_us_report_writer.py` 自动触发，优先使用脚本自动获取 `DATAYES_TOKEN`，不要额外要求用户手动传 token 参数。
- 输出目录应为可写路径，避免写到容易被 sandbox 拦截的位置。
- writer 会在输出目录写入 `{ticker}_materials.json`、`source_trace.json`、`id_audit.json`、`generation_status.json`、`report.md` 和最终 DOCX。
- 若需要排查采集问题，才单独运行 `fetch_materials.py`；排查完成后仍回到 §4.1 的 writer 单入口命令重新生成。

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
- US 若缺少结构化三表，诊断文件只能记为 `skipped_with_reason` 或 `N/A`，不能写成“检查通过”；报告正文只能从研报/财报点评抽取财务数据，不用 `N/A` 填正文。
- 港美股特殊行业必须使用适配的指标体系，不得套用不适用的通用消费模板。

### 4.5 HK/US Writer
- `hk_us_report_writer.py` 是首选自动 writer，负责：
  1. 采集材料
  2. 生成 `source_trace.json` / `id_audit.json`
  3. 生成章节正文，并对 JSON、引用、表格行数和章节完整性做内置校验
  4. 按最终有效章节连续重编号，写入 `section_number_mapping`
  5. 生成 MD 后直接转 DOCX（港美股已移除 post-repair 和 checker 阻断）
- 不要使用旧式 `--data` / `--output` / `--docx` 参数；这些是 A 股 writer 或旧版本接口，当前港美股 writer 不接受。
- 无 LLM API Key 时自动降级为材料直写模式，从采集材料手动拼装各章节并标注引用来源。
- §3 投资逻辑：短期与长期并行生成；任一侧失败时使用一次合并短重试，仍不合格则整章 fail closed。
- §4 催化事件：LLM 空响应、超时或 schema 失败时，允许从目标公司研报摘要确定性生成 4-7 行 source-backed 催化表；时间轴应同时覆盖近期已发生验证事件和未来可跟踪催化，不能把券商评级/目标价调整当催化。
- §5.2 分业务表现：优先输出近三年已完成年度 actual 分业务收入/占比/毛利率；没有最近一年收入或占比时，不硬造表格，改为 `§5.2 业务深度`，用分点叙述，分点小标题加粗。
- §6 产销链与生态：目标输出至少 4 行；补充调用最多补 1 行，不能用空补充覆盖主调用合格结果；最终 3 行可作为 `partial_json` 输出，少于 3 行或证据不足则 fail closed，不做通用确定性补行。
- §8 市场关注/调研大纲：JSON 解析失败时必须 retry；港股调研议题允许截断为 3-4 个，合法后输出。
- §9 行业对比：LLM 返回后必须校验目标公司只出现一次，且至少包含 3 家非目标 peer；peer 行必须有引用，且引用证据需包含该 peer 公司名或 ticker；不再使用 peer context/raw snippet 兜底，证据不足则 fail closed。
- §10 市场分歧：`section_10_a` / `section_10_b` 并行生成并合并 3 行；合并失败时走一次 compact short retry，允许 2 行 `partial_json_short_retry`；不再使用确定性原文摘录兜底。
- §11 估值与预测：大段估值 LLM 调用只做 1 次主 attempt；`EMPTY_TEXT_RESPONSE` / `NETWORK_TEMPORARY` 可由 `_call_llm` 触发轻量 compact retry；§11.1 盈利预测表必须删除所有数据行均为空/破折号的预测年份列；失败后按既有规则省略 §11 并重编号。
- 慢章节耗时控制：港股 §4、§10、§11 及 §11 前置 target-price basis 不做 LLM repair 叠加；单次 LLM 调用使用 `HKUS_LLM_SLOW_SECTION_TIMEOUT_SECONDS` 硬预算（默认 90 秒，最高 120 秒），§10 两个 split part 保持并行。轻量重试由 `HKUS_LLM_LIGHT_RETRY` 控制，默认开启。

### 4.6 HK Financials
- 港股 PIT 三表补齐由 `hk_financials.py` 负责。
- 只保留可验证字段，缺失项保留 `None`，不要硬造数。

## 5. Quality Gates

### 5.1 Blocking Levels
- 质检脚本（checker）已移除，不再有 P0/P1/P2 阻断。
- 生成 MD 后直接调用 `build_docx.py` 转 Word，不做质检过滤。

### 5.2 Required Content
- 所有必填章节必须非空。
- 不能输出模板壳、占位符、`N/A` 大面积充数、无来源空表。

### 5.3 Citation Closure
- 正文中的关键事实、财务数据、经营指标都应带行内 `[N]` 引用。
- 参考资料与正文保持双向闭环。

### 5.4 Reference Metadata
- 参考资料必须逐字复制 `title`、`organization`、`publishTime`。

### 5.5 Actual / Forecast / Guidance / Estimate
- `actual` 只表示已发生或已披露事实。
- `forecast` 只表示研报预测。
- `guidance` 只表示公司指引。
- `estimate` 只表示模型估算。
- 不得把预测写成已发生，不得把指引写成事实，不得把模型估算写成公司披露。

### 5.6 Tables, Sparse Data and Peer Comparison
- 同业比较表必须是正式 Markdown 表格，不能只用纯文字描述行业格局。
- 表头必须中文。

**A 股同业比较表 schema（10 列，含"市值"，缺数据可删）**，权威定义见 `references/a-share-report-structure.md` §8.2：
  - 竞争关系
  - 公司（代码）
  - 市场
  - 可比业务
  - 行业地位
  - 相关业务进展
  - 市值（缺数据可删列）
  - 商业模式
  - 目标客户群体
  - 核心产品

**港美股同业比较表 schema（9 列，不含"市值"）**：
  - 竞争关系
  - 公司（代码）
  - 市场
  - 可比业务
  - 行业地位
  - 相关业务进展
  - 商业模式
  - 目标客户群体
  - 核心产品

- Markdown 表头必须保持可正常渲染，A 股示例：

```markdown
| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 市值 | 商业模式 | 目标客户群体 | 核心产品 |
|:--|:--|:--|:--|:--|:--|:--|:--|:--|:--|
```

港美股示例：

```markdown
| 竞争关系 | 公司（代码） | 市场 | 可比业务 | 行业地位 | 相关业务进展 | 商业模式 | 目标客户群体 | 核心产品 |
|:--|:--|:--|:--|:--|:--|:--|:--|:--|
```

- 不能只留下口语描述。
- 不能用 `Comparable peer A/B/C`。
- 不能用英文表头。
- 不能大面积用 `—` / `N/A` / `未披露` 填充。
- 同业比较最低要求：
  1. 目标公司必须为第一行，且只能出现一次；
  2. 至少 3 家有独立 peer-specific 来源的真实可比公司；
  3. 每个可比公司必须经过证券解析、上市状态确认和业务重合验证；
  4. 客户、供应商、合作方、投资方和未上市主体不能作为 peer；
  5. 相关业务进展必须具体可验证，且引用 peer 自己的材料；引用证据必须包含该 peer 公司名或 ticker；
  6. 表格必须能被 Markdown 正常渲染；
  7. 表头必须中文。
- 稀疏数据规则：
  - 结构化表格若只剩 0 或 1 个有效值，删除整行。
  - 若整列只剩 0 或 1 个有效值，删除整列。
  - 删除后若仍不足以支撑 2 个有效指标或 2 个比较维度，删除整张表。
  - 不能用空白、`N/A`、`未披露`、`--`、`待补充` 大面积充数。

### 5.7 Chapter 9 Data-Availability Rules
- 第九章各小节（9.1-9.4）在无可用数据时必须**整节跳过**，不得保留空壳标题或”暂缺”占位符。
- 9.1 市场一致预期：`research_sec_coredata` 接口无数据 → 跳过整节。
- 9.2 各机构盈利预测：`research_sec_foredata` 接口无数据 → 跳过整节。
- 9.3 估值分析：`diagnosis_valuation_rank` 所有估值维度均无效 → 跳过整节（不调用 LLM）。
- 9.4 情景推演：无一致预期 EPS/PE 且研报/纪要无可提取的业务驱动变量 → 跳过整节（不调用 LLM）。
- 四小节全空时，整章 `## 9` 不出现。
- 9.4 情景推演生成后校验：核心变量必须含具体数字+`[N]` 引用，情景表禁止模板话术；不合格则 LLM 补写一次，仍失败则删除 9.4 空壳（fail-closed）。

### 5.8 Scenario Analysis Content Rules
- 情景推演必须写出可验证公式、基础数据、核心假设和单位。
- 不能只写”乐观 / 中性 / 悲观”三档而没有具体数值。
- 每个情景至少给出 2-3 个核心变量，必须能够算出目标值或估值区间。
- 情景推演里的核心变量、核心假设和经营含义有 `[N]` 引用即可，不额外写”来源：公司年度报告””来源：行业一致预期”等括号来源说明。
- 情景推演不得输出”基于[N]推算””内部测算”等过程标签；估值含义直接写 `EPS＝X.XX元 × PE=Yx = Z.ZZ元` 算式。
- 情景推演表同一单元格内的多个小点必须用 `<br>` 换行，不能挤在同一长句里。
- 若使用模型推导，正文必须保留推导公式和 sanity check，但不输出内部过程标签。
- 无法完整复核时，只保留定性判断，不要报出不可验证的数字。

### 5.9 Final Normalization
- 港美股生成正文后执行脚本内置的引用闭环、表格清洗、章节连续重编号和 DOCX 导出，不再运行独立 post-repair/checker 阻断链路。
- A 股仍按 A 股 writer 的自动修复与质量检查规则执行。
- 港美股检查发现的空章节、空表、空预测、空催化、空情景、空比较表应在 writer 内 fail closed、省略并重编号，或写入 `generation_status.json` 的降级原因，不可硬留空壳。

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

