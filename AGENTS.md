# Team APEX agent contract

This repository is a shared-application workflow supporting APEX 26.1+ and APEXlang only. The logical application alias is stable across developers; `apps/<alias>/` is the tracked APEXlang source. The development workspace and application ID are shared by the team, so a Git branch does not isolate Builder state. Each developer has a separate Git repository with no shared remote; the repositories meet only in the shared development database. Never pull, push or merge between developer repositories, and never assume a colleague's commits are present locally.

## Three Authoring Routes

1. **Builder-first route:** Make edits in Builder, run `scripts/team.sh export-app <alias>`, review changes, and commit. There is no import in the normal Builder-first loop; export reflects the team's observed state.
2. **File-first / Agent APEXlang route:** When editing APEXlang directly or using an AI coding agent:
   - Edit `apps/<alias>/`, review, and commit exact source.
   - Run `scripts/team.sh prepare-publish <alias> --ref HEAD` to generate an app-scoped pause notice and durable evidence.
   - Keep a stable `TEAM_CHECKOUT_UUID` for each repository and use it when registering that checkout.
   - After the pause notice is posted, each teammate runs `scripts/team.sh ack-publish <id>` from their own registered checkout; it records the preparation digest, checkout UUID, host, user, and time in shared control metadata.
   - Run `scripts/team.sh publish-app --prepared <id> --confirm-pause` only after all required acknowledgement rows are present. Never invent a teammate acknowledgement or set another checkout's identity.
3. **Schema migration route:** Author immutable pairs under `migrations/`; verify drift and apply only through the qualified METADATA profile. Applied migrations change the shared TABLES/CODE schemas for everyone; a colleague's applied migration appears in the shared history even though its files are not in this repository.

## Guards, Locks & Boundaries

- **App-scoped pause:** Publishing pauses only the selected applications. Teammates working on sibling apps continue editing uninterrupted.
- **Checkout-authored acknowledgement:** `setup-state` creates additive preparation and acknowledgement tables without altering existing metadata tables. `ack-publish` resolves the preparation from shared metadata and writes only for this checkout's registered identity; `publish-app` checks shared preparation and acknowledgement rows bound to the exact preparation digest.
- **Informational page-lock report:** Page-lock reports query `APEX_APPLICATION_LOCKED_PAGES`. The absence of locks does not prove the absence of unsaved or in-progress Builder work. Always communicate before publish.
- **Before/after check:** Preflight captures live Builder state before writes and halts on unreconciled edits; post-import verifies that re-exported APEXlang matches exact source bytes.
- **Independent releases:** `build-release --kind schema|app` cuts a release from the shared development database. Schema releases apply once per environment; app releases deploy only the selected application after verifying its schema prerequisites. Release tags do not identify or build releases.
- **Production refusal:** Production writes remain strictly refused. Local test-profile qualification emits signed evidence and offline owner runbooks; live production actions are never automated.
- **Durable recovery:** Recovery captures and journals belong under `.sync-state/` and survive process failure. Never clear locks or repair refusals by importing over the workspace.
- **Evidence truth:** Never claim unavailable live checks passed. If an environment or flow check is unavailable, record `UNKNOWN`. Never silently commit or push to Git.
