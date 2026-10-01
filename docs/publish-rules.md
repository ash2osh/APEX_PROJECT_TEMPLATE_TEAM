# Publish rules: why publish refuses and what to do

`scripts/team.sh publish <id> --env dev` imports your committed APEXlang
files into the shared DEV app. Because the whole team edits that one app,
publish first proves that nobody changed the app since you last took a copy
of it. If it cannot prove that, it refuses rather than silently overwriting a
teammate's work.

## How the check works

Every export and every successful publish records a **baseline** in the local,
Git-ignored file `apps/<schema>/<id>/apex-team-export.json`. The baseline is
what the live app looked like at that moment:

- **Builder timestamp** (`last_updated_on`). APEX sets it whenever someone
  saves the app in Builder, and clears it (NULL) whenever someone imports the
  app.
- **Version text**, which ends with a publish tag such as
  `[ASHARIF-2026-09-26r001]`. Each DEV publish stamps a new tag into
  `application.apx` before importing, so the version shows which import is
  live.

Before importing, publish reads the same two values from the live app and
compares them with your baseline. The import session then reads them once more,
immediately before `apex import`, and refuses if they changed after the drift
check approved them. That shrinks the window for a save to slip in to the
import itself; it still cannot see Builder edits that were never saved, so tell
the team before you publish.

## The usual fix

Almost every drift refusal is fixed the same way:

```bash
scripts/team.sh export <id>          # take the current live app
git diff -- apps/<schema>/<id>/      # see what the teammate changed
# merge your changes with theirs, then:
git add apps/<schema>/<id>/ && git commit -m "Merge DEV changes into app <id>"
scripts/team.sh publish <id> --env dev
```

Export refuses to overwrite uncommitted files, so commit (or stash) your own
edits first and merge after the export. Tell the teammate before you publish
over their work.

## Drift guard rules

The guard runs before import and changes nothing when it refuses.

| Your baseline | Live app now | Result | Message |
| --- | --- | --- | --- |
| Missing, or from an export made before the version was recorded | – | Refused | `Database export baseline is unavailable` |
| – | SQLcl cannot read it | Refused | `Could not read live APEX App` |
| App absent | App absent | OK | `remains absent since the local export` |
| App absent | App exists | Refused | `was created after the local export` |
| App exists | App absent | Refused | `no longer exists in the target after the local export` |
| No timestamp, version X | No timestamp, version X | OK | `has no Builder edits since its last import` |
| No timestamp, version X | No timestamp, version Y | Refused: someone imported | `was re-imported since the local export` |
| Timestamp | No timestamp | Refused: someone imported | `was re-imported after the local export` |
| No timestamp | Timestamp | Refused: someone saved in Builder | `was modified in Builder on` |
| Timestamp T | Later than T | Refused: someone saved in Builder | `was modified in Builder on` |
| Timestamp T | Equal to T, in the current second | Refused: ambiguous | `matches the current database second` |
| Timestamp T | T or earlier, different version | Refused | `changed version since the local export` |
| Timestamp T | T or earlier, same version | OK | `No uncaptured Builder edits detected` |

What to do:

| Message | Action |
| --- | --- |
| `Database export baseline is unavailable` | Run `scripts/team.sh export <id>` once, then publish. |
| `Could not read live APEX App` | Fix the SQLcl connection (`scripts/team.sh doctor`) and retry. |
| `was created after the local export` | Someone created the app after your export. Export, merge, publish. |
| `no longer exists in the target after the local export` | Someone deleted the app. Ask the team before recreating it. |
| `was re-imported since the local export` / `was re-imported after the local export` | A teammate published. The message shows both publish tags. Export, merge, publish. |
| `was modified in Builder on` | Someone saved in Builder. Export, merge, publish. |
| `matches the current database second` | Wait one second and retry. |
| `changed version since the local export` | The version text changed. Export, merge, publish. |

`--force` skips only this guard. Use it only after you have reviewed the live
app and agreed with the team that your files should replace it.

## Other refusals

**Before connecting.** Nothing is changed.

