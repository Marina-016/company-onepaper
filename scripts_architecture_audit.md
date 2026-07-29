# v1.2.4 Scripts Architecture Audit

Scope: read-only architecture audit for `skills/v1.2.4/scripts/`.

Files reviewed:
- `scripts/report_writer.py`
- `scripts/hk_us_report_writer_v124.py`
- `scripts/hk_us_post_repair_v124.py`
- `scripts/check_report_quality_v124.py`

Boundaries:
- No `.py` code changed.
- No report generation was run.
- No LLM report generation was called.
- No `SKILL.md`, `references/`, `CURRENT_VERSION`, or `v1.2.3` changes are part of this audit.

## 1. Current Pipeline

### A-share pipeline

```text
fetch_data.py
  -> report_writer.py
     -> extract_* structured data
     -> build_ref_map / refs_to_markdown
     -> table builders
     -> LLM section generators
     -> assemble_report
     -> _postprocess_v123
     -> _v124_post_repair
     -> _final_self_check_v123
     -> markdown_to_docx.py when --docx is provided
```

Current responsibilities:
- `fetch_data.py`: upstream data collection, not audited in detail here.
- `report_writer.py`: A-share report generation, data extraction, prompt construction, deterministic fallback, reference formatting, markdown cleanup, v1.2.3/v1.2.4 post-processing, final self-check, DOCX export orchestration.
- `markdown_to_docx.py`: DOCX conversion for A-share path.

### HK/US pipeline

```text
fetch_materials.py
  -> hk_us_report_writer_v124.py
     -> source_trace.json / id_audit.json
     -> section-wise generation or deterministic fallback
     -> assemble_fixed_hk_us_sections
     -> normalize_used_references
     -> hk_us_post_repair_v124.py
     -> build_docx.py
     -> check_report_quality_v124.py
     -> quality_check.json / generation_status.json
```

Current responsibilities:
- `fetch_materials.py`: upstream material collection, not audited in detail here.
- `hk_us_report_writer_v124.py`: full HK/US orchestration, material compression, source routing, prompt building, deterministic section builders, post-repair/checker/DOCX subprocess orchestration, generation blocking status.
- `hk_us_post_repair_v124.py`: post-generation repair and audit log for references, sparse tables, empty forecast section, catalyst/scenario/peer checks, pipeline phrase removal, recency warnings.
- `check_report_quality_v124.py`: layered quality gates, P0/P1/P2 issue classification, citation/data/section/table/market/hygiene checks, JSON and console output.
- `build_docx.py`: DOCX export for HK/US path.

## 2. Function Responsibility Inventory

### `report_writer.py`

