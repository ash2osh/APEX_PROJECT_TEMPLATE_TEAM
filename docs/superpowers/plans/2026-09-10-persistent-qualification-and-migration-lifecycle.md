# Persistent Qualification and Migration Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unusable disposable replay gate with signed qualification of persistent non-production targets, then add safe authored migration undo/redo with one exact destructive-confirmation contract.

**Architecture:** This is one plan containing two independently shippable workstreams. Workstream A extracts the useful application-check adapters, adds a read-only persistent-target qualification command and version-2 signed evidence, and simplifies CI/release workflows. Workstream B extends immutable migration bundles with optional down members, changes metadata into an append-only event ledger, and routes migrate/undo/redo through one fail-closed operation engine.

**Tech Stack:** Python 3.10+ standard library, unittest, Oracle SQL/PLSQL through qualified SQLcl profiles, GitHub Actions, Bash/PowerShell launchers, and cryptography>=41 only for the promotion signing/verification path.

**Spec:** [Design index](../specs/2026-09-10-staging-qualification-and-undo-design.md), [persistent staging qualification](../specs/2026-09-10-staging-qualification-design.md), and [migration undo/redo](../specs/2026-09-10-migration-undo-redo-design.md). Read all three before execution; the two child specs are authoritative.

## Global Constraints

- Python remains requires-python = ">=3.10"; GitHub Actions continues to pin python-version: '3.10'.
- The daily workflow remains dependency-free. cryptography>=41 stays confined to the promotion extra and the release signing/verification job.
- Production database and application writes remain refused. Production application remains an owner-operated runbook procedure.
- Default push and pull-request CI remains offline and needs no Oracle, Docker, SQLcl, credentials, or secrets.
- Persistent qualification is manual and optional. It is evidence about one observed shared target, not proof of a fresh install or isolated upgrade.
- Version-2 evidence has one source SHA field named source_commit. Version 1 is rejected rather than translated.
- Migration bundles remain immutable and checksum-bound. Down SQL is authored; it is never generated.
- A down pair is optional as a unit and must exist before the forward bundle is first applied.
- Undo is global LIFO; redo is explicit and dependency-safe; routine migrate never reapplies REVERTED migrations.
- Migrate, undo, and redo share exactly one --destructive-confirmation file contract. No boolean or loose mapping bypass remains.
- Target identity, drift, mutex, attempt, verification, inventory, observation-chain, and unknown-result recovery gates remain fail-closed.
- Files written by tooling use UTF-8 and LF via newline="\n". Migration members reject symlinks and CR line endings.
- No command commits or pushes automatically. The commit steps below are explicit execution checkpoints and never include push.

---

## Scope and delivery order

The specs already split the two independent subsystems. They remain in one plan only because that is the requested delivery format:

- Tasks 1-9 implement and finish persistent qualification. The repository is usable and testable after Task 9 without undo/redo.
- Tasks 10-17 implement and finish migration lifecycle. They consume no private Workstream A API.
- Task 18 is the combined regression and documentation audit.

Do not mix tasks from both workstreams in one commit. If Workstream A is merged first, Workstream B must rebase and rerun the full suite before changing shared files such as team.py, release.py, or release.yml.

## Requirement traceability

| Specification section | Implemented and tested by |
|---|---|
| Qualification §2, remove disposable surface | Task 6 |
| Qualification §3, reusable checks and qualification command | Tasks 1-3 |
| Qualification §4, offline CI and manual staging | Tasks 6-7 |
| Qualification §5, release evidence/signing/runbook | Tasks 2, 4, 5, 8 |
| Qualification §6, tests and documentation | Tasks 1-9 |
| Migration §1-2, boundaries and directional bundles | Task 10 |
| Migration §3, append-only lifecycle | Tasks 13-14 |
| Migration §4, planning/undo/redo/release | Tasks 11, 15, 17 |
| Migration §5, destructive confirmation | Tasks 12, 15-16 |
| Migration §6, state machine and CLI | Tasks 15-16 |
| Migration §7, metadata v2 rollout | Tasks 13-14 |
| Migration §8, tests and documentation | Tasks 10-18 |

## File structure

### Created

- scripts/teamlib/qualification.py — target-neutral app-check adapters, persistent-target validation, version-2 qualification evidence, canonical JSON writing, and Ed25519 evidence signing.
- scripts/teamlib/destructive_confirmation.py — closed-schema confirmation parsing, canonical digesting, dry-run templates, and exact required-entry matching.
- scripts/tests/test_qualification.py — qualification, evidence, archive/apply-report binding, and signing tests.
- scripts/tests/test_destructive_confirmation.py — destructive-confirmation schema and matching tests.

### Deleted

- ci/provisioners/docker_pdb.sh — non-working disposable Oracle/ORDS provisioner.
- scripts/ci_replay_runner.py — disposable orchestration after its reusable adapters move to qualification.py.
- scripts/tests/live/test_docker_qualification.py — disposable-only live qualification.

### Modified for Workstream A

- scripts/teamlib/app_checks.py — remove disposable/replay terminology and carry target_identity instead of replay_identity.
- scripts/teamlib/ci.py — retain ci_doctor only; remove replay orchestration and disposable requirements.
- scripts/teamlib/config.py — remove ci-replay from OFFLINE_COMMANDS and add sign-test-evidence.
- scripts/teamlib/release.py — make ApplyReport canonical and identity-bound.
- scripts/teamlib/release_adapter.py — emit the canonical apply report through --out.
- scripts/teamlib/runbook.py — accept only signed version-2 evidence.
- scripts/team.py — add qualify-target online dispatch and route sign-test-evidence offline.
- ci/runner-contract.json — retain only toolchain/profile/production-safety declarations.
- .github/workflows/database-checks.yml — one offline job only.
- .github/workflows/integration.yml — manual serial persistent qualification.
- .github/workflows/release.yml — apply, qualify, sign, verify, and retain a production handoff.
- scripts/tests/test_app_checks.py, test_ci_contract.py, test_integration_workflow.py, test_release.py, test_release_adapter.py, test_release_workflow.py, test_runbook.py, test_launchers.py, and test_docs.py — updated contracts.
- README.md, docs/ci.md, ci/app-checks/README.md, and docs/promotion.md — persistent qualification and signing documentation.

### Modified for Workstream B

- scripts/teamlib/migration_bundle.py — optional down pair, directional parsing, four-member checksum.
- scripts/teamlib/migration_plan.py — REVERTED-aware forward planning plus undo/redo planners.
- scripts/teamlib/migration_store.py — v2 JSON store, v2 Oracle schema upgrade, event-ledger reads/writes, attempt action and confirmation digest.
- scripts/teamlib/migrate.py — shared operation engine and public migrate/undo/redo entry points.
- scripts/teamlib/release.py and scripts/teamlib/release_adapter.py — package down members and apply REVERTED-aware release planning.
- scripts/teamlib/config.py and scripts/team.py — CLI confirmation, undo, and redo wiring.
- scripts/sql/migration_metadata.sql — reference v2 DDL.
- scripts/tests/test_migration_bundle.py, test_migration_plan.py, test_migration_store.py, test_sql_metadata_store.py, test_migration_runner.py, test_release.py, test_release_adapter.py, test_production_boundary.py, and test_launchers.py — lifecycle coverage.
- README.md, docs/migrations.md, and docs/promotion.md — authored reversal, LIFO undo, dependency-safe redo, metadata upgrade, and confirmation usage.

---

# Workstream A — Persistent target qualification

### Task 1: Make application checks target-neutral and extract the reusable adapters

**Files:**
- Create: scripts/teamlib/qualification.py
- Modify: scripts/teamlib/app_checks.py:1-302
- Modify: scripts/tests/test_app_checks.py:1-195

**Interfaces:**
- Consumes: existing AppCheckError, CheckResult, AppCheckReport, verify_candidate_apps, run_sqlcl.
- Produces:
  - AppCheckReport.target_identity: Mapping[str, Any]
  - declaration_paths(repo: Path, aliases: Sequence[str]) -> dict[str, Path]
  - select_runner(*, profile: Target, repo: Path, work: Path, sql_runner=run_sqlcl) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]
  - require_flow_adapter(declarations: Mapping[str, Any], executable: str | None) -> None
  - flow_runner(executable: str, work: Path) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]

