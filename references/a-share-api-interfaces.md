# Datayes API接口参考

本文档列出公司一页纸所需的所有Datayes API接口英文名及调用说明。

**重要**：调用任何接口前，必须先通过元信息接口获取其实际URL、HTTP方法和参数：
```
GET https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api?nameEn={nameEn}
Authorization: Bearer {DATAYES_TOKEN}
```

---

## ⚠️ ticker 格式速查（2026-04-09 更新）

**通用规则（优先级最高）**：凡接口参数中有名为 `ticker`、`secCode`、`secCodeList` 等股票代码字段时，**一律使用 6 位纯数字代码，不加 `.SH`/`.SZ` 后缀**。

传错会导致 404 或空数据：

| 格式 | 示例 | 适用接口 |
|:-----|:-----|:---------|
| **6位纯数字（无后缀）** | `600030` | **全部接口**，无例外——使用 `stock_search` 返回的 `entity_id` 字段原值 |

- `stock_search` 的响应 code 字段为 `200`（非通用的 `1`），message 为 `Success`（大写S）；`entity_id` 字段即为6位纯数字股票代码，**后续所有接口只用这个字段，禁止使用任何含 `.SH`/`.SZ` 的字段**
- `batchGetReportContent` 的 report_id 取自 research_search 返回的 `list[i]["data"]["id"]`（嵌套层级）

---

## API接口列表

### 1. 股票搜索
- **nameEn**: `stock_search`（底层是 gaea 语义/向量搜索）
- **用途**: 通过**公司名称**获取标准化股票代码（如 600519）和公司简称
- **必填参数**: `data_type`（下划线，股票填 `"1"`、指数填 `"6"`）、`query`、`topK`，缺一即失败
- **🔴 只能按名称搜，不能按6位代码搜**：用代码当 `query` 会被当成语义文本，返回数字谐音的无关结果（实测 `000848`→三六零、`600519`→二六三，且不同代码常返回**相同**垃圾）。需要代码反查名称时，改走 `Ashare_info`（取 `companyName`）再用全称反查本接口回收简称——脚本 `a_share_fetch_data.py` 已内置该逻辑，**优先用脚本，不要手搓**。
- **响应解析**: 命中结果在 `data.hits[]`，每条含 `entity_id`（6位纯数字代码）、`name`（简称）、`score`（相关度，精确命中=1.0）；按 `score` 降序，取 `hits[0]`。
- **⚠️ 响应格式**：该接口成功时响应体的 `code` 字段为 `200`（而非通用的 `1`），`message` 为 `Success`（大写S），解析成功判断时需兼容两种 code 值

### 2. 获取报告期
- **nameEn**: `ticker_period`
- **用途**: 获取当前股票的最新报告期（财务报告期信息）
- **输入**: ticker 为 **6位纯数字代码**（如 `600030`），**不带** `.SH`/`.SZ` 交易所后缀，作为 URL 路径参数传入

### 3. 主营业务占比（图表数据）
- **nameEn**: `main_composition_ratio`
- **用途**: 获取主营业务各板块收入占比数据，用于业务拆分
- **参数说明**:
  - 年报: `reportType=A`，`year`取最近3年
  - 最新2期年报: `reportType=LAST`

### 4-7. 关键数据图谱系列
- **nameEn**: `stock_financial_indicator_revenue` — 营业收入及同比
- **nameEn**: `stock_financial_indicator_earning_structure` — 营收结构分布
- **nameEn**: `stock_financial_indicator_net_profit` — 扣非归母净利及同比
- **nameEn**: `stock_financial_indicator_gross_margin` — 主营业务毛利率
- **用途**: 提供可视化图表数据，用于财务趋势分析（含数据和图表）
- **参数**: 股票代码 + 报告期

### 8. 主营构成（业务明细）
- **nameEn**: `getFdmtMoStdItem`
- **用途**: 各业务板块的收入/毛利率明细，用于业务拆分章节（近3年年报）
- **参数**: `ticker`（6位代码）, `classifCD=2`（按产品分类）, `beginDate`/`endDate`（日期范围）
- **返回结构**: 扁平数组，每条记录对应一个报告期 × 一个业务段
  - `itemID=0` 且无 `itemIDSuperior` → 合计行（总收入）
  - `itemIDSuperior=0` 且 `itemID!=0` → 一级业务子项（直接挂在合计下）
  - 关键字段：`itemName`（业务名）、`revenue`（元，÷1e8得亿）、`revYOY`（同比%）、`grossMargin`（毛利率%）
