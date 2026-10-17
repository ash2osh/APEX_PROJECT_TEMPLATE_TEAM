# DEV test runner design

Date: 2026-10-16
Status: specification for review; no implementation or live execution authorized by this document.
Applies to: APEX 26.1 and APEX 26.2 templates, with each release's existing contracts preserved.

## 1. Purpose and agreed decisions

Provide a repeatable way to exercise the template against a real Oracle DEV database through a developer's normal saved connection. Developers should not need a separate database, another schema, SYS access, or a copy of their application's dependency tree.

The user accepted qualification against DEV's installed versions because DEV is a production clone. This feature records those versions; it does not require a separate version/platform matrix before a project can use it.

The user explicitly chose optional cleanup. Fixture objects and data are retained by default for inspection. Retention is a normal result, not a failure. Expected rollbacks inside a particular transaction test can still undo that test's transient rows; retention does not mean every intermediate row must survive.

The first release combines six built-in template scenarios with a small, declarative interface for project SQL scenarios. NATREC business names, IDs and repair procedures do not belong in the generic runner.

Success means a developer can preview the target and effects, execute selected scenarios, inspect evidence and retained fixtures, and optionally clean up supported fixtures later. Reports must distinguish successful assertions, failed assertions, missing prerequisites, incomplete execution and retained data.

Writing this specification does not apply a migration, run a scenario, change an application, create a user, commit or push files. Implementation planning follows review of this document.

## 2. Approach and boundaries

### Alternatives considered

| Approach | Benefit | Limitation | Decision |
| --- | --- | --- | --- |
| Dedicated disposable database/schema as a prerequisite | Stronger isolation for destructive experiments | Often unavailable; changes privilege and dependency context; adds setup burden | Optional outside this feature; not required |
| Repeat the current one-off scripts manually | Minimal new tooling | Repeated setup, inconsistent reports and incomplete reproducibility | Preserve as evidence; replace routine qualification with the runner |
| Small fixtures in existing DEV schemas, using existing execution modules | Works with normal developer access and provides reusable evidence | Shares database resources; project effects require explicit review | Selected |

### First-release scope

- One selected DEV owner per invocation, sequential execution, Bash and PowerShell entry points with equivalent behavior.
- Read-only planning and prerequisite inspection; explicit execution of built-in or selected project scenarios.
- Production SQL generators, SQLcl transport, migration checks, migration attempts and receipts exercised in private scratch projects.
- Unique fixture names, durable local run evidence, retained-fixture inventory and separately requested cleanup.
- Project SQL scenarios with explicit prerequisites, effects, steps, checks and narrowly defined fixture lookup.
- Current target identity/version capture and bounded compilation diagnostics.

### Exclusions

- Staging/production execution, automatic user creation, grants, package copying, global recompilation or database configuration changes.
- Copying the application or resolving its entire dependency graph.
- Automatic modification of an existing application object as part of a generic test.
- APEX application import/publish, native workflow/task operations, ORDS deployment, HTTP requests, payments, SMS, push or email scenario adapters. Existing dedicated qualification probes remain separate.
- An automatic historical-receipt regression classifier, arbitrary Python plug-ins, automatic code rewriting or arbitrary fixture reuse.
- Database-backed run ledgers, locks, team metadata tables, a scheduler or automatic CI access to shared DEV.

Existing `check-conflicts`, `migrate`, `verify`, `rollout`, `doctor`, publish and deployment contracts remain authoritative for their operations. The new runner does not replace them or automatically apply actual project migrations.

## 3. Public interface

Proposed commands; these do not exist at the time of this specification:

```bash
# Read-only database inspection plus a local evidence report; no scenario SQL runs.
scripts/team.sh dev-test --env dev --schema APP --plan

# Execute the six built-in scenarios; retain their fixtures.
scripts/team.sh dev-test --env dev --schema APP --run

# Select a reviewed project suite, optionally narrowed to named scenarios.
scripts/team.sh dev-test --env dev --schema APP \
  --suite dev-tests/suites/orders.json --scenario cancel-order --run

# Inspect a saved report without connecting.
scripts/team.sh dev-test --report <run-id> --format markdown

# Explicitly clean supported artifacts from a saved run.
scripts/team.sh dev-test --cleanup <run-id>
```