- [ ] **Step 1: Change the app-check tests to describe a persistent target**

Replace disposable-target fixtures and ci_replay_runner imports in scripts/tests/test_app_checks.py with:

~~~python
from teamlib.qualification import flow_runner, require_flow_adapter, select_runner

target = {
    "target_kind": "persistent",
    "role": "integration",
    "environment": "staging",
    "instance_id": "STAGE1",
    "workspace_id": 22,
    "app_ids": {"employee": 11},
    "select_runner": select_callback,
    "flow_runner": flow_callback,
}
report = verify_candidate_apps(
    {"commit": "abc123", "apps": {"employee": {"app_id": 11}}},
    target,
    {"employee": declaration()},
)
self.assertEqual(report.target_identity["instance_id"], "STAGE1")
~~~

Keep the existing PASS/FAIL/UNKNOWN and coverage assertions. Add refusals for target_kind != "persistent", production, unsupported roles, missing app bindings, missing verify SQL, and absent named flow adapters.

- [ ] **Step 2: Run the focused tests and verify the new import fails**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -v

Expected: FAIL with ModuleNotFoundError for teamlib.qualification or missing adapter symbols.

- [ ] **Step 3: Generalize AppCheckReport and target validation**

In scripts/teamlib/app_checks.py:

~~~python
@dataclass(frozen=True)
class AppCheckReport:
    source_commit: str
    target_identity: Mapping[str, Any]
    results: tuple[CheckResult, ...]
    source_digest: str
    checks_digest: str
    coverage: Mapping[str, Any]
    status: str

def _target_details(target: Any) -> tuple[str, dict[str, Any]]:
    value = _mapping(target, "qualification target")
    if value.get("target_kind") != "persistent":
        raise AppCheckError("candidate checks require a persistent qualification target")
    if value.get("environment") == "production":
        raise AppCheckError("candidate checks refuse production")
    if value.get("role") not in {"integration", "test"}:
        raise AppCheckError("candidate checks require integration or test role")
    instance = value.get("instance_id")
    if not isinstance(instance, str) or not instance:
        raise AppCheckError("qualification target identity is incomplete")
    identity = {
        key: value[key]
        for key in ("target_kind", "role", "environment", "instance_id", "workspace_id", "app_ids")
        if key in value
    }
    return instance, identity
~~~

Update as_dict() to emit target_identity, update variable names inside verify_candidate_apps, and keep all declaration and coverage validation unchanged.

- [ ] **Step 4: Move the four adapters into qualification.py**

Copy the behavior of _check_declarations, _select_runner, _require_flow_adapter, and _flow_runner from scripts/ci_replay_runner.py into the public signatures listed above. Raise AppCheckError instead of SystemExit. Preserve TEAM_ASSERT parsing, VERIFY-profile read mode, safe subprocess argv, and FAIL-not-UNKNOWN behavior for unavailable members/adapters.

- [ ] **Step 5: Run the focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_app_checks -v

Expected: PASS, including adapter failure diagnostics naming the alias/check.

- [ ] **Step 6: Commit**

~~~bash
git add scripts/teamlib/app_checks.py scripts/teamlib/qualification.py scripts/tests/test_app_checks.py
git commit -m "Extract persistent qualification app-check adapters"
~~~

### Task 2: Build canonical version-2 qualification evidence

**Files:**
- Modify: scripts/teamlib/qualification.py
- Create: scripts/tests/test_qualification.py

**Interfaces:**
- Consumes: declaration_paths, select_runner, require_flow_adapter, flow_runner; Config, Target, profile_target; SqlMigrationStore.read_history/read_state/validate_observation_chain; verify_release; verify_candidate_apps.
- Produces:
  - QualificationError(message: str, report: Mapping[str, Any] | None = None)
  - canonical_json(value: Mapping[str, Any]) -> bytes
  - qualify_target(repo: Path, config: Config, source_commit: str, aliases: Sequence[str], *, store: Any, work: Path, release_archive: Path | None = None, apply_report: Path | None = None, flow_executable: str | None = None, sql_runner=run_sqlcl, run_identity: Mapping[str, Any] | None = None, runner_contract: Path | None = None) -> dict[str, Any]
  - write_report(report: Mapping[str, Any], out: Path) -> None

- [ ] **Step 1: Add a complete passing evidence test**

Create scripts/tests/test_qualification.py with a fake Config, fake migration store, fake SQLcl assertions, one select-only declaration, and assertions for this exact top-level shape:

~~~python
self.assertEqual(report["version"], 2)
self.assertEqual(report["final_status"], "PASS")
self.assertEqual(report["source_commit"], "a" * 40)
self.assertNotIn("qualification_sha", report)
self.assertNotIn("replay_identity", report)
self.assertEqual(report["qualification_identity"]["target_kind"], "persistent")
self.assertEqual(report["qualification_identity"]["observation_sequence"], 3)
self.assertEqual(report["qualification_identity"]["observation_digest"], "d" * 64)
self.assertRegex(report["qualification_identity"]["history_digest"], r"^[0-9a-f]{64}$")
self.assertEqual(
    report["results"],
    {
        "migrations": "PASS",
        "application_deploy": "PASS",
        "application_checks": "PASS",
    },
)
~~~

The fake store must return collapsed history and a continuous observation list whose last row has sequence 3 and evidence d * 64.

- [ ] **Step 2: Add fail-closed evidence tests**

In the same file, add tests that assert QualificationError for:

~~~python
cases = (
    ("production", "production"),
    ("developer", "development"),
    ("missing observation", []),
    ("unresolved attempt", {"a": {"state": "UNKNOWN"}}),
    ("source alias mismatch", ("unknown-app",)),
)
~~~

Also assert release_archive/apply_report must be both absent or both present, archive source_commit must match, apply-report archive_digest must match, apply-report status must be applied, and apply-report source_commit must match.

- [ ] **Step 3: Run the new test and verify it fails**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_qualification -v

Expected: FAIL because QualificationError and qualify_target do not exist.

- [ ] **Step 4: Implement canonical identity and report assembly**

Use compact sorted UTF-8 JSON for all digests:

~~~python
def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
~~~

Validate integration/test and non-production before any read. Require aliases to be unique safe names and exactly match selected config.apps. Probe TABLES, CODE, METADATA, VERIFY, and each bound APEX target with scripts/sql/identity.sql using operation "read". Build target_identity only from project, role, environment, instance_id, db_name, service, workspace_id, app_ids, state_key, and binding digests; never include credentials or secret-like environment values.

- [ ] **Step 5: Bind evidence to collapsed history and observation state**

Call store.validate_observation_chain(metadata), then read history/state. Refuse any attempt in RUNNING, UNKNOWN, or FAILED. Require a latest observation. Calculate history_digest from the collapsed history and emit:

~~~python
"qualification_identity": {
    "target_kind": "persistent",
    "observation_sequence": latest["sequence"],
    "observation_digest": latest["evidence"],
    "history_digest": _digest(history),
}
~~~

Use declaration_paths and verify_candidate_apps for application checks. Set application_deploy PASS only after all configured app bindings were probed and all declared app checks passed.

Normalize the nested evidence instead of copying AppCheckReport.as_dict() verbatim:

~~~python
"application_checks": {
    "status": app_report.status,
    "checks_digest": app_report.checks_digest,
    "coverage": dict(app_report.coverage),
    "unknown": int(app_report.coverage.get("unknown", 0)),
    "results": [item.as_dict() for item in app_report.results],
}
~~~

- [ ] **Step 6: Validate release mode and assemble results**

When archive/report are present, call verify_release and load a closed canonical apply-report object. Require version 1, status applied, identical archive_digest/source_commit/target_state_key, and no unknown keys. Staging evidence omits archive_digest; release-test evidence includes it. Both use version 2 and the same qualification_identity/application_checks/results shape.

Load runner-contract.json as a closed non-secret mapping, pass it through ci_doctor, and calculate toolchain_digest from canonical_json({"toolchain": contract["toolchain"], "profiles": contract["profiles"]}). Refuse an invalid doctor report rather than signing an unqualified toolchain declaration.

On a check failure, raise QualificationError with a complete report whose final_status is FAIL and whose application_checks retains coverage and diagnostics.

