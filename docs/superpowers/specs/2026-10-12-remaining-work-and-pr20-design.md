# Remaining work and PR #20: planning decisions

Date: 2026-10-12. Status: draft design addendum for implementation planning.

## Outcome and scope

Complete the remaining deployment-tooling backlog, qualify its currently unknown
runtime/platform boundaries, and reconcile the one open upstream branch without
losing the safeguards already merged into `main`. The user requested a detailed
implementation plan including that branch. This addendum records the choices
used to write that plan; writing it does not implement features or merge PRs.
Execution remains inline only, as requested earlier in the conversation.

Read this with the [existing follow-up design](2026-10-11-deployment-followup-ideas-design.md).
The [consolidated plan](../plans/2026-10-12-remaining-work-and-pr20.md) is the
execution roadmap for both documents. The older seven-task plan remains historical.

## Verified planning baseline

- `main` and `origin/main`: `df22dbbfc5a53bac6dc28f2b4f4910fec6604c6e`.
- Local `codex/deployment-review-and-completion` has no unique commits.
- [PR #20](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/20):
  `feature/post-import-automations-verify`, head
  `9a59a855ae098243d5b9d06966f117c3777c1814`; one commit ahead and one behind
  `main`. GitHub reports conflicts. A `git merge-tree` diagnostic found content
  conflicts in `docs/TROUBLESHOOTING.md` and `docs/known-limitations.md`.
- PR #20's offline CI is successful on its existing head. Its own description
  says the native automation API path and native Windows behavior are unverified.
- PRs #14–18 are merged. GitHub currently exposes `main` and the PR #20 branch.
- [Issue #19](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/19)
  remains open although the code fix is included in `df22dbb`. Its mixed-index
  regression has not been separately qualified on native Oracle.
- `.env`, `apps/DEMO/`, `database/DEMO/`, and `uv.lock` are local untracked work;
  preserve them and exclude them from template commits.

All identities and GitHub states above are observations, not future guarantees.
Refresh them before execution and integration.

## Branch reconciliation decision

Three approaches were considered:

| Approach | Tradeoff | Disposition |
|---|---|---|
| Merge PR #20 as authored after resolving text conflicts | Changes the default source-acceptance rule and introduces automatic writes without native proof | Excluded under the current contract |
| Reconcile PR #20 into strict, complete mismatch diagnostics; develop explicit automation commands separately | Reuses useful work while keeping each behavior independently testable and reviewable | Recommended plan |
| Replace the default contract with automatic restoration and normalized acceptance | Requires a separately approved policy change and authentic release-specific evidence | Conditional future design only |

The recommended route keeps exact comparison after the existing documented
deployment projections, reports every mismatch without printing source text,
and retains the current baseline/lock/lifecycle recovery rules. A stable second
normalized export does not prove that a dropped whitespace or ordering change
is harmless. PR #20's normalizer must not participate in acceptance by default.
Do not describe sorting columns or trimming embedded literal whitespace as
cosmetic without component-specific evidence.

Rework the existing PR branch by merging current `main` and adding reviewed
correction commits; do not cherry-pick its entire commit into `main`. Update its
title/body to the final strict-diagnostics scope. Move explicit automation work
to a separate reviewable slice. Preserve its original commit as ancestry, and
use normal pushes; no history rewrite or branch deletion is implied.

## Shared interface decisions

1. **Durable attempts:** immutable `attempt-event.<UUID>.json` files live beside
   migration SQL. UUIDs identify events; `attemptId` identifies one attempt;
   `runId` links a batch. Each attempt is one pending-to-terminal chain.
   Verified completion still freezes the source. Only validated no-write
   evidence can remove a pending lock; missing referenced evidence after scratch
   deletion remains unknown/locked. No timestamp-order guessing or second lock
   classifier. Folder validation, input hashing, frozen copies, and `revise`
   must all know these files are operational evidence, not migration SQL.
2. **Version provenance:** observe APEX release during application export and
   bind it to a sorted canonical file-set SHA-256 in an optional nested
   `sourceProvenance` object in `apex-team-export.json`, outside deployment descriptors.
   Retain the existing schema-2 sourceFormat/sourceFiles/page-lock baseline unchanged;
   its major-release format label is not export-time patch provenance.
   Unknown, stale, manual, or edited source remains exact-byte-only. Database
   stored-source export uses independently observed database-version provenance;
   an APEX release must not be invented from a client or database version.
3. **Automation commands:** `list` and `check` are read-only. `enable`, `disable`,
   and `run-once` are separate, exactly scoped actions with displayed target and
   selection confirmation. Publish/deploy never invoke these commands implicitly.
   A successful explicit repair does not automatically advance a failed publish's
   baseline or release a retained lock.
4. **Rehearsal metrics:** `checks.json` schema 1 retains its exact original
   contract. Schema 2 adds an optional `rehearsalMetrics` array with `id`, `sql`,
   `expectedBefore`, `expectedAfter`, and `expectedRollback`. At most 50 metrics
   per folder; expected values are integers in the inclusive range
   `-9007199254740991..9007199254740991`, excluding booleans. Metrics use the
   existing conservative scalar-query validator. Capture before each folder's
   payload and immediately after it in the same transaction; capture rollback
   expectations after the final rollback. A metric SQL must intentionally scope
   its tagged rows when later folders affect shared data. No per-row values,
   inferred column changes, per-statement claim, or new DDL rehearsal permission.
5. **Template drift:** separate local lock/hash drift from an explicitly
   requested upstream comparison. Routine offline checks never fetch. Missing
   locks in downstream projects are unknown; the original template is a distinct
   not-applicable case. Customization alone does not mean upstream obsolescence.

## Evidence and acceptance

Keep source review, offline behavior, native Oracle/API behavior, native Windows,
and historical evidence in separate result fields. A qualification result binds
the exact Git commit, client/server/APEX/ORDS releases, configured saved alias,
observed identity, selected numeric app/workspace/schema, owned fixture inventory,
and cleanup result. Missing targets remain blocked/unknown rather than passed.
Only sanitized summaries are tracked; full output remains in ignored evidence.

Prior Docker authorization allows scoped disposable qualification after fresh
identity checks. Use `docker-demo2`/`DEMO2` only if still verified as local and
disposable. Keep existing DEMO apps/data outside fixture ownership. Creating a
disposable app requires identifying an actually unused numeric ID and an existing
workspace/developer; no value is invented in the plan. Shared existing apps and
staging/production operations require their own exact authorization/coordination.

## Conditional work

- APEX 26.1 backport: separate release branch and release-matching skill/client
  contracts; never lower `main`'s APEX 26.2 / SQLcl 26.3.0.0 floor.
- Additional database/ORDS pairs: qualify only identified combinations. No
  universal compatibility claim from one Docker installation.
- Tolerant profiles: only after authentic version-pair evidence, source
  provenance, component-aware negative tests, and explicit acceptance-policy
  approval; unknown pairs retain strict behavior.
- Custom-function allowance: security research and a design verdict first.
  A user-editable name list is not a purity proof. Refusal is an acceptable final
  verdict if autonomy, dependency, edition, or external-effect guarantees fail.

## Completion definition

Each deliverable has its named tests, scoped evidence, documentation, and an
integration decision. PR #20 is either merged in its corrected scope or explicitly
closed as superseded with replacement links after authorized reporting. Historical
merged branches have no implementation work remaining. Unknown qualifications and
conditional policy decisions stay visible; their existence does not force a
weaker safety contract.
