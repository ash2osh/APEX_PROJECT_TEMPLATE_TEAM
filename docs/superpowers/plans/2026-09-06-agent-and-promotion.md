# Agents, Integration and Promotion — Implementation Plan (Plan 3 of 3)

> **For agentic workers:** Use superpowers:executing-plans, or
> superpowers:subagent-driven-development when delegation is authorized.
> Do not mark a gate complete before its behavioral acceptance tests run.

**Revision:** 3 — verifies candidate-app dependencies on disposable replay
before shared integration.
**Goal:** Give agents correct team rules and prove selected source can be built,
deployed to integration, promoted to test and handed to production safely.
**Architecture:** CI first proves canonical replay, then deploys an exact commit
to integration. Release builds export a pinned Git tree and complete migration
history; target-specific pending plans are external to the immutable artifact.
Production gets verified bytes plus a human runbook, with no automated apply.
**Tech Stack:** Python 3.10+ shared core, Bash/PowerShell, qualified SQLcl/APEX,
GitHub Actions, isolated disposable database runner.
**Spec:** [Team design](../specs/2026-09-06-team-template-design.md), §§4, 7–11.
**Dependencies:** [Plan 1](2026-09-06-apex-round-trip.md) and
[Plan 2](2026-09-06-migration-layer.md).

## Global constraints

Spec §8 applies to every task. Production reads are SELECT-only; all production
writes, including metadata, adoption, recovery and bootstrap, are refused.
No CI job receives a production saved connection, wallet or credential.
Runtime target roles and verified identities enforce deployment boundaries.

Use one Python core and thin shell launchers. Artifact construction, validation,
pending-plan calculation and runbook generation are offline operations.
Deployments use protected non-production contexts only. Test SQLcl commands
through the same adapter and error protocol as developer commands.

## File responsibilities

| Files to create | Responsibility |
|---|---|
| AGENTS.md; self_improve.md; .agents/rules/; .agents/workflows/team-flow.md; docs/app-recovery.md | corrected team agent contract |
| app_context/README.md; .graphifyignore; setup_graphify_apx.py; scripts/graphify_*.py | optional alias-keyed knowledge layer |
| scripts/teamlib/conflict_assistant.py; docs/conflict-resolution.md | plain-language, property-level conflict explanation and developer Q&A; never auto-resolves |
| scripts/teamlib/announce.py; docs/import-pause.md | drafted import announcement and all-clear from observed state; import confirmation that never bypasses a guard |
| scripts/teamlib/deploy.py; scripts/deploy_app.sh/.ps1 | named-target exact-source deployment |
| scripts/teamlib/release.py; scripts/teamlib/runbook.py; scripts/build_release.sh/.ps1 | offline artifact, plan, runbook and release launcher |
| scripts/teamlib/ci.py; ci/runner-contract.json; ci/provisioners/docker_pdb.sh; docs/ci.md | qualification/provisioning, reference provisioner and job entry points |
| .github/workflows/database-checks.yml | required disposable replay gate |
| .github/workflows/integration.yml; release.yml | merge deployment and tag-to-test promotion |
| scripts/tests/test_deploy.py; test_release.py; test_production_boundary.py | public-entry-point regression tests |
| README.md; docs/promotion.md; docs/design-review-resolution.md | user workflow, environment setup, recovery and review-finding traceability |

Public commands added to team.py:

```text
explain-conflict RECOVERY_ID
announce-import ALIAS (--ref COMMIT | --all-clear OPERATION_ID)
deploy-app ALIAS --target TARGET_JSON --ref COMMIT
build-release --ref TAG_OR_COMMIT --version SEMVER --out DIRECTORY
verify-release ARCHIVE
plan-release ARCHIVE --history HISTORY_JSON --target TARGET_JSON --out FILE
apply-release ARCHIVE --target TARGET_JSON --plan PLAN_JSON
gen-runbook ARCHIVE --history HISTORY_JSON --target TARGET_JSON --test-evidence FILE --signature FILE --trust-key FILE --out FILE
ci-doctor --contract FILE
ci-replay --ref COMMIT --previous PREVIOUS_ARTIFACT
```

plan-release and gen-runbook use history supplied by the environment owner;
they do not fetch it or connect. gen-runbook also requires signed test evidence
and a trust key supplied outside the artifact. apply-release accepts only test/integration
roles with non-production classification. It verifies the artifact, reads
actual target history again and refuses stale/mismatched plans before apply.
explain-conflict reads only a retained Plan 1 recovery bundle and writes only
to scratch/; it never touches tracked source and is not part of resolve-export's
own verification, which is unchanged by its existence (see Task 3).
announce-import is read-only and produces text for a person to post; it never
imports, never sends, and its confirmation step is additional to Plan 1's
refusals rather than a way through them.

