# Walkthrough 2026-11-20

## 1. Live baseline check and Windows readiness script (core `afd278b`, synced)

Two shared files, written in APEX_TEMPLATE_CORE and synced into both templates (the stamp names `afd278b`):

- **`scripts/live_verify.py`** (tests: `tests/test_live_verify.py`): the 32 live checks of the baseline generator, cleaned up from the scratch harness used on 2026-11-19 and 2026-11-20. It refuses unless the login equals `--schema` and `--confirm-target` equals the database unique name the database reports (exit 2, one read-only query, nothing created). It creates only `LVF_` objects and drops them before and after. `--list` needs no database. Documented in `docs/live-verification.md`.
- **`scripts/windows_check.ps1`** (tests: `tests/test_windows_check.py`): an offline check for a Windows machine that runs under Windows PowerShell 5.1 or PowerShell 7 with the steps environment, ascii, parse, env-loader, python-tools, team-offline, tests and pre-push. CI parses and tests PowerShell as PowerShell 7 only, so this is the first check that can run on 5.1. On a bare template `team-offline` passes when the only `verify-local` failures are `configured_app_<id>` (a template ships no application); with applications it checks the lowest id. Documented in `docs/windows-verification.md`.
- **Docs and links:** README table rows, a sentence in `docs/ci-checks.md` and `docs/live-feature-tests.md`, and two bullets in `TODO.md`.

## Verification

- **Offline, full suite on this branch:** 3,436 tests, OK, 71 skipped (543 s). The two new test modules ran 9 and 11 tests (all use mocks or `pwsh` stand-ins; no database).
- **Live, Oracle 23ai Free (23.26.3), scratch container `fq-scratch-dev`, saved connection `fqdev`, schema `FEATURE_QA`:** `scripts/live_verify.py` passed 33 of 33 in both templates (the 32 checks plus the cleanup check) and left no `LVF_` objects. Its refusals were exercised live: a wrong `--confirm-target` and a wrong `--schema` both exited 2 and created nothing. The container was stopped afterwards; `local-26ai` was not touched.
- **`windows_check.ps1` under `pwsh` 7.7 on Linux, both templates:** environment, ascii, parse, env-loader, python-tools and team-offline all PASS (exit 0). Its `tests` and `pre-push` steps were not run through the script here; the full suite was run directly (see above).
- **Review notes on `live_verify.py` (left as they are):** the "column grant is not in ALL_TAB_PRIVS" check proves absence only (column grants live in `ALL_COL_PRIVS`); the foreign-key order scenario feeds the ordering function a placeholder-shaped reference, so it proves the DDL order works rather than that the generator reads it from a live catalog; the virtual-expression comparison strips spaces and upper-cases, which is tolerant of Oracle's formatting.

## Not verified

- **Windows PowerShell 5.1 and any Windows host.** `windows_check.ps1` was written to the 5.1 rules and checked by a static scan and the PowerShell 7 parser, not run under `powershell.exe`. This is the item left for a Windows machine.
- A SQLcl temporary-script path that contains spaces (Windows user profiles) has not been tried with `live_verify.py`.
- Databases older than 23ai.