`scripts/team.ps1` accepts the same arguments. `APP`, the suite path and scenario name above are examples, not inferred live targets.

- Exactly one action is required: `--plan`, `--run`, `--report` or `--cleanup`. No arguments print usage without connecting.
- Planning/running require explicit `--env dev`. Other environments are refused. Multi-schema profiles require `--schema`; a single configured owner can be inferred using existing resolution rules.
- Without `--suite`, select the complete built-in suite. A project suite selects its own declared scenarios; it does not silently add project writes to the built-in default. Repeated `--scenario` narrows selection, retaining declared dependency order.
- A narrowed selection reports its exact coverage; it does not certify the omitted scenarios.
- `--format text|json|markdown` changes rendering, not execution or stored facts. Text is the console default; every plan/run writes JSON and Markdown evidence.
- Report/cleanup use the saved run identity. They do not accept flags that substitute another schema, connection or environment. The recorded alias must still resolve to the recorded target before cleanup writes.
- `--run` is the explicit request to execute the displayed suite's declared effects. No second routine confirmation prompt or prior-plan token is required. Run recalculates prerequisites and freezes its own inputs rather than trusting an older preview.
- There is no automatic resume/replay of a write attempt. New execution creates a new run. Interrupted runs remain available for report and reconciliation.

## 4. Target, capabilities and planning

Use the existing configuration loader and `resolve_target(..., operation="migration")`, including configured owner selection, expected user and production-target refusals. Do not introduce another credential store or automatically choose a DBA alias.

For first-release execution, the observed session user must equal the selected schema owner. A privileged deployment account using another current schema is reported as unsupported for execution, not silently substituted. Separate schema owners use separate invocations and their existing saved connections. Cross-schema references remain references to their real owners.

Plan performs only controlled read-only catalog/check queries, never suite setup/exercise/cleanup SQL. It may create private local evidence files. It records:

- Configured alias/owner and independently observed session user, current schema, database/unique name, service, container, edition and database version using the existing identity model.
- APEX release when installed and visible, otherwise its explicit unavailable/not-applicable status; database-only fixtures do not require an APEX session.
- SQLcl version, current commit and whether tracked inputs differ from that commit. File hashes identify dirty inputs; a SHA alone must not label them as committed code.
- Scenario object/member requirements, accessible privilege/quota evidence, planned object names, expected persistent states, declared affected objects and known background consumers.
- Pre-existing status/errors for explicitly selected application prerequisites. Do not perform an unrestricted whole-database invalid-object scan.
- Expected small fixture shape and row limits. Metadata access and successful planning cannot guarantee sufficient free space at execution time; actual allocation failures remain possible.

Prerequisite evidence distinguishes `satisfied`, `missing` and `unknown`. Unknown metadata visibility is not proof of an absent grant/object. Basic member existence is not overload/type compatibility proof; advanced signatures need an explicit project check. Bounded synonym/grant inspection retains the existing known limitations, including unresolved chains, links and role-only compile access. Free-space/quota visibility is advisory, not a mandatory DBA capability: lack of access to database-wide space views must not block an otherwise eligible owner. Known allocation refusal blocks the affected scenario; space can also run out after planning.

An overall identity failure stops before any scenario writes. An independent scenario with an unavailable prerequisite is `BLOCKED`; dependent scenarios are `NOT_RUN`. Other independent scenarios may continue. No automatic grant, recompile, dependency transfer or privilege escalation follows a failed prerequisite.

## 5. Built-in scenarios and exact success criteria

All generated migration folders and their receipts/events belong to the private scratch project, not the application's tracked migration tree. Helpers may be factored for reuse, but do not duplicate the production generator or migration state machine inside the test runner.