| Function / Block | Current responsibility | Too long | Mixed responsibilities | Version patch | Recommendation |
|---|---|---:|---:|---:|---|
| `SYSTEM_PROMPT` | Global A-share writing rules, v1.2.3 rule reminders, data priority, sparse table and citation rules | Yes | Yes | Yes | Move prompt fragments to `generation/section_prompts.py` or config-backed prompt builder |
| `extract_financial` | Parse structured financial data | Medium | No | No | Keep, move to `pipeline/a_share_extractors.py` later |
| `extract_maincomp` / `extract_main_comp_region` | Parse product and regional composition | Medium | Partly | No | Keep, later combine with composition adapter |
| `extract_consensus` / `extract_actual_consensus` / `extract_profit_forecast` | Parse forecast and consensus data | Medium | Partly | No | Keep, move to extractor module |
| `extract_reports_summary` / `extract_meetings_summary` / `extract_surveys_detail` | Compress research, meetings and surveys | Medium | Partly | No | Keep, move to material normalization module |
| `build_ref_map` / `refs_to_markdown` / `get_ref_n` | Build citation map and reference section | Medium | Yes | Yes | Extract to `utils/citations.py`; make reference schema single source of truth |
| `gen_financial_table` | Deterministic A-share financial table | Medium | No | No | Keep, move to `utils/tables.py` |
| `gen_maincomp_fallback` | Three-level fallback for composition table | Yes | Yes | Yes | Split data fallback from rendering; use industry adapter |
| `gen_maincomp_table` | Business composition table and derived row cleanup | Yes | Yes | Yes | Split parse/render/cleanup |
| `gen_consensus_table` / `gen_forecast_table` | Forecast tables | Medium | No | No | Keep, move to `utils/tables.py` |
| `gen_sections_1_2_3` | Combined LLM generation for sections 1-3 plus deterministic fallback | Yes | Yes | Yes | Split prompt, validation, fallback table, and section parsing |
| `gen_section1` to `gen_section10` | Section-specific prompt assembly and LLM calls | Yes in aggregate | Yes | Some | Extract prompts to `generation/section_prompts.py`; keep small call wrappers |
| `gen_section4` | Combined 4.1/4.5 prompt, Q&A fallback parsing, section splitting | Yes | Yes | Yes | Split into `business_model_builder.py` and `qa_builder.py` |
| `_fallback_qa_from_raw` | Regex fallback Q&A extraction | Medium | No | No | Move to `generation/deterministic_sections.py` |
| `gen_peer_table` | LLM peer table generation, validated peer context, fallback table | Yes | Yes | Yes | Move to `generation/peer_table_builder.py`; schema from config |
| `_strip_all_dash_columns` | Sparse peer table cleanup | Medium | No | Yes | Move to `utils/tables.py`; share with checker/post-repair |
| `_a_share_profile` | Hardcoded company/industry deterministic fallback profile | Yes | Yes | Yes | Move to `configs/company_profiles.json` and `configs/industry_adapters.json` |
| `_build_a_share_peer_table` | Deterministic peer table using hardcoded profile | Medium | Yes | Yes | Move to `generation/deterministic_sections.py`; profile-driven |
| `assemble_report` | Final A-share assembly with deterministic insertion of peer/catalyst/scenario blocks | Yes | Yes | Yes | Split into `pipeline/a_share_pipeline.py` plus `generation/deterministic_sections.py` |
| `_postprocess_v123` | Dead-reference removal, renumbering, sparse cleanup, scenario fallback | Yes | Yes | Yes | Move citation repair to `utils/citations.py`; table cleanup to `utils/tables.py`; scenario fallback to builder |
| `_sparse_cleanup` | Numeric sparse table cleanup | Yes | No | Yes | Share with post-repair/checker through `quality/quality_rules.py` |
| `_final_self_check_v123` | Blocking pre-delivery self-check | Yes | Yes | Yes | Replace with checker invocation or shared quality rule layer |
| `_v124_post_repair` | A-share catalyst/scenario repair, body format fixes, EPS x PE fallback | Yes | Yes | Yes | Move to `quality/post_repair.py`; share repair primitives |
| `_enforce_v124_a_share_blocks` | Force catalyst/peer/scenario blocks after generation | Yes | Yes | Yes | Profile/config-driven deterministic repair |
| `main` | CLI, data load, extraction, parallel generation, retry, postprocess, self-check, DOCX export | Yes | Yes | Yes | Move orchestration to `pipeline/a_share_pipeline.py` |

### `hk_us_report_writer_v124.py`

