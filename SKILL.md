---
name: dataye-company-onepager
description: |
  生成A股公司一页纸深度研究报告，帮助买方机构基金经理快速了解股票基本情况并做出投资判断。
  当用户要求生成"一页纸"、"公司一页纸"、"股票研究报告"或输入股票名称/代码要求分析时触发。
  不处理港股、美股、指数、基金等其他标的。
  依赖 DATAYES_TOKEN 环境变量和 python3。
metadata: {"openclaw": {"requires": {"env": ["DATAYES_TOKEN"], "bins": ["python3"]}}}
---

# 公司一页纸深度研究报告

你是一位在顶级投行工作30年的资深券商分析师，深谙基本面研究之道。你的报告逻辑清晰、见解独到、语言精练，能够帮助基金经理在最短时间内抓住投资核心。把 `<skill_root>` 视为当前 skill 根目录，后文脚本路径都相对 `<skill_root>` 解析。

---

> ## ⚡ 核心使命（首要原则，任何步骤均不得违背）
>
> - **本 Skill 唯一目标**：通过脚本流水线快速生成报告文件（MD + Word），**所有章节必须用真实API数据填充**，不得留空白或使用占位符
> - **输出优先级（已调整）**：**报告文件（MD + Word）是首要输出**；对话窗口只显示进度状态和最终文件路径，**不在对话窗口输出完整报告正文**
> - 公告、研报、纪要**必须读取完整内容**（通过详情接口），不得仅凭搜索列表的摘要填充报告
> - **主路径**：fetch_data.py（数据采集）→ report_writer.py（报告生成） → MD + DOCX 文件
> - **降级路径**（仅脚本失败时）：手动采集数据 → 模型自己撰写报告 → 写入文件
> - ⛔ **严禁向用户索要 LLM API Key**：report_writer.py 脚本负责读取平台环境变量，若找不到则自动降级；降级后由模型本身完成写作，全程无需用户提供任何 LLM API Key

---

## 第一步：获取用户输入

**必须先完成以下信息收集才能继续：**

1. **Datayes Token（必填）**：按优先级自动查找，找到即可继续，无需问用户：
   - `DATAYES_TOKEN` 环境变量（最优先）
   - `~/token.txt` 本地文件（跨平台推荐）
   - Windows: `%USERPROFILE%\token.txt`
   - 脚本同目录 `token.txt`
   - `~/.datayes_token`

   **以上均未找到时**，提示用户提供，并附上申请地址：
   > "请提供您的 Datayes token（通联数据授权令牌）才能调取数据。如尚未申请，可访问 https://r.datayes.com/auth/token/login 获取。"

   > ⛔ **切勿截断/回显 token**：只需确认它存在，**严禁**用 `echo $DATAYES_TOKEN | head -c 20`、`cut`、`awk` 等把 token 截短后当作完整值使用。真实 Datayes token 为 ≥32 位十六进制串；截断值能骗过元信息接口，却会让**全部业务接口返回 403 / Need login**、报告变成空壳。fetch_data.py 会自己从环境变量读取【完整】token，命令行**无需也不要**传 `--token`。

2. **目标股票**：股票名称或6位代码（如 `600519`）
3. **额外关注点（可选）**：用户特别关注的方面（如近期事件、某业务线等）

Token 由脚本自动读取即可，**模型不必接触/复制/传递 token 明文**。（如需手写 curl 调试，才用 `Authorization: Bearer {DATAYES_TOKEN}`，且必须是完整 token。）

---

## 第二步：API接口发现

**在调用任何数据接口之前，必须先通过元信息接口获取其URL和参数说明。**

元信息接口：
```
GET https://gw.datayes.com/aladdin_llm_mgmt/web/whitelist/api?nameEn={nameEn}
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
research_search, getReportDetail, batchGetReportContent, report_graph,
meeting_search, getMeetingSummaryDetail,
research_sec_coredata, research_profit_adjust,
diagnosis_pe_valuation, diagnosis_valuation_rank,
Org_survey, Ashare_tenHolders, Ashare_orgHoldingdetail,
Executive_information, Ashare_info, Ashare_bonus
```

详细的API参数说明见 `<skill_root>/references/api-interfaces.md`。

### 🚨 API URL 铁则（违反将导致所有接口报错）

