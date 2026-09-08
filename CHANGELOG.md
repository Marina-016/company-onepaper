# Changelog

## v1.2.37

- 行业池兜底：正则/覆盖名单候选经精确验证不足两家时，用 `getEquIndustry`（申万 2021 三级行业）成分股生成 peer 候选，走既有 stock_search 名称路径验证，解锁 §8.2 生成。
- 兜底触发条件为「验证通过 <2」而非候选数 <2：恒瑞类（研报只点名港股同行）在验证被拒后正确进入行业池；候选按 query 去重合并，正则提取的 peer 保持优先。
- 行业池候选剔除 ST/*ST/退市股；三级行业不足两家时降级到二级行业。
- 端到端验证 6/6（茅台/宁德/招行/比亚迪/恒瑞/海天）均生成 §8.2。
## v1.2.36

- 新增「覆盖名单/全称公司枚举」peer 候选通道：识别券商「覆盖范围内公司」名单与连续全称公司枚举，扩大 §8.2 候选来源。
- enum 提取容忍 `（37个）` 等括号数量注释，并新增 `同行，如X和Y` / `同业，如X和Y` 语境触发，修复「信达生物（37个）和中国生物制药（35个）」类句式无法提取的问题。
- 合规闸门：投行报酬披露（如摩根士丹利投资银行客户名单，常混入石化等非同业）、券商自身法律实体名单（杰富瑞/高盛等系列）一律不作为同业候选；扩充免责声明噪音后缀。
- 无代码名称候选验证改为：覆盖名单来源候选按归一化包含匹配（全称「千禾味业食品」可验证到「千禾味业」），普通候选仍要求完全一致。
- 不改变既有 §8.2 fail-closed 门禁：仍须至少两家经 stock_search 精确验证的有效 peer 才输出同业表。
## v1.2.35

- 同业候选中已带证券代码时，改用候选名称查询 `stock_search`，并同时核验返回代码与名称；规避数字代码查询仅返回模糊结果造成的误拒绝。
- 扩展明确竞争语境和混合分隔枚举提取，支持“相比之下”“主要对手”等研报常见写法；免责声明长句不再进入候选。
- 不新增行业池或其他兜底；后续证券验证、定向同业材料获取及同业表 fail-closed 门禁保持不变。
## v1.2.34

- 清理 A 股 writer 已不再调用的旧章节生成器、旧表格裁剪器、旧模型配置读取和通用催化剂兜底，避免历史分支继续干扰维护。
- 统一主链路中仍在使用的后处理、估值和自检函数命名；来源、风险、同业及情景推演门禁逻辑保持不变。
- 移除对不存在的 `check_report_quality_v123.py` 的陈旧说明；质量控制均由 writer 内置主链路执行。
- `SKILL.md`、`CHANGELOG.md` 与代码版本同步为 v1.2.34。
## v1.2.33

- Rebuilt `SKILL.md` as clean UTF-8 and consolidated the active A-share workflow rules.
- Treat a successful `ticker_period` response with `data: null` as a non-blocking annual-period fallback.
- Removed market cap from the A-share peer schema; baseline progress now uses target-company evidence, sourced progress is limited to one compact event, and the full progress column is omitted when the baseline cannot be sourced.
- Narrowed §9.4 fact cards to operating drivers and added a source-bound deterministic fallback when LLM output loses required core variables during provenance cleanup.
## v1.2.32

- Replaced title keyword gating with structural title checks so complete investment viewpoints are not rejected for omitting a fixed word.
- Preserved all ten A-share peer-comparison dimensions during Markdown normalization.
- Made peer-table generation prompt-led: only related business progress requires peer-specific citations, and that column prioritizes operating developments over financial-scorecard summaries.

## v1.2.31

- Unified §9.3/§9.4 on one consensus-implied price anchor and stopped treating unreconciled valuation-rank PE as current pricing.
- Replaced LLM-written §9.3 with deterministic relative-valuation interpretation.
- Changed §9.4 to source-bound operating scenarios; without three sourced scenario EPS/PE pairs it emits no numerical target price and fails closed on malformed output.
- Removed the legacy postprocessor that could recreate an X.XX × Yx placeholder scenario table.
- Relaxed §10 to 2-5 source-backed risks and allowed explicitly sourced company-specific demand/competition/macro risks while retaining citation and anti-boilerplate gates.
- Logged risk JSON rejection reasons before the source-backed fallback.
## v1.2.30

### P1: Competition-context peer extraction

- Extend A-share peer candidate extraction to recognize an enumerated set of two or more companies when a direct competition verb introduces it, such as “挤占五粮液、泸州老窖等其他高端品牌”.
- Preserve fail-closed safeguards: isolated mentions and compliance disclosures remain ineligible; every name without a ticker must still pass exact normalized `stock_search` matching before `getMaterialsV2` retrieval or §8.2 use.

## v1.2.29

### P0: Report-specific source binding and structural fail-close

- Bind each `research_reports` item to only its own `reportId` body returned by the batch-content API. Missing report bodies remain unavailable; the writer no longer sees another report's full text under a different citation.
- Decouple §8.1 from §8.2: a lack of two validated peers suppresses only the peer table, not an independently sourced industry discussion.
- Retire the post-processing LLM scenario-table rewrite and duplicate citation-cleanup passes. Final cleanup now runs once after provenance filtering.
- Add a final optional-scenario gate: incomplete §9.4 fragments are removed, and scenario rows outside §9 are stripped before MD/DOCX delivery.

## v1.2.28

### P1: Relaxed peer candidates with exact-name verification

- Accept plain company-name phrases in a comparable-company context (after lead-ins such as 如/包括/对标) as peer candidates in addition to `name (code)` pairs; every candidate without a code must still pass an exact `stock_search` name match after suffix normalization, preserving the fail-closed gate.
- Fix fetch→writer wiring so peer materials are fetched from the validated peer list (previously the unvalidated candidate list was passed, yielding empty `peer_materials`).
- Guard Windows stdio re-wrapping under `__main__` so the fetch module can be imported safely by unit tests.

## v1.2.27

### P0: Source-bound peer comparison

- Replace free-text peer extraction with explicit `company name (six-digit code)` candidates from comparable-company report context, then validate the exact security code before retrieval.
- Call `getMaterialsV2` once per validated peer in parallel; keep only materials that actually name that peer and register each as an auditable reference source.
- Restrict §8.2 to the validated peer list. Only the “related business progress” column needs a citation; without source material it is rendered as `—`, while other qualitative columns remain citation-free.
- Remove the stacked peer-table fallback/rebuild path that could inject generic-industry peers after generation. If fewer than two validated peers exist, omit §8.2.
- Remove the retired no-op title fallback while cleaning the same post-generation fallback layer.
## v1.2.26

### P0: Source-bound A-share quality gates

- Filter multi-stock meeting summaries to target-company material before writing; final validation also blocks cited numeric claims from a non-target meeting.
- Remove the retired `check_report_quality_v124.py` invocation and retain the quality checks in the writer's internal delivery gate.
- Add a structural check that blocks Markdown headings injected into table cells.

### P1: Financial terminology and interpretation

- Require full financial metric names and explicit field definitions throughout the report, including a strict distinction between revenue and total revenue.
- Label Q&A as meeting-note views rather than company guidance, and align DuPont output to the diluted ROE basis rather than weighted-average ROE.


## v1.2.25

### P1: Minimal cross-field consistency guards

- 修复主营构成毛利率字段在零值场景下的错误回退，杜邦计算增加空值、除零和与加权 ROE 差异保护。
- 收紧风险提示最终条数为 3–4 条，修复 A 股同业 fallback 基准行缺少“市值”列的问题。
- 无法从真实材料提取合格催化事件时不再写入泛化季度占位事件，保持 fail-closed。

### Remaining known limitations

- 业务事实卡仍主要是数值级绑定，直销与 i 茅台占比、不同收入口径的跨章节统一仍需后续字段级事实注册表治理。
- PEG 增长口径、peer-specific 证据和跨章节一致性尚未完全自动化校验。

## v1.2.24

### P0: Inline numbered Q&A provenance closure

- Split concatenated institution-survey payloads such as `Q1…A1…Q2…A2` before candidate extraction, preventing later questions from being absorbed into the preceding answer.
- Bind the source reference to every rendered answer, rather than relying on a citation that happens to appear at the end of a raw multi-question block.
- Add a regression that requires every rendered Q&A pair to carry its source within the quality-gate window.
## v1.2.23

### P0: Fact-level provenance cleanup and valuation bypass guard

- Keep the v1.2.19 fail-closed provenance gate, but narrow cleanup from a whole line to an unsupported fact clause or table cell so independently cited content survives.
- Reject per-share price wording when traditional PE target-price methods are disabled, preventing a bypass of the existing target-price and EPS×PE checks.
- Add regressions for disguised per-share prices, mixed-fact retention, and gross-margin field isolation.
## v1.2.22

### P0: Derived main-comp residual exclusion
- Exclude residual/calculated main-comp labels at extraction time, including top-level, child and later-period rows. These residual calculations cannot be represented as disclosed business lines in any report section.
- Add a regression test covering the shared extraction path, so the restriction applies to narrative, prompts, tables and fallback blocks consistently.
## v1.2.21

### P0: 空章节、风险与高估值情景门禁

- 空章节恢复不再匹配会被前序清理删除的 `---` 分隔线，改为严格使用相邻 H2/H3 边界；§5、§7、§8 的最小 fallback 仅使用已有主营构成或研报来源，不再使用无来源的静态内容。
- §10 优先拆分研报/纪要明确列出的风险清单；连接词、日期和正面经营描述不能成为风险标题，fallback 在写入前必须通过标题、解释、引用和去重校验。
- 高 PB / PE(TTM) 不适用标的允许声明“传统PE法失效，不输出目标价”；门禁仅拦截实际目标价数值或 EPS×PE 公式。
- §9 的 LLM 片段在组装前移除重复 H2，并拒绝仅标题空壳，防止产生空的 `## 9`。
- 新增 writer 回归测试，覆盖章节边界、空章节恢复、风险 fallback、§9 空壳和高估值情景禁用目标价。
- 后处理阶段新增 fallback 一律从当前参考资料反查最终引用号，避免引用重排后错引或被移除。
## v1.2.20

### P0: 情景表章节边界

- 情景推演表列修复与文本格式化仅允许在 `## 9` 内执行；报告任意其他章节的裸表格、异常表格或“乐观/中性/悲观”行不再触发情景表处理。
- 情景表修复的终点固定为下一 H2，禁止跨越 `## 10` 或影响 `## 5`–`## 8`。
## v1.2.19

### P0: 交付前溯源清洗

- writer 在最终自检前自动删除所有无法通过“正文数值 → 引用 → 原始来源”闭合的生成行，并重新处理引用；禁止把未经核验的数字写入 MD/DOCX。
- 保留最终 fail-closed 门禁：若无法安全删除（如章节标题或表头）或仍有溯源问题，继续阻断交付。
## v1.2.18

### P0: 事实卡程序化引用

- 对研报、纪要、MD&A、主营构成中的高风险数值构建 `{{FACT:F#}}` 事实卡；模型输出标记后由程序统一渲染为原文数值与对应 `[N]`，不再允许按上下文猜测引用。
- 事实卡按来源轮转、去重并支持“数值/数值/数值 + 共用单位”写法，保证唯一宿主中的关键数值进入模型上下文。
- 前三节的原始材料缩减为 3 篇研报加 1 条纪要；其余可引用经营事实只通过短事实卡进入上下文，降低同主题长文互相干扰。
- 最终自检会阻断未渲染的事实标记；`fdmtNew`、一致预期和估值接口不会生成经营事实卡。
## v1.2.17

### P0: 引用绑定与单位归一化

- 研报、纪要素材窗口直接显示最终 [N]、文档 ID 和元数据，不再让模型从原始 ID 与裸序号自行推断引用。
- 主营构成来源审计同时提供元与亿元标准化值，避免真实业务收入被单位差异误拦。
- 情景推演禁止用市场一致预期接口承载产品投放、渠道占比和产品增速等经营假设。

## v1.2.15

### P0: A股正文数字来源审计

- 最终自检新增高风险数字的“正文 → [N] → 原始 JSON / 研报全文 / 纪要”验证；经营分项误引 `fdmtNew`、基酒产量误引主营构成、ROE 误引主营构成，以及来源缺少关键词或数值，均会 fail-closed。
- LLM 提示新增数字溯源、字段白名单和 `operateProfitRatio`/`grossMARgin` 禁混用规则。

### P1: 确定性格式与编号修复

- 财务表毛利率字段改为 `grossMARgin`，不再误用营业利润率 `operateProfitRatio`。
- 风险 fallback 去除正文中重复的标题前缀，保证加粗标题闭合。
- 删除空子节后，自动连续重排直属 H3 编号。
## v1.2.14

### P0: A股情景估值锚定与失效保护

- §9.4 目标价改为以当前一致预期隐含价格（`一致预期 EPS × 当前隐含 PE`）为锚；生成后会校验每档算术及目标价是否落在锚定价的 0.25–4.00 倍内，防止微利/高估值标的出现数量级错误的目标价。
- 当 PE(TTM)≤0，或 PB 超过行业均值 3 倍时，禁用“EPS × 固定 PE”目标价法；情景表仅可说明当前主题/预期定价与估值敏感性，不得给出脱离市价的目标价。

### P0: `fdmt_indi_rtn` 参数修复

- `fetch_fin_indicators()` 改为按接口元信息传入 `ticker`、`beginDate`、`endDate`，移除不兼容的 `period` 参数。

## v1.2.13

Status: LLM 域名白名单扩容，解除国产 LLM 厂商阻断。

### P0: A 股 writer 域名白名单扩展

`a_share_report_writer.py` 的 `ALLOWED_LLM_HOSTS` 从 6 个扩展至 18 个，新增 12 家国产 LLM 厂商域名：

- 智谱AI (`open.bigmodel.cn`)
- DeepSeek (`api.deepseek.com`)
- 阿里通义千问 (`dashscope.aliyuncs.com`)
- 月之暗面 Kimi (`api.moonshot.cn`)
- 百川智能 (`api.baichuan-ai.com`)
- MiniMax (`api.minimax.chat`)
- 字节豆包 (`ark.cn-beijing.volces.com`)
- 阶跃星辰 (`api.stepfun.com`)
- 零一万物 Yi (`api.lingyiwanwu.com`)
- 讯飞星火 (`spark-api-open.xf-yun.com`)
- 百度文心 (`aip.baidubce.com`)
- 腾讯混元 (`hunyuan.tencentcloudapi.com`)

### Files changed

- `datayes-company-onepaper/scripts/a_share_report_writer.py`：`ALLOWED_LLM_HOSTS` 扩展

## v1.2.11

Status: Chapter 9 subsection-level no-data handling — conditional skip + fail-closed.

### P0: 第九章小节独立跳过

第九章（一致预期、盈利预测与估值）四个小节在无可用数据时各自跳过，不再保留空壳：

- **9.1 市场一致预期**：`research_sec_coredata` 无数据或返回"暂缺" → 整节跳过（对齐 9.2 已有逻辑）。
- **9.2 各机构盈利预测**：已有条件跳过，无变动。
- **9.3 估值分析**：`diagnosis_valuation_rank` 所有估值维度无效 → 跳过，不调用 LLM（新增 `_has_valuation_data` 预检）。
- **9.4 情景推演**：无一致预期 EPS/PE 且材料无业务驱动变量文本 → 跳过，不调用 LLM（新增 `_has_scenario_input` 预检）。
- 四节全空时整章 `## 9` 标题也不出现。

### P0: 9.3/9.4 拆分生成

原 270 行单函数 `gen_section9_valuation`（一次 LLM 调用同时生成 9.3+9.4）拆为三个函数：

- `_has_valuation_data()`：检查估值接口是否返回任何有效维度。
- `_has_scenario_input()`：检查是否有一致预期 EPS/PE 或材料中的量化业务指标。
- `_gen_section93()`：仅生成 9.3 估值分析，估值维度全空时返回 `""`。
- `_gen_section94()`：仅生成 9.4 情景推演，无输入基础时返回 `""`。
- `gen_section9_valuation()`：路由器，按数据可用性条件调用上述函数。

### P0: 9.4 增强校验 + fail-closed

`_v124_post_repair` 中 9.4 情景推演检查增强：

- **新增核心变量具体性校验**：`_has_concrete_core_vars` 检查核心变量 bullet 是否含「数字+`[N]`」模式，泛化核心变量（如「需求风险：[N]」无具体数字）直接触发修复。
- **修复失败则删除空壳**：LLM 补写返回空、找不到情景推演表标记、或找不到下一章结束位置 → 删除整个 9.4 节，不再保留模板内容。
- **新增辅助函数**：`_find_section_start()`（定位章节起始）、`_remove_section()`（安全删除章节区间）。

### P1: 组装处重构

- 9.1 与 9.2 对齐条件跳过模式，不再硬编码输出。
- 新增 `_chapter_9_block`：预计算第九章所有有效内容，全空时整章不输出，避免触发自检"空章节"阻断。

### Files changed

- `scripts/a_share_report_writer.py`：~260 行净增
- `SKILL.md`：版本号 + §5.7/§5.8 重编号为 §5.7/§5.8/§5.9 + Appendix A
- `CHANGELOG.md`：this entry

### Unchanged

- 所有港美股脚本、fetch_data、build_docx、fetch_materials、gen_charts、llm_adapter 无变化
- 既有测试全部通过（7/7）

## v1.2.10

Status: A-share §10 risk evidence and validation repair.

### P0: A 股风险提示生成修复

- 风险项目符号校验由 `^\s*[-*]\s+` 改为统一识别 `•/-/*`，修复 v1.2.9 合规 `•` 输出被计为 0 条并覆盖的问题。
- 研报上下文改读真实字段 `title/detail_text/abstract/text`；最新财务快照改读 `latest_data` 或最近年报的真实字段。
- 移除未赋值的 `catalyst_table_ctx` 依赖，直接注入带引用的研报、会议纪要和机构调研事件。
- 删除 `_a_share_profile` 的“关键假设/数据缺失/模型不确定性/不构成投资建议”四条静态风险。
- 新增 source-backed fallback：仅从目标公司的研报、纪要和调研风险句生成 3-4 条带引用风险。
- 风险 fallback 前置到死引用清理之前，保证新引用的参考资料不会被提前删除。
- 新增最终质量门禁：3-4 条、加粗标题、每条有 `[N]`、标题不重复、单条不超过 70 字且不得命中通用模板；修复失败时 fail closed。

### Tests

- 新增 `tests/datayes-company-onepaper/test_a_share_risk_logic.py`，覆盖 `•/-/*`、模板拦截、证据提取、财务快照、fallback 与整章修复。

### Files changed

- `scripts/a_share_report_writer.py`
- `tests/datayes-company-onepaper/test_a_share_risk_logic.py`
- `references/a-share-quality-checklist.md`
- `SKILL.md`
- `README.md`
- `CHANGELOG.md`

## v1.2.9

Status: Bullet normalization + §4.5 Q&A extraction rewrite + doc cleanup.

### P0: 列表标记统一（`•`）

所有 LLM prompt 正文列举项统一改为 `•` 无序符号，替换遗留的 `1）2）3）` / `-` / `*` 混用：

- **`_normalize_bullet_markers`**（新增）：后处理正则归一化，将 `-/*/1)/2)/3)/1）/2）/3）` → 顶格 `•`，覆盖 LLM 格式漂移兜底。
- **`markdown_to_docx.py`**：扩展无序列表正则匹配 `•`，渲染为零缩进，与 prompt 输出对齐。
- **LLM prompt 集中修复**：`gen_section2`、`gen_section3`、`gen_section4_deep`（含 §4.4，上次遗漏）、`_a_share_profile` long_term 模板等全部替换。
- **`a-share-report-structure.md`**：列表标记规范从"避免中文序号"升级为"统一 `•`"，所有示例同步更新。

### P0: §4.5 调研问答生成重写

旧版 `_fallback_qa_from_raw` 依赖单层正则直接拼装 Markdown，对异构格式（`question:/answer:`、`N、...答:`、同行多对 Q/A、纪要/报告体无标记文本）覆盖率不足，输出质量不稳定。

方案：**分层管线的确定性生产架构**——正则提取候选结构化列表 → LLM 精选+压缩 → 代码排版：

- **`_extract_qa_candidates`**（重写）：双路径正则引擎：
  - 路径 A：归一化 `**Q：**`/`Q1、`/`question:` → 按 Q/A 双向拆分，递归处理同行多对
  - 路径 B：中文序号 `N、...答:...` 格式解析
  - 返回 `[{"q", "a", "ref"}, ...]` 结构化列表（上限 10 条），不含 Markdown
- **`_format_qa_markdown`**（新增）：确定性排版——Q 末尾补 `？`、A 截断至 400 字（句号自然断句）、引用末尾去重追加 → 输出 `**Q：**` / `**A：**` 两行格式
- **`_llm_fallback_extract_qa`**（新增）：正则完全无法提取时，LLM 从任意格式（纪要/报告体）提取 Q&A 对，支持 compact retry
- **`_normalize_survey_qa_markdown`**（精简）：从 17 行堆砌 regex 精简为 7 行核心兜底，只处理 `Q:/A:` → `**Q：**`/`**A：**` 加粗归一化
- **`gen_section4`** 重构：§4.1 + §4.5 合并调用中，§4.5 走新管线生成；候选不足时自动从 `surveys` 的 `ref_map` 补齐引用
- **LLM select prompt**：从候选人中选 3-4 组最有基本面价值的问答，判断标准为业绩驱动 > 一般行业展望，回答超 200 字压缩至 200 字内
- **调研引用溯源**：`build_ref_map` + 参考资料格式化新增 `"调研"` 类型，为 `institution_research_detail` 提供独立引用编号

### P1: 格式与占位清洗

- **`_normalize_final_markdown_format`**：新增占位文本正则 `第X节：…` 移除，消除 LLM 泄漏的章节标注
- **`_fix_truncated_chinese`**（新增）：修正截断处残留的半角字符
- **§2.1 标题强制检测**：`_enforce_v124_a_share_blocks` 增加兜底——`## 2` 后无 `### 2.1` 子标题时自动插入
- **§9.4 核心变量加冒号**：prompt `• **[变量]**：[数值]`（变量名与数值间补冒号）
- **§10 风险标题去双写**：enforcer `split('风险')[0]` → `split('：')[0]`，消除 `"风险**：风险：**"` 双写
- **关注事项 prompt 换行显式化**：Q&A 两行格式要求写入 prompt，非仅后处理

