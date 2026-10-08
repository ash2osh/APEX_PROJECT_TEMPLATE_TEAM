# Canonical APEXlang source upgrade

Use this separate DEV operation when moving reviewed source to the installed
APEX 26.2 format. It requires a clean local app tree and SQLcl 26.3 or newer.
Review and commit or stash local edits before starting; a Git branch does not
isolate Builder or the shared database.

```bash
scripts/team.sh upgrade-apexlang 100 --env dev --mode builder
scripts/team.sh upgrade-apexlang 100 --env dev --mode files
```

PowerShell uses the same arguments with `scripts/team.ps1`.

`builder` exports the existing live app twice without importing. Choose it
when Builder holds the authoritative work. `files` coordinates a shared DEV
import: it validates the explicit workspace developer, acquires the native app
lock, checks the existing whole-app drift baseline, preserves a live backup,
and imports a stamped private copy of the reviewed files. It then exports twice
and checks the selected descriptor's effective values. Tell the team before
using files mode and check for unsaved Builder edits.

Authentic 26.1 files conversion accepts an identity-only DEV descriptor
(workspace, numeric app ID, parsing schema). SQLcl did not preserve a selected
26.2 checksum-salt override when importing 26.1 source during Docker
qualification. Optional overrides therefore refuse before any write on that
path. Use Builder conversion first to preserve an authored descriptor containing
26.2 overrides, then apply those overrides to canonical 26.2 source.

Both modes require stable native source and revision observations. Canonical
source replaces the complete app tree through the existing atomic mirror
replacement, removing obsolete component files while preserving authored
`dev.json`, `staging.json` and `prod.json`. Files mode also checks imported
static and generated-artifact bytes. Canonical 26.2 input additionally requires
exact post-import source equality, allowing only qualified descriptor effects.
Authentic 26.1 conversion changes the layout, so review its structural diff;
stable exports do not prove that every old-format source property was preserved. No in-place source export with `-force` is used.

The new baseline records `schemaVersion: 2`, the measured `mmdVersion`, and raw
SHA-256 values for source, binary assets and preserved deployment descriptors.
Historical baselines remain usable for full publishing; they cannot qualify
partial publishing. A conversion report and the original source/live backups
remain under ignored `scratch/apexlang-upgrade.*` for review and recovery.

An ambiguous import keeps the original local source and baseline, native lock,
and recovery evidence. Reconcile live state before an owner-filtered unlock.
Failure to install verified canonical source leaves the staged source for review.
The command never commits, promotes, upgrades a theme, migrates workflows, or
opts into executing supporting objects. Review the structural diff separately
from feature changes. Both wrappers passed Builder and files conversion on a
seeded French-language fixture, preserving generated translation SQL and PNG
bytes. Exports include SQLcl `-exptranslations`; generated translation artifacts
are native export/import inputs and must not be edited manually. See
[the qualification results](apex-26.2-qualification.md).
