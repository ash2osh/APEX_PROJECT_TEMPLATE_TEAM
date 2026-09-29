# Multi-schema support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one project manage several standalone database schemas that share one APEX workspace, configured through comma-separated values in the existing `.env` keys, with a single value behaving exactly as today.

**Architecture:** One selection channel, the `PROJECT_SCHEMA` environment variable (set by `--schema` or derived per command). `load_env.sh` / `load_env.ps1` validate the comma lists and, when `PROJECT_SCHEMA` is set, narrow every profile's scalar variables to that schema's entry, so scripts that read scalars keep working. Scripts that must handle several schemas at once (`backup-db`, `export`, `doctor`) split the lists themselves. Python (`db_targets.py`) resolves a `Target` per schema by name for `migrate`, `check-conflicts` and `compare-schema`. Graphify's `DatabaseMirror` indexes every `database/*` schema and follows one synonym hop.

**Tech Stack:** Bash (4.3+), PowerShell 5.1+ twins, Python 3 stdlib (`unittest`), SQLcl scripts, Graphify extractor.

**Spec:** `docs/superpowers/specs/2026-09-29-multi-schema-support-design.md` (read it first; this plan implements it).

## Global Constraints

Copied from the spec. Every task's requirements include these.

- Configuration stays in `.env` using the existing keys with comma-separated values, the same style as `APEX_APP_ID=100,200`. No new key families, no registry file, no per-schema env files.
- A profile's schema, connection and expected-user lists have equal length, are position-aligned, and schemas within a list are unique. A repeated connection is allowed.
- One value in any key is exactly today's behavior; every existing single-schema `.env` stays valid and the existing test suite passes unchanged (except where a task says a test is updated).
- `STAGING_*` and `PROD_*` follow the same list convention. A DEV schema maps to the **same name** in the staging and production lists; a schema absent from a target's list is an error stating it is not deployable there. One exception keeps today's behavior: a project with a **single** DEV schema may name its staging or production schema differently, and that mapping is allowed **only for the project's one configured DEV schema**; any other schema name is refused.
- The loaders validate list **syntax** only (identifiers, SQLcl alias pattern, lengths, uniqueness). They do **not** check production markers and must not start to: `PROD_*` connections legitimately contain "prod". Production markers, read-only production and `DB_ENVIRONMENT` classification stay in `check_db_target` and `resolve_target`, applied to the entry that is selected.
- `TABLES_PREFIXES` and `CODE_PREFIXES` keep their meaning and apply to every schema in the profile. No per-schema prefixes.
- The tables profile mirrors tables for the schemas it lists; the code profile mirrors code for the schemas it lists. Split (`TABLES_SCHEMA=A`, `CODE_SCHEMA=B`) keeps working unchanged.
- `--schema <NAME>` narrows any command to one configured schema. `doctor` and `backup-db` default to all configured schemas. `publish` and `export` run one app at a time.
- `export <id>` reads the app's parsing schema from `APEX_APPLICATIONS` using the first `APEX_SQLCL_CONNECTION` entry (only when several schemas are configured), requires it to be listed in `APEX_PARSING_SCHEMA`, then reconnects with that schema's own connection.
- `publish <id>` requires the app folder's schema, the descriptor's `parsingSchema`, and the live app's parsing schema to agree and to be listed (multi-schema projects only).
- `backup-db` and multi-app `export` install nothing unless every schema verified; one `replace_mirror` call installs them all.
- Migrations live in `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/`. With exactly one code-profile schema the flat layout stays valid and `migrations/<SCHEMA>/` is also accepted; with two or more, `migrations/<SCHEMA>/` is required and a flat folder is refused. One migration changes one schema; a batch cannot mix schemas.
- `compare-schema` requires `--schema` when more than one schema is configured.
- Synonyms: `backup_db.sql` exports each schema's own private synonyms (`PUBLIC` excluded) with `DBMS_METADATA.GET_DDL('SYNONYM', …)` into `database/<SCHEMA>/synonyms/`; the unsafe-filename check, manifest counts and fail-closed install cover it. Sequences are **not** mirrored.
- Graphify: an unqualified name resolves in the app's own schema first; a qualified name resolves in the schema it names; otherwise follow a mirrored synonym one hop via its `FOR "SCHEMA"."OBJECT"` clause. A database-link target (`@`) or anything not in the mirror stays a stub; never invent an edge.
- Preserve all existing safety properties: per-target identity checks, production markers, read-only production, all-or-nothing mirror installs, the drift guard, byte-equality publish verification.
- Keep APEXlang and SQL files in LF line endings. Bash scripts are Bash 4.3+ (they already use `mapfile` and `${var,,}`).
- Out of scope: per-schema prefix filters, sequences, different schema names across environments, cross-schema migrations, and the one-time `epromhq_all` adoption script.

## Review Focus

Failure modes the spec implies that the most likely mistakes would miss. Each has a test in the task named.

