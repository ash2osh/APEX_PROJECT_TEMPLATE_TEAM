# Template review remediation results

**Run date:** 2026-10-13
**Target:** APEX 26.2 template, SQLcl 26.3.0.0 minimum
**Plan:** [`2026-10-13-template-review-remediation.md`](../2026-10-13-template-review-remediation.md)
**Spec:** [`2026-10-13-template-review-remediation.md`](../../specs/2026-10-13-template-review-remediation.md)

## Status

All seven source findings are implemented and have named regression coverage. The fresh review found one Important gap in APEXlang normalization; a test-first fix now preserves valid unquoted scalar and array content. The final full offline suite passed. Live Oracle/Builder qualification, native Windows CI, and a 26.1 backport were not performed.

The implementation changes cover 25 previously tracked files (`1,406` insertions, `195` deletions), plus this result report. The review package SHA-256 is `499a3e724aa808ddef08ff40872dbfd709a0b40b24b357a48e63b33bdbf425dc`; it predates the test-first follow-up fix prompted by the reviewer.

## Findings

1. **APEXlang literal fidelity — implemented.** Literal-aware normalization preserves quoted and fenced content, plus valid unquoted scalar and array payloads. Changed scalar/array trailing spaces and array entries beginning with `//` remain source mismatches; only structural line whitespace is normalized. Ambiguous literal boundaries receive no formatting exemption.
   - Negative coverage: `test_fenced_plsql_trailing_space_is_significant`, `test_javascript_template_comment_is_literal`, `test_unicode_line_separators_are_preserved`, `test_escaped_triple_fence_does_not_close_literal`, and `test_ambiguous_literal_is_raw_mismatch_not_cosmetic`.
   - Unquoted regression coverage: `test_unquoted_scalar_trailing_spaces_are_content`, `test_unquoted_array_entries_starting_with_comment_marker_are_content`, `test_unquoted_array_entry_trailing_spaces_are_content`, and `test_unquoted_array_payload_difference_does_not_advance_marker`.
   - Positive coverage: `test_comments_messages_report_columns_and_trailing_space_are_canonicalized` and `test_quoted_parenthesis_does_not_break_component_reordering`; the former confirms structural declaration whitespace still normalizes.
   - Focused result: 34 normalization/verifier tests passed after the review fix; the original 67-test normalization, verifier, partial-publish, and upgrade run also passed.

2. **Automation restore identity — implemented.** The writer session and the workspace/application scope are checked before restore; a mismatched or unavailable post-read cannot report restore success.
   - Negative coverage: `test_restore_identity_drift_never_passes`, `test_restore_scope_drift_blocks_even_without_active_automations`, and `test_restore_failed_identity_guard_and_unavailable_post_read_need_attention`.
   - Positive coverage: `test_restore_enables_only_pre_import_active_automations_and_prints_readback`.
   - Focused result: 32 lifecycle and automation-operation tests passed. Fake publish/promotion flows are also covered by the complete suite.

3. **Backup manifest integrity — implemented.** Bash and PowerShell require exact object-type rows and bounded ASCII decimal counts; malformed manifests refuse before replacing existing mirrors.
   - Negative coverage: `test_invalid_manifest_preserves_existing_mirror_in_both_wrappers`.
   - Positive coverage: `test_valid_manifest_variants_have_bash_powershell_parity`, including explicit zeros, CRLF, blank lines, and leading-zero counts.
   - Focused result: 6 backup CLI tests passed; 101 related backup, replace, and ORDS tests also passed.

4. **Environment comparison scope — implemented.** Equal observed database/container/schema/edition scope is blocked even when environment labels or connection aliases differ; a different schema or edition remains comparable.
   - Negative coverage: `test_compare_env_refuses_same_observed_scope` and `test_compare_env_capture_errors_remain_blocked`; the same-scope case verifies no DBA script is emitted.
   - Positive coverage: `test_compare_env_allows_same_database_different_schema_or_edition`.
   - Focused result: 50 compare-env and compare-schema tests passed.

5. **Timezone type dispatch — implemented.** Precision-qualified `TIMESTAMP WITH TIME ZONE` spellings use the timezone-preserving typed formatter; malformed types and values without offsets refuse, while plain and local-time-zone types remain distinct.
   - Negative coverage: `test_tstz_requires_a_timezone_even_for_date_only_values` and `test_malformed_timestamp_type_does_not_get_a_timestamp_fallback`.
   - Positive coverage: `test_precision_qualified_tstz_preserves_offset_fraction_and_format_mask` and `test_precision_plain_timestamp_and_local_timezone_remain_separate`.
   - Focused result: 42 baseline tests passed, including baseline-build coverage for precision-qualified metadata.

6. **Rollout receipt preservation — implemented.** Every selected execution path is checked before execution. Existing files, directories, malformed receipts, and symlinks are preserved and refused; receipt installation uses an atomic no-overwrite link.
   - Negative coverage: `test_rerun_preserves_original_receipt_without_executing`, `test_malformed_and_symlink_receipts_are_refused_without_mutation`, `test_resume_refuses_receipted_step_in_selected_range`, `test_dry_run_refuses_receipted_step_in_selected_range_without_source_mutation`, and `test_receipt_install_race_preserves_existing_bytes`.
   - Positive coverage: `test_resume_requires_matching_receipts_and_skips_only_prior_steps` verifies a genuine unfinished-step resume with valid earlier receipts.
   - Focused result: 60 rollout and migration-manifest tests passed. If installation fails after execution, the report preserves evidence and says the step may have completed without a receipt. This does not serialize cross-process or database writes.