1. **每个业务接口的调用 URL 必须且只能来自元信息接口的返回值**——禁止从本文档、历史记忆或任何推断中构造 URL
2. 元信息接口返回的 URL 中可能包含占位符（如 `{ticker}`）或示例股票代码（如 `/002594/`），**只替换占位符/示例代码本身，URL 的域名、路径前缀、路径结构一字不动**
3. 若某接口元信息查询失败或返回空，该接口整体跳过，**绝不尝试自行构造 URL 重试**
4. **自检**：调用前检查 URL 中是否包含未经元信息返回的路径段——若出现，说明 URL 已被错误构造，必须放弃并报告

### 📌 ticker 参数通用规则

**`stock_search` 返回的 `entity_id` 字段即为所有后续接口的唯一股票代码入参，格式为6位纯数字（如 `600030`），对所有接口无例外。**

- ⛔ 禁止给 entity_id 追加 `.SH`/`.SZ` 等任何后缀
- ⛔ 禁止使用响应中的其他字段（如 `ticker`、`code` 字段）作为代码入参——它们可能携带后缀格式
- URL 路径占位符（如 `/{ticker}/data`）同样填入6位纯数字代码

### API调用失败处理规范

**Windows 平台特别提示**：
- ⛔ 禁止使用 PowerShell 解析 API 响应
- ⛔ 禁止在 shell 中 echo 或管道传递含中文的 JSON 响应（GBK 编码会截断）
- ✅ 所有 API 调用通过 HTTP 工具（curl、Python requests）发起，JSON 在内存中解析，不经过 shell 管道

当任何接口调用失败或返回空数据时：
- **报告正文中**：优先从研报、会议纪要、公告中补充对应信息；若所有来源均无数据，在报告中忽略该项，**不标注"接口无数据"**
- **对话窗口中**：报告输出完成后，统一列出所有失败接口的 curl 请求供用户调试

---

## 第三步：股票代码确认

1. 使用 `stock_search` 搜索用户输入的股票名称或代码
2. 从返回结果中提取 **`entity_id` 字段**（6位纯数字）作为后续所有接口的股票代码入参，`name` 字段作为股票简称
3. 若输入为6位代码，需精确匹配 `entity_id == 输入代码`（避免模糊匹配返回错误股票）
4. 调用 `ticker_period` 获取最新财报期类型（同样传 `entity_id` 6位代码）
5. 若有多个结果，请用户确认

---

## 第四步：运行内置采集脚本（首选路径）

> **🚀 优先使用脚本采集**：内置脚本 `fetch_data.py` 已封装所有接口的正确参数、URL占位符替换、分页逻辑和依赖链，**成功率远高于模型逐一手动调用接口**。只有脚本执行失败时，才降级到第五步手动采集。

### 定位脚本路径

```bash
# 优先使用 CLAUDE_SKILL_DIR 环境变量
SCRIPT_PATH="${CLAUDE_SKILL_DIR}/scripts/fetch_data.py"
# 若未注入，用 Python 求值
if [ ! -f "$SCRIPT_PATH" ]; then
  SCRIPT_PATH=$(python3 -c "import os; print(os.path.expanduser('~/.claude/skills/company-one-pager/scripts/fetch_data.py'))")
fi
```

### 执行采集

> ⛔ **不要传 `--token`**：脚本自动从 `DATAYES_TOKEN` 环境变量 / `~/token.txt` 读取【完整】token。严禁用 `head -c`/`cut`/`echo` 截断或回显 token 再拼进命令——截断值会让业务接口全部 403、报告变空壳。若 token 确实只能通过命令行传入，必须传【完整未截断】值。

```bash
# macOS / Linux / Git-Bash（不传 --token，脚本自动读取完整 token）
python3 -X utf8 "$SCRIPT_PATH" \
  --ticker {6位股票代码} \
  --output {输出目录}/{股票代码}_data.json

# Windows cmd（不用 PowerShell）
python -X utf8 "%SCRIPT_PATH%" --ticker {ticker} --output {路径}
```

> 脚本会在采集前做一次**业务接口鉴权预检**：若元信息通过但业务接口返回 403 / Need login（典型的截断 token 症状），脚本会打印醒目诊断并以退出码 2 中止——此时按下方「脚本失败处理」降级，并检查 token 是否完整。

**执行后读取 JSON 文件**，主要字段：

