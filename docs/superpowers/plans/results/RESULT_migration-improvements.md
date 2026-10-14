# Migration Improvements Implementation Results

Recorded 2026-10-14 from the exact remote heads listed below.

## Delivery

- **APEX 26.1:** migration inventory/reporting, guarded live preflight, baseline prerequisites and optional baseline bundling are on `main` in the public repository [ash2osh/APEX_PROJECT_TEMPLATE_26_1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_26_1), at `297e84f8ea46d4fd124927cc744b909028a27334`. The original source tip `0bb72a356aa8b6cb6242e4e20b4653801037679a` is an ancestor of that commit. Original-repository PR [#26](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/26) was closed as superseded after the repository split; it was not merged into the 26.2 template.
- **APEX 26.2:** the same report and bundling contract is implemented at `a74d9a1df7eaac6ddef7c7795ead7a3f80ef8cb9` on `codex/migration-improvements-26.2`; PR [#25](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/25) targets the team template's `main`.

## Verification

| Track | Exact tip | Linux | Windows | macOS installer |
| --- | --- | --- | --- | --- |
| 26.1 | `297e84f8ea46d4fd124927cc744b909028a27334` | 1,331 tests, 30 skipped | 1,331 tests, 56 skipped | 20 tests, 5 skipped; job passed |
| 26.2 | `a74d9a1df7eaac6ddef7c7795ead7a3f80ef8cb9` | 1,735 tests, 28 skipped | 1,735 tests, 60 skipped | 17 tests, 3 skipped; job passed |

- The 26.1 jobs are from [workflow run 38593199104](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_26_1/actions/runs/38593199104); all three platform jobs passed, including Ruff in the Linux job.
- The 26.2 jobs are from [PR workflow run 38551701449](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/actions/runs/38551701449); all three platform jobs passed. A full local Python 3.14.4 suite at the same 26.2 tip also passed: 1,740 tests, 35 skipped.
- The fixture tests demonstrate three eligible generated migration folders combining into one, and four folders combining into two when reference data is included. Default granular output remains the default.
- The 26.2 PR head was open and mergeable when this report was captured; its exact-head checks were green.

## Boundaries

- No live SQLcl/Oracle target was used. No migration was applied, and no APEX application was imported or deployed.
- CI and local suites verify the offline behavior, fakes and platform-specific installer paths. They do not establish live database qualification.
- Migration receipts and source files remain immutable after a possible write; bundling does not combine reference data with structure/grants/code by default.