- **重要**: 分板块业务情况表格的数据来源，必须使用接口返回的精确数字

### 9. 财报摘要（核心财务数据）
- **nameEn**: `fdmtNew`
- **用途**: 完整财务报表数据（利润表、资产负债表、现金流量表、关键财务指标）
- **ticker格式**: 6位股票代码（如 `600030`），作为 URL **路径参数**传入
- **调用逻辑**（重要，依赖 ticker_period 结果）：
  - **若最新财报季为年报**：`reportPeriodType=A`，`period=3`（近3年年报）
  - **若最新财报季不是年报**：
    1. 用 ticker_period 返回的最新季度或半年报告期，获取最新一期财报摘要
    2. 同时 `reportType=A`，`period=2`，获取前两年完整年报
    3. 将最新季报数据 + 前两年年报合并用于分析
- **重要**: 这是财务分析章节的主要数据来源，数据均为精确财报数字，报告中不得对其进行模糊化处理

### 10. 盈利指标（ROE）
- **nameEn**: `fdmt_indi_rtn`
- **用途**: 盈利能力指标，重点用于获取ROE及杜邦分解数据
- **必填参数**: `ticker`、`beginDate`、`endDate`（日期格式 `YYYYMMDD`）
- **采集口径**: 近三年完整年度起始日至当日；不得传入未在接口元信息定义的 `period` 参数

### 11. 市盈率历史百分位
- **nameEn**: `diagnosis_pe_valuation`
- **用途**: 当前PE/PB估值水平相对历史的百分位分析
- **注意**: **只需提取comment字段**（文字分析），作为估值判断参考

### 12. 同业估值排名
- **nameEn**: `diagnosis_valuation_rank`
- **用途**: 与同行业公司的估值比较排名
- **输入**: 股票代码

### 13. 机构调研记录
- **nameEn**: `Org_survey`
- **用途**: 查看机构近期调研的核心问题，了解市场关注焦点
- **注意**: **只取近一月**
- **⚠️ 参数名称**：日期参数为 `startDate`/`endDate`（**不是** `beginDate`），翻页参数为 `pageNow`（**不是** `pageNo`）

### 14. 十大股东
- **nameEn**: `Ashare_tenHolders`
- **用途**: 获取最新期十大股东信息（股东结构、持股比例变化）
- **ticker格式**: **6位纯数字代码**（如 `600030`），**不带** `.SH`/`.SZ` 后缀
- **参数**: 最新报告期

### 15. 董监高信息
- **nameEn**: `Executive_information`
- **用途**: 董事会、监事会及高管团队信息（管理层稳定性评估）
- **参数**: 最新期

### 16. 公司概况
- **nameEn**: `Ashare_info`
- **用途**: 公司基础信息（注册地、行业分类、主营业务描述、成立日期等）
- **ticker格式**: **6位纯数字代码**（如 `600030`）

### 17. 分红转增历史
- **nameEn**: `Ashare_bonus`
- **用途**: 历史分红记录（股息率评估）
- **注意**: **只取近一月**

### 18. 管理层讨论
- **nameEn**: `management_discussion`
- **用途**: MD&A讨论内容（经营分析、战略展望）
- **ticker格式**: **6位纯数字代码**（如 `600030`）
- **参数**: `startDate`/`endDate`（格式 yyyyMMdd，**不是** `beginDate`），以当天日期为结束日期，往前推4个月为起始日期；需传 `pageNo=1`、`pageSize=20`
- **使用规则**: 财报发布后14天以内为核心参考；更早的仅作背景

### 19. 机构持股明细
- **nameEn**: `Ashare_orgHoldingdetail`
- **用途**: 各类机构（公募/险资/外资）持仓情况及变化
- **参数**: 最新期
- **⚠️ URL结构**: 元信息 URL 末尾含 `{type}` 路径参数（0=全部，1=公募，2=险资等），**必须替换到 URL 路径中**，不放入 query params
- **⚠️ Query参数**: `ticker`（6位纯数字）、`year`（年份）、`quarter`（季度1-4），三个参数均为必传
- **示例**: `/insStockInfo/0?ticker=601888&year=2025&quarter=3`

### 20. 公告搜索
- **nameEn**: `announcement`
- **用途**: 获取近期重要公告
- **参数说明**:
  - 时间范围: 最近1周
  - **重要**: 过滤掉定期报告（年报/季报/半年报）——财务数据通过fdmtNew获取
  - 通过`announcement_type`确定目标公告类型范围
  - 关注: 重大事项、战略调整、重要合同、股东增减持、回购等