- [ ] **Step 7: Implement atomic report writing**

write_report must reject symlink destinations, create the parent, write to a same-directory temporary file with LF, fsync, and os.replace. Its bytes are canonical_json(report) + b"\n".

- [ ] **Step 8: Run the qualification tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_qualification tests.test_app_checks -v

Expected: PASS.

- [ ] **Step 9: Commit**

~~~bash
git add scripts/teamlib/qualification.py scripts/tests/test_qualification.py
git commit -m "Add persistent target qualification evidence"
~~~

### Task 3: Expose qualify-target through the online CLI

**Files:**
- Modify: scripts/team.py:28-49, 53-126, 271-570
- Modify: scripts/tests/test_integration_workflow.py
- Modify: scripts/tests/test_launchers.py

**Interfaces:**
- Consumes: qualify_target and write_report from Task 2.
- Produces: team.py --env FILE qualify-target --source-commit SHA --aliases a,b --out FILE [--release-archive TAR --apply-report JSON].

- [ ] **Step 1: Add parser and production-refusal tests**

Add:

~~~python
parsed = team._parser().parse_args([
    "qualify-target",
    "--source-commit", "a" * 40,
    "--aliases", "employee,admin",
    "--out", "qualification.json",
])
self.assertEqual(parsed.command, "qualify-target")
self.assertEqual(parsed.aliases, "employee,admin")
self.assertIn("qualify-target", team.PRODUCTION_REFUSED_COMMANDS)
~~~

Add a dispatch test that patches team.qualify_target and confirms the parsed archive/apply-report pair reaches it without loading an offline handler.

- [ ] **Step 2: Run the focused tests and verify parser failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow tests.test_launchers -v

Expected: FAIL because qualify-target is not a known command.

- [ ] **Step 3: Add parser arguments and imports**

Add qualify-target to the online parser with required source-commit, aliases, and out arguments plus the optional release pair. Add it to PRODUCTION_REFUSED_COMMANDS. Import QualificationError, qualify_target, and write_report.

- [ ] **Step 4: Implement online dispatch**

The branch must:

~~~python
config = _config(args, require_verify=True)
if config.environment == "production":
    raise ConfigError("qualify-target is refused for production targets")
if bool(args.release_archive) != bool(args.apply_report):
    raise ConfigError("--release-archive and --apply-report must be supplied together")
if _resolved_commit(repo, "HEAD") != args.source_commit:
    raise ConfigError("qualification checkout is not the exact source commit")
aliases = tuple(part.strip() for part in args.aliases.split(",") if part.strip())
metadata = profile_target(config, "METADATA")
store = _sql_migration_store(repo, metadata)
~~~

Call qualify_target with work under scratch/qualification, TEAM_FLOW_RUNNER, GitHub run identifiers from GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT/GITHUB_WORKFLOW/GITHUB_SHA, and ci/runner-contract.json. Write PASS or carried FAIL report to --out, print a compact status object, then surface QualificationError for exit 3.

- [ ] **Step 5: Run the focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow tests.test_launchers tests.test_qualification -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add scripts/team.py scripts/tests/test_integration_workflow.py scripts/tests/test_launchers.py
git commit -m "Expose persistent target qualification"
~~~

### Task 4: Make apply-release write an identity-bound canonical report

**Files:**
- Modify: scripts/teamlib/release.py:44-60, 474-581
- Modify: scripts/teamlib/release_adapter.py:69-235
- Modify: scripts/tests/test_release.py
- Modify: scripts/tests/test_release_adapter.py
- Modify: scripts/tests/test_launchers.py

**Interfaces:**
- Consumes: existing ReleasePlan and verified target/history digests.
- Produces:
  - ApplyReport(version: int, status: str, source_commit: str, archive_digest: str, target_state_key: str, target_digest: str, history_digest: str, pending: tuple[str, ...])
  - ApplyReport.as_dict() -> dict[str, Any]
  - apply-release --out PATH.

- [ ] **Step 1: Write apply-report contract tests**

Add assertions:

~~~python
report = apply_release(...)
self.assertEqual(report.version, 1)
self.assertEqual(report.status, "applied")
self.assertEqual(report.source_commit, manifest.source_commit)
self.assertEqual(report.archive_digest, manifest.archive_digest)
self.assertEqual(report.target_digest, plan.target_digest)
self.assertEqual(report.history_digest, plan.history_digest)
self.assertEqual(report.pending, plan.pending)
~~~

In release-adapter CLI tests, pass --out report.json and assert the file equals report.as_dict() encoded with sorted compact JSON plus LF.

- [ ] **Step 2: Run focused tests and verify the dataclass mismatch**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release tests.test_release_adapter tests.test_launchers -v

Expected: FAIL because ApplyReport lacks the new fields and --out.

- [ ] **Step 3: Expand ApplyReport and preserve reviewed identity**

Implement as_dict and populate it only after verified adapters complete. Compute target_state_key from the METADATA Target in apply_verified_release; pass it to apply_release as a required keyword. Do not derive it from mutable CLI text.

- [ ] **Step 4: Add --out and canonical writing**

release_adapter.main must require --out for the online applied path, reject symlink outputs, and write compact sorted JSON plus LF atomically. It may still print the same report to stdout for operators.

- [ ] **Step 5: Update release launcher tests**

Update shell/PowerShell forwarding fixtures to include --out scratch/apply-report.json and assert both launchers forward the literal path.

- [ ] **Step 6: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release tests.test_release_adapter tests.test_launchers -v

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add scripts/teamlib/release.py scripts/teamlib/release_adapter.py scripts/tests/test_release.py scripts/tests/test_release_adapter.py scripts/tests/test_launchers.py
git commit -m "Emit canonical release apply reports"
~~~

### Task 5: Add evidence signing and enforce version 2 in the runbook

**Files:**
- Modify: scripts/teamlib/qualification.py
- Modify: scripts/teamlib/runbook.py:41-121, 225-286
- Modify: scripts/teamlib/config.py:53-71
- Modify: scripts/team.py:574-613
- Modify: scripts/tests/test_qualification.py
- Modify: scripts/tests/test_runbook.py
- Modify: scripts/tests/test_launchers.py

**Interfaces:**
- Consumes: canonical_json and version-2 qualification reports.
- Produces:
  - sign_test_evidence(evidence: Path, private_key: Path, signature_out: Path) -> str, returning the evidence SHA-256.
  - offline command sign-test-evidence --evidence FILE --private-key FILE --out FILE.
  - gen_runbook accepts version 2 only.

- [ ] **Step 1: Add signing refusal and success tests**

Generate an Ed25519 key in the test, write canonical PASS evidence, call sign_test_evidence, and verify the detached 64-byte signature over the exact evidence bytes. Add refusals for FAIL evidence, version 1, non-canonical JSON, a non-Ed25519 key, symlink inputs/outputs, and extra top-level keys.

- [ ] **Step 2: Replace runbook fixtures with version-2 evidence**

Use this required shape:

~~~python
evidence_data = {
    "version": 2,
    "final_status": "PASS",
    "source_commit": commit,
    "archive_digest": manifest.archive_digest,
    "toolchain_digest": "b" * 64,
    "target_identity": {"instance_id": "TEST", "workspace_id": 1},
    "run_identity": {"run_id": "1", "run_attempt": "1"},
    "qualification_identity": {
        "target_kind": "persistent",
        "observation_sequence": 2,
        "observation_digest": "d" * 64,
        "history_digest": "e" * 64,
    },
    "application_checks": {
        "status": "PASS",
        "checks_digest": "c" * 64,
        "coverage": {"apps": ["a"], "checks": 2},
        "unknown": 0,
    },
    "results": {
        "migrations": "PASS",
        "application_deploy": "PASS",
        "application_checks": "PASS",
    },
}
~~~

Add an explicit test that a correctly signed version-1 document is rejected.

- [ ] **Step 3: Run tests and verify failures**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_qualification tests.test_runbook tests.test_launchers -v

Expected: FAIL because signing is absent and runbook still requires version 1/replay fields.

- [ ] **Step 4: Implement exact-byte signing**