## Task 1: Agent contract and recovery instructions

**Files:** AGENTS.md, self_improve.md, .agents/rules/agent-safety.md,
.agents/workflows/team-flow.md, docs/app-recovery.md, scripts/tests/test_agent_docs.py.

- [ ] Adapt the solo instruction files, preserving evidence-backed portability
  lessons. Replace conflicting statements about numeric paths, database mirrors
  as truth, AI-generated SQL placement and temporary recovery state.
- [ ] Explicitly route APEX changes to apps/<alias>, SQL intent to migration
  bundles, database evidence to canonical replay, and recovery captures to
  .sync-state. Check app_context before complex app work.
- [ ] State the shared-application rules (spec §2, §2.1) as agent contract, since
  an agent inheriting solo-template habits will get every one of them wrong:
  the daily loop is build/export/commit with **no import step**; an export
  carries the whole team's current application state, not the agent's own
  changes, so it must never describe an export diff as "my changes"; `import-app`
  overwrites what everyone is editing and is never run to "refresh" or "reset"
  a workspace; and a git branch does not isolate APEX source, so work cannot be
  parked on a branch by exporting it there.
- [ ] Require an agent that applies a migration bundle to the shared schema to
  say, in the same turn, that the bundle must be merged promptly because any
  colleague's export can now carry a page depending on it (Plan 2). Do not let
  an agent treat applied-and-unmerged as a finished state.
- [ ] Require drift inspection before database changes; describe its structural
  coverage and inability to observe uncaptured/transient writes or arbitrary DML.
- [ ] Document capture/resolve/commit/import and uncertain-attempt recovery.
  No instruction may say a rejected export can be fixed simply by importing
  over the Builder workspace.
- [ ] Keep exact connection/profile gates, production SELECT-only rule and
  no automatic commit/push. Reconcile inherited initialization/uc-apx workflows
  with new alias and config contracts before including them; omit incompatible
  copies rather than leaving broken routing links.
- [ ] Test links and key path/command contracts. No case-colliding agent files.

## Task 2: Optional Graphify and alias-keyed context

**Files:** .graphifyignore, setup_graphify_apx.py, scripts/graphify_*.py,
scripts/tests/test_graphify_corpus.py, app_context/README.md.

- [ ] Inspect and vendor the tested extractor/setup helpers with their real
  dependencies and licenses. Do not install or run extraction automatically.
- [ ] Allowlist apps/, database/ and app_context/. Exclude deployments,
  .apex tooling metadata, static payloads, logs and all sync/recovery state.
  Migrations remain execution history; canonical database evidence supplies
  current object definitions.
- [ ] Key context by alias and include purpose, data dependencies, subscription
  master identities, known issues and recovery notes.
- [ ] Test alias paths, deployment exclusion, binary exclusion, optional absence
  and cache invalidation for changed APEXlang extractor. Verify local links.
- [ ] Preserve optional uc-apx opt-in semantics; adding the team template does
  not imply installation.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_graphify_*.py' -v`.

## Task 3: Developer-facing assistants

**Files:** scripts/teamlib/conflict_assistant.py, scripts/teamlib/announce.py,
scripts/team.py, scripts/tests/test_conflict_assistant.py,
scripts/tests/test_announce.py, docs/conflict-resolution.md,
docs/import-pause.md.

Two assistants, one posture. Both exist because this template's users are APEX
developers rather than git users (spec §1), and both are bound by the same rules:
explain in plain language, ask rather than decide, write nothing tracked, and
never let an answer from a person substitute for a machine guard.

### `explain-conflict` — resolving a conflict without reading a diff

**Interface:** `explain_conflict(recovery_id) -> ConflictBriefing` — offline,
reads only the retained Plan 1 recovery bundle (base/source_base/head/mine
trees, the conflict path list and capture/commit author metadata Plan 1 already
persists). ConflictBriefing enumerates, per conflicted path, the APEXlang
properties that actually differ and a bounded question set (keep head / keep
mine / keep both with developer-supplied text / raw diff fallback). It writes
nothing to tracked source.

Written for the audience spec §1 assumes throughout — APEX developers, not
git users. A raw three-way text conflict on `.apx` source is not a reasonable
thing to hand someone who has never resolved one; this task exists so the
choice they're asked to make is "which page title do you want" rather than
"resolve this diff3 marker."

- [ ] Depends on Plan 1 Task 10's recovery bundle format and Task 1's
  corrected agent contract; do not define a second bundle schema here.
- [ ] Parse each conflicted file's base/source_base/head/mine as APEXlang
  structure, not raw text — show any captured/reconciled checkpoint
  divergence and diff at the property level (page/region/item/button/
  subscription). A file the parser cannot confidently handle refuses to
  guess; it returns the raw captured/source-checkpoint/HEAD/current-capture
  comparison instead, never a guessed structural read.