7. **Graphify guidance — implemented.** Operator docs use the isolated project helper and query executable paths; semantic backend access/cost terms are explicit, Graphify remains optional, and database mirror refresh remains a separately requested source refresh.
   - Regression: `test_operator_graphify_guidance_uses_project_environment` checks helper commands, rejects global setup/extract/update examples, checks Unix and Windows query paths, and verifies the Claude skill points to the canonical Agent skill.
   - Focused result: the Graphify project/corpus, pipeline, template component/manifest, upgrade, and documentation modules passed (147 tests).
   - No package installation, Graphify extraction, backup, or export was run for this documentation change.

## Whole-change verification

- `uvx ruff check scripts tools tests` — passed.
- `python3 -m compileall -q scripts tools tests` — passed.
- `bash -n` on all 26 tracked shell scripts — passed.
- PowerShell Parser on all 12 tracked `.ps1` files — passed under PowerShell 7 on Linux; this is not native Windows runtime qualification.
- `git diff --check` — passed. A direct LF scan found no CR bytes in the 25 changed tracked files and the two plan/spec documents. Documentation tests validated local links and embedded JSON examples; template manifest/ownership tests passed in the full suite.
- The full suite also passed the valid base-environment fixture (`test_valid_base_config_parses_expected_keys`), original-template offline fixture (`test_original_template_with_local_config_does_not_need_downstream_lock`), and template ownership/JSON example tests.
- `scripts/team.sh template-status --format json` — exit 0, local status `current`; upstream comparison was not checked.
- `scripts/team.sh verify-local --format json` — exit 2 on the pre-existing checkout state. The report found retired uc-apx keys in the preserved root `.env`, no downstream `.template-lock.json` in this original template checkout, missing DEV descriptors in the pre-existing sample apps, and an optional stale Graphify state. The template manifest and Oracle skill registry checks passed. These inputs were left untouched.
- After the final report edit, `PYTHONPATH=tests:. python3 -m unittest -v test_documentation_contract test_template_manifest` — 35 tests passed.
- `python3 -m unittest discover -s tests -v` — **1,618 tests ran: 1,586 passed and 32 skipped**, exit 0, 625.805 seconds. Skips were platform/runtime gates: 11 tests rejected the sandbox-blocked Snap PowerShell launcher, 7 require Windows PowerShell and Git Bash, and 14 require Windows-specific process, signal, or launcher behavior. Full output is preserved at `scratch/template-review-remediation-20261013-full-suite.log` with exit status in the matching `.exit` file; the plan workspace retains the original `task-8-tests.log`.
- The suite emitted one `ResourceWarning` for an unclosed SQLite connection in unchanged `tests/test_catalog_transport.py:120`. Other Graphify warnings came from negative/simulated fixture cases. No test failed.

## Independent review

- A fresh-context read-only reviewer found 0 Critical, 1 Important, and 0 Minor issues. The Important issue was that valid unquoted APEXlang scalar trailing spaces and array entries beginning with `//` could be treated as formatting/comments. The 26.2 grammar confirmed the concern.
- The fix added four regressions; all failed before the change for the expected false-normalization behavior. After the fix, all 34 focused normalization/verifier tests passed, and the full 1,618-test suite passed. Per the executing-plans review protocol, there was no second review.
- The reviewer declined to judge live Oracle/APEX enforcement, native Windows runtime behavior, and a 26.1 backport. Their status and costs if wrong are recorded as rulings in the plan ledger.

## Boundaries and remaining gates

- Oracle skills were current at startup: oldest relevant recorded installation `2026-10-11T03:31:05.585689846Z`, 10 installations. No live database or shared Builder operation was run.
- Live checks remain unverified: disposable-target identity/restore refusal, fenced-string import/export fidelity, live typed reference-data capture, and workflow behavior on Windows. Production writes and shared Builder imports remain outside this offline run.
- Linux CI and native Windows CI were not run as part of this offline verification. Native Windows remains unqualified.
- The actual `codex/apex-26.1` branch is `0bb72a356aa8b6cb6242e4e20b4653801037679a`. It lacks `scripts/apexlang_normalization.py`, `scripts/application_lifecycle.py`, `scripts/compare_env.py`, `scripts/baseline.py`, and `scripts/graphify_project.py`; `scripts/backup_db.sh`, `scripts/backup_db.ps1`, and `scripts/rollout.py` exist there with separate tests. Do not copy the 26.2 APEXlang adapter or helper references blindly. Any 26.1 backport needs its own source-path audit, release-specific design, and tests.
- Preserved pre-existing `.env`, `apps/DEMO/`, `database/DEMO/`, `uv.lock`, and the supplied plan/spec. No changes were made to those user inputs.