Load evidence with _read_file semantics, require raw == canonical_json(parsed) + b"\n", validate the complete closed version-2 PASS schema, load a PEM Ed25519 private key via cryptography, sign raw bytes, and atomically write only the 64 signature bytes. Never log key bytes or copy the key.

- [ ] **Step 5: Route the offline signing command**

Add sign-test-evidence to OFFLINE_COMMANDS and team._offline module routing. qualification.main must expose only the signing subcommand in offline mode; qualify-target remains online because it loads database profiles.

Add sign-test-evidence to command_prefixed so qualification.main receives the subcommand token before its options.

- [ ] **Step 6: Replace runbook version checks**

Delete qualification_sha and replay_identity handling. Require exactly the v2 fields shown above, validate all SHA-256 fields, require target_kind persistent and unknown == 0, require exactly the three result keys, and compare source_commit/archive_digest to the verified release. Verify the detached signature before generating a production runbook.

- [ ] **Step 7: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_qualification tests.test_runbook tests.test_launchers -v

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add scripts/teamlib/qualification.py scripts/teamlib/runbook.py scripts/teamlib/config.py scripts/team.py scripts/tests/test_qualification.py scripts/tests/test_runbook.py scripts/tests/test_launchers.py
git commit -m "Sign and verify version two qualification evidence"
~~~

### Task 6: Remove disposable replay and simplify ci-doctor

**Files:**
- Delete: ci/provisioners/docker_pdb.sh
- Delete: scripts/ci_replay_runner.py
- Delete: scripts/tests/live/test_docker_qualification.py
- Modify: scripts/teamlib/ci.py:1-349
- Modify: scripts/teamlib/config.py:53-71
- Modify: scripts/team.py:574-613
- Modify: ci/runner-contract.json
- Modify: .github/workflows/database-checks.yml
- Modify: scripts/tests/test_ci_contract.py
- Modify: scripts/tests/test_release_workflow.py

**Interfaces:**
- Consumes: DoctorReport and ci_doctor.
- Produces: ci_doctor validates only contract version, Python/SQLcl/JDK/APEX/database declarations, all five profile names, production credentials/writes false, and absence of secret-like populated fields.

- [ ] **Step 1: Rewrite CI contract tests around the retained behavior**

Delete ci_replay tests. Keep/add:

~~~python
report = ci_doctor({
    "version": 1,
    "toolchain": {
        "python": "3.10+",
        "sqlcl": "26.2.1+",
        "jdk": "17+",
        "apex": "26.1+",
        "database": "23ai+",
        "cryptography": "Ed25519-qualified",
    },
    "profiles": ["TABLES", "CODE", "APEX", "METADATA", "VERIFY"],
    "production": {"credentials": False, "writes": False},
})
self.assertTrue(report.valid, report.issues)
self.assertNotIn("provisioner", report.capabilities)
self.assertNotIn("runner", report.capabilities)
~~~

Add failures for a missing toolchain/profile, true production credentials/writes, and populated secret-like keys.

- [ ] **Step 2: Change workflow contract tests**

Assert database-checks.yml has exactly one job named offline and contains unittest plus ci-doctor. Assert it lacks ci-replay, Docker, Oracle images, SQLcl PATH probing, and upload-artifact.

- [ ] **Step 3: Run focused tests and verify old assumptions fail**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_ci_contract tests.test_release_workflow -v

Expected: FAIL because disposable fields/jobs still exist.

- [ ] **Step 4: Delete replay orchestration**

Remove ReplayReport, _invoke_provisioner, _validate_created, ci_replay, and the replay parser branch from ci.py. Remove ci-replay from OFFLINE_COMMANDS and team._offline. Delete the provisioner, replay runner, and disposable live test.

- [ ] **Step 5: Simplify runner-contract.json and ci_doctor**

The tracked contract becomes:

~~~json
{
  "version": 1,
  "toolchain": {
    "python": "3.10+",
    "sqlcl": "26.2.1+",
    "jdk": "17+",
    "apex": "26.1+",
    "database": "23ai+",
    "cryptography": "Ed25519-qualified"
  },
  "profiles": ["TABLES", "CODE", "APEX", "METADATA", "VERIFY"],
  "production": {
    "credentials": false,
    "writes": false
  }
}
~~~

Return capabilities containing only toolchain, sorted profiles, and the two false production booleans.

- [ ] **Step 6: Collapse database-checks.yml**

Keep push/pull_request, contents: read, checkout exact github.sha, Python 3.10, cryptography install for runbook tests, unittest discovery, and ci-doctor. Delete the replay job completely.

- [ ] **Step 7: Run focused tests and absence scan**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_ci_contract tests.test_release_workflow -v

Run: test ! -e ci/provisioners/docker_pdb.sh && test ! -e scripts/ci_replay_runner.py && test ! -e scripts/tests/live/test_docker_qualification.py

Run: ! rg -n "ci-replay|docker_pdb|disposable replay" scripts ci .github/workflows/database-checks.yml

Expected: all commands succeed.

- [ ] **Step 8: Commit**

~~~bash
git add -A ci/provisioners/docker_pdb.sh scripts/ci_replay_runner.py scripts/tests/live/test_docker_qualification.py scripts/teamlib/ci.py scripts/teamlib/config.py scripts/team.py ci/runner-contract.json .github/workflows/database-checks.yml scripts/tests/test_ci_contract.py scripts/tests/test_release_workflow.py
git commit -m "Remove disposable replay from default CI"
~~~

### Task 7: Convert integration into one manual serial qualification job

**Files:**
- Modify: .github/workflows/integration.yml
- Modify: scripts/tests/test_integration_workflow.py
- Modify: scripts/tests/test_release_workflow.py

**Interfaces:**
- Consumes: qualify-target CLI.
- Produces: workflow_dispatch-only integration job using the protected integration environment and qualification report artifact.

- [ ] **Step 1: Write the workflow contract test**

Assert:

~~~python
self.assertIn("workflow_dispatch:", workflow)
self.assertNotIn("push:", workflow)
self.assertEqual(workflow.count("runs-on:"), 1)
self.assertIn("environment: integration", workflow)
self.assertIn("cancel-in-progress: false", workflow)
self.assertIn("setup-state", workflow)
self.assertIn("adopt-frontier", workflow)
self.assertIn("check-drift", workflow)
self.assertIn("migrate", workflow)
self.assertIn("deploy-app", workflow)
self.assertIn("qualify-target", workflow)
self.assertNotIn("--destructive-confirmation", workflow)
~~~

Also assert the command ordering matches the list above and the qualification artifact is uploaded with if: always().

- [ ] **Step 2: Run the tests and verify the current push/polling structure fails**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow tests.test_release_workflow -v

Expected: FAIL because integration is push-triggered and has two jobs.

- [ ] **Step 3: Rewrite integration.yml**

Use workflow_dispatch, contents: read, the existing concurrency group, cancel-in-progress false, and one job with environment integration. Retain exact github.sha checkout and protected TEAM_ENV_FILE/TEAM_ENV_CONTENT/TEAM_APP_ALIASES handling.

- [ ] **Step 4: Add the qualification step**

After deploy, run:

~~~bash
PYTHONPATH=scripts python3 scripts/team.py --env "$TEAM_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" \
  --aliases "$TEAM_APP_ALIASES" \
  --out "$RUNNER_TEMP/qualification.json"
~~~

Upload that file with actions/upload-artifact@v4, if: always(), and if-no-files-found: error. The workflow must not supply destructive confirmation.

- [ ] **Step 5: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_integration_workflow tests.test_release_workflow -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add .github/workflows/integration.yml scripts/tests/test_integration_workflow.py scripts/tests/test_release_workflow.py
git commit -m "Make persistent integration qualification manual"
~~~

### Task 8: Qualify and sign the applied release in the protected test job

**Files:**
- Modify: .github/workflows/release.yml
- Modify: scripts/tests/test_release_workflow.py
- Modify: scripts/tests/test_docs.py

**Interfaces:**
- Consumes: canonical apply report, qualify-target, sign-test-evidence, gen-runbook.
- Produces: exact archive -> test apply -> qualification v2 -> detached signature -> production runbook chain.

- [ ] **Step 1: Write release workflow ordering and secret-handling tests**

