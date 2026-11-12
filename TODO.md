# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

## Verification not done (parked 2026-11-12)

- `team.ps1` on native Windows: the `-y` and `--confirm-target` flags, `reconcile-migration` and the `archive-migrations` options (`--preserve-unresolved`, `--skip-refused`, `--all`) are covered by the CI PowerShell parse step and offline tests only. Run them on a Windows 11 machine against the disposable Docker schemas before relying on them there.
- Offline-only coverage: `_refuse_body_outside_release` (`scripts/feature_release.py`) and the superseded-cleanup rule (`_superseded_reconciliations` in `scripts/release_cleanup.py`) have unit tests but no scenario in `tests/test_live_feature_release.py`.
- `baseline export-grants` is not covered by `tests/test_live_baseline.py`: its system-privilege part needs a DBA login, which the disposable connections do not have.

## Known defects in generated baseline SQL (found 2026-11-12, not fixed)

Found by `tests/test_live_baseline.py` when the generated folders were applied to a disposable database for the first time. Until now they had only been built, never applied. The apply check in that module is an `expectedFailure` until both are fixed.

- **Structure family, new table with constraints.** The captured `DBMS_METADATA.GET_DDL('TABLE')` text is `CREATE TABLE ...` followed by one `ALTER TABLE ... ADD CONSTRAINT ...` per primary key, foreign key and check constraint. `_wrap_guarded_ddl` puts all of it in one `EXECUTE IMMEDIATE`, so the block fails with `ORA-00922: missing or invalid option` and every later block that needs the table fails too. Repro: build a structure family for a table that staging lacks and that has a primary key, then run `001-structure-delta.sql` on staging with `SET SQLBLANKLINES ON` (the apply session's setting).
- **`005-compile-all.sql` (and the compile step inside a bundle).** The generated `DECLARE` block declares the local function `quoted` before the variables `l_previous`, `l_current`, `l_pass` and `l_name`. PL/SQL requires variables before subprograms, so it fails with `PLS-00103: Encountered the symbol "L_PREVIOUS"` and the stored units are never recompiled. Repro: run any generated code folder's `005-compile-all.sql`.
