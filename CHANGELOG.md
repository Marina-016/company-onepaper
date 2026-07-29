# Changelog

## v1.1.0

Status: active working revision.

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