| ID | Exercise | Pass evidence | Expected retained state |
| --- | --- | --- | --- |
| `datatype-roundtrip` | Create a small source fixture with BOOLEAN, NUMBER(10,2), NUMBER(*,3), FLOAT(63), VARCHAR2 CHAR semantics and RAW; capture through the production catalog path; generate and execute a second definition through baseline code | Fresh-session attributes match; production definition checks return 1; deliberate wrong expectations return 0 | Source/copy fixture tables; no business rows copied |
| `compiler-settings` | Create a minimal stored unit, capture its actual settings and execute the production-generated settings query against Oracle | Exact settings accepted; wrong scope and deliberately wrong setting expectations rejected; null/non-null CCFLAGS cases covered | Minimal fixture unit and its final settings |
| `sqlcl-delivery` | Seed a two-row fixture, run an UPDATE containing a blank line before WHERE through the actual rollout path | Completion/identity evidence plus fresh-session readback: selected row changed and the other row unchanged | Two-row fixture table |
| `migration-success` | Generate a nullable-column structure change and apply it through the actual migration path | Exact live column postcondition and valid scratch receipt with matching payload/check hashes | Small fixture table with added column; successful local receipt |
| `migration-refusal` | Apply a deliberately incorrect fixture definition with a reviewed failing postcondition, then exercise the guarded retry | Expected verification refusal, no success receipt, pending/locking event retained; retry refused before another payload execution | Deliberately mismatched fixture table; failed attempt evidence |
| `invalid-view` | Create a fixture table/view, verify VALID, then mutate the fixture dependency | Generated validity check changes from 1 to 0; current status and accessible ALL_ERRORS captured; migration verification refuses success for the invalid fixture | Table and deliberately INVALID fixture view |

The invalid-view scenario passes by detecting its expected invalid view. That view is listed as intentionally retained, not reported as an unexplained application regression. If Oracle reports no error rows for a particular invalidation, record the observed status and zero rows; never invent compiler diagnostics.

Negative scenarios pass only when the intended failure and all required postconditions are observed. Authentication failures, missing result frames, transport errors and unexpected Oracle errors cannot satisfy an expected-failure assertion.

No built-in scenario modifies real application packages, disables shared automations, calls business APIs or copies application data. Database/global triggers and resource contention can still affect fixture execution; separate object names are not full database isolation.

## 6. Project scenario contract

Project authors place suites under `dev-tests/`. This directory is project-owned, survives template upgrades and is outside inherited template-maintenance `tests/` cleanup.

A suite uses strict JSON `schemaVersion: 1`, unique scenario IDs and explicit ordered scenario definitions. Unknown fields, duplicate IDs, cyclic dependencies and path escapes refuse before SQLcl execution. SQL paths are confined to that suite's resolved directory tree; symlink escapes are refused. No implicit recursive discovery executes newly added files.

Each scenario declares:

| Field | Contract |
| --- | --- |
| `id`, `description` | Stable identifier and human-readable intent |
| `dependsOn` | Earlier scenarios whose success is required; no inferred cross-schema order |
| `requirements` | Explicit owner/name/type/status/member requirements and reviewed scalar capability checks; no automatic repairs |
| `effects` | Declared writes, transaction behavior, external effects, affected object families and known background consumers, including empty lists when none are expected |
| `fixtures` | Planned new objects and narrowly scoped selectors for declared existing fixture rows |
| `steps` | Ordered SQL-file paths; each step declares its expected session outcome |
| `checks` | Nonempty independent postconditions using the existing scalar check shape: `id`, `sql`, integer `expected` |
| `observations` | Optional bounded read-only outputs whose absence is reported without converting a failed mandatory assertion to success |
| `cleanup` | Optional reviewed SQL plus absence checks for declared artifacts; otherwise retention/manual management is supported |

This is a SQL scenario interface, not a dependency solver or sandbox. Declared effects are author-supplied expectations; they do not prove there are no indirect trigger, autonomous or external effects. Unknown transitive effects remain explicit. First-release execution refuses a scenario declaring external effects or unresolved transaction/effect classification; its plan remains inspectable.

Project steps use the existing rollout SQL-only validator/driver semantics, including explicit transaction control only in this scenario route. Ordinary migrations keep their current transaction-ownership restrictions. Host/client commands and hidden connection changes remain prohibited. Mandatory assertions use the existing conservative read-only check evaluator, not arbitrary PL/SQL labeled as a read.