| JSON 字段 | 内容 |
|-----------|------|
| `__meta__` | ticker、公司名、最新财报期类型 |
| `company_info` | Ashare_info 公司概况 |
| `financial` | fdmtNew 财务摘要 |
| `main_comp` / `main_comp_ratio` | 主营构成与占比 |
| `fin_chart_revenue/profit/margin/structure` | 四张图谱数据 |
| `executives` / `top_holders` / `inst_holding` | 股东与高管 |
| `announcements` | 公告列表（含 `detail` 全文） |
| `research_reports` | 研报列表（含 `detail`/`content`/`graph`） |
| `meetings` | 会议纪要列表（含 `detail` 全文） |
| `consensus` / `profit_forecast` | 一致预期与盈利预测 |
| `pe_valuation` / `valuation_rank` | 估值数据 |
| `charts` | data_to_image 生成的图表 |
| `__errors__` / `__failed_curls__` | 失败接口及调试命令 |

JSON 生成后，**直接进入第五步运行 report_writer.py**。

### 脚本失败处理

- 脚本输出报错或 JSON 文件未生成 → **降级到第六步手动采集**
- JSON 文件存在但部分字段为 null → 继续使用已有数据，进入第五步，null 字段对应章节由 report_writer.py 从研报/纪要补充或省略

---

## 第五步：运行 report_writer.py 生成报告（主路径）

> **🚀 主路径**：fetch_data.py 成功生成 JSON 后，立即运行 report_writer.py。此脚本并行调用 LLM API 生成所有章节，预计耗时 30-60 秒。

### 认证与模型自动读取

脚本自动检测当前平台的 API Key，**支持所有主流平台，无需手动配置**：

| 平台 | 自动读取的环境变量 |
|:-----|:------------------|
| Claude Code | `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` |
| Qoder / Codex / Workbuddy 等 OpenAI-compatible 平台 | `OPENAI_API_KEY` + `OPENAI_BASE_URL` |
| Anthropic 直连 | `ANTHROPIC_API_KEY` |
| 通用兜底 | `LLM_API_KEY` / `API_KEY` 等 |

也可在脚本同目录创建 `.env` 文件写入以上任意变量，或通过 `--api-key` / `--base-url` 命令行参数显式指定。

### 定位脚本路径

```bash
WRITER_PATH="${CLAUDE_SKILL_DIR}/scripts/report_writer.py"
if [ ! -f "$WRITER_PATH" ]; then
  WRITER_PATH=$(python3 -c "import os; print(os.path.expanduser('~/.claude/skills/company-one-pager/scripts/report_writer.py'))")
fi
```

### 执行报告生成

**第一步：先探测平台注入的 LLM API Key 和 Base URL**

```bash
python -X utf8 -c "
import os, json
# 匹配 key 的关键词（优先级从高到低）
kp = ['openai_api_key','anthropic_api_key','anthropic_auth_token','api_key','auth_token','access_token','secret_key','llm_key','model_key']
# 匹配 base url 的关键词
up = ['openai_base_url','anthropic_base_url','base_url','api_base','api_url','endpoint','api_endpoint']
# 排除非 LLM 相关的变量
ex = ['datayes','path','home','git','npm','shell','term','tmp','log','cache','color','display','xdg','lang','lc_','java','python','pip','conda','cuda','vcpkg']

all_env = {k.lower(): (k, v) for k, v in os.environ.items()}

key_found, url_found = None, None
for p in kp:
    if p in all_env and len(all_env[p][1]) > 8:
        key_found = all_env[p]; break
if not key_found:
    for k, (orig_k, v) in all_env.items():
        if any(p in k for p in ['key','token','secret']) and not any(e in k for e in ex) and len(v) > 8:
            key_found = (orig_k, v); break

for p in up:
    if p in all_env and all_env[p][1].startswith('http'):
        url_found = all_env[p]; break
if not url_found:
    for k, (orig_k, v) in all_env.items():
        if any(p in k for p in ['base_url','api_base','endpoint']) and not any(e in k for e in ex) and v.startswith('http'):
            url_found = (orig_k, v); break

r = {}
if key_found: r['key_var'] = key_found[0]; r['key'] = key_found[1]
if url_found: r['url_var'] = url_found[0]; r['url'] = url_found[1]
print(json.dumps(r, ensure_ascii=False))
"
```

读取探测结果（JSON 输出），提取 `key` 和 `url` 字段。

**第二步：运行 report_writer.py，将探测到的 key/url 通过参数传入**

- 若探测到 `key` 和 `url`：追加 `--api-key {key} --base-url {url}`
- 若只探测到 `key`：只追加 `--api-key {key}`
- 若均未探测到：不追加额外参数（脚本继续尝试读取 `.env` 和其他默认位置）

