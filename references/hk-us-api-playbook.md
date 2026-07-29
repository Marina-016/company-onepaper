# Datayes API 编排手册

## 运行时铁则

- 调业务接口前必须先查元信息：

```text
GET https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api?nameEn={nameEn}
Authorization: Bearer {DATAYES_TOKEN}
```

- 只使用元信息返回的 `httpUrl`、`httpMethod`、`parametersInput[].location`。不要从记忆中拼业务 URL。
- Python 中使用 `urllib.request` 处理请求和 JSON；响应在内存中解析。不要把 token 打印到日志。
- 成功判断兼容 `code == 1` 和 `stock_search` 的 `code == 200`。

## 代码规范

- 港股：使用 5 位代码，如 `00700`。用户输入 `00700.HK` 时去掉后缀。
- 美股：使用 ticker，如 `NVDA`。用户输入 `NVDA.O`、`NVDA.N`、`NVDA.US` 时去掉后缀。
- `stock_search` 对港股名称较有效，但港股纯代码和美股名称/代码可能返回无关结果；用户明确给 ticker 时以用户输入为准。

## 推荐 API

### 综合素材检索

- `getMaterialsV2`：查询个股资料时的第一入口。范围只选择研报、纪要、市场点评和公众号，用公司名、ticker、产品、财务指标、催化事件等 query 同时检索，优先读取返回的相关片段、素材 ID、发布日期、来源类型和机构。
- 默认使用 `scripts/fetch_materials.py` 内置的六组 query，或单独运行 `scripts/fetch_materials_v2.py`：
  - 近况/催化：业绩、指引、资本市场事件、监管、产品发布。
  - 投资逻辑：增长驱动、商业模式、竞争优势、主要风险。
  - 业务/财务：业务拆分、收入、毛利率、ARR、客户、订单、预测。
  - 产销生态：客户、供应商、渠道、生态伙伴、产业链合作。
  - 估值/分歧：目标价、盈利预测、多空观点、同业对比。
  - 市场关注：调研问题、解禁、监管、竞争、下一次验证点。
- 固定素材范围为 `research,meetingSummary,marketView,wechat`，不要把普通新闻混入 `getMaterialsV2` 主召回；新闻用公开检索或公告接口单独补充。
- 使用方式：先用 `getMaterialsV2` 快速定位“哪些材料、哪些段落”与 query 相关，再对最关键的研报/纪要调用 `batchGetReportContent`、`getReportDetail`、`report_graph` 或 `getMeetingSummaryDetail` 补证。
- 写作限制：`getMaterialsV2` 返回的片段可以作为正文来源，但参考资料必须记录对应素材类型、日期、ID、机构/发布方、标题和 API：`getMaterialsV2（综合素材检索）`；如果随后读取了全文/详情，参考资料 API 改为全文/详情接口。

### 公开新闻补充

- 公开新闻不是主素材源，只用于补齐最新事件事实，尤其是指数调整、上市/再融资、监管、回购、重大合作、产品发布和公司公告被媒体报道的情形。
- 若运行平台具备联网检索能力，生成报告时默认做一次近 1-3 个月公开检索；优先公司/交易所/监管披露，其次权威财经媒体。若平台不能联网，不执行公开新闻搜索，也不要把未验证消息写入报告。
- 公开来源参考资料格式：`[序号] 公开新闻 | YYYY-MM-DD | 发布方 | 标题 | URL`。

### 标准化

- `stock_search`：公司名称与代码关联。参数：`dataType=1`、`query`、`topK=5`。

### 港股结构化财务

- `getHkFdmtIsPit`：港股利润表（Point in time）。API ID 925。参数：`ticker` 或 `secID`，可选 `beginDate/endDate`、`publishDateBegin/publishDateEnd`、`repTypeDate`、`pagenum/pagesize`。
- `getHkFdmtBsPit`：港股资产负债表（Point in time）。API ID 924。参数：`ticker` 或 `secID`，可选 `beginDate/endDate`、`publishDateBegin/publishDateEnd`、`repTypeDate`、`pagenum/pagesize`。
- `getHkFdmtCfPit`：港股现金流量表（Point in time）。API ID 923。参数：`ticker` 或 `secID`，可选 `beginDate/endDate`、`publishDateBegin/publishDateEnd`、`repTypeDate`、`pagenum/pagesize`。

