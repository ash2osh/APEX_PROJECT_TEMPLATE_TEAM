# APEX Team Round Trip — Implementation Plan (Plan 1 of 3)

> **For agentic workers:** Use superpowers:executing-plans, or
> superpowers:subagent-driven-development when delegation is authorized.
> Execute tasks with their tests; checkboxes below describe unfinished work.

**Revision:** 2 — replaces the original runnable examples after design review.
**Goal:** Capture Builder changes without silently overwriting Git or local work.
**Architecture:** One Python core performs target validation, content reconciliation,
journaled file changes and verified imports. Bash and PowerShell are launchers.
Baselines and recovery captures retain exact bytes independently of Git history.
**Tech Stack:** Python 3.10+, Bash, PowerShell 5.1/7, Git, qualified SQLcl/APEX.
**Spec:** [Team design](../specs/2026-09-06-team-template-design.md), §§4–6, 8–11.

## Global constraints

All tasks inherit spec §8 verbatim as their safety/portability contract.
No database operation is authorized by executing a documentation example.
Preserve the solo repository. The TEAM directory initially contains only docs:
repository initialization is an implementation step, not already completed.
Do not commit/push unless delivery is authorized. Keep durable recovery in
.sync-state; temporary helpers belong in scratch.

The earlier exported-directory deletion, filename-only decision and
"import first" recovery examples have been removed. Do not restore them.

Follow spec §8 "Executable content in plans": pure offline modules
(config.py, trees.py, reconcile.py, patch.py, masters.py parsing) carry exact
signatures and runnable tests; anything reaching a database is a directive
behind the verified adapter, never a runnable command line. Where a task below
gives an interface but no test body, write the tests first from the listed
cases — an unlisted case is not thereby excluded.

## File responsibilities and shared contracts