- [ ] Distinguish two outcomes per path: (a) the two sides changed *different*
  properties — report this, but still require developer confirmation before
  writing anything, never auto-apply; (b) the two sides changed the *same*
  property to different values — this is the only case that produces a
  question. State both values in plain language and, where capture/commit
  metadata identifies them, who made each change.
- [ ] Frame the two sides correctly for the shared topology (spec §2.1). A
  conflict here is Git against the shared application, not one developer against
  another: HEAD is what somebody committed to this branch, and the capture is
  what the application currently holds, which may include work by several people
  and is not the operator's own. Never label the capture side "your change" or
  the HEAD side "their change" — attribute HEAD from commit metadata, and
  attribute the capture side only as "the shared application", since no
  per-property author exists for it.
- [ ] The assistant never selects a value for the developer and never
  synthesizes a merged value on its own; "keep both" requires the developer's
  own supplied text. This is the spec's existing "no automatic line merge"
  rule (§6) applied at the property level — the assistant narrows what a
  human has to read, it does not remove the decision from them.
- [ ] Candidate output is written only under scratch/, one file at a time,
  only after every question for that path is answered. Never written to
  tracked source; never invoked automatically from resolve-export or any
  other command.
- [ ] Plan 1's `resolve-export` (Task 10) is unchanged by this task: it still
  requires an explicit `--resolved` path and independently re-verifies
  conflict-path coverage against the original preimage. The assistant's
  scratch/ output is one valid way to produce that directory, not a trusted
  bypass of resolve-export's own checks.
- [ ] Test: different-property auto-identification, same-property question
  generation, unparsable-file fallback to raw checkpoint comparison, refusal to
  write outside scratch/, refusal to proceed with an unanswered path, and that
  resolve-export applied to the assistant's output is byte-identical to the
  same resolution supplied by hand.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_conflict_assistant.py -v`.

### `announce-import` — the pause protocol, written for the developer

Spec §9 makes announcing an import part of the operation rather than a courtesy,
and Plan 1 Task 9 prints the requirement. That is the floor, not the feature: a
requirement to announce something, given to a developer with no wording and no
observed state to describe, produces a vague message that its readers ignore. The
assistant writes the message from what is actually in the application, and asks
before the write.

**Interface:** `draft_import_announcement(alias, commit) -> Announcement` and
`draft_all_clear(alias, result) -> str`. Announcement carries the message text,
the observed state it was written from, and the findings that must appear in it.
Both are read-only and produce text; neither imports anything. The CLI's two
modes map to these two functions and are mutually exclusive: `--ref` drafts the
pre-import announcement, `--all-clear` drafts the post-import all-clear from a
completed import's result, and `--ref` is not read in the second mode.
`--all-clear` takes the `operation_id` Plan 1 Task 1 already prints in every
command's JSON result — not a new identifier scheme — and `draft_all_clear`
loads that operation's persisted outcome from `.sync-state` to build `result`;
it is the same ID `recover-files OPERATION_ID` already addresses recovery
state by.

- [ ] Draft from observed state, never from assumption: the resolved commit, the
  target's workspace and application identity, the `app-status` roster (spec §9)
  and a fresh read-only capture compared against the baseline.
- [ ] The message states which application and target by name and ID, which
  commit is being imported and how it differs from what is deployed, that
  everyone must stop editing in the App Builder now and not save, who is running
  it, and that an all-clear will follow. It must not invent a duration: give a
  measured expectation where prior runs supply one, otherwise say the duration is
  unknown. People plan their afternoon around that number.
- [ ] **Uncaptured work is named, not summarised.** Where the capture differs
  from the baseline, list the affected pages and components in the message, so
  the person whose work it is recognises it and exports before the pause instead
  of discovering the loss afterwards. A message saying only "some uncommitted
  changes exist" fails this requirement and is a defect.
  The list is nonetheless a snapshot: it is drawn before the pause, while people
  are still editing, so work started after the draft cannot appear in it. Say so
  in the message. Presenting it as exhaustive would invite readers to conclude
  their work is safe because it was not listed, which is the opposite of the
  message's purpose.
- [ ] Draft the all-clear as a separate output after a verified import: what was
  imported, that editing may resume, and where the recovery bundle is if
  something looks wrong. A pause with no resume signal leaves the team either
  idle or drifting back mid-import, which is the state the pause exists to
  prevent.
- [ ] The assistant drafts; the developer posts. This tooling has no channel
  access and must not acquire any. Nothing here sends, schedules or auto-posts.

#### Confirming the import

- [ ] This confirmation lives in `team.py`'s CLI dispatch for `import-app`, not
  in `announce-import` itself and not in Plan 1's `import_app` library
  function: the dispatcher calls `draft_import_announcement` internally to
  gather what to show, prompts, and only then calls `import_app`.
  `announce-import` stays a separately-run, read-only drafting command a
  developer can invoke on its own; it is not modified to prompt or block.
  "Proceed" below means the `import-app` dispatcher proceeding to call
  `import_app`, never `import_app` proceeding on its own.
- [ ] Put the consequences to the developer as a question they must answer, not a
  banner they scroll past: the application and target by identity, the paths that
  will change, any uncaptured work found, and whether the announcement has been
  posted. Proceed only on explicit confirmation.
- [ ] **Confirmation never substitutes for the §6 baseline refusal.** A developer
  answering "yes, I announced it" does not unlock an import that Plan 1 refuses
  because a colleague's uncaptured work is present. The two are independent and
  the machine check decides. Building confirmation as a route past that guard
  would reintroduce precisely the loss it prevents, so test explicitly that a
  fully confirmed import over uncaptured work still refuses.
- [ ] Where state could not be read — no connection, an unreadable roster, an
  uncertain baseline — say so and decline to draft a reassuring message. Silence
  about unknown state reads to a developer as "nothing found".
- [ ] Test: message contains target identity and named uncaptured paths; absent
  prior timings produce "unknown" rather than an invented duration; unreadable
  state produces a refusal rather than a confident draft; confirmation alone does
  not bypass the baseline guard; all-clear is produced only after a verified
  import and never after a refusal or an uncertain result.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_announce.py -v`.