1. **Misaligned or malformed lists** (`A,B` schemas with one connection, `A,,B`, trailing comma, duplicate schema, swapped counts across staging keys). Must fail with a message naming the keys, never load partially. Test in Task 2.
2. **`--schema` naming a schema that a profile does not list** (tables profile lists only `ONE`, code lists `ONE,TWO`, `--schema TWO`). `backup-db` must run only the code scope, not crash on empty arrays under `set -u`, and an entirely unlisted schema is refused before any SQLcl call. Tests in Tasks 2 and 4.
3. **Partial failure atomicity** (second schema's SQLcl fails or its manifest is short). Nothing may be installed for any schema. Tests in Tasks 4 and 5.
4. **Disagreeing parsing schema** (folder `apps/ONE/117`, descriptor says `TWO`, or the live app says `THREE`). `publish` must refuse before importing. Test in Task 6.
5. **Flat migration folder in a multi-schema project, and a batch mixing schemas.** Both must be refused with a message telling the user where to move it. Test in Task 7.

## Conventions for the executor

- Work on a new branch from `main`: `git checkout -b feat/multi-schema`. Commit after each task as the steps say. **Do not push, do not merge.** The plan's commit steps are the only commits authorized.
- Run the whole suite with `python3 -m unittest discover -s tests`. Record the baseline result (pass/fail counts) in Task 0 before changing anything, and compare after every task.
- **PowerShell is not installed on this machine** (`which pwsh` prints nothing), so every `*.ps1` test skips. You must still write each PowerShell change as the exact twin of its Bash change, and re-read each `.ps1` diff against its `.sh` twin before committing. State in your final report that PowerShell paths were not executed.
- Tests copy an explicit list of scripts into a temporary checkout (`make_checkout` / `make_publish_fixture` helpers). When a task adds a file a script needs (for example `lookup_app_schema.sql`), add it to every fixture list that copies that script. A failing fixture usually means a missing copied file.
- Follow the existing style: comments explain why, not what; no new dependencies; Python is stdlib-only.
- If a step's expected output differs from what you see, stop and report; do not weaken a test to make it pass.
- **Dry-run status of this plan.** The Bash and Python code and tests in Tasks 1-8 were applied verbatim to a scratch copy of `main` before this plan was handed over. There the whole suite ran 484 tests with no failures other than artifacts of the scratch copy (it had no committed files), and none of the Graphify tests were skipped. Expect roughly 390 + 94 tests, `OK`, with about the same 18 skips (PowerShell). **Not dry-run:** every `.ps1` change, the documentation in Task 9, the Task 6 probe-script edit, and the standalone port in Task 8 Step 6. Give those extra care.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `scripts/db_targets.py` | modify | `split_list`, `configured_schemas`, schema-aware `resolve_target`, `batch_schema` |
| `scripts/load_env.sh`, `scripts/load_env.ps1` | modify | List validation, `PROJECT_SCHEMA` narrowing, `PROJECT_SCHEMAS`, `PROJECT_MULTI_SCHEMA`, `project_env_require_single` |
| `.env.example` | modify | Document list syntax in comments (values stay single) |
| `scripts/check_db_target.sh`, `.ps1` | modify | Optional schema argument, single-schema guard, unlisted-profile error |
| `scripts/team.sh`, `scripts/team.ps1` | modify | `--schema` parsing, multi-schema `doctor`, help text |
| `scripts/backup_db.sh`, `.ps1`, `backup_db.sql` | modify | Loop over schema lists, synonyms scope |
| `scripts/lookup_app_schema.sql` | create | Read-only lookup of an app's parsing schema |
| `scripts/sqlcl_safe.sh`, `scripts/invoke_sqlcl.ps1` | modify | `sqlcl_app_parsing_schema` / `Get-AppParsingSchema` helpers |
| `scripts/export_apps.sh`, `.ps1` | modify | Per-app schema resolution and per-schema connections |
| `scripts/publish_app.sh`, `.ps1`, `scripts/deploy.sh` | modify | Schema selection, agreement checks, by-name staging/prod |
| `.agents/skills/apex-background/probe/run.sh` (+ `.claude` copy) | modify | Refuse multi-schema without `--schema` |
| `scripts/migration_manifest.py` | modify | `migrations/<SCHEMA>/` discovery, `Migration.schema` |
| `scripts/migrate.py`, `scripts/migration_checks.py`, `scripts/compare_schema.py` | modify | `--schema`, batch schema resolution |
| `scripts/graphify_apexlang_extractor.py` | modify | Multi-schema, synonym-aware `DatabaseMirror` |
| `tests/test_db_targets.py` | modify | List resolution tests |
| `tests/test_env_schema_lists.py` | create | Loader tests (Bash, PowerShell parity) |
| `tests/test_multi_schema_cli.py` | create | `team.sh` `--schema`, doctor, backup, export, publish, deploy CLI tests |
| `tests/test_migration_schema_folders.py` | create | Manifest and migrate/check-conflicts schema-folder tests |
| `tests/test_graphify_multi_schema.py` | create | `DatabaseMirror` unit tests |
| `tests/test_graphify_pipeline.py` | modify | Second schema, cross-schema and synonym reads through the real pipeline |
| `tests/test_documentation_contract.py`, `docs/publish-rules.md` | modify | New publish refusals documented |
| `AGENTS.md`, `README.md`, `docs/migration-rules.md`, `migrations/README.md`, `.agents/rules/graphify.md`, `.agents/workflows/graphify.md`, `.agents/skills/initialize-project/SKILL.md` (+ `.claude` copy) | modify | Document the feature |
| `/home/ash/projects/APEX_PROJECT_TEMPLATE` | port | Graphify parts only (Task 8) |

---

### Task 0: Branch and baseline

**Files:** none.

- [ ] **Step 1: Create the branch**

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM
git checkout main && git status --short
git checkout -b feat/multi-schema
```

Expected: on `feat/multi-schema`. The untracked `docs/superpowers/plans/2026-09-23-local-team-e2e-review-remediation.md` may be listed; leave it alone. The spec and this plan are untracked too; commit them in Step 3.

- [ ] **Step 2: Record the baseline**

```bash
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
```

Expected on today's `main`: `Ran 390 tests` then `OK (skipped=18)`. Write the exact counts in your working notes. If anything already fails, stop and report; do not proceed on a red baseline.

- [ ] **Step 3: Commit the spec and plan**

```bash
git add docs/superpowers/specs/2026-09-29-multi-schema-support-design.md docs/superpowers/plans/2026-09-29-multi-schema-support.md
git commit -m "docs: add multi-schema support spec and plan"
```

---

### Task 1: List-aware target resolution in `db_targets.py`

**Files:**
- Modify: `scripts/db_targets.py`
- Test: `tests/test_db_targets.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (later tasks rely on these exact names):
  - `split_list(value: str | None) -> tuple[str, ...]` (empty or `None` → `()`)
  - `configured_schemas(values: Mapping[str, str], environment: str) -> tuple[str, ...]`
  - `resolve_target(values, environment, operation, schema: str | None = None) -> Target` (signature gains a keyword-compatible fourth parameter)
  - `batch_schema(schemas: Iterable[str | None], requested: str | None, values: Mapping[str, str]) -> str | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db_targets.py` (before the `if __name__ == "__main__":` line):

```python
MULTI_ENV = {
    "DB_ENVIRONMENT": "development",
    "CODE_SQLCL_CONNECTION": "conn-a,conn-b,conn-c",
    "CODE_EXPECTED_USER": "AAA,BBB,CCC",
    "CODE_SCHEMA": "AAA,BBB,CCC",
    "STAGING_SQLCL_CONNECTION": "stage-a,stage-b",
    "STAGING_EXPECTED_USER": "SAAA,SBBB",
    "STAGING_SCHEMA": "AAA,BBB",
    "PROD_SQLCL_CONNECTION": "prod-a",
    "PROD_EXPECTED_USER": "PDEPLOY",
    "PROD_SCHEMA": "AAA",
}


class MultiSchemaTargetTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(targets, "explicit environment target resolution is not implemented")
        return targets

    def test_split_list_treats_empty_and_missing_as_no_entries(self) -> None:
        api = self.api()
        self.assertEqual((), api.split_list(""))
        self.assertEqual((), api.split_list(None))
        self.assertEqual(("A",), api.split_list("A"))
        self.assertEqual(("A", "B"), api.split_list("A,B"))

    def test_dev_target_is_selected_by_schema_name(self) -> None:
        target = self.api().resolve_target(MULTI_ENV, "dev", "read", schema="BBB")
        self.assertEqual(("conn-b", "BBB", "BBB"), (target.connection, target.expected_user, target.schema))

    def test_several_schemas_without_a_name_is_an_error_that_points_at_schema(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "dev", "read")
        self.assertIn("--schema", str(raised.exception))

    def test_unknown_schema_lists_the_configured_ones(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "dev", "read", schema="ZZZ")
        self.assertIn("AAA, BBB, CCC", str(raised.exception))

    def test_staging_uses_the_same_schema_name_in_its_own_list(self) -> None:
        target = self.api().resolve_target(MULTI_ENV, "staging", "read", schema="BBB")
        self.assertEqual(("stage-b", "SBBB", "BBB"), (target.connection, target.expected_user, target.schema))

    def test_schema_missing_from_a_target_list_is_not_deployable_there(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "staging", "read", schema="CCC")
        self.assertIn("STAGING_SCHEMA", str(raised.exception))

    def test_single_entry_target_list_is_not_reused_for_a_different_schema(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(MULTI_ENV, "prod", "read", schema="BBB")

    def test_blank_narrowed_profile_reports_the_schema_is_not_listed(self) -> None:
        api = self.api()
        narrowed = {**MULTI_ENV, "STAGING_SQLCL_CONNECTION": "", "STAGING_EXPECTED_USER": "", "STAGING_SCHEMA": ""}
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(narrowed, "staging", "read", schema="CCC")
        self.assertIn("not listed", str(raised.exception))

    def test_unequal_list_lengths_are_rejected(self) -> None:
        api = self.api()
        broken = {**MULTI_ENV, "CODE_EXPECTED_USER": "AAA,BBB"}
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(broken, "dev", "read", schema="AAA")
        self.assertIn("same number of entries", str(raised.exception))

    def test_duplicate_schemas_are_rejected(self) -> None:
        api = self.api()
        broken = {**MULTI_ENV, "CODE_SCHEMA": "AAA,AAA,CCC"}
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(broken, "dev", "read", schema="AAA")

    def test_single_schema_project_still_maps_dev_name_to_a_differently_named_staging_schema(self) -> None:
        # Existing single-schema projects may name staging differently from DEV.
        target = self.api().resolve_target(BASE_ENV, "staging", "read", schema="APP_DEV")
        self.assertEqual("APP_STAGE", target.schema)

    def test_single_schema_project_refuses_an_unknown_schema_for_staging_and_prod(self) -> None:
        # The differently-named-staging exception covers the project's own DEV
        # schema only; it must never turn an arbitrary name into a target.
        api = self.api()
        for environment in ("staging", "prod"):
            with self.subTest(environment=environment):
                with self.assertRaises(api.TargetResolutionError) as raised:
                    api.resolve_target(BASE_ENV, environment, "read", schema="OTHER")
                self.assertIn("OTHER", str(raised.exception))

    def test_single_schema_dev_still_rejects_a_wrong_schema_name(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(BASE_ENV, "dev", "read", schema="OTHER")

    def test_configured_schemas_lists_the_environment_schemas(self) -> None:
        api = self.api()
        self.assertEqual(("AAA", "BBB", "CCC"), api.configured_schemas(MULTI_ENV, "dev"))
        self.assertEqual(("AAA", "BBB"), api.configured_schemas(MULTI_ENV, "staging"))
        with self.assertRaises(api.TargetResolutionError):
            api.configured_schemas(MULTI_ENV, "production")

    def test_batch_schema_rules(self) -> None:
        api = self.api()
        multi = {**MULTI_ENV, "PROJECT_MULTI_SCHEMA": "true"}
        self.assertEqual("BBB", api.batch_schema(["BBB", "BBB"], None, multi))
        self.assertEqual("BBB", api.batch_schema([None], "BBB", BASE_ENV))
        self.assertIsNone(api.batch_schema([None], None, BASE_ENV))
        with self.assertRaises(api.TargetResolutionError):
            api.batch_schema(["AAA", "BBB"], None, multi)
        with self.assertRaises(api.TargetResolutionError) as flat:
            api.batch_schema([None], None, multi)
        self.assertIn("migrations/<SCHEMA>/", str(flat.exception))
        with self.assertRaises(api.TargetResolutionError):
            api.batch_schema(["AAA"], "BBB", multi)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_db_targets -v 2>&1 | tail -30
```

Expected: the new `MultiSchemaTargetTests` fail with `AttributeError: module ... has no attribute 'split_list'` (or `resolve_target() got an unexpected keyword argument 'schema'`). The original `DatabaseTargetTests` still pass.

- [ ] **Step 3: Implement**

In `scripts/db_targets.py`, add `from collections.abc import Iterable, Mapping` (replace the existing `Mapping` import), then replace `resolve_target` and add the helpers. Keep everything above `resolve_target` as is.

```python
SCHEMA_KEYS = {"dev": "CODE_SCHEMA", "staging": "STAGING_SCHEMA", "prod": "PROD_SCHEMA"}


def split_list(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated setting. An empty or missing value has no entries."""
    if not value:
        return ()
    return tuple(value.split(","))


def configured_schemas(values: Mapping[str, str], environment: str) -> tuple[str, ...]:
    """Return the schemas an environment's profile lists, in configured order."""
    try:
        key = SCHEMA_KEYS[environment]
    except KeyError:
        raise TargetResolutionError("environment must be dev, staging, or prod") from None
    return split_list(values.get(key))


def _select_index(
    values: Mapping[str, str],
    environment: str,
    schemas: tuple[str, ...],
    requested: str | None,
    schema_key: str,
) -> int:
    if requested is None:
        if len(schemas) == 1:
            return 0
        raise TargetResolutionError(
            f"{schema_key} lists {len(schemas)} schemas ({', '.join(schemas)}); name one with --schema"
        )
    _validate_identifier(requested, "--schema")
    if requested in schemas:
        return schemas.index(requested)
    dev_schemas = split_list(values.get("CODE_SCHEMA"))
    if environment != "dev" and len(schemas) == 1 and len(dev_schemas) == 1 and requested in dev_schemas:
        # A project with one DEV schema may name its staging or production
        # schema differently. That mapping covers the project's own schema
        # only; several schemas map by name, and any other name is refused.
        return 0
    raise TargetResolutionError(
        f"schema {requested} is not listed in {schema_key} for {environment}; "
        f"configured: {', '.join(schemas)}"
    )


def resolve_target(
    values: Mapping[str, str],
    environment: str,
    operation: str,
    schema: str | None = None,
) -> Target:
    """Resolve a read/migration target; never fall back from stage/prod to DEV.

    Several schemas are configured as position-aligned comma lists. Name the
    schema to pick one; a single-entry list needs no name.
    """
    if operation not in {"read", "migration"}:
        raise TargetResolutionError("operation must be read or migration")
    if environment == "dev":
        connection_key = "CODE_SQLCL_CONNECTION"
        user_key = "CODE_EXPECTED_USER"
        schema_key = "CODE_SCHEMA"
        classification = _required(values, "DB_ENVIRONMENT").lower()
        if classification not in CLASSIFICATIONS:
            raise TargetResolutionError("DB_ENVIRONMENT must be development, test, staging, or production")
        if operation == "migration" and classification not in {"development", "test"}:
            raise TargetResolutionError("DEV migrations require DB_ENVIRONMENT=development or test")
    elif environment in {"staging", "prod"}:
        prefix = "STAGING" if environment == "staging" else "PROD"
        connection_key = f"{prefix}_SQLCL_CONNECTION"
        user_key = f"{prefix}_EXPECTED_USER"
        schema_key = f"{prefix}_SCHEMA"
        classification = "staging" if environment == "staging" else "production"
    else:
        raise TargetResolutionError("environment must be dev, staging, or prod")

    if schema is not None and not values.get(schema_key):
        # The loader blanks a profile that does not list the selected schema.
        raise TargetResolutionError(f"schema {schema} is not listed in {schema_key} for {environment}")
    connections = split_list(_required(values, connection_key))
    users = split_list(_required(values, user_key))
    schemas = split_list(_required(values, schema_key))
    if not len(connections) == len(users) == len(schemas):
        raise TargetResolutionError(
            f"{connection_key}, {user_key} and {schema_key} must list the same number of entries"
        )
    if len(set(schemas)) != len(schemas):
        raise TargetResolutionError(f"{schema_key} must not repeat a schema")
    index = _select_index(values, environment, schemas, schema, schema_key)
    connection, expected_user, target_schema = connections[index], users[index], schemas[index]
    if SQLCL_ALIAS.fullmatch(connection) is None:
        raise TargetResolutionError(f"{connection_key} contains unsupported characters")
    if environment != "prod" and looks_like_production_identity(connection):
        raise TargetResolutionError(f"{connection_key} resembles a production connection; use --env prod after reviewing the target")
    _validate_identifier(expected_user, user_key)
    _validate_identifier(target_schema, schema_key)
    return Target(environment, connection, expected_user, target_schema, classification)


def batch_schema(
    schemas: Iterable[str | None],
    requested: str | None,
    values: Mapping[str, str],
) -> str | None:
    """Pick the one schema a batch of migration folders targets.

    ``schemas`` holds each folder's ``migrations/<SCHEMA>/`` name, or ``None``
    for a flat folder. ``requested`` is ``--schema`` or ``PROJECT_SCHEMA``.
    """
    folder_schemas = set(schemas)
    if len(folder_schemas) > 1:
        raise TargetResolutionError("selected migrations belong to different schemas; run one schema at a time")
    folder_schema = next(iter(folder_schemas), None)
    multi = values.get("PROJECT_MULTI_SCHEMA") == "true" or len(split_list(values.get("CODE_SCHEMA"))) > 1
    if multi and folder_schema is None:
        raise TargetResolutionError(
            "several schemas are configured, so migrations must live under migrations/<SCHEMA>/; "
            "move the folder into its schema directory"
        )
    if folder_schema is not None and requested is not None and folder_schema != requested:
        raise TargetResolutionError(
            f"--schema {requested} does not match the migration folder's schema {folder_schema}"
        )
    return folder_schema if folder_schema is not None else requested
```

Note `BASE_ENV` in the tests has no `PROJECT_MULTI_SCHEMA` and a single `CODE_SCHEMA`, so it is non-multi.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest tests.test_db_targets -v 2>&1 | tail -30
```

Expected: all pass, including the original `DatabaseTargetTests`.

- [ ] **Step 5: Run the wider suite and commit**

```bash
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/db_targets.py tests/test_db_targets.py
git commit -m "feat: resolve database targets from comma-separated schema lists"
```

Expected: same counts as the baseline plus the new tests, `OK`.

---

- [ ] **Step 6 (correction, only if your Task 1 commit predates it): tighten the single-schema fallback**

An earlier revision of this plan let `_select_index` map **any** requested schema to the sole staging or production entry in a single-schema project. That made `resolve_target(BASE_ENV, "staging", "read", schema="OTHER")` succeed. If your `scripts/db_targets.py` still contains `multi = len(schemas) > 1 or len(split_list(values.get("CODE_SCHEMA"))) > 1`, apply the corrected code shown in Step 3 (the `dev_schemas` version) and add the test `test_single_schema_project_refuses_an_unknown_schema_for_staging_and_prod` from Step 1. Run `python3 -m unittest tests.test_db_targets -v` (all pass, including `test_single_schema_project_still_maps_dev_name_to_a_differently_named_staging_schema`), then commit:

```bash
git add scripts/db_targets.py tests/test_db_targets.py
git commit -m "fix: refuse unknown schemas instead of mapping them to a single staging target"
```

---

### Task 2: Environment loader (list validation, `PROJECT_SCHEMA` narrowing)

**Files:**
- Modify: `scripts/load_env.sh`, `scripts/load_env.ps1`, `.env.example`
- Test: `tests/test_env_schema_lists.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks (Python is separate).
- Do **not** add a production-marker check to either loader; see Global Constraints. The loaders validate list syntax only, exactly like today.
- Produces, for every later shell task:
  - Exported `PROJECT_SCHEMAS` (comma list: the union of schemas in the tables, code and APEX profiles, first-seen order) and `PROJECT_MULTI_SCHEMA` (`true` when any list key has more than one entry, else `false`). Both reflect configuration, not the selection.
  - `PROJECT_SCHEMA` is honored on load. When set: it must be an uppercase Oracle identifier and be in `PROJECT_SCHEMAS`; then each profile's three keys are narrowed to that schema's entry. A profile that does not list it gets those three variables set to the **empty string**. `STAGING_*`/`PROD_*` (when their schema key is present) are narrowed the same way, except a non-multi project with a single-entry list is left alone (legacy differently-named staging schema).
  - Bash: `project_env_require_single <label>` (returns non-zero after printing an error when `PROJECT_MULTI_SCHEMA=true` and `PROJECT_SCHEMA` is unset) and `project_env_fail <message>` stay defined after the loader returns.
  - PowerShell: `Assert-ProjectEnvSingleSchema -Label <label>` (throws in the same condition) stays defined.
  - To select a schema mid-script: `export PROJECT_SCHEMA=X` then source the loader again (`$env:PROJECT_SCHEMA = 'X'` then dot-source again).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_env_schema_lists.py`:

```python
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

MULTI = {
    "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=ONE,TWO",
    "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-one,conn-two",
    "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=ONE,TWO",
    "CODE_SCHEMA=DEMO": "CODE_SCHEMA=ONE,TWO",
    "CODE_SQLCL_CONNECTION=docker-demo": "CODE_SQLCL_CONNECTION=conn-one,conn-two",
    "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=ONE,TWO",
    "APEX_PARSING_SCHEMA=DEMO": "APEX_PARSING_SCHEMA=ONE,TWO",
    "APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=conn-one,conn-two",
    "APEX_EXPECTED_USER=DEMO": "APEX_EXPECTED_USER=ONE,TWO",
}

PROBE = (
    'printf "%s|%s|%s|%s|%s|%s|%s\\n" "$PROJECT_MULTI_SCHEMA" "$PROJECT_SCHEMAS" '
    '"$TABLES_SCHEMA" "$TABLES_SQLCL_CONNECTION" "$CODE_SCHEMA" "$CODE_EXPECTED_USER" "$APEX_SQLCL_CONNECTION"'
)


def write_env(directory: Path, replacements: dict[str, str], extra: str = "") -> Path:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    path = directory / ".env"
    path.write_text(text + extra, encoding="utf-8")
    return path


def load(env_path: Path, probe: str = PROBE, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PROJECT_SCHEMA", None)
    if project_schema is not None:
        environment["PROJECT_SCHEMA"] = project_schema
    return subprocess.run(
        ["bash", "-c", f'set -e; source "$1" "$2"; {probe}', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class BashEnvironmentListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_single_values_load_exactly_as_before(self) -> None:
        result = load(write_env(self.directory, {}))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("false|DEMO|DEMO|docker-demo|DEMO|DEMO|docker-demo\n", result.stdout)

    def test_lists_load_and_stay_unnarrowed_without_a_selection(self) -> None:
        result = load(write_env(self.directory, MULTI))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("true|ONE,TWO|ONE,TWO|conn-one,conn-two|ONE,TWO|ONE,TWO|conn-one,conn-two\n", result.stdout)

    def test_project_schema_narrows_every_profile_to_its_entry(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="TWO")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("true|ONE,TWO|TWO|conn-two|TWO|TWO|conn-two\n", result.stdout)

    def test_a_profile_that_does_not_list_the_schema_is_blanked(self) -> None:
        replacements = {
            **MULTI,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        result = load(write_env(self.directory, replacements), project_schema="TWO")
        self.assertEqual(0, result.returncode, result.stderr)
        fields = result.stdout.rstrip("\n").split("|")
        self.assertEqual(["true", "ONE,TWO"], fields[0:2])
        self.assertEqual(["", ""], fields[2:4], "tables schema and connection must be empty")
        self.assertEqual(["TWO", "TWO"], fields[4:6])

    def test_unknown_project_schema_lists_the_configured_ones(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="NOPE")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not configured", result.stderr)
        self.assertIn("ONE,TWO", result.stderr)

    def test_lowercase_project_schema_is_rejected(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="two")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("PROJECT_SCHEMA must be an uppercase Oracle identifier", result.stderr)

    def test_unequal_list_lengths_name_the_keys(self) -> None:
        replacements = {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SQLCL_CONNECTION, CODE_EXPECTED_USER and CODE_SCHEMA must list the same number of entries", result.stderr)

    def test_empty_entries_and_trailing_commas_are_rejected(self) -> None:
        for bad in ("ONE,,TWO", "ONE,TWO,", ",ONE,TWO"):
            with self.subTest(value=bad):
                replacements = {**MULTI, "CODE_SCHEMA=ONE,TWO": f"CODE_SCHEMA={bad}"}
                result = load(write_env(self.directory, replacements))
                self.assertNotEqual(0, result.returncode)
                self.assertIn("CODE_SCHEMA must not contain empty entries", result.stderr)

    def test_duplicate_schemas_are_rejected(self) -> None:
        replacements = {**MULTI, "APEX_PARSING_SCHEMA=ONE,TWO": "APEX_PARSING_SCHEMA=ONE,ONE"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("APEX_PARSING_SCHEMA must not contain duplicate values", result.stderr)

    def test_each_list_entry_is_validated(self) -> None:
        replacements = {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,two"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SCHEMA must be an uppercase Oracle identifier", result.stderr)
        replacements = {**MULTI, "CODE_SQLCL_CONNECTION=conn-one,conn-two": "CODE_SQLCL_CONNECTION=conn-one,bad name"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SQLCL_CONNECTION contains unsupported characters", result.stderr)

    def test_staging_lists_narrow_by_name(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
        result = load(
            write_env(self.directory, MULTI, extra),
            probe='printf "%s|%s|%s\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
            project_schema="TWO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("stage-two|STWO|TWO\n", result.stdout)

    def test_staging_without_the_selected_schema_is_blanked_in_a_multi_project(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one\nSTAGING_EXPECTED_USER=SONE\nSTAGING_SCHEMA=ONE\n"
        result = load(
            write_env(self.directory, MULTI, extra),
            probe='printf "[%s][%s][%s]\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
            project_schema="TWO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[][][]\n", result.stdout)

    def test_single_schema_project_keeps_a_differently_named_staging_schema(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE\n"
        result = load(
            write_env(self.directory, {}, extra),
            probe='printf "%s\\n" "$STAGING_SCHEMA"',
            project_schema="DEMO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("APP_STAGE\n", result.stdout)

    def test_only_the_projects_dev_schema_keeps_a_differently_named_staging_schema(self) -> None:
        # Split profile: tables live in DATA, code and APEX in DEMO. DATA is a
        # configured schema but not the project's DEV (CODE) schema, so it must
        # not map onto the single staging entry.
        replacements = {
            "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=DATA",
            "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=DATA",
        }
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE\n"
        env_path = write_env(self.directory, replacements, extra)
        probe = 'printf "[%s][%s][%s]\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"'
        dev = load(env_path, probe=probe, project_schema="DEMO")
        self.assertEqual(0, dev.returncode, dev.stderr)
        self.assertEqual("[stage-db][STAGE_DEPLOYER][APP_STAGE]\n", dev.stdout)
        other = load(env_path, probe=probe, project_schema="DATA")
        self.assertEqual(0, other.returncode, other.stderr)
        self.assertEqual("[][][]\n", other.stdout)

    def test_several_staging_connections_require_a_staging_schema_list(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\n"
        result = load(write_env(self.directory, MULTI, extra))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("STAGING_SCHEMA is required", result.stderr)

    def test_require_single_refuses_a_multi_project_without_a_selection(self) -> None:
        env_path = write_env(self.directory, MULTI)
        refused = load(env_path, probe='project_env_require_single "unit test"')
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--schema", refused.stderr)
        self.assertIn("ONE,TWO", refused.stderr)
        allowed = load(env_path, probe='project_env_require_single "unit test"; echo ok', project_schema="ONE")
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual("ok\n", allowed.stdout)

    def test_require_single_allows_a_single_schema_project(self) -> None:
        result = load(write_env(self.directory, {}), probe='project_env_require_single "unit test"; echo ok')
        self.assertEqual(0, result.returncode, result.stderr)

    def test_split_profile_without_lists_is_not_multi(self) -> None:
        replacements = {"CODE_SCHEMA=DEMO": "CODE_SCHEMA=OTHER", "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=OTHER"}
        result = load(write_env(self.directory, replacements))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.startswith("false|DEMO,OTHER|"), result.stdout)


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellEnvironmentListTests(unittest.TestCase):
    def run_probe(self, env_path: Path, probe: str, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PROJECT_SCHEMA", None)
        if project_schema is not None:
            environment["PROJECT_SCHEMA"] = project_schema
        script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; {probe}'
        return subprocess.run([PWSH, "-NoProfile", "-Command", script], env=environment, text=True, capture_output=True, check=False)

    def test_lists_narrowing_and_guard_match_the_bash_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), MULTI)
            probe = '"$($env:PROJECT_MULTI_SCHEMA)|$($env:PROJECT_SCHEMAS)|$($env:TABLES_SCHEMA)|$($env:CODE_SQLCL_CONNECTION)"'
            plain = self.run_probe(env_path, probe)
            self.assertEqual(0, plain.returncode, plain.stderr)
            self.assertEqual("true|ONE,TWO|ONE,TWO|conn-one,conn-two", plain.stdout.strip())
            narrowed = self.run_probe(env_path, probe, "TWO")
            self.assertEqual("true|ONE,TWO|TWO|conn-two", narrowed.stdout.strip())
            refused = self.run_probe(env_path, 'Assert-ProjectEnvSingleSchema -Label "unit test"')
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("--schema", refused.stderr)
            unknown = self.run_probe(env_path, probe, "NOPE")
            self.assertNotEqual(0, unknown.returncode)
            self.assertIn("not configured", unknown.stderr)

    def test_unequal_lengths_and_empty_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unequal = write_env(Path(temporary), {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"})
            result = self.run_probe(unequal, "'loaded'")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must list the same number of entries", result.stderr)
            empty = write_env(Path(temporary), {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,,TWO"})
            result = self.run_probe(empty, "'loaded'")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must not contain empty entries", result.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_env_schema_lists -v 2>&1 | tail -30
```

Expected: most Bash tests fail (`lists load` fails because the current loader rejects `ONE,TWO` as an invalid identifier; `PROJECT_MULTI_SCHEMA` is empty). PowerShell tests skip.

- [ ] **Step 3: Implement the Bash loader**

In `scripts/load_env.sh`:

**3a.** The loader must not clear `PROJECT_SCHEMA`. It already only unsets `PROD_*`, `STAGING_*` and the uc-apx keys at the top, so no change is needed there.

**3b.** Replace the entire `for project_env_prefix in PROD STAGING; do … done` block (from that line through its closing `done`, just before `[[ "$APEX_APP_ID" =~ ...`) with the block below. It must sit **after** the definition of `project_env_validate_unique_csv`, so first **move** the `project_env_validate_unique_csv() { … }` function definition (currently after the `APEX_APP_ID` check) to just above this block, and add the new helper functions right after it:

```bash
# Comma lists. One value is the classic single-schema setup; several values
# are position-aligned across a profile's schema, connection and user keys.
project_env_csv_shape_ok() {
  # `read -a` silently drops a trailing empty field, so reject empties here.
  case "$1" in ,*|*,|*,,*) return 1 ;; esac
  return 0
}
project_env_split_csv() {
  local -n project_env_split_out="$1"
  project_env_split_out=()
  [ -z "$2" ] || IFS=',' read -r -a project_env_split_out <<< "$2"
  return 0
}
project_env_count() {
  local -a project_env_count_items=()
  project_env_split_csv project_env_count_items "$1"
  printf '%s' "${#project_env_count_items[@]}"
}
project_env_check_list() {
  local key="$1" kind="$2" value item
  local -a items=()
  value="${!key:-}"
  [ -n "$value" ] || return 0
  project_env_csv_shape_ok "$value" || { project_env_fail "$key must not contain empty entries"; return 1; }
  project_env_split_csv items "$value"
  for item in "${items[@]}"; do
    if [ "$kind" = identifier ] && [[ ! "$item" =~ $project_env_oracle_identifier_regex ]]; then
      project_env_fail "$key must be an uppercase Oracle identifier"
      return 1
    fi
    if [ "$kind" = alias ] && [[ ! "$item" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      project_env_fail "$key contains unsupported characters"
      return 1
    fi
  done
}
project_env_check_aligned() {
  local schema_key="$1" connection_key="$2" user_key="$3"
  local -a schemas=() connections=() users=()
  project_env_split_csv schemas "${!schema_key:-}"
  project_env_split_csv connections "${!connection_key:-}"
  project_env_split_csv users "${!user_key:-}"
  if [ "${#schemas[@]}" -ne "${#connections[@]}" ] || [ "${#schemas[@]}" -ne "${#users[@]}" ]; then
    project_env_fail "$connection_key, $user_key and $schema_key must list the same number of entries"
    return 1
  fi
  project_env_validate_unique_csv "$schema_key" "${!schema_key}"
}

for project_env_prefix in PROD STAGING; do
  project_env_connection_key="${project_env_prefix}_SQLCL_CONNECTION"
  project_env_user_key="${project_env_prefix}_EXPECTED_USER"
  project_env_schema_key="${project_env_prefix}_SCHEMA"
  project_env_connection_seen=false
  project_env_user_seen=false
  project_env_schema_seen=false
  for project_env_seen_key in "${project_env_seen_keys[@]}"; do
    [ "$project_env_seen_key" = "$project_env_connection_key" ] && project_env_connection_seen=true
    [ "$project_env_seen_key" = "$project_env_user_key" ] && project_env_user_seen=true
    [ "$project_env_seen_key" = "$project_env_schema_key" ] && project_env_schema_seen=true
  done
  if [ "$project_env_connection_seen" != "$project_env_user_seen" ]; then
    project_env_fail "$project_env_connection_key and $project_env_user_key must be configured together"
    return 1 2>/dev/null || exit 1
  fi
  if [ "$project_env_schema_seen" = true ] && [ "$project_env_connection_seen" != true ]; then
    project_env_fail "$project_env_schema_key requires $project_env_connection_key and $project_env_user_key"
    return 1 2>/dev/null || exit 1
  fi
  if [ "$project_env_connection_seen" = true ]; then
    project_env_connection_value="${!project_env_connection_key:-}"
    project_env_user_value="${!project_env_user_key:-}"
    if [ -z "${project_env_connection_value//[[:space:]]/}" ] || \
       [ -z "${project_env_user_value//[[:space:]]/}" ]; then
      project_env_fail "$project_env_connection_key and $project_env_user_key must not be empty"
      return 1 2>/dev/null || exit 1
    fi
    project_env_check_list "$project_env_connection_key" alias || { return 1 2>/dev/null || exit 1; }
    project_env_check_list "$project_env_user_key" identifier || { return 1 2>/dev/null || exit 1; }
    if [ "$project_env_schema_seen" = true ]; then
      project_env_check_aligned "$project_env_schema_key" "$project_env_connection_key" "$project_env_user_key" || {
        return 1 2>/dev/null || exit 1
      }
    else
      if [ "$(project_env_count "$project_env_connection_value")" -ne "$(project_env_count "$project_env_user_value")" ]; then
        project_env_fail "$project_env_connection_key and $project_env_user_key must list the same number of entries"
        return 1 2>/dev/null || exit 1
      fi
      if [ "$(project_env_count "$project_env_connection_value")" -gt 1 ]; then
        project_env_fail "$project_env_schema_key is required when $project_env_connection_key lists several connections"
        return 1 2>/dev/null || exit 1
      fi
    fi
  fi
done
```

**3c.** Replace the three trailing validation loops (the `for project_env_key in TABLES_SCHEMA TABLES_EXPECTED_USER CODE_SCHEMA CODE_EXPECTED_USER APEX_PARSING_SCHEMA APEX_EXPECTED_USER; do …`, the `for project_env_key in STAGING_SCHEMA PROD_SCHEMA; do …`, and the `for project_env_key in TABLES_SQLCL_CONNECTION CODE_SQLCL_CONNECTION APEX_SQLCL_CONNECTION; do …` loops) with:

```bash
for project_env_key in TABLES_SCHEMA TABLES_EXPECTED_USER CODE_SCHEMA \
  CODE_EXPECTED_USER APEX_PARSING_SCHEMA APEX_EXPECTED_USER STAGING_SCHEMA PROD_SCHEMA; do
  project_env_check_list "$project_env_key" identifier || { return 1 2>/dev/null || exit 1; }
done
for project_env_key in TABLES_SQLCL_CONNECTION CODE_SQLCL_CONNECTION APEX_SQLCL_CONNECTION; do
  project_env_check_list "$project_env_key" alias || { return 1 2>/dev/null || exit 1; }
done
project_env_check_aligned TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }
project_env_check_aligned CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }
project_env_check_aligned APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }

# The configured schemas, and whether any list names more than one.
project_env_union=()
project_env_multi=false
for project_env_key in TABLES_SCHEMA CODE_SCHEMA APEX_PARSING_SCHEMA; do
  project_env_items=()
  project_env_split_csv project_env_items "${!project_env_key}"
  [ "${#project_env_items[@]}" -le 1 ] || project_env_multi=true
  for project_env_item in "${project_env_items[@]}"; do
    project_env_known=false
    for project_env_union_item in ${project_env_union[@]+"${project_env_union[@]}"}; do
      [ "$project_env_union_item" = "$project_env_item" ] && project_env_known=true
    done
    [ "$project_env_known" = true ] || project_env_union+=("$project_env_item")
  done
done
for project_env_key in STAGING_SCHEMA PROD_SCHEMA STAGING_SQLCL_CONNECTION PROD_SQLCL_CONNECTION; do
  if [ "$(project_env_count "${!project_env_key:-}")" -gt 1 ]; then project_env_multi=true; fi
done
# The project's one DEV schema (CODE_SCHEMA with exactly one entry), captured
# before narrowing rewrites it. Only that schema may map to a differently named
# staging or production schema; the Python resolver applies the same rule.
project_env_dev_schema=""
[ "$(project_env_count "$CODE_SCHEMA")" -ne 1 ] || project_env_dev_schema="$CODE_SCHEMA"
PROJECT_SCHEMAS="$(IFS=,; printf '%s' "${project_env_union[*]}")"
PROJECT_MULTI_SCHEMA="$project_env_multi"
export PROJECT_SCHEMAS PROJECT_MULTI_SCHEMA

project_env_narrow() {
  # <schema-key> <connection-key> <user-key> <strict|lenient>
  local schema_key="$1" connection_key="$2" user_key="$3" mode="$4"
  local index=-1 i
  local -a schemas=() connections=() users=()
  project_env_split_csv schemas "${!schema_key:-}"
  project_env_split_csv connections "${!connection_key:-}"
  project_env_split_csv users "${!user_key:-}"
  for ((i = 0; i < ${#schemas[@]}; i++)); do
    if [ "${schemas[$i]}" = "$PROJECT_SCHEMA" ]; then index="$i"; break; fi
  done
  if [ "$index" -ge 0 ]; then
    export "$schema_key=${schemas[$index]}" "$connection_key=${connections[$index]}" "$user_key=${users[$index]}"
  elif [ "$mode" = lenient ] && [ "$project_env_multi" != true ] && [ "${#schemas[@]}" -eq 1 ] \
      && [ -n "$project_env_dev_schema" ] && [ "$PROJECT_SCHEMA" = "$project_env_dev_schema" ]; then
    # A project with one DEV schema may name staging or production differently,
    # for that schema only.
    :
  else
    export "$schema_key=" "$connection_key=" "$user_key="
  fi
}

if [ -n "${PROJECT_SCHEMA:-}" ]; then
  if [[ ! "$PROJECT_SCHEMA" =~ $project_env_oracle_identifier_regex ]]; then
    project_env_fail "PROJECT_SCHEMA must be an uppercase Oracle identifier"
    return 1 2>/dev/null || exit 1
  fi
  project_env_selected_known=false
  project_env_items=()
  project_env_split_csv project_env_items "$PROJECT_SCHEMAS"
  for project_env_item in "${project_env_items[@]}"; do
    [ "$project_env_item" = "$PROJECT_SCHEMA" ] && project_env_selected_known=true
  done
  if [ "$project_env_selected_known" != true ]; then
    project_env_fail "schema $PROJECT_SCHEMA is not configured; configured schemas: $PROJECT_SCHEMAS"
    return 1 2>/dev/null || exit 1
  fi
  project_env_narrow TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER strict
  project_env_narrow CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER strict
  project_env_narrow APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER strict
  for project_env_prefix in STAGING PROD; do
    project_env_schema_key="${project_env_prefix}_SCHEMA"
    if [ -n "${!project_env_schema_key:-}" ]; then
      project_env_narrow "$project_env_schema_key" "${project_env_prefix}_SQLCL_CONNECTION" "${project_env_prefix}_EXPECTED_USER" lenient
    fi
  done
fi

# Kept after the load so a script can refuse to guess between schemas.
project_env_require_single() {
  if [ "${PROJECT_MULTI_SCHEMA:-false}" = true ] && [ -z "${PROJECT_SCHEMA:-}" ]; then
    project_env_fail "$1 needs one schema because several are configured ($PROJECT_SCHEMAS); pass --schema <NAME>"
    return 1
  fi
  return 0
}
```

**3d.** Update the cleanup at the very end. Add these to the `unset` variable lines: `project_env_union project_env_multi project_env_items project_env_item project_env_known project_env_union_item project_env_selected_known project_env_count_items project_env_dev_schema`. Replace the last two lines (the comment and `unset -f project_env_fail project_env_validate_unique_csv`) with:

```bash
# project_env_fail and project_env_require_single stay defined for callers that
# refuse an ambiguous schema; the parsing helpers do not.
unset -f project_env_validate_unique_csv project_env_csv_shape_ok project_env_split_csv \
  project_env_count project_env_check_list project_env_check_aligned project_env_narrow
```

Careful: `project_env_narrow` reads `project_env_multi` and `project_env_oracle_identifier_regex`, so the `unset` of those variables (lines already present) must stay **after** the narrowing block, which it is (they are at the end of the file).

Also update the file header comment of `unset project_env_repo_root` neighbours only as needed; do not change anything else.

- [ ] **Step 4: Implement the PowerShell loader (twin)**

In `scripts/load_env.ps1`:

**4a.** Do **not** add `PROJECT_SCHEMA` to the removed-at-top list (it is caller-provided).

**4b.** Inside the `foreach ($projectEnvPrefix in @("PROD", "STAGING")) { … }` block, replace the inner `if ($projectEnvConnectionSeen) { … }` body with:

```powershell
  if ($projectEnvConnectionSeen) {
    $projectEnvConnectionValue = [Environment]::GetEnvironmentVariable($projectEnvConnectionKey, "Process")
    $projectEnvUserValue = [Environment]::GetEnvironmentVariable($projectEnvUserKey, "Process")
    if ([string]::IsNullOrWhiteSpace($projectEnvConnectionValue) -or
        [string]::IsNullOrWhiteSpace($projectEnvUserValue)) {
      throw "$projectEnvConnectionKey and $projectEnvUserKey must not be empty"
    }
    Assert-ProjectEnvList -Name $projectEnvConnectionKey -Kind alias
    Assert-ProjectEnvList -Name $projectEnvUserKey -Kind identifier
    if ($projectEnvSchemaSeen) {
      Assert-ProjectEnvTriple -SchemaKey $projectEnvSchemaKey -ConnectionKey $projectEnvConnectionKey -UserKey $projectEnvUserKey
    } else {
      $projectEnvConnectionCount = @(Split-ProjectEnvList $projectEnvConnectionValue).Count
      if ($projectEnvConnectionCount -ne @(Split-ProjectEnvList $projectEnvUserValue).Count) {
        throw "$projectEnvConnectionKey and $projectEnvUserKey must list the same number of entries"
      }
      if ($projectEnvConnectionCount -gt 1) {
        throw "$projectEnvSchemaKey is required when $projectEnvConnectionKey lists several connections"
      }
    }
  }
```

**4c.** Move the definition of `Assert-ProjectEnvUniqueCsv` above that `foreach` block and add the helpers with it (before the `foreach ($projectEnvPrefix …)` line):

```powershell
function Split-ProjectEnvList([string]$Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
function Assert-ProjectEnvList([string]$Name, [string]$Kind) {
  $projectEnvListValue = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrEmpty($projectEnvListValue)) { return }
  if ($projectEnvListValue -cmatch '(^,|,$|,,)') { throw "$Name must not contain empty entries" }
  foreach ($projectEnvListItem in @(Split-ProjectEnvList $projectEnvListValue)) {
    if ($Kind -eq "identifier" -and $projectEnvListItem -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
      throw "$Name must be an uppercase Oracle identifier"
    }
    if ($Kind -eq "alias" -and $projectEnvListItem -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
      throw "$Name contains unsupported characters"
    }
  }
}
function Assert-ProjectEnvTriple([string]$SchemaKey, [string]$ConnectionKey, [string]$UserKey) {
  $projectEnvSchemas = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($SchemaKey, "Process")))
  $projectEnvConnections = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($ConnectionKey, "Process")))
  $projectEnvUsers = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($UserKey, "Process")))
  if ($projectEnvSchemas.Count -ne $projectEnvConnections.Count -or $projectEnvSchemas.Count -ne $projectEnvUsers.Count) {
    throw "$ConnectionKey, $UserKey and $SchemaKey must list the same number of entries"
  }
  Assert-ProjectEnvUniqueCsv -Name $SchemaKey -Value ([Environment]::GetEnvironmentVariable($SchemaKey, "Process"))
}
```

**PowerShell array-return rule (applies to `Split-ProjectEnvList`, `Split-BackupList` and `Split-ExportList`).** An earlier revision of this plan used `return ,@(...)` in these helpers. That is wrong for the way they are called: the comma emits the whole array as a single pipeline object, so `@(Split-ProjectEnvList "A,B").Count` is 1, not 2, and an empty list becomes a one-element array. If your `load_env.ps1` still has `return ,@()`, replace the two `return` lines with `if ([string]::IsNullOrEmpty($Value)) { return }` and `return $Value.Split(',')`, and make sure every caller wraps the call in `@( )`, including the `foreach` in `Assert-ProjectEnvList`. Because `pwsh` is not installed, reason about this from the semantics, not by running it.

**4d.** Replace everything from the line `foreach ($projectEnvKey in @("TABLES_SCHEMA", "TABLES_EXPECTED_USER", "CODE_SCHEMA", …` through the end of the `try` block's last `Remove-Item -Path Function:Assert-ProjectEnvUniqueCsv …` line with:

```powershell
foreach ($projectEnvKey in @("TABLES_SCHEMA", "TABLES_EXPECTED_USER", "CODE_SCHEMA", "CODE_EXPECTED_USER",
    "APEX_PARSING_SCHEMA", "APEX_EXPECTED_USER", "STAGING_SCHEMA", "PROD_SCHEMA")) {
  Assert-ProjectEnvList -Name $projectEnvKey -Kind identifier
}
foreach ($projectEnvKey in @("TABLES_SQLCL_CONNECTION", "CODE_SQLCL_CONNECTION", "APEX_SQLCL_CONNECTION")) {
  Assert-ProjectEnvList -Name $projectEnvKey -Kind alias
}
Assert-ProjectEnvTriple -SchemaKey TABLES_SCHEMA -ConnectionKey TABLES_SQLCL_CONNECTION -UserKey TABLES_EXPECTED_USER
Assert-ProjectEnvTriple -SchemaKey CODE_SCHEMA -ConnectionKey CODE_SQLCL_CONNECTION -UserKey CODE_EXPECTED_USER
Assert-ProjectEnvTriple -SchemaKey APEX_PARSING_SCHEMA -ConnectionKey APEX_SQLCL_CONNECTION -UserKey APEX_EXPECTED_USER

# The configured schemas, and whether any list names more than one.
$projectEnvUnion = @()
$projectEnvMulti = $false
foreach ($projectEnvKey in @("TABLES_SCHEMA", "CODE_SCHEMA", "APEX_PARSING_SCHEMA")) {
  $projectEnvItems = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($projectEnvKey, "Process")))
  if ($projectEnvItems.Count -gt 1) { $projectEnvMulti = $true }
  foreach ($projectEnvItem in $projectEnvItems) {
    if ($projectEnvUnion -cnotcontains $projectEnvItem) { $projectEnvUnion += $projectEnvItem }
  }
}
foreach ($projectEnvKey in @("STAGING_SCHEMA", "PROD_SCHEMA", "STAGING_SQLCL_CONNECTION", "PROD_SQLCL_CONNECTION")) {
  if (@(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($projectEnvKey, "Process"))).Count -gt 1) {
    $projectEnvMulti = $true
  }
}
# The project's one DEV schema (CODE_SCHEMA with exactly one entry), captured
# before narrowing rewrites it. Only that schema may map to a differently named
# staging or production schema; the Python resolver applies the same rule.
$projectEnvDevSchema = ""
if (@(Split-ProjectEnvList $env:CODE_SCHEMA).Count -eq 1) { $projectEnvDevSchema = $env:CODE_SCHEMA }
$env:PROJECT_SCHEMAS = ($projectEnvUnion -join ",")
$env:PROJECT_MULTI_SCHEMA = if ($projectEnvMulti) { "true" } else { "false" }

function Set-ProjectEnvNarrow([string]$SchemaKey, [string]$ConnectionKey, [string]$UserKey, [string]$Mode) {
  $schemas = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($SchemaKey, "Process")))
  $connections = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($ConnectionKey, "Process")))
  $users = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($UserKey, "Process")))
  $index = -1
  for ($i = 0; $i -lt $schemas.Count; $i++) {
    if ($schemas[$i] -ceq $env:PROJECT_SCHEMA) { $index = $i; break }
  }
  if ($index -ge 0) {
    Set-Item -LiteralPath "Env:$SchemaKey" -Value $schemas[$index]
    Set-Item -LiteralPath "Env:$ConnectionKey" -Value $connections[$index]
    Set-Item -LiteralPath "Env:$UserKey" -Value $users[$index]
  } elseif ($Mode -eq "lenient" -and -not $projectEnvMulti -and $schemas.Count -eq 1 -and
      $projectEnvDevSchema -ne "" -and $env:PROJECT_SCHEMA -ceq $projectEnvDevSchema) {
    # A project with one DEV schema may name staging or production differently,
    # for that schema only.
  } else {
    # An empty value would be removed by Set-Item on some hosts, so set it
    # through the .NET API, which keeps a defined-but-empty variable.
    foreach ($name in @($SchemaKey, $ConnectionKey, $UserKey)) {
      [Environment]::SetEnvironmentVariable($name, "", "Process")
    }
  }
}

if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) {
  if ($env:PROJECT_SCHEMA -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
    throw "PROJECT_SCHEMA must be an uppercase Oracle identifier"
  }
  if (@($projectEnvUnion) -cnotcontains $env:PROJECT_SCHEMA) {
    throw "schema $($env:PROJECT_SCHEMA) is not configured; configured schemas: $($env:PROJECT_SCHEMAS)"
  }
  Set-ProjectEnvNarrow TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER strict
  Set-ProjectEnvNarrow CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER strict
  Set-ProjectEnvNarrow APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER strict
  foreach ($projectEnvPrefix in @("STAGING", "PROD")) {
    if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("${projectEnvPrefix}_SCHEMA", "Process"))) {
      Set-ProjectEnvNarrow "${projectEnvPrefix}_SCHEMA" "${projectEnvPrefix}_SQLCL_CONNECTION" "${projectEnvPrefix}_EXPECTED_USER" lenient
    }
  }
}

# Kept after the load so a script can refuse to guess between schemas.
function Assert-ProjectEnvSingleSchema([string]$Label) {
  if ($env:PROJECT_MULTI_SCHEMA -eq "true" -and [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) {
    throw "$Label needs one schema because several are configured ($($env:PROJECT_SCHEMAS)); pass --schema <NAME>"
  }
}

# Mirror load_env.sh, which unsets its own temporaries after a successful load.
Remove-Variable -Name projectEnvRepoRoot, projectEnvSeen, projectEnvAllowed,
  projectEnvRequired, projectEnvLine, projectEnvKey, projectEnvValue,
  projectEnvPrefixValue, projectEnvPrefixItem, projectEnvQuoted,
  projectEnvPrefix, projectEnvConnectionKey, projectEnvUserKey, projectEnvSchemaKey,
  projectEnvConnectionSeen, projectEnvUserSeen, projectEnvConnectionValue,
  projectEnvUserValue, projectEnvSchemaSeen, projectEnvSchemaValue,
  projectEnvRootRelative, projectEnvUnion, projectEnvMulti, projectEnvItems,
  projectEnvItem, projectEnvConnectionCount, projectEnvDevSchema `
  -ErrorAction SilentlyContinue
Remove-Item -Path Function:Assert-ProjectEnvUniqueCsv, Function:Split-ProjectEnvList,
  Function:Assert-ProjectEnvList, Function:Assert-ProjectEnvTriple,
  Function:Set-ProjectEnvNarrow -ErrorAction SilentlyContinue
```

Keep the existing `}` / `finally { … }` lines that follow. Note `Set-ProjectEnvNarrow` reads `$projectEnvMulti` from the caller scope (dot-sourced), so it must be removed only at the end, which the block above does.

One PowerShell detail to verify by reading, since it cannot be run here: `Set-Item -LiteralPath "Env:X" -Value ""` deletes the variable on Windows PowerShell 5.1, which is why the blank case uses `[Environment]::SetEnvironmentVariable`. Confirm every other narrowing assignment uses non-empty values.

- [ ] **Step 5: Document the syntax in `.env.example`**

Add a comment block above the `TABLES_SCHEMA` block in `.env.example` (comments only; the values stay single):

```
# Several schemas in one workspace: give any of the schema, connection and
# expected-user keys below a comma-separated list. The three keys of a profile
# must list the same number of entries, in the same order, and each schema must
# appear once. Example:
#   CODE_SCHEMA=EPROMHQ,TMS
#   CODE_SQLCL_CONNECTION=42_epromhq,42_tms
#   CODE_EXPECTED_USER=EPROMHQ,TMS
# Prefix filters apply to every schema in the profile. STAGING_* and PROD_*
# take the same lists, and a schema keeps the same name in every environment.
# Pass --schema <NAME> to scripts/team.sh to run one schema.
```

Confirm no line in the block starts with `KEY=` (the loader would try to parse it); every line starts with `#`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
python3 -m unittest tests.test_env_schema_lists -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
```

Expected: the new Bash tests pass; the whole suite matches the baseline plus new tests. If `test_team_cli` env-loader tests fail, the single-value path regressed; fix the loader, not the tests.

- [ ] **Step 7: Commit**

```bash
git add scripts/load_env.sh scripts/load_env.ps1 .env.example tests/test_env_schema_lists.py
git commit -m "feat: load comma-separated schema lists and narrow to PROJECT_SCHEMA"
```

---

### Task 3: `--schema`, `check_db_target`, and multi-schema `doctor`

**Files:**
- Modify: `scripts/check_db_target.sh`, `scripts/check_db_target.ps1`, `scripts/team.sh`, `scripts/team.ps1`
- Test: `tests/test_multi_schema_cli.py` (create)

**Interfaces:**
- Consumes: Task 2 (`PROJECT_SCHEMA`, `PROJECT_SCHEMAS`, `PROJECT_MULTI_SCHEMA`, `project_env_require_single`, `Assert-ProjectEnvSingleSchema`).
- Produces:
  - `scripts/check_db_target.sh <read|write> <tables|code|apex> [schema]` and `check_db_target.ps1 -Operation … -Target … [-Schema NAME]`. With no schema in a multi-schema project it refuses; with a schema that the profile does not list it exits 2 with `the <target> profile does not list schema <NAME>`.
  - `team.sh` / `team.ps1` accept `--schema <NAME>` (also `--schema=NAME` in Bash) on every command except `upgrade-template`, validate it, strip it from the argument list, and export it as `PROJECT_SCHEMA` for the child scripts.
  - `team.sh doctor` checks every distinct `(connection, expected user, schema)` across the apex, tables and code profiles, honoring `--schema`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_multi_schema_cli.py` with the shared fixture helpers and the first tests. Later tasks append to this file, so write the helpers generically.

```python
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

TWO_SCHEMAS = {
    "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=ONE,TWO",
    "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-one,conn-two",
    "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=ONE,TWO",
    "CODE_SCHEMA=DEMO": "CODE_SCHEMA=ONE,TWO",
    "CODE_SQLCL_CONNECTION=docker-demo": "CODE_SQLCL_CONNECTION=conn-one,conn-two",
    "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=ONE,TWO",
    "APEX_PARSING_SCHEMA=DEMO": "APEX_PARSING_SCHEMA=ONE,TWO",
    "APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=conn-one,conn-two",
    "APEX_EXPECTED_USER=DEMO": "APEX_EXPECTED_USER=ONE,TWO",
}


def env_text(replacements: dict[str, str]) -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Multi Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "multi@example.test"], check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)


class DoctorCliTests(unittest.TestCase):
    NAMES = ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "verify_db_access.sql", "sqlcl_safe.sh")

    def make_checkout(self, root: Path, replacements: dict[str, str]) -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text(replacements), encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        calls = root / "sql-calls.txt"
        fake_sql = fake_bin / "sql"
        # Positional: -S -noupdates -name <conn> @<script> <schema> <env> <user>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s|%s|%s\\n' \"$4\" \"$6\" \"$8\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_CONNECTION:-}\" == \"$4\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "printf 'APEX_DOCTOR_VERIFIED:%s\\n' \"$8\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(calls)
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "team.sh", environment

    def run_team(self, script: Path, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_single_schema_doctor_checks_one_identity_and_prints_the_old_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), {})
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Doctor checks passed for the configured DEV connection.", result.stdout)
            self.assertEqual(["docker-demo|DEMO|DEMO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_multi_schema_doctor_checks_every_schema_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-one|ONE|ONE", "conn-two|TWO|TWO"], sorted(calls))
            self.assertIn("Doctor checks passed for all 2 configured DEV schema connections.", result.stdout)

    def test_schema_option_narrows_doctor_to_one_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema", "TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|TWO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_one_failing_schema_fails_doctor_after_checking_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            environment["FAKE_FAIL_CONNECTION"] = "conn-one"
            result = self.run_team(script, environment, "doctor")
            self.assertNotEqual(0, result.returncode)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-one|ONE|ONE", "conn-two|TWO|TWO"], sorted(calls))
            self.assertIn("ONE", result.stderr)

    def test_unknown_or_malformed_schema_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            for value, expected in (("NOPE", "not configured"), ("two", "uppercase Oracle identifier")):
                with self.subTest(schema=value):
                    result = self.run_team(script, environment, "doctor", "--schema", value)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(expected, result.stderr)
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

    def test_schema_option_requires_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema")
            self.assertEqual(2, result.returncode)
            self.assertIn("--schema requires a schema name", result.stderr)

    def test_split_profile_project_still_checks_each_distinct_identity(self) -> None:
        # Tables in one schema, code and APEX in another: no --schema needed.
        replacements = {
            "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=DATA",
            "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=DATA",
            "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-data",
        }
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), replacements)
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-data|DATA|DATA", "docker-demo|DEMO|DEMO"], sorted(calls))


class CheckDbTargetTests(unittest.TestCase):
    def run_check(self, replacements: dict[str, str], *arguments: str, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("check_db_target.sh", "load_env.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (root / ".env").write_text(env_text(replacements), encoding="utf-8")
            environment = os.environ.copy()
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment.pop("PROJECT_SCHEMA", None)
            if project_schema:
                environment["PROJECT_SCHEMA"] = project_schema
            return subprocess.run(
                ["bash", str(scripts / "check_db_target.sh"), *arguments],
                env=environment, text=True, capture_output=True, check=False,
            )

    def test_single_schema_check_is_unchanged(self) -> None:
        self.assertEqual(0, self.run_check({}, "read", "apex").returncode)

    def test_multi_schema_without_a_schema_is_refused(self) -> None:
        result = self.run_check(TWO_SCHEMAS, "read", "apex")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--schema", result.stderr)

    def test_schema_argument_selects_the_profile_entry(self) -> None:
        self.assertEqual(0, self.run_check(TWO_SCHEMAS, "read", "code", "TWO").returncode)

    def test_a_profile_that_does_not_list_the_schema_is_refused(self) -> None:
        replacements = {
            **TWO_SCHEMAS,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        result = self.run_check(replacements, "read", "tables", "TWO")
        self.assertEqual(2, result.returncode)
        self.assertIn("the tables profile does not list schema TWO", result.stderr)

    def test_production_looking_connection_is_still_refused_per_entry(self) -> None:
        replacements = {**TWO_SCHEMAS, "APEX_SQLCL_CONNECTION=conn-one,conn-two": "APEX_SQLCL_CONNECTION=conn-one,conn-prod"}
        result = self.run_check(replacements, "read", "apex", "TWO")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("resembles production", result.stderr)


if __name__ == "__main__":
    unittest.main()
```

`env_text` asserts each replacement exists in `.env.example`, so the fixture fails loudly if the example's defaults change.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_multi_schema_cli -v 2>&1 | tail -30
```

Expected: failures (`--schema` unknown, multi doctor checks nothing per schema, etc.). `test_single_schema_doctor_checks_one_identity…` may already pass; that is fine.

- [ ] **Step 3: Implement `check_db_target.sh`**

Replace the top of `scripts/check_db_target.sh` through the `case "$TARGET" in … esac` block with:

```bash
OPERATION="${1:?usage: check_db_target.sh <read|write> <tables|code|apex> [schema]}"
TARGET="${2:?usage: check_db_target.sh <read|write> <tables|code|apex> [schema]}"
SCHEMA_ARG="${3:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# A schema argument is the same selection as --schema; the loader narrows on it.
if [ -n "$SCHEMA_ARG" ]; then export PROJECT_SCHEMA="$SCHEMA_ARG"; fi
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"

case "$OPERATION" in
  read|write) ;;
  *) echo "unsupported database operation class: $OPERATION" >&2; exit 2 ;;
esac

case "$TARGET" in
  tables) TARGET_CONNECTION="$TABLES_SQLCL_CONNECTION" ;;
  code)   TARGET_CONNECTION="$CODE_SQLCL_CONNECTION" ;;
  apex)   TARGET_CONNECTION="$APEX_SQLCL_CONNECTION" ;;
  *) echo "unsupported database target: $TARGET" >&2; exit 2 ;;