| Files to create | Responsibility |
|---|---|
| scripts/team.py; scripts/teamlib/__init__.py | CLI and package |
| scripts/teamlib/config.py; targets/*.json; .env.example | literal config, expected identities and roles |
| scripts/teamlib/sqlcl.py; scripts/sql/identity.sql | process protocol and SELECT-only identity capture |
| scripts/teamlib/trees.py; scripts/teamlib/state.py | exact trees, ownership, baseline/capture receipts |
| scripts/teamlib/control_store.py; scripts/sql/control_metadata.sql | isolated shared metadata bootstrap, checkout registry and app mutex |
| scripts/teamlib/reconcile.py; scripts/teamlib/patch.py | pure decision and journaled application |
| scripts/teamlib/apex.py; scripts/teamlib/masters.py | export/import qualification and subscriptions |
| scripts/export_app.sh/.ps1; scripts/import_app.sh/.ps1 | compatibility launchers |
| scripts/team.sh; scripts/team.ps1 | complete command surface |
| scripts/tests/test_*.py; scripts/tests/fixtures/ | unit and public-entry-point tests |
| .github/workflows/template-checks.yml | database-free portability checks |

Public commands (all accept --env as an alternative to PROJECT_ENV_FILE):

```text
team.py doctor
team.py setup-state
team.py register-app ALIAS [--transfer-from CHECKOUT_UUID]
team.py app-status ALIAS
team.py recover-app-lock ALIAS --run-token TOKEN --evidence DIRECTORY
team.py capture-app ALIAS
team.py bootstrap-app ALIAS
team.py adopt-app ALIAS --ref COMMIT
team.py export-app ALIAS
team.py resolve-export RECOVERY_ID --resolved DIRECTORY
team.py import-app ALIAS --ref COMMIT [--replace-from RECOVERY_ID]
team.py recover-files OPERATION_ID --action finish|restore
```

Developer import defaults --ref to HEAD, resolved once before I/O.
capture-app only captures, never imports or modifies tracked source.
app-status is read-only. It prints every checkout registered against the target
with its host, user and registration timestamp, plus any held app-target mutex
and the current import generation. For the shared development application that
list has many rows and is a roster, not an ownership claim (spec §9). For a
single-owner target it also makes the value `--transfer-from` needs discoverable
from the registry rather than from local state a re-clone may have lost.
`--transfer-from` applies only to single-owner targets and is rejected against
the shared application.
Exit codes: 0 verified success, 2 invalid contract/target, 3 conflict or
precondition refusal, 4 uncertain/incomplete operation, 5 external-tool failure.
Print a JSON result containing status, operation_id, changed_paths,
conflicts and recovery_path; keep sanitized diagnostics on stderr.

Core types, defined in their owning modules before consumers:

```python
# trees.py
from dataclasses import dataclass
from typing import Mapping
Tree = Mapping[str, bytes]                 # absence is not an empty file

# config.py
@dataclass(frozen=True)
class Target:
    project: str
    role: str
    environment: str
    connection: str
    instance_id: str
    db_name: str
    service: str
    session_user: str
    current_schema: str
    workspace_id: int | None
    app_id: int | None
    parsing_schema: str | None
    binding_digest: str

# reconcile.py
@dataclass(frozen=True)
class Decision:
    tree: dict[str, bytes]                 # candidate, incomplete on conflicts
    conflicts: tuple[str, ...]

# state.py
@dataclass(frozen=True)
class Baseline:
    version: int
    target_key: str
    source_commit: str
    tree_digest: str
    blobs: dict[str, str]                  # path -> SHA-256
```

## Task 1: Repository, config and target contracts

**Files:** .gitignore, .gitattributes, .env.example, targets/integration.json,
targets/test.json, scripts/teamlib/config.py, scripts/tests/test_config.py.

- [ ] Inspect parent instructions and existing files; initialize TEAM Git only
  during implementation, preserving all four documents.
- [ ] Define strict literal .env parsing in Python. Supported keys: PROJECT_NAME,
  TARGET_ROLE, DB_ENVIRONMENT, APEX_APPS; TABLES_SCHEMA, CODE_SCHEMA,
  APEX_PARSING_SCHEMA, METADATA_SCHEMA; for each profile prefix require
  SQLCL_CONNECTION, EXPECTED_USER, EXPECTED_CURRENT_SCHEMA,
  EXPECTED_DB_NAME, EXPECTED_SERVICE, EXPECTED_INSTANCE_ID; additionally
  APEX_WORKSPACE_ID.
- [ ] Split profiles into core and on-demand, and validate them separately.
  **Core** is TABLES, CODE, APEX and METADATA: required by every command,
  validated at load. **On-demand** is VERIFY: Plan 1 never uses it, so an
  absent VERIFY profile must not fail `doctor`, `export-app`, `import-app` or
  any Plan 1 command. Validate VERIFY only when a command that observes
  postconditions requests it (Plan 2 `migrate`, `replay`, `check-drift`), and
  fail then with a specific "VERIFY profile required for <command>" error
  naming the missing keys. A partially configured VERIFY profile is an error
  whenever it is present, so a half-filled profile cannot be ignored.
  Mirror this split in `.env.example`: core keys uncommented, VERIFY keys
  present but commented with a note that Plan 2 requires them.
  Test that every Plan 1 command succeeds with no VERIFY keys at all, that a
  Plan 2 command refuses clearly without them, and that a partial VERIFY
  profile refuses in both cases. EXPECTED_INSTANCE_ID encodes the stable database/container
  identity obtained through a qualified identity query; all profiles within
  a shared schema set must match it, independent of service aliases. No fallback connection.
- [ ] Enforce exact known keys, duplicates, empty required values, control
  characters, alias/path grammar and uppercase Oracle identifier contracts.
  Compare role and environment exactly. Permit equal tables/code profiles.
  Require METADATA_SCHEMA distinct from both application schemas. The metadata
  owner is the controller principal; VERIFY has read-only grants and owns no
  application/metadata objects. Setup checks effective roles/ANY/proxy access
  cannot bypass these boundaries before enabling writes.
- [ ] Reject mismatched deployment JSON and profile identities. Define target
  JSON schema with these same fields plus per-alias app IDs; secret values are
  prohibited. Local default binding is generated only by explicit setup.
- [ ] Add ignore rules for .env and .env.*, with !.env.example; .sync-state/,
  scratch/, dist/, apps/**/deployments/default.json. Add LF rules for .apx,
  .sql, .json, .sh and .ps1.
- [ ] Run this regression suite before implementation, then make it pass:

```python
# scripts/tests/test_config.py
import unittest
from teamlib.config import parse_apps

class ConfigTests(unittest.TestCase):
    def test_alias_mapping(self):
        self.assertEqual(parse_apps("checkout:101,admin:102"),
                         {"checkout": 101, "admin": 102})
    def test_reject_ambiguous_or_unsafe_mapping(self):
        for value in ("Checkout:101", "../x:101", "x:0",
                      "x:101,x:102", "x:101,y:101", "x:101, y:102"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_apps(value)
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_config.py -v`.
Add loader fixtures for CRLF, duplicate/missing keys and literal shell syntax;
assert a sentinel command in a value never executes.

