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
- `baseline` against a database: `export-source`, `export-data`, `build --data` and `build --bundle` were run by hand against disposable Docker schemas on 2026-11-12, before and after the module splits, with identical results. That harness is not in the repository, so the run is not repeatable by anyone else. `export-grants` was not run: its system-privilege part needs a DBA login, which the disposable connections do not have. An opt-in live module for `baseline` would make this repeatable.