For expected step failures, permit an explicit expected SQLcl outcome and named Oracle error code, then require independent database assertions. The implementation must retain the full outcome classification; matching an error substring alone never passes a scenario.

### Bindings and fixture lookup

- Built-in run variables are typed bindings: run ID, validated owner and generated fixture names. SQLcl substitution is disabled. Identifiers use a separate validated identifier-rendering path; never concatenate untrusted row values into SQL.
- Project lookup queries are conservative read-only SELECTs returning a bounded row with named scalar values. A selector declaring one fixture must yield exactly one row; zero or multiple rows block the scenario before writes. No automatic MIN(ID), positional row choice or copied environment-specific IDs.
- Captured identifiers are available as typed binds in later steps and fresh-session checks. Sensitive values are not eligible for saved bindings; credentials/tokens are supplied through existing credential mechanisms, not manifests or reports.
- Project author supplies retention-aware assertions. An intentionally retained CANCELLED order, for example, is not expected to disappear at the end of the run.
- The first release has no generic APEX session adapter. A scenario that needs native APEX sessions/workflow/task execution remains outside first-release execution; its dedicated probe must retain its own lifecycle contract.

Existing application changes continue through their original-owner migration/publish routes. A project scenario may exercise a reviewed operation on explicit fixture records, but the generic runner never replaces the implementation under test with a copied version from another environment.

### Manifest types and minimal example

The suite root has exactly `schemaVersion`, `id`, and nonempty `scenarios`. Every scenario has the fields in the table above; arrays can be empty where described as optional and `cleanup` can be null. IDs use lowercase ASCII letters, digits and hyphens, start with a letter, and have a maximum length of 64. Descriptions are nonempty strings. No shell command or environment-variable interpolation field exists.

- `requirements` is `{ "objects": [...], "checks": [...] }`. Object entries have exactly `owner`, `name`, `type`, `requiredStatus` and nullable `member`; values are validated catalog identifiers, type names and `VALID`/`ANY` status requirements. The special owner value `@selected` means the resolved owner. Checks use the existing `id`/`sql`/integer `expected` shape.
- `effects` has exactly `writes` (boolean), `transaction` (`read-only`, `caller-controlled`, `commits`, `ddl`, `mixed`, or `unknown`), `external` (`none`, `present`, or `unknown`), `affectedObjects` (explicit `OWNER.NAME` patterns, with `@fixtures` for generated fixture objects) and `backgroundConsumers` (reviewed names/descriptions). These fields describe expectations; the runner cannot enforce complete effect isolation by parsing SQL.
- `fixtures` has `objects` and `selectors` arrays. Object entries are `{ "binding": "items", "suffix": "ITEMS", "type": "TABLE" }` with a unique binding/suffix; supported cleanup object types in v1 are TABLE, VIEW, PACKAGE, PROCEDURE and FUNCTION. Package spec/body share one package cleanup inventory entry. Selectors have `id`, `sql` and `columns`, where each column has `name`, `binding` and `type` (`integer` or `string`). Selectors identify existing fixture rows before execution and must return exactly one row; setup-generated values use run tags and bounded observations instead of implicit global row discovery.
- Reserved value binding `:DEVTEST_RUN_ID` is a string. Selector bindings are prefixed `DEVTEST_`, validated and unique. Generated object references use the explicit `${object.items}` token, expanded only to the selected owner and validated generated name as quoted identifiers. Arbitrary text substitutions, missing tokens and token use inside SQL literals/comments are refused. Both frozen input and exact rendered SQL hashes are recorded.
- `steps` entries have `id`, relative `sqlFile`, and `expected`. Expected is either `{ "kind": "success" }` or `{ "kind": "oracle-error", "code": "ORA-12345" }`. A named Oracle error requires a verified step-start/identity observation, the expected SQLcl error outcome and subsequent independent checks. A timeout or truncated/unframed session is UNKNOWN, not an expected error.
- `observations` entries have `id`, conservative SELECT `sql`, and integer `maxRows` from 1 to 100. They may record non-sensitive fixture keys/current states after steps. Truncation is reported and makes inventory incomplete when those keys are required for cleanup; no deletion is then inferred from an incomplete inventory.
- `cleanup` is null or `{ "sqlFiles": [...], "checks": [...] }`, with nonempty ordered relative SQL files and nonempty existing-shape absence checks. Its files may address only declared run-created objects or recorded exact fixture keys. Cleanup is never automatically added to ordinary execution.

