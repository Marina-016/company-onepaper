# Changelog

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

- Updated Datayes token URL to `https://r.datayes.com/auth/token/login`.
- Unified metadata API references to `https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api`.
- Removed hardcoded `gptMaterials/v2` business URL from references.