| Function / Block | Current responsibility | Too long | Mixed responsibilities | Version patch | Recommendation |
|---|---|---:|---:|---:|---|
| `SECTION_MATERIAL_MAP` | Section grouping, titles, routing tags, source caps, token caps | Medium | Yes | No | Move to `configs/section_material_map.yaml` |
| `_HK_US_REPORT_SYSTEM_CONSTRAINTS` | Global prompt constraints and hygiene rules | No | Partly | Yes | Keep as prompt fragment, but source from prompt builder |
| `_find_token` / `_find_llm_creds` | Credential discovery | No | No | No | Move to shared `utils/runtime.py` |
| `_call_llm` | LLM call with fallback truncation | Yes | Yes | No | Move to shared LLM client; make fallback policy explicit |
| `compress_materials` / `_assign_sources` | Source compression and section assignment | Medium | Yes | No | Move to `pipeline/hk_us_material_router.py` |
| `_build_ref_text` / `_ref_guide` | Deterministic reference section and guide | Medium | Yes | Yes | Move to shared citation module |
| `_build_section_prompt` | Section prompt body plus hardcoded peer/scenario/hygiene rules | Yes | Yes | Yes | Move to `generation/section_prompts.py`; reference shared rules |
| `write_report` | Section-wise generation, deterministic bypass, assembly, normalization, status | Yes | Yes | Yes | Split orchestration, generation, assembly, and status accounting |
| `_normalize_section_titles` / `_strip_section_headers` / `_extract_numbered_h2_sections` | Cleanup and section routing | Medium | Yes | Yes | Move to `utils/markdown_cleaner.py` |
| `_validate_final_hk_us_sections` | Final structure validation | Medium | Yes | Yes | Delegate to checker/shared quality rules |
| `assemble_fixed_hk_us_sections` | Fixed H2 shell assembly and validation | Medium | Partly | No | Keep as assembler, move to `pipeline/hk_us_pipeline.py` |
| `_build_deterministic_1011` | Deterministic market debate and valuation sections | Medium | Yes | Yes | Move to `generation/deterministic_sections.py` |
| `_build_fallback_valuation_section` | Fallback valuation section with missing target price text | Medium | Yes | Yes | Replace template shell with fail-closed structured status |
| `_build_market_debate` | Extract market debate from materials | Medium | Partly | No | Move to deterministic section builder |
| `_target_aliases` / `_source_matches_target` | Target-company matching heuristics | Medium | Yes | Yes | Move to `utils/target_match.py`; profile-driven aliases |
| `_target_profile` | Hardcoded target profile and peer lists for NVDA/Meituan plus generic peers | Yes | Yes | Yes | Move to `configs/company_profiles.json`; generic peers should be forbidden or marked blocking |
| `_hk_financial_rows` / `_us_financial_rows_from_materials` | Financial row extraction and fallback rows | Medium | Yes | Yes | Move to market adapter; remove fuzzy US numbers from deterministic fallback |
| `_build_target_deterministic_group` | Deterministic full-section generation using profile | Yes | Yes | Yes | Split by section; make profile source explicit |
| `_build_deterministic_group` | Generic deterministic HK/US section fallback | Yes | Yes | Yes | Split by section and market adapter |
| `_build_scenario_section` | Scenario analysis from target prices | Medium | Partly | Yes | Move to `generation/scenario_builder.py` |
| `normalize_used_references` | Reference closure and renumbering | Yes | Yes | Yes | Move to shared citation module |
| `_collect` / `_build_trace` / `_build_audit` | Fetch subprocess and trace/audit construction | Medium | Yes | No | Move to pipeline module |
| `_run_repair` / `_run_checker` / `_run_docx` | Subprocess orchestration | No | No | No | Keep wrappers, move to pipeline |
| `run` | End-to-end pipeline and blocking status | Yes | Yes | Yes | Move to `pipeline/hk_us_pipeline.py` |
| `_fallback` | Skeleton report with placeholders and `待补充` | Yes | Yes | Yes | Remove from candidate path; replace with generation failure artifact only |

### `hk_us_post_repair_v124.py`

| Function | Current responsibility | Too long | Mixed responsibilities | Version patch | Recommendation |
|---|---|---:|---:|---:|---|
| `load_materials_ids` | Build title-to-ID lookup from materials/source trace | Medium | Yes | No | Move to citation/source utilities |
| `repair_references_and_source_trace` | Remove synthetic refs, match IDs by title, update source_trace counts | Yes | Yes | Yes | Split into ref parse, ref match, trace update |
| `remove_sparse_rows_and_columns` | Delete sparse rows and columns from all Markdown tables | Yes | Yes | Yes | Replace with shared table cleanup utility |
| `omit_empty_forecast_section_112` | Remove empty HK/US forecast section | No | No | Yes | Keep as rule-backed repair |
| `ensure_catalyst_timeline` | Validate catalyst row count and log need for LLM repair | Medium | No | Yes | Rename to `check_catalyst_timeline`; it does not actually repair |
| `ensure_scenario_analysis` | Detect template scenario and missing numbers | Medium | No | Yes | Rename to `check_scenario_analysis`; share with checker |
| `ensure_peer_comparison_table` | Detect peer table schema and row count | Medium | No | Yes | Share schema with checker/SKILL config |
| `remove_internal_pipeline_footer` | Remove internal pipeline/checker phrases | Medium | No | Yes | Move phrase list to quality rules config |
| `validate_hk_us_minimum_structure` | Required section presence check | Medium | No | Yes | Delegate to checker/shared structure rules |
| `check_reference_recency` | Recency rule for references | Medium | No | Yes | Move to checker; post-repair should not own severity logic |
| `repair_report` | Sequential post-repair orchestration and logging | Medium | Yes | Yes | Keep orchestration, call shared repair primitives |

