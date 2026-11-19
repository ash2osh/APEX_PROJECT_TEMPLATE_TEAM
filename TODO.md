# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

## Database workflow inventory — 2026-11-18

Reviewed template 26.2 at `735996c`, template 26.1 at `2572abd`, and two
downstream checkouts pinned to 26.1 at `9802cc5` and `6c3158e`. Evidence comes
from repository history, configuration, generated SQL, release records,
offline validation and synthetic fixtures.

Status updated 2026-11-19 on `codex/review-fixes-2026-11-19`. A row marked **Landed** or **Partly landed** names the file, function and test that prove it. Every other row is still open.

### Correctness and reliability

| Priority | Finding and evidence | Suggested change |
| --- | --- | --- |
| P1 | **Landed 2026-11-19.** **Concurrent migration replay in one checkout.** A simulated apply completed while another invocation waited for staging confirmation. The second invocation then reached the apply function and failed only when installing its receipt. *Evidence:* `scripts/migrate.py` `apply` takes `release_ownership.target_writer_lease` after the confirmation prompt and reruns the receipt and attempt recheck under that lease, before any write and at each folder boundary. `tests/test_migrate_cli.py` `test_migration_completed_while_confirmation_waits_blocks_the_confirmed_replay` covers the reported sequence. | Atomically acquire a checkout-local lock for the target, recheck receipt and attempt evidence under it at the write boundary, and hold it through verification. Add a same-checkout concurrent regression test. |
| P1 | **Landed 2026-11-19.** **Generated baseline can overwrite intervening target code changes.** A downstream replay added its own target-source guards. The current code generator selects changed units and views during capture, then emits zero preconditions. A target edit between generation and apply can be lost. [Generator](scripts/baseline_bundles.py#L998) *Evidence:* `scripts/baseline_bundles.py` `_code_before_step` writes `001-verify-code-before.sql`, which checks the captured target identity before any DDL. The preconditions add source and settings before-state checks for each unit already in the target, and `_code_object_before_check` with `present=False` for each new unit. `tests/test_baseline_generated_sql.py` `test_generated_code_checks_the_reviewed_scope_and_before_state_first` covers the order. | Generate target before-state guards for replacements and absence guards for new units. Bind them to the reviewed target and run them before DDL. |
| P2 | **Partly landed 2026-11-19:** Oracle-generated hidden columns are skipped, and virtual columns and user-visible hidden columns are refused with a named message. Still open: capturing the virtual expression and the column defaults. **Baseline column fidelity.** A staging replay exposed discarded defaults and ordinary ADD COLUMN statements for internal hidden columns. A synthetic fixture still produces both behaviours. Arbitrary default reconciliation is already out of scope. [Column generation](scripts/baseline_bundles.py#L817) *Evidence:* `scripts/baseline_bundles.py` `_column_kind` classifies each column, and `tests/test_baseline_generated_sql.py` covers both flag encodings (`Y`/`N` and `YES`/`NO`) for each class. | Exclude system-generated columns. Preserve supported, safely representable defaults on new columns or report and refuse those additions explicitly. Report unsupported physical or generated attributes in one pass. |
| P2 | **Baseline SQL needs target capability checks.** Staging revisions removed unsupported compiler-setting SQL and a privileged identity reset after apply failures. The shared generator still emits both forms. [Compiler settings](scripts/baseline_bundles.py#L398) · [Identity SQL](scripts/baseline_bundles.py#L700) | Qualify required compiler and identity operations on the configured deployment login and database service. Surface incompatibilities and any reviewed per-target setting policy before apply. |
| P2 | **Landed 2026-11-19 for the refusal and the target listing only.** **Generated migrations and migration profiles disagree.** The generator reads the code profile, while apply and verify use the independently configured migration profile. A synthetic split-owner example generated an unusable folder. The emitted SQL and checks also bind the DEV owner literally, even where the target resolver accepts a different owner. [Generator](scripts/new_migration.py#L88) · [Apply selection](scripts/migrate.py#L1396) *Evidence:* `scripts/new_migration.py` refuses a code owner that `migrate` would reject, before DEV is read, and names the migration profile. `scripts/team.sh targets` (`scripts/show_targets.py`) lists each code and migration owner and marks a split owner `REFUSED`. `tests/test_new_migration.py` and `tests/test_show_targets.py` cover both. Not done: the owner-relative DDL and target-owner-bound checks in the suggested change, so this row stays open for them. | Bind capture, preflight, apply and verification to one logical target. Generate owner-relative DDL and target-owner-bound checks for the supported owner-remapping cases. |
| P2 | **Cloud catalog duplicate blocks preflight.** One downstream carries a local patch for an Oracle-maintained PUBLIC synonym reported through two catalog rows. Both current template parsers reject that shape in the synthetic reproduction. [Parser](scripts/schema_catalog.py#L161) | Qualify selection against actual Oracle name resolution and the captured container. Preserve refusal of genuinely ambiguous or conflicting rows. |
| P2 | **Landed 2026-11-19 for the replacement only.** **Backup replaces unrefreshed scopes.** A simulated tables-only refresh in both wrappers removed a committed code mirror and manifest. A downstream also moved a reviewed ORDS replay input out of the generated mirror because a refresh could replace it. [Replacement](scripts/backup_db.sh#L283) *Evidence:* `scripts/backup_db.sh` `carry_over_unrefreshed_scopes` (commit `2add258`) and the same rule in `scripts/backup_db.ps1`, which arrived with the shared-core sync (`8128be7`). `tests/test_backup_db_cli.py` runs the tables-only, code-only and both-scope cases in both wrappers. Not verified: moving reviewed ORDS replay inputs out of the generated mirror, so that part stays open. | Replace only the verified scopes. Point release manifests at immutable reviewed ORDS inputs outside generated mirrors. |
| P2 | **No pre-write compile check for shared package edits.** Two downstream package replacements left the live body invalid until restored. The direct cause was application PL/SQL errors. The existing runner detected them after creating the body. A temporary test compile preceded the successful third attempt. | Document a short qualification route using an isolated target that matches production version, dependencies, settings and privileges. Preserve the existing apply compile guard and recovery evidence. |
| P3 | **Daily diffs omit useful evidence.** A generated compiler preamble prevents the package matcher from finding the existing mirror. In a partially successful DEV batch, the first folder gets a receipt but no diff when a later folder fails. [Matcher](scripts/migration_diff.py#L34) · [Diff trigger](scripts/migrate.py#L1410) | Match past the generated settings and write the diff at each verified folder boundary. Test both cases with generated SQL. |
| P3 | **Pre-push tries to run deleted test files.** Downstream cleanup removed the inherited suite and was followed by a local fix to check file existence. Both current templates still select absent paths. Unittest discovery returns 1 when the test directory has been removed. [Selector](scripts/pre_push_check.py#L75) | Filter for existing test files. Test deletion with and without surviving project tests. |

The baseline, catalog, migration and pre-push files listed in the core stamp
are fixed in the shared core and then synced into both templates. See the
[maintenance route](docs/ci-checks.md#shared-core-files).

### Open after the 2026-11-19 work

- Virtual-column expression capture needs an edit in each template's `scripts/compare_env_catalog.sql`. That file is template-owned and is not in the shared core, so each template changes it separately.
- Oracle's catalog representation of `INVISIBLE` columns has not been verified against a live database.
- The PowerShell backup change was verified on PowerShell 7.7 on Linux only, not on Windows PowerShell 5.1.

### Configuration and adoption evidence

| Checkout | Observed configuration and retained project artifacts | Design implication |
| --- | --- | --- |
| Aligned multi-owner | Eight identical DEV target tuples repeated in the tables, code and APEX lists, eight staging entries, two owner-only ORDS scopes and one production entry covering one of four migration owners. The checkout has 1,489 tracked mirror SQL files, 13 migration folders and six DEV receipts. Of 23 app source trees, 20 lack DEV descriptors and one of the three supplied descriptors uses an unsupported 26.1 property. | A shared target or list of named target records would remove repeated positional configuration. Keep purpose-specific and promotion coverage visible. Help users validate or scaffold descriptors only for apps they intend to deploy, with the correct release contract. |
| Split-owner | The primary dotenv file assigns tables, code and APEX to three different owners and scopes three owners for DEV migration. A second dotenv file broadens the code profile and carries staging and production bindings, including acting-as-schema logins. Tracked evidence includes 51 active migration folders, 45 DEV receipts, five DEV resolutions, 281 archived migration folders, 107 feature SQL files and one version-2 release manifest. Two staging app descriptors match targets only with the second file selected. A production descriptor uses the filename `production.json`; the runner looks for `prod.json`. A separate incomplete app directory also fails offline validation. | Keep explicit purpose overrides and intentional operating profiles. Show the effective target and profile source before any operation. Report configured, locally validated and historically verified coverage independently. Preserve dated migrations and feature sources already in use. |

All three dotenv files passed their checkout's configuration validation. Each
`verify-local` invocation returned 2 because of application source or descriptor
checks. No live target availability was established in this review.

### Simplification order

1. Make features the default for stored-code edits. Establish a guarded DEV
   apply path for the same feature files. Promote one final definition per
   object in the capsule, with dated migrations for required one-time and
   intermediate effects. The split-owner checkout recorded a 20-line package
   edit producing a 131 KB step and a 289 KB check file. Follow the
   [feature-first design](docs/superpowers/specs/2026-11-18-feature-first-releases-design.md)
   and its [implementation plan](docs/superpowers/plans/2026-11-18-feature-first-releases.md).
2. Fix direct migration replay and baseline write safety as independent
   work. Keep explicit identity checks, reviewed preconditions, independent
   verification and durable receipts.
3. Show one offline target matrix for each operating profile. Use the aligned
   eight-owner and split-owner configurations as anonymous regression fixtures.
   Cover aliases, acting-as-schema, owner-only ORDS, partial promotion coverage
   and explicit dotenv selection from both wrappers.
4. Consolidate repeated DEV preflights and measure with realistic catalogs.
   The original review counted 13 SQLcl sessions for the documented add-column
   path under single-batch, no-retry assumptions. Historical field notes
   recorded 1.5–2 minutes for small folders and a five-minute default timeout
   for some baseline captures. Keep separate timing for connection startup,
   catalog capture, write and verification.
5. Keep baseline generation as a provisioning and drift-reconciliation tool.
   Use representative staged fixtures for defaults, hidden columns, settings,
   dependency ordering, grants, and reference rows keyed by business meaning.
   Avoid replaying broad baseline output during an ordinary release window.
6. Simplify archive adoption and recovery. The converted checkout had to
   update consumers that assumed active folder paths and reconcile attempts
   against their original targets. Standardize lookup across active and
   indexed archived folders. Treat old project archives as provenance inputs
   during planning. Retain same-target evidence and block unsafe replay.

### Historical fixes and remaining qualification

- Current template code includes fixes for grant visibility under acting-as-schema,
  trigger validity, constraints paging and scoped comparison, supplementary
  Unicode in check transport, generated new-table statement boundaries, compile
  block ordering, and view import ordering. Keep these cases in the field
  regression set. The older pinned checkout needs release-specific validation
  before adoption.
- `feature doctor --live`, single ownership, controlled source refresh and
  accepted differences address the observed stale and duplicated feature files.
  Feature delivery from DEV through staging still needs representative proof.
  `JSON_TABLE` dependency refusal and shape-only view, index and CHECK proofs are
  documented limits. Qualify an explicit migration route for unsupported DDL.
- The historical typed recovery stops are addressed by recorded `--yes`
  support. Old byte-comparison and automation-restoration incidents inform the
  current 26.2 normalization and lifecycle checks. Preserve those checks when
  simplifying the workflow.
- One downstream recorded a Graphify parallel test race. Current reproduction
  is pending. Its older quoted-identifier and MIME parser patch has equivalent
  handling in the current extractor.

## Parked 2026-11-16

- [x] Dry-run `.env` wording. The shared wrapper now loads the selected dotenv file when present. README, migration rules and feature releases document dotenv resolution. Verified by source review at `735996c` on 2026-11-18.

## Verification not done (parked 2026-11-12)

- `team.ps1` on native Windows: the `-y` and `--confirm-target` flags, `reconcile-migration` and the `archive-migrations` options (`--preserve-unresolved`, `--skip-refused`, `--all`) are covered by the CI PowerShell parse step and offline tests only. Run them on a Windows 11 machine against the disposable Docker schemas before relying on them there.
- Offline-only coverage: `_refuse_body_outside_release` (`scripts/feature_release.py`) and the superseded-cleanup rule (`_superseded_reconciliations` in `scripts/release_cleanup.py`) have unit tests but no scenario in `tests/test_live_feature_release.py`.
- `baseline export-grants` is not covered by `tests/test_live_baseline.py`: its system-privilege part needs a DBA login, which the disposable connections do not have.