### 21. 公告详情
- **nameEn**: `getAnnouncementDetail`
- **用途**: 获取具体公告全文
- **输入**: 公告的id（由announcement接口返回）
- **优先使用htmlUrl字段**（可解析图表和表格）
- downloadUrl为原始PDF（备用）

### 22. 公告类型查询
- **nameEn**: `announcement_type`
- **用途**: 获取公告类型分类体系，用于精确筛选所需类型公告

### 23. 研报搜索
- **nameEn**: `research_search`
- **用途**: 搜索最新卖方研究报告
- **双路径策略**:
  - 路径1: `ticker` = 6位股票代码，支持逗号分隔多个代码
  - 路径2: `query` = 股票名称关键词
  - 两路合并去重，确保相关性
- **⚠️ HTTP方法**: **POST**，请求体 JSON 格式
- **⚠️ 请求体格式（两路径通用）**:
  - 路径1: `{"type": "EXTERNAL_REPORT", "ticker": "601888", "reportType": "COMPANY", "pageNow": 1, "pubTimeStart": "20260314", "pubTimeEnd": "20260414", "sortOrder": "desc"}`
  - 路径2: `{"type": "EXTERNAL_REPORT", "query": "中国中免", "reportType": "COMPANY", "pageNow": 1, "pubTimeStart": "20260314", "pubTimeEnd": "20260414", "sortOrder": "desc"}`
  - **`type` 字段必填**，值为 `"EXTERNAL_REPORT"`；翻页参数为 `pageNow`（**不是** `pageNo`）
- **时间参数**: `pubTimeStart`/`pubTimeEnd`，格式 `yyyyMMdd`；**优先近1个月，无结果时自动扩大至近3个月**
- **排序**: `sortOrder` 默认 `"desc"`（按发布时间降序）
- **参数**: `reportType=COMPANY`（公司研报类型），至少包含一篇深度报告
- **返回数量上限**：每路最多取 20 条，合并去重后选最优的10篇
- **优先级**: 头部券商（中金、中信、高盛、摩根士丹利、花旗等）
- 若第1次搜索（近1月）无结果则扩大至近3个月重试

### 24. 研报摘要和结构化基本信息
- **nameEn**: `getReportDetail`
- **用途**: 获取研报摘要和基本信息
- **⚠️ URL结构**: 元信息返回的 URL 中含示例 ID（如 `/externalReport/5588490/info`），**必须将路径中的示例 ID 替换为真实 report ID**，不能作为 query 参数传入
- **输入**: 研报id（由research_search返回，取 `list[i]["data"]["id"]`）
- **关键响应字段**:
  - `articleTitle` — 研报标题
  - `textAbstract` — 研报摘要全文（HTML格式）
  - `orgName` — 券商机构名称
  - `rating` — 投资评级（如"买入"）
  - `ratingChange` — 评级变动（如"维持"）
  - `targetPrice` — 目标价
  - `publishTimeReadable` — 发布日期（格式 yyyy-MM-dd）
  - `authorList` — 分析师列表

### 25. 研报全文
- **nameEn**: `batchGetReportContent`
- **用途**: 获取研报全文文本
- **输入**: 研报id（由research_search返回）
- **⚠️ ID提取路径**: research_search 返回的 `list` 中每项结构为 `{"type": "EXTERNAL_REPORT", "data": {"id": ..., "title": ...}}`，report_id 取 `list[i]["data"]["id"]`（不是顶层 `list[i]["id"]`）

### 26. 研报图表数据
- **nameEn**: `report_graph`
- **用途**: 提取研报中的图表数据（可能含财务预测、行业数据）
- **输入**: 研报id（由research_search返回）

### 27. 文本转图表
- **nameEn**: `data_to_image`
- **用途**: 将文本格式的财务数据转换为ECharts图表图片，用于报告可视化
- **触发时机**: 在财务数据采集完成后，优先为以下四组数据生成图表：
  1. 营业收入及同比（stock_financial_indicator_revenue 数据）
  2. 归母净利润及同比（stock_financial_indicator_net_profit 数据）
  3. 分业务毛利率（stock_financial_indicator_gross_margin 数据）
  4. 营收结构（stock_financial_indicator_earning_structure 数据）