### `check_report_quality_v124.py`

| Function / Block | Current responsibility | Too long | Mixed responsibilities | Version patch | Recommendation |
|---|---|---:|---:|---:|---|
| `Severity`, `GateStatus`, `Issue`, `GateResult` | Quality model | No | No | No | Keep |
| `HK_US_H2_TEMPLATE`, `A_SHARE_H2_TEMPLATE`, required section maps | Structure rules | No | No | Yes | Move to `configs/quality_rules.yaml` or shared structure config |
| `_target_semantic_profile` | Hardcoded target pollution checks | Medium | Yes | Yes | Move to `configs/company_profiles.json` |
| `check_delivery` | File presence, DOCX presence, trace presence, header metadata | Medium | Yes | No | Keep but split file existence/header metadata |
| `check_structure` | H2/H3, required chapters, title format, empty sections, forecast section | Yes | Yes | Yes | Split into structure/title/empty-section checks |
| `check_citation` | Citation closure, numbering, refs, source_trace, target price binding hints | Yes | Yes | Yes | Split into citation closure and target-price source binding |
| `check_data_integrity` | Data rules, GAAP/FY/CY/currency, formulas, target price basis | Yes | Yes | Yes | Split by rule family; config severity |
| `_check_placeholder_financials` | Placeholder financial data detection | No | No | Yes | Move rule patterns to config |
| `check_source_company_mismatch` | Trace company-match and target profile mismatch | Medium | Yes | Yes | Move profile to config and target matcher |
| `check_section_quality` | Section content quality, catalyst, peer, scenario, market concern, target price format | Yes | Yes | Yes | Split by section/rule family |
| `check_table_quality` | Markdown table validity, sparse rows, placeholders, scenario probabilities | Yes | Yes | Yes | Share parser and sparse rules with post-repair |
| `check_market_specific` | HK/US specific rules | Medium | Yes | Yes | Market adapter based rules |
| `check_hygiene` | Internal phrase leakage, placeholder leakage, temp file language | Yes | Yes | Yes | Move phrase lists to quality config |
| `check_target_price_binding` | Materials-backed target price validation | Yes | Yes | Yes | Move to `quality/target_price_binding.py` |
| `check_generation_status_file` | generation_status blocking | No | No | Yes | Keep in delivery/generation gate |
| `run_all_checks` | Gate orchestration | No | No | No | Keep |
| `format_output` / `to_json` | Output formatting | Medium | No | No | Keep |

## 3. Duplicate Rule Scan