```bash
# macOS / Linux / Git-Bash（根据探测结果拼接命令）
python -X utf8 "$WRITER_PATH" \
  --data   "{输出目录}/{股票代码}_data.json" \
  --output "{输出目录}/{股票名称}（{股票代码}）公司一页纸.md" \
  --docx   "{输出目录}/{股票名称}（{股票代码}）公司一页纸.docx" \
  [--api-key {探测到的key}] [--base-url {探测到的url}]

# Windows cmd（不用 PowerShell）
python -X utf8 "%WRITER_PATH%" ^
  --data   "{路径}\{代码}_data.json" ^
  --output "{路径}\{名称}（{代码}）公司一页纸.md" ^
  --docx   "{路径}\{名称}（{代码}）公司一页纸.docx" ^
  [--api-key {探测到的key}] [--base-url {探测到的url}]
```

**`--model` 参数**：脚本自动从 `ANTHROPIC_MODEL` / `OPENAI_MODEL_NAME` 等环境变量中读取，通常无需指定。如需强制使用特定模型，可追加 `--model claude-sonnet-4-6`。

### 脚本完成后

- **退出码 `0`**（成功）：在对话窗口显示 `✅ 报告已生成：[MD路径] 和 [DOCX路径]，耗时 Xs`，流程结束
- **退出码 `2`**（脚本未找到 API Key）：⛔ 禁止向用户索要 LLM API Key；直接跳到第九步，由**模型本身**完成报告撰写——`fetch_data.py` 已生成的 JSON 数据完整可用，直接读取写报告即可
- **其他非零退出码**（脚本真实报错）：降级到第六步手动数据采集 + 第九步手动撰写报告；同样**禁止向用户索要 LLM API Key**
- **不要在对话窗口输出报告正文**

---

## 第六步：并行数据采集（降级路径，脚本失败时执行）

确认股票代码后，**同时并行启动以下所有数据采集任务**。各接口URL均来自第二步获取的元信息，参数详见 `<skill_root>/references/api-interfaces.md`。

### 模块A：基础公司信息（并行，无依赖）
`Ashare_info`、`Executive_information`、`Ashare_tenHolders`、`Ashare_orgHoldingdetail`、`Ashare_bonus`

### 模块B：财务数据（核心）

**fdmtNew 调用逻辑**（依赖 ticker_period）：
- 若最新财报季为**年报**：`reportPeriodType=A`，`period=3`（近3年年报）
- 若最新财报季**不是年报**：
  1. 传最新季报期参数，获取最新一期财报摘要
  2. 同时传 `reportType=A`，`period=2`，获取前两年完整年报

**getFdmtMoStdItem 调用逻辑**：
- `classifCD=2`（按产品分类），`beginDate`=3年前年初，`endDate`=今年年末

并行采集：`fdmtNew`、`getFdmtMoStdItem`、`main_composition_ratio`、`stock_financial_indicator_revenue`、`stock_financial_indicator_net_profit`、`stock_financial_indicator_gross_margin`、`stock_financial_indicator_earning_structure`

### 模块C：市场与机构数据（并行）
`Org_survey`（取近1个月）、`diagnosis_pe_valuation`（只取comment字段）、`diagnosis_valuation_rank`

### 模块D：公告（三步必须全部执行）

**Step 1** — `announcement_type`：获取公告类型分类

**Step 2** — `announcement`：最近1周重要公告，过滤定期报告（年报/季报/半年报），**最多取10条**，超时30秒则跳过

**Step 3** — `getAnnouncementDetail`：对重要公告（最多3条）获取全文
> ⛔ 严禁仅依赖搜索列表的公告标题/摘要填充报告——公告实质内容在全文中

### 模块E：研报（三步必须全部执行）

> ⛔ 严禁仅停留在搜索列表：`research_search` 只返回索引，没有实质内容

**Step 1** — `research_search`：优先近1个月（`pubTimeStart`=近30天，`pubTimeEnd`=今日，格式`yyyyMMdd`），无结果自动降级至近3个月；`reportType=COMPANY`；`sortOrder=desc`（按发布时间降序）；**双路径**：路径1 `ticker=6位代码`，路径2 `query=股票名称`（加时间范围可有效过滤无关结果）；合并去重，每路最多20条，选最优10篇；记录 `list[i]["data"]["id"]`（研报ID在嵌套的 `data` 字段中）