| Message | Meaning and action |
| --- | --- |
| `publish targets DEV only` | Use `scripts/team.sh deploy <id> --env staging\|prod`. |
| `resembles production but DB_ENVIRONMENT` | The connection name looks like production. Check `.env`, and ask before continuing. |
| `deployment descriptor not found` | Add `apps/<schema>/<id>/deployments/dev.json`; copy it from `apps/templates/deployments/`. |
| `is stored under apps/` | The app folder and descriptor name different parsing schemas. Move the folder under `apps/<PARSING_SCHEMA>/` or correct the descriptor. |
| `does not match the application's parsing schema` | The selected `--schema` differs from the descriptor. Drop or correct `--schema`. |
| `is not listed in APEX_PARSING_SCHEMA` | The app's parsing schema has no DEV APEX profile entry. Add the schema, connection, and expected user to the aligned APEX profile lists in `.env`. |
| `is not listed in STAGING_SCHEMA` / `is not listed in PROD_SCHEMA` | The app's parsing schema has no entry for that target. Add its schema, connection, and expected user to the aligned staging or production lists in `.env`. |
| `application source is outside the repository` / `symbolic links or reparse points are not supported` | Keep the app source as real files inside the checkout. |
| `the application path contains characters SQLcl cannot pass` | The repository path contains `'`, `"` or `&`, which SQLcl would cut short or substitute. Move the checkout to a path without them. |
| `DEV publish needs application.apx to stamp the publish tag` | The app source is incomplete; export it again. |
| `could not stamp the application version` | Fix the `version:` line in `application.apx` as the message says (for example, a duplicate line or a version over 255 bytes). |
| `application.apx changed while the publish tag was stamped` | You saved `application.apx` while publish was stamping it. Your save is kept; publish again. |
| `could not move application.apx to stamp the publish tag` | The file or its folder cannot be renamed: on Windows an editor or another program usually holds it open; elsewhere check the folder's permissions. Close it or fix the permissions and publish again. |
| `could not install the stamped application.apx` | The stamped copy could not be moved into place; your file was put back and nothing was imported. Publish again. |

**After connecting, before import.** Nothing is changed when this check refuses.

| Message | Meaning and action |
| --- | --- |
| `is parsed by` | The live application uses a different parsing schema than the descriptor. Review the live app and correct the descriptor and folder, or arrange the intended schema change with the team before importing. |
| `Live application changed after the Builder drift check` (ORA-20016) | Someone saved or imported the app between the drift check and the import. Nothing was imported. Export, merge, publish. |

**During import.** Publish restores your unstamped `application.apx`; read the
SQLcl output printed above the message. If the file changed while publish ran
(you saved it in an editor), publish leaves your edit in place and warns
`changed while publishing; left as is`; check its `version:` line. If the
file could not be swapped back (for example an editor holds it open), publish
warns `could not remove the publish tag`; remove the tag from its `version:`
line by hand. Publish
swaps `application.apx` by moving it into `scratch/apex-publish.*` first; if a
publish is killed at that moment the file is missing from the app folder, and
its previous bytes are in that scratch folder as `application.apx.before-stamp`
or `application.apx.displaced`.

| Message | Meaning and action |
| --- | --- |
| `SQLcl application import failed` | SQLcl exited with an error. Fix the reported problem and retry. |
| `SQLcl reported a client or database error during the application import` | An `ORA-`, `SP2-`, `PLS-`, or `TNS-` error was printed. Fix it and retry. |
| `SQLcl did not report a successful APEX import` | SQLcl skipped the import, for example because `dev.json` names a workspace that does not exist. Fix the descriptor. |
| `SQLcl did not verify the imported application` | The import result is unknown. Export and compare before retrying. |

**After import.** The import has already changed DEV, but publish could not
prove the result, so it keeps your old baseline and the next publish refuses.
Your stamped `application.apx` is kept, because that is what was imported.
Commit it first (export refuses to write over uncommitted changes), then run
`scripts/team.sh export <id>` to see exactly what is live, reconcile, and
commit. Publish prints these steps when it stops here.

| Message | Meaning |
| --- | --- |
| `post-import APEX export failed` | The verification export failed. |
| `APEXlang source file set does not match the post-import re-export` | APEX wrote a different set of files than you committed. |
| `APEXlang source bytes do not match the post-import re-export` | APEX normalized something you wrote by hand. After exporting, commit APEX's form. |
| `is not visible in the post-import state` | The app is missing after import. |
| `changed while its post-import source was being verified` | Someone edited or imported during the verification. |
| `same database second` / `later than the database-time observation` | The revision is ambiguous; export and retry. |

## Example

Two developers, ASHARIF and BOB, both export app 9000. ASHARIF publishes
twice (`r001`, `r002`). BOB then publishes from the older export and is
refused with `was re-imported after the local export`, because the app's
Builder timestamp was cleared by ASHARIF's import. BOB exports, merges, and
publishes (`r003`). When ASHARIF publishes again, the guard reports
`was re-imported since the local export`, showing live `[BOB-2026-09-26r003]`
against their baseline `[ASHARIF-2026-09-26r002]`. ASHARIF exports, merges,
and publishes `r004`.