## Task 2: One SQLcl process boundary

**Files:** scripts/teamlib/sqlcl.py, scripts/sql/identity.sql,
scripts/tests/test_sqlcl.py, scripts/tests/fixtures/fake_sqlcl.py.

**Interface:** `run_sqlcl(target: Target, operation: str, driver: Path,
work: Path) -> SqlResult`. SqlResult contains verified identity, sanitized log
path and parsed result manifest. operation is read or write.

- [ ] Read the solo launcher/guards/identity SQL and their tests. Port their
  lessons, not the incorrect calling convention: the old identity include
  requires target_schema, db_environment and expected_user definitions.
- [ ] Use a regular empty file as stdin, shell=False, an argv list, explicit
  working directory, UTF-8 and a generated @driver.sql file. Resolve Windows
  sql.exe/sql.bat launch behavior and quoting with native tests; paths containing
  spaces must work. Reject unsupported shell metacharacters explicitly.
- [ ] Perform SELECT-only identity discovery for every connection. Verify all
  expected fields, classify production-like identities, then verify again in
  the actual payload session before any source statement. Use a SQLcl driver
  mechanism qualified to stop on mismatch; do not let a Python preflight alone
  authorize a later unchecked session.
- [ ] Refuse production writes before launching SQLcl. Production read drivers
  contain SELECT statements only, without a PL/SQL assertion block.
- [ ] Keep metadata/verification sessions separate from payload sessions. Do
  not expose controller credentials or mutex tokens to payload processes. Use
  controlled startup/login scripts; reject unreviewed connection-changing or
  mutating startup behavior. Profile operation classes include metadata writes
  and verification reads, and verification writes always refuse.
- [ ] Set SQLERROR/OSERROR exits, DEFINE OFF for payloads and positive,
  operation-specific completion records. Reject ORA-/SP2-/SQLcl/Java failures
  even on exit 0, missing/truncated output and malformed records. A bare marker
  after an unsuccessful APEX command is insufficient: verify expected exports
  or post-import state.
- [ ] Test error-zero, startup exception, wrong identity, changed second-session
  identity, no completion, CRLF, Unicode and stdin regular-file type.

Executable subprocess contract to implement inside the adapter:

```python
with stdin_path.open("rb") as empty:
    result = subprocess.run(
        argv, stdin=empty, cwd=work, shell=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_sqlcl.py -v`.

## Task 3: Exact trees and owned-source manifest

**Files:** scripts/teamlib/trees.py, scripts/tests/test_trees.py.

**Interfaces:** `read_git_tree(repo, commit, alias) -> dict[str, bytes]`;
`read_export_tree(path) -> dict[str, bytes]`;
`tree_digest(tree) -> str`; `assert_source_clean(repo, alias) -> None`.

