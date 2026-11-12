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