Assert release.yml contains, in order, apply_release.sh, qualify-target, sign-test-evidence, and gen-runbook. Assert it uses TEAM_TEST_SIGNING_KEY_CONTENT as a secret, writes it below RUNNER_TEMP, chmod 600, removes it in if: always(), uploads unsigned diagnostics on failure, and no longer consumes pre-supplied TEAM_TEST_EVIDENCE/TEAM_TEST_SIGNATURE paths.

- [ ] **Step 2: Run workflow tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release_workflow tests.test_docs -v

Expected: FAIL because release.yml expects externally prepared evidence/signature.

- [ ] **Step 3: Capture the apply report**

Pass --out "$RUNNER_TEMP/apply-report.json" through scripts/apply_release.sh. Keep release.tar and plan verification unchanged.

- [ ] **Step 4: Generate version-2 evidence**

Use protected TEAM_TEST_ENV_FILE and TEAM_APP_ALIASES:

~~~bash
PYTHONPATH=scripts python3 scripts/team.py --env "$TEAM_TEST_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" \
  --aliases "$TEAM_APP_ALIASES" \
  --release-archive scratch/release/release.tar \
  --apply-report "$RUNNER_TEMP/apply-report.json" \
  --out "$RUNNER_TEMP/test-evidence.json"
~~~

- [ ] **Step 5: Materialize, use, and remove the signing key**

Write secrets.TEAM_TEST_SIGNING_KEY_CONTENT to "$RUNNER_TEMP/test-signing-key.pem" with umask 077 and chmod 600. Run sign-test-evidence to "$RUNNER_TEMP/test-evidence.sig". Add an if: always() cleanup step using rm -f on that exact file only. Missing key/trust configuration must prevent gen-runbook but leave apply-report/evidence diagnostics uploadable.

- [ ] **Step 6: Generate and retain the handoff**

Pass the newly generated evidence/signature plus the independently configured public trust key to gen-runbook. Upload apply-report.json, test-evidence.json, test-evidence.sig when present, and PRODUCTION_RUNBOOK.md when present. The handoff artifact must still fail if the runbook is missing.

- [ ] **Step 7: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release_workflow tests.test_docs tests.test_runbook tests.test_qualification -v

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add .github/workflows/release.yml scripts/tests/test_release_workflow.py scripts/tests/test_docs.py
git commit -m "Qualify and sign protected test releases"
~~~

### Task 9: Document persistent qualification and close Workstream A

**Files:**
- Modify: README.md
- Modify: docs/ci.md
- Modify: ci/app-checks/README.md
- Modify: docs/promotion.md
- Modify: scripts/tests/test_docs.py

**Interfaces:**
- Consumes: all Workstream A commands and workflows.
- Produces: operator documentation matching the shipped CLI and explicitly stating the removed guarantee.

- [ ] **Step 1: Add documentation assertions**

Require the docs to contain qualify-target, sign-test-evidence, target_kind persistent, version 2, TEAM_FLOW_RUNNER, manual workflow_dispatch, and the statement that persistent staging does not prove fresh installation. Require removal of ci-replay, disposable Oracle, fresh result, upgrade result, qualification_sha, and replay_identity.

- [ ] **Step 2: Run doc tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_docs -v

Expected: FAIL until the old replay text is removed.

- [ ] **Step 3: Rewrite CI and app-check documentation**

Document the offline default job; protected integration/test environment variables; required SQLcl and five profiles; manual exact-SHA qualification; declaration coverage; SELECT checks through VERIFY; TEAM_FLOW_RUNNER argv/result schema; artifact paths; and failure-report behavior.

- [ ] **Step 4: Rewrite promotion documentation**

Document evidence v2 fields, one source_commit, canonical LF JSON, protected Ed25519 private key, independently held public trust key, detached signature, version-1 refusal, and unsigned staging evidence rejection.

- [ ] **Step 5: Update README command examples**

Include exact staging and release-test qualify-target invocations and sign-test-evidence. State production writes remain refused and persistent evidence is observational.

- [ ] **Step 6: Run Workstream A verification**

Run: PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v

Run: python3 -m ruff check scripts/

Run: git diff --check

Expected: all tests and lint pass; no whitespace errors.

- [ ] **Step 7: Commit**

~~~bash
git add README.md docs/ci.md ci/app-checks/README.md docs/promotion.md scripts/tests/test_docs.py
git commit -m "Document persistent qualification and signed evidence"
~~~

---

# Workstream B — Migration undo, redo, and destructive confirmation

### Task 10: Extend immutable bundles with an optional authored down pair

**Files:**
- Modify: scripts/teamlib/migration_bundle.py:21-244
- Modify: scripts/tests/test_migration_bundle.py
- Modify: scripts/tests/test_authoring.py

**Interfaces:**
- Consumes: _assert_controls and _validate_verify.
- Produces Migration fields:
  - down_sql_path: Path | None
  - down_verify_path: Path | None
  - down_sql_bytes: bytes
  - down_verify_bytes: bytes
  - down_destructive: bool
  - reversible property returning bool.

- [ ] **Step 1: Add reversible bundle tests**

Extend the fixture writer with optional down_sql/down_verify. Assert a complete down pair loads, reversible is true, down_destructive is independent of destructive, and modifying either down member changes the bundle checksum.

- [ ] **Step 2: Add malformed down-pair tests**

Assert BundleError for only one down member, symlinks, invalid UTF-8, CR line endings, nested SQLcl commands, down verification writes, target/depends-on in the down header, duplicate migration-version/destructive, and unexpected migration siblings.

Use a valid down header:

~~~sql
-- migration-version: 1
-- destructive: true

DROP TABLE T_X;
~~~

- [ ] **Step 3: Run focused tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_bundle tests.test_authoring -v

Expected: FAIL because down members are currently rejected as unexpected siblings.

- [ ] **Step 4: Add down filename recognition and dataclass fields**

Add anchored regexes for .down.sql and .down.verify.sql. Track all four filenames per migration ID. The optional down pair must be both present or both absent before reading bytes.

- [ ] **Step 5: Add a direction-specific header parser**

Keep _parse_header for forward members. Add:

~~~python
def _parse_down_header(text: str, migration_id: str) -> tuple[int, bool]:
    # Allowed exactly once: migration-version, destructive.
    # Forbidden: target, depends-on, every unknown directive.
~~~

Require version 1 and true/false destructive. Run _assert_controls on down SQL and _validate_verify on down verification.

- [ ] **Step 6: Extend the checksum**

Build the canonical member list in exact order: forward SQL, forward verification, down SQL, down verification. Include only present down members; each entry contains path, byte length, and SHA-256. This makes adding a down pair after apply a checksum conflict.

- [ ] **Step 7: Preserve authoring behavior**

Keep new-migration creating only forward SQL and forward verification. Add a test that it does not create down members and documentation-facing output does not claim reversibility.

- [ ] **Step 8: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_bundle tests.test_authoring -v

Expected: PASS.

- [ ] **Step 9: Commit**

~~~bash
git add scripts/teamlib/migration_bundle.py scripts/tests/test_migration_bundle.py scripts/tests/test_authoring.py
git commit -m "Support authored migration down pairs"
~~~

### Task 11: Make planning understand APPLIED and REVERTED lifecycle state

**Files:**
- Modify: scripts/teamlib/migration_plan.py:13-114
- Modify: scripts/tests/test_migration_plan.py

**Interfaces:**
- Consumes: Migration.reversible and collapsed history entries.
- Produces:
  - Plan gains foreign_reverted: tuple[str, ...]
  - plan_undo(bundles: Mapping[str, Migration], history: Mapping[str, Any], migration_id: str) -> str
  - plan_redo(bundles: Mapping[str, Migration], history: Mapping[str, Any], migration_id: str) -> str.

- [ ] **Step 1: Add forward-plan lifecycle tests**

Assert REVERTED local migrations are excluded from pending, a pending migration depending on REVERTED blocks, an APPLIED migration whose dependency is REVERTED blocks, shared mode reports foreign APPLIED and REVERTED separately, and strict mode rejects both.

- [ ] **Step 2: Add undo planner tests**

Build collapsed histories with sequence values and assert only the greatest-sequence APPLIED migration can be undone, the top changes after a later down event, a non-reversible top refuses, and there is no force path.