## Task 4: Exact-source named deployment

**Files:** scripts/teamlib/deploy.py, scripts/deploy_app.sh/.ps1,
scripts/tests/test_deploy.py.

**Interface:** `deploy_app(target, source_tree, source_commit,
replay_proof=None) -> DeployReport`; source_tree is immutable materialized
bytes, not a live path.
`target.alias` (Plan 1's `Target`) identifies which archive tree to deploy —
there is no separate `alias` parameter, since `team.py deploy-app ALIAS
--target TARGET_JSON` already resolves both into one `Target` before calling
this, and a second parameter could only ever agree with `target.alias` or be
a bug. DeployReport includes commit, target identity, verified
tree/subscription digests and recovery location.

- [ ] Resolve target JSON and .env explicitly. For integration/test require
  named deployment binding agreement with the exact profile, workspace, schema,
  app ID and saved connection. Reject default, traversal and mismatched role.
- [ ] The internal candidate-app caller may use role replay only with an exact
  provisioner proof binding instance token, run ID and target identity from
  Task 5. Validate the proof before any capture/write and require its disposable
  fixture binding. Public deploy-app remains integration/test only and exposes
  no replay override. Test forged, missing and mismatched proof refusal.
- [ ] Stage exact source from a resolved Git commit or verified artifact.
  Preserve tracked bindings in Git; inject only the selected verified binding
  into staging. Do not copy local default.json or credentials into the source.
- [ ] Capture destination before replacement to durable deployment recovery.
  These downstream workspaces are explicitly replaceable from approved source;
  they do not use developer .sync-state baseline or capture receipt.
- [ ] Validate masters and source, guard production, verify in-session
  identity, call `mark_payload_starting` (Plan 1 Task 5) before import, then
  import and re-export via `capture_app(target, held_by=run_token)` — the
  same held-mutex carve-out import_app uses, since this path holds the exact
  same mutex — and verify owned source and subscription linkage. Record
  deployment evidence and call `release_app` with `confirmed_success=True`
  only after verification passes. A known failure with ended workers releases
  without confirmed_success; an unknown result retains ownership. Both retain
  uncertainty once payload started and require recover-app-lock review.
- [ ] Acquire Plan 1 control_store's persistent app-target mutex before capture,
  hold through import/verification and retain it on unknown results. This protects
  separate clones and local automated clients as well as CI. Controller access
  uses METADATA; payloads never receive controller tokens.
- [ ] Serialize same-target CI jobs. Track last verified commit; refuse a stale
  ancestor deployment arriving after a newer successful deployment. Require an
  explicit reviewed non-production rollback workflow for rollback, not a race
  between queued jobs. Unknown/failed deployment remains visible.
- [ ] Test binding/profile mismatch, alternate embedded connection, dirty source
  exclusion, stale commit, wrong master, partial import and verification failure.
  Prove this path never changes developer baseline state.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_deploy.py -v`.

## Task 5: Required CI database provisioning and replay gate

**Files:** scripts/teamlib/ci.py, ci/runner-contract.json,
ci/provisioners/docker_pdb.sh, docs/ci.md,
.github/workflows/database-checks.yml, scripts/tests/test_ci_contract.py.
**Interface:** `ci_doctor(contract) -> DoctorReport`;
`ci_replay(ref, previous_artifact) -> ReplayReport`.

- [ ] Specify an isolated disposable runner contract: qualified exact SQLcl,
  JDK/APEX/database versions, Python, provisioner executable and named profiles.
  CI-doctor validates capabilities and saved connections without exposing secrets.
- [ ] Provisioner interface is an executable receiving argv
  `create --run-id UUID --out scratch/ci/UUID`. It returns a versioned JSON
  file with instance token, explicit replay .env path and app/workspace fixtures.
  `destroy --run-id UUID --instance-token TOKEN` deletes only that verified
  disposable resource. A reused/shared target fails empty-target/identity checks.
- [ ] Implement the provider for the team's chosen isolated runner by qualified
  scripts or image pinned in runner-contract.json; acceptance must execute
  create/probe/replay/destroy. Missing provisioner/toolchain/connection fails
  the required job.
- [ ] **Ship one working reference provisioner**, so the template is adoptable.
  A contract plus abstract glue with no implementation leaves every adopting
  team stalled here with no CI at all, which in practice means the required
  replay gate is skipped and `database/` evidence silently stops being
  trustworthy — the exact outcome this gate exists to prevent.
  Provide `ci/provisioners/docker_pdb.sh` implementing the documented argv
  contract (`create --run-id UUID --out DIR`, `destroy --run-id UUID
  --instance-token TOKEN`) against a disposable Oracle Free container or a
  cloned template PDB, emitting the versioned JSON with instance token, replay
  `.env` path and app/workspace fixtures. Pin its image digest in
  `runner-contract.json`.
  Treat it as a **reference**, not a mandate: it must be replaceable by a
  team's own provisioner without code changes, which is the test of whether
  the argv contract is actually sufficient. Document its resource
  requirements and teardown guarantees, and run the same acceptance
  (`create/probe/replay/destroy`) against it in CI.
- [ ] Run Plan 2 fresh replay and previous-release upgrade, history immutability
  and canonical evidence comparison, then Plan 1 disposable APEX round-trip
  and master fixtures. Run the actual selected candidate applications and their
  declared checks on disposable replay as specified below. Upload sanitized
  results and failed captures.
- [ ] Run untrusted PR code only in disposable isolated runners without shared
  integration credentials. Do not use pull_request_target to execute PR payloads.
  Destructive migration fixtures are confined to explicitly authorized test
  instances; deployment defaults still refuse destructive pending work.
- [ ] Record complete manifests including additions. Never overwrite tracked
  database/ to measure a replay diff. Qualification and evidence must identify
  the exact source SHA.
- [ ] Test missing toolchain, absent provisioning, mismatched token, cleanup
  failure, nonempty/shared target, malformed output and a deliberately broken
  migration. None may report PASS/skipped as successful qualification.

### Required candidate-application checks

**Files:** `ci/app-checks/<alias>.json`, `ci/app-checks/<alias>/*.verify.sql`,
`ci/app-checks/<alias>/flows/*.json`, `scripts/teamlib/app_checks.py`,
`scripts/tests/test_app_checks.py`, `scripts/tests/live/test_app_checks.py`.
**Interface:** `verify_candidate_apps(source, replay_target, checks) ->
AppCheckReport`.

- [ ] Materialize every actual app from the same selected SHA used for
  migrations; deploy into that disposable replay target in master order through
  Task 4 with verified replay_proof. The provisioner exposes ORDS/base URL and
  isolated test-user provisioning as well as SQLcl profiles. Shared
  development/integration profiles cannot satisfy this gate.
- [ ] Version 1 declaration schema contains alias, covered page IDs, checks
  (unique ID, page ID, kind, expected object names) and required fixture IDs.
  `select` checks reference a tracked .verify.sql member using Plan 2's
  restricted observation-only grammar and VERIFY profile; checks assert the
  actual page's required schema/data conditions. `flow` checks reference
  declarative steps with path, action (`navigate`, `fill`, `click`),
  selector, optional value or test-secret reference, and expected visible
  text/URL. Run them through a pinned browser adapter on verified replay/test
  targets with isolated test fixtures; only disposable replay satisfies
  source qualification. Test promotion supplies its own fixture identities.
  No arbitrary script/eval steps or embedded credentials. Record page/flow
  coverage and source/check digests.
- [ ] Every shipped app requires at least one SELECT dependency assertion and
  one authenticated or explicitly public page smoke flow. Changed database
  dependencies require updated checks in review. Report uncovered paths
  honestly; these declarations do not automatically discover every dynamic SQL
  reference.
- [ ] AppCheckReport contains source SHA, replay identity, app/page/check IDs,
  declared object names, observed diagnostics and PASS/FAIL/UNKNOWN for each
  required check. Any missing declaration, fixture, runner capability, result,
  skipped check or UNKNOWN fails qualification. Do not infer runtime correctness
  from app export equality or schema fingerprints.
- [ ] Acceptance fixture: candidate page requires a column absent from selected
  migrations but present on shared development/integration. Its SELECT assertion
  and/or exercised page must fail on disposable replay, reporting app, page and
  column; integration deployment is never launched. Merging the migration makes
  the same checks pass. Run this on fresh replay and previous-release upgrade.

The workflow job dependency graph is mandatory:

```text
offline checks -> provision disposable targets -> fresh replay
              -> upgrade replay -> APEX round-trip/master cases
              -> candidate-app dependency/page checks
              -> publish qualification evidence -> cleanup exact targets
```

Initial release with no previous artifact requires an explicit initial-release
classification and records that upgrade was not applicable; it does not silently
substitute an empty previous release.

## Task 6: Post-merge integration from canonical source

**Files:** .github/workflows/integration.yml, scripts/tests/test_integration_workflow.py.

- [ ] Trigger on push to main and resolve the event SHA. Require offline checks
  and qualified database-checks for that exact SHA before deployment.
- [ ] Use a protected integration environment and concurrency group scoped to
  project/target with cancel-in-progress false. A queued older SHA must not
  replace a newer verified deployment.
- [ ] Provision saved connections in the job's isolated store from protected
  non-production secrets. .env contains names and expectations only.
- [ ] Apply migrations using shared mode when integration uses the shared dev
  schema. Report foreign_applied IDs and enforce the observed-history drift
  gate; do not pretend the shared schema equals HEAD. Fresh replay supplies
  canonical proof.
- [ ] Deploy every tracked application from the selected commit, respecting
  master-before-subscriber dependencies. Detect cycles/missing managed masters;
  externally managed masters must already satisfy the contract.
- [ ] Verify actual app bytes/linkage and schema drift after deployment.
  Upload per-app and migration evidence; failure blocks the successful build
  status and retains recovery. Cleanup only job-local secrets/temporary files.
- [ ] Require Task 5's candidate-app results for this exact SHA before the
  first integration write. Missing dependencies are tested on disposable replay,
  not on shared integration, which can already contain unmerged columns. Display
  failed app/page/check/object identities from that gate in the integration
  failure report. Shared integration never claims its schema is merged-only.

- [ ] Test workflow behavior with a fake provisioner/SQLcl and two queued commits.
  YAML parsing alone is not acceptance.

## Task 7: Immutable release artifact and target-specific planning

**Files:** scripts/teamlib/release.py, scripts/tests/test_release.py,
scripts/build_release.sh/.ps1, docs/promotion.md.

**Interfaces:** `build_release(repo, ref, version, out) -> Manifest`;
`verify_release(release_tar) -> Manifest`;
`plan_release(release_tar, history, target) -> ReleasePlan`;
`apply_release(release_tar, target, plan) -> ApplyReport`.

Canonical distributable: release.tar. Build returns a staging directory plus
this exact archive. CI uploads/downloads it as an opaque file; the GitHub wrapper
archive is not the release identity. Test and production verify the same tar
SHA-256 before safe extraction and complete-manifest verification.
Every consumer takes the archive file, opens one stable file handle, verifies
its digest and extracts from those same bytes into a fresh private staging
directory. It rejects archive mutation, unsafe members and manifest mismatch;
it never trusts a caller-supplied previously extracted directory. Internal
replay/deploy APIs receive this verified materialization and its archive digest.

Artifact layout after safe extraction:

```text
release/
  MANIFEST.json
  apps/<alias>/...                 owned source only, no deployments/
  migrations/...                  complete immutable two-member bundles
  evidence/schema/...             canonical schema fingerprints
  contracts/masters.json          master and component requirements
  contracts/toolchain.json        qualified versions and manifest format
  checks/apps/...                 candidate app checks and flow declarations
  tools/...                      versioned offline verification/planning tools
```

- [ ] Resolve the tag/commit once and read only its Git blobs via an explicit
  allowlist. Dirty/untracked local files cannot enter the artifact, even when
  they match filenames. Refuse symlinks, paths escaping root and unsupported
  modes. Never copy the working directory with cp -r.
- [ ] Include complete migration history; exclude operations/zz_*, credentials,
  .env, default/named deployment bindings, sync state, logs and scratch.
- [ ] Manifest fields: format_version, version, source_commit, source_tree,
  toolchain, ordered migration IDs/checksums/dependencies, owned app tree
  digests, master contract digest, app-check digest, sorted payload
  path/size/SHA-256 entries.
  The manifest does not hash itself. The SHA-256 of release.tar is the external
  artifact digest recorded in CI evidence and the protected release record.
  Serialize sorted POSIX ustar entries with UTF-8 names, uid/gid 0, empty
  uname/gname, mtime 0, file modes 0644 (0755 only for allowlisted tools),
  directory mode 0755, no compression/PAX/extensions, and standard 10240-byte
  record padding. Reject names/sizes that ustar cannot represent; never truncate.
- [ ] **Pre-validate ustar path representability before writing any entry.**
  ustar stores a 100-byte name and an optional 155-byte prefix that must split
  at a `/`; a path is representable only if such a split exists, which is a
  stricter condition than "under 255 bytes". Real APEXlang trees approach this:
  `release/apps/<alias>/shared-components/themes/universal-theme/...` and
  static-file paths are already ~115 bytes before a long component name, and
  `tarfile` with `USTAR_FORMAT` raises `ValueError: name is too long` at write
  time — after partial output, on a release build.
  Compute the split for every path up front and refuse with a message naming
  the offending path, its byte length and the best available split point.
  Add a test over the deepest realistic APEXlang hierarchy, including a
  path with no valid split point, asserting refusal happens before any bytes
  are written and that the output path is not left partially populated.
  If a genuine APEX export is found that cannot be represented, that is an
  explicit decision point — switch to `PAX_FORMAT` with pinned deterministic
  headers, re-verifying cross-platform byte identity — not a silent format
  change and never a truncation.
- [ ] Verify file hashes AND complete file set before any application. Missing,
  unexpected and changed files all refuse. Reject output-directory reuse.
- [ ] plan-release compares the destination's exact history (identity, metadata
  version, sequence and checksums) with artifact history in strict mode,
  producing pending IDs plus target/history/artifact digests. No DB connection.
- [ ] apply-release takes --plan, verifies its archive/target/history digests,
  then rereads history under the migration mutex and recomputes the pending plan.
  Require exact equality before writes; changed pending work refuses and requires
  a new plan. Pass the reviewed plan into Plan 2 apply_plan as expected_plan;
  that comparison and execution share the same held mutex. apply-release is
  also where the archive's packaged apps reach a target: after migrations
  apply, deploy every `apps/<alias>` tree the archive contains, in master
  dependency order, through Task 4's `deploy_app` — fed the verified archive
  bytes as its `source_tree`, not a Git commit. `deploy-app`'s own CLI stays
  `--ref COMMIT`-only, for an ad hoc named-target deployment outside a
  release; apply-release is the release-shaped path, and is what Task 8's
  test job means by "deploy apps in master dependency order". Credentials
  and selected binding are injected only into deployment staging.
- [ ] Add positive build tests from a temporary Git commit, dirty/untracked
  contamination tests, binary parity, deterministic rebuild, path attacks,
  historical-already-applied exclusion, tampered history and artifact tampering.
  Build release.tar on Linux and Windows and compare identical bytes. Test
  unsupported ustar names and a changed target history after plan generation.

Core hashing recipe shared with bundle manifests:

```python
import hashlib
import json

def canonical_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_release.py -v`.

## Task 8: Tag-to-test promotion and offline production handoff

**Files:** .github/workflows/release.yml, scripts/teamlib/runbook.py,
scripts/tests/test_release_workflow.py, scripts/tests/test_production_boundary.py,
docs/promotion.md.
**Interface:** `gen_runbook(release_tar, history, target, test_evidence,
signature, trust_key) -> Runbook`.

- [ ] Bind refs/tags/vX.Y.Z exactly to manifest version X.Y.Z. Configure protected
  immutable tags and a protected append-only release record mapping version to
  source SHA/archive digest. Refuse mismatched --version, moved/recreated tags
  and version reuse, even if an output directory does not yet exist. Test all
  three cases with simulated release records.
- [ ] Trigger release workflow on semver tags; resolve the tag SHA and require
  it belongs to reviewed canonical history and has exact-SHA qualification.
  Build once, upload/retain the immutable artifact and external digest.
- [ ] In a protected test job, download and verify that artifact, load explicit
  test bindings, check strict history, apply pending migrations, deploy apps in
  master dependency order and run structural/data/APEX/subscription checks plus
  the artifact's packaged application checks through the qualified adapter.
  Serialize target jobs and refuse outdated deployment.
- [ ] Emit TEST_EVIDENCE.json with format version, archive SHA-256, source SHA,
  qualification SHA/toolchain digest, target identity, CI run identity and final
successful deployment/schema/data/APEX/subscription/application-check results,
  including disposable replay identity, coverage and check digests. Sign
  canonical JSON bytes with the protected CI Ed25519 signing key after all gates
  pass.
  Use a qualified pinned cryptography dependency for offline verification;
  include its installation/version/hash requirements in the toolchain contract.
  Production receives the same artifact bytes; never rebuild from main.
- [ ] Implement gen-runbook as offline code taking artifact, owner-supplied
  history, target contract, TEST_EVIDENCE.json, detached signature and a trusted
  public key supplied independently of the artifact. Verify signature, archive
digest, source SHA, qualification identity, app-check digests/coverage and all
  required PASS results. Missing/failed/untrusted evidence refuses a ready
  handoff. Test tampering,
  wrong key, unrelated artifact and failed-result attestations. Output is a
  human document and pending plan, not a production apply command.
- [ ] Runbook must state exact source/artifact digest, tool versions, target
  identity, metadata owner, pending IDs/targets/checksums, master requirements,
  backup/restore evidence, maintenance/destructive prerequisites, verification
  queries, app import order, success-before-log uncertainty and recovery.
- [ ] Include a reviewed manual metadata/attempt recording procedure for the
  production owner (all log writes through the isolated metadata schema), supported-object
  expectations and data verification. Never instruct them to blindly replay
  all migration history or expect DDL rollback.
- [ ] State that source SQL is trusted reviewed deployment code, not a sandbox.
  Wrapper tests prove the declared automated entry points reject production;
  they do not prove arbitrary SQL cannot be run by a human with credentials.
- [ ] Parameterize fake-SQLcl boundary tests over import-app, deploy-app,
  apply-release, migrate, bootstrap-app, migrate --bootstrap, setup-state,
  register-app, adopt-app, adopt-baseline, recover-app-lock and
  recover-migration — bootstrap and adopt each have two distinct entry points
  (app-level and metadata-level) and all four need their own production
  refusal test, not two standing in for four.
  With valid
  production config, assert explicit production refusal and ZERO write
  launches. Pair every negative with valid non-production positive coverage
  so a missing file or broken parser cannot masquerade as protection.
- [ ] For build-release, verify-release, plan-release and gen-runbook, make any
  process/network/database invocation fail the test. Add malicious alternate
  deployment connection tests and ensure no artifact carries production secrets.
- [ ] Test a complete tag/build/download/verify/test/deploy/evidence flow plus
  tampering, failed migration, wrong master and unavailable target failures.
  Confirm no production apply job or configuration override exists.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_production_boundary.py' -v`;
run test_release_workflow.py for the non-production end-to-end fixture.

## Task 9: User documentation and final acceptance

**Files:** README.md, docs/promotion.md, .agents/workflows/team-flow.md,
scripts/tests/test_docs.py, docs/design-review-resolution.md.

- [ ] Document prerequisites, clone setup, exact profiles (including isolated
  METADATA and observation-only VERIFY), initial app adoption,
  migration-baseline adoption, ordinary Builder/source workflows, recovery,
  branch integration, test promotion and production handoff.
- [ ] Explain that alias is stable logical identity and app IDs differ **between
  environments, not between developers** (spec §2): the team shares one
  development workspace, application and ID, while integration, test and
  production give the same application different ones. Named deploy bindings and
  profiles must agree; neither can silently override the other.
- [ ] Document the shared-application working agreement as workflow, not caveat:
  the three-step daily loop, why there is no import step, the team pause an
  import requires, that exports need no announcement, and that a branch does not
  isolate APEX source. Include a worked day for two developers.
- [ ] Explain shared integration contamination, foreign migrations, canonical
  replay and partial-DDL limitations with concrete examples.
- [ ] Add a review-resolution matrix mapping every original finding to the
  revised contract, implementation task and acceptance test.
- [ ] Verify local links, documented CLI --help examples, all offline/native
  suites, isolated live gates and complete release-to-test acceptance.
- [ ] Inspect final diff and preserve unrelated files. No implementation claim
  is made for a checklist item still unchecked.

## Completion checklist

- [ ] Agent rules are consistent with alias/recovery/migration ownership, and
  with the shared application: no import in the daily loop, no export described
  as the agent's own changes, no branch treated as isolating APEX source.
- [ ] Conflict assistant never writes tracked source or selects a value on the
  developer's behalf; resolve-export independently re-verifies its output.
- [ ] The import announcement names the target and the uncaptured work it found,
  invents no duration, and is followed by an all-clear only after a verified
  import. A confirmed import over a colleague's uncaptured work still refuses.
- [ ] Required CI actually provisions fresh targets and proves replay/import.
- [ ] Integration deploys an exact SHA and reports shared foreign history.
- [ ] Releases are deterministic and verified; test promotes the downloaded bytes.
- [ ] Production generation is offline and every automated write surface refuses it.
- [ ] Human handoff includes pending selection, log ownership and partial-failure recovery.
