# Template parity: 26.2 and 26.1

This repository (APEX 26.2) and `APEX_PROJECT_TEMPLATE_26_1` share most files but are
separate repositories, so a fix made in one is easy to miss in the other.
`tools/template_parity.py` lists the shared files whose committed content differs and checks
each against [template-parity.json](template-parity.json):

| Status | Meaning |
| --- | --- |
| `unreviewed` | differs and nobody has looked at it |
| `changed` | reviewed, but either side changed since (recheck it) |
| `missed` | a fix one line has and the other still needs (to port) |
| `feature` | a feature only one line has; porting it is a separate decision |
| `intended` | the release lines differ on purpose |
| `stale` | an entry for files that now match; remove it |

```bash
python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1
python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1 --mark scripts/x.py --status missed --reason "26.1 has the Windows path fix"
```

Run it after a fix lands in either repository. It exits 1 while anything is `unreviewed` or
`changed`. Both checkouts are read at their committed `HEAD`. The tool and this list are
template-only: projects never receive them.

Review 2026-10-20 (TEAM `c15be4f`, 26_1 `f19ba08`): 390 shared files, 120 differing, all classified:
33 `missed`, 27 `feature`, 60 `intended`, none unreviewed. The `missed` entries are fixes to port (each reason
says to which repository). Tests, documents and skills were classified together with the module they
accompany. Scripts were compared by their function sets and by reading the hunks that exist in only one
line; very large files (`rollout.py`, `publish_app.*`, `team.*`) were not read hunk by hunk, so a `feature` or
`intended` mark on them rests on the function lists and commit history. The largest groups of `missed` entries:

- **26.2 needs:** `relative_to_root` (Windows alias safe) in the migration modules, the strict
  COMMIT/ROLLBACK-only rule in `validate_migration.py`, NOT NULL data preconditions in baseline bundles,
  the pinned-handle `directory_install.py`, and 26.1's tests for attempts, lock scoping and compare-env.
  The lock policy for malformed attempt chains differs between the lines and needs an owner decision.
  `backup_db.sh` is the suspect for a Windows-only flake that only 26.2 shows (see `tests/known_flaky.txt`).
- **26.1 needs:** strict UTF-8 reads in `ords_export.py`, the SQLcl transport preflight in `migrate.py`,
  `attempt-event` files in `.gitignore`, and `team-flow.md` frontmatter.

After 26.1 gained `verify-local` (2026-10-20, 26.1 PR 21) the report covers 392 shared files, 123 differing: 33 `missed`, 27 `feature`, 63 `intended`, none unreviewed.

## Decisions of 2026-10-20 (final state: 114 of 392 shared files differ, none unreviewed, 0 `missed`, 32 `feature`, 82 `intended`)

- **Ports done:** 26.1 PRs 23 and 24, TEAM PRs 53, 54 and 55 (strict UTF-8 ORDS reads, SQLcl transport preflight, plan identity
  refinement, `attempt-event` in `.gitignore`, the Windows-safe path helper, baseline NOT NULL preconditions, compare-env same-scope
  blocker and column order, the pinned-handle installer).
- **Rollout transaction control:** both lines accept only `COMMIT` and `ROLLBACK [WORK]` (TEAM PR 59, 26.1 PR 25 pins it with a
  test). 26.2 had also accepted `ROLLBACK TO SAVEPOINT`, which could never be used because a top-level `SAVEPOINT` is refused, and
  `COMMIT`/`ROLLBACK FORCE`, `COMMIT COMMENT` and `COMMIT WRITE`, which it should not.
- **Lock policy for damaged attempt-event evidence:** kept different and documented in `docs/migration-rules.md` of both lines
  ("Evidence of other targets"). 26.1 fails closed for any target; 26.2 checks the selected target strictly, ignores damaged evidence
  of other targets, and stays fail-closed when no target is given. Neither lowers the check on the target being applied.
