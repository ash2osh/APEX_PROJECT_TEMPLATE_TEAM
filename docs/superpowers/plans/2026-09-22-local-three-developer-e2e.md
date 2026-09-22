# Local Three-Developer End-to-End Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add and run a repeatable local end-to-end acceptance test in which Alice, Bob, and Carol use independent Git clones against one disposable shared APEX application and schema, proving the repository's real export, reconciliation, migration, coordinated-import, mutex, recovery, convergence, and cleanup behavior.

**Architecture:** A small Python harness creates a run-owned fixture on the `docker-demo` Oracle/APEX instance, clones the current repository three times behind a local bare Git remote, and drives the existing public `scripts/team.py` commands from each clone. Fixture-only APEXlang imports emulate completed Builder saves by applying one exact, reviewed mutation to a fresh capture; product imports, exports, migrations, drift checks, receipts, mutexes, and recovery continue through the repository's qualified adapters. Every database object, saved connection, clone, and application is recorded in an immutable ownership manifest before creation, and cleanup refuses unless the live identity still matches that manifest.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Git, Oracle SQLcl 26.2+, Oracle Database 23ai Docker (`docker-demo`/`docker-sys` saved connections), Oracle APEX 26.1+, APEXlang, ORDS, and the repository's existing qualified SQLcl adapters.