- [ ] **Step 3: Add redo planner tests**

Assert the requested migration must be REVERTED, must have matching checksum and a complete down pair, and all dependencies must currently be APPLIED at their declared checksums. Assert two independent reverted migrations may be redone in either requested order.

- [ ] **Step 4: Run focused tests and verify failures**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_plan -v

Expected: FAIL because REVERTED and the two planners are unsupported.

- [ ] **Step 5: Implement status validation and dependency state**

Recognize only empty, APPLIED, REVERTED, RUNNING, UNKNOWN, and FAILED. For local APPLIED/REVERTED entries, require matching bundle checksum. A dependency is satisfied only by current APPLIED with the declared checksum or by a never-applied local migration ordered earlier in the same forward plan.

- [ ] **Step 6: Implement plan_undo and plan_redo**

Both functions return the selected migration ID and raise BundleError with deterministic diagnostics. plan_undo computes the actual top from current APPLIED entries sorted by sequence; plan_redo checks only dependency validity, not global stack position.

- [ ] **Step 7: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_plan -v

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add scripts/teamlib/migration_plan.py scripts/tests/test_migration_plan.py
git commit -m "Plan applied and reverted migration states"
~~~

### Task 12: Implement the one closed destructive-confirmation contract

**Files:**
- Create: scripts/teamlib/destructive_confirmation.py
- Create: scripts/tests/test_destructive_confirmation.py

**Interfaces:**
- Produces:
  - ConfirmationRequirement(migration_id: str, action: str, bundle_checksum: str, payload_target_state_key: str)
  - load_confirmation(path: str | Path) -> tuple[dict[str, Any], str], returning validated data and SHA-256 of canonical bytes.
  - confirmation_template(requirements: Sequence[ConfirmationRequirement]) -> dict[str, Any].
  - require_confirmations(requirements: Sequence[ConfirmationRequirement], document: Mapping[str, Any] | None) -> str, returning canonical digest or "" when no requirements.

- [ ] **Step 1: Write exact-match tests**

Use two requirements and assert a canonical version-1 document with one confirmed entry per requirement returns the SHA-256 of canonical compact JSON plus LF. Assert confirmation_template emits the same entries with confirmed false in deterministic migration/action order.

- [ ] **Step 2: Write closed-schema refusal tests**

Assert refusal for:

~~~python
invalid = (
    True,
    {"version": 2, "confirmations": []},
    {"version": 1, "confirmations": [], "extra": 1},
    {"version": 1, "confirmations": [valid_entry, valid_entry]},
)
~~~

Also cover missing/extra entries, confirmed false, wrong action, migration ID, checksum, target state key, malformed digests, symlinks, invalid UTF-8/JSON, non-canonical bytes, and unexpected entry keys.

- [ ] **Step 3: Run tests and verify the missing module**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_destructive_confirmation -v

Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 4: Implement dataclass and canonical loader**

Allowed document keys are exactly version and confirmations. Allowed entry keys are exactly migration_id, action, bundle_checksum, payload_target_state_key, confirmed. Actions are migrate/undo/redo. confirmed must be the bool True for require_confirmations; integers such as 1 do not qualify.

- [ ] **Step 5: Implement set equality matching**

Convert requirements and entries to immutable four-field keys, reject duplicates before comparing sets, and produce diagnostics listing missing and extra keys. Return the digest only after exact equality. When requirements is empty, reject a supplied document with entries and return "".

- [ ] **Step 6: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_destructive_confirmation -v

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add scripts/teamlib/destructive_confirmation.py scripts/tests/test_destructive_confirmation.py
git commit -m "Define exact destructive migration confirmation"
~~~

### Task 13: Upgrade the JSON migration store to a v2 event ledger

**Files:**
- Modify: scripts/teamlib/migration_store.py:879-1171
- Modify: scripts/tests/test_migration_store.py
- Modify: scripts/tests/test_migration_runner.py

**Interfaces:**
- Consumes: action values migrate/undo/redo and operation values up/down.
- Produces shared store signatures:
  - record_attempt_start(..., *, action: str, confirmation_digest: str = "") -> None
  - record_event(..., operation: str, ..., run_token: str, attempt_id: str) -> None
  - read_history(target) -> dict[str, Any] returns latest collapsed entry per migration ID.
  - read_state(target) attempts contain action and confirmation_digest.

- [ ] **Step 1: Add v2 initialization and migration tests**

Assert a new store writes version 2 with history as a list. Seed an exact v1 document, reopen it, and assert it atomically becomes v2, existing history becomes an up event, existing attempts gain action migrate and empty confirmation_digest, and the original bytes remain in a .v1-backup file until the v2 write succeeds.

- [ ] **Step 2: Add append/collapse tests**

Record A up, B up, B down. Assert raw event sequences are 1/2/3, read_history reports A APPLIED and B REVERTED, B has sequence 3 and operation down, and A is the current undo top. Add malformed sequence, duplicate sequence, invalid operation, and down-before-up refusals.

- [ ] **Step 3: Add attempt evidence tests**

Assert destructive attempts require a 64-hex confirmation_digest, non-destructive attempts store an empty digest, action must be migrate/undo/redo, and failed/unknown attempts retain both fields.

- [ ] **Step 4: Run focused tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_store tests.test_migration_runner -v

Expected: FAIL because the JSON store is version 1 and overwrites history by ID.

- [ ] **Step 5: Implement restart-safe JSON v1-to-v2 upgrade**

Use:

~~~python
{
    "version": 2,
    "meta": ...,
    "mutex": ...,
    "history": [event, ...],
    "attempts": {...},
    "inventories": {...},
    "observations": [...],
}
~~~

Validate the complete v1 value before writing a sibling backup. Use the existing fsync + os.replace writer. Never delete the backup automatically.

- [ ] **Step 6: Implement record_event and collapse**

Append only. operation up maps to status APPLIED; down maps to REVERTED. The event carries id/checksum/target/dependencies/source_commit/applied sequence/time/by applied_by/run_token/attempt_id/observation. read_history validates sequence order and collapses by replacing only with a later sequence.

- [ ] **Step 7: Expand attempt persistence**

Validate action and confirmation digest in record_attempt_start and include both in read_state/exported evidence. Keep recovery and unresolved-state behavior unchanged.

- [ ] **Step 8: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_store tests.test_migration_runner -v

Expected: PASS for store tests; runner tests may remain pending until Task 15 only where explicitly marked with expectedFailure. Do not commit unexpected failures.

- [ ] **Step 9: Commit**

~~~bash
git add scripts/teamlib/migration_store.py scripts/tests/test_migration_store.py scripts/tests/test_migration_runner.py
git commit -m "Store migration lifecycle as JSON events"
~~~

### Task 14: Upgrade Oracle metadata to the same v2 event model

**Files:**
- Modify: scripts/teamlib/migration_store.py:71-878
- Modify: scripts/sql/migration_metadata.sql
- Modify: scripts/tests/test_sql_metadata_store.py

**Interfaces:**
- Consumes: the shared Task 13 store signatures.
- Produces: restart-safe Oracle metadata v2 with history PK applied_sequence, history operation, index (id, applied_sequence), attempt action, and attempt confirmation_digest.

- [ ] **Step 1: Add fresh-schema DDL assertions**

Assert both embedded SQL and scripts/sql/migration_metadata.sql contain:

~~~sql
operation VARCHAR2(4) NOT NULL
CONSTRAINT team_migration_history_operation_ck CHECK (operation IN ('up','down'))
CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence)
CREATE INDEX team_migration_history_id_ix ON TEAM_MIGRATION_HISTORY (id, applied_sequence)
action VARCHAR2(16) NOT NULL
CONSTRAINT team_migration_attempt_action_ck CHECK (action IN ('migrate','undo','redo'))
confirmation_digest VARCHAR2(64)
~~~

Assert PRIMARY KEY (id) is absent.

- [ ] **Step 2: Add upgrade-shape tests**

Using the existing fake SQLcl runner, cover v1, fresh v2, and each partial shape after: operation column, operation constraint, PK replacement, index, action column, action constraint, and confirmation_digest. Assert bootstrap emits only the missing transition and updates TEAM_MIGRATION_META to version 2 last.

- [ ] **Step 3: Add refusal tests for ambiguous existing objects**