### 移除

- **图表本地化回退**：v1.2.9 初期尝试的 `_download_chart_images` 已移除，图表保留远程 URL，由 `markdown_to_docx.py` 的 `try_insert_image` 负责 DOCX 嵌入。
- **§3.2 Resolve Before Fetch**：`--resolve-only` 参数在脚本中未实现，SKILL.md §3.2 整节删除，§3.3-3.5 重编号为 §3.2-3.4。README + a-share-api-interfaces.md 同步清理引用。

### Files changed

- `scripts/a_share_report_writer.py`：列表统一 + §4.5 重写 + 格式清洗（~777 行 diff）
- `scripts/markdown_to_docx.py`：`•` 零缩进渲染
- `references/a-share-report-structure.md`：列表规范 + §4.5 节新增 + 全量示例更新
- `references/a-share-api-interfaces.md`：`--resolve-only` 引用清理
- `SKILL.md`：§3.2 移除（重编号 §3.2-3.4）+ Appendix A 更新
- `README.md`：`--resolve-only` 描述清理
- `CHANGELOG.md`：this entry

### Unchanged scripts

- `a_share_fetch_data.py`：与 v1.2.8 完全一致
- `hk_us_report_writer.py`、`fetch_materials.py`、`fetch_materials_v2.py`、`gen_charts.py`、`build_docx.py`、`llm_adapter.py`：与 v1.2.8 完全一致
- 所有港美股 reference 文件：无变化

