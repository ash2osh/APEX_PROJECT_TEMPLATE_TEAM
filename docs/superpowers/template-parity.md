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

Update 2026-10-20, after the ports (26.1 PRs 23 and 24, TEAM PRs 53, 54 and 55): 114 of 392 shared files differ, none unreviewed:
5 `missed`, 32 `feature`, 77 `intended`. What is left in `missed` are two open owner decisions, not ports:

- **Lock policy for a malformed attempt-event chain** (`scripts/migration_revision.py`, `tests/test_migration_revision.py`,
  `tests/test_migrate_cli.py`): 26.1 fails closed for any target, 26.2 scopes to the selected one.
- **Transaction control in rollout scripts** (`scripts/validate_migration.py`, `tests/test_validate_migration.py`): 26.1 accepts only
  `COMMIT` / `ROLLBACK [WORK]`, 26.2 also accepts `ROLLBACK TO SAVEPOINT`.

`scripts/backup_db.sh` is `intended`: the Windows ORDS flake (TEAM issue 49) is not caused by its `return`/`exit` form.
