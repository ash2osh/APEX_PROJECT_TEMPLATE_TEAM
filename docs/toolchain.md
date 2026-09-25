# Qualified team toolchain

The team workflow is intentionally conservative about the tools that can touch
an application. A Python core launches SQLcl with an argv list, a regular
empty stdin file, UTF-8 output, and a generated driver that records identity
before and after the payload. Shell and PowerShell files are compatibility
launchers only.

Before a protected online run, record the outputs of `sql -V`, `java -version`,
and the APEX release shown by the verified identity query. Runtime acceptance
uses the repository's qualified commands against prepared persistent targets:

```bash
PYTHONPATH=scripts python3 scripts/team.py --env integration.env \\
  run-integration --out scratch/integration-evidence.json
PYTHONPATH=scripts python3 scripts/team.py --env test.env \\
  run-release-test scratch/release/release.tar --target targets/test.json \\
  --out scratch/test-evidence.json
```

One SQLcl process is bounded by `TEAM_SQLCL_TIMEOUT` seconds, defaulting to 120.
APEX export and import use a longer built-in budget because a timeout there
marks the shared application uncertain and pauses the team. Raise the variable
for a slow link; do not lower it below the time a metadata write needs.

These commands expect the five credential-free profile names to resolve through
the local SQLcl connection store. They verify identity and versions before any
write and refuse production. Persistent qualification proves only the observed
target state; it does not prove a fresh installation or isolation. A local
`docker-demo` profile is never an implicit destructive-test target.

Database-built release captures require SQLcl production build
`26.2.2.233.1901`. The version is checked before the schema-release command
bootstraps metadata or reads the live frontier, and format-3 archives record
the observed build. Application release captures use the same pin.

APEX exports are accepted only when the run has two matching identity
observations, a positive operation completion record, `application.apx`,
`.apex/apexlang.json`, and a complete owned tree. `.apx` and APEXlang metadata
are normalized to LF; binaries are hashed byte-for-byte. Production drivers are
SELECT-only and all writes are rejected before SQLcl starts.

`INSTANCE_ID` is the verified `INSTANCE_NAME@SERVER_HOST` pair. The host suffix
is intentional: cloned Oracle Free containers can otherwise all report the
same `INSTANCE_NAME` (`FREE`) and incorrectly share a physical lock key.

Offline portability remains separate and does not claim Oracle acceptance:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```

## Local test environment

On Debian, the system `cryptography` package can crash during import in the
`gen-runbook` signing-verification path. Use a project virtual environment for
the promotion and development extras instead of the system package:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[promotion,dev]'
.venv/bin/python -m unittest discover -s scripts/tests -v
.venv/bin/python -m ruff check scripts/
```

The daily workflow still uses only the Python standard library; these optional
packages are needed for signed handoff verification and offline linting.

## Target contracts (version 2)

Target contracts (`targets/*.json`) use version 2 format with per-app parsing
schema definitions:

```json
{
  "version": 2,
  "project": "example-team-apex",
  "role": "integration",
  "environment": "test",
  "apps": {
    "employee-self-service": {
      "id": 101,
      "parsing_schema": "EXAMPLE_APP"
    }
  }
}
```

Version 1 contracts with a global `binding.parsing_schema` or flat `app_ids` mapping
are rejected. To convert from version 1, update `"version": 2` and convert `app_ids`
to `"apps": {"<alias>": {"id": <id>, "parsing_schema": "<SCHEMA>"}}`.

## Application identity and APEX 26.1+ verification

Before any application write (`import_app`, `deploy_app`) or daily export, the
toolchain observes live application identity from the APEX data dictionary:

- **APEX version**: Must be 26.1 or higher (from `APEX_RELEASE.VERSION_NO`).
  Versions below 26.1, unknown versions, or malformed markers refuse the operation.
- **Application presence**: Observed via `APEX_APPLICATIONS` (matching
  `APPLICATION_ID`, `WORKSPACE_ID`, and parsing schema `OWNER`).
- **Workspace-schema assignment**: Observed via `APEX_WORKSPACE_SCHEMAS`.
  The target parsing schema must be explicitly assigned to the target workspace.
- **First deploy vs. daily import**: `deploy_app` accepts an `ABSENT` app only
  if the target workspace and its parsing-schema assignment are positively verified.
  `import_app` and `export_app` strictly require `PRESENT`.
