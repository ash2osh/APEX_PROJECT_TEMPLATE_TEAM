# Phase 9: restore active automations after import; normalized post-import verification

Date: 2026-10-11. Branch `feature/post-import-automations-verify` (from `main` at `9aed8a2`). Requested by the ABSHRY downstream project; implemented with Codex (gpt-6-luna) and finished and reviewed by the coordinating agent.

## Problems

1. An APEX application import disables every automation. The downstream project re-enabled them by hand after each deploy; forgetting is silent.
2. The post-import verification compared the re-exported APEXlang with the source byte for byte and reported only the first mismatching file. On APEX 26.1 the re-export differs cosmetically (no `//` comment lines, `textMessage`, report `column` and saved-report `displayColumn` blocks in alphabetical order, trailing blanks), so a real difference in a later file could hide behind a cosmetic one.

## What changed

- **Automation restore** (`scripts/application_lifecycle.py` / `.sql`, `publish_app.sh` / `.ps1`, `deploy.sh`, `partial_publish.py`): full publish, selected-page publish and `deploy` capture the ACTIVE automations of the target application before the import, and after a successful import call `APEX_AUTOMATION.ENABLE` for exactly those (security group set first), read back and print a before/after table. Nothing that was not ACTIVE is ever enabled. A previously active automation that is not ACTIVE afterwards fails the command (`automation restore did not verify`). `--no-restore-automations` keeps the old behaviour. `deploy --manual` lists the restore step in the DBA runbook. The typed `[y/N]` confirmation is unchanged.
- **Normalized verification** (`scripts/apexlang_normalization.py`, `scripts/verify_publish_state.py`): both sides are normalized (drop `//` comment lines outside backtick text, sort `textMessage` blocks by key and language, `column` blocks by name and `displayColumn` blocks by column, strip trailing whitespace and blank lines). All files are compared; every real difference is reported with the file and the first differing line (no content); cosmetic-only files are listed on one summary line and pass. `--strict-bytes` keeps raw byte equality.
- **Baseline rule** (`docs/publish-rules.md`): the DEV drift baseline advances only when the normalized source matches, a second re-export has the same normalized form, and the live Builder revision is unchanged across both exports. Otherwise the old baseline stays (`post-import APEXlang source was not stable ...`).
- Documentation: README, AGENTS.md, `docs/publish-rules.md`, `docs/TROUBLESHOOTING.md`, `docs/known-limitations.md`, `docs/partial-publish.md`; the documentation-contract test follows.

## Verification

- Full suite `python -m unittest discover -s tests`: 1,446 tests (32 skipped), including new tests for the capture/restore logic, the never-enable-extra rule, the failure exit code, the opt-out, the normalizer on realistic fixtures, a real difference hidden behind a cosmetic one in an earlier file, the strict flag and the unstable second export. `ruff check scripts tools tests`: clean (three findings fixed by the coordinator: two `zip(strict=False)` and one unused variable).
- All tests use the template's fake SQLcl and fixtures. **Not verified against a real database:** the `APEX_AUTOMATION.ENABLE` call path and the PowerShell scripts on Windows.

## Limits and open questions

- The restore uses the static ids captured before the import; an automation created by the import itself is not enabled (by design).
- Selected-page publish keeps its whole-app exact-source rule after synchronization.
- The ABSHRY project does not run these template scripts yet (its upgrade script predates manifest schema 2); until it upgrades, it uses its own wrapper `scripts/abshry/dev26/redeploy.sh`.