- **请求**: POST，body `{"text": "...财务数据文本..."}`
- **返回值**: `{"chart_urls": ["https://..."], "message": "图表生成成功"}` — 使用 `chart_urls[0]` 插入报告
- **失败处理**: 若 API 返回失败，在对应位置以表格 + 文字描述替代，不阻塞报告生成

### 28. 市场一致预期
- **nameEn**: `research_sec_coredata`
- **用途**: 获取市场各机构对目标股票的盈利预期综合数据
- **ticker格式**: **6位纯数字代码**（如 `600030`），**不带** `.SH`/`.SZ` 后缀
- **⚠️ 参数名称**: 股票代码参数为 `tickers`（**不是** `ticker`）
- **输出**: 一致预期EPS/营收/净利润

### 29. 盈利预测明细
- **nameEn**: `research_profit_adjust`
- **用途**: 各券商最新盈利预测明细（营收/归母净利/EPS）
- **ticker格式**: **6位纯数字代码**（如 `600030`），**不带** `.SH`/`.SZ` 后缀
- **⚠️ 必传参数（6个全部传入，缺少时间参数会返回全历史最旧数据）**:
  - `tickers`: 6位股票代码（**不是** `ticker`）
  - `foreYears`: 预测年度，逗号分隔，如 `2026,2027,2028`（取当前年份起未来3年）
  - `pubTimeStart`: 研报发布开始时间，格式 `yyyyMMdd`，取近1个月
  - `pubTimeEnd`: 研报发布结束时间，格式 `yyyyMMdd`，取今日
  - `sortField`: 排序字段，固定传 `thisWriteDate`
  - `sortType`: 排序方向，固定传 `desc`（降序，最新在前）
  - `pageSize`: 建议传 `20`

### 30. 盈利预测历史数据（注：A股专用，港美股已停用）
- **nameEn**: `research_sec_foredata`
- **用途**: 获取机构对目标股票的历史盈利预测数据（预测值 vs 实际值），用于评估机构预测准确性
- **ticker格式**: **6位纯数字代码**（如 `600030`），**不带** `.SH`/`.SZ` 后缀
- **⚠️ 日期格式**: 该接口使用 `yyyy-MM-dd` 格式（不同于其他接口的 `yyyyMMdd`）
- **⚠️ 参数名称**: 股票代码参数为 `tickers`（**不是** `ticker`）
- **⚠️ 注意**: 该接口仅 A 股可用；港美股已将其列入停用接口列表

### 31. 会议纪要搜索
- **nameEn**: `meeting_search`
- **用途**: 搜索与目标股票相关的路演、业绩说明会、投资者调研会等会议纪要列表，从结果中优先选近一个月的高相关纪要。
- **HTTP方法**: POST（以元信息接口返回为准）
- **请求体参数**:
  - `ticker`: 6位股票代码（如 `600030`）
  - `input`: 股票简称（如`中信证券`）
  - `pageSize`: 每页条数（建议20）
  - `meetingType`（可选）: 会议类型过滤（如"公司分析"）
- **返回关键字段**:
  - `id`: 会议纪要唯一ID，**后续调用 getMeetingSummaryDetail 必须传此值**
  - `ticker[]`: 相关股票代码列表
  - `secShortName[]`: 相关股票简称列表
  - `industry[]`: 行业分类
  - `stockInfo[].pe`, `stockInfo[].marketCap` 等市场指标
- **⛔ 重要**: 此接口只返回会议列表索引，**不含会议实质内容**，必须用 id 调用 getMeetingSummaryDetail 获取全文

### 32. 会议纪要详情
- **nameEn**: `getMeetingSummaryDetail`
- **用途**: 获取单条会议纪要的完整内容（AI整理摘要 + 原始转写全文）
- **HTTP方法**: GET
- **输入**: 纪要id（由`meeting_search`返回）
- **返回关键字段及阅读顺序（重要）**：
  1. `aiSummary.aiOverview`: AI整理的会议核心观点，**最先阅读**，快速了解会议主旨
  2. `aiSummary.aiQa`: AI整理的问答对，含分析师完整提问 + 管理层完整回答，**信息密度最高，重点提取**
  3. `text[]`: 原始发言逐句转写（按说话人和句子拆分），必要时作为补充验证
- **⚠️ 注意**: 不要将目录、标题等结构性文字误当正文；aiSummary 是最高效的阅读路径，text 是原文备查