## v1.2.8

Status: Title generation refactored — full-context post-generation across all markets.

### Title generation (A-share + HK/US)

1. **A 股标题后置生成**：LLM context 从 s1+s2(800字) → s1+s2+s5(1300字)，全文生成后再出标题；修正误将 s3(催化表格)当作投资逻辑喂 LLM 的 bug。
2. **港美股标题后置生成**：新增 `_gen_full_context_title()`，收集 s12+s34+s57(≤1800字) 全文生成标题。删除旧降级链 `_derive_title_conclusion` / `_repair_title_from_verified_sections`，移除 §§1&2 JSON 中的 `title_conclusion` 字段。
3. **移除硬编码兜底**：A 股 `_fallback_title_conclusion` 清空中际旭创特例及 "核心主业稳健，盈利修复可期" 通用字符串。
4. **统一降级策略**：两边统一为 `LLM → _build_deterministic_fallback_title` (从生成章节提取关键词拼接)，0 层硬编码。

### Bug fixes

5. **港美股引用全角修复**：`normalize_refs` 入口新增 `re.sub(r'【(\d+)】', r'[\1]', text)`，修复 DeepSeek-V4 产出全角 `【N】` 导致的 §5 内联-尾部双套引用。
6. **A 股 `None` 防护**：`_compact_reports`(line 1276) / `gen_peer_table`(line 2644) 中 `r['abstract']` / `r['text']` 为 `None` 时加 `or ''`，修复宁德时代(300750)因同行研报摘要缺失导致的崩溃。