Illustrative project manifest (its referenced SQL files are authored alongside the manifest during implementation):

```json
{
  "schemaVersion": 1,
  "id": "example-dev-fixture",
  "scenarios": [
    {
      "id": "fixture-insert",
      "description": "Create a small run-owned table and retain two synthetic rows.",
      "dependsOn": [],
      "requirements": { "objects": [], "checks": [] },
      "effects": {
        "writes": true,
        "transaction": "mixed",
        "external": "none",
        "affectedObjects": ["@fixtures"],
        "backgroundConsumers": []
      },
      "fixtures": {
        "objects": [
          { "binding": "items", "suffix": "ITEMS", "type": "TABLE" }
        ],
        "selectors": []
      },
      "steps": [
        {
          "id": "create-and-seed",
          "sqlFile": "sql/create-and-seed.sql",
          "expected": { "kind": "success" }
        }
      ],
      "checks": [
        {
          "id": "two-retained-rows",
          "sql": "SELECT COUNT(*) FROM ${object.items} WHERE RUN_TAG = :DEVTEST_RUN_ID",
          "expected": 2
        }
      ],
      "observations": [],
      "cleanup": null
    }
  ]
}
```

In this example `create-and-seed.sql` creates the declared generated table with a RUN_TAG column, inserts two synthetic rows tagged by the reserved binding and explicitly commits them. Null cleanup means retained/manual-management support, not an invitation to generate a DELETE statement. Built-in suites additionally ship their reviewed cleanup contracts.

## 7. Execution and transaction model

1. Validate arguments, suite definitions and local paths; resolve the existing target configuration.
2. Allocate a unique local run directory and freeze selected manifests, SQL, checks and implementation input hashes.
3. Capture target identity/capabilities and record planned fixture names. Refuse collisions with pre-existing objects.
4. Write a durable intent event before each database write may start, including scenario/step/input hashes and target identity.
5. Recheck identity in each execution session, then execute through the existing SQLcl launcher. Use existing timeouts, byte budgets, error framing and platform process handling.
6. Capture postconditions using fresh sessions for persistent results. Assertions about same-session uncommitted state must be explicitly marked and cannot prove durable state.
7. Save exact created-object and fixture-key observations, scenario outcome and persistent effects. Continue only independent scenarios when an earlier scenario fails.
8. Finalize JSON/Markdown and retained inventory. Do not perform default cleanup.

A scenario must not expect session-local package state, uncommitted DML or APEX collections to survive into a fresh verification session. Its steps explicitly persist intended retained data before a durable-state assertion. Transaction tests must state which operations should roll back and which effects can survive.

SQL steps can have effects beyond a surrounding rollback. The runner does not claim rollback-only safety. Project cleanup can also commit or invoke business behavior; its effects require their own declaration.

## 8. Retention, optional cleanup and interruption

### Retention

All runs default to retain. Report fixture names, synthetic row identifiers, expected invalid objects, expected states, known automation consumers and cleanup support. Do not put sample data into production baseline bundles or mark it as required reference data.

Every execution uses new names/keys; it never overwrites or automatically reuses an old run. Leaving old fixtures consumes resources. Report retention and creation limits without a background deletion policy or an unverified hard storage-budget promise.

### Optional cleanup

`--cleanup <run-id>` explicitly requests supported cleanup. It loads the saved frozen cleanup contract and inventory, checks the current saved alias against the original observed target identity, and refuses identity changes or missing/corrupt ownership evidence.

