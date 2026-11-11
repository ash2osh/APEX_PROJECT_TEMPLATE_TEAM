# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

### Decision: refresh of an unedited unit after DEV changed a line

- Symptom: after DEV replaces a line of a captured unit, `feature capture --refresh` refuses the unedited file ("has 1 line(s) that DEV does not have"). The retained DEV baseline shows the file has no local edits.
- Cause: the refresh guard in `scripts/feature_sources.py` compares the file with DEV only and does not use the retained baseline.
- Current behaviour matches `docs/feature-releases.md`, which says a refresh stops on lines DEV does not have. The operator must pass `--discard-unapplied`.
- Options: (a) keep the documented rule; (b) compare with the retained baseline so an unedited file refreshes without the flag, with an offline test and a doc change.
- Evidence: `test_standalone_procedure_and_function_capture_refresh_move_and_doctor_drift` in `tests/test_live_feature_release.py`.

### Open: type body left INVALID when its specification is not in the release

- A release that re-creates a `TYPE` specification invalidates its `TYPE BODY`. The body is re-created only when it is in the same feature (fixed in this change, pinned by `test_a_type_body_is_recreated_when_its_specification_is_recreated`).
- If the body is not in the release, it stays INVALID, and no check of that release reports it.
- Option: refuse at prepare when a selected specification is replaced and an existing body on the target is not in the checkpoint. Not implemented.