esac

project_env_require_single "check_db_target ($TARGET)" || exit 2
if [ -z "$TARGET_CONNECTION" ]; then
  echo "the $TARGET profile does not list schema ${PROJECT_SCHEMA:-}" >&2
  exit 2
fi
```

Leave the production-marker and `DB_ENVIRONMENT=production` sections after it unchanged.

- [ ] **Step 4: Implement `check_db_target.ps1` (twin)**

Change the `param` block and the start of the script:

```powershell
param(
  [Parameter(Mandatory = $true)][ValidateSet("read", "write")][string]$Operation,
  [Parameter(Mandatory = $true)][ValidateSet("tables", "code", "apex")][string]$Target,
  [string]$Schema
)

$ErrorActionPreference = "Stop"
# A schema argument is the same selection as --schema; the loader narrows on it.
if (-not [string]::IsNullOrEmpty($Schema)) { $env:PROJECT_SCHEMA = $Schema }
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE

switch ($Target) {
  "tables" { $targetConnection = $env:TABLES_SQLCL_CONNECTION }
  "code"   { $targetConnection = $env:CODE_SQLCL_CONNECTION }
  "apex"   { $targetConnection = $env:APEX_SQLCL_CONNECTION }
}

Assert-ProjectEnvSingleSchema -Label "check_db_target ($Target)"
if ([string]::IsNullOrEmpty($targetConnection)) {
  throw "the $Target profile does not list schema $($env:PROJECT_SCHEMA)"
}
```

Leave the production-pattern block below unchanged.

- [ ] **Step 5: Implement `--schema` parsing and `doctor` in `team.sh`**

In `scripts/team.sh`, immediately after `command_name="$1"` / `shift` and before `REPO_ROOT=`, add:

```bash
# --schema NAME is the one selection channel: strip it and export PROJECT_SCHEMA
# so every child script's loader narrows to that schema.
if [ "$command_name" != upgrade-template ]; then
  schema_filtered=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --schema)
        [ "$#" -ge 2 ] || fail "--schema requires a schema name"
        export PROJECT_SCHEMA="$2"
        shift 2
        ;;
      --schema=*)
        export PROJECT_SCHEMA="${1#--schema=}"
        shift
        ;;
      *)
        schema_filtered+=("$1")
        shift
        ;;
    esac
  done
  set -- ${schema_filtered[@]+"${schema_filtered[@]}"}
  if [ -n "${PROJECT_SCHEMA:-}" ]; then
    schema_pattern='^[A-Z][A-Z0-9_$#]{0,127}$'
    [[ "$PROJECT_SCHEMA" =~ $schema_pattern ]] || fail "--schema must be an uppercase Oracle identifier"
  fi