Assert bootstrap refuses a wrong column type/nullability, wrong check constraint, a same-named index on different columns, duplicate applied_sequence values, invalid operations, and any meta version other than 1 or 2.

- [ ] **Step 4: Add repeated-ID CLOB read tests**

Return scalar rows and CLOB chunks keyed by applied_sequence rather than ID. Include two events for one ID with multi-part dependency/observation CLOBs. Assert read_history reconstructs each event independently, validates sequence order, and collapses to the later REVERTED state.

- [ ] **Step 5: Run focused tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_sql_metadata_store -v

Expected: FAIL because current DDL is v1 and CLOB keys collide on ID.

- [ ] **Step 6: Implement the idempotent schema transition**

Replace create-if-missing-only bootstrap with dictionary checks against USER_TAB_COLUMNS, USER_CONSTRAINTS, USER_CONS_COLUMNS, and USER_INDEXES/USER_IND_COLUMNS. Backfill operation='up' and action='migrate' before adding NOT NULL/check constraints. Drop only the verified old TEAM_MIGRATION_HISTORY_PK on ID; refuse any unexpected definition.

Before the final metadata-version update, require exactly one meta row for the configured project. Change version_number from 1 to 2 with a guarded UPDATE; if a v2 row already exists, verify it is the sole matching row. Never leave parallel v1 and v2 rows.

- [ ] **Step 7: Update Oracle writes**

record_attempt_start inserts action and nullable confirmation_digest. record_event allocates MAX(applied_sequence)+1 while holding the mutex and inserts operation. Successful event writes update the linked attempt to APPLIED exactly once.

- [ ] **Step 8: Fix reads and exports**

Include applied_sequence in every scalar/CLOB row key. Reconstruct event CLOBs by (sequence, field), validate all scalar/checksum/operation values, collapse after full ordered validation, and include linked attempt evidence in exported state.

- [ ] **Step 9: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_sql_metadata_store tests.test_migration_store -v

Expected: PASS.

- [ ] **Step 10: Commit**

~~~bash
git add scripts/teamlib/migration_store.py scripts/sql/migration_metadata.sql scripts/tests/test_sql_metadata_store.py
git commit -m "Upgrade Oracle migration metadata to version two"
~~~

### Task 15: Route migrate, undo, and redo through one operation engine

**Files:**
- Modify: scripts/teamlib/migrate.py:1-211
- Modify: scripts/tests/test_migration_runner.py

**Interfaces:**
- Consumes: plan_migrations/plan_undo/plan_redo, confirmation requirements, and v2 store methods.
- Produces:
  - RunReport(run_token: str, action: str, selected: tuple[str, ...], applied: tuple[str, ...], reverted: tuple[str, ...], foreign_applied: tuple[str, ...], foreign_reverted: tuple[str, ...], blocked_attempt: str | None, verified_inventory_digest: str | None, confirmation_template: Mapping[str, Any] | None)
  - apply_plan(source: Path, profiles: Mapping[str, Any], *, expected_plan: Mapping[str, Any] | None = None, confirmation: Mapping[str, Any] | None = None) -> RunReport
  - apply_undo(source: Path, migration_id: str, profiles: Mapping[str, Any], *, confirmation: Mapping[str, Any] | None = None) -> RunReport
  - apply_redo(source: Path, migration_id: str, profiles: Mapping[str, Any], *, confirmation: Mapping[str, Any] | None = None) -> RunReport
  - private _apply_operation(action: str, selected_ids: tuple[str, ...], bundles: Mapping[str, Migration], profiles: Mapping[str, Any], confirmation: Mapping[str, Any] | None) -> RunReport.
  - profiles["payload_targets"] is a Mapping[str, Target] with exactly tables/code keys; profiles["execute"] has signature (migration: Migration, action: str, sql_path: Path) -> Any; profiles["verify"] has signature (migration: Migration, action: str, verify_path: Path | None) -> bool; profiles["observe"] retains (migration: Migration, phase: str) -> Any.

- [ ] **Step 1: Replace loose confirmation tests**

Add a destructive forward migration and assert confirmation=True and {"confirmed": True} both fail. Assert an exact Task 12 document passes and its digest appears on the attempt.

- [ ] **Step 2: Add undo/redo happy-path tests**

Apply A then B, undo B, undo A, redo A, redo B. Record which bytes execute and verify:

~~~python
self.assertEqual(executed, [
    ("migrate", a.sql_bytes),
    ("migrate", b.sql_bytes),
    ("undo", b.down_sql_bytes),
    ("undo", a.down_sql_bytes),
    ("redo", a.sql_bytes),
    ("redo", b.sql_bytes),
])
~~~

Assert forward verification runs for migrate/redo and down verification for undo.

- [ ] **Step 3: Add refusal and failure-state tests**

Cover non-top undo, missing down pair, reverted dependency, checksum mismatch, production target, stale confirmation, destructive undo/redo without confirmation, verification false, known execute failure -> FAILED, unknown transport outcome -> UNKNOWN, and held mutex after unresolved outcomes.

- [ ] **Step 4: Add dry-run tests**

Assert no bootstrap/acquire/execute occurs. The report must contain selected action/IDs and confirmation_template with confirmed false only for destructive selections.

- [ ] **Step 5: Run runner tests and verify failures**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_runner -v

Expected: FAIL because apply_undo/apply_redo and strict confirmation are absent.

- [ ] **Step 6: Extract _apply_operation**

The helper must execute this order: validate non-production and bundles; dry-run return; optional bootstrap; acquire; reread history/state and replan; validate frontier; build requirements from profile_target state keys; require exact confirmation; record attempt with action/digest; observe before; execute direction member; verify direction member; observe after; record inventories; record up/down event; release.

For each destructive selection, construct the requirement from profiles["payload_targets"][migration.target].state_key. Refuse absent/non-Target tables or code bindings before acquiring the mutex.

- [ ] **Step 7: Select directional members**

Use:

~~~python
if action == "undo":
    sql_path = migration.down_sql_path
    verify_path = migration.down_verify_path
    destructive = migration.down_destructive
    operation = "down"
else:
    sql_path = migration.sql_path
    verify_path = migration.verify_path
    destructive = migration.destructive
    operation = "up"
~~~

Redo uses action redo but operation up. Never mutate the Migration object to swap paths.

- [ ] **Step 8: Preserve failure classification**

Any failure before attempt start releases the mutex. A known failure after attempt start records FAILED and leaves the mutex held. A success-before-log/transport uncertainty records UNKNOWN and leaves it held. Successful verification/event recording marks APPLIED and permits release.

- [ ] **Step 9: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_runner tests.test_destructive_confirmation -v

Expected: PASS.

- [ ] **Step 10: Commit**

~~~bash
git add scripts/teamlib/migrate.py scripts/tests/test_migration_runner.py
git commit -m "Execute migration undo and redo safely"
~~~

### Task 16: Wire the three CLI commands to the shared confirmation file

**Files:**
- Modify: scripts/team.py:28-49, 53-126, 315-374
- Modify: scripts/tests/test_launchers.py
- Modify: scripts/tests/test_production_boundary.py
- Modify: scripts/tests/test_integration_workflow.py

**Interfaces:**
- Consumes: load_confirmation, apply_plan, apply_undo, apply_redo.
- Produces:
  - migrate --destructive-confirmation FILE
  - undo-migration ID [--dry-run] --destructive-confirmation FILE
  - redo-migration ID [--dry-run] --destructive-confirmation FILE.

- [ ] **Step 1: Add parser tests**

Assert all three commands accept the same option name. Assert undo/redo require the positional migration ID and support --source default migrations and --dry-run. Assert undo/redo and migrate are production-refused.

- [ ] **Step 2: Add dispatch tests**

Patch load_confirmation and each apply function. Assert the document, never its path or a boolean, reaches the operation. Assert no confirmation file is loaded when omitted. Assert dry-run JSON includes the generated false confirmation template.

- [ ] **Step 3: Run focused tests and verify parser failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_launchers tests.test_production_boundary tests.test_integration_workflow -v

Expected: FAIL because flags/commands are absent.

- [ ] **Step 4: Add parser/import/refusal wiring**

