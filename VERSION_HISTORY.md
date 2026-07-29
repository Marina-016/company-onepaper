# Version History — company-onepaper

Historical version import from `company-onepager-eval/skills/`.

Generated: 2026-07-29

## Version Map

| Seq | Original Directory | Git Tag | Commit Hash | Content Status | Excluded Files | Notes |
|-----|--------------------|---------|-------------|----------------|----------------|-------|
| 1 | A股v0 | `archive-a-share-v0` | `3fd9975` | unique | 1 (.env.example template) | Initial A-share version |
| 2 | v1.0.0 | `v1.0.0` | `4b933a0` | unique | 0 | — |
| 3 | v1.1.0 | `v1.1.0` | `f584b58` | unique | 0 | — |
| 4 | v1.2.0 | `v1.2.0` | `bc54db3` | unique | 0 | — |
| 5 | v1.2.1 | `v1.2.1` | `dd898c8` | unique | 0 | — |
| 6 | v1.2.2 | `v1.2.2` | `500bcae` | unique | 0 | — |
| 7 | v1.2.3 | `v1.2.3` | `eff10f5` | sanitized | `__pycache__/` (1 dir) | — |
| 8 | v1.2.4.1 | `v1.2.4-history.1` | `8ddff6b` | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus` | — |
| 9 | v1.2.4.2 | `v1.2.4-history.2` | `82b8e0b` | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus`, `~$share-report-structure.md` | — |
| 10 | v1.2.4.3 | `v1.2.4-history.3` | `c2e49eb` | sanitized | `__pycache__/` (2 dirs), `SKILL.md.bak`, `.bak_hkus`, `~$share-report-structure.md` | — |
| 11 | v1.2.4.4 | `v1.2.4-history.4` | `f943b4f` | unique | 0 | Major cleanup: removed tests/, SKILL_REORG_NOTES, etc. |
| 12 | v1.2.4.5 | `v1.2.4-history.5` | `0d7730c` | sanitized | `__pycache__/` (1 dir) | Major refactor: renamed scripts (v124 suffixes removed) |
| 13 | v1.2.4.6 | `v1.2.4-history.6` | `b4ae867` | sanitized | `__pycache__/` (1 dir) | — |
| 14 | v1.2.4.7 | `v1.2.4-history.7` | `a4157c3` | sanitized | `__pycache__/` (1 dir) | — |
| 15 | v1.2.4_final | `v1.2.4` | `bac9cef` | sanitized | `__pycache__/` (1 dir) | — |
| 16 | v1.2.5 | `v1.2.5` | `a56645d` | unique | 0 | — |
| 17 | v1.2.6 | `v1.2.6` | `9b76518` | unique | 0 | — |
| 18 | v1.2.7 | `v1.2.7` | `1a5f797` | unique | 0 | — |
| 19 | v1.2.8 | `v1.2.8` | `488a7b5` | unique | 0 | — |
| 20 | v1.2.9.1 | `v1.2.9-history.1` | `32de1ee` | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 21 | v1.2.9.2 | `v1.2.9-history.2` | `b1b29dc` | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 22 | v1.2.9.3 | `v1.2.9-history.3` | `b1b29dc` | identical | `__pycache__/` (1 dir) | **Content identical to v1.2.9.2** (SHA-256: `2705ff83...`). Both tags point to the same commit. |
| 23 | v1.2.9_final | `v1.2.9` | `be62e2e` | sanitized | `__pycache__/` (1 dir) | Includes nested `v1.2.8/` reference copy |
| 24 | v1.2.10 | `v1.2.10` | `72f97b3` | sanitized | `.git/` (nested repo), `__pycache__/` (2 dirs) | Removed nested `v1.2.8/`, added `.gitignore` and `tests/` |
| 25 | v1.2.11 | `v1.2.11` | `b1d89b7` | sanitized | `.git/` (nested repo), `__pycache__/` (2 dirs) | **Latest stable.** Added `tests/test_ch9_no_data.py` |

## Identical Versions

| Version Pair | Shared Commit | SHA-256 Fingerprint |
|-------------|---------------|---------------------|
| v1.2.9.2 ↔ v1.2.9.3 | `b1b29dc` | `2705ff836a41a27090dec468e973efae54f2e47eb877de0281fe2e094e5a158c` |

## Exclusion Summary by Category

| Category | Versions Affected | Count |
|----------|-------------------|-------|
| `__pycache__/` and `*.pyc` | Multiple versions | ~80 files across 20+ directories |
| `.bak` and `.bak_*` files | v1.2.4.1–4.3 | 6 files |
| `~$*` (Word temp) | v1.2.4.2–4.3 | 2 files |
| Nested `.git/` | v1.2.10, v1.2.11 | 2 repos |
| `.env.example` | A股v0 | Kept (template, no real credentials) |

## Current Version

The repository root is at **v1.2.11** (`b1d89b7`).

## Security Audit

- No real credentials, API keys, tokens, or private keys found in any version.
- Only `.env.example` in A股v0 (template with commented-out placeholders).
- All `__pycache__/`, `.pyc`, `.bak`, Word temp files, and nested `.git/` excluded.
- No `.docx`, `.xlsx`, `.pdf`, `.csv`, `.jsonl`, or log files.
- No large files (>500KB) detected.