- For built-in objects, record pre-creation absence and observed creation, then verify name/type and the saved live definition/status where meaningful before dropping. A matching prefix alone is insufficient ownership evidence.
- If a fixture has been replaced, extended with unexpected dependencies, or cannot be verified, leave it in place and report why. The local run inventory is not a database lock and cannot eliminate every concurrent change window.
- Cleanup uses explicit object/key lists. It does not scan a prefix and drop everything, issue DROP USER, purge a schema, or delete generic business rows.
- Project business-data cleanup is available only when the scenario supplies a reviewed exact-key script and postconditions. Otherwise report `unsupported`; manual management is valid.
- Successful cleanup verifies supported artifacts' absence in a fresh session. Previously verified absence is idempotent. Missing observations are not evidence of absence.
- Cleanup outcomes never rewrite historical functional results. Cleanup does not remove the local report, SQL, receipts or attempt evidence.

### Interruptions and ambiguous writes

There is no automatic replay after timeout, connection loss, process interruption or missing verification evidence. The run records `UNKNOWN` when a write may have occurred, keeps its intent/input evidence, and leaves fixtures in place. A new invocation creates a new run; the old one requires inspection.

Cleanup of an ambiguous creation refuses automated deletion until exact ownership is reconciled; uncertainty must not be converted into permission to remove a matching name. An interrupted cleanup similarly remains incomplete until readback proves its result.

## 9. Files, architecture and packaging

Proposed implementation boundaries:

- A small command/parser and orchestrator under `scripts/`, reached by both team wrappers.
- A strict suite/manifest loader for selection, paths, bindings and effect contracts.
- Built-in scenario definitions/adapters under `scripts/`, using baseline, rollout, migration and check modules.
- A run-evidence/report component for intent events, frozen inputs, outcomes and optional cleanup.
- Project-authored suites and SQL under `dev-tests/`; no arbitrary imported Python plug-ins.

Extract shared helpers only where necessary to reuse real behavior. No broad refactor of migration/rollout or replacement SQLcl bridge belongs to this feature.

Run workspace:

```text
scratch/dev-test/<run-id>/
  inputs/                 # frozen selected suite, steps, checks and input hashes
  project/                # isolated generated migration/rollout project
  sessions/               # SQLcl evidence and bounded diagnostics
  events/                 # intent and observation events, including ambiguous attempts
  inventory.json          # recorded fixture ownership, keys and retained states
  report.json
  report.md
  cleanup/                # appended cleanup attempts and results, when requested
```

Run IDs are validated path components, not arbitrary filesystem paths. Refuse symlinked scratch roots, output paths escaping the run directory and overwrites of existing run evidence. Use existing private scratch permission conventions and supported Windows behavior. Save evidence atomically where practical; a truncated report is incomplete, not a pass.

Use a random per-run uppercase ASCII object prefix with suffixes kept within a 30-byte identifier budget. Freeze final names before writing and check absence. No database registry or global locking table is introduced.

Runtime scripts, core definitions and user documentation are template-owned. Project suites/fixture contracts are project-owned and must survive upgrades. Template-maintenance regression tests remain removable during downstream post-clone cleanup, while the runnable core scenarios stay with operational scripts. Update the ownership manifest, upgrade protections and downstream-cleanup guidance during implementation; this specification itself is covered by `docs/superpowers/**` templateOnly ownership.

## 10. Reports and exit contract

Report schemaVersion 1 includes:

- Run/action IDs and timestamps; selected suite/scenarios; manifest, SQL/check and implementation hashes; Git SHA and dirty-input status.
- Configured profile and independently observed target/version facts, with explicit missing-visibility fields.
- Prerequisite evidence, per-step SQLcl outcomes, per-check expected/actual values, current compilation diagnostics and evidence paths.
- Scenario outcomes, selected coverage and declared/observed persistent effects. Do not claim a complete transitive impact inventory from a few checks.
- Retained object/key inventory, cleanup support/status and unresolved ownership observations.
- Separate functional, coverage and cleanup summaries; limitations and machine-readable diagnostic codes.

Scenario states: `PASSED`, `FAILED`, `BLOCKED`, `NOT_RUN`, `UNKNOWN`. A planned scenario is not executed and has no functional pass. `BLOCKED` means a known unmet prerequisite; `UNKNOWN` means execution/observation cannot establish the result. Unexpected postcondition differences are failures unless an explicit expected-negative scenario proves its intended outcome.

