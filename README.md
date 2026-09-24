# APEX project template for teams

This repository treats each APEX application as a shared physical Builder resource. The tracked source is `apps/<alias>/`; the database and Builder workspace are identified by validated target profiles, not by a Git branch.

Every developer keeps a **separate Git repository** created from this template. There is no shared remote: the repositories never pull from each other. What the team shares is the development database, and that is where everyone's work meets.

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

There is no import in the normal builder-first loop, and no pull or push: the commit stays in your own repository. The export reflects the team's observed application state, including colleagues' saved Builder changes, which is how their work reaches your repository.

## 4. File-first publish workflow

When authoring APEXlang files directly or working with an agent, changes must be committed first. Publishing into the shared Builder workspace is app-scoped and requires an exact preparation and explicit teammate acknowledgements:

```bash
# 1. Edit apps/hr/, review it, and commit it.
# 2. Prepare publish for the reviewed commit:
scripts/team.sh prepare-publish hr --ref HEAD

# 3. Post the printed HR pause notice to teammates and gather acknowledgements.
# 4. Enter the printed preparation ID and Omar's registered checkout UUID:
read -r -p "Preparation ID: " PREPARATION_ID
read -r -p "Omar's checkout UUID: " OMAR_UUID
scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause --ack "hr:$OMAR_UUID"
```

> [!IMPORTANT]
> **Page-lock reports are informational:** Page-lock queries report active locks and comments. However, absence of locks does not prove the absence of unsaved or in-progress Builder edits by teammates. Always communicate before publishing. If a pre-publish before-check detects changes, publish halts so you can export and reconcile.

### App-scoped pause examples

- **HR-only pause:** When publishing `hr`, only HR is paused; teammates working on `payroll` continue uninterrupted:
  ```bash
  scripts/team.sh prepare-publish hr --ref HEAD
  # After posting the notice and gathering acknowledgements:
  read -r -p "Preparation ID: " PREPARATION_ID
  read -r -p "Omar's checkout UUID: " OMAR_UUID
  scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause --ack "hr:$OMAR_UUID"
  ```
- **Multiple apps pause:** When publishing both `hr` and `payroll`:
  ```bash
  scripts/team.sh prepare-publish hr payroll --ref HEAD
  # After posting both notices and gathering acknowledgements:
  read -r -p "Preparation ID: " PREPARATION_ID
  read -r -p "Omar's checkout UUID: " OMAR_UUID
  read -r -p "Carol's checkout UUID: " CAROL_UUID
  scripts/team.sh publish-app --prepared "$PREPARATION_ID" --confirm-pause \
    --ack "hr:$OMAR_UUID" --ack "payroll:$CAROL_UUID"
  ```

Add one `--ack` for every registered checkout shown in the selected apps' pause notice.

## 5. Shared migrations and independent releases

Releases are still built from a Git commit today. Until they are built from the shared development database ([design](docs/superpowers/specs/2026-09-24-dev-database-release-source-design.md)), build every release from one designated repository that holds all migration files; see [docs/promotion.md](docs/promotion.md).

- **Shared schema release:** Migrations under `migrations/` apply once per environment to the shared TABLES/CODE schema:
  ```bash
  scripts/team.py build-release --kind schema --ref schema/v1.0.0 --version 1.0.0 --out scratch/release
  scripts/team.py verify-release scratch/release/release.tar
  ```
- **Independent application release:** Releasing an application packages only that application and verifies its required migration prerequisites against destination history without deploying sibling apps:
  ```bash
  scripts/team.py build-release --kind app --alias hr --ref app/hr/v1.0.0 --version 1.0.0 --out scratch/release
  scripts/team.py verify-release scratch/release/release.tar
  ```
- **Destructive migration confirmation:** Destructive migrations require an explicit `--destructive-confirmation` document reviewed by the migration owner; see [docs/migrations.md](docs/migrations.md).
- **Persistent qualification:** Protected integration and test runs verify persistent targets (`target_kind: persistent`). Persistent staging does not prove fresh installation or isolation. Use `qualify-target` for read-only target diagnostics; see [docs/ci.md](docs/ci.md).

## 6. Asking an AI coding agent

Coding agents follow the exact same public CLI commands and rules as human developers:
1. When asked to author APEXlang, an agent edits `apps/<alias>/`, reviews the diff, and commits.
2. The agent runs `scripts/team.sh prepare-publish <alias> --ref HEAD`.
3. The agent must pause and present the pause notice to the human developer. The agent cannot invent teammate acknowledgements or bypass the publish gate.
4. Once genuine teammate checkout acknowledgements are provided by the user, the agent runs `scripts/team.sh publish-app`.

## 7. Runbooks and advanced references

- [Import pause & lock details](docs/import-pause.md)
- [Application recovery & retained evidence](docs/app-recovery.md)
- [Database migrations & confirmation](docs/migrations.md)
- [Promotion, CI & production runbooks](docs/promotion.md)
- [CI workflows & protected test runs](docs/ci.md)
- [Toolchain qualification & APEX 26.1+](docs/toolchain.md)
- [Conflict resolution](docs/conflict-resolution.md)
- [Design review resolution](docs/design-review-resolution.md)
- [Local three-developer acceptance run](docs/local-three-developer-e2e.md)