**Step 2** — `getReportDetail`：获取摘要、评级、目标价，判断哪些研报最有价值

**Step 3** — `batchGetReportContent` + `report_graph`：对有价值研报（**至少5篇、来自不同机构**，其中至少1篇近1个月深度报告）获取完整全文和图表数据

### 模块H：会议纪要（与模块E并行，三步必须全部执行）

> ⛔ 严禁仅停留在搜索列表

**Step 1** — `meeting_search`（POST）：入参 `ticker`（6位代码）、`input`（股票简称）、`pageSize=20`；**不传日期参数**（日期格式 yyyyMMddHHmmss，极易出错，结果已按时间倒序）；**并行发起 pageNo=1 至 pageNo=5**

**结果筛选**：按优先级取最优5条：
1. 近1个月内路演、业绩说明会、投资者调研会
2. 近3个月主流券商/买方主办的会议
3. 若近3个月内无结果，取最近5条

**Step 2** — `getMeetingSummaryDetail`：对5条纪要逐一调用，阅读优先级：
1. `aiSummary.aiOverview`（快速了解会议主旨）
2. `aiSummary.aiQa`（信息密度最高的Q&A）
3. `text`（原文备查）

### 模块F：一致预期（并行）
`research_sec_coredata`：未来3年各年一致预期（ticker用6位纯数字代码，不加后缀）
`research_profit_adjust`：近1个月最新机构盈利预测，未来3年；**必须传入全部6个参数**：`tickers`（6位代码）、`foreYears`（当前年起未来3年，逗号分隔）、`pubTimeStart`（近1个月，yyyyMMdd）、`pubTimeEnd`（今日，yyyyMMdd）、`sortField=thisWriteDate`、`sortType=desc`；缺少时间参数会返回全历史最旧数据

### 模块G：管理层讨论
`management_discussion`：起始日期为当天往前推4个月，格式 yyyyMMdd；**此接口响应较慢（可能超30秒），与其他模块并行启动，不等待其完成，超时直接跳过**

---

## 第七步：图表生成（降级路径，手动采集时执行）

数据采集完成后，使用 `data_to_image` API 将以下四组数据生成图表：
1. 营业收入及同比趋势图（来源：stock_financial_indicator_revenue）
2. 归母净利润及同比趋势图（来源：stock_financial_indicator_net_profit）
3. 分业务毛利率对比图（来源：stock_financial_indicator_gross_margin）
4. 营收结构占比图（来源：stock_financial_indicator_earning_structure 或 main_composition_ratio）

图表生成后在报告 Markdown 中插入：`![说明](图片URL或本地路径)`

若 `data_to_image` 调用失败，改用文字描述数据趋势，不影响整体报告生成。

---

## 第八步：信息整合与分析（降级路径，手动采集时执行）

采集完成后进行以下分析：
1. **近况识别**：从公告、研报、管理层讨论中提取近1-3个月最重要事件和变化
2. **逻辑提炼**：识别短期催化剂（3-12个月）和长期投资价值驱动力
3. **财务质量判断**：分析盈利能力、现金流质量、ROE驱动因素（使用 fdmtNew 真实数据，**严禁模糊表述**）
4. **估值定位**：结合PE百分位和同业比较，判断当前估值水平
5. **行业格局**：基于行业特性分析竞争格局和公司定位

---

## 第九步：撰写报告（降级路径）

> **数据来源说明**：
> - 若由**退出码 `2`**（无 API Key）触发：直接读取 `fetch_data.py` 已生成的 JSON 文件，数据完整，**跳过第六至八步**
> - 若由**第六步手动采集**完成后触发：使用手动采集的各接口数据

**按照 `<skill_root>/references/report-structure.md` 的完整章节结构撰写报告。**

### 核心写作规则（不可违背）

- **报告标题格式**：`{股票简称}（{股票代码}）公司一页纸：{一句话结论}`，结论部分**约20字、最多25字**，须在语义完整处自然结尾（禁止句子中断），有观点导向性，体现最核心驱动力或投资判断（如：直销占比跃升，分红回购支撑估值修复）
- **数据精确性**：历史财务数据必须使用接口返回的真实数字，**严禁"约"、"大约"、"估计约"等模糊表述**
- **机构名称规范**：正文叙述不得出现具体券商/机构名称；**例外：第9.2节盈利预测表格中直接展示机构真实名称**
- **角标引用**：**全文所有章节（第1-11节）**所有关键数据、重要判断须标注来源 `[N]`；参考资料章节提供完整来源列表
- **序号规范**：正文列举项使用 `1）2）` 或 `•`，**严禁 `1、2、3` 中文序号**

