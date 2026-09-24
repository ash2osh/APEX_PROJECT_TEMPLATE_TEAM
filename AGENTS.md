# Team APEX agent contract

This repository is a shared-application workflow supporting APEX 26.1+ and APEXlang only. The logical application alias is stable across developers; `apps/<alias>/` is the tracked APEXlang source. The development workspace and application ID are shared by the team, so a Git branch does not isolate Builder state. Each developer has a separate Git repository with no shared remote; the repositories meet only in the shared development database. Never pull, push or merge between developer repositories, and never assume a colleague's commits are present locally.

## Three Authoring Routes

1. **Builder-first route:** Make edits in Builder, run `scripts/team.sh export-app <alias>`, review changes, and commit. There is no import in the normal Builder-first loop; export reflects the team's observed state.
2. **File-first / Agent APEXlang route:** When editing APEXlang directly or using an AI coding agent:
   - Edit `apps/<alias>/`, review, and commit exact source.
   - Run `scripts/team.sh prepare-publish <alias> --ref HEAD` to generate an app-scoped pause notice and durable evidence.
   - Gather genuine teammate checkout acknowledgements (never invent fictitious acknowledgements or bypass gates).
   - Run `scripts/team.sh publish-app --prepared <id> --confirm-pause --ack <alias>:<uuid>`.
3. **Schema migration route:** Author immutable pairs under `migrations/`; verify drift and apply only through the qualified METADATA profile. Applied migrations change the shared TABLES/CODE schemas for everyone; a colleague's applied migration appears in the shared history even though its files are not in this repository.

## Guards, Locks & Boundaries

- **App-scoped pause:** Publishing pauses only the selected applications. Teammates working on sibling apps continue editing uninterrupted.
- **Informational page-lock report:** Page-lock reports query `APEX_APPLICATION_LOCKED_PAGES`. The absence of locks does not prove the absence of unsaved or in-progress Builder work. Always communicate before publish.
- **Before/after check:** Preflight captures live Builder state before writes and halts on unreconciled edits; post-import verifies that re-exported APEXlang matches exact source bytes.
- **Independent releases:** Release tags are `schema/v<semver>` (applied once to shared schema) and `app/<alias>/v<semver>` (deploys only the selected app after verifying schema prerequisites; zero sibling app deployments).
- **Production refusal:** Production writes remain strictly refused. CI test runs emit signed evidence and offline owner runbooks; live actions are never automated in production.
- **Durable recovery:** Recovery captures and journals belong under `.sync-state/` and survive process failure. Never clear locks or repair refusals by importing over the workspace.
- **Evidence truth:** Never claim unavailable live checks passed. If an environment or flow check is unavailable, record `UNKNOWN`. Never silently commit or push to Git.
