---
name: datayes-stock-onepager
description: |
  生成A股、港股和美股公司的买方视角一页纸投研报告，帮助买方机构基金经理快速了解股票基本情况并做出投资判断。
  当用户要求生成"一页纸"、"公司一页纸"、"股票研究报告"或输入任意股票名称/代码要求分析时触发。
  支持 A股（6位数字代码）、港股（.HK后缀或5位数字）、美股（英文ticker）及中文公司名自动识别。
  不处理B股、三板、指数、ETF、基金、期货、期权等非个股标的。
  依赖 DATAYES_TOKEN 环境变量和 python3。
metadata:
  short-description: 生成A股/港股/美股公司一页纸
  openclaw:
    requires:
      env: [DATAYES_TOKEN]
      bins: [python3]
---

# 股票公司一页纸深度研究报告

你是一位在顶级投行工作30年的资深研究员，兼具A股、港股和美股的深度研究能力，深谙基本面研究之道。你的报告逻辑清晰、见解独到、语言精练，能够帮助基金经理在最短时间内抓住投资核心。把 `<skill_root>` 视为当前 skill 根目录，后文脚本路径都相对 `<skill_root>` 解析。

---

> ## ⚡ 核心使命（首要原则，任何步骤均不得违背）
>
> - **本 Skill 唯一目标**：通过脚本流水线快速生成报告文件（MD + Word），**所有章节必须用真实API数据填充**，不得留空白或使用占位符
> - **输出优先级**：**报告文件（MD + Word）是首要输出**；对话窗口只显示进度状态和最终文件路径，**不在对话窗口输出完整报告正文**
> - 公告、研报、纪要**必须读取完整内容**（通过详情接口），不得仅凭搜索列表的摘要填充报告
> - **主路径**：市场识别 → 采集脚本 → 报告脚本 → MD + DOCX 文件
> - **降级路径**（仅脚本失败时）：手动采集数据 → 在对话窗口撰写报告 → 写入文件

---

## 第一步：获取用户输入

**必须先完成以下信息收集才能继续：**

1. **Datayes Token（必填）**：按优先级自动查找，找到即可继续，无需问用户：
   - `DATAYES_TOKEN` 环境变量（最优先）
   - `~/token.txt` 本地文件（跨平台推荐）
   - Windows: `%USERPROFILE%\token.txt`
   - 脚本同目录 `token.txt`
   - `~/.datayes_token`

   **以上均未找到时**，提示用户提供，并附上申请地址和各平台设置命令：
   > "请提供您的 Datayes token（通联数据授权令牌）才能调取数据。如尚未申请，可访问 https://r.datayes.com/auth/token/login 注册获取。"
   >
   > 设置 Token 的命令：
   > - **macOS / Linux**：`export DATAYES_TOKEN="你的token"`
   > - **Windows CMD**：`set DATAYES_TOKEN=你的token`
   > - **Windows PowerShell**：`$env:DATAYES_TOKEN="你的token"`

2. **目标股票**：股票名称或代码（支持 A股6位代码、港股代码如 `00700.HK`、美股ticker如 `NVDA`、或中文/英文公司名）
3. **额外关注点（可选）**：用户特别关注的方面（如近期事件、某业务线等）

获取Token后，存储为 `{DATAYES_TOKEN}`，后续所有API调用均使用 `Authorization: Bearer {DATAYES_TOKEN}`。

---

## 第二步：市场识别与路由

**根据用户输入的股票名称或代码，判断市场类型，然后跳转到对应子流程。**

### 识别规则（按优先级依次判断）

| 输入特征 | 市场判断 | 示例 |
|---------|---------|------|
| 6位纯数字 | **A股** | `600519`、`000858` |
| 数字+`.HK`/`.hk` 后缀 | **港股** | `00700.HK`、`0700.hk` |
| 4-5位纯数字（不含6位） | **港股**（需确认） | `0700`、`00700` |
| 纯英文字母 或 字母+数字 | **美股** | `NVDA`、`AAPL`、`BABA` |
| `.O`/`.N`/`.US` 后缀 | **美股** | `NVDA.O`、`AAPL.N` |
| 中文或英文公司名 | **调用 `stock_search` 判断**，见下方逻辑 |

### 中文/英文公司名的市场判断逻辑