### Verification

- 6/6 跨市场验证通过：茅台/宁德(A)、腾讯/美团(HK)、Apple/Tesla(US)，全部由 LLM 全文生成标题，0 篇落入兜底。

### Files changed

- `scripts/a_share_report_writer.py`: 标题逻辑重构 + `None` 防护 ×3
- `scripts/hk_us_report_writer.py`: 标题逻辑重构 + `normalize_refs` 全角修复
- `SKILL.md`: 版本号 + Appendix A 新增 v1.2.8
- `README.md`: 版本号 + 变更表 + 验证状态
- `CHANGELOG.md`: this entry

## v1.2.5

Status: Auto-repair pipeline + HK-US automation enhancement.

### P0: 港美股自动化 post-repair

1. **`hk_us_post_repair_v124.py`**：港美股独立 post-repair 脚本，8 项修复能力：
   - 参考资料 ID 修复（100% real ID，禁止 SRC-NN / missing_from_source_trace）
   - 稀疏行列自动清理
   - §11.2 空机构预测表自动省略
   - 催化事件表补齐检测
   - 情景推演补齐检测
   - 同业比较表补齐
   - 内部 pipeline 话术清除
   - 港美股最小结构检查 + 参考资料时效性检查
   - 支持 `--materials` 和 `--id-audit` 双输入源