### 33. AI搜索-同业素材召回
- **nameEn**: `getMaterialsV2`
- **用途**: 萝卜AI投研搜索，输入用户 Query，召回研报、会议纪要、市场点评、资讯、公众号、指标库等素材，返回相关的搜索片段和溯源URL。**专用于同业对比表格**：用目标公司与可比同业的财务指标对比问题召回有效素材，解决同业数据靠模型估算的问题。
- **HTTP方法**: POST
- **URL**: 通过元信息接口 `https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api?nameEn=getMaterialsV2` 获取，禁止硬编码业务 URL（元信息返回的 httpUrl 格式为 `https://gw.datayes.com/aladdin_proxy/aladdin_info/web/gptMaterials/v2`）。
- **请求体参数**:
  - `queryScope` (String, 可选, 默认 `"research"`): 素材范围，多值用英文逗号分隔。枚举：`news`（资讯）/ `research`（研报）/ `researchTable`（研报图表）/ `announcement`（公告）/ `meetingSummary`（会议纪要）/ `indicator`（数据指标）/ `marketView`（市场点评）/ `wechat`（微信公众号）；**同业比对取 `"research,researchTable,meetingSummary"`**
  - `question` (String, **必填**): 自然语言问题。示例："对比{company_name}与主要可比同业公司在营收、净利润、PE、PB、ROE等核心财务指标的最新数据"
  - `rewriteQuestion` (Boolean, 可选, 默认 `true`): 是否对问题优化后再搜索；`true` 提升精准度但响应稍慢，`false` 直接使用原问题
  - `size` (Integer, 可选, 默认 `10`, 范围 1-20): 召回素材数量，超出按上限20返回
  - `startTime` (String, 可选): 素材起始时间，格式 `yyyyMMdd` 或 `yyyyMMddHHmmss`
  - `endTime` (String, 可选): 素材截止时间，格式同上
  - `rewriteModel` (String, 可选, 默认 `"FLAGSHIP"`): 改写问题模型。枚举：`FLAGSHIP`（旗舰）/ `STANDARD`（标准）/ `DATAYES`（通联自研）
- **返回结构**: `data` 数组，每条素材包含：
  - `dataType` (String): 数据类型（`research`/`meetingsummary`/`researchtable`/`news`/`indicator`/`announcement`/`other`）
  - `title` (String): 素材标题
  - `resource` (String): 素材引用信息（含内部ID、类型、页码等）
  - `text` (String): 素材相关片段（核心字段，含原始数据）
  - `score` (Number): 相关性得分（0-1，越接近1越相关）
  - `url` (String): 素材前端网页地址
  - `metadata` (Object): 元数据
    - `id` (String): 素材唯一ID
    - `reportType` (String): 研报类型（仅 research 类返回）
    - `publishTime` (String): 发布日期
    - `organization` (String): 研究机构
    - `analyst` (String): 分析师
    - `source` (String): 资讯来源或公众号ID（仅 news 类返回）
    - `category` (String): 公告类型（仅 announcement 类返回）
    - `industry` (String): 行业类型
- **调用时机**: 在 fetch_data.py 的 Phase 3 并行采集阶段调用，结果存入 JSON 的 `peer_materials` 字段
- **使用方式**: report_writer.py 的 `gen_peer_table()` 读取 `key_data["peer_materials"]`，将 `text` 字段传入 LLM prompt 作为同业数据来源
- **⚠️ 注意**: 超时60s；若失败静默跳过，gen_peer_table 降级到研报摘要作为数据来源

---

## API调用最佳实践

### 并行调用原则
- 元信息查询阶段：所有API名称并行查询
- 数据采集阶段：6个模块并行启动
- 单模块内独立接口并行调用

### 错误处理
- API返回空数据：报告正文中忽略该项（优先从研报/纪要补充），**不在正文标注"接口无数据"或"数据暂缺"**；在报告输出完成后于对话窗口统一列出失败接口的 curl 调试命令
- Token无效：立即提示用户检查token
- 接口超时：重试1次后如仍失败，跳过该接口继续其他数据采集

### 报告期参数规范
- 最新年报: `reportType=A`，取当前年份或上一年
- 最新季报: `reportType=Q`，取最新季度
- 近期任意: `reportType=LAST`，获取最近1个已发布期（具体参数以元信息接口返回为准）

### 数据质量验证
- 财务数据交叉验证：fdmtNew与research_profit_adjust中历史数据应一致
- 若研报预测与一致预期差异>15%，在报告中标注并说明可能原因