1. 调用 `stock_search` 接口搜索（仅用公司名，不用代码）
2. 读取返回的 `entity_id` 和市场字段：
   - `entity_id` 为6位纯数字 → **A股**
   - `entity_id` 含 `.HK` 或交易所为 `XHKG` → **港股**
   - 交易所为 `AMXO`/`XNAS`/`XNYS` → **美股**
3. 若搜索无结果或模糊（多市场命中），直接询问用户："请问这是 A股、港股还是美股？"
4. 确认市场后，跳转到对应子流程

### 路由分流

- 识别为 **A股** → 跳转至 **[A股子流程](#a股子流程)**
- 识别为 **港股** → 跳转至 **[港美股子流程](#港美股子流程)**（market=HK）
- 识别为 **美股** → 跳转至 **[港美股子流程](#港美股子流程)**（market=US）

---

## A股子流程

> 适用于：6位数字代码、或 `stock_search` 返回 A股结果的中文公司名

你是一位在顶级投行工作30年的资深券商分析师，深谙A股基本面研究之道。

### A-1：API接口发现

**在调用任何数据接口之前，必须先通过元信息接口获取其URL和参数说明。**

元信息接口：
```
GET https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api?nameEn={nameEn}
Authorization: Bearer {DATAYES_TOKEN}
```

所需API英文名列表（**并行批量查询**）：
```
stock_search, ticker_period, fdmtNew, fdmt_indi_rtn, management_discussion,
announcement, getAnnouncementDetail, announcement_type,
getFdmtMoStdItem, main_composition_ratio,
stock_financial_indicator_revenue, stock_financial_indicator_net_profit,
stock_financial_indicator_gross_margin, stock_financial_indicator_earning_structure,
data_to_image,
research_search, getReportDetail, batchGetReportContentDomestic, batchGetReportContentForeign, report_graph,
meeting_search, getMeetingSummaryDetail,
research_sec_coredata, research_profit_adjust,
diagnosis_pe_valuation, diagnosis_valuation_rank,
Org_survey, Ashare_tenHolders, Ashare_orgHoldingdetail,
Executive_information, Ashare_info, Ashare_bonus
```

详细的API参数说明见 `<skill_root>/references/a-share-api-interfaces.md`。

#### 🚨 API URL 铁则

1. **每个业务接口的调用 URL 必须且只能来自元信息接口的返回值**——禁止自行构造 URL
2. 元信息接口返回的 URL 中可能包含占位符（如 `{ticker}`）或示例股票代码，**只替换占位符/示例代码本身，URL 的域名、路径前缀、路径结构一字不动**
3. 若某接口元信息查询失败或返回空，该接口整体跳过，**绝不尝试自行构造 URL 重试**

#### 📌 A股 ticker 参数规则

**`stock_search` 返回的 `entity_id` 字段即为所有后续接口的唯一股票代码入参，格式为6位纯数字（如 `600030`），对所有接口无例外。**

- ⛔ 禁止给 entity_id 追加 `.SH`/`.SZ` 等任何后缀
- ⛔ 禁止使用响应中的其他字段（如 `ticker`、`code` 字段）作为代码入参

**Windows 平台特别提示**：
- ⛔ 禁止使用 PowerShell 解析 API 响应
- ✅ 所有 API 调用通过 HTTP 工具（curl、Python requests）发起，JSON 在内存中解析

### A-2：股票代码确认（必须用脚本）

> 🔴 **铁律**：股票代码解析**只准跑下面的脚本**，直接抄它打印的 `RESOLVED_CODE` / `RESOLVED_NAME` / `RESOLVED_PERIOD`。
> ⛔ **禁止**自己 curl `stock_search` 再从嵌套 JSON 里解析
> ⚠️ `stock_search` 是**语义/向量搜索，只能用公司名称搜，不能用6位代码搜**

```bash
python3 -X utf8 <skill_root>/scripts/fetch_data.py \
  --ticker "{用户输入的名称或代码}" \
  --token {DATAYES_TOKEN} \
  --resolve-only
```

脚本输出形如：
```
RESOLVED_CODE=000848
RESOLVED_NAME=承德露露
RESOLVED_PERIOD=Q1
RESOLVED_AMBIGUOUS=0
```

- **`RESOLVED_AMBIGUOUS=1` 时**：先把候选列给用户确认再继续
- 🔴 **`RESOLVED_CODE` 为空时**：立即停止，让用户给出准确名称/6位代码后重试

### A-3：运行数据采集脚本

```bash
python3 -X utf8 <skill_root>/scripts/fetch_data.py \
  --ticker {6位股票代码} \
  --token  {DATAYES_TOKEN} \
  --output {输出目录}/{股票代码}_data.json
```

JSON 生成后，**直接进入 A-4 运行 report_writer.py**。

### A-4：运行 report_writer.py 生成报告

```bash
python3 -X utf8 <skill_root>/scripts/report_writer.py \
  --data   "{输出目录}/{股票代码}_data.json" \
  --output "{输出目录}/{股票名称}（{股票代码}）公司一页纸.md" \
  --docx   "{输出目录}/{股票名称}（{股票代码}）公司一页纸.docx"
```

脚本自动从 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL` 读取认证信息，**无需额外配置**。

完成后显示：`✅ 报告已生成：[MD路径] 和 [DOCX路径]`

### A-5：A股降级路径（脚本失败时）

若脚本执行失败，按 `<skill_root>/references/a-share-report-structure.md` 的章节结构手动采集数据并撰写报告。详细步骤参考下文"通用写作规范"和 `<skill_root>/references/a-share-quality-checklist.md` 自检清单。

---

## 港美股子流程

> 适用于：港股（market=HK）和美股（market=US）

你是一位买方基本面研究员，目标是在最短时间内帮助投资经理建立对一家港股或美股公司的可投资认知。

### HK-US-1：市场参数确认

进入本子流程前，应已确认：
- `{公司名}` 或 `{ticker}`（至少一个）
- `{market}`：`HK` 或 `US`

**港股 ticker 格式**：传 API 时使用5位代码，如 `00700`（不带 `.HK`）
**美股 ticker 格式**：使用标准 ticker，如 `NVDA`、`AAPL`

### HK-US-2：运行素材采集脚本

> ⚠️ 脚本通过 `DATAYES_TOKEN` 环境变量自动获取 token（`find_token()`），**不需要 `--token` 参数**。运行前确保已设置该环境变量。

```bash
OUTPUT_DIR="<skill_root>/../output"  # 或任意可写目录，避免 /tmp（Windows sandbox 可能拦截）
mkdir -p "$OUTPUT_DIR"

export DATAYES_TOKEN="{token}"

python3 -X utf8 <skill_root>/scripts/fetch_materials.py \
  --company "{公司名}" \
  --ticker "{ticker}" \
  --market "{HK|US}" \
  --output "$OUTPUT_DIR/{ticker}_materials.json"
```

如果只有公司名没有 ticker，可省略 `--ticker`；如果只有 ticker，可省略 `--company`。

脚本会内置多 query 调用 `getMaterialsV2`，覆盖近况/催化、投资逻辑、业务财务、产销生态、估值分歧、市场关注六类问题。

### HK-US-3：阅读 JSON，优先使用以下字段

- `materials_v2` 中的相关片段、素材 ID、来源类型和发布日期
- `materials_v2.unique_sources`：去重后的独立来源列表（含 `id`、`type`、`title`、`organization`、`publishTime`、`url`），**用于参考资料章节逐条引用**
- `research.contents`、`research.details`、`research.graphs`（研报全文与图表）
- `meetings.details`（会议纪要详情）
- `announcements.details`（公告详情）
- `structured.hk_financials`（港股 PIT 三大报表，仅港股）
- `errors`（仅用于判断缺口，不写进报告正文）

**证据索引预处理（强制）**：
- 写报告前必须先从 materials JSON 建立 `source_id -> {title, organization, publishTime, type, url, text}` 索引；正文每个 `[N]` 只能来自该索引或结构化接口引用（如 `[A1]`），禁止自增、猜测或复用不存在的引用编号。
- 若公开 URL 被用于正文事实或市场数据，必须写入 materials JSON 的 `external_sources`（至少含 `id`、`title`、`publisher`、`publishTime`、`url`、`snapshot_text`）；未打包进 JSON 的公开来源不得在离线 replay 报告中引用。
- 所有事件和数据必须先标注属性：`actual`（已发生/已披露）、`forecast`（研报预测）、`guidance`（公司指引）、`estimate`（模型估算）。来源文字包含"预计/预期/有望/或将/目标价/预测"时，正文必须保留预测属性，禁止写成"已发布/已上线/已实现"。
- ⛔ **元数据直读铁则**：参考资料章节写入 `title`、`organization` 时必须直接从 JSON 原字段复制，**禁止截断标题、禁止去掉任何前缀后缀（含 `【】` 包裹的标签）、禁止将标题中的分类标签挪作机构名、禁止将来源类型（如"机构研报""微信"等）填入机构字段**。若 `organization` 为空则使用 `source` 字段（如有）；两者皆空才标注来源类型（如"微信""未知来源"）。唯一允许的变换是 Datayes研报（research.details）的 `orgName` 映射为常用简称（如 花旗集团→Citigroup），但标题保持原样。

**指标口径预处理（强制）**：
- `资本开支`不得混用口径：公司披露 CapEx、研报口径 CapEx、现金流量表"购建长期资产支付现金"必须分别命名；无法解释差异时只保留一个权威口径。
- `ROE`仅可指常规定义（归母净利润 / 平均归母股东权益，或接口直接披露 ROE）。若使用"税前利润/期末净资产"等替代算法，必须改名为该算法本身，不得命名为 ROE。
- 风险提示和市场关注必须优先使用最新报告期数据；若引用旧期数据，必须说明旧期原因和最新期缺口。

**港股特别补充**：优先调用 `getHkFdmtIsPit`（利润表）、`getHkFdmtBsPit`（资产负债表）、`getHkFdmtCfPit`（现金流量表）补齐财务数据。

**美股财务特别说明（v1.2.0）**：默认不假设通联有完整三大报表；美股财务、分业务、估值和市场分歧主要从研报、会议纪要、财报点评中抽取。美股报告必须显式区分 GAAP / non-GAAP、segment actual、company guidance、机构 forecast/estimate、财年与自然年、人民币/美元单位；若结构化三表缺失，评测中只能记录为 `skipped_with_reason` / `N/A`，不得将“未检查”计为“检查通过”。

### HK-US-4：写报告

按 `<skill_root>/references/hk-us-report-structure.md` 写报告，保存为：

- 港股 → `{公司名}（{ticker}）港股公司一页纸.md`
- 美股 → `{公司名}（{ticker}）美股公司一页纸.md`

- **报告标题格式**：
  - **港股**：`{股票简称}（{股票代码}）港股公司一页纸：{一句话结论}`
  - **美股**：`{股票简称}（{股票代码}）美股公司一页纸：{一句话结论}`
  - 结论部分**约20字、最多25字**，须在语义完整处自然结尾（禁止句子中断），有观点导向性，体现最核心驱动力或投资判断（如：直销占比跃升，分红回购支撑估值修复）

写作核心原则：
- 关键要点写在**第1章**，不放尾部
- 买方语言优先：先讲"发生了什么、为什么重要、市场怎么定价、关键分歧是什么"
- 所有关键数字、事实和判断都必须标注来源 `[N]`，每条 `[N]` 写入前必须在 materials JSON 中按 `id` 反查来源实体确认匹配
- **引用闭环铁则**：正文引用集合必须是参考资料集合的子集；交付前若发现正文出现未定义引用（如 `[30]` 不在参考资料中），必须先修复，不得交付报告。
- **引用元数据铁则**：参考资料中每条 `[N]` 的标题、机构、日期必须直接从 JSON 原始元数据字段逐字复制，**禁止截断、改写、去掉前缀后缀（含 `【】` 标签）、替换机构名、拼接来源类型到机构列**。标题中的前缀是标题的组成部分，不是机构名。
- **事实属性铁则**：预测、指引、目标价、情景推演必须在正文中显式写明"预计/预测/指引/情景假设/基于[N]推算"；不得把研报预测、市场估算或管理层展望改写为公司已发生事实。
- **口径一致铁则**：同一指标在全文只能使用一个主口径；若同名指标存在多个口径，必须在指标名中写清口径差异，例如"公司披露 CapEx"与"购建长期资产现金流"，不得都写作"资本开支"。
- **敏感性数字铁则（v1.2.1新增）**：来源直接给出的敏感性可以引用。自行推导的敏感性必须标注"内部测算"，并给出公式、基准值、单位和假设（如"内部测算：基于FY2025归母净利润3389亿元，每±100亿=±2.9%"）。无法找到来源复核的敏感性数字**不得生成**——删除对应行，不保留空行占位。
- **时间口径铁则（v1.2.1新增）**：FY（财年）、CY（自然年）、季度、日历年必须保留来源原始口径标签，**禁止默认互换**。NVDA 的 FY2027（截至2027年1月）≠ CY2027（2027年1-12月）；阿里的 FY2027（截至2027年3月）≠ 自然年2027。跨公司比较时必须显式标注各公司财年结束月份。
- **同业对比铁则**：第9章每个可比公司行至少绑定一个来源；市值必须注明日期或删去，未验证的私有公司估值不得作为确定事实写入。
- 正文禁止出现"未披露""无数据""N/A"等填空式措辞
- 不在正文中堆券商名称；参考资料中保留真实来源
- **§5.2 年份列隐藏**：若某年全部分业务行均无数据、仅知总收入，隐藏该年的收入/占比/毛利率三列，只保留有数据的年份
- **§11.2 单行隐藏**：某机构行仅有定性描述而无具体数值时，直接隐藏该行，不占位
- **§9 相关业务进展**：须具体可验证（如"2026Q1市占率提升Xppt"），禁止笼统（"行业领先""持续增长"），并同步填写行业地位列

### HK-US-5：生成 Word 文件

```bash
# 港股
python3 -X utf8 <skill_root>/scripts/build_docx.py \
  "{公司名}（{ticker}）港股公司一页纸.md" \
  --output "{公司名}（{ticker}）港股公司一页纸.docx"

# 美股
python3 -X utf8 <skill_root>/scripts/build_docx.py \
  "{公司名}（{ticker}）美股公司一页纸.md" \
  --output "{公司名}（{ticker}）美股公司一页纸.docx"
```

### HK-US-6：自检与清理

用 `<skill_root>/references/hk-us-quality-checklist.md` 逐项自检。**必须额外执行** `<skill_root>/references/report-verification-prompt.md` 中的深度核验——重点核验：① 每个 `[N]` 的 ID 是否在 materials JSON 中真实存在且标题/机构一致；② 所有财务数字是否与接口/研报一致；③ 引用 `[N]` 是否只跟数值、不跟机构名；④ 正文引用集合是否全部出现在参考资料章节；⑤ Forecast/Guidance/Estimate 是否被误写为 Actual；⑥ CapEx、ROE、现金流、股权投资等指标口径是否全篇一致。清理临时素材目录，最终只保留 `.md` 和 `.docx`。

### HK-US-7：港美股降级路径（脚本失败时）

1. 根据 `<skill_root>/references/hk-us-api-playbook.md` 手动调用必要 API
2. 关键规则：
   - 研报搜索必须双路径：`ticker` 路径和 `query` 路径；港股 `exchangeCode=XHKG`，美股 `exchangeCode=AMXO,XNAS,XNYS`
   - 研报至少取 5 篇有价值报告；优先近 6 个月
   - 会议纪要用 `marketType=港股` 或 `marketType=美股`
   - **不要调用以下不支持港股或已停用的接口**：`research_sec_foredata`、`stock_evaluationAnalysis`、`diagnosis_valuation_rank`、`Stock_OnePage`、`market_snapshot`、`market_HK`
3. 若 Datayes 无法返回足够材料，明确告知用户缺口，基于已有来源生成"可验证版"报告

---

## 美股内容增强（仅美股适用）

美股缺少完整结构化财务 API 时，必须从研报全文、业绩点评、电话会纪要中抽取以下维度：

- **TAM/市场空间**：核心市场、扩展市场、机构测算口径和关键假设
- **产品矩阵/商业化路径**：核心产品、新产品、seat/usage/广告/订阅等收费方式
- **客户与留存 KPI**：客户数、ARR、NDR/DBNR、MAU/DAU、ARPU/ASP
- **技术/AI/平台化变量**：AI 是否带来新增收入、成本项和商业化时点
- **盈利质量**：毛利率/经营利润率/FCF、一次性费用、股权激励
- **估值方法**：目标价、EV/S、PE、DCF、SOTP，并解释估值溢价或折价的验证条件
- **市场分歧**：至少 3 组多空观点，每组必须有可验证指标
- **下一次验证点**：下一份财报、产品商业化、指引、解禁、监管、订单/客户事件等

### v1.2.0 美股/ADR 强制校验点

- **GAAP / non-GAAP**：同一表格中可并列，但指标名必须写出口径；不得把 non-GAAP 净利、non-GAAP EPS 写成 GAAP actual。
- **segment / guidance**：分部收入属于 segment actual；下一季收入、毛利率、费用率属于 company guidance；机构模型属于 forecast/estimate。
- **币种和单位**：ADR 公司常同时出现人民币、美元、港元；表头必须写清币种与单位，禁止在同一列混用。
- **财年和自然年**：阿里、英伟达等公司必须写清 FY2026、CY2026 或 “FY2026（截至 YYYY-MM-DD 财年）”；不得用自然年替代财年。
- **估值方法**：PE、EV/S、DCF、SOTP 若来自来源必须保留来源口径；若从上下文推断，写作“推断：”并保留引用。
- **评测记录**：美股缺少 HK PIT 三表时，正式 evaluator 应记录 `skipped_with_reason`，该项只能计入 skipped，不能计入 passed。

---

## 通用写作规范

**全部格式与引用规则以对应报告结构文件为准**：
- **A股** → `<skill_root>/references/a-share-report-structure.md`
- **港股/美股** → `<skill_root>/references/hk-us-report-structure.md`

补充性工作流规则（结构文件中不含）：
- **数据优先级**：最新财报 > 近期研报、会议纪要 > 近期公告 > 历史数据
- **报告文件为唯一输出**：对话窗口仅显示进度和路径，**不在对话中输出完整报告正文**

---

## Word 版样式规范（A股、港美股统一）

- 页面：纵向 A4；左右边距约 0.83 inch，上下边距约 0.71 inch
- 字体：英文 Calibri，中文微软雅黑；正文 10.5pt，颜色 `#1F1F1F`
- 标题：主标题居中 18pt 深蓝 `#1F3A5F`；一级标题 13.5pt；二级标题 12pt；三级标题 11pt
- 表格：表头浅蓝底 `#D9EAF7`，细网格线，表内文字 10pt（港美股）/ 9.5pt（A股），表头加粗
- 引用 `[N]` 保持正文可读性，不做过小上标
- 无目录页

---

## 执行约束

- **禁止网页搜索**：本 Skill 依赖 Datayes API，不使用 WebSearch/WebFetch（但允许公开来源补充港美股重大事件，详见港美股子流程）
- **减少探索**：优先使用脚本流水线，避免模型手动调用接口
- **脚本路径降级**：本 skill 的脚本路径优先级：`<skill_root>/scripts/` → 错误报告

---

## 跨平台注意事项

本 Skill 的脚本经过跨平台测试，以下规则在所有平台上强制执行：

### Python 调用
- **所有 Python 脚本必须通过 `python3 -X utf8` 调用**，确保 UTF-8 模式在 Windows GBK 环境下正常工作（`-X utf8` 将默认编码从 GBK 切换为 UTF-8）
- ⛔ 禁止使用 `python` 或 `py` 命令替代 `python3`

### Windows 平台额外约束
- ⛔ 禁止使用 PowerShell 解析 API 响应或处理 JSON
- ✅ 所有 API 调用通过 Python 脚本发起，JSON 在 Python 内解析
- ✅ 文件路径使用正斜杠 `/` 或 `pathlib.Path`，不要硬编码反斜杠 `\`
- ✅ 若需设置环境变量，使用 `$env:DATAYES_TOKEN="xxx"`（PowerShell）或 `set DATAYES_TOKEN=xxx`（CMD）

### 中文字体（Word 生成时）
- macOS：脚本优先使用系统自带中文字体（如苹方/PingFang）
- Windows：脚本自动查找微软雅黑，**若缺失需提前安装**
- Linux：需安装 `fonts-wqy-microhei` 或类似中文字体包

### 脚本路径
- 所有脚本和参考文件均通过 `<skill_root>/` 相对路径引用
- `<skill_root>` 由 WorkBuddy 运行时自动解析，无需手动处理

---

## 参考文件索引

| 文件 | 用途 |
|------|------|
| `references/a-share-api-interfaces.md` | A股 API 接口参数详细说明 |
| `references/a-share-report-structure.md` | A股报告章节结构与写作规范 |
| `references/a-share-quality-checklist.md` | A股报告质量自检清单（20项） |
| `references/hk-us-api-playbook.md` | 港美股 API 编排手册 |
| `references/hk-us-report-structure.md` | 港美股报告章节结构 |
| `references/hk-us-quality-checklist.md` | 港美股报告质量自检清单 |
