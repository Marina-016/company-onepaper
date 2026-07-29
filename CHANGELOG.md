# Changelog

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
- `references/report-verification-prompt.md`：新增第八章 v1.2.3 核验项（7 组）
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
- `references/report-verification-prompt.md`
- `scripts/fetch_data.py`
- `scripts/fetch_materials.py`
- `scripts/gen_charts.py`

Compliance updates:

- Updated Datayes token URL to `https://r.datayes.com/auth/token/login`.
- Unified metadata API references to `https://gw.datayes.com/aladdin_llm_mgmt/web/mgr/api`.
- Removed hardcoded `gptMaterials/v2` business URL from references.
