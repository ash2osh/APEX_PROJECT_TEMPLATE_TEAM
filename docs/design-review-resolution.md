# Design review resolution — 2026-09-07

The seven findings from the project review are addressed in the specifications
and implementation plans. The tooling is not implemented; database, concurrency,
CI and native-platform acceptance remain unchecked future work.

| Finding | Revised contract | Required implementation evidence |
|---|---|---|
| 1. Repeated exports reuse an obsolete import baseline | Spec §6 separates paired export checkpoints from verified import baselines; Plan 1 Tasks 4–5 define reconciliation and commit anchoring. | Successive edits and intentional reverts work without import; Git-only divergence remains protected; interrupted patches and branch rewinds cannot reuse unsafe state. |
| 2. Receipt containment loses deletion intent | Spec §6 and Plan 1 Task 3 require cumulative absence evidence as well as matching present files. | An older commit restoring a deleted page refuses; unrelated new paths remain allowed; tombstones survive no-op export. |
| 3. Export ignores uncertain targets | Plan 1 Tasks 5, 8–9 require uncertainty checks at both observations, narrow owner verification, and separately guarded recovery capture. | Caught partial import with unchanged generation and cleared ownership cannot yield a usable export; wrong tokens and competing recoveries refuse. |
| 4. Local bindings can split physical locks | Spec §5 and Plan 1 Tasks 1 and 5 define a physical app key separately from local state identity and bind it to a tracked controller contract. | Different connection/service/binding names for one physical app share a mutex; different physical apps do not. |
| 5. Drift attribution and blocking lack a contract | Spec §7 and Plan 2 Tasks 2–6 retain immutable observed inventory chains and require continuity before payload execution. | Foreign observed changes remain allowed; manual drift before unrelated work blocks before RUNNING; missing attribution evidence is reported as unknown. |
| 6. Shared integration cannot prove absence of unmerged columns | Plan 2 acceptance and Plan 3 Tasks 4–8 run declared candidate-app checks on disposable replay before integration, and retain check coverage in release evidence. | A dependent page/query fails on disposable replay even when shared integration already has the column; merging its migration makes the check pass. |
| 7. Global database configuration prevents offline commands | Spec §8 and Plan 1 Task 1 dispatch offline commands before loading database profiles. | Authoring and artifact inspection work without .env; online commands still reject incomplete required profiles. |

The [plain-language explainer](working-on-apex-together.html) now describes
checkpoints, deletion protection, uncertainty, observed drift continuity,
disposable application checks and offline authoring consistently with the plans.

Validation performed for this documentation revision:

- Parsed all 10 Python code fences in the four design/plan documents.
- Executed the exact documented reconciliation/receipt functions against all
  eight embedded regression tests, including successive edits, reverts and
  deleted-page resurrection.
- Checked all 256 combinations of missing, empty, A and B across the four-tree
  reconciliation state table.
- Checked Markdown links, code-fence balance, LF line endings, HTML IDs/anchors
  and Git whitespace errors.

These checks validate the documented pure contracts and document structure.
They do not qualify SQLcl, database locking, CI provisioning or the future
application-check runner. No database operation was performed.