港股财务表格优先使用以上三个 PIT 报表接口的真实返回。接口没有的分业务收入、客户、供应商、指引、non-IFRS 调整项，从研报全文、图表、业绩公告和会议纪要抽取。

### 研报

- `research_search`：POST。核心参数：
  - `type=EXTERNAL_REPORT`
  - `reportType=COMPANY`
  - `ticker` 或 `query`
  - `exchangeCode`: 港股 `XHKG`；美股 `AMXO,XNAS,XNYS`
  - `pubTimeStart/pubTimeEnd`: `yyyyMMdd`
  - `pageNow=1`、`pageSize=20`、`sortOrder=desc`
- `getReportDetail`：GET path 参数 `reportId`，获取摘要、评级、目标价、机构、页数。
- `batchGetReportContent`：POST body `reportIds`，一次最多 10 个，获取研报全文。
- `report_graph`：GET query `reportId`，获取研报图表和表格数据。
- `core_viewpoint/`：GET query `rrId`，快速获取研报 AI 解读，可作为辅助筛选，不替代全文。

选择规则：

- ticker 路径和 query 路径都跑，合并去重。
- 优先近 180 天；不足 5 篇时扩大到 365 天。
- 保留公司深度、业绩点评、事件点评、海外机构报告和包含图表的报告。
- 列表 `abstractText` 可用于筛选，正文必须优先来自 `batchGetReportContent`、`getReportDetail`、`report_graph`。

### 会议纪要

- `meeting_search`：POST。参数：
  - `ticker`
  - `input`: 公司名、英文名或产品关键词
  - `marketType`: `港股` 或 `美股`
  - `meetingType`: 可选 `业绩说明会,机构调研,电话会议`
  - `pubTimeStart/pubTimeEnd`: `yyyyMMddHHmmss`
  - `pageNo=1..3`、`pageSize=20`
- `getMeetingSummaryDetail`：GET query `id`，获取 `aiOverview`、`keyData`、`aiQa` 和原始转写 `text`。

阅读顺序：`aiOverview/keyData` 快速定位 → `aiQa` 抽取市场关注和管理层回答 → `text` 补证关键原话。

### 公告

- `announcement_type`：公告类型分类。
- `announcement`：POST。参数：`ticker`、`input`、`beginDate/endDate`、`pageNow`、`pageSize`、可选 `category`。
- `getAnnouncementDetail`：GET path 参数 `id`。优先读取 `htmlUrl`，PDF 作为备用。

港股优先看业绩、回购、重大交易、股权变动、监管诉讼、公司行动。美股公告覆盖不稳定时不要硬补，转用研报/纪要。

### 停用接口

- 不要调用以下接口：`research_sec_foredata`、`stock_evaluationAnalysis`、`diagnosis_valuation_rank`、`Stock_OnePage`、`market_snapshot`、`market_HK`。
- 港股报告的预测、估值、行情和市值口径从 `getMaterialsV2`、研报全文/图表、公告/公司披露和公开来源补充，不再用上述接口。

## 缺口处理

- 美股没有可用的三表结构化接口；三大表、完整财务指标、分业务历史序列缺失时，只能从研报全文、图表、财报点评、公司公告/IR 材料中抽取；没有明确来源就省略。
- 港美股正文缺章时，writer 应省略空章节并对后续章节连续重编号；`generation_status.json` 记录 `section_number_mapping`、`omitted_original_sections` 和 `reference_display_section`。
- §9/§10 这类高风险章节不得用目标公司原文片段硬填同行或多空证据；LLM 与 compact retry 均失败时 fail closed，由重编号消除跳号。
- 不要把接口失败写入报告正文；可在最终进度里简短说明“部分接口无数据，已用研报/纪要补充”。
- 财务单位必须保留来源口径：人民币、港元、美元；IFRS、US GAAP、non-GAAP、经调整口径不要混用。
