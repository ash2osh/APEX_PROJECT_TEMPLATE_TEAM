# Team APEX agent contract

This repository is a shared-application workflow. The logical application
alias is stable across developers; `apps/<alias>/` is the tracked APEXlang and
binary source. The development workspace and application ID are shared by the
team, so a Git branch does not isolate Builder state.

- Route APEX edits to `apps/<alias>/`; route schema intent to an immutable pair
  under `migrations/`; route canonical database evidence to replay output.
- Check `app_context/<alias>/` before complex app work. Keep deployment bindings
  credential-free and keep local defaults out of Git.
- The daily Builder loop is build, `export-app`, review, and commit. There is
  no import step in the normal edit loop. An export is the team's observed
  application state, not “my changes”.
- `import-app` overwrites the shared application. Never use it to refresh,
  reset, or discard Builder work. It requires a verified baseline/receipt,
  team pause, exact source, and verified re-export.
- Before database writes, inspect drift and use the qualified SQLcl adapter.
  Production writes, metadata bootstrap, adoption, recovery, and setup are
  refused. No command commits or pushes automatically.
- Recovery captures and journals belong under `.sync-state/` and survive
  process failure. Do not repair a refusal by importing over the workspace.
- A migration applied to the shared schema must be merged promptly: a
  colleague's export can carry a page that depends on an unmerged bundle.

Uncaptured/transient Builder edits and arbitrary DML outside the supported
inventory remain outside what the tooling can observe. Say that boundary when
reporting results.