Cleanup states: `NOT_REQUESTED`, `COMPLETE`, `INCOMPLETE`, `REFUSED`, `UNSUPPORTED`. Retained state does not lower a successful functional result when cleanup was not requested.

| Exit | Meaning |
| --- | --- |
| 0 | Plan is complete with selected scenarios eligible; or all selected execution scenarios passed; or requested supported cleanup completed |
| 1 | At least one functional assertion failed, with complete evidence and no higher-priority incomplete/unknown condition |
| 2 | Invalid input, target refusal, blocked/incomplete coverage, unknown execution, unsupported requested cleanup or incomplete/refused cleanup |
| 130 | Interrupted before any scenario/cleanup write may have started |

An interrupt after a possible write returns 2 with unknown/incomplete evidence, preserving the existing principle that a potentially partial apply is not a clean stop. Exit 2 takes precedence over 1 when both occur. Each report lists both categories. `--report` returns the recorded aggregate functional result, not a new execution success; a saved plan returns its eligibility result. Cleanup has its own exit result and does not erase the execution outcome.

Example summary:

```text
DEV / APP: 5 passed, 0 failed, 1 blocked
Coverage: incomplete (compiler-settings privilege unavailable)
Fixtures: retained; cleanup not requested
Report: scratch/dev-test/<run-id>/report.md
Exit: 2
```

Historical receipt checks are not silently mixed into a new run. Projects may report them as dated observations; classifying a mismatch as deliberately superseded requires explicit supporting evidence. Fixture drift, known supersession, a current assertion failure and unknown attribution are distinct observations, not automatic repair instructions.

Console/Markdown reports contain sanitized diagnostics, identifiers, counts and paths, not credentials, bearer tokens, OTPs, request bodies or customer/employee data. Private raw logs are not automatically safe to share; do not echo sensitive payloads in project scenarios or silently promise generic redaction can discover every secret.

## 11. Release compatibility and documentation

- Implement the command/report contract on both current template repositories while retaining each release's manifest/upstream, APEX validation and known limitations.
- Share scenario expectations where behavior is common. A release/capability mismatch is explicit blocked coverage, not an automatic skip counted as success.
- Reuse the existing configured SQLcl minimum and platform launcher requirements. No new runtime package dependency beyond the supported Python standard library is required.
- Existing offline CI stays offline. No automatic scheduled shared-DEV writes or credentials added to GitHub workflows.
- Document execution against an existing owner, project-suite authoring, effect declarations, retention, optional cleanup, interrupted-run inspection, quota/privilege failures and the meaning of incomplete coverage.
- Preserve the difference between fixture mechanism qualification and actual application/business acceptance. Passing the six scenarios certifies only those scenarios on the recorded inputs/target.

## 12. Acceptance criteria

### Offline behavior and regression checks

1. Bash/PowerShell argument, selection, rendering and exit-code parity, including refusal of non-DEV execution.
2. Strict suite/path/binding validation; no client command/connection escape; SQLcl substitution disabled; malformed or changed frozen inputs refused before the next write.
3. Owner/target mismatch refuses before payload execution; unavailable catalog visibility produces unknown prerequisites, not invented absence.
4. Plan never executes scenario SQL or imports; report never connects; ordinary built-in execution never modifies application objects or tracked migration folders.
5. Unique fixtures and pre-existing-name collision refusal; no automatic adoption/reuse of previous fixtures.
6. Negative-test success requires the named failure plus independent postconditions. Missing frames/transport failures never pass.
7. Explicit failure, blocked, not-run and unknown states; exit precedence; retained fixtures with no requested cleanup can coexist with exit 0.
8. Interrupt/timeout evidence survives; no implicit write replay. Exact-list cleanup refuses changed identity, corrupt inventory, prefix-only ownership and unsupported business cleanup.
9. Project suites survive template upgrades/post-clone maintenance cleanup; built-in scenario runtime remains available after template tests are removed.
10. Reports do not expose supplied secret fixtures; raw-log sharing limitations remain documented.

### Real DEV acceptance