2. **`hk_financials.py`**：港美股 PIT 三表数据聚合函数：
   - 从 getHkFdmtIsPit / getHkFdmtBsPit / getHkFdmtCfPit 聚合 FY2023/FY2024/FY2025 主要指标
   - 映射收入、毛利、经营利润、归母净利润、EPS、经营现金流、资产负债率等
   - 缺失项保留 None，不硬造
   - 输出 source_api、id_field、id_value、payload_hash
   - 支持 `--dry-run` 最小测试

### P1: A股 report_writer.py 增强

3. **催化事件表自动补写**：组装报告后自动扫描 §3 催化事件时间表，若表格行数 <3 或为空壳，自动调用 LLM 从研报/纪要/公告素材提取事件并生成 ≥6 行完整表格。
4. **情景推演自动替换**：若 §9.5 含模板话术（"基于核心变量乐观假设""基于EPS×PE=目标价"等），自动调用 LLM 用基准财务数据生成含 EPS×PE=目标价的三档可计算情景。
5. **修复日志输出**：完成修复后打印摘要，方便排查（如 `v1.2.5 自动修复: ['催化事件表不足3行→LLM补写 ✅']`）。

### 规范与 checker 补齐

6. **近况跟踪句首加粗规范**：§1/§2 每条 bullet 必须以 `**加粗关键词**` 开头 + 具体数字/事实 + `[N]` 引用。checker 新增 check 39。
7. **参考资料时效性约束**：Materials V2 检索优先近 1 个月；超 6 个月来源须标注 `old_source_reason`；估值/预测/催化过旧引用不可接受。checker 新增 check 37。
8. **§11.2 整节省略规则**：空预测表→P1，应整节省略。checker 新增 check 38，post-repair 可自动删除。
9. **港美股特殊行业适配**：保险/银行/科技互联网/资源周期/REITs/生物医药指标体系已在 `hk-us-report-structure.md` 补齐。