**Spec:** `docs/superpowers/specs/2026-09-06-team-template-design.md` §§2, 2.1, 6, 7, 10, and 11; `docs/migrations.md`; Oracle SQLcl [APEX command](https://docs.oracle.com/en/database/oracle/sql-developer-command-line/26.1/sqcug/apexlang.html); Oracle APEX [`APEX_APPLICATION_INSTALL.REMOVE_APPLICATION`](https://docs.oracle.com/en/database/oracle/apex/26.1/aeapi/APEX_APPLICATION_INSTALL.REMOVE_APPLICATION-Procedure.html).

## Global Constraints

- This is a local acceptance harness, not a new production workflow. Do not weaken or bypass any product guard to make the scenario pass.
- Use application ID `9099`, tracked alias `team-e2e`, APEX alias `TEAM-E2E-9099`, project ID `local-team-e2e`, and metadata schema `TEAM_E2E_META`. Refuse before writes if any already exists; never silently choose, overwrite, adopt, or delete another resource.
- Treat existing app `103` only as a read-only seed. Capture it, clone its exported bytes into app `9099`, and never import, mutate, reserve, or remove app `103`.
- Verify `DB_NAME`, service, `INSTANCE_ID`, workspace ID/name, session user, current schema, seed app ownership, and absence of fixture resources before the first write. The APEX, TABLES, CODE, VERIFY, METADATA, and admin sessions must resolve to the same physical database identity.
- Create and use a distinct `TEAM_E2E_META` controller schema. Application/table/code work uses `DEMO`; controller and migration metadata work uses `TEAM_E2E_META`. Credentials may exist only in memory, SQLcl's secure saved-connection store, and mode-`0600` run scratch; never print, commit, or put them in process arguments.
- Only fixture administration—creating/dropping `TEAM_E2E_META`, creating/removing the fixture saved connection, and removing app `9099`—may use the verified `docker-sys` adapter. All workflow writes use normal environment profiles and repository adapters.
- Use a local bare remote under `scratch/local-team-e2e/<run-id>/remote.git`. Do not contact GitHub, push a shared remote, alter the caller's current branch, or reuse the main checkout as a developer clone.
- Alice, Bob, and Carol have independent clone paths, branches, `TEAM_CHECKOUT_UUID` values, `.sync-state/` directories, environment files, and Git identities. They share exactly one APEX app, workspace, schema set, metadata controller, and local remote.
- The daily test loop remains Builder-state mutation, `export-app`, review, explicit staging, commit, pull/rebase, and push. There is no normal import step.
- Fixture mutation imports are test-driver actions that emulate the resulting shared APEX state after a Builder save; label them `fixture_builder_save` in evidence. Do not count them as proof of APEX page-lock UI behavior or simultaneous browser editing.
- Product `import-app` remains a coordinated whole-app overwrite. It must use a verified baseline or receipt, `announce-import`, `--confirm-pause`, the shared mutex, a post-import re-export, and an all-clear notice.
- Retain `.sync-state/`, SQLcl logs, subprocess results, Git graphs, captures, screenshots, and a redacted JSON report under the ignored run root. On failure, retain the fixture by default for diagnosis. Cleanup is a separate exact-owned action.
- Cleanup must verify the ownership manifest, live identity, application ID plus alias plus workspace, metadata schema identity, saved-connection name, and clone roots. Any mismatch refuses cleanup. Existing applications, schemas, connections, and repositories are never cleanup targets.
- Uncaptured/transient Builder edits and arbitrary DML remain outside tooling visibility. The final report must distinguish offline tests, live database/APEX evidence, Git convergence, runtime browser evidence, and any unavailable layer.

## Review Focus

- **Wrong target or occupied fixture:** a changed database identity, workspace, seed ownership, app `9099`, alias `TEAM-E2E-9099`, metadata schema, or connection name must stop before the first write and must never be "fixed" by deletion.
- **Real clone isolation:** Alice, Bob, and Carol must have distinct checkout UUIDs and local histories while the controller roster shows all three against the same physical app key.
- **Shared-state reconciliation:** a stale clone export must preserve a colleague's already-live APEX change; a source-only same-file change against a different live value must create a retained conflict recovery instead of silently selecting either side.
- **Coordinated overwrite safety:** an import overlapping an export must make the export refuse/discard its capture; a lost import acknowledgement must retain an uncertain mutex until evidence-driven recovery.
- **Owned cleanup only:** cleanup must remove app `9099`, `TEAM_E2E_META`, its saved connection, and run-owned scratch only after proving ownership; app `103`, user `DEMO`, and every pre-existing app/schema must remain byte-for-byte or identity-equivalent.

---

## File Structure

### New files

- `scripts/teamlib/local_team_e2e.py` — fixture manifest, strict subprocess runner, live preflight/provisioning, three-clone orchestration, scenario assertions, reporting, and cleanup.
- `scripts/local-team-e2e.py` — thin CLI exposing `preflight`, `run`, `status`, and `cleanup`.
- `scripts/tests/test_local_team_e2e.py` — offline unit/contract tests using fake Git, SQLcl, and process adapters; no Docker requirement.
- `docs/local-three-developer-e2e.md` — operator runbook, evidence layers, failure recovery, and explicit coverage limits.

### Modified files

- `README.md` — link the local team acceptance runbook without adding it to the normal daily workflow.
- `.gitignore` — no change expected because `scratch/`, `.env.*`, and `.sync-state/` are already ignored; add a regression assertion instead.

---

### Task 1: Define the Run-Owned Fixture Contract and Fail-Closed CLI

**Files:**
- Create: `scripts/teamlib/local_team_e2e.py`
- Create: `scripts/local-team-e2e.py`
- Create: `scripts/tests/test_local_team_e2e.py`

**Interfaces:**
- `FixtureSpec(seed_app_id=103, fixture_app_id=9099, tracked_alias="team-e2e", apex_alias="TEAM-E2E-9099", metadata_schema="TEAM_E2E_META")`
- `RunManifest.create(run_root, spec, source_commit, expected_identity)` writes `manifest.json` once with mode `0600` and refuses overwrite.
- `CommandResult(argv, cwd, returncode, stdout_path, stderr_path, started_at, finished_at)` stores paths and hashes, not credentials.
- CLI: `local-team-e2e.py preflight|run|status|cleanup --run-root PATH [--keep-on-success]`.

- [ ] **Step 1: Write failing manifest and argument-safety tests**

Cover exact constants, canonical run paths, immutable manifest creation, source commit binding, explicit phase transitions, redaction, safe argv validation, symlink refusal, traversal refusal, and cleanup target enumeration. Assert that a manifest cannot name `/`, the repository root, a path outside its run root, app `103`, schema `DEMO`, or any resource not carrying the fixture namespace.

```python
def test_cleanup_targets_are_exact_and_never_include_seed(self):
    manifest = fixture_manifest()
    targets = manifest.cleanup_targets()
    self.assertEqual(targets.application_id, 9099)
    self.assertEqual(targets.metadata_schema, "TEAM_E2E_META")
    self.assertNotEqual(targets.application_id, manifest.spec.seed_app_id)
    self.assertNotIn("DEMO", targets.schemas)


def test_manifest_refuses_rebinding_after_creation(self):
    manifest = RunManifest.create(self.root, fixture_spec(), "a" * 40, docker_identity())
    with self.assertRaisesRegex(E2EError, "already exists"):
        RunManifest.create(self.root, fixture_spec(), "b" * 40, docker_identity())
```

- [ ] **Step 2: Confirm the test contract fails before implementation**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
```

Expected: FAIL because the module and launcher do not exist.

- [ ] **Step 3: Implement the data model, durable recorder, and CLI parser**

Use frozen dataclasses and closed JSON shapes. `run_id` is UTC `YYYYMMDDTHHMMSS-<8 lowercase hex>`; derive all resource names from the fixed spec, not user-controlled SQL text. `run_command()` must accept an argv list only, use `shell=False`, write stdout/stderr to run-owned files, scrub generated secrets before persistence, and record SHA-256 plus exit status. Never interpolate a credential into an error.

The launcher only dispatches. `preflight` is read-only, `run` executes all phases and retains resources after a failure, `status` is read-only, and `cleanup` requires both `--run-root` and `--confirm-run-id <exact-id>`.

- [ ] **Step 4: Run focused tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/local-team-e2e.py \
  scripts/tests/test_local_team_e2e.py
git commit -m "test: add local team e2e harness contract"
```

Expected: unit tests PASS; no database or Git remote is touched.

---

### Task 2: Add Identity-Checked Fixture Provisioning and Exact Cleanup

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

**Interfaces:**
- `inspect_fixture(payload_connection, admin_connection, spec) -> PreflightEvidence`
- `provision_fixture(evidence, manifest) -> ProvisionedFixture`
- `cleanup_fixture(manifest, live_evidence) -> CleanupReport`
- Generated saved connection: `docker-team-e2e-meta-<run-id-suffix>`.

- [ ] **Step 1: Write failing preflight and cleanup-ownership tests**

Fake the qualified adapter and assert zero write calls when:

- payload/admin physical identities differ;
- the environment class is production;
- app `103` is missing or not in the observed workspace;
- app ID `9099` or alias `TEAM-E2E-9099` exists;
- `TEAM_E2E_META` exists;
- the generated saved-connection name exists;
- the APEX workspace ID or parsing schema is absent; or
- cleanup sees a different alias, workspace, schema creation marker, or run ID.

Add a positive call-order assertion:

```python
self.assertEqual(events, [
    "read:payload-identity",
    "read:admin-identity",
    "read:workspace-and-apps",
    "read:schemas-and-users",
    "read:saved-connections",
    "write:create-metadata-user",
    "write:save-metadata-connection",
    "write:clone-seed-to-fixture",
    "read:verify-fixture-export",
])
```

- [ ] **Step 2: Implement read-only preflight through qualified targets**

Construct non-production `Target` values and use `teamlib.sqlcl.run_sqlcl` for identity and dictionary queries. The query returns explicit markers for workspace ID/name, app ID/alias, parsing schema, user/schema existence, and APEX version. Capture app `103` with SQLcl `APEX EXPORT -APPLICATIONID 103 -EXPTYPE APEXLANG` into run scratch and hash every member; this is the immutable seed evidence.

Do not infer workspace or instance values from `.env`. Generate the per-clone environment from observed markers only after all identities agree.

- [ ] **Step 3: Implement secure, bounded provisioning**

Generate a random Oracle password in memory with `secrets`, create only `TEAM_E2E_META` through the verified admin target, and grant only `CREATE SESSION`, `CREATE TABLE`, and an explicit tablespace quota needed by controller/migration metadata. Create the SQLcl saved connection without putting the password in argv or logs; feed it through a mode-`0600` transient SQLcl input under the run root, delete that input immediately, and verify the saved connection by running the normal identity adapter.

Clone the captured app-103 APEXlang tree to app `9099` using SQLcl's documented `APEX IMPORT` overrides:

```text
APEX IMPORT -INPUT <run-owned-seed-dir> -ID 9099 \
  -ALIAS TEAM-E2E-9099 -WORKSPACEID <observed-id> -SCHEMA DEMO
```

Immediately export app `9099`, require its observed app ID/alias/workspace/parsing schema to match the manifest, and persist its tree digest. Record a fixture marker inside `TEAM_E2E_META` containing the run ID and app digest; cleanup later requires this exact marker.

- [ ] **Step 4: Implement cleanup as a separately confirmed phase**

Before deletion, repeat the complete read-only preflight. Remove app `9099` with `APEX_APPLICATION_INSTALL.SET_WORKSPACE(<observed-name>)`, `SET_KEEP_SESSIONS(false)`, and `REMOVE_APPLICATION(9099)` only after ID, alias, and workspace match. Drop `TEAM_E2E_META` only after its marker matches. Delete only the generated saved connection and run-owned clone/remote directories. Verify absence and also verify that app `103`, its tree digest, user `DEMO`, and the preflight set of unrelated application IDs still exist.

If any delete result is unknown, stop, retain the manifest, and report cleanup `UNKNOWN`; do not retry destructively.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: guard disposable oracle apex fixture lifecycle"
```

---

### Task 3: Build Three Independent Developer Clones and Adopt the Shared App

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

**Interfaces:**
- `create_team_topology(manifest, env_values) -> TeamTopology`
- `Developer(name, clone, branch, checkout_uuid, git_email, env_file)`.
- `run_team_command(developer, *args) -> CommandResult` sets `USER`, `TEAM_CHECKOUT_UUID`, and `PYTHONDONTWRITEBYTECODE=1` without changing the parent environment.

- [ ] **Step 1: Write failing topology tests**

Assert one run-owned bare remote, three non-overlapping clone roots, exact branch names `e2e/alice`, `e2e/bob`, `e2e/carol`, unique deterministic checkout UUIDs, and distinct Git identities. Assert every clone's `origin` is the local bare path and no configured remote URL has a network scheme or host.

Assert generated `.env.local-team-e2e` files are mode `0600`, ignored by Git, contain connection names/expected identities only, use `APP_OWNERSHIP_MODE=shared`, and bind:

```text
APEX_APPS=team-e2e:9099
TABLES_SCHEMA=DEMO
CODE_SCHEMA=DEMO
APEX_PARSING_SCHEMA=DEMO
METADATA_SCHEMA=TEAM_E2E_META
```

- [ ] **Step 2: Implement local Git topology and environment generation**

Resolve and record the caller's exact `HEAD`; require a clean tracked/untracked source set except ignored scratch. Create a bare repository, seed it from that commit, then clone Alice/Bob/Carol. Configure only clone-local `user.name`, `user.email`, and branch upstreams. Never add the bare remote to the caller checkout.

- [ ] **Step 3: Exercise the real bootstrap/adoption workflow**

From Alice:

```bash
scripts/team.py --env .env.local-team-e2e doctor
scripts/team.py --env .env.local-team-e2e setup-state
scripts/team.py --env .env.local-team-e2e register-app team-e2e
scripts/team.py --env .env.local-team-e2e bootstrap-app team-e2e
git add apps/team-e2e
git commit -m "test: bootstrap shared e2e app"
git push -u origin e2e/alice:main
scripts/team.py --env .env.local-team-e2e adopt-app team-e2e
```

Bob and Carol fast-forward from local `origin/main`, run `register-app`, then `adopt-app`. Assert `app-status` lists exactly three fixture checkout UUIDs and one clear generation-1 mutex. Save each clone's baseline/checkpoint digest and require equality.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: model three independent shared app checkouts"
```

---

### Task 4: Prove Two-Developer Daily Export and Git Reconciliation

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

**Interfaces:**
- `capture_live_tree(developer) -> CapturedTree`
- `fixture_builder_save(developer, mutation) -> FixtureMutationEvidence`
- `ApexMutation(relative_path, expected_old, replacement, actor, reason)` requires exactly one matching UTF-8 line and records before/after member hashes.

- [ ] **Step 1: Write failing exact-mutation and sequence tests**

Assert mutation refuses zero or multiple matches, binary members, a stale expected value, a path absent from the fresh live capture, or any path outside the captured application. Assert the driver always starts from a fresh capture and imports only to app `9099` on the bound workspace/schema.

Model the expected daily sequence:

1. Alice's fixture save changes the app message text in `shared-components/messages.apx` from the exact captured `text: Simple App` value to `text: Simple App - Alice`.
2. Alice runs `export-app`, reviews only expected APEXlang changes, commits, and pushes to local `main`.
3. Bob remains on his stale pre-Alice commit.
4. Bob's fixture save starts from the now-live Alice tree and changes the page region name in `pages/p00001-home.apx` from the exact captured `name: Simple App` value to `name: Simple App - Bob`.
5. Bob runs `export-app`; reconciliation must preserve Alice's message change even though his Git HEAD is stale.

- [ ] **Step 2: Implement the fixture-only Builder-state injector**

Capture the live fixture through the normal qualified APEX target, make one exact line mutation in a run-owned copy, invoke APEXlang import with the fixture ID/workspace/schema, and re-export to prove the live tree equals the intended mutation. Record the event as `fixture_builder_save`, not `import-app`, and record that this proves the shared database state seen by export—not Builder page locks or browser save UX.

- [ ] **Step 3: Execute and assert the two-developer scenario**

After Bob's export, require:

- Bob's working tree contains both Alice and Bob changes;
- no other source member changed unexpectedly;
- Bob commits, rebases on local `origin/main`, and pushes without forced update;
- Alice fast-forwards and `export-app` reports no changes after adoption/checkpoint refresh; and
- all source tree digests equal a fresh live capture.

Store `git log --graph --decorate --oneline --all`, `git status --porcelain=v1 --untracked-files=all`, export command results, and member hashes.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: exercise shared app daily reconciliation"
```

---

### Task 5: Prove Same-File Conflict Retention and Reviewed Resolution

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

- [ ] **Step 1: Write a failing three-way-conflict scenario test**

From Carol's clean adopted baseline, commit a source-only change of the page region name to `Simple App - Carol Source`. Without importing Carol's commit, apply a fresh fixture Builder save that changes the same exact live line to `Simple App - Shared Builder`. Then run Carol's `export-app`.

Expected behavior:

- command exits non-zero with an export conflict;
- Carol's committed source remains unchanged;
- a recovery ID and four-way capture are retained under Carol's `.sync-state/recovery/`;
- evidence contains base, mine/live, current Git source, and conflict path; and
- no automatic import or resolution occurs.

- [ ] **Step 2: Implement evidence extraction without parsing human prose**

Read the immutable recovery JSON using its closed schema and verify target state key, HEAD, app identity, tree digests, and conflict member. Materialize a complete reviewed result tree choosing `Simple App - Shared Builder` while preserving all non-conflicting source bytes. Pass that directory to:

```bash
scripts/team.py --env .env.local-team-e2e resolve-export <recovery-id> \
  --resolved <run-owned-reviewed-tree>
```

Assert a resolution receipt is created, commit the result, rebase/push to local `main`, and prove a fresh export is clean. This tests Git-versus-database conflict handling; it must not be reported as recovery of a developer-versus-developer Builder overwrite.

- [ ] **Step 3: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: retain and resolve shared apex export conflicts"
```

---

### Task 6: Prove Import Pause, Export Interlock, Unknown Result, and Recovery

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

**Interfaces:**
- `SqlclGate` creates a run-owned executable wrapper and two FIFOs/files: `payload-started` and `release-payload`; it delegates to the resolved real SQLcl executable.
- Per-process `TEAM_SQLCL_EXECUTABLE` selects the gate only for the intended import process.

- [ ] **Step 1: Write failing concurrency and unknown-result orchestration tests**

Use fake subprocesses to prove the harness waits for the product import to mark the shared mutex before starting the competing export, has bounded timeouts, always releases local child processes, and never treats timeout/unknown as success.

- [ ] **Step 2: Exercise a successful coordinated import overlapping an export**

Bring all clones to the same local `main`. In Alice, run `announce-import team-e2e --ref HEAD`, persist the exact pause notice, then start `import-app team-e2e --ref HEAD --confirm-pause` with `SqlclGate` paused after the import payload has been marked starting but before SQLcl completes.

While Alice owns the mutex, Bob runs `export-app team-e2e`. Require Bob to refuse/discard the capture, retain no reconciled application changes, and name the held/uncertain target. Release Alice's SQLcl, require verified post-import re-export, a generation increment, a clear mutex, and `announce-import --all-clear <operation-id>`.

- [ ] **Step 3: Exercise lost acknowledgement and explicit recovery**

Run a second coordinated Alice import through a gate mode that lets the database payload complete and then terminates the wrapper before the repository receives its completion marker. Require:

- `import-app` reports unknown, not failed/success;
- the application mutex remains owned and uncertain;
- Bob and Carol exports refuse;
- live read-only capture is compared with the intended commit;
- the harness writes a human-readable recovery evidence file containing command/result hashes, live tree digest, intended tree digest, target identity, and reviewer decision; and
- only then does Alice run `recover-app-lock team-e2e --run-token <token> --evidence <file>`.

After recovery, require a clean mutex, incremented generation consistent with the controller rules, and successful no-change exports from all three clones. Do not synthesize a success result if live bytes differ.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: cover coordinated import interlocks and recovery"
```

---

### Task 7: Prove One Shared Migration and Cross-Clone Visibility

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`

- [ ] **Step 1: Write a failing migration scenario test**

Alice creates one non-destructive migration with the public authoring command, then the harness fills its exact members:

```sql
-- forward SQL after the generated header
CREATE TABLE TEAM_E2E_SHARED_NOTE (
  NOTE_ID NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  NOTE_TEXT VARCHAR2(200) NOT NULL
);
```

```sql
-- verification member
SELECT 'TEAM_ASSERT|team_e2e_shared_note|' ||
       CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END
  FROM USER_TABLES
 WHERE TABLE_NAME = 'TEAM_E2E_SHARED_NOTE';
```

The migration ID is generated once by `new-migration --author alice --slug shared-note --target tables` and captured from stdout; tests must not hardcode a timestamp.

- [ ] **Step 2: Establish and verify the migration frontier**

From Alice, run `adopt-frontier` against the fixture schema set, then `check-drift`. Run `migrate --source migrations --dry-run`, review the exact pending ID and target identity, and apply the non-destructive bundle through the public `migrate` command. Require PASS verification, history, after-inventory, and released migration mutex.

Alice commits and pushes the migration immediately. Before pulling, Bob must observe that the shared table exists but his source does not contain the bundle; record this deliberately inconsistent window. Bob then fast-forwards, runs `check-drift`, and requires clean status plus the applied migration in exported history. Carol repeats the clean check from a fresh fast-forward.

- [ ] **Step 3: Add an authored, explicitly confirmed cleanup migration**

Create a second fixture-only migration that drops `TEAM_E2E_SHARED_NOTE`; change its generated header to `-- destructive: true` and add verification that the table count is zero. Run `migrate --dry-run --confirmation-out <run-owned-path>`, review the exact migration ID, action, bundle checksum, and payload target-state key, then have the executing human change only that exact document's `confirmed` field from `false` to `true`. Apply it with `--destructive-confirmation <reviewed-path>`, verify, commit, and push before fixture teardown. The harness must never create or edit the affirmative confirmation itself. This keeps schema cleanup visible in the same immutable history the test is validating; dropping the metadata owner afterward removes only test-controller history.

- [ ] **Step 4: Run tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py
git commit -m "test: prove shared migration visibility across clones"
```

---

### Task 8: Add Final Convergence, Runtime Smoke, Reporting, and Runbook

**Files:**
- Modify: `scripts/teamlib/local_team_e2e.py`
- Modify: `scripts/tests/test_local_team_e2e.py`
- Create: `docs/local-three-developer-e2e.md`
- Modify: `README.md`

**Interfaces:**
- `verify_convergence(topology, fixture) -> ConvergenceEvidence`
- `write_report(manifest, phases, cleanup) -> scratch/local-team-e2e/<run-id>/report.json`
- Final statuses: `PASS`, `FAIL`, or `UNKNOWN`; no boolean shortcut.

- [ ] **Step 1: Write failing final-report and documentation tests**

Require the report to bind source commit, run ID, live identity, fixture ownership, three checkout UUIDs, clone HEADs, Git tree digests, live APEX tree digest, migration frontier/history digest, mutex states, runtime URL/status, per-phase command evidence, cleanup status, and explicit coverage limits. Reject credentials, connection passwords, raw environment dumps, and absolute paths outside the run root.

Add docs tests asserting the runbook names `preflight`, `run`, `status`, `cleanup`, `--confirm-run-id`, fixture constants, failure retention, and the Builder-UI limitation.

- [ ] **Step 2: Implement convergence gates**

Before PASS:

- fast-forward Alice, Bob, and Carol to the same local `origin/main` commit;
- require clean tracked/untracked status in all clones;
- run `adopt-app` as needed to refresh each clone's local baseline/checkpoint, then require no-change `export-app` from each;
- compare each `apps/team-e2e` Git tree digest with a fresh live app-9099 capture;
- require `check-drift` clean and migration history identical from all clones;
- require application and migration mutexes clear and not uncertain;
- require the controller roster to contain the three expected checkout UUIDs only for the fixture app key; and
- perform an ORDS runtime smoke against app `9099` in a private/background browser context, proving HTTP success plus the final visible page marker. Save a screenshot and close only the test context.

The browser smoke proves runtime rendering, not Builder editing, login roles, or a browser matrix.

- [ ] **Step 3: Implement redacted report and safe default lifecycle**

`run` writes the report atomically after every phase. On PASS it keeps the evidence report and, unless `--keep-on-success` is supplied, invokes the exact-owned cleanup phase. On FAIL/UNKNOWN it retains the live fixture and prints only the run root plus the `status`/`cleanup` commands required for inspection. A cleanup failure changes the overall outcome to `UNKNOWN` and preserves evidence.

- [ ] **Step 4: Write the operator runbook and README link**

Document prerequisites (`docker-demo`, `docker-sys`, SQLcl, Git, running ORDS), exact commands:

```bash
python3 scripts/local-team-e2e.py preflight \
  --run-root scratch/local-team-e2e/manual

python3 scripts/local-team-e2e.py run \
  --run-root scratch/local-team-e2e/manual

python3 scripts/local-team-e2e.py status \
  --run-root scratch/local-team-e2e/manual

python3 scripts/local-team-e2e.py cleanup \
  --run-root scratch/local-team-e2e/manual \
  --confirm-run-id <run-id printed by preflight>
```

The angle-bracket value appears only in operator documentation as a value emitted by the immediately preceding command, not as an implementation placeholder. Document the exact resources cleanup may remove and how to retain a failed run for review.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
python3 -m unittest discover -s scripts/tests -v
git diff --check
git add scripts/teamlib/local_team_e2e.py scripts/tests/test_local_team_e2e.py \
  docs/local-three-developer-e2e.md README.md
git commit -m "docs: add local three developer acceptance runbook"
```

---

### Task 9: Run the Real Docker/APEX Acceptance and Preserve Evidence

**Files:**
- No tracked source changes expected.
- Evidence: ignored `scratch/local-team-e2e/<run-id>/`.

- [ ] **Step 1: Verify the implementation checkout before live writes**

```bash
git status --short --branch
git log -1 --oneline
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -m unittest scripts.tests.test_local_team_e2e -v
python3 -m unittest discover -s scripts/tests -v
ruff check scripts
shellcheck scripts/*.sh
git diff --check
```

Expected: all commands PASS. If Ruff or ShellCheck is unavailable, report that layer unavailable; do not claim it passed.

- [ ] **Step 2: Run read-only live preflight and inspect its manifest**

```bash
python3 scripts/local-team-e2e.py preflight \
  --run-root scratch/local-team-e2e/docker-demo-20260922
```

Manually inspect `manifest.json` and `preflight.json`. Confirm the target is the local Docker database, seed app is 103, fixture app/schema/connection are absent, and the cleanup list contains no pre-existing resource.

- [ ] **Step 3: Run the full scenario without automatic cleanup**

```bash
python3 scripts/local-team-e2e.py run \
  --run-root scratch/local-team-e2e/docker-demo-20260922 \
  --keep-on-success
```

Expected: every phase reports PASS, report status is PASS, all three clones converge, app/runtime and migration checks pass, and the fixture remains available for inspection. A FAIL/UNKNOWN stops; inspect evidence and use product recovery commands where named rather than rerunning provisioning.

- [ ] **Step 4: Independently inspect final live state**

Run read-only `status`, compare the three Git logs/tree digests, inspect application/migration mutexes, fresh app capture, schema inventory, migration history, runtime screenshot, and command hashes. Confirm no GitHub remote was contacted and the caller checkout was not mutated.

- [ ] **Step 5: Clean up only the owned fixture and verify non-interference**

```bash
python3 scripts/local-team-e2e.py cleanup \
  --run-root scratch/local-team-e2e/docker-demo-20260922 \
  --confirm-run-id <exact run ID from manifest.json>
```

Expected: app `9099`, `TEAM_E2E_META`, its generated saved connection, and run-owned Git clones/remote are absent; app `103` has its preflight digest; `DEMO` and every unrelated app ID remain. The evidence report and redacted logs remain under the run root.

- [ ] **Step 6: Final tracked-scope audit**

```bash
git status --short --branch
git diff --check
git log --oneline --decorate -9
git ls-files .env .sync-state scratch
```

Expected: only planned tracked files/commits exist; no environment, credential, `.sync-state`, or scratch evidence is tracked. Do not push unless the user separately authorizes it.

---

## Acceptance Summary

The implementation is complete only when all of the following are evidenced:

1. Offline harness tests and the complete repository test suite pass.
2. Read-only preflight proves the exact local Docker target and absence of fixture resources before writes.
3. Alice, Bob, and Carol are independent Git clones and controller registrations against one shared app key.
4. Bob's stale-branch export preserves Alice's already-live APEX change.
5. Carol's same-file Git-versus-live conflict refuses, retains recovery evidence, and resolves only through `resolve-export`.
6. Coordinated import blocks a concurrent export, and lost acknowledgement retains uncertainty until evidence-driven recovery.
7. One shared migration becomes visible to stale clones, is merged promptly, and returns to clean drift after authored cleanup.
8. All clones, live APEX bytes, schema frontier, histories, and mutexes converge.
9. ORDS runtime smoke passes for app `9099` with a retained screenshot.
10. Exact-owned cleanup removes only fixture resources and re-verifies app `103` plus all unrelated preflight identities.

## Explicit Data-Quality Limits

- The automated fixture mutation proves the same post-save shared APEX state consumed by `export-app`; it does not automate App Builder login, page locking, or two humans clicking Save simultaneously.
- APEX last-save-wins between two active Builder sessions remains outside this repository's recoverable boundary. That behavior requires a separate manual/browser protocol and must not be inferred from this run.
- Runtime smoke covers one local ORDS/browser path and the fixture's public page. It is not authentication, authorization, accessibility, performance, or cross-browser certification.
- The test observes supported schema inventory and migration metadata. Arbitrary out-of-band DML and uncaptured/transient Builder edits remain unobserved.