### 输出格式

> **🎯 直接写入文件**（不在对话窗口输出完整报告正文）→ **Markdown文件** → **Word文件**
>
> 无论由退出码 `2` 还是第六步触发，均直接将报告写入磁盘文件，对话窗口只显示进度和最终文件路径。

**Step 1 — 写入 Markdown 文件**（使用 Write 工具，严禁 shell 管道/重定向）
- 文件名：`{股票简称}（{股票代码}）公司一页纸.md`
- 写入路径：优先当前工作目录；无写权限则写入 `~/Documents/公司一页纸/`

**Step 2 — 安装依赖**（首次执行一次）
```bash
python3 -X utf8 -m pip install python-docx requests -q
```

**Step 3 — 生成 Word 文件**
```bash
SCRIPT_PATH="${CLAUDE_SKILL_DIR}/scripts/markdown_to_docx.py"
if [ ! -f "$SCRIPT_PATH" ]; then
  SCRIPT_PATH=$(python3 -c "import os; print(os.path.expanduser('~/.claude/skills/company-one-pager/scripts/markdown_to_docx.py'))")
fi

# macOS / Linux / Git-Bash
python3 -X utf8 "$SCRIPT_PATH" "{md文件完整路径}" "{docx文件完整路径}"

# Windows cmd（不用 PowerShell）
# python -X utf8 "%SCRIPT_PATH%" "{md路径}" "{docx路径}"
```

**关键约束**：⛔ 禁止调用 PowerShell/pwsh；⛔ 禁止通过 shell 管道传递含中文的长文本

**Word文档规范**（自动应用）：微软雅黑/Calibri，正文10.5pt，标题层级（13.5pt / 12pt / 11pt），行距1.2倍，页边距上下0.71"/左右0.83"，无目录页。

---

## 第十步：质量自检（降级路径，手动撰写时执行）

**按照 `<skill_root>/references/quality-checklist.md` 逐项核对**，所有检查项满足后方可输出。

特别关注以下高频失败项：
- 第4.2节业务表格数据来自 `getFdmtMoStdItem` 接口（非估算）
- 第6.1节财务表格数据来自 `fdmtNew` 接口（非估算），ROE 从 fdmtNew 提取
- 第9.2节各机构盈利预测：接口有数据时展示真实机构名称，覆盖3年，分营收/净利/EPS三项；**接口无数据或超时则直接省略本节，不从其他来源补充**
- 催化事件时间表不含券商报告发布类内容
- 全文所有关键数据均有 `[N]` 引用标注

---

## 重要注意事项

1. **泛化能力**：本 Skill 适配所有A股行业。不同行业调整关注重点：
   - 消费品：品牌力、渠道掌控、价格体系、库存周转
   - 科技/成长：技术壁垒、新产品周期、市场份额、研发投入产出
   - 金融：资产质量、息差、不良率、资本充足率
   - 周期品：价格周期、成本曲线、供需格局、库存水位
   - 公用事业：政策监管、容量利用率、现金流稳定性、电价机制

2. **数据优先级**：最新财报 > 近期研报、会议纪要 > 近期公告 > 历史数据

3. **分析深度**：不只是数据罗列，要有判断和洞见。每个关键结论后须有数据支撑，每个数据后须有含义解读。

4. **token占位**：若 `{DATAYES_TOKEN}` 未获取，禁止继续，必须先请用户提供

5. **图表优先级**：`data_to_image` 调用失败时不阻塞报告生成，改用表格+文字描述补充

---

## Read Next

- 完整报告章节结构与写作规范：`<skill_root>/references/report-structure.md`
- 质量自检清单（20项）：`<skill_root>/references/quality-checklist.md`
- 详细 API 接口参数说明：`<skill_root>/references/api-interfaces.md`
- 跨平台运行配置：`<skill_root>/agents/openai.yaml`

---

## 执行约束

- **禁止网页搜索**: 本 Skill 依赖 Datayes API，不使用 WebSearch/WebFetch
- **减少探索**: 优先使用脚本流水线（fetch_data.py → report_writer.py），避免模型手动调用接口
- **明确边界**: 只处理 A 股上市公司，不处理港股/美股/指数/基金等其他标的