### Files changed (本轮 v1.2.5 补充):
- `scripts/hk_us_post_repair_v124.py`: +`--id-audit` 参数支持，双输入源
- `scripts/hk_financials.py`: 新建，PIT 三表聚合函数
- `scripts/check_report_quality_v123.py`: +check 37（时效性）、check 38（§11.2省略）、check 39（加粗规范）
- `references/hk-us-report-structure.md`: +近况跟踪§2加粗规范
- `references/hk-us-quality-checklist.md`: +v1.2.5 检查项（加粗、时效性、§11.2省略、PIT聚合）
- `references/a-share-quality-checklist.md`: +v1.2.5 检查项
- `SKILL.md`: +v1.2.5 港美股 post-repair、PIT 聚合、加粗规范、时效性约束、§11.2 省略规则
- `CHANGELOG.md`: this entry (updated)

### Already present (A股 pipeline):
- `scripts/report_writer.py`: `_v124_post_repair()`, `_extract_section()`, `_gen_catalyst_table()`, `_gen_scenario_table()`

## v1.2.3

Status: Skill rule optimization — no pipeline or regression changes.

Applied optimizations (8 core problem areas from real report review):

1. **章节编号规则**：H2 与模板一致；H3 必须继承父 H2 编号（如 `## 3` → `### 3.1`、`### 3.2`）；同级连续不重复不倒序；输出前逐章验证。
2. **数据获取优先级**：结构化接口 → Materials V2 → 研报全文 → 研报图表 → 公告/纪要/披露 → 公开权威来源。严禁第一级无数据即放弃。
3. **稀疏行列处理**：行≤1 有效→删行；列≤1 有效→删列；不足 2 指标/2 维度→删表。不提 `—`/`N/A`/空格/待补充，不编造，不写"已隐去"。纯定性表豁免。
4. **正文行内引用**：核心结论/财务/经营/行业/预测/估值/催化剂/风险中的事实和数字必须有行内 `[N]`，非仅在末尾列参考资料。双向闭环：正文引用 ⊆ 参考资料 且 ⊇ 正文引用。
5. **参考资料精确格式**：`[N]`来源类型 | 日期 | ID：值 | 机构 | 标题 | API：值。序号后不加空格，字段顺序固定，元数据从 JSON 逐字复制。公开网页含完整 URL。
6. **参考资料覆盖**：不设机械数量但核心维度均有来源；材料充足时交叉验证；不加入未使用资料。
7. **派生测算**：必须写"内部测算"+基础数据+公式+单位+假设；无法完整复核则只保留定性判断。
8. **特殊行业适配**：新增保险（NBV/EV/P-EV/偿付能力）、银行、科技/平台、资源/周期、REITs、生物医药等行业指标体系；严禁套用不适用指标。
9. **输出前自检清单**：12 项最终检查，任一项不满足不得输出。

