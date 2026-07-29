# Changelog

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