Add --destructive-confirmation to migrate and to a shared helper used when constructing undo/redo parsers. Add migrate, undo-migration, and redo-migration to PRODUCTION_REFUSED_COMMANDS.

- [ ] **Step 5: Generalize online migration callbacks**

Build execute(migration, action, path), verify(migration, action, path), and observe(migration, phase) once. Payload profile derives only from migration.target. VERIFY remains read-only. Use the same drift, schema-set, source-commit, applied-by, observation, and store profiles for all actions. Pass payload_targets={"tables": profile_target(config, "TABLES"), "code": profile_target(config, "CODE")} to every apply function.

- [ ] **Step 6: Dispatch action and output**

Load the confirmation JSON once if supplied. Call apply_plan/apply_undo/apply_redo. Emit status, operation, action, applied/reverted IDs, foreign states, blocked attempt, and dry-run confirmation template. Never print the confirmation file body.

- [ ] **Step 7: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_launchers tests.test_production_boundary tests.test_integration_workflow tests.test_migration_runner -v

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add scripts/team.py scripts/tests/test_launchers.py scripts/tests/test_production_boundary.py scripts/tests/test_integration_workflow.py
git commit -m "Expose migration undo redo and confirmation"
~~~

### Task 17: Package down members and apply lifecycle rules to releases

**Files:**
- Modify: scripts/teamlib/release.py:97-140, 160-372, 436-519
- Modify: scripts/teamlib/release_adapter.py:133-174
- Modify: scripts/tests/test_release.py
- Modify: scripts/tests/test_release_adapter.py
- Modify: scripts/tests/test_runbook.py

**Interfaces:**
- Consumes: four-member bundle loading and REVERTED-aware plan_migrations.
- Produces: release manifests/archive extraction preserve down pairs; plan_release excludes REVERTED artifacts and blocks their dependents.

- [ ] **Step 1: Add archive tests for down members**

Commit a reversible migration in the temporary release repo and assert release_migration_files returns all four members in deterministic order. Tamper with one down member and assert verify_release rejects the archive.

- [ ] **Step 2: Add release planning lifecycle tests**

Assert a matching REVERTED artifact migration is absent from pending; an independent never-applied migration remains pending; a dependent of REVERTED blocks; and strict release planning rejects foreign APPLIED or REVERTED history.

- [ ] **Step 3: Add release adapter test**

Materialize an archive containing down members and assert apply_verified_release leaves them intact for load_bundles while apply_plan executes only pending forward SQL.

- [ ] **Step 4: Run focused tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release tests.test_release_adapter tests.test_runbook -v

Expected: FAIL because release filtering knows only .sql/.verify.sql and planning treats REVERTED as unknown.

- [ ] **Step 5: Extend release path filters and manifest entries**

Allow exactly the four migration suffixes. Build each manifest migration from load_bundles so the checksum and reversible/down_destructive properties come from one parser. Keep archive member safety and deterministic USTAR behavior unchanged.

When release_adapter calls apply_plan, pass the same payload_targets mapping required by the online CLI:

~~~python
"payload_targets": {
    "tables": profile_target(config, "TABLES"),
    "code": profile_target(config, "CODE"),
},
~~~

- [ ] **Step 6: Reuse lifecycle planning**

Route _plan_from_manifest through the same REVERTED-aware rules as plan_migrations. Include foreign_reverted diagnostics. Do not add undo/redo to apply-release; release application remains forward-only.

- [ ] **Step 7: Run focused tests**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_release tests.test_release_adapter tests.test_runbook -v

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add scripts/teamlib/release.py scripts/teamlib/release_adapter.py scripts/tests/test_release.py scripts/tests/test_release_adapter.py scripts/tests/test_runbook.py
git commit -m "Preserve migration lifecycle in releases"
~~~

### Task 18: Document migration lifecycle and run the complete safety audit

**Files:**
- Modify: README.md
- Modify: docs/migrations.md
- Modify: docs/promotion.md
- Modify: scripts/tests/test_docs.py
- Modify: scripts/tests/test_release_workflow.py

**Interfaces:**
- Consumes: all Workstream B behavior.
- Produces: final documented operator contract and full-repository verification.

- [ ] **Step 1: Add lifecycle documentation assertions**

Require authored .down.sql/.down.verify.sql, global-LIFO undo, dependency-safe redo, REVERTED, applied_sequence event ledger, metadata v2, --destructive-confirmation, confirmation JSON fields, dry-run generation, non-production only, and APEX recovery boundary. Assert there is no force flag or generated rollback claim.

- [ ] **Step 2: Run doc tests and verify failure**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_docs -v

Expected: FAIL until docs match the new CLI.

- [ ] **Step 3: Write operator examples**

Include:

~~~bash
PYTHONPATH=scripts python3 scripts/team.py --env .env.development migrate \
  --source migrations --dry-run

PYTHONPATH=scripts python3 scripts/team.py --env .env.development undo-migration \
  20260910T120000__alice__example --source migrations --dry-run

PYTHONPATH=scripts python3 scripts/team.py --env .env.development redo-migration \
  20260910T120000__alice__example --source migrations --dry-run
~~~

Explain that operators copy the emitted template, independently review ID/action/checksum/target-state key, change confirmed to true, then pass the file without editing any identity field.

- [ ] **Step 4: Document failure and recovery boundaries**

State that down SQL is reviewed deployment code, Oracle DDL rollback is not assumed, unresolved attempts retain the mutex, recover-migration requires evidence, database undo does not roll back an APEX import, and uncaptured Builder changes/arbitrary DML remain outside observable inventory.

- [ ] **Step 5: Run every focused subsystem**

Run: PYTHONPATH=scripts python3 -m unittest tests.test_migration_bundle tests.test_migration_plan tests.test_destructive_confirmation tests.test_migration_store tests.test_sql_metadata_store tests.test_migration_runner tests.test_release tests.test_release_adapter tests.test_runbook -v

Expected: PASS.

- [ ] **Step 6: Run the complete suite and lint**

Run: PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v

Run: python3 -m ruff check scripts/

Expected: all tests pass and ruff reports no errors.

- [ ] **Step 7: Run repository contract scans**

Run:

~~~bash
! rg -n "ci-replay|docker_pdb|qualification_sha|replay_identity|fresh.*PASS|upgrade.*PASS" README.md docs/ci.md docs/migrations.md docs/promotion.md ci scripts/team.py scripts/teamlib .github/workflows
rg -n -- "--destructive-confirmation" scripts/team.py docs/migrations.md README.md
rg -n "PRIMARY KEY \\(applied_sequence\\)|operation IN \\('up','down'\\)|action IN \\('migrate','undo','redo'\\)" scripts/teamlib/migration_store.py scripts/sql/migration_metadata.sql
git diff --check
git status --short
~~~

Expected: the absence scan returns success; the positive scans find CLI/docs and both metadata definitions; diff check is clean; status lists only intended files.

- [ ] **Step 8: Review both specs line by line**

Create a temporary checklist outside the repository. Map every numbered section in persistent staging qualification to Tasks 1-9 and every numbered section in migration undo/redo to Tasks 10-18. If any requirement has no passing test or documented implementation step, amend this plan and implement that gap before the final commit.

- [ ] **Step 9: Commit**

~~~bash
git add README.md docs/migrations.md docs/promotion.md scripts/tests/test_docs.py scripts/tests/test_release_workflow.py
git commit -m "Document migration lifecycle and recovery"
~~~

## Completion criteria

Work is complete only when:

1. Default CI is one offline job and no disposable replay implementation remains.
2. Manual integration qualification produces persistent-target evidence.
3. Release test applies the exact archive, emits a canonical apply report, qualifies it, signs exact version-2 bytes, and generates the production handoff.
4. Version-1, unsigned, non-PASS, incomplete, or mismatched evidence cannot generate a runbook.
5. Migration bundles accept only a complete authored down pair and checksum all present members.
6. JSON and Oracle metadata are v2 append-only event ledgers with equivalent reads/writes.
7. Only the current global APPLIED top can be undone; redo requires a currently REVERTED migration with satisfied dependencies.
8. Destructive migrate/undo/redo accept only the exact shared confirmation document.
9. Production writes remain refused, unresolved outcomes remain recoverable and mutex-protected, and the complete test/lint suite passes.
