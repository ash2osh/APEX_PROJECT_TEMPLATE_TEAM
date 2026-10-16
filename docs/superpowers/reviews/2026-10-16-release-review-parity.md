# APEX 26.2 review parity with the confirmed 26.1 findings

Reviewed base: `2b071c0` in `ash2osh/APEX_PROJECT_TEMPLATE_TEAM`.
Reference repairs: `1985fed` and `9c233a4` in the separate 26.1 template.
Changes were adapted inline; release-specific contracts were not copied between repositories.

## Findings and disposition

| Prior finding | Observed on the 26.2 base | Result |
| --- | --- | --- |
| F1: malformed comment conceals SQLcl commands | Already refused by the full SQL-only parser. | Retained that stronger parser and added the rollout/ORDS regression. Valid standalone COMMIT/ROLLBACK were also incorrectly refused; rollout alone now opts into these statements. Migration parsing remains strict by default. |
| F2: blank-line rollout delivery | Driver omitted SQLBLANKLINES ON. | Enabled before the payload include, covering SQL and ORDS rollout steps. |
| F3: malformed backup manifests | Both wrappers already enforce the type inventory, duplicates, ASCII counts, range and sum limits before replacing mirrors. | Production code retained. Added form-feed/vertical-tab acceptance and Unicode whitespace/separator refusal cases; both wrappers preserve old mirror bytes on failure. |
| F4: DBA connection aliases rejected | Python, Bash and PowerShell already accept/validate configured aliases. | Existing 26.2 configuration contract retained. |
| F5: wrong 26.1 upstream | The 26.2 manifest correctly selects TEAM with schemaVersion 2 and apexRelease 26.2. | Not applicable; historical 26.1-lock recovery must not rewrite the 26.2 upstream. |
| F6: nullable additions lack definition checks | Reproduced. | Every added column receives an owner/table/column-scoped definition postcondition. |
| F7 / R4: lost or truncated numeric attributes | Reproduced NUMBER scale-only loss, FLOAT precision loss, missing bounds checks and fractional truncation. | Preserve NUMBER(p,s), NUMBER(*,s), FLOAT(p); reject fractional/nonfinite/boolean metadata and invalid ranges. |
| F8: generated views checked only for existence | Reproduced. | Require exact owner/name/type, VALID status and no matching compilation errors. |
| F9: Graphify distribution typo | No graph extra remains; project-local requirements already pin graphifyy. | No production change. The pinned Python 3.12 dependency set resolved with uv without installation. |
| R1: SQL BOOLEAN accidentally rejected | SQL BOOLEAN was already supported. | Preserved; executable scoped column-check regression added. |
| R2: historical TEAM lock recovery | Specific to the separate 26.1 repository and schema-1 lock. | Not copied into the 26.2 contract. |
| R3: wrong receipt filename in tests | The new column/view regression tests were absent. | Added tests for the real status.dev.json, pending events, failed-check evidence, locked retry and no extra writes. |
| R5: incomplete ASCII whitespace | Both 26.2 wrappers already trim the full relevant ASCII set. | Expanded parity coverage passed without changing their implementations. |

## Additional shared defect

Generated compiler-setting postconditions concatenated the object TYPE predicate
and first setting predicate without AND. The same defect was independently
confirmed in 26.1. Both now include the conjunction. A new test executes the
production query against catalog-shaped rows, checks null/non-null conditional
flags, and rejects missing rows and every owner/name/type/setting mismatch.
It failed with a SQL syntax error before the fix in both repositories.

## Verification and merge gates

- New baseline and driver regressions failed on the reviewed base for the
  missing postconditions, lost metadata and missing SQLBLANKLINES setting.
- Focused baseline, bundle, migration receipt, rollout, SQL driver, settings,
  lexer and backup-wrapper modules passed after adaptation. The full lexer
  tests retain rejection of transaction completion in ordinary migrations.
- Ruff, Python compilation, Bash syntax, example-environment loading and
  `git diff --check` passed. The approved golden check-file hash changes only
  for the added view-validity check and corrected compiler-setting SQL.
- Full offline suites and exact-candidate platform CI are required before
  merging; their final counts and commit identities are recorded in the PR
  and ignored `scratch/review-parity-2026-10-16/` evidence.
- No new live database operation was performed. Earlier local 26.1 database
  qualification is evidence for its recorded commit/target only, not live
  APEX 26.2 qualification. Skipped native-platform cases remain unexecuted locally.
- The existing primary TEAM checkout and its unrelated untracked configuration,
  application source, mirrors and plans were preserved. Work used the clean
  existing 26.2 worktree on `codex/fix-26-2-review-findings`.