1. Run all six scenarios using a non-administrative existing owner on an identified DEV target. Verify fresh-session results and retain their fixtures by default.
2. Inspect retained fixture definitions/rows and evidence after the process exits; a second run creates different names without altering the first run's fixtures.
3. Exercise one simple project SQL scenario with a unique-key lookup and explicit persistent-effect declaration; zero/multiple lookup rows must block before mutation.
4. Exercise one missing-prerequisite path and a controlled allocation/permission failure where the target allows it. Do not grant privileges or change quotas automatically to make tests pass.
5. Request cleanup of one completed built-in run; verify supported fixture removal and preservation of the second run and unrelated objects. Repeat cleanup to prove idempotent verified absence.
6. Demonstrate incomplete/unknown handling on an interrupted fixture write without replay, and refuse automatic cleanup when ownership is unresolved.
7. Record release-specific results separately. DEV clone qualification is sufficient for the agreed scope; unsupported platforms/versions remain declared limits, not silently claimed coverage.

## 13. Before and after

| Area | Current workflow | With this feature |
| --- | --- | --- |
| Live qualification | Purpose-built scripts and manual assembly | Six repeatable core scenarios plus selected project suites |
| Setup | Probe-specific target/schema assumptions | Existing configured DEV owner; no required extra schema |
| Evidence | Several logs and reports to reconcile | Frozen inputs, per-scenario assertions and one linked run report |
| Test retention | Script-specific | Retain by default, exact inventory, optional supported cleanup |
| Project realism | Repeated domain-specific test setup | Explicit prerequisites, fixture selection and effect contracts |
| Failure interpretation | Manual distinction between drift and defects | Current run outcomes separated from historical observations |
| Recovery | Inspect individual script artifacts | Durable intent/outcome evidence; no replay; inventory-based cleanup |

## 14. Evidence informing the design

These are historical repository/chat observations inspected on 2026-10-16, not new live verification. Sources with later corrections take precedence over earlier diagnoses.

- The 26.1 [live fixture summary](../../../../APEX_PROJECT_TEMPLATE_26_1/scratch/live-qualification-2026-10-15/SUMMARY.md) recorded datatype roundtrip, blank-line UPDATE, migration refusal, invalid-view and verified cleanup behavior at `9c233a4`, using administrative sessions. It did not qualify the later compiler-settings correction or normal owner login behavior.
- NATREC [retained fixture impact, I-50](../../../../natrec_team/docs/project/abshry/dev26-issues.md) records demo service areas/automation affecting existing customer addresses; [lessons](../../../../natrec_team/docs/project/abshry/lessons.md) document environment-specific IDs and transaction assumptions.
- The current [worker-dispatch test](../../../../natrec_team/scripts/abshry/dev26/test_deployed_worker_dispatch.sql) rolls back before closing its APEX session; the [deployment log](../../../../natrec_team/docs/project/abshry/dev26-deployment-log.md) records the prior accidental persistence and retained sample orders.
- The final [dependency repair record](../../../../natrec_team/scratch/app115-dev26-sysdata-recompile-2026-10-13/RESULT.md) states the suspected missing grant already existed and no grant was issued. Earlier chat diagnosis is not current grant proof.
- The [receipt review](../../../../natrec_team/docs/project/abshry/agent-records/RESULT_codex_db_review.md) classified 75 historical failed checks as 64 superseded, seven test-data drift, three transfer regressions and one unknown, rather than treating every old mismatch as a current defect.
- NATREC's [API contract](../../../../natrec_team/docs/project/abshry/api-contract.md) distinguishes business reads, persistent logging, mutations, external effects and unresolved routes; its [workflow recovery notes](../../../../natrec_team/app_context/115/recovery.md) identify APEX context/transaction limits.
- Existing template integration points: `scripts/team.sh`, `scripts/team.ps1`, `scripts/db_targets.py`, `scripts/sqlcl_session.py`, `scripts/migration_checks.py`, `scripts/verify_checks.py`, `scripts/rollout.py`, `scripts/baseline.py`, `template-manifest.json`, and `docs/known-limitations.md`.

Cross-repository scratch links are local evidence pointers and may be unavailable on another clone. The normative requirements above are self-contained and do not require those historical artifacts at runtime.