fi
```

Add `--schema <NAME>` to the usage text in `usage()`: append after the `--help` line's block a new line in the `Commands:` list heading area:

```
Options:
  --schema <NAME>                             Run one configured schema (any command except upgrade-template)
```

(place it between the commands list and `--help`). The existing test only asserts the substrings it already checks.

Replace the whole `doctor)` case body with:

```bash
  doctor)
    [ "$#" -eq 0 ] || fail "doctor does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
    # shellcheck source=load_env.sh
    source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
    # shellcheck source=sqlcl_safe.sh
    source "$REPO_ROOT/scripts/sqlcl_safe.sh"
    mkdir -p "$REPO_ROOT/scratch"

    doctor_one() {
      # Never let SQLcl start in the caller's directory: SQLcl executes a
      # login.sql found there before doctor.sql.
      local connection="$1" expected_user="$2" schema="$3" workdir stdin output status=0
      workdir="$(mktemp -d "$REPO_ROOT/scratch/sqlcl-doctor.XXXXXX")"
      stdin="$workdir/.sqlcl-stdin"
      : > "$stdin"
      output="$workdir/sqlcl-output.log"
      if ! invoke_sqlcl_safe "$workdir" \
        -S -noupdates -name "$connection" \
        "@$REPO_ROOT/scripts/doctor.sql" \
        "$schema" "$DB_ENVIRONMENT" "$expected_user" \
        < "$stdin" > "$output" 2>&1; then
        cat "$output" >&2
        printf 'team error: SQLcl doctor check failed for schema %s (connection %s)\n' "$schema" "$connection" >&2
        status=1
      else
        cat "$output"
        if ! grep -Fxq "APEX_DOCTOR_VERIFIED:$expected_user" "$output"; then
          printf 'team error: SQLcl did not verify the doctor script for schema %s; the result is unknown\n' "$schema" >&2
          status=1
        fi
      fi
      rm -rf -- "$workdir"
      return "$status"
    }

    doctor_seen="|"
    doctor_total=0
    doctor_failed=0
    for doctor_profile in apex tables code; do
      case "$doctor_profile" in
        apex)   doctor_schemas="$APEX_PARSING_SCHEMA"; doctor_connections="$APEX_SQLCL_CONNECTION"; doctor_users="$APEX_EXPECTED_USER" ;;
        tables) doctor_schemas="$TABLES_SCHEMA"; doctor_connections="$TABLES_SQLCL_CONNECTION"; doctor_users="$TABLES_EXPECTED_USER" ;;
        code)   doctor_schemas="$CODE_SCHEMA"; doctor_connections="$CODE_SQLCL_CONNECTION"; doctor_users="$CODE_EXPECTED_USER" ;;
      esac
      [ -n "$doctor_schemas" ] || continue
      IFS=',' read -r -a doctor_schema_list <<< "$doctor_schemas"
      IFS=',' read -r -a doctor_connection_list <<< "$doctor_connections"
      IFS=',' read -r -a doctor_user_list <<< "$doctor_users"
      for ((doctor_index = 0; doctor_index < ${#doctor_schema_list[@]}; doctor_index++)); do
        doctor_key="${doctor_connection_list[$doctor_index]}|${doctor_user_list[$doctor_index]}|${doctor_schema_list[$doctor_index]}"
        case "$doctor_seen" in *"|$doctor_key|"*) continue ;; esac
        doctor_seen="$doctor_seen$doctor_key|"
        doctor_total=$((doctor_total + 1))
        PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" read "$doctor_profile" \
          "${doctor_schema_list[$doctor_index]}"
        if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
          printf 'Doctor: schema %s via connection %s as %s\n' \
            "${doctor_schema_list[$doctor_index]}" "${doctor_connection_list[$doctor_index]}" "${doctor_user_list[$doctor_index]}"
        fi
        doctor_one "${doctor_connection_list[$doctor_index]}" "${doctor_user_list[$doctor_index]}" \
          "${doctor_schema_list[$doctor_index]}" || doctor_failed=$((doctor_failed + 1))
      done
    done
    [ "$doctor_total" -gt 0 ] || fail "no configured profile lists schema ${PROJECT_SCHEMA:-?}"
    [ "$doctor_failed" -eq 0 ] || fail "$doctor_failed of $doctor_total doctor check(s) failed"
    if [ "$doctor_total" -eq 1 ]; then
      printf 'Doctor checks passed for the configured DEV connection.\n'
    else
      printf 'Doctor checks passed for all %s configured DEV schema connections.\n' "$doctor_total"
    fi
    ;;
```

The error text of a failing check must contain the schema name (the test asserts `ONE` in stderr).

- [ ] **Step 6: Implement `--schema` parsing and `doctor` in `team.ps1` (twin)**

In `scripts/team.ps1`, after the `if ([string]::IsNullOrWhiteSpace($Command) -or …) { Show-Usage; exit 0 }` block and before `switch ($Command)`, add:

```powershell
# --schema NAME is the one selection channel: strip it and set PROJECT_SCHEMA so
# every child script's loader narrows to that schema.
if ($Command -ne "upgrade-template") {
  $schemaFiltered = @()
  for ($index = 0; $index -lt $Arguments.Count; $index++) {
    if ($Arguments[$index] -eq "--schema") {
      if ($index + 1 -ge $Arguments.Count) { throw "--schema requires a schema name" }
      $env:PROJECT_SCHEMA = $Arguments[$index + 1]
      $index++
    } elseif ($Arguments[$index] -like "--schema=*") {
      $env:PROJECT_SCHEMA = $Arguments[$index].Substring("--schema=".Length)
    } else {
      $schemaFiltered += $Arguments[$index]
    }
  }
  $Arguments = $schemaFiltered
  if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $env:PROJECT_SCHEMA -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
    throw "--schema must be an uppercase Oracle identifier"
  }
}
```

Add the same `Options:` line to `Show-Usage`. Replace the `"doctor"` case body with the twin of the Bash logic:

```powershell
  "doctor" {
    if ($Arguments.Count -ne 0) { throw "doctor does not accept arguments" }
    . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
    . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
    $scratchPath = Join-Path ((Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path) "scratch"
    [System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null

    function Invoke-DoctorOne([string]$Connection, [string]$ExpectedUser, [string]$SchemaName) {
      $sqlclWorkDir = Join-Path $scratchPath ("sqlcl-doctor-" + [Guid]::NewGuid().ToString("N"))
      [System.IO.Directory]::CreateDirectory($sqlclWorkDir) | Out-Null
      $stdinFile = Join-Path $sqlclWorkDir ".sqlcl-stdin"
      $transcriptFile = Join-Path $sqlclWorkDir "sqlcl-output.log"
      New-Item -ItemType File -Path $stdinFile | Out-Null
      try {
        $sqlclExit = Invoke-Sqlcl -WorkingDirectory $sqlclWorkDir -StdInFile $stdinFile `
          -TranscriptFile $transcriptFile -Arguments @(
          "-S", "-noupdates", "-name", $Connection,
          "@$(Join-Path $PSScriptRoot 'doctor.sql')",
          $SchemaName, $env:DB_ENVIRONMENT, $ExpectedUser
        )
        $output = [System.IO.File]::ReadAllText($transcriptFile)
        Write-Output $output
        if ($sqlclExit -ne 0) {
          [Console]::Error.WriteLine("team error: SQLcl doctor check failed for schema $SchemaName (connection $Connection)")
          return $false
        }
        if ($output -notmatch "(?m)^\s*APEX_DOCTOR_VERIFIED:$([regex]::Escape($ExpectedUser))\s*$") {
          [Console]::Error.WriteLine("team error: SQLcl did not verify the doctor script for schema $SchemaName; the result is unknown")
          return $false
        }
        return $true
      } finally {
        if (Test-Path -LiteralPath $sqlclWorkDir) {
          Remove-Item -LiteralPath $sqlclWorkDir -Recurse -Force -ErrorAction SilentlyContinue
        }
      }
    }

    $doctorSeen = @{}
    $doctorTotal = 0
    $doctorFailed = 0
    foreach ($doctorProfile in @("apex", "tables", "code")) {
      switch ($doctorProfile) {
        "apex"   { $doctorSchemas = $env:APEX_PARSING_SCHEMA; $doctorConnections = $env:APEX_SQLCL_CONNECTION; $doctorUsers = $env:APEX_EXPECTED_USER }
        "tables" { $doctorSchemas = $env:TABLES_SCHEMA; $doctorConnections = $env:TABLES_SQLCL_CONNECTION; $doctorUsers = $env:TABLES_EXPECTED_USER }
        "code"   { $doctorSchemas = $env:CODE_SCHEMA; $doctorConnections = $env:CODE_SQLCL_CONNECTION; $doctorUsers = $env:CODE_EXPECTED_USER }
      }
      if ([string]::IsNullOrEmpty($doctorSchemas)) { continue }
      $schemaList = @($doctorSchemas.Split(","))
      $connectionList = @($doctorConnections.Split(","))
      $userList = @($doctorUsers.Split(","))
      for ($doctorIndex = 0; $doctorIndex -lt $schemaList.Count; $doctorIndex++) {
        $doctorKey = "$($connectionList[$doctorIndex])|$($userList[$doctorIndex])|$($schemaList[$doctorIndex])"
        if ($doctorSeen.ContainsKey($doctorKey)) { continue }
        $doctorSeen[$doctorKey] = $true
        $doctorTotal++
        & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target $doctorProfile -Schema $schemaList[$doctorIndex]
        if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
          Write-Output "Doctor: schema $($schemaList[$doctorIndex]) via connection $($connectionList[$doctorIndex]) as $($userList[$doctorIndex])"
        }
        if (-not (Invoke-DoctorOne $connectionList[$doctorIndex] $userList[$doctorIndex] $schemaList[$doctorIndex])) { $doctorFailed++ }
      }
    }
    if ($doctorTotal -eq 0) { throw "no configured profile lists schema $($env:PROJECT_SCHEMA)" }
    if ($doctorFailed -gt 0) { throw "$doctorFailed of $doctorTotal doctor check(s) failed" }
    if ($doctorTotal -eq 1) {
      Write-Output "Doctor checks passed for the configured DEV connection."
    } else {
      Write-Output "Doctor checks passed for all $doctorTotal configured DEV schema connections."
    }
  }
```

Note: the existing PowerShell `doctor` dot-sourced `load_env.ps1` and called `check_db_target.ps1 -Operation read -Target apex` first. The loop above now runs `check_db_target.ps1` per schema. Because `check_db_target.ps1` dot-sources the loader in the child script scope (invoked with `&`), it does not disturb the parent's narrowing.

- [ ] **Step 7: Run tests and commit**

```bash
python3 -m unittest tests.test_multi_schema_cli tests.test_team_cli -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/check_db_target.sh scripts/check_db_target.ps1 scripts/team.sh scripts/team.ps1 tests/test_multi_schema_cli.py
git commit -m "feat: add --schema and per-schema doctor checks"
```

Expected: all pass. If an existing `test_team_cli` doctor test fails because its fixture copies fewer scripts, add `verify_db_access.sql` to that fixture's copied list (it is now needed by nothing new in `team.sh`; only add it if the failure says it is missing).

---

### Task 4: `backup-db` for several schemas, plus the synonyms scope

**Files:**
- Modify: `scripts/backup_db.sh`, `scripts/backup_db.ps1`, `scripts/backup_db.sql`
- Test: `tests/test_multi_schema_cli.py` (append), `tests/test_backup_db_cli.py` (append one static test)

**Interfaces:**
- Consumes: Task 2 loader (`PROJECT_SCHEMA` narrowing, lists in scalars when unnarrowed), Task 3 `check_db_target.sh <read> <scope> <schema>` / `-Schema`.
- Produces: `backup-db` mirrors every schema in the tables list (tables scope) and every schema in the code list (code scope); `database/<SCHEMA>/synonyms/` in the code scope; `--schema` narrows; an all-schemas failure installs nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_schema_cli.py`:

```python
class BackupCliTests(unittest.TestCase):
    NAMES = (
        "backup_db.sh", "backup_db.sql", "load_env.sh", "check_db_target.sh", "sqlcl_safe.sh", "replace_mirror.sh",
    )

    def make_checkout(self, root: Path, replacements: dict[str, str]) -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text(replacements), encoding="utf-8")
        git_init(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        # Positional: -S -noupdates -name <conn> @<script> <schema> <scope> <env> <user> <prefixes> <spool_schema>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "connection=\"$4\"; schema=\"$6\"; scope=\"$7\"; spool_schema=\"${11}\"\n"
            "printf '%s|%s|%s\\n' \"$connection\" \"$schema\" \"$scope\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_SCHEMA:-}\" == \"$schema\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "if [[ \"$scope\" == tables ]]; then\n"
            "  mkdir -p \"database/$spool_schema/tables\"\n"
            "  printf 'CREATE TABLE T_%s;\\n' \"$schema\" > \"database/$spool_schema/tables/T_$schema.sql\"\n"
            "  printf 'TABLE=1\\n' > \"database/$spool_schema/manifest-tables.txt\"\n"
            "else\n"
            "  mkdir -p \"database/$spool_schema/views\" \"database/$spool_schema/synonyms\"\n"
            "  printf 'CREATE VIEW V_%s;\\n' \"$schema\" > \"database/$spool_schema/views/V_$schema.sql\"\n"
            "  printf 'CREATE SYNONYM S_%s;\\n' \"$schema\" > \"database/$spool_schema/synonyms/S_$schema.sql\"\n"
            "  if [[ \"${FAKE_SHORT_MANIFEST:-}\" == \"$schema\" ]]; then extra=2; else extra=1; fi\n"
            "  printf 'VIEW=1\\nSYNONYM=%s\\n' \"$extra\" > \"database/$spool_schema/manifest-code.txt\"\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "backup_db.sh", environment

    def run_backup(self, script: Path, environment: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script)],
            cwd=script.parents[1],
            env={**environment, **extra},
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return sorted(path.read_text().splitlines()) if path.exists() else []

    def test_every_listed_schema_is_mirrored_with_tables_code_and_synonyms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(
                ["conn-one|ONE|code", "conn-one|ONE|tables", "conn-two|TWO|code", "conn-two|TWO|tables"],
                self.calls(environment),
            )
            for schema in ("ONE", "TWO"):
                mirror = root / "database" / schema
                self.assertTrue((mirror / "tables" / f"T_{schema}.sql").is_file())
                self.assertTrue((mirror / "views" / f"V_{schema}.sql").is_file())
                self.assertTrue((mirror / "synonyms" / f"S_{schema}.sql").is_file())
            self.assertEqual(["ONE", "TWO"], sorted(path.name for path in (root / "database").iterdir()))

    def test_schema_option_mirrors_only_that_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|code", "conn-two|TWO|tables"], self.calls(environment))
            self.assertEqual(["TWO"], sorted(path.name for path in (root / "database").iterdir()))

    def test_a_profile_that_does_not_list_the_schema_skips_that_scope(self) -> None:
        replacements = {
            **TWO_SCHEMAS,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, replacements)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|code"], self.calls(environment))
            self.assertFalse((root / "database" / "TWO" / "tables").exists())

    def test_an_unlisted_schema_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="NOPE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("not configured", result.stderr)
            self.assertEqual([], self.calls(environment))

    def test_a_failing_schema_installs_nothing_for_any_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, FAKE_FAIL_SCHEMA="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / "database").exists(), "no mirror may be installed after a failure")

    def test_a_short_manifest_installs_nothing_for_any_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, FAKE_SHORT_MANIFEST="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("incomplete", result.stderr)
            self.assertFalse((root / "database").exists())

    def test_a_dirty_mirror_for_any_schema_is_refused_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            dirty = root / "database" / "TWO"
            dirty.mkdir(parents=True)
            (dirty / "local-edit.sql").write_text("-- edit\n", encoding="utf-8")
            result = self.run_backup(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing to back up over dirty mirror: database/TWO", result.stderr)
            self.assertEqual([], self.calls(environment))
```

Append to `tests/test_backup_db_cli.py` inside `BackupDbCliTests`:

```python
    def test_sql_driver_mirrors_private_synonyms_and_counts_them(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("SPOOL database/&&spool_schema/synonyms/", script)
        self.assertIn("GET_DDL(''SYNONYM''", script)
        self.assertIn("SELECT 'SYNONYM', 'code' FROM dual", script)
        self.assertIn("'VIEW', 'PACKAGE', 'PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SYNONYM'", script)
        # Only the schema's own (private) synonyms: filtered by owner, never PUBLIC.
        self.assertIn(
            "AND objects_to_export.owner = UPPER('&&target_schema')\n  AND objects_to_export.object_type = 'SYNONYM'",
            script,
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_multi_schema_cli.BackupCliTests tests.test_backup_db_cli -v 2>&1 | tail -30
```

Expected: `BackupCliTests` fail; the static SQL test fails on the missing `synonyms` strings.

- [ ] **Step 3: Implement the SQL change**

In `scripts/backup_db.sql`:

1. In the unsafe-filename check's `object_type IN (` list, add `'SYNONYM'` after `'TRIGGER'`, so it reads `'VIEW', 'PACKAGE', 'PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SYNONYM'` on one line (the test asserts that exact text; keep it on a single line).
2. After the `triggers` SELECT and before the `SPOOL OFF` that closes the driver, add:

```sql
SELECT 'SPOOL database/&&spool_schema/synonyms/' || REPLACE(object_name, '$', '-S-') || '.sql'
       || CHR(10) || 'SELECT DBMS_METADATA.GET_DDL(''SYNONYM'', ''' || object_name
       || ''', ''&&target_schema'') FROM DUAL' || CHR(59)
       || CHR(10) || 'SPOOL OFF'
FROM all_objects objects_to_export
WHERE LOWER('&&object_scope') = 'code'
  -- A private synonym is owned by the schema; PUBLIC synonyms are owned by
  -- PUBLIC and are deliberately not mirrored here.
  AND objects_to_export.owner = UPPER('&&target_schema')
  AND objects_to_export.object_type = 'SYNONYM'
  AND objects_to_export.object_name NOT LIKE 'BIN$%'
  AND (
    '&&object_prefixes' = '*'
    OR EXISTS (
      SELECT 1
      FROM (
        SELECT REGEXP_SUBSTR('&&object_prefixes', '[^,]+', 1, LEVEL) object_prefix
        FROM dual
        CONNECT BY LEVEL <= REGEXP_COUNT('&&object_prefixes', ',') + 1
      ) configured_prefixes
      WHERE INSTR(objects_to_export.object_name, configured_prefixes.object_prefix) = 1
    )
  )
ORDER BY object_name;
```

3. In the manifest `expected_types` CTE, add `SELECT 'SYNONYM', 'code' FROM dual UNION ALL` before the final `SELECT 'TRIGGER', 'code' FROM dual`, i.e. the list becomes TABLE, VIEW, PACKAGE, PACKAGE BODY, PROCEDURE, FUNCTION, SYNONYM, TRIGGER.

The statement must contain the literal text `SELECT 'SYNONYM', 'code' FROM dual` (test).

- [ ] **Step 4: Implement `backup_db.sh`**

Replace the section from `PROJECT_ENV_FILE=… check_db_target.sh read tables` through the end of `add_backup_schema "$CODE_SCHEMA"` with:

```bash
split_csv() {
  # split_csv <array-name> <value>: an empty value is an empty array.
  local -n split_out="$1"
  split_out=()
  [ -z "$2" ] || IFS=',' read -r -a split_out <<< "$2"
  return 0
}
split_csv TABLES_SCHEMAS "$TABLES_SCHEMA"
split_csv TABLES_CONNECTIONS "$TABLES_SQLCL_CONNECTION"
split_csv TABLES_USERS "$TABLES_EXPECTED_USER"
split_csv CODE_SCHEMAS "$CODE_SCHEMA"
split_csv CODE_CONNECTIONS "$CODE_SQLCL_CONNECTION"
split_csv CODE_USERS "$CODE_EXPECTED_USER"
if [ "${#TABLES_SCHEMAS[@]}" -eq 0 ] && [ "${#CODE_SCHEMAS[@]}" -eq 0 ]; then
  echo "backup error: no profile lists schema ${PROJECT_SCHEMA:-?}; nothing to back up" >&2
  exit 2
fi
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read tables "${TABLES_SCHEMAS[$index]}"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read code "${CODE_SCHEMAS[$index]}"
done

BACKUP_SCHEMAS=()
add_backup_schema() {
  local candidate="$1"
  local existing
  for existing in ${BACKUP_SCHEMAS[@]+"${BACKUP_SCHEMAS[@]}"}; do
    [ "$existing" = "$candidate" ] && return
  done
  BACKUP_SCHEMAS+=("$candidate")
}
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do add_backup_schema "${TABLES_SCHEMAS[$index]}"; done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do add_backup_schema "${CODE_SCHEMAS[$index]}"; done
```

In `scope_directories`, change the code line to `code)   printf '%s\n' views packages procedures functions triggers synonyms ;;`.

Replace the two final `run_backup_scope …` calls (the ones after the comment "Both exports and manifests must complete…") with:

```bash
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  run_backup_scope tables "${TABLES_SCHEMAS[$index]}" "${TABLES_CONNECTIONS[$index]}" \
    "${TABLES_USERS[$index]}" "$TABLES_PREFIXES"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  run_backup_scope code "${CODE_SCHEMAS[$index]}" "${CODE_CONNECTIONS[$index]}" \
    "${CODE_USERS[$index]}" "$CODE_PREFIXES"
done
```

Nothing else changes: the dirty-mirror loop, staging, `verify_scope_complete`, rename and single `replace_mirror.sh` call already iterate `BACKUP_SCHEMAS`.

- [ ] **Step 5: Implement `backup_db.ps1` (twin)**

Replace the two `check_db_target.ps1` lines near the top with nothing (they move below), change `Get-ScopeDirectory`'s code branch to `return @("views", "packages", "procedures", "functions", "triggers", "synonyms")`, and replace the `$backupTargets = @( … )` and `$backupSchemas = …` definitions with:

```powershell
function Split-BackupList([string] $Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
$backupTargets = @()
foreach ($profile in @(
    @{ Scope = "tables"; Schemas = $env:TABLES_SCHEMA; Connections = $env:TABLES_SQLCL_CONNECTION; Users = $env:TABLES_EXPECTED_USER; Prefixes = $env:TABLES_PREFIXES },
    @{ Scope = "code"; Schemas = $env:CODE_SCHEMA; Connections = $env:CODE_SQLCL_CONNECTION; Users = $env:CODE_EXPECTED_USER; Prefixes = $env:CODE_PREFIXES }
  )) {
  $schemaList = @(Split-BackupList $profile.Schemas)
  $connectionList = @(Split-BackupList $profile.Connections)
  $userList = @(Split-BackupList $profile.Users)
  for ($index = 0; $index -lt $schemaList.Count; $index++) {
    $backupTargets += [PSCustomObject]@{
      Scope = $profile.Scope
      Schema = $schemaList[$index]
      Connection = $connectionList[$index]
      ExpectedUser = $userList[$index]
      Prefixes = $profile.Prefixes
    }
  }
}
if ($backupTargets.Count -eq 0) { throw "backup error: no profile lists schema $($env:PROJECT_SCHEMA); nothing to back up" }
foreach ($target in $backupTargets) {
  & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target $target.Scope -Schema $target.Schema
}
$backupSchemas = @($backupTargets | ForEach-Object { $_.Schema } | Select-Object -Unique)
```

Note the profile name for `check_db_target.ps1 -Target` equals `$target.Scope` (`tables` or `code`), which are both valid `-Target` values. The rest of the script already iterates `$backupTargets` and `$backupSchemas`.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m unittest tests.test_multi_schema_cli tests.test_backup_db_cli tests.test_sql_driver_contracts -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/backup_db.sh scripts/backup_db.ps1 scripts/backup_db.sql tests/test_multi_schema_cli.py tests/test_backup_db_cli.py
git commit -m "feat: back up every configured schema and mirror private synonyms"
```

Expected: all pass. The existing `test_backup_db_cli` `DEMO$` tests must still pass (single schema, `$` in the name). If the existing fake `sql` there only writes `views`, the manifest count still matches because synonyms have zero files and the fake writes `VIEW=1` only.

---

### Task 5: `export` resolves each app's schema

**Files:**
- Create: `scripts/lookup_app_schema.sql`
- Modify: `scripts/sqlcl_safe.sh`, `scripts/invoke_sqlcl.ps1`, `scripts/export_apps.sh`, `scripts/export_apps.ps1`
- Test: `tests/test_multi_schema_cli.py` (append), `tests/test_sql_driver_contracts.py` (add the new driver), `tests/test_export_cli.py` (fixture copy list)

**Interfaces:**
- Consumes: Task 2 loader, Task 3 `check_db_target.sh read apex <schema>`.
- Produces:
  - `scripts/lookup_app_schema.sql <schema> <app-id> <environment> <expected-user>` prints `APEX_APP_SCHEMA:<app-id>:<OWNER>` (`NOT_FOUND` when the app is absent).
  - Bash: `sqlcl_app_parsing_schema <connection> <expected-user> <schema> <app-id> <work-dir>` in `sqlcl_safe.sh`; prints the owner on stdout, returns non-zero on any SQLcl or parse failure. It needs `REPO_ROOT` and `DB_ENVIRONMENT` set by the caller.
  - PowerShell: `Get-AppParsingSchema -Connection -ExpectedUser -Schema -AppId -WorkDirectory -ScriptPath` in `invoke_sqlcl.ps1` (`-ScriptPath` is the absolute path of `scripts/lookup_app_schema.sql`; Task 6 passes it too), returns the owner string or throws.
  - `export_apps.sh` / `.ps1` with several schemas configured: for every app ID, resolve the parsing schema (live lookup), require it in `APEX_PARSING_SCHEMA` (and equal to `PROJECT_SCHEMA` when set), export with that schema's connection into `apps/<SCHEMA>/<id>/`, and install every app in one `replace_mirror` call.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_schema_cli.py`:

```python
class ExportCliTests(unittest.TestCase):
    NAMES = (
        "export_apps.sh", "export_apps.sql", "lookup_app_schema.sql", "load_env.sh", "check_db_target.sh",
        "sqlcl_safe.sh", "normalize_apx.sh", "replace_mirror.sh", "verify_db_access.sql",
        "record_export_state.py", "preserve_deployments.py",
    )
    APP_SCHEMAS = "117:ONE,301:TWO,205:THREE"

    def make_checkout(self, root: Path, replacements: dict[str, str], app_ids: str = "117,301") -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text({**replacements, "APEX_APP_ID=100,200": f"APEX_APP_ID={app_ids}"}), encoding="utf-8")
        git_init(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        # lookup:  -S -noupdates -name <conn> @lookup_app_schema.sql <schema> <app> <env> <user>
        # export:  -S -noupdates -name <conn> @export_apps.sql      <schema> <app> <env> <user>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "connection=\"$4\"; script=\"$5\"; schema=\"$6\"; app=\"$7\"\n"
            "printf '%s|%s|%s|%s\\n' \"$(basename \"${script#@}\")\" \"$connection\" \"$schema\" \"$app\" >> \"$FAKE_SQL_CALLS\"\n"
            "case \"$script\" in\n"
            "  *lookup_app_schema.sql)\n"
            "    for pair in ${FAKE_APP_SCHEMAS//,/ }; do\n"
            "      if [[ \"${pair%%:*}\" == \"$app\" ]]; then printf 'APEX_APP_SCHEMA:%s:%s\\n' \"$app\" \"${pair##*:}\"; exit 0; fi\n"
            "    done\n"
            "    printf 'APEX_APP_SCHEMA:%s:NOT_FOUND\\n' \"$app\"\n"
            "    ;;\n"
            "  *export_apps.sql)\n"
            "    if [[ \"${FAKE_FAIL_APP:-}\" == \"$app\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "    mkdir -p \"apps/$schema/exported/.apex\"\n"
            "    printf 'source of %s\\n' \"$app\" > \"apps/$schema/exported/application.apx\"\n"
            "    printf '{\"v\":1}\\n' > \"apps/$schema/exported/.apex/apexlang.json\"\n"
            "    printf '2026-09-26T08:00:00|2026-09-26T09:00:00|Release 1.0\\n' > .apex-export-before.txt\n"
            "    printf '2026-09-26T08:00:00|2026-09-26T09:00:02|Release 1.0\\n' > .apex-export-after.txt\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment["FAKE_APP_SCHEMAS"] = self.APP_SCHEMAS
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "export_apps.sh", environment

    def run_export(self, script: Path, environment: dict[str, str], *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=script.parents[1],
            env={**environment, **extra},
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return path.read_text().splitlines() if path.exists() else []

    def test_each_app_is_exported_under_its_own_schema_with_its_own_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("source of 117\n", (root / "apps" / "ONE" / "117" / "application.apx").read_text())
            self.assertEqual("source of 301\n", (root / "apps" / "TWO" / "301" / "application.apx").read_text())
            exports = [call for call in self.calls(environment) if call.startswith("export_apps.sql")]
            self.assertEqual(["export_apps.sql|conn-one|ONE|117", "export_apps.sql|conn-two|TWO|301"], exports)
            lookups = [call for call in self.calls(environment) if call.startswith("lookup_app_schema.sql")]
            self.assertEqual(["lookup_app_schema.sql|conn-one|ONE|117", "lookup_app_schema.sql|conn-one|ONE|301"], lookups)

    def test_single_app_argument_exports_only_that_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment, "301")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue((root / "apps" / "TWO" / "301").is_dir())
            self.assertFalse((root / "apps" / "ONE").exists())

    def test_app_owned_by_an_unlisted_schema_is_refused_before_any_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS, app_ids="205")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("THREE", result.stderr)
            self.assertIn("not listed in APEX_PARSING_SCHEMA", result.stderr)
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))
            self.assertFalse((root / "apps").exists())

    def test_unknown_app_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS, app_ids="999")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("999", result.stderr)

    def test_schema_option_must_match_the_apps_parsing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_export(script, environment, "117", PROJECT_SCHEMA="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("117", result.stderr)
            self.assertIn("ONE", result.stderr)
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))

    def test_a_failing_export_installs_no_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment, FAKE_FAIL_APP="301")
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / "apps").exists(), "an earlier app must not be installed after a later failure")

    def test_dirty_destination_is_refused_before_any_export_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            dirty = root / "apps" / "TWO" / "301"
            dirty.mkdir(parents=True)
            (dirty / "application.apx").write_text("local edit\n", encoding="utf-8")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing to export over dirty mirror: apps/TWO/301", result.stderr)
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))

    def test_single_schema_export_makes_no_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, {}, app_ids="100")
            result = self.run_export(script, environment, "100")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["export_apps.sql|docker-demo|DEMO|100"], self.calls(environment))
            self.assertTrue((root / "apps" / "DEMO" / "100").is_dir())
```

Add `"lookup_app_schema.sql"` to `SQL_DRIVERS` in `tests/test_sql_driver_contracts.py`, and to the read-only tuple in `test_read_only_drivers_exit_success_with_rollback`. Add `"lookup_app_schema.sql"` to the copied `names` in `tests/test_export_cli.py::make_checkout` (both the Bash and PowerShell lists) and, in `tests/test_publish_cli.py::make_publish_fixture`, to the copied tuple (Task 6 needs it there).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_multi_schema_cli.ExportCliTests tests.test_sql_driver_contracts -v 2>&1 | tail -30
```

Expected: `lookup_app_schema.sql` does not exist; the export tests fail.

- [ ] **Step 3: Create the lookup driver**

Create `scripts/lookup_app_schema.sql`:

```sql
-- Read-only lookup of an application's parsing schema. Arguments are supplied
-- by the validated wrappers: schema, app id, environment, expected session
-- user. The identity check runs first, so the lookup only ever reads through a
-- verified session.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE app_id = '&2'
DEFINE db_environment = '&3'
DEFINE expected_user = '&4'
SET ENCODING UTF-8
SET HEADING OFF
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
SET PAGESIZE 0
SET TRIMSPOOL ON
SET LINESIZE 32767
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

@@verify_db_access.sql

SELECT 'APEX_APP_SCHEMA:' || &&app_id || ':' || NVL(MAX(owner), 'NOT_FOUND')
FROM apex_applications
WHERE application_id = &&app_id;

SET DEFINE OFF
EXIT SUCCESS ROLLBACK
```

- [ ] **Step 4: Add the lookup helpers**

Append to `scripts/sqlcl_safe.sh`:

```bash

# Print the parsing schema that owns an APEX application, or fail. The caller
# supplies a newly-created work directory (never application source) and has
# REPO_ROOT and DB_ENVIRONMENT set. NOT_FOUND is reported as a failure.
sqlcl_app_parsing_schema() {
  [ "$#" -eq 5 ] || {
    printf 'usage: sqlcl_app_parsing_schema <connection> <expected-user> <schema> <app-id> <work-dir>\n' >&2
    return 2
  }
  local connection="$1" expected_user="$2" schema="$3" app_id="$4" work_dir="$5"
  local stdin_file="$work_dir/.sqlcl-stdin" output_file="$work_dir/lookup-output.log" owner
  mkdir -p -- "$work_dir"
  : > "$stdin_file"
  if ! invoke_sqlcl_safe "$work_dir" \
    -S -noupdates -name "$connection" \
    "@$REPO_ROOT/scripts/lookup_app_schema.sql" \
    "$schema" "$app_id" "$DB_ENVIRONMENT" "$expected_user" \
    < "$stdin_file" > "$output_file" 2>&1; then
    cat "$output_file" >&2
    printf 'could not look up the parsing schema of application %s\n' "$app_id" >&2
    return 1
  fi
  if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:' "$output_file"; then
    cat "$output_file" >&2
    printf 'the parsing-schema lookup for application %s reported an error\n' "$app_id" >&2
    return 1
  fi
  owner="$(sed -n "s/^[[:space:]]*APEX_APP_SCHEMA:${app_id}:\\(.*[^[:space:]]\\)[[:space:]]*\$/\\1/p" "$output_file" | tail -n 1)"
  if [ -z "$owner" ]; then
    printf 'the parsing-schema lookup for application %s returned no result\n' "$app_id" >&2
    return 1
  fi
  if [ "$owner" = NOT_FOUND ]; then
    printf 'application %s was not found in the workspace visible to this connection\n' "$app_id" >&2
    return 1
  fi
  printf '%s\n' "$owner"
}
```

Append to `scripts/invoke_sqlcl.ps1`:

```powershell

# Return the parsing schema that owns an APEX application, or throw.
function Get-AppParsingSchema {
  param(
    [Parameter(Mandatory = $true)][string] $Connection,
    [Parameter(Mandatory = $true)][string] $ExpectedUser,
    [Parameter(Mandatory = $true)][string] $Schema,
    [Parameter(Mandatory = $true)][string] $AppId,
    [Parameter(Mandatory = $true)][string] $WorkDirectory,
    [Parameter(Mandatory = $true)][string] $ScriptPath
  )
  [System.IO.Directory]::CreateDirectory($WorkDirectory) | Out-Null
  $transcript = Join-Path $WorkDirectory "lookup-output.log"
  $exit = Invoke-Sqlcl -WorkingDirectory $WorkDirectory `
    -StdInFile (Join-Path $WorkDirectory ".sqlcl-stdin") -TranscriptFile $transcript `
    -Arguments @("-S", "-noupdates", "-name", $Connection, "@$ScriptPath", $Schema, $AppId, $env:DB_ENVIRONMENT, $ExpectedUser)
  $text = [System.IO.File]::ReadAllText($transcript)
  if ($exit -ne 0 -or $text -match '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:') {
    throw "could not look up the parsing schema of application ${AppId}:`n$text"
  }
  $match = [regex]::Match($text, "(?m)^\s*APEX_APP_SCHEMA:$([regex]::Escape($AppId)):(.*?)\s*$")
  if (-not $match.Success) { throw "the parsing-schema lookup for application $AppId returned no result" }
  $owner = $match.Groups[1].Value
  if ($owner -eq "NOT_FOUND") { throw "application $AppId was not found in the workspace visible to this connection" }
  return $owner
}
```

- [ ] **Step 5: Rewrite `export_apps.sh`**

Replace the entire body of `scripts/export_apps.sh` after the first two comment/`set` lines with the version below. It keeps every existing guard (dirty check, scratch staging, exact-one-directory, marker files, all-or-nothing install) and adds per-app schema resolution.

```bash
#!/usr/bin/env bash
# Export the configured APEX applications as APEXlang mirrors.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=sqlcl_safe.sh
source "$REPO_ROOT/scripts/sqlcl_safe.sh"

split_csv() {
  # split_csv <array-name> <value>: an empty value is an empty array.
  local -n split_out="$1"
  split_out=()
  [ -z "$2" ] || IFS=',' read -r -a split_out <<< "$2"
  return 0
}
split_csv APEX_SCHEMAS "$APEX_PARSING_SCHEMA"
split_csv APEX_CONNECTIONS "$APEX_SQLCL_CONNECTION"
split_csv APEX_USERS "$APEX_EXPECTED_USER"
if [ "${#APEX_SCHEMAS[@]}" -eq 0 ]; then
  echo "export error: the APEX profile does not list schema ${PROJECT_SCHEMA:-?}" >&2
  exit 2
fi

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/export_apps.sh [numeric_app_id]" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || { echo "export error: expected a positive numeric application id" >&2; exit 2; }
  APP_IDS=("$1")
else
  IFS=',' read -r -a APP_IDS <<< "$APEX_APP_ID"
fi

mkdir -p "$REPO_ROOT/scratch"
STAGING_DIR="$(mktemp -d "$REPO_ROOT/scratch/apex-export.XXXXXX")"
cleanup() { rm -rf -- "$STAGING_DIR"; }
trap cleanup EXIT

# SQLcl builds a JLine console over its standard input at startup. Handed a
# descriptor it cannot probe -- a pipe, or the Windows NUL device that
# /dev/null becomes under Git Bash -- it aborts with
# "java.io.IOException: Incorrect function" before running the script, and
# still exits 0. An empty regular file is a standard input every platform can
# probe, and it also stops SQLcl from consuming the caller's own input.
SQLCL_STDIN="$STAGING_DIR/.sqlcl-stdin"
: > "$SQLCL_STDIN"

# Which schema parses each application. With one schema configured that is the
# schema itself; with several it is read from the live workspace, using the
# first connection of the profile (the selected schema's, under --schema).
declare -A APP_SCHEMA_OF
for app_id in "${APP_IDS[@]}"; do
  if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
    app_schema="$(sqlcl_app_parsing_schema "${APEX_CONNECTIONS[0]}" "${APEX_USERS[0]}" \
      "${APEX_SCHEMAS[0]}" "$app_id" "$STAGING_DIR/lookup/$app_id")" || {
      echo "export error: could not determine the parsing schema of application $app_id" >&2
      exit 1
    }
    if [ -n "${PROJECT_SCHEMA:-}" ] && [ "$app_schema" != "$PROJECT_SCHEMA" ]; then
      echo "export error: application $app_id is parsed by $app_schema, not $PROJECT_SCHEMA" >&2
      exit 2
    fi
  else
    app_schema="${APEX_SCHEMAS[0]}"
  fi
  found=false
  for ((index = 0; index < ${#APEX_SCHEMAS[@]}; index++)); do
    [ "${APEX_SCHEMAS[$index]}" = "$app_schema" ] && found=true
  done
  if [ "$found" != true ]; then
    echo "export error: application $app_id is parsed by $app_schema, which is not listed in APEX_PARSING_SCHEMA (${APEX_SCHEMAS[*]})" >&2
    exit 2
  fi
  APP_SCHEMA_OF["$app_id"]="$app_schema"
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read apex "$app_schema"
done

# Refuse any dirty destination before opening the first export session. Git
# warns when the schema parent does not exist on a first export; suppress that
# diagnostic while preserving the command's failure status.
for app_id in "${APP_IDS[@]}"; do
  destination="apps/${APP_SCHEMA_OF[$app_id]}/$app_id"
  dirty_status="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all -- "$destination" 2>/dev/null)" || {
    echo "unable to inspect Git status for mirror: $destination" >&2
    exit 1
  }
  if [ -n "$dirty_status" ]; then
    echo "refusing to export over dirty mirror: $destination" >&2
    echo "commit, stash, or remove local changes first" >&2
    exit 1
  fi
done

for app_id in "${APP_IDS[@]}"; do
  app_schema="${APP_SCHEMA_OF[$app_id]}"
  for ((index = 0; index < ${#APEX_SCHEMAS[@]}; index++)); do
    if [ "${APEX_SCHEMAS[$index]}" = "$app_schema" ]; then
      app_connection="${APEX_CONNECTIONS[$index]}"
      app_user="${APEX_USERS[$index]}"
    fi
  done
  STAGE_PARENT="$STAGING_DIR/staged/apps/$app_schema"
  mkdir -p "$STAGE_PARENT"
  RUN_DIR="$STAGING_DIR/runs/$app_id"
  RUN_STAGE_PARENT="$RUN_DIR/apps/$app_schema"
  mkdir -p "$RUN_STAGE_PARENT"
  SQLCL_OUTPUT="$RUN_DIR/sqlcl-output.log"

  if ! (
    invoke_sqlcl_safe "$RUN_DIR" \
      -S -noupdates -name "$app_connection" \
      "@$REPO_ROOT/scripts/export_apps.sql" \
      "$app_schema" "$app_id" "$DB_ENVIRONMENT" \
      "$app_user" < "$SQLCL_STDIN"
  ) > "$SQLCL_OUTPUT" 2>&1; then
    cat "$SQLCL_OUTPUT" >&2
    echo "APEX export for application $app_id failed in SQLcl" >&2
    exit 1
  fi
  if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:' "$SQLCL_OUTPUT"; then
    cat "$SQLCL_OUTPUT" >&2
    echo "APEX export for application $app_id reported a client or database error" >&2
    exit 1
  fi
  cat "$SQLCL_OUTPUT"

  # SQLcl names each export directory after the application alias, which can
  # change independently of the immutable application id used by the mirror.
  EXPORTED_DIR=""
  EXPORTED_COUNT=0
  while IFS= read -r -d '' candidate; do
    EXPORTED_DIR="$candidate"
    EXPORTED_COUNT=$((EXPORTED_COUNT + 1))
  done < <(find "$RUN_STAGE_PARENT" -mindepth 1 -maxdepth 1 -type d -print0)

  if [ "$EXPORTED_COUNT" -ne 1 ]; then
    echo "expected exactly one exported directory for application $app_id, found $EXPORTED_COUNT" >&2
    exit 1
  fi
  test -f "$EXPORTED_DIR/application.apx" || {
    echo "APEX export for application $app_id did not create application.apx" >&2
    exit 1
  }
  test -f "$EXPORTED_DIR/.apex/apexlang.json" || {
    echo "APEX export for application $app_id did not create .apex/apexlang.json" >&2
    exit 1
  }

  APP_STAGE="$STAGE_PARENT/$app_id"
  mv -- "$EXPORTED_DIR" "$APP_STAGE"
  "$REPO_ROOT/scripts/normalize_apx.sh" "$APP_STAGE"
  python3 "$REPO_ROOT/scripts/record_export_state.py" "$app_id" \
    "$RUN_DIR/.apex-export-before.txt" "$RUN_DIR/.apex-export-after.txt" \
    "$APP_STAGE/apex-team-export.json"
  python3 "$REPO_ROOT/scripts/preserve_deployments.py" \
    "$REPO_ROOT/apps/$app_schema/$app_id" "$APP_STAGE"
done

# Install only after every requested application has exported and verified, and
# install them in one call so a failure on the last application does not leave
# the earlier ones replaced.
REPLACE_ARGS=()
for app_id in "${APP_IDS[@]}"; do
  app_schema="${APP_SCHEMA_OF[$app_id]}"
  REPLACE_ARGS+=("$STAGING_DIR/staged/apps/$app_schema/$app_id" "apps/$app_schema/$app_id")
done
"$REPO_ROOT/scripts/replace_mirror.sh" "${REPLACE_ARGS[@]}"
```

Two deliberate differences from the old script, which the executor must keep: (1) the read-only parsing-schema lookups (multi-schema only) run **before** the dirty-destination check, because the destination depends on the result; they never write. (2) `check_db_target.sh read apex <schema>` now runs once per app after resolution instead of once at the top.

- [ ] **Step 6: Rewrite `export_apps.ps1` (twin)**

Replace the whole of `scripts/export_apps.ps1` with the script below. It is the twin of the Bash script in Step 5: same order (lookups, then the dirty check, then exports, then one install), same messages.

```powershell
#Requires -Version 5.1
# Export the configured APEX applications as APEXlang mirrors.
param([string] $AppId)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")

function Invoke-PythonScript {
  param([string] $ScriptPath, [string[]] $ScriptArguments)
  $python = Get-Command python3 -ErrorAction SilentlyContinue
  if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
  if ($null -eq $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
  if ($null -eq $python) { throw "Python 3 is required for APEX export metadata" }
  if ($python.Name -in @("py.exe", "py")) {
    & $python.Source -3 $ScriptPath @ScriptArguments
  } else {
    & $python.Source $ScriptPath @ScriptArguments
  }
  if ($LASTEXITCODE -ne 0) { throw "Python export helper failed with exit code $LASTEXITCODE" }
}

function Split-ExportList([string] $Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
$apexSchemas = @(Split-ExportList $env:APEX_PARSING_SCHEMA)
$apexConnections = @(Split-ExportList $env:APEX_SQLCL_CONNECTION)
$apexUsers = @(Split-ExportList $env:APEX_EXPECTED_USER)
if ($apexSchemas.Count -eq 0) {
  throw "export error: the APEX profile does not list schema $($env:PROJECT_SCHEMA)"
}

if (-not [string]::IsNullOrWhiteSpace($AppId)) {
  if ($AppId -cnotmatch '^[1-9][0-9]*$') { throw "export error: expected a positive numeric application id" }
  $appIds = @($AppId)
} else {
  $appIds = @($env:APEX_APP_ID.Split(','))
}

$scratchPath = Join-Path $repoRoot "scratch"
# New-Item has no -LiteralPath parameter on either Windows PowerShell 5.1 or
# PowerShell 7. The .NET API is literal and has the same create-if-missing behavior.
[System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
$stagingPath = Join-Path $scratchPath ("apex-export-" + [Guid]::NewGuid().ToString("N"))
[System.IO.Directory]::CreateDirectory($stagingPath) | Out-Null

try {
  # Which schema parses each application. With one schema configured that is
  # the schema itself; with several it is read from the live workspace, using
  # the first connection of the profile (the selected schema's, under --schema).
  $appSchemaOf = @{}
  foreach ($appId in $appIds) {
    if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
      try {
        $appSchema = Get-AppParsingSchema -Connection $apexConnections[0] -ExpectedUser $apexUsers[0] `
          -Schema $apexSchemas[0] -AppId $appId `
          -WorkDirectory (Join-Path $stagingPath "lookup/$appId") `
          -ScriptPath (Join-Path $repoRoot "scripts/lookup_app_schema.sql")
      } catch {
        throw "export error: could not determine the parsing schema of application ${appId}: $($_.Exception.Message)"
      }
      if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $appSchema -cne $env:PROJECT_SCHEMA) {
        throw "export error: application $appId is parsed by $appSchema, not $($env:PROJECT_SCHEMA)"
      }
    } else {
      $appSchema = $apexSchemas[0]
    }
    if ($apexSchemas -cnotcontains $appSchema) {
      throw "export error: application $appId is parsed by $appSchema, which is not listed in APEX_PARSING_SCHEMA ($($apexSchemas -join ' '))"
    }
    $appSchemaOf[[string]$appId] = $appSchema
    & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target apex -Schema $appSchema
  }

  # Refuse any dirty destination before opening the first export session. Git
  # warns when the schema parent does not exist on a first export; suppress
  # that diagnostic while preserving the command's failure status.
  foreach ($appId in $appIds) {
    $destination = "apps/$($appSchemaOf[[string]$appId])/$appId"
    $dirty = @(git -C $repoRoot status --porcelain --untracked-files=all -- $destination 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "unable to inspect Git status for mirror: $destination" }
    if (-not [string]::IsNullOrWhiteSpace(($dirty -join "`n"))) {
      throw "refusing to export over dirty mirror: $destination; commit, stash, or remove local changes first"
    }
  }

  foreach ($appId in $appIds) {
    $appSchema = $appSchemaOf[[string]$appId]
    $schemaIndex = [Array]::IndexOf($apexSchemas, $appSchema)
    $appConnection = $apexConnections[$schemaIndex]
    $appUser = $apexUsers[$schemaIndex]
    $stageParent = Join-Path $stagingPath "staged/apps/$appSchema"
    New-Item -ItemType Directory -Force -Path $stageParent | Out-Null
    $runPath = Join-Path $stagingPath "runs/$appId"
    $runStageParent = Join-Path $runPath "apps/$appSchema"
    New-Item -ItemType Directory -Force -Path $runStageParent | Out-Null
    $sqlclOutput = Join-Path $runPath "sqlcl-output.log"

    $sqlclExit = Invoke-Sqlcl -WorkingDirectory $runPath `
      -StdInFile (Join-Path $stagingPath ".sqlcl-stdin") `
      -TranscriptFile $sqlclOutput `
      -Arguments @(
        "-S", "-noupdates", "-name", $appConnection,
        "@$(Join-Path $repoRoot 'scripts/export_apps.sql')",
        $appSchema, $appId, $env:DB_ENVIRONMENT, $appUser
      )
    $transcript = [System.IO.File]::ReadAllText($sqlclOutput)
    if ($sqlclExit -ne 0 -or $transcript -match '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:') {
      throw "SQLcl export for application $appId failed or reported a client or database error:`n$transcript"
    }
    Write-Output $transcript

    # SQLcl names each export directory after the application alias, which can
    # change independently of the immutable application id used by the mirror.
    $exported = @(Get-ChildItem -LiteralPath $runStageParent -Directory)
    if ($exported.Count -ne 1) {
      throw "expected exactly one exported directory for application $appId, found $($exported.Count)"
    }
    $exportedDir = $exported[0].FullName
    if (-not (Test-Path -LiteralPath (Join-Path $exportedDir "application.apx") -PathType Leaf)) {
      throw "APEX export for application $appId did not create application.apx"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $exportedDir ".apex/apexlang.json") -PathType Leaf)) {
      throw "APEX export for application $appId did not create .apex/apexlang.json"
    }

    $appStage = Join-Path $stageParent $appId
    Move-Item -LiteralPath $exportedDir -Destination $appStage
    & (Join-Path $PSScriptRoot "normalize_apx.ps1") $appStage
    Invoke-PythonScript -ScriptPath (Join-Path $PSScriptRoot "record_export_state.py") `
      -ScriptArguments @(
        $appId,
        (Join-Path $runPath ".apex-export-before.txt"),
        (Join-Path $runPath ".apex-export-after.txt"),
        (Join-Path $appStage "apex-team-export.json")
      )
    Invoke-PythonScript -ScriptPath (Join-Path $PSScriptRoot "preserve_deployments.py") `
      -ScriptArguments @(
        (Join-Path $repoRoot "apps/$appSchema/$appId"),
        $appStage
      )
  }

  # Install every application in one call so a failure on the last does not
  # leave the earlier ones replaced.
  $replaceArgs = @()
  foreach ($appId in $appIds) {
    $appSchema = $appSchemaOf[[string]$appId]
    $replaceArgs += (Join-Path $stagingPath "staged/apps/$appSchema/$appId")
    $replaceArgs += "apps/$appSchema/$appId"
  }
  & (Join-Path $PSScriptRoot "replace_mirror.ps1") @replaceArgs
} finally {
  if (Test-Path -LiteralPath $stagingPath) {
    Remove-Item -LiteralPath $stagingPath -Recurse -Force -ErrorAction Stop
  }
}
```

PowerShell variable names are case-insensitive, so the `$appId` loop variable overwrites the `$AppId` parameter after `$appIds` is built; that matches the original script and is harmless here because `$appIds` is already computed. Re-read this file against the Bash script before committing.

- [ ] **Step 7: Run tests and commit**

```bash
python3 -m unittest tests.test_multi_schema_cli tests.test_export_cli tests.test_sql_driver_contracts -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/lookup_app_schema.sql scripts/sqlcl_safe.sh scripts/invoke_sqlcl.ps1 scripts/export_apps.sh scripts/export_apps.ps1 tests/test_multi_schema_cli.py tests/test_export_cli.py tests/test_sql_driver_contracts.py tests/test_publish_cli.py
git commit -m "feat: export each application under its own parsing schema"
```

Expected: all pass, including the existing single-schema export tests (no lookup is made there).

---

### Task 6: `publish` and `deploy` select and verify the schema

**Files:**
- Modify: `scripts/publish_app.sh`, `scripts/publish_app.ps1`, `scripts/deploy.sh`, `.agents/skills/apex-background/probe/run.sh`, `.claude/skills/apex-background/probe/run.sh` (if it is a separate tracked copy), `docs/publish-rules.md`, `tests/test_documentation_contract.py`
- Test: `tests/test_multi_schema_cli.py` (append)

**Interfaces:**
- Consumes: Task 2 loader (`PROJECT_SCHEMA`, `PROJECT_MULTI_SCHEMA`), Task 5 `sqlcl_app_parsing_schema` / `Get-AppParsingSchema`.
- Produces: in a multi-schema project `publish`/`deploy` (a) locate `apps/*/<id>`, (b) require folder schema == descriptor `parsingSchema` == live parsing schema (a not-yet-imported app, `NOT_FOUND`, is allowed), (c) re-source the loader with `PROJECT_SCHEMA=<parsing schema>` so DEV/staging/prod connection and user come from that schema's entry, and (d) print a clear error when the schema is not listed in the target's list. New refusal messages (must be documented in `docs/publish-rules.md`):
  - `is stored under apps/`
  - `does not match the application's parsing schema`
  - `is not listed in APEX_PARSING_SCHEMA`
  - `is not listed in STAGING_SCHEMA` / `is not listed in PROD_SCHEMA`
  - `is parsed by`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_schema_cli.py`. These tests drive `publish_app.sh` with a fake `sql` that answers the lookup, drift, import and export drivers. Reuse the structure of `tests/test_publish_cli.py::make_publish_fixture`: read it first and copy its fake `sql` (the `case "$mode"` script) into a helper here, extended with a `lookup` mode. Keep it short:

```python
class PublishCliTests(unittest.TestCase):
    NAMES = (
        "publish_app.sh", "publish_app.sql", "load_env.sh", "check_db_target.sh", "export_apps.sql",
        "lookup_app_schema.sql", "verify_db_access.sql", "normalize_apx.sh", "record_export_state.py",
        "verify_publish_state.py", "validate_app_source.py", "stamp_publish_version.py",
        "check_builder_drift.py", "check_builder_drift.sql", "sqlcl_safe.sh",
    )

    def make_fixture(self, root: Path, folder_schema: str, descriptor_schema: str, extra_env: str = "") -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text({**TWO_SCHEMAS, "APEX_APP_ID=100,200": "APEX_APP_ID=117"}) + extra_env, encoding="utf-8")
        app = root / "apps" / folder_schema / "117"
        (app / "deployments").mkdir(parents=True)
        (app / ".apex").mkdir()
        (app / "application.apx").write_text('app SAMPLE (\n    name: Sample\n    version: "Release 1.0"\n)\n', encoding="utf-8")
        (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
        import json
        for environment_name in ("dev", "staging", "prod"):
            (app / "deployments" / f"{environment_name}.json").write_text(
                json.dumps({"workspace": {"name": "WS"}, "app": {"id": 117, "databaseSession": {"parsingSchema": descriptor_schema}}}),
                encoding="utf-8",
            )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "connection=\"$4\"; script=\"$5\"\n"
            "printf '%s|%s\\n' \"$(basename \"${script#@}\")\" \"$connection\" >> \"$FAKE_SQL_CALLS\"\n"
            "case \"$script\" in\n"
            "  *lookup_app_schema.sql) printf 'APEX_APP_SCHEMA:117:%s\\n' \"${FAKE_LIVE_SCHEMA:-NOT_FOUND}\" ;;\n"
            "  *check_builder_drift.sql) printf 'APEX_DRIFT_QUERY_VERIFIED\\n' ;;\n"
            "  *publish_app.sql) printf 'Import successful.\\nAPEX_IMPORT_VERIFIED:117\\n' ;;\n"
            "  *) : ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "publish_app.sh", environment

    def run_publish(self, script: Path, environment: dict[str, str], *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), "117", *arguments],
            cwd=script.parents[1], env={**environment, **extra}, text=True, capture_output=True, check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return path.read_text().splitlines() if path.exists() else []

    def test_describe_selects_the_entry_for_the_descriptors_schema(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            result = self.run_publish(script, environment, "--env", "staging", "--describe")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(["TWO", "stage-two", "STWO"], [lines[2], lines[3], lines[4]])

    def test_dev_describe_uses_the_apex_profile_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--describe")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(["TWO", "conn-two", "TWO"], [lines[2], lines[3], lines[4]])

    def test_folder_and_descriptor_schema_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "ONE", "TWO")
            result = self.run_publish(script, environment, "--describe")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is stored under apps/ONE", result.stderr)
            self.assertEqual([], self.calls(environment))

    def test_schema_option_must_match_the_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--describe", PROJECT_SCHEMA="ONE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("does not match the application's parsing schema", result.stderr)

    def test_live_parsing_schema_must_agree_before_the_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--force", FAKE_LIVE_SCHEMA="ONE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is parsed by ONE", result.stderr)
            self.assertFalse(any(call.startswith("publish_app.sql") for call in self.calls(environment)))

    def test_schema_missing_from_the_staging_list_is_refused(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one\nSTAGING_EXPECTED_USER=SONE\nSTAGING_SCHEMA=ONE\n"
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            result = self.run_publish(script, environment, "--env", "staging")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is not listed in STAGING_SCHEMA", result.stderr)
            self.assertEqual([], self.calls(environment))

    def test_schema_missing_from_the_apex_profile_is_refused(self) -> None:
        replacements = {**TWO_SCHEMAS, "APEX_PARSING_SCHEMA=ONE,TWO": "APEX_PARSING_SCHEMA=ONE,THREE", "APEX_EXPECTED_USER=ONE,TWO": "APEX_EXPECTED_USER=ONE,THREE"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_fixture(root, "TWO", "TWO")
            (root / ".env").write_text(env_text({**replacements, "APEX_APP_ID=100,200": "APEX_APP_ID=117"}), encoding="utf-8")
            result = self.run_publish(script, environment, "--force")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is not listed in APEX_PARSING_SCHEMA", result.stderr)
```

Also append to `PUBLISH_REFUSALS` in `tests/test_documentation_contract.py` these pairs, and add matching rows to `docs/publish-rules.md` (Step 5):

```python
    ("scripts/publish_app.sh", "is stored under apps/"),
    ("scripts/publish_app.sh", "does not match the application's parsing schema"),
    ("scripts/publish_app.sh", "is not listed in APEX_PARSING_SCHEMA"),
    ("scripts/publish_app.sh", "is not listed in STAGING_SCHEMA"),
    ("scripts/publish_app.sh", "is parsed by"),
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_multi_schema_cli.PublishCliTests -v 2>&1 | tail -30
```

Expected: failures, because `publish_app.sh` does not yet select by schema (in a multi-schema `.env` the current script passes the CSV to SQLcl or fails on the app directory).

- [ ] **Step 3: Implement `publish_app.sh`**

1. Change the preferred-directory line to use the selected schema:

```bash
preferred_app_dir="$REPO_ROOT/apps/${PROJECT_SCHEMA:-$APEX_PARSING_SCHEMA}/$app_id"
```

In a multi-schema project without a selection `$APEX_PARSING_SCHEMA` is a comma list, so this directory does not exist and the existing scan of `apps/*/<id>` finds the single match.

2. Immediately after `IFS=$'\t' read -r workspace_name parsing_schema <<< "$deployment_values"` add:

```bash
# With several schemas the descriptor's parsing schema selects the connection.
# Folder, descriptor, --schema and (below) the live app must all agree.
if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  app_folder_schema="$(basename "$(dirname "$app_dir")")"
  if [ "$app_folder_schema" != "$parsing_schema" ]; then
    fail "application $app_id is stored under apps/$app_folder_schema but its descriptor parses as $parsing_schema; move the folder or fix the descriptor"
  fi
  if [ -n "${PROJECT_SCHEMA:-}" ] && [ "$PROJECT_SCHEMA" != "$parsing_schema" ]; then
    fail "--schema $PROJECT_SCHEMA does not match the application's parsing schema $parsing_schema"
  fi
  export PROJECT_SCHEMA="$parsing_schema"
  # shellcheck source=load_env.sh
  source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
fi
```

3. In the `case "$app_environment" in dev) … esac` block nothing changes (the re-sourced loader already narrowed the scalars). Right after the `if [ "$describe" = true ]; then … exit 0; fi` block, replace the existing `if [ "$app_environment" != dev ] && { [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; }; then …fi` block with:

```bash
if [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; then
  case "$app_environment" in
    dev)
      fail "schema $parsing_schema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
      ;;
    staging)
      if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
        fail "schema $parsing_schema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      fi
      fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
      ;;
    prod)
      if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
        fail "schema $parsing_schema is not listed in PROD_SCHEMA, so it cannot be published to production"
      fi
      fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
      ;;
  esac
fi
```

4. Add the live-schema check just before the `# Classify the target before the drift guard…` comment:

```bash
# The live application must be parsed by the schema the descriptor names. An
# application that is not there yet (first import) is allowed.
if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  mkdir -p "$REPO_ROOT/scratch"
  lookup_dir="$(mktemp -d "$REPO_ROOT/scratch/apex-lookup.XXXXXX")"
  live_schema=""
  if live_schema="$(sqlcl_app_parsing_schema "$sqlcl_connection" "$expected_user" "$parsing_schema" "$app_id" "$lookup_dir" 2>"$lookup_dir/error.txt")"; then
    if [ "$live_schema" != "$parsing_schema" ]; then
      rm -rf -- "$lookup_dir"
      fail "application $app_id is parsed by $live_schema, not the descriptor's $parsing_schema; refusing to import"
    fi
  elif ! grep -Fq "was not found in the workspace" "$lookup_dir/error.txt"; then
    cat "$lookup_dir/error.txt" >&2
    rm -rf -- "$lookup_dir"
    fail "could not verify the live parsing schema of application $app_id"
  fi
  rm -rf -- "$lookup_dir"
fi
```

`sqlcl_app_parsing_schema` returns non-zero with the text `was not found in the workspace` for an absent app; that is the only failure this block tolerates. `DB_ENVIRONMENT` must be exported for the helper; it is (loader exports it).

- [ ] **Step 4: Implement `publish_app.ps1` (twin), `deploy.sh`, and the probe guard**

**`scripts/publish_app.ps1`.** Four edits.

(a) Change the preferred directory line (`$preferredAppDir = …`) to:

```powershell
$selectedSchema = if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) { $env:PROJECT_SCHEMA } else { $env:APEX_PARSING_SCHEMA }
$preferredAppDir = Join-Path $repoRoot "apps/$selectedSchema/$AppId"
```

(b) Immediately after the `$parsingSchema` identifier check (the `throw "publish error: deployment parsingSchema must be an uppercase Oracle identifier"` block) add:

```powershell
# With several schemas the descriptor's parsing schema selects the connection.
# Folder, descriptor, --schema and (below) the live app must all agree.
if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
  $appFolderSchema = Split-Path -Leaf (Split-Path -Parent $appDir)
  if ($appFolderSchema -cne $parsingSchema) {
    throw "publish error: application $AppId is stored under apps/$appFolderSchema but its descriptor parses as $parsingSchema; move the folder or fix the descriptor"
  }
  if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $env:PROJECT_SCHEMA -cne $parsingSchema) {
    throw "publish error: --schema $($env:PROJECT_SCHEMA) does not match the application's parsing schema $parsingSchema"
  }
  $env:PROJECT_SCHEMA = $parsingSchema
  . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
}
```

(c) Replace the `if ($appEnvironment -ne "dev" -and (…IsNullOrWhiteSpace…)) { … }` missing-connection block (the one right after the `if ($describe) { … exit 0 }` block) with:

```powershell
if ([string]::IsNullOrWhiteSpace($sqlclConnection) -or [string]::IsNullOrWhiteSpace($expectedUser)) {
  switch ($appEnvironment) {
    "dev" {
      throw "publish error: schema $parsingSchema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
    }
    "staging" {
      if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
        throw "publish error: schema $parsingSchema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      }
      throw "publish error: set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
    }
    "prod" {
      if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
        throw "publish error: schema $parsingSchema is not listed in PROD_SCHEMA, so it cannot be published to production"
      }
      throw "publish error: set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
    }
  }
}
```

(d) Immediately before the `# Classify the target before the drift guard opens its read-only session.` comment add the live check. It needs `Get-AppParsingSchema`, so dot-source `invoke_sqlcl.ps1` here (the later dot-source of the same file stays; dot-sourcing twice is harmless):

```powershell
# The live application must be parsed by the schema the descriptor names. An
# application that is not there yet (first import) is allowed.
if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
  . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
  $lookupDir = Join-Path $repoRoot ("scratch/apex-lookup-" + [Guid]::NewGuid().ToString("N"))
  try {
    try {
      $liveSchema = Get-AppParsingSchema -Connection $sqlclConnection -ExpectedUser $expectedUser `
        -Schema $parsingSchema -AppId $AppId -WorkDirectory $lookupDir `
        -ScriptPath (Join-Path $repoRoot "scripts/lookup_app_schema.sql")
      if ($liveSchema -cne $parsingSchema) {
        throw "publish error: application $AppId is parsed by $liveSchema, not the descriptor's $parsingSchema; refusing to import"
      }
    } catch {
      if ($_.Exception.Message -like "*was not found in the workspace*") {
        # First import: nothing to compare.
      } elseif ($_.Exception.Message -like "publish error:*") {
        throw
      } else {
        throw "publish error: could not verify the live parsing schema of application ${AppId}: $($_.Exception.Message)"
      }
    }
  } finally {
    if (Test-Path -LiteralPath $lookupDir) { Remove-Item -LiteralPath $lookupDir -Recurse -Force -ErrorAction SilentlyContinue }
  }
}
```

**`scripts/deploy.sh`.** It only sources the loader and calls `publish_app.sh --describe`, so selection already works. Make two small changes: (1) replace the two missing-connection messages (the `fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to deploy to staging"` and PROD equivalents) with the same multi-aware form used in `publish_app.sh`:

```bash
if [ "$manual" != true ] && { [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; }; then
  if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
    fail "schema $parsing_schema is not listed in ${app_environment^^}_SCHEMA, so it cannot be deployed to $app_environment"
  fi
  if [ "$app_environment" = staging ]; then
    fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to deploy to staging"
  fi
  fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to deploy to production"
fi
```

and (2) show the connection in the confirmation summary by changing the `printf 'Application: %s\nWorkspace: %s\nTarget Schema: %s\n'` call to:

```bash
printf 'Application: %s\nWorkspace: %s\nTarget Schema: %s\nConnection: %s\n' \
  "$app_id" "$workspace_name" "$parsing_schema" "${sqlcl_connection:-<not configured>}"
```

If an existing `tests/test_publish_cli.py` or `tests/test_team_cli.py` deploy test asserts the exact summary text, update only that assertion to include the new `Connection:` line. Do not touch the `--manual` runbook output.

**`.agents/skills/apex-background/probe/run.sh`** (and the `.claude/skills` copy if `git ls-files .claude/skills/apex-background/probe/run.sh` lists it as a separate file): read the code around line 73 where it obtains `APEX_PARSING_SCHEMA` through `profile_value`. If that value can now legitimately be a comma list, make the probe refuse it: after the `profile_schema=…` assignment add

```bash
[[ "$profile_schema" != *,* ]] || fail 'several schemas are configured; set PROJECT_SCHEMA and use a single APEX_PARSING_SCHEMA entry for the probe'
```

Keep the change to those lines; do not restructure the script.

- [ ] **Step 5: Document the new refusals**

In `docs/publish-rules.md`, add one row/entry for each new message using the same format as the existing entries (read the file first and match its structure exactly): the verbatim text from the script, what it means (folder, descriptor, `--schema`, or live schema disagree, or the schema is not in the target list), and the fix (move the folder under `apps/<PARSING_SCHEMA>/`, correct the descriptor, drop or correct `--schema`, add the schema to the profile lists).

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m unittest tests.test_multi_schema_cli tests.test_publish_cli tests.test_documentation_contract -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/publish_app.sh scripts/publish_app.ps1 scripts/deploy.sh .agents .claude docs/publish-rules.md tests/test_multi_schema_cli.py tests/test_documentation_contract.py
git commit -m "feat: publish and deploy select and verify the application's schema"
```

Expected: all pass. The existing single-schema publish tests must be unchanged (`PROJECT_MULTI_SCHEMA=false` skips every new block).

---

### Task 7: Migrations, `check-conflicts`, and `compare-schema` per schema

**Files:**
- Modify: `scripts/migration_manifest.py`, `scripts/migrate.py`, `scripts/migration_checks.py`, `scripts/compare_schema.py`
- Test: `tests/test_migration_schema_folders.py` (create)

**Interfaces:**
- Consumes: Task 1 (`resolve_target(..., schema=)`, `batch_schema`, `configured_schemas`), Task 2 (`PROJECT_SCHEMA`, `PROJECT_MULTI_SCHEMA` in `os.environ`).
- Produces:
  - `Migration.schema: str | None = None` (last field, default `None`).
  - `load_migration` / `load_batch` accept `migrations/<SCHEMA>/<dated-folder>` besides `migrations/<dated-folder>`; `list_migration_folders` lists both.
  - `migrate.py`, `migration_checks.py` (live mode) and `compare_schema.py` accept `--schema NAME`, default it from `PROJECT_SCHEMA`, and resolve their targets by schema.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migration_schema_folders.py`:

```python
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import migration_manifest as manifest
from scripts.db_targets import TargetResolutionError, resolve_target


ROOT = Path(__file__).resolve().parents[1]
CHECKS = json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}) + "\n"


def add_folder(root: Path, relative: str, sql: str = "CREATE TABLE T1 (ID NUMBER);\n") -> Path:
    folder = root / relative
    folder.mkdir(parents=True)
    (folder / "001-change.sql").write_text(sql, encoding="utf-8", newline="\n")
    (folder / "checks.json").write_text(CHECKS, encoding="utf-8")
    return folder


class SchemaFolderManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def test_schema_folder_migration_loads_with_its_schema(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        migration = manifest.load_migration(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        self.assertEqual("TMS", migration.schema)
        self.assertEqual("create-t1", migration.family)

    def test_flat_folder_has_no_schema(self) -> None:
        add_folder(self.root, "migrations/2026-09-29_create-t1-r001")
        self.assertIsNone(manifest.load_migration(self.root, "migrations/2026-09-29_create-t1-r001").schema)

    def test_listing_includes_both_layouts_newest_first(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        add_folder(self.root, "migrations/APR/2026-09-30_create-t2-r001")
        names = [path.name for path in manifest.list_migration_folders(self.root)]
        self.assertEqual(["2026-09-30_create-t2-r001", "2026-09-29_create-t1-r001"], names)

    def test_same_family_and_revision_in_two_schemas_is_not_a_duplicate(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APR/2026-09-29_add-flag-r001")
        self.assertEqual(2, len(manifest.list_migration_folders(self.root)))

    def test_revision_gap_is_checked_per_schema(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APR/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APR/2026-09-30_add-flag-r003")
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.list_migration_folders(self.root)

    def test_lowercase_directory_is_still_a_legacy_error(self) -> None:
        (self.root / "migrations" / "old-style").mkdir()
        with self.assertRaises(manifest.MigrationManifestError) as raised:
            manifest.list_migration_folders(self.root)
        self.assertIn("legacy or invalid migration directory", str(raised.exception))

    def test_three_part_path_needs_a_schema_shaped_directory(self) -> None:
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_migration(self.root, "migrations/tms/2026-09-29_create-t1-r001")
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_migration(self.root, "migrations/A/B/2026-09-29_create-t1-r001")

    def test_batch_family_order_is_per_schema(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/TMS/2026-09-30_add-flag-r002")
        batch = manifest.load_batch(self.root, ["migrations/TMS/2026-09-29_add-flag-r001", "migrations/TMS/2026-09-30_add-flag-r002"])
        self.assertEqual(["TMS", "TMS"], [item.schema for item in batch])
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_batch(self.root, ["migrations/TMS/2026-09-30_add-flag-r002", "migrations/TMS/2026-09-29_add-flag-r001"])


class SchemaFolderCliTests(unittest.TestCase):
    MULTI_ENV = {
        "PROJECT_ENV_FILE": "",
        "DB_ENVIRONMENT": "development",
        "CODE_SQLCL_CONNECTION": "conn-tms,conn-apr",
        "CODE_EXPECTED_USER": "TMS,APR",
        "CODE_SCHEMA": "TMS,APR",
        "PROJECT_MULTI_SCHEMA": "true",
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def run_checker(self, *arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "check_conflicts.sh"), "--repo-root", str(self.root), *arguments],
            cwd=ROOT, text=True, capture_output=True, check=False, env={**base, **(environment or {})},
        )

    def test_local_check_accepts_a_schema_folder(self) -> None:
        add_folder(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        result = self.run_checker("migrations/TMS/2026-09-29_create-t1-r001", "--local")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)

    def test_migrate_refuses_a_flat_folder_when_several_schemas_are_configured(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/2026-09-29_create-t1-r001")
        code = migrate.main(
            ["migrations/2026-09-29_create-t1-r001", "--env", "dev"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_migrate_refuses_a_batch_that_mixes_schemas(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        add_folder(self.root, "migrations/APR/2026-09-29_create-t2-r001")
        code = migrate.main(
            ["migrations/TMS/2026-09-29_create-t1-r001", "migrations/APR/2026-09-29_create-t2-r001", "--env", "dev"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_migrate_refuses_a_schema_option_that_disagrees_with_the_folder(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/TMS/2026-09-29_create-t1-r001")
        code = migrate.main(
            ["migrations/TMS/2026-09-29_create-t1-r001", "--env", "dev", "--schema", "APR"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_resolver_picks_the_folders_schema_for_dev(self) -> None:
        target = resolve_target(self.MULTI_ENV, "dev", "migration", schema="APR")
        self.assertEqual(("conn-apr", "APR", "APR"), (target.connection, target.expected_user, target.schema))
        with self.assertRaises(TargetResolutionError):
            resolve_target(self.MULTI_ENV, "dev", "migration")


class CompareSchemaOptionTests(unittest.TestCase):
    def test_compare_requires_schema_when_several_are_configured(self) -> None:
        from scripts import compare_schema
        environment = {
            "DB_ENVIRONMENT": "development",
            "CODE_SQLCL_CONNECTION": "conn-a,conn-b", "CODE_EXPECTED_USER": "AAA,BBB", "CODE_SCHEMA": "AAA,BBB",
            "STAGING_SQLCL_CONNECTION": "stage-a,stage-b", "STAGING_EXPECTED_USER": "SAAA,SBBB", "STAGING_SCHEMA": "AAA,BBB",
        }
        code = compare_schema.main(["--env", "staging", "--object", "T1"], environ=environment)
        self.assertNotEqual(0, code)


if __name__ == "__main__":
    unittest.main()
```

`compare_schema.main(...)` keeps its existing keyword `environ`; read its signature and the first 30 lines of `main` at `scripts/compare_schema.py:541` and adapt the assertion only if `main` prints and returns a different failure code than `!= 0` (the important check is that it fails and names `--schema`; also assert the captured stderr contains `--schema` using `contextlib.redirect_stderr` if `main` writes there).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_migration_schema_folders -v 2>&1 | tail -30
```

Expected: manifest tests fail (`Migration` has no `schema`, three-part paths rejected); CLI tests fail (`--schema` unknown).

- [ ] **Step 3: Implement the manifest changes**

In `scripts/migration_manifest.py`:

1. Add after `CHECK_ID_RE`: `SCHEMA_DIRECTORY_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)`.
2. Add `schema: str | None = None` as the **last** field of the frozen `Migration` dataclass (after `payload_digest`).
3. Replace `_migration_directories` with (returns `(path, schema, family, revision)` 4-tuples):

```python
def _migration_directories(repo_root: Path) -> list[tuple[Path, str | None, str, int]]:
    root = repo_root.resolve()
    migrations_dir = root / "migrations"
    if migrations_dir.is_symlink() or not migrations_dir.is_dir():
        raise MigrationManifestError(f"migrations directory does not exist or is unsafe: {migrations_dir}")
    directories: list[tuple[Path, str | None, str, int]] = []

    def add_dated(path: Path, schema: str | None) -> None:
        if FOLDER_RE.fullmatch(path.name) is None:
            raise MigrationManifestError(
                f"legacy or invalid migration directory '{path.name}'; convert it to YYYY-MM-DD_<name>-rNNN"
            )
        _, family, revision = _folder_parts(path.name)
        directories.append((path, schema, family, revision))

    def skip_entry(path: Path, label: str) -> bool:
        if path.is_symlink():
            raise MigrationManifestError(f"symbolic link is not allowed in {label}: {path.name}")
        if path.name == ".gitkeep" and path.is_file():
            return True
        if not path.is_dir():
            if path.suffix.lower() == ".sql":
                raise MigrationManifestError("root-level SQL is not supported; put SQL in a dated migration folder")
            return True
        return False

    for path in sorted(migrations_dir.iterdir(), key=lambda entry: entry.name):
        if skip_entry(path, "migrations/"):
            continue
        if FOLDER_RE.fullmatch(path.name) is not None:
            add_dated(path, None)
            continue
        if SCHEMA_DIRECTORY_RE.fullmatch(path.name) is None:
            raise MigrationManifestError(
                f"legacy or invalid migration directory '{path.name}'; convert it to YYYY-MM-DD_<name>-rNNN"
            )
        for child in sorted(path.iterdir(), key=lambda entry: entry.name):
            if skip_entry(child, f"migrations/{path.name}/"):
                continue
            add_dated(child, path.name)

    seen: dict[tuple[str | None, str, int], Path] = {}
    revisions: dict[tuple[str | None, str], set[int]] = {}
    for path, schema, family, revision in directories:
        identity = (schema, family, revision)
        if identity in seen:
            raise MigrationManifestError(
                f"duplicate local migration identity {family}-r{revision:03d}: "
                f"{seen[identity].name} and {path.name}"
            )
        seen[identity] = path
        revisions.setdefault((schema, family), set()).add(revision)
    for (schema, family), values in revisions.items():
        expected = set(range(1, max(values) + 1))
        if values != expected:
            raise MigrationManifestError(f"migration family {family} has a revision gap; revisions start at r001")
    return directories
```

4. `list_migration_folders`: `return tuple(sorted((path for path, _, _, _ in directories), key=lambda path: path.name, reverse=True))`.
5. `_validate_relative_folder`: keep the original message for every wrong-shape case (including `migrations/<dated-folder>/<file>`, which `tests/test_check_conflicts.py` expects to be reported as a legacy file path), allowing two or three parts:

```python
    parts = tuple(relative_folder.split("/"))
    legacy = "use a repository-relative migrations/<dated-folder> path; legacy file paths are not supported"
    if len(parts) not in (2, 3) or parts[0] != "migrations" or any(part in {"", ".", ".."} for part in parts):
        raise MigrationManifestError(legacy)
    if len(parts) == 3 and FOLDER_RE.fullmatch(parts[1]) is not None:
        # migrations/<dated-folder>/<file>: a legacy file path, not a schema folder.
        raise MigrationManifestError(legacy)
    if len(parts) == 3 and SCHEMA_DIRECTORY_RE.fullmatch(parts[1]) is None:
        raise MigrationManifestError("the schema directory in migrations/<SCHEMA>/<dated-folder> must be an uppercase Oracle identifier")
    return parts
```

6. `load_migration`: replace the `directories`/`any(...)` lines and the `Migration(...)` construction:

```python
    directories = _migration_directories(root)
    schemas = [schema for candidate, schema, _, _ in directories if candidate == folder]
    if not schemas:
        raise MigrationManifestError(f"migration folder is not directly under migrations/ or migrations/<SCHEMA>/: {relative_folder}")
```

and add `schema=schemas[0],` as the last argument of `Migration(...)`.

7. `load_batch`: key the ascending-revision check by schema too: `previous: dict[tuple[str | None, str], int] = {}`, and use `key = (migration.schema, migration.family)` for the `get`/set.

- [ ] **Step 4: Implement `--schema` in `migrate.py`, `migration_checks.py`, `compare_schema.py`**

`scripts/migrate.py` — add `batch_schema` to the `.db_targets` import, add `parser.add_argument("--schema")` in `main`, and replace the `try:` block that loads and resolves:

```python
    try:
        migrations = load_batch(Path(repo_root), args.folders)
        values = os.environ if environ is None else environ
        requested = args.schema or values.get("PROJECT_SCHEMA") or None
        schema = batch_schema([migration.schema for migration in migrations], requested, values)
        target = resolve_target(values, args.env[0], "migration", schema=schema)
    except (MigrationManifestError, TargetResolutionError, OSError) as error:
        print(f"migration error: {error}", file=sys.stderr)
        return 2
```

`scripts/migration_checks.py` — import `batch_schema` beside `Target`; in `main` add `parser.add_argument("--schema")` next to `--env`; keep the `--local` branch untouched (no environment is loaded, so the layout rule is not enforced there, which the docs must state). In `_live_report` change the signature to `_live_report(migrations, environment, repo_root, schema=None)`, and inside its `try` replace `target = resolve_target(values, environment, "read")` with:

```python
        requested = schema or values.get("PROJECT_SCHEMA") or None
        chosen = batch_schema([migration.schema for migration in migrations], requested, values)
        target = resolve_target(values, environment, "read", schema=chosen)
```

Confirm `_live_report`'s `except` clause already catches `TargetResolutionError` (it imports it inside the function); if a `batch_schema` failure would escape, add `TargetResolutionError` to the caught tuple so the report uses the existing `LIVE_PREFLIGHT_UNAVAILABLE` error path. Pass `args.schema` from `main` into `_live_report`.

`scripts/compare_schema.py` — add `parser.add_argument("--schema", help="configured schema to compare; required when several are configured")` next to `--object`, and pass `schema=args.schema or values.get("PROJECT_SCHEMA") or None` to both `resolve_target` calls at lines ~575-576 (`source_target`, `target_target`). Its existing `except TargetResolutionError` path prints the message, which already contains `--schema` for the several-schemas case.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_migration_schema_folders tests.test_migration_manifest tests.test_check_conflicts tests.test_migrate_cli tests.test_compare_schema tests.test_compare_schema_cli -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/migration_manifest.py scripts/migrate.py scripts/migration_checks.py scripts/compare_schema.py tests/test_migration_schema_folders.py
git commit -m "feat: migrations, conflict checks and schema comparison per schema"
```

Expected: all pass. If an existing manifest test asserts the tuple shape of `_migration_directories`, update only that test's unpacking; the arity change is intentional.

---

### Task 8: Graphify follows cross-schema references and synonyms

**Files:**
- Modify: `scripts/graphify_apexlang_extractor.py`, `tests/test_graphify_pipeline.py`
- Create: `tests/test_graphify_multi_schema.py`
- Port: `/home/ash/projects/APEX_PROJECT_TEMPLATE` (Step 6)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DatabaseMirror` indexes every `database/<schema>` directory once per root, and:
  - `table(label)`: 1 part → own schema; 2 parts `SCHEMA.NAME` → that schema; either falls back to a synonym in the named schema (one hop, to that synonym's target schema/name).
  - `package(label)`: 2 parts `PKG.MEMBER` → own schema; 3 parts `SCHEMA.PKG.MEMBER` → that schema; with the same one-hop synonym fallback.
  - `DatabaseMirror.clear_cache()` classmethod for tests.
  - A synonym whose `FOR` target has a database link (`@`) is not indexed and resolves to nothing.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_graphify_multi_schema.py`:

```python
#!/usr/bin/env python3
"""DatabaseMirror across several schemas and synonyms."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"


@unittest.skipUnless(MODULE_PATH.is_file(), "canonical extractor is missing")
class MultiSchemaMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("graphify_apexlang_extractor_multi", MODULE_PATH)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def setUp(self) -> None:
        self.module.DatabaseMirror.clear_cache()
        self.root = Path(tempfile.mkdtemp(prefix="graphify-multi."))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def mirror(self, schema: str):
        return self.module.DatabaseMirror(self.root, schema)

    def node_id(self, relative: str, schema: str, name: str) -> str:
        make_id = self.module.make_id
        return make_id(make_id(relative.removesuffix(".sql")), schema, name)

    def test_qualified_table_resolves_in_the_schema_it_names(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        mirror = self.mirror("DEMO")
        self.assertEqual(self.node_id("database/OTHER/tables/WIDGETS.sql", "OTHER", "WIDGETS"), mirror.table("OTHER.WIDGETS"))
        self.assertEqual(self.node_id("database/DEMO/tables/USERS.sql", "DEMO", "USERS"), mirror.table("USERS"))
        self.assertEqual(mirror.table("USERS"), mirror.table("DEMO.USERS"))

    def test_an_unqualified_name_does_not_leak_into_another_schema(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        self.assertIsNone(self.mirror("DEMO").table("WIDGETS"))

    def test_unmirrored_schema_or_object_stays_unresolved(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        mirror = self.mirror("DEMO")
        self.assertIsNone(mirror.table("NOPE.THING"))
        self.assertIsNone(mirror.table("DEMO.MISSING"))
        self.assertIsNone(mirror.table("A.B.C"))

    def test_a_synonym_resolves_to_the_target_schemas_table_in_one_hop(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/WIDGET_SYN.sql", '\n  CREATE OR REPLACE EDITIONABLE SYNONYM "DEMO"."WIDGET_SYN" FOR "OTHER"."WIDGETS";\n')
        mirror = self.mirror("DEMO")
        expected = self.node_id("database/OTHER/tables/WIDGETS.sql", "OTHER", "WIDGETS")
        self.assertEqual(expected, mirror.table("WIDGET_SYN"))
        self.assertEqual(expected, mirror.table("DEMO.WIDGET_SYN"))

    def test_an_unqualified_synonym_target_stays_in_the_synonyms_schema(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/PEOPLE.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."PEOPLE" FOR "USERS";\n')
        self.assertEqual(self.mirror("DEMO").table("USERS"), self.mirror("DEMO").table("PEOPLE"))

    def test_a_database_link_synonym_resolves_to_nothing(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/REMOTE_W.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."REMOTE_W" FOR "OTHER"."WIDGETS"@"REMOTE_LINK";\n')
        self.assertIsNone(self.mirror("DEMO").table("REMOTE_W"))

    def test_a_synonym_is_followed_only_one_hop(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/OTHER/synonyms/W1.sql", 'CREATE OR REPLACE SYNONYM "OTHER"."W1" FOR "OTHER"."WIDGETS";\n')
        self.write("database/DEMO/synonyms/W2.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."W2" FOR "OTHER"."W1";\n')
        self.assertIsNone(self.mirror("DEMO").table("W2"))

    def test_packages_resolve_by_schema_and_through_synonyms(self) -> None:
        self.write("database/OTHER/packages/OTHER_PKG_SPEC.sql", 'CREATE OR REPLACE PACKAGE "OTHER"."OTHER_PKG" AS\nEND;\n')
        self.write("database/DEMO/synonyms/PKG_SYN.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."PKG_SYN" FOR "OTHER"."OTHER_PKG";\n')
        mirror = self.mirror("DEMO")
        expected = self.module.make_id("database/OTHER/packages/OTHER_PKG_SPEC")
        self.assertEqual(expected, mirror.package("OTHER.OTHER_PKG.DO_IT"))
        self.assertEqual(expected, mirror.package("PKG_SYN.DO_IT"))
        self.assertIsNone(mirror.package("OTHER_PKG.DO_IT"))

    def test_the_index_is_built_once_per_root(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        scans = []
        original = self.module.DatabaseMirror._scan_root
        self.module.DatabaseMirror._scan_root = staticmethod(lambda root: scans.append(root) or original(root))
        self.addCleanup(setattr, self.module.DatabaseMirror, "_scan_root", original)
        self.mirror("DEMO").table("USERS")
        self.mirror("DEMO").table("USERS")
        self.module.DatabaseMirror(self.root, "OTHER").table("X")
        self.assertEqual(1, len(scans))

    def test_a_mirror_without_a_root_resolves_nothing(self) -> None:
        mirror = self.module.DatabaseMirror()
        self.assertIsNone(mirror.table("USERS"))
        self.assertIsNone(mirror.package("PKG.FN"))

    def test_the_schema_of_an_application_file_is_still_taken_from_its_path(self) -> None:
        page = self.write("apps/DEMO/102/pages/p00001-home.apx", "page 1 (\n    name: Home\n)\n")
        mirror = self.module.DatabaseMirror.for_application_file(page)
        self.assertEqual("DEMO", mirror.schema)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Extend the real-pipeline test**

In `tests/test_graphify_pipeline.py`:

1. Add to the `FILES` dict:

```python
    "database/OTHER/tables/WIDGETS.sql": (
        'CREATE TABLE "OTHER"."WIDGETS"\n'
        '   ("ID" NUMBER NOT NULL ENABLE) ;\n'
    ),
    "database/DEMO/synonyms/WIDGET_SYN.sql": (
        '  CREATE OR REPLACE EDITIONABLE SYNONYM "DEMO"."WIDGET_SYN" FOR "OTHER"."WIDGETS";\n'
    ),
    "apps/DEMO/102/pages/p00006-widgets.apx": (
        "page 6 (\n"
        "    name: Widgets\n"
        "    region direct (\n"
        "        name: Direct\n"
        "        source {\n"
        "            sqlQuery: select w.id from other.widgets w\n"
        "        }\n"
        "    )\n"
        "    region via-synonym (\n"
        "        name: Via synonym\n"
        "        source {\n"
        "            sqlQuery: select s.id from widget_syn s\n"
        "        }\n"
        "    )\n"
        ")\n"
    ),
```

2. In `setUpClass`, add `"labels": sorted(node.get("label", "") for node in graph["nodes"]),` to `cls.result`.
3. In `test_page_reads_land_on_the_mirrored_table_nodes`, the expected label set now also includes the new table: change the final `assertEqual` to `{'"DEMO"."ORDERS"', '"DEMO"."USERS"', '"OTHER"."WIDGETS"'}`. Its loop over `edge["target_file"].startswith("database/DEMO/tables/")` must become `startswith(("database/DEMO/tables/", "database/OTHER/tables/"))`.
4. Add:

```python
    def test_cross_schema_and_synonym_reads_land_on_the_other_schemas_table(self) -> None:
        widget_edges = [edge for edge in self.edges("reads_from") if "WIDGETS" in edge["target_label"]]
        self.assertTrue(widget_edges, "the page never linked to OTHER.WIDGETS")
        for edge in widget_edges:
            self.assertEqual("database/OTHER/tables/WIDGETS.sql", edge["target_file"], edge)
        self.assertNotIn("WIDGET_SYN", " ".join(self.result["labels"]), "the synonym must not survive as a stub node")
```

The synonym file itself is a node labelled with its file name; the assertion uses `"WIDGET_SYN"` in labels, which would match a synonym *file* node. If the real graph labels the synonym file `WIDGET_SYN.sql`, change the assertion to check only stub nodes: nodes with an empty `source_file` whose label equals `WIDGET_SYN`. To do that, add `"stubs": sorted(node["label"] for node in graph["nodes"] if not node.get("source_file"))` to `cls.result` and assert `self.assertNotIn("WIDGET_SYN", self.result["stubs"])`. Prefer this stub-based form; keep `labels` only if something else uses it.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
python3 -m unittest tests.test_graphify_multi_schema tests.test_graphify_pipeline -v 2>&1 | tail -30
```

Expected: `clear_cache` / `_scan_root` attribute errors; the pipeline's cross-schema test fails (or skips if Graphify is not installed; if it skips, note it in the final report, do not treat a skip as a pass).

- [ ] **Step 4: Implement the mirror**

In `scripts/graphify_apexlang_extractor.py` replace the whole `class DatabaseMirror` (from its docstring through the `package` method) with the class below, keeping `for_application_file`/`for_database_file` semantics. Add `SYNONYM_TARGET_RE` at module level next to the other regexes, or as a class attribute as shown.

```python
class DatabaseMirror:
    """Resolve references to objects mirrored under ``database/<SCHEMA>/``.

    Graphify's SQL extractor names a table node ``<file id>_<schema>_<table>``
    and cannot rewire a bare-name stub onto it: a schema-qualified label
    contains a dot, which disqualifies it as a rewire target. A reference from
    an application file therefore has to name that node id itself, or the
    application and database halves of the graph never connect. Anything the
    mirror does not hold (APEX dictionary views, DUAL, unexported objects, a
    synonym over a database link) stays a stub.

    Every ``database/*`` schema is indexed, so a qualified name reaches another
    schema's object, and a mirrored synonym is followed one hop to its target.
    """

    TABLE_FOLDERS = ("tables", "views")
    # CREATE ... SYNONYM "S"."N" FOR "T"."O";  A database link ("O"@link) has
    # no ';' straight after the name, so it does not match.
    SYNONYM_TARGET_RE = re.compile(
        r'\bFOR\s+(?:"?([A-Za-z0-9_$#]+)"?\s*\.\s*)?"?([A-Za-z0-9_$#]+)"?\s*;',
        re.IGNORECASE,
    )
    _INDEX_CACHE: dict[str, dict[str, dict[str, dict]]] = {}

    def __init__(self, root: Path | None = None, schema: str | None = None) -> None:
        self.root = root
        self.schema = schema

    @classmethod
    def clear_cache(cls) -> None:
        cls._INDEX_CACHE.clear()

    @classmethod
    def for_application_file(cls, source: Path) -> "DatabaseMirror":
        """Locate the mirror from ``<root>/apps/<schema>/<app id>/...``."""
        parts = source.parts
        for index in range(len(parts) - 3, -1, -1):
            if parts[index] == "apps" and parts[index + 2].isdigit():
                return cls(Path(*parts[:index]) if index else Path(), parts[index + 1])
        return cls()

    @classmethod
    def for_database_file(cls, source: Path) -> "DatabaseMirror":
        """Locate the mirror from ``<root>/database/<schema>/<folder>/...``."""
        parts = source.parts
        for index in range(len(parts) - 4, -1, -1):
            if parts[index] == "database":
                return cls(Path(*parts[:index]) if index else Path(), parts[index + 1])
        return cls()

    @staticmethod
    def _sql_files(directory: Path) -> list[Path]:
        try:
            return sorted(directory.glob("*.sql"))
        except OSError:
            return []

    @classmethod
    def _scan_root(cls, root: Path) -> dict[str, dict[str, dict]]:
        """Index tables, packages and synonyms of every mirrored schema."""
        index: dict[str, dict[str, dict]] = {}
        try:
            schema_dirs = sorted(path for path in (root / "database").iterdir() if path.is_dir())
        except OSError:
            return index
        for schema_dir in schema_dirs:
            entry: dict[str, dict] = {"tables": {}, "packages": {}, "synonyms": {}}
            for folder in cls.TABLE_FOLDERS:
                for file in cls._sql_files(schema_dir / folder):
                    file_id = make_id(file.relative_to(root).with_suffix("").as_posix())
                    entry["tables"].setdefault(
                        file.stem.upper(), make_id(file_id, schema_dir.name, file.stem)
                    )
            for file in cls._sql_files(schema_dir / "packages"):
                name = file.stem.upper()
                for suffix in ("_SPEC", "_BODY"):
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
                        break
                file_id = make_id(file.relative_to(root).with_suffix("").as_posix())
                # The specification is the package's public face; prefer it.
                if file.stem.upper().endswith("_SPEC") or name not in entry["packages"]:
                    entry["packages"][name] = file_id
            for file in cls._sql_files(schema_dir / "synonyms"):
                try:
                    text = file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                match = cls.SYNONYM_TARGET_RE.search(text)
                if match:
                    target_schema = (match.group(1) or schema_dir.name).upper()
                    entry["synonyms"][file.stem.upper()] = (target_schema, match.group(2).upper())
            index[schema_dir.name.upper()] = entry
        return index

    def _index(self) -> dict[str, dict[str, dict]]:
        if self.root is None:
            return {}
        key = str(self.root.resolve())
        if key not in self._INDEX_CACHE:
            self._INDEX_CACHE[key] = self._scan_root(self.root)
        return self._INDEX_CACHE[key]

    def _resolve(self, schema: str, name: str, kind: str) -> str | None:
        """Find *name* in *schema*, or through one synonym of that schema."""
        index = self._index()
        entry = index.get(schema.upper())
        if entry is None:
            return None
        found = entry[kind].get(name)
        if found:
            return found
        target = entry["synonyms"].get(name)
        if target is None:
            return None
        target_entry = index.get(target[0])
        return target_entry[kind].get(target[1]) if target_entry else None

    def table(self, label: str) -> str | None:
        parts = label.upper().split(".")
        if len(parts) == 1:
            schema, name = (self.schema or ""), parts[0]
        elif len(parts) == 2:
            schema, name = parts
        else:
            return None
        return self._resolve(schema, name, "tables")

    def package(self, label: str) -> str | None:
        parts = label.upper().split(".")
        if len(parts) == 3:
            schema, name = parts[0], parts[1]
        elif len(parts) == 2:
            schema, name = (self.schema or ""), parts[0]
        else:
            return None
        return self._resolve(schema, name, "packages")
```

`extract_sql_linked` and `add_reference_node`/`mirrored_members` need no code change. Note the behavior differences to keep in mind when running the old tests: the index is now cached per root, so any existing test that writes files, builds a mirror, edits `database/` and builds another mirror **on the same path** in one process needs `DatabaseMirror.clear_cache()` in its `setUp`. Add that call to `ApexlangExtractorTests` (in `tests/test_graphify_apexlang_extractor.py`) only if a failing test proves it is needed.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_graphify_multi_schema tests.test_graphify_apexlang_extractor tests.test_graphify_pipeline tests.test_graphify_corpus tests.test_setup_graphify -v 2>&1 | tail -30
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add scripts/graphify_apexlang_extractor.py tests/test_graphify_multi_schema.py tests/test_graphify_pipeline.py
git commit -m "feat: link cross-schema references and synonyms in the Graphify mirror"
```

- [ ] **Step 6: Port to the standalone template**

The standalone `/home/ash/projects/APEX_PROJECT_TEMPLATE` has no `team.sh` and a different test layout (tests live in `scripts/` and are registered in `scripts/test_template.sh`), so only the Graphify change is ported. Do not port Tasks 1-7 there.

```bash
cd /home/ash/projects/APEX_PROJECT_TEMPLATE
git checkout main && git status --short   # must be clean; stop and report if not
git checkout -b feat/multi-schema-graphify
cp /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM/scripts/graphify_apexlang_extractor.py scripts/graphify_apexlang_extractor.py
cp /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM/tests/test_graphify_multi_schema.py scripts/test_graphify_multi_schema.py
```

Then in `scripts/test_graphify_multi_schema.py` change `REPO_ROOT = Path(__file__).resolve().parent.parent` (unchanged: the file now sits in `scripts/`, one level below the repo root, same as the team repo's `tests/`) and `MODULE_PATH = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"` (also unchanged). Apply the same `FILES`/assertion edits to `scripts/test_graphify_pipeline.py` as in Step 2 (its `EXTRACTOR` path is already `REPO_ROOT / "scripts" / …`; read the file first and adapt paths only if they differ). Register the new test in `scripts/test_template.sh` next to the other graphify tests, following its existing pattern. Run that script's graphify tests:

```bash
python3 scripts/test_graphify_multi_schema.py
python3 scripts/test_graphify_pipeline.py
python3 scripts/test_graphify_apexlang_extractor.py
python3 scripts/test_graphify_corpus.py
python3 scripts/test_setup_graphify.py
git add scripts/graphify_apexlang_extractor.py scripts/test_graphify_multi_schema.py scripts/test_graphify_pipeline.py scripts/test_template.sh
git commit -m "feat: link cross-schema references and synonyms in the Graphify mirror"
cd /home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM
```

Confirm the two `graphify_apexlang_extractor.py` files are byte-identical (`cmp`). The standalone repo's extractor was kept identical to the team one before this task.

---

### Task 9: Documentation

**Files:**
- Modify: `AGENTS.md`, `README.md`, `docs/migration-rules.md`, `migrations/README.md`, `.agents/rules/graphify.md`, `.agents/workflows/graphify.md`, `.agents/skills/initialize-project/SKILL.md`, `.claude/skills/initialize-project/SKILL.md`
- Test: `tests/test_documentation_contract.py` (run only; extend if a doc rule requires it)

**Interfaces:**
- Consumes: the behavior implemented in Tasks 1-8.
- Produces: user-visible documentation of exactly that behavior; nothing here changes behavior.

- [ ] **Step 1: Write the documentation**

Read each file first and add to the existing structure. Required content, stated once so the docs and code cannot drift:

- **`AGENTS.md`** (Configuration section): one bullet that the schema, connection and expected-user keys of each profile accept position-aligned comma lists, that a single value is unchanged, that `--schema <NAME>` narrows any command, and that `doctor` and `backup-db` default to all schemas. Add a bullet under Migrations that multi-schema projects use `migrations/<SCHEMA>/…` and one migration changes one schema. Under Optional Tooling, one sentence that Graphify links cross-schema references and mirrored synonyms.
- **`README.md`**: a "Several schemas in one workspace" section with the `.env` example from the spec (two schemas), the rule list (equal lengths, unique schemas, same names in staging/prod, prefixes apply to all), the per-command table from the spec, the `migrations/<SCHEMA>/` layout rule, the `database/<SCHEMA>/synonyms/` mirror, and the limits (positional lists rely on order; schemas sharing a connection share its privileges; `--local` conflict checks cannot enforce the layout rule because they load no `.env`; `publish --force` skips the drift check but not the schema agreement checks).
- **`docs/migration-rules.md`** and **`migrations/README.md`**: the `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/` layout, the flat-layout compatibility rule (one code schema only), per-schema revision numbering (`r001` per schema and family), and "a cross-schema change such as a grant or a synonym is two coordinated migrations, one per schema".
- **`.agents/rules/graphify.md`** and **`.agents/workflows/graphify.md`**: the cross-schema/synonym resolution rules and limits from the spec (`FOR "SCHEMA"."OBJECT"` one hop; database links and unmirrored targets stay stubs; expression-style three-part package references without parentheses are not detected; the index is cached per root, so rerun `python3 scripts/setup_graphify_apx.py` after mirror changes as today).
- **`.agents/skills/initialize-project/SKILL.md`** and its `.claude` copy (they are two tracked files; edit both identically): where the skill writes `TABLES_SCHEMA=<schema>` etc., add one sentence that several schemas can be given as comma lists and point at the README section.

- [ ] **Step 2: Run the documentation tests**

```bash
python3 -m unittest tests.test_documentation_contract tests.test_template_manifest -v 2>&1 | tail -20
```

Expected: pass. If the contract test rejects a term you used (for example a retired term), reword the doc, not the test.

- [ ] **Step 3: Full-suite gate and commit**

```bash
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git add AGENTS.md README.md docs/migration-rules.md migrations/README.md .agents .claude
git commit -m "docs: document multi-schema configuration and workflow"
```

- [ ] **Step 4: Final verification for the reviewer**

Run and paste the output in the report:

```bash
git log --oneline main..HEAD
git diff --stat main..HEAD | tail -5
python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
git status --short
```

Then list, explicitly: which tests were skipped and why (PowerShell absent, Graphify absent), every place you deviated from this plan and why, and every `.ps1` change (so the reviewer can diff it against its `.sh` twin). Do not push and do not merge.