| Rule | Current locations | Duplicate | Suggested authority | Other locations should reference |
|---|---|---:|---|---|
| Citation closure | `SKILL.md`, `references/report-verification-prompt.md`, `report_writer.py` `_postprocess_v123`, `hk_us_report_writer_v124.py` `normalize_used_references`, `hk_us_post_repair_v124.py` `repair_references_and_source_trace`, `check_report_quality_v124.py` `check_citation` | Yes | `quality/quality_rules.py` plus `utils/citations.py` | Prompts should say "must satisfy citation closure"; repairs/checker call shared code |
| Reference format | `SKILL.md`, `references/*report-structure.md`, `report_writer.py` `refs_to_markdown`, `hk_us_report_writer_v124.py` `_build_ref_text`, post-repair reference fixer, checker citation checks | Yes | `utils/citations.py` reference formatter | SKILL and references document the schema; writers call formatter |
| Peer comparison schema | `SKILL.md`, `references/*report-structure.md`, `report_writer.py` `gen_peer_table`/`_build_a_share_peer_table`, `hk_us_report_writer_v124.py` `_build_section_prompt`/deterministic groups, `hk_us_post_repair_v124.py`, checker `E6`/table checks | Yes | `configs/quality_rules.yaml` or `configs/peer_schema.yaml` | Prompts, deterministic builders, checker and repair read the same schema |
| Catalyst >=3 rows | `SKILL.md`, A-share prompts, `_v124_post_repair`, `hk_us_post_repair_v124.py` `ensure_catalyst_timeline`, checker `E3` | Yes | Shared quality rule with market-specific section number | Writers prompt for it; repair/checker use same threshold |
| Scenario sanity | `SKILL.md`, `SYSTEM_PROMPT`, A-share `_v124_post_repair`, HK/US `_build_scenario_section`, post-repair `ensure_scenario_analysis`, checker `D8`/`E8`/`T7` | Yes | `generation/scenario_builder.py` plus quality rule | Prompts only reference required fields; checker validates |
| No template shell | `SKILL.md`, HK/US writer constraints, writer `_fallback`, checker structure/hygiene/section checks, post-repair internal phrase cleanup | Yes | Checker hygiene and generation status gate | Writer should emit structured failure instead of shell report |
| No `N/A` / `未披露` / placeholders | `SKILL.md`, prompts, table cleanup, post-repair sparse cleanup, checker hygiene/table/data checks | Yes | Shared table/placeholder quality rule | Prompts use concise instruction; code calls shared list |
| P0/P1 blocking | `SKILL.md`, `hk_us_report_writer_v124.py` `run`, checker severity model, checker CLI exit code comments | Yes | `check_report_quality_v124.py` severity model and pipeline gate | Writers only consume checker result and block on P0/P1 |
| DOCX output style | `SKILL.md`, `markdown_to_docx.py`, `build_docx.py`, checker delivery check | Likely | DOCX scripts and config | SKILL documents required style; checker checks artifact presence/selected XML rules |
| GAAP/non-GAAP / FY/CY / currency | `SKILL.md`, HK/US prompt, deterministic financial rows, checker data/market checks, references | Yes | Market adapter + quality rules | Prompt and references point to adapter rules |

## 4. Hardcode / Profile Scan

| Hardcode content | File | Function / block | Risk | Suggested migration |
|---|---|---|---|---|
| A-share company profiles for `002594`, `300308`, `600519` with business, peers, catalysts, risks, chain, questions | `report_writer.py` | `_a_share_profile` | Report may inject company-specific content when data is sparse; hard to audit and update | `configs/company_profiles.json` |
| Generic A-share profile with generic catalysts and risks | `report_writer.py` | `_a_share_profile` | Can create template shell and generic events | `configs/industry_adapters.json` with fail-closed behavior |
| Deterministic A-share peer rows | `report_writer.py` | `_build_a_share_peer_table` | Uses static peers and may diverge from latest comparable set | `configs/company_profiles.json` plus source-backed peer resolver |
| Hardcoded catalyst fallback rows | `report_writer.py` | `_fallback_catalyst_table`, `_enforce_v124_a_share_blocks` | May output event-looking rows without current source evidence | `configs/company_profiles.json` only for guardrails; report rows should be source-derived |
| `v1.2.3` and `v1.2.4` comments and print messages | `report_writer.py`, `hk_us_report_writer_v124.py`, `hk_us_post_repair_v124.py`, `check_report_quality_v124.py` | Multiple | Version patches are now mixed into production flow | Changelog plus module names; runtime rules should be versionless |
| HK/US generic peers `Comparable peer A/B/C` | `hk_us_report_writer_v124.py` | `_target_profile` base profile | Directly violates current SKILL rule; can leak into report fallback | Remove from report-generating fallback; put in blocking test fixture only |
| HK/US target overrides for `NVDA`, `03690` | `hk_us_report_writer_v124.py` | `_target_profile` | Narrow company coverage and risk of stale peer sets | `configs/company_profiles.json` |
| Target aliases for META, Kuaishou, Tencent, MSFT | `hk_us_report_writer_v124.py` | `_target_aliases` | Entity matching behavior hard to test centrally | `configs/company_profiles.json` or `profiles/company_profiles.py` |
| US financial fallback phrases like "about USD 100bn quarterly revenue run-rate" and "around 70%" | `hk_us_report_writer_v124.py` | `_us_financial_rows_from_materials` | Can create fuzzy financial values from text detection | Market adapter with explicit source field extraction; otherwise skip with reason |
| Fallback valuation text "targetPrice field not disclosed" | `hk_us_report_writer_v124.py` | `_build_fallback_valuation_section` | May form publishable-looking valuation shell without target price | `generation_status.json`; block candidate generation |
| Skeleton report sections with `待补充` and "本节未形成可发布正文" | `hk_us_report_writer_v124.py` | `_fallback` | Explicit template shell; should never enter candidate path | Replace with failure artifact outside report path |
| Internal phrase blacklist | `hk_us_post_repair_v124.py`, checker `check_hygiene` | `remove_internal_pipeline_footer`, `check_hygiene` | Lists drift and may disagree | `configs/quality_rules.yaml` |
| Target pollution profiles for NVIDIA/Meituan/Meta/Maotai/BYD | `check_report_quality_v124.py` | `_target_semantic_profile` | Hardcoded semantic tests are useful but brittle | `configs/company_profiles.json` plus tests |
| Required H2 templates | `check_report_quality_v124.py` | `HK_US_H2_TEMPLATE`, `A_SHARE_H2_TEMPLATE` | Duplicates references and SKILL | `configs/quality_rules.yaml` or `references` parsed into config |
| Placeholder patterns and sparse thresholds | `report_writer.py`, `hk_us_post_repair_v124.py`, checker | Multiple | Rule drift across repair/checker/writer | `configs/quality_rules.yaml` |