- [ ] Define ownership using a qualified full SQLcl export fixture. Include
  binaries and .apex/apexlang.json; exclude deployments/** and export logs.
  Unknown export classes fail rather than being silently dropped.
- [ ] Read Git paths with NUL-delimited plumbing and blob bytes, not locale
  text listings. Fail on unresolved commit/read failures. Preserve zero-byte
  files, normalize only APEXlang LF, reject symlinks, unsafe modes, traversal,
  case collisions and Windows-reserved components.
- [ ] Check index/worktree plus untracked and ignored collisions in the owned
  source set. Unrelated ignored files outside that set remain untouched.
- [ ] Digest sorted POSIX paths plus explicit byte lengths and file hashes;
  use the same canonical JSON manifest encoding everywhere.
- [ ] Add fixtures for names containing spaces, binary zeroes, LF/CRLF,
  zero-byte-versus-missing, deleted/added Git files, corrupt refs and preserved
  deployment JSON. Confirm corrupted Git reads never become empty trees.
- [ ] Add these regression tests first. `reconcile` distinguishes a missing
  path from a zero-byte file, so a Tree that collapses the two silently
  destroys that distinction one layer down, where it is no longer observable:

```python
import unittest
from teamlib.trees import read_export_tree, tree_digest

class TreeSemanticsTests(unittest.TestCase):
    def test_zero_byte_file_is_present_not_absent(self):
        tree = read_export_tree(self.fixture("zero-byte"))
        self.assertIn("pages/p00001-home.apx", tree)
        self.assertEqual(tree["pages/p00001-home.apx"], b"")

    def test_absent_path_is_not_a_key(self):
        tree = read_export_tree(self.fixture("zero-byte"))
        self.assertNotIn("pages/p00002-missing.apx", tree)

    def test_zero_byte_and_absent_digest_differently(self):
        # The two states must never produce the same manifest digest.
        self.assertNotEqual(tree_digest({"a": b""}), tree_digest({}))

    def test_binary_bytes_are_exact(self):
        tree = read_export_tree(self.fixture("binary"))
        blob = tree["shared-components/static-files/icons/app-icon-32.png"]
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")

    def test_binary_is_not_line_ending_normalised(self):
        # LF normalisation applies to APEXlang only; touching binaries here
        # corrupts assets in a way no later stage can detect.
        tree = read_export_tree(self.fixture("binary-with-crlf-bytes"))
        self.assertIn(b"\r\n", tree["shared-components/static-files/x.png"])

    def test_digest_is_order_independent(self):
        self.assertEqual(tree_digest({"a": b"1", "b": b"2"}),
                         tree_digest({"b": b"2", "a": b"1"}))

    def test_digest_separates_path_from_content(self):
        # A digest concatenating path and bytes without length framing
        # collides here; this asserts the framing exists.
        self.assertNotEqual(tree_digest({"ab": b"c"}), tree_digest({"a": b"bc"}))
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_trees.py -v`.

## Task 4: Content reconciliation

**Files:** scripts/teamlib/reconcile.py, scripts/tests/test_reconcile.py.
**Interface:** `reconcile(base: Tree, head: Tree, mine: Tree) -> Decision`.

- [ ] Add the regression tests below and verify module/function failure first.
- [ ] Implement the complete state table, shared by binary/text files:

```python
def reconcile(base, head, mine):
    absent = object()
    output, conflicts = {}, []
    for path in sorted(set(base) | set(head) | set(mine)):
        b, h, m = (tree.get(path, absent) for tree in (base, head, mine))
        if m == h:
            selected = m
        elif m == b:
            selected = h
        elif h == b:
            selected = m
        else:
            conflicts.append(path)
            continue
        if selected is not absent:
            output[path] = selected
    return Decision(output, tuple(conflicts))
```

```python
import unittest
from teamlib.reconcile import reconcile

class ReconcileTests(unittest.TestCase):
    def test_colleague_addition_is_preserved(self):
        d = reconcile({}, {"new": b"alice"}, {})
        self.assertEqual(d.tree, {"new": b"alice"})
        self.assertFalse(d.conflicts)
    def test_stale_existing_page_preserves_head(self):
        d = reconcile({"p": b"old"}, {"p": b"alice"}, {"p": b"old"})
        self.assertEqual(d.tree, {"p": b"alice"})
    def test_colleague_deletion_stays_deleted(self):
        self.assertEqual(reconcile({"p": b"x"}, {}, {"p": b"x"}).tree, {})
    def test_conflicting_content_is_not_applied(self):
        d = reconcile({"p": b"old"}, {"p": b"a"}, {"p": b"b"})
        self.assertEqual(d.conflicts, ("p",))
    def test_modify_delete_conflicts(self):
        self.assertEqual(reconcile({"p": b"old"}, {"p": b"a"}, {}).conflicts,
                         ("p",))
```

- [ ] Exhaustively enumerate one-path states missing, empty, A and B for
  base/head/mine. Check the specification table for all 64 cases; add
  multi-path cases combining a safe edit and conflict.
- [ ] Assert callers never apply Decision.tree when conflicts is nonempty.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_reconcile.py -v`.

## Task 5: Baselines, durable captures and receipts

**Files:** scripts/teamlib/state.py, scripts/teamlib/control_store.py,
scripts/sql/control_metadata.sql, scripts/tests/test_state.py,
scripts/tests/live/test_app_lock.py.
**Interfaces:** `save_capture(target, base, head, mine, diagnostics) -> str`;
`load_baseline(target) -> Baseline`;
`save_verified_baseline(target, commit, tree) -> None`.

- [ ] Implement setup-state through the metadata write profile. Install exact
  versioned TEAM_CONTROL_META, TEAM_APP_REGISTRY and TEAM_APP_MUTEX structures
  only in METADATA_SCHEMA. Partial or incompatible setup refuses. Plan 2 extends
  this shared schema; it does not create a second controller identity.
- [ ] register-app records a checkout against a target. For the **shared
  development application** the registry is a roster (spec §2, §9): multiple
  concurrent checkouts are expected, every registration succeeds, and
  `--transfer-from` is neither required nor accepted. Refusing a second checkout
  here would lock out the whole team after the first developer registered, so a
  test asserting that refusal would be asserting the bug. Verify registration
  before developer app mutation, and record host and user so a held mutex and a
  recovery can name a person.
- [ ] Keep exclusive-checkout and transfer semantics for a target declared
  single-owner — an optional isolated developer copy (spec §2.1). There a
  transfer requires the old UUID, a retained fresh capture and proof the old
  worker ended; unknown liveness blocks. Drive this from the target's declared
  ownership mode, not from the command name, so the shared and isolated paths
  cannot diverge in their guards.
- [ ] Define acquire_app(target_key, run_token) and release_app(target_key,
  run_token) on control_store.py using committed conditional mutex updates.
  Hold this app-target mutex across import and verification; do not expire or
  steal it. Recovery follows exact-token/no-live-worker rules. Downstream deploy
  uses the same mutex without developer registration. A second concurrent
  *import* must fail to acquire the same target; a second *checkout* of the
  shared application must not.
- [ ] Maintain a monotonic import generation for each target alongside the mutex,
  incremented when an import completes, so a reader can prove no import
  intervened during its capture (spec §2.1). Expose it through a single
  read-only `read_app_sync_state(target_key)` returning generation plus current
  mutex holder, so export never acquires anything. Test that an import which
  begins and completes entirely between two reads is still detected.
- [ ] recover-app-lock requires the selected run token, worker-termination
  evidence and retained current target capture. It may clear ownership only
  after resolving uncertain app state; it cannot stamp a verified baseline
  from a lock-clear operation. Refuse production and unverifiable worker state.
- [ ] Store versioned manifests, immutable blobs and operation records under
  .sync-state. Derive target keys from canonical Target identity; binding and
  workspace changes invalidate reuse. Store canonical Git blob provenance,
  but do not depend on its continued reachability.
- [ ] Store capture receipts linking mine digest to successfully written source
  digest; resolution receipts additionally record conflict paths and resolved
  digest. These are not database-alignment baselines.
- [ ] Atomic-save manifests only after all blobs exist; validate hashes on load.
  An existing baseline becomes uncertain at the start of an import mutation.
- [ ] Add tests for target rebinding, pruned commit with retained blobs,
  corrupted manifests/blobs, partial save, concurrent local operation refusal
  and receipts that do not match the chosen commit.
- [ ] Recovery captures survive every failure and process restart. Garbage
  collection is an explicit command listing exact recoverable entries; never
  delete captures as an EXIT side effect. Baselines, receipts, quarantined state
  and interrupted journals are GC roots; shared blobs cannot be removed while
  referenced. Corrupt/unreadable roots block collection. Test these cases.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_state.py -v`.

## Task 6: Journaled file-level application

**Files:** scripts/teamlib/patch.py, scripts/tests/test_patch.py.
**Interface:** `apply_tree(repo, alias, expected_head, before, after,
recovery_id) -> None`; `recover_files(operation_id, action) -> None`.

- [ ] Refuse conflicts, changed HEAD, dirty source and mismatched before
  manifest. Acquire an app-local lock and recheck immediately before writing.
- [ ] Compute writes/deletes only over owned paths, persisting every preimage
  and postimage in the journal before the first write.
- [ ] Replace individual files through sibling temporary files and os.replace;
  remove only explicit files whose current hashes match expected preimages.
  Never recursively delete apps/<alias> or deployments.
- [ ] Verify full result, then record completion and receipt. On failure leave
  journal/recovery intact and block further app operations.
- [ ] Inject failure after each write/delete, test finish/restore and refuse
  recovery over a later user edit. Assert all deployment bindings and unrelated
  files remain byte-identical.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_patch.py -v`.

## Task 7: Master contracts and tool qualification

**Files:** targets/masters.json, scripts/teamlib/masters.py,
scripts/tests/test_masters.py, docs/toolchain.md.
**Interface:** `validate_masters(source_tree, target, contract) -> MasterReport`.

- [ ] Parse subscription/master properties from qualified APEXlang fixtures;
  ignore quoted SQL, comments and non-master @ references. Reject unsupported
  subscription syntax explicitly.
- [ ] Contract entries contain app ID, app alias, workspace identity,
  component type and symbol. Query through the exact selected target and
  verify each required component, including built-in theme masters.
- [ ] Treat visibility failure as unknown/refusal. Do not accept ID-only
  existence as successful resolution.
- [ ] Record qualified SQLcl/JDK/APEX versions and command success/error formats.
  Cross-instance linkage is an acceptance test, not inferred from the earlier
  same-instance spike.
- [ ] Tests: correct app/wrong component, reused ID, invisible master,
  malformed export, built-in theme and valid external master.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_masters.py -v`.

## Task 8: Export and explicit bootstrap

**Files:** scripts/teamlib/apex.py, scripts/tests/test_export_app.py.
**Interfaces:** `capture_app(target) -> Capture` (Capture has tree,
recovery_id, target and the sync state observed either side of the read);
`export_app(target) -> Decision`.

- [ ] Implement spec §6 export sequence using Tasks 2–7. Capture first, retain
  before mutation, reconcile contents, journal the patch, verify and receipt.
- [ ] Bracket the capture with `read_app_sync_state` (Task 5). Refuse before
  starting if an import holds the mutex. After the read, accept the capture only
  if the generation is unchanged and no holder appeared; otherwise discard it as
  possibly torn and retry a bounded number of times before reporting that an
  import is in progress. **Export never acquires the mutex** — readers that lock
  can block the team and strand a lock when a developer's export dies, and two
  concurrent exports are harmless (spec §2.1). A discarded capture has no side
  effects, so retry is free.
- [ ] bootstrap-app requires absent tracked alias source, captures the existing
  app and writes a reviewable candidate without database writes. adopt-app
  requires clean committed source and exact re-export equality before baseline.
- [ ] Reject export for integration/test/replay regardless of binding filename.
  Verify actual workspace/app identity and alias match.
- [ ] Test through the public CLI in temporary Git repositories with a fake
  SQLcl process: colleague changes, both changes, missing baseline, false-success
  partial export, dirty source, source directory symlink, rebinding, preserved
  deployments and HEAD movement. Assert actual filesystem and exit status.
- [ ] Test the torn-capture cases with a fake SQLcl that mutates the sync state
  mid-read: an import already holding the mutex when export starts, an import
  starting and still running when the capture ends, and an import that begins
  and completes entirely within the capture window. All three must discard the
  capture and write no tracked source. Add the negative: two concurrent exports
  must both succeed and neither may block the other.
- [ ] Verify a refusal prints the durable capture location and resolution
  command, never an unconditional instruction to import over Builder work.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_export_app.py -v`.

## Task 9: Guarded import with capture protection

**Files:** scripts/teamlib/apex.py, scripts/tests/test_import_app.py.
**Interface:** `import_app(target, commit, replace_from=None) -> Baseline`.

- [ ] Verify checkout registration and acquire the shared app-target mutex
  through control_store before capture. Refuse parallel clients and retain the
  lock on unknown import state until reviewed recovery; release on verified
  success or a known refusal before payload starts.
- [ ] Resolve the commit, require clean source, materialize exact owned blobs
  and validated binding into scratch.
- [ ] Capture current app before replacement. Require baseline equality,
  valid capture/resolution receipt matching selected source, or the exact
  --replace-from capture for first alignment. Verify absent-app bootstrap
  explicitly. A generic --force flag is not provided.
- [ ] Treat the baseline-mismatch refusal as the design's primary guard, not a
  conservative default (spec §6). The application is shared, so the work it
  protects belongs to colleagues who do not know the command is running and
  cannot consent to losing it. The refusal names the paths that differ and the
  export that would preserve them; no flag waives it.
- [ ] Increment the target's import generation on completion (Task 5) so a
  concurrent export can prove whether its capture straddled this import.
  Increment on the completion path only — a refused or failed import that wrote
  nothing must not advance it, and one that wrote partially must leave the
  target uncertain rather than merely bumping a counter.
- [ ] Print the team-pause requirement before any write: this import overwrites
  the application everyone is editing, and §9 makes announcing it part of the
  operation. State the alias, target identity and expected duration. This is the
  floor. Plan 3 Task 3 adds `announce-import`, which drafts the message from
  observed state and confirms with the developer; keep this print correct on its
  own, because Plan 1 must be usable before Plan 3 exists.
- [ ] Validate masters/source, guard write, recheck capture and target, import,
  re-export and verify bytes plus master linkage. Baseline becomes verified
  only after all checks. Preserve old state as uncertain on import failure.
- [ ] Test uncaptured Builder edit refusal, captured-and-committed edits,
  dirty source, stale receipt, changed app between captures, import error-zero,
  subscription mismatch, interrupted verification and successful stamping.
  Include a second developer's uncaptured Builder work as the refusal case, and
  assert the generation does not advance on any refused or failed path.
- [ ] Demonstrate that every DB write occurs after exact in-session identity
  assertion and no failure path reports a current baseline.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_import_app.py -v`.

## Task 10: Resolution flow and command integration

**Files:** scripts/team.py, scripts/teamlib/state.py,
scripts/tests/test_recovery_flow.py.

- [ ] Wire all documented commands with argparse; no implicit commit or push.
- [ ] resolve-export takes a validated complete resolved source tree and the
  immutable recovery ID. Require original HEAD/preimage unchanged, or re-run
  reconciliation against new HEAD and retain a new bundle.
- [ ] Use Task 6 for writes, record the resolution receipt, and print review,
  commit and import steps. Reject unresolved conflict marker content where
  produced by tooling, missing paths and extra unsafe files.
- [ ] Test the full Bob scenario: capture conflict, restart process, resolve,
  user commit, import, then verified re-export. Also add a new Builder edit
  after capture and prove import refuses without losing either version.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_recovery_flow.py -v`.

## Task 11: Thin Bash/PowerShell launchers and native parity

**Files:** scripts/team.sh/.ps1, export_app.sh/.ps1, import_app.sh/.ps1,
scripts/tests/test_launchers.py, .github/workflows/template-checks.yml.

- [ ] Launch Python with quoted argv and propagate its status; choose python3
  or py -3 only after a version check. Do not duplicate config or reconciliation.
- [ ] PowerShell dynamic environment reads use the .NET API. Pass arrays,
  never an interpolated command string; test native Windows SQLcl launch.
- [ ] Run the same parameterized public-command fixtures on Linux Bash,
  Windows Git Bash, PowerShell 7 and Windows PowerShell 5.1.
- [ ] Add CI syntax checks, full Python tests, case-collision and LF checks.
  Execute Bash syntax validation once per file, not bash -n scripts/*.sh
  (which does not parse every positional argument as a script).

```bash
for script in scripts/*.sh; do
  bash -n "$script" || exit
done
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```

## Task 12: Live acceptance, documentation and handoff

**Files:** scripts/tests/live/test_apex_round_trip.py, docs/toolchain.md,
docs/app-recovery.md.

- [ ] Use explicitly provisioned disposable APEX targets with exact profiles;
  run all write guards. Never default to the historical docker-demo fixture.
- [ ] Verify determinism, round-trip, binaries, subscription linkage,
  target remapping, missing/wrong master and full capture/resolve/import flow.
  Include cross-instance masters, not just two apps on one instance, and payload
  attempts to mutate isolated metadata.
- [ ] Verify the shared-application concurrency cases live, against a real
  application two checkouts both target (spec §2): two checkouts registering
  must both succeed; two concurrent exports must both succeed; a second
  concurrent import must fail to acquire the mutex; and an export whose capture
  straddles a real import must discard rather than commit a torn tree. The
  refusal expected here is the second *import* and the torn *capture* — never
  the second checkout, which is the ordinary case.
- [ ] Capture tool versions and signed-off test evidence; no automatic Git
  staging/commit in test code. Do not classify unrun live cases as passing.
- [ ] Run all offline/native suites once more and review diffs. Document
  remaining Builder race and import-failure boundaries.
- [ ] Keep live cases available to Plan 3's required isolated CI gate.

## Completion checklist

- [ ] Pure reconciliation exhaustive cases and real wrapper filesystem cases pass.
- [ ] Baseline provenance, receipts and interrupted recovery are tested.
- [ ] Concurrent checkouts and concurrent exports of the shared application both
  succeed; concurrent imports and torn captures are refused.
- [ ] Target/deployment agreement and production write refusal are tested.
- [ ] All four shell/platform surfaces execute the shared core successfully.
- [ ] Live qualification evidence exists, including cross-instance subscriptions.
- [ ] Plan 2 consumes Target/run_sqlcl without another safety implementation.

No task is complete merely because an argument-validation test passes.
