# Import pause notice

Whole-application imports overwrite the application the team is editing. The
announcement is drafted from a fresh observed capture, target identity, roster,
commit, and named changed paths. It never invents a duration and never bypasses
the baseline/refusal guard. A person posts it; the tool has no channel access.

An all-clear is drafted only from a verified import result. Until it exists,
the team remains paused.

## Page lock ownership and manual review

Before preparing or executing a publish, the tool inspects locked pages via the
`APEX_APPLICATION_LOCKED_PAGES` dictionary view. A page locked by the importer
is reported with its owner, never filtered out. If the view is inaccessible or
returns malformed data, status is marked `UNKNOWN` and publish is refused.

When automated queries are unavailable, an operator may provide a recent manual
JSON report via `--manual-lock-report <alias>:<path>`. The operator is responsible
for checking the APEX Builder UI (under **Application > Utilities > Page Locks**)
and recording the report within 5 minutes of capture. The report is explicitly
labeled `MANUAL BUILDER REVIEW` and never masquerades as an automated query.
