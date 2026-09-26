# APEX project template for teams

This is the canonical template repository and its changes use the normal upstream branch and pull-request workflow. It treats each APEX application as a shared physical Builder resource. The tracked source is `apps/<alias>/`; the database and Builder workspace are identified by validated target profiles, not by a Git branch.

Teams created from this template use a **separate Git repository per developer**. Those downstream repositories do not share a Git remote; what the team shares is the development database, and that is where everyone's work meets. The canonical template repository follows its upstream branch and pull-request process.

## 1. What is shared

- **Database and workspace:** The team shares a single development database instance and APEX workspace per environment.
- **Not Git:** each developer's repository is their own. Colleagues' Builder changes reach your repository through `export-app`, and their migrations are recorded in the shared migration history, not in your `migrations/` folder.
- **Application source:** APEX edits are tracked as APEXlang source under `apps/<alias>/`.
- **Database schema stream:** Shared database objects live under `TABLES_SCHEMA` and `CODE_SCHEMA` and are evolved via migrations under `migrations/`.
- **Parsing schemas:** Each application is bound to exactly one parsing schema in the format `alias:id:PARSING_SCHEMA`.

Applications may share one common parsing schema:
```text
APEX_APPS=hr:100:APP,payroll:200:APP
```
Or applications may use separate parsing schemas:
```text
APEX_APPS=hr:100:HR_CODE,payroll:200:FIN_CODE
```

## 2. Local setup

Copy `.env.example` to the ignored `.env` file, configure your saved SQLcl connection names (credentials remain securely inside SQLcl's wallet/store), and check your environment with `doctor`:

```bash
cp .env.example .env
scripts/team.sh doctor
```

## 3. Builder-first workflow

When working directly in the shared APEX Builder, save your changes, export the application to track the observed state, review the diff, and commit:

```bash
# Builder-first: capture the shared app after saving in Builder.
scripts/team.sh export-app hr
git status --short --untracked-files=all -- apps/hr/
git add -- apps/hr/
git diff --cached -- apps/hr/
git commit -m "Capture reviewed HR Builder changes"
```

There is no import in the normal builder-first loop. In a downstream developer repository, the commit stays in that developer's repository; colleagues' saved Builder changes reach it through `export-app`. Changes to this canonical template repository follow its upstream branch and pull-request workflow.

## 4. File-first publish workflow

When authoring APEXlang files directly or working with an agent, changes must be committed first. Publishing into the shared Builder workspace is app-scoped. Keep a stable `TEAM_CHECKOUT_UUID` for each repository and use it when registering that checkout; if registration already returned an ID, export that value in this checkout before acknowledging.

```bash
# 1. Edit apps/hr/, review it, and commit it.
# 2. Prepare publish for the reviewed commit:
scripts/team.sh prepare-publish hr --ref HEAD

# 3. Post the printed HR pause notice. Each registered checkout runs the
#    acknowledgement command from its own repository, including the publisher.
read -r -p "Preparation ID: " PREPARATION_ID
scripts/team.sh ack-publish "$PREPARATION_ID"
# 4. After all acknowledgements are recorded, the publisher runs:
scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause
```

> [!IMPORTANT]
> **Page-lock reports are informational:** Page-lock queries report active locks and comments. However, absence of locks does not prove the absence of unsaved or in-progress Builder edits by teammates. Always communicate before publishing. If a pre-publish before-check detects changes, publish halts so you can export and reconcile.

### App-scoped pause examples

- **HR-only pause:** When publishing `hr`, only HR is paused; teammates working on `payroll` continue uninterrupted:
  ```bash
  scripts/team.sh prepare-publish hr --ref HEAD
  # From each registered HR checkout, after the notice is posted:
  read -r -p "Preparation ID: " PREPARATION_ID
  scripts/team.sh ack-publish "$PREPARATION_ID"
  # The publisher runs after every registered HR checkout has acknowledged:
  scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause
  ```
- **Multiple apps pause:** When publishing both `hr` and `payroll`:
  ```bash
  scripts/team.sh prepare-publish hr payroll --ref HEAD
  # From each registered checkout for either selected app:
  read -r -p "Preparation ID: " PREPARATION_ID
  scripts/team.sh ack-publish "$PREPARATION_ID"
  # The publisher runs after every registered checkout has acknowledged:
  scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause
  ```

`ack-publish` records the preparation digest, this checkout's registered UUID, current host and user, and acknowledgement time in shared control metadata. It acknowledges only selected applications where that checkout is registered. The pause notice shows checkout counts, not UUIDs. The command checks host and user against registration, but the UUID is still an environment-provided self-attestation rather than a cryptographic identity.

`prepare-publish` also registers its immutable digest, selected aliases, target keys, and checkout rosters in shared control metadata. This lets teammates acknowledge from separate repositories without copying the publisher's local `.sync-state` files; `publish-app` checks that shared registration against the publisher's durable preparation record.

## 5. Shared migrations and independent releases

Build each release from the shared development database at a qualified ledger cut. No repository needs to contain every developer's migration files; the immutable migration bundles are stored in METADATA. See [docs/promotion.md](docs/promotion.md) for the local release runbook.

- **Shared schema release:**
  ```bash
  scripts/team.sh --env .env build-release --kind schema --version 1.0.0 --out scratch/release
  scripts/team.sh verify-release scratch/release/release.tar
  ```
- **Independent application release:** capture only the selected app, its required-migration set, checks and master contracts:
  ```bash
  scripts/team.sh --env .env build-release --kind app --alias hr --version 2.0.0 --out scratch/release
  scripts/team.sh verify-release scratch/release/release.tar
  ```
- **Qualification and handoff:** run the archive on the protected test profile, sign passing evidence with the local test signing key, then generate the owner-reviewed production runbook. Production writes remain refused.
- **Destructive migration confirmation:** destructive migrations require an explicit `--destructive-confirmation` document reviewed by the migration owner; see [docs/migrations.md](docs/migrations.md).
- **Persistent qualification:** protected integration and test runs verify persistent targets (`target_kind: persistent`). Persistent staging does not prove fresh installation or isolation. Use `qualify-target` for read-only target diagnostics; see [docs/ci.md](docs/ci.md).

## 6. Asking an AI coding agent

Coding agents follow the exact same public CLI commands and rules as human developers:
1. When asked to author APEXlang, an agent edits `apps/<alias>/`, reviews the diff, and commits.
2. The agent runs `scripts/team.sh prepare-publish <alias> --ref HEAD`.
3. The agent must pause and present the pause notice to the human developer. Each teammate runs `ack-publish` from their own registered checkout; the agent cannot acknowledge for them or bypass the publish gate.
4. After every required acknowledgement is in shared control metadata, the agent runs `scripts/team.sh publish-app --prepared <id> --confirm-pause`.

## 7. Runbooks and advanced references

- [Import pause & lock details](docs/import-pause.md)
- [Application recovery & retained evidence](docs/app-recovery.md)
- [Database migrations & confirmation](docs/migrations.md)
- [Promotion, CI & production runbooks](docs/promotion.md)
- [CI workflows & protected test runs](docs/ci.md)
- [Toolchain qualification & APEX 26.1+](docs/toolchain.md)
- [Conflict resolution](docs/conflict-resolution.md)
- [Local three-developer acceptance run](docs/local-three-developer-e2e.md)
- [Live database test plan](docs/live-test-plan.md)
- [Pending work](docs/pending-work.md)
