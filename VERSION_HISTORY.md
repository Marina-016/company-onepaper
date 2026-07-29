# Version History — company-onepaper

Historical version import from `company-onepager-eval/skills/`.

Generated: 2026-07-29

## Version Map

| Seq | Original Directory | Git Tag | Commit Hash | Tree Hash | Content Status | Excluded Files | Notes |
|-----|--------------------|---------|-------------|-----------|----------------|----------------|-------|
| 1 | A股v0 | `archive-a-share-v0` | `3fd9975` | — | unique | 1 (.env.example template) | Initial A-share version |
| 2 | v1.0.0 | `v1.0.0` | `4b933a0` | — | unique | 0 | — |
| 3 | v1.1.0 | `v1.1.0` | `f584b58` | — | unique | 0 | — |
| 4 | v1.2.0 | `v1.2.0` | `bc54db3` | — | unique | 0 | — |
| 5 | v1.2.1 | `v1.2.1` | `dd898c8` | — | unique | 0 | — |
| 6 | v1.2.2 | `v1.2.2` | `500bcae` | — | unique | 0 | — |
| 7 | v1.2.3 | `v1.2.3` | `eff10f5` | — | sanitized | `__pycache__/` (1 dir) | — |
| 8 | v1.2.4.1 | `v1.2.4-history.1` | `8ddff6b` | — | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus` | — |
| 9 | v1.2.4.2 | `v1.2.4-history.2` | `82b8e0b` | — | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus`, `~$share-report-structure.md` | — |
| 10 | v1.2.4.3 | `v1.2.4-history.3` | `c2e49eb` | — | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus`, `~$share-report-structure.md` | — |
| 11 | v1.2.4.4 | `v1.2.4-history.4` | `f943b4f` | — | unique | 0 | Major cleanup: removed tests/, SKILL_REORG_NOTES, scripts_architecture_audit, many test files |
| 12 | v1.2.4.5 | `v1.2.4-history.5` | `0d7730c` | — | sanitized | `__pycache__/` (1 dir) | Major refactor: renamed scripts (v124 suffixes removed) |
| 13 | v1.2.4.6 | `v1.2.4-history.6` | `b4ae867` | — | sanitized | `__pycache__/` (1 dir) | — |
| 14 | v1.2.4.7 | `v1.2.4-history.7` | `a4157c3` | — | sanitized | `__pycache__/` (1 dir) | — |
| 15 | v1.2.4_final | `v1.2.4` | `bac9cef` | — | sanitized | `__pycache__/` (1 dir) | — |
| 16 | v1.2.5 | `v1.2.5` | `a56645d` | — | unique | 0 | — |
| 17 | v1.2.6 | `v1.2.6` | `9b76518` | — | unique | 0 | — |
| 18 | v1.2.7 | `v1.2.7` | `1a5f797` | — | unique | 0 | — |
| 19 | v1.2.8 | `v1.2.8` | `488a7b5` | — | unique | 0 | — |
| 20 | v1.2.9.1 | `v1.2.9-history.1` | `32de1ee` | — | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 21 | v1.2.9.2 | `v1.2.9-history.2` | `b1b29dc` | — | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 22 | v1.2.9.3 | `v1.2.9-history.3` | `b1b29dc` | — | identical | `__pycache__/` (1 dir) | **Content identical to v1.2.9.2** (SHA-256: `2705ff83...`). Both tags point to the same commit. |
| 23 | v1.2.9_final | `v1.2.9` | `be62e2e` | — | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 24 | v1.2.10 | `v1.2.10` | `72f97b3` | — | sanitized | `.git/` (nested repo), `__pycache__/` (2 dirs, 10 .pyc files) | Final stable version. Removed nested `v1.2.8/`, added `.gitignore` and `tests/` |

## Identical Versions

| Version Pair | Shared Commit | SHA-256 Fingerprint | Notes |
|-------------|---------------|---------------------|-------|
| v1.2.9.2 ↔ v1.2.9.3 | `b1b29dc` | `2705ff836a41a27090dec468e973efae54f2e47eb877de0281fe2e094e5a158c` | Both original directories contain exactly the same files. Two separate annotated tags created, both pointing to the same commit. |

## Exclusion Summary by Category

| Category | Versions Affected | Count |
|----------|-------------------|-------|
| `__pycache__/` and `*.pyc` | v1.2.3, v1.2.4.1–4.7, v1.2.4_final, v1.2.9.1–9.3, v1.2.9_final, v1.2.10 | ~75 files across 20 directories |
| `.bak` and `.bak_*` files | v1.2.4.1–4.3 | 6 files |
| `~$*` (Word temp) | v1.2.4.2–4.3 | 2 files |
| Nested `.git/` | v1.2.10 | 1 repo (~63 files) |
| `.env.example` | A股v0 | Kept (template, no real credentials) |

## Source Directory Timestamps

| Directory | Latest File Modified |
|-----------|---------------------|
| A股v0 | 2026-07-16 15:36 |
| v1.0.0 | 2026-07-07 17:19 |
| v1.1.0 | 2026-07-01 16:56 |
| v1.2.0 | 2026-07-02 09:55 |
| v1.2.1 | 2026-07-02 16:23 |
| v1.2.2 | 2026-07-03 15:50 |
| v1.2.3 | 2026-07-08 09:40 |
| v1.2.4.1 | 2026-07-16 12:04 |
| v1.2.4.2 | 2026-07-16 18:31 |
| v1.2.4.3 | 2026-07-17 16:55 |
| v1.2.4.4 | 2026-07-20 09:33 |
| v1.2.4.5 | 2026-07-20 17:07 |
| v1.2.4.6 | 2026-07-21 14:36 |
| v1.2.4.7 | 2026-07-21 17:11 |
| v1.2.4_final | 2026-07-20 11:30 |
| v1.2.5 | 2026-07-21 18:01 |
| v1.2.6 | 2026-07-22 17:02 |
| v1.2.7 | 2026-07-23 16:52 |
| v1.2.8 | 2026-07-24 10:40 |
| v1.2.9.1 | 2026-07-24 16:50 |
| v1.2.9.2 | 2026-07-27 10:10 |
| v1.2.9.3 | 2026-07-27 10:56 |
| v1.2.9_final | 2026-07-27 14:01 |
| v1.2.10 | 2026-07-28 17:51 |

## Current Version

The repository root is at **v1.2.10** (`72f97b3`).

## Security Audit

- No real credentials, API keys, tokens, or private keys found in any version.
- Only `.env.example` in A股v0 (template with commented-out placeholders).
- All `__pycache__/`, `.pyc`, `.bak`, Word temp files, and nested `.git/` excluded.
- No `.docx`, `.xlsx`, `.pdf`, `.csv`, `.jsonl`, or log files.
- No large files (>500KB) detected.