## 5. Fallbacks That Can Produce Template Shells

| Location | Behavior | Risk | Suggested change |
|---|---|---|---|
| `hk_us_report_writer_v124.py::_fallback` | Writes a full report shell with empty sections, `待补充`, and placeholder peer row | Highest; creates invalid artifact that looks like a report | Write `generation_failed.md` or JSON status only; do not create candidate report body |
| `hk_us_report_writer_v124.py::_target_profile` base peers | Generic `Comparable peer A/B/C` rows | Violates current peer comparison rule | Remove generic peers or make checker block before report assembly |
| `hk_us_report_writer_v124.py::_build_fallback_valuation_section` | Valuation section with missing target price but table shell | Can pass structure while missing substance | Return empty and block, or require real target-price source binding |
| `report_writer.py::gen_sections_1_2_3` fallback | Creates deterministic s1/s2/s3 from profile and latest financials | Useful fail-closed path but can become generic | Require provenance tag for each inserted row; keep blocked if evidence is weak |
| `report_writer.py::_fallback_catalyst_table` | Profile-based catalyst rows | Can be future-looking generic event table | Source-derived event extractor should own catalyst rows |
| `report_writer.py::_enforce_v124_a_share_blocks` | Forces A-share catalyst/peer blocks after generation | May overwrite report with profile-derived block | Make this a repair decision with explicit evidence threshold |
| `hk_us_post_repair_v124.py::ensure_*` | Named "ensure" but often only logs "needs LLM补写" | May give false sense of repair completion | Rename to check/flag or implement actual deterministic repair |

## 6. Main Structural Problems

1. `report_writer.py` is doing extraction, prompting, deterministic generation, rule repair, citation repair, table cleanup, self-check and DOCX export in one file.
2. HK/US writer mixes pipeline orchestration, deterministic builders, profiles, target matching, source trace creation, subprocess calls and blocking policy.
3. Quality rules exist in at least five places: SKILL, references, prompts, post-repair and checker.
4. Post-repair and checker both inspect the same conditions but do not share rule definitions.
5. Several "ensure" functions detect but do not repair, which makes the pipeline name more optimistic than the behavior.
6. Deterministic fallbacks can emit template shells or generic peer rows.
7. Company and industry profiles are hardcoded inside generation and checker code.
8. Version labels are embedded in runtime comments, function names, docstrings and print messages.
9. DOCX style requirements are documented outside the writers but not centrally represented as testable config.
10. P1 is blocking in the HK/US pipeline, but the checker CLI comments still describe WARN for P1/P2; this should be aligned without lowering P1.

## 7. Refactor Plan

### Phase 0: Read-only audit

Goal:
- Document current responsibilities, duplicated rules, hardcodes and fallback risks.

Files:
- New `scripts_architecture_audit.md` only.

Risk:
- None to runtime behavior.

Regression test:
- Not applicable.

Human confirmation:
- Not required beyond accepting audit scope.

### Phase 1: No-behavior-change refactor

Goal:
- Split files and extract constants/config without changing output behavior.

Files:
- `report_writer.py`
- `hk_us_report_writer_v124.py`
- new modules under `scripts/pipeline/`, `scripts/generation/`, `scripts/quality/`, `scripts/utils/`
- new configs under `configs/`