Changed files:
- `SKILL.md`：通用写作规范中新增全部 8 大规则 + 自检清单
- `references/a-share-report-structure.md`：强化章节编号、数据优先级、稀疏行列、参考资料格式、行业适配
- `references/hk-us-report-structure.md`：新增章节编号、数据优先级、稀疏行列、统一参考资料格式、行业适配章节
- `references/a-share-quality-checklist.md`：新增 v1.2.3 检查项（12 项）
- `references/hk-us-quality-checklist.md`：新增 v1.2.3 检查项（13 项）
- `CHANGELOG.md`：本文

No changes to: v1.2.0, v1.2.1, v1.2.2, pipeline scripts, evaluator, or root-level tools.

## v1.2.1

Status: hotfix for production launch.

Applied fixes:

- Add sensitivity-number rule: source-given sensitivities may be cited; self-derived must be labeled "内部测算" with formula/base/unit/assumptions; unverifiable sensitivities must be deleted.
- Add FY/CY time-caliber rule: fiscal year, calendar year, quarter labels must preserve source original labels; never default-interchange (e.g. NVDA FY2027 ≠ CY2027).
- Extend §11.4 in report structure with sensitivity formatting examples.
- Extend quality checklist with sensitivity and FY/CY verification items.

Changed files:

- `SKILL.md`
- `references/hk-us-report-structure.md`
- `references/hk-us-quality-checklist.md`
- `CHANGELOG.md`