- **`feature` entries (32) are not ported, on purpose.** They are 26.2-only: native application locks, partial publish, APEXlang
  normalization and `upgrade-apexlang`, source provenance and the schema-2 export marker, deployment verification, the APEX release and
  workspace-user checks in `doctor`, and two generic ones (the `MIGRATION_*` owner profile and rehearsal metrics). 26.1 is the
  maintenance line: the APEX-specific ones cannot work on 26.1, and the two generic ones reach the whole configuration and migration
  stack, so they are not worth the risk for a line whose projects can move to 26.2. Revisit only if a 26.1 project asks for one.
- **`scripts/backup_db.sh` and the Windows ORDS flake (TEAM issue 49):** the `return`/`exit` form is not the cause (the failure
  predates it, and over 11,000 Windows attempts in three experiments reproduced nothing, including 3 x 3,600 isolated exit-status
  loops). The two tests stay in `tests/known_flaky.txt`; the issue stays open.

## Full-review integration — 2026-10-20

After the review corrections were committed in both release lines,
`template_parity.py --mark` refreshed the fourteen changed shared-file records.
Verification against the committed source: 395 shared files, 114 differing,
32 `feature`, 82 `intended`, and no unreviewed, changed, missed or stale entries.
The shared CI, publication identity/transcript, daily-diff and DEV planning fixes
exist in both lines. The 26.1 lifecycle baseline/recovery correction restores its
existing contract; it does not port the 26.2-only publish features. Exact-detector
Graphify coverage receipts and the missing-template-file readiness correction
remain in the 26.2 tooling. See [the consolidated review](reviews/2026-10-20-two-template-review.md)
for reproductions, verification and remaining runtime limits.
The additional pre-push hook environment isolation fix and its regression are
identical in both lines; the number of intentional differences is unchanged.

2026-11-12: after the migration capsule checkpoints and the review follow-ups were ported to both lines, the report
listed 7 unreviewed, 52 changed and 2 stale records. The 7 were reviewed and marked `intended`, the 52 kept their status
and reason with refreshed blob ids, and the 2 stale records were removed. The report exits 0 against 26.1 `main` at
`de0487e`.

2026-11-12: both lines moved the feature-capsule rollout path into `scripts/rollout_capsule.py`. The new shared file is
marked `intended` (26.2 keeps its capsule bindings and receipt check), and three records were refreshed.

2026-11-12: migration attempt resolution (`reconcile-migration`) is in both lines. Twenty-four records were refreshed
with their status and reason unchanged, and the stale `TODO.md` record was removed. 26.2 keeps its target-scoped
attempt validation and its Graphify 0.9.75 pin.

2026-11-12: 26.2 moved its Graphify pin from 0.9.75 to 0.9.106 and took the tolerant SQL-import anchor from 26.1.

2026-11-12: both lines split `release_state.py` and `baseline.py` (cleanup, recovery, data export, ORDS filter and the
build group). `baseline_build.py` and `TODO.md` are marked `intended`; two records were refreshed.

2026-11-12: both lines cut `baseline_build.py` into `baseline_sql.py`, `baseline_reference_data.py` and
`baseline_bundles.py`. The first two are byte-identical in both lines. `baseline_bundles.py` is marked `intended` (26.2
lists a few helpers in a different order), two records were refreshed, and the `baseline_build.py` and `TODO.md` records
were removed as stale because those files now match. 26.2 also gained a pinned-set `graphify` CI job, which 26.1 does not
have (its Graphify tests run in the latest-Python job), so the two CI records carry that reason.

2026-11-12: both lines gained `tests/test_live_baseline.py`, an opt-in live check of `baseline` (the file is identical in
both). `docs/baseline.md` and `docs/live-feature-tests.md` carry the same added section and pointer, so their two records
were refreshed with status and reason unchanged, and `TODO.md` stays identical.

2026-11-12: both lines fixed the SQL that `baseline build` generates (a new table's statements, the compile step's
declaration order, and the baseline module import order), found by `tests/test_live_baseline.py`. `baseline_sql.py`,
`baseline_build.py`, the new `tests/test_baseline_generated_sql.py` and `TODO.md` are identical in both lines; the four
changed shared records were refreshed with status and reason unchanged.