Risk:
- Medium, because import paths and subprocess entry points must remain compatible.

Regression test:
- Golden-file comparison on existing saved inputs.
- Run unit tests for citation renumbering, table parsing, sparse cleanup and section assembly.
- Run checker on existing known-good and known-bad reports.

Human confirmation:
- Required for directory layout and compatibility policy.

### Phase 2: Fix deterministic fallback template shells

Goal:
- Stop skeleton reports and generic fallback peers from entering candidate validation.

Files:
- `hk_us_report_writer_v124.py`
- `report_writer.py`
- `check_report_quality_v124.py`
- new `generation_status` helpers

Risk:
- Medium-high. Some previously generated fallback artifacts will become blocked earlier.

Regression test:
- Simulate no LLM, sparse materials, missing target price and unrelated source cases.
- Assert candidate report is not emitted or is marked blocking.

Human confirmation:
- Required, because this changes failure surface and may reduce apparent throughput.

### Phase 3: Unify checker and post-repair rules

Goal:
- Make repair and checker consume the same quality rule definitions.

Files:
- `hk_us_post_repair_v124.py`
- `check_report_quality_v124.py`
- `report_writer.py` postprocess/self-check areas
- `configs/quality_rules.yaml`

Risk:
- High if severities drift; P0/P1 must remain blocking.

Regression test:
- Rule-level tests for every P0/P1/P2.
- Before/after checker JSON diff on fixture reports.
- Repair-then-check idempotency tests.

Human confirmation:
- Required for severity mapping.

### Phase 4: Expand industry adapters

Goal:
- Replace hardcoded company snippets with source-backed profiles and industry adapters.

Files:
- `configs/company_profiles.json`
- `configs/industry_adapters.json`
- `profiles/company_profiles.py`
- `profiles/industry_adapters.py`

Risk:
- Medium. Better abstractions can still encode stale assumptions if not source-backed.

Regression test:
- Cross-industry fixtures: EV, semis, liquor, internet, insurance, bank, REIT, biotech.
- Negative tests for wrong peer pollution.

Human confirmation:
- Required for adapter taxonomy and seed company profiles.

## 8. Suggested Target Architecture

```text
skills/v1.2.4/
  scripts/
    report_writer.py
    hk_us_report_writer_v124.py
    pipeline/
      a_share_pipeline.py
      hk_us_pipeline.py
      material_router.py
      generation_status.py
    generation/
      section_prompts.py
      deterministic_sections.py
      peer_table_builder.py
      scenario_builder.py
      catalyst_builder.py
      valuation_builder.py
    quality/
      check_report_quality_v124.py
      quality_rules.py
      post_repair.py
      target_price_binding.py
    profiles/
      company_profiles.py
      industry_adapters.py
      target_match.py
    utils/
      markdown_cleaner.py
      citations.py
      tables.py
      docx_export.py
      runtime.py
  configs/
    company_profiles.json
    industry_adapters.json
    quality_rules.yaml
    peer_schema.yaml
    section_material_map.yaml
```

Compatibility notes:
- Keep current CLI entry scripts as thin wrappers during Phase 1.
- Preserve output paths and file names until fixtures prove parity.
- Move behavior first, then change behavior only in later phases.

## 9. Recommended Priorities

1. Extract shared citation/reference formatter and renumbering into `utils/citations.py`.
2. Extract peer schema and sparse table rules into shared config/utilities.
3. Convert hardcoded company/industry profiles into `configs/company_profiles.json` and `configs/industry_adapters.json`.
4. Replace HK/US skeleton `_fallback` with explicit blocking generation status.
5. Split checker into rule-family modules while preserving JSON schema and P0/P1 blocking semantics.

## 10. Items Requiring Human Confirmation

- Whether generic deterministic fallback should be removed entirely or retained only as a blocked debug artifact.
- Which company profiles are allowed as seeded configs, and who owns updates.
- Whether peer selection should be purely source-derived or allowed to use curated profile defaults.
- Exact P0/P1/P2 severity mapping before checker/post-repair unification.
- Whether `v1.2.4` file names remain for compatibility after internals are split.
- Whether DOCX style verification should become checker-enforced XML checks or remain documented plus converter-owned.

