# Optional workflow skills

The template ships its 11 APEX, database, browser and initialization skills.
General engineering workflow packs are optional. A new project starts with
`none`; the Oracle skills refresh and all project safety rules still apply.

## Choose a pack

Run from your project, with Git and Python 3.10+ available. These commands
do not load `.env`, connect to a database, or require Node.js:

```bash
scripts/team.sh skills status
scripts/team.sh skills setup none
scripts/team.sh skills setup superpowers
scripts/team.sh skills setup mattpocock
scripts/team.sh skills setup both --primary superpowers
scripts/team.sh skills setup both --primary mattpocock
scripts/team.sh skills status --format json
```

PowerShell exposes the same arguments through `scripts/team.ps1 skills`.
Each setup command replaces the previous repository selection. `none` removes
only this installer's unchanged, recorded skill directories; it makes no
network request. Single-pack selections use that pack as primary. `both`
defaults to Superpowers as primary unless explicitly overridden.

| Selection | Installed workflow guidance |
| --- | --- |
| `none` | No repository-managed workflow pack |
| `superpowers` | [Superpowers](https://github.com/obra/superpowers) workflow skills |
| `mattpocock` | [Matt Pocock's skills](https://github.com/mattpocock/skills), engineering and productivity groups |
| `both` | Both packs, with one primary workflow |

The primary pack supplies the default design, implementation, testing and
review process. Use the other pack for an explicitly requested skill or task
override; do not automatically run two complete processes for the same task.
Project rules and direct user instructions take precedence over either pack.
Upstream names stay intact so skill references and relative helper paths work.

**Scope limit:** this selection controls only files managed in this repository.
User-level installations and enabled IDE/app plugins can still expose the same
skills. `status` does not inspect or disable them. To use neither everywhere,
disable those separate installations too and restart the agent session. For
local Codex skills, see [OpenAI's disable instructions](https://learn.chatgpt.com/docs/build-skills#enable-or-disable-local-codex-skills).

## What setup writes

- `.agent-skills.json`: project-owned selection and primary pack; review and
  version it in your downstream repository. Template upgrades preserve it.
- `.agents/skills/<skill>/` and `.claude/skills/<skill>/`: ignored local copies
  for the two agent discovery conventions. Each includes the upstream license.
- `.agent-skills.lock.json`: ignored source repositories, exact commits and
  SHA-256 content inventories for both copies. Do not hand-edit this file.
- `.agent-skills/`: ignored temporary downloads and a local setup lock.

Setup fetches the exact commits listed in
[`tools/workflow-skills/catalog.json`](../tools/workflow-skills/catalog.json).
It never executes downloaded scripts or changes global agent configuration.
`status` reads local selection and file hashes without fetching anything.
Running setup again explicitly installs the catalog's pinned versions; a
template upgrade alone never installs or updates a pack. Version bumps require
reviewing the catalog and testing both packs before changing their commits.
Matt's experimental `in-progress` and miscellaneous tooling groups are excluded.

Installation prepares both packs before changing either discovery directory.
An unavailable commit, filename collision, tracked destination, locally edited
file, added file, symlink or reparse point refuses setup. Existing custom and
Oracle skills are preserved. Ordinary write failures restore the old managed
directories and metadata. There is no overwrite/force option.

## Matt Pocock's per-project setup

After installing a selection containing Matt's skills, start a fresh agent
session if necessary and invoke `/setup-matt-pocock-skills`. It configures the
issue tracker, labels and domain documentation. Installation itself does not
perform this interactive configuration or create remote issues/labels.

Save its project-specific guidance in `AGENTS.project.md`, which this template
loads and preserves, instead of editing template-owned `AGENTS.md` or
`CLAUDE.md`. Preserve existing project instructions. Its `docs/agents/` files,
glossary and ADRs belong to the downstream project. Any external issue, label,
message, commit, push or database operation still follows project authorization
rules, regardless of what an upstream skill suggests.

## Existing projects and recovery

Upgrade the template before enabling these packs. The normal upgrade removes
unchanged historical bundled Superpowers files and preserves customized removed
files with `KEEP-REMOVED`. Review and move any custom workflow skills outside
the optional packs' reserved directories, then remove the retired tracked
copies from your project Git index before running setup. Setup refuses to
overwrite any tracked skill, even if its contents match upstream.

Upgrade preserves the selection, local lock and installed optional packs.
It does not migrate the old bundled workflow's project-specific adaptations
into upstream Superpowers; review upstream behavior against your project rules.
Inspect `skills status` after upgrading, then explicitly run setup if you want
the newly pinned versions.

If setup refuses a local edit, copy it to a project-owned custom skill or
otherwise reconcile it first. Restore the recorded contents/file set before
retrying; do not discard edits just to satisfy the hash check. When an install
or removal was interrupted, inspect `.agent-skills/setup-*/backup/` and both
agent directories against `.agent-skills.lock.json`. Preserve recovery files,
restore the last complete directory/metadata set, and remove
`.agent-skills/setup-lock` only after confirming no setup process is running.
A hard kill or power loss can interrupt a multi-directory change; it is not
claimed as a completed install and the retained setup lock flags recovery.