## v1.2.0

Status: active regression revision.

Applied fixes:

- Freeze `skills/v1.1.0` as the passed HK baseline; all new changes are made under `skills/v1.2.0`.
- Formalize US-market fallback: do not require HK PIT structured financial APIs for US cases.
- Require US financial fallback to be explicit in the evaluator as `skipped_with_reason` / `N/A`; skipped checks must not be counted as passed checks.
- Extend US-case validation focus to GAAP / non-GAAP separation, segment actuals, company guidance, fiscal-year versus calendar-year labels, currency/unit labeling, and US valuation frameworks.
- Add US ADR regression coverage requirement for BABA alongside NVDA.

Changed files:

- `SKILL.md`
- `references/hk-us-report-structure.md`
- `references/hk-us-quality-checklist.md`
- `CHANGELOG.md`

## v1.1.0

Status: frozen passed revision.

Applied fixes:

- Enforce citation completeness before report delivery.
- Prevent Forecast, Guidance, and Actual data from being mixed.
- Standardize metric definitions, especially CapEx and ROE.
- Require dated evidence for peer comparison rows.
- Package public URL evidence into materials JSON for offline replay.
- **Enforce verbatim copy of title/organization from JSON metadata** — no truncation, prefix stripping, or type label substitution. Add `source` field fallback when `organization` is empty.

Changed files:

- `SKILL.md`
- `references/hk-us-report-structure.md`
- `references/hk-us-quality-checklist.md`
- `scripts/fetch_data.py`
- `scripts/fetch_materials.py`
- `scripts/gen_charts.py`

Compliance updates:

- Updated Datayes token URL to `https://ai.datayes.com`.
- Unified metadata API references to `https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api`.
- Removed hardcoded `gptMaterials/v2` business URL from references.
