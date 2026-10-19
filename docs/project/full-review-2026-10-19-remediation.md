# Full review and remediation: APEX 26.2 and 26.1 templates

Review and remediation date: 2026-10-19 (Africa/Cairo). The initial review identified ten actionable findings (F01–F10): writer-session identity binding, 26.1 automation restoration, APEXlang files-mode lifecycle handling, baseline cache provenance, CRLF predicate preservation, generated rollout-attempt hashing, DEV scenario write classification, cleanup dependency accounting, malformed-environment redaction, and workflow-skill interrupt recovery. The initial evidence and detailed source references remain in the review record maintained in the working checkout.

## Remediated findings

- **F01:** 26.2 publish and rollout SQL/ORDS writers now receive the independently observed target identity. Publish validates database, service, container, edition, version, session user and schema in the writer session. Rollout emits and validates a full hex-encoded writer identity before accepting a successful step. A receipt is written only after the writer identity and independent post-observation agree.
- **F02–F03:** 26.1 publish and 26.2 files-mode upgrade now capture lifecycle state, restore exactly the previously active automations, verify read-back, and retain recovery state when lifecycle verification is unavailable or fails.
- **F04–F05:** Both baseline implementations preserve CRLF exactly. The 26.2 reference-data cache now uses an identity-bound schema-2 manifest with complete inventory, row counts and checksums; incomplete, unrelated or tampered captures are refused.
- **F06–F10:** Generated `attempt-event.*` files are excluded from frozen migration inputs while remaining evidence; DEV write classification is per scenario; cleanup tolerates only dependents successfully removed by that run; malformed environment diagnostics redact line contents; workflow-skill rename intent is recorded before mutation and backup presence is checked during cleanup.

## Live Docker qualification

- **26.2:** read-only `doctor` passed against `local-26ai` (`FREEPDB1`, `FREE`, service `freepdb1`, database `23.26.3.0.0`, `COMPATIBLE 23.6.0`, APEX `26.2`, workspace user `DEMO`). A read-only application query confirmed applications 100, 102, 104 and 200.
- **26.1:** read-only `doctor` passed against `local-apex261-sys` (`FREEPDB1`, `FREE`, service `freepdb1`, database `23.26.3.0.0`, `COMPATIBLE 23.6.0`). The available saved connection is SYS-level; a DEMO owner connection was not available, so owner-level application/runtime qualification remains unknown.
- ORDS containers, browser checks and shared/staging/production targets were not available or were not modified. No import, migration, deployment or live write was run.

## Branch and verification record

- The updated remote `main` refs were fetched before integration. 26.2 advanced to `1e1df99912c462b4b1f3f3482957a1da3ffd508b`; 26.1 advanced to `78d82ebe1c2ed0239f8bba2edf2e85dcadb7d1b4`. Their new paths did not overlap the remediation paths; each branch was rebased cleanly before push.
- Focused regression suites cover every remediation. Full offline suites, `git diff --check`, shell/Python syntax checks, and the updated CI/pre-push checks were run before the final push; exact counts and commit SHAs are reported with the pushed branches.
- The original dirty 26.2 checkout was preserved unchanged; all edits were made in isolated temporary clones.

## Limits

The Docker checks establish the local database identity and read-only metadata only. They do not qualify Oracle routing races, Builder unsaved edits, ORDS/browser behavior, or production safety. The existing upstream issue for generated rollout attempt events remains historical context; the local remediation prevents those generated files from being misclassified as frozen source input.
