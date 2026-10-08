# APEX 26.2 qualification

The template targets APEX 26.2 with SQLcl 26.3.0.0 or newer. Docker DEMO was
measured on 2026-10-08 using APEX 26.2.0, Oracle Database 23.26.3.0.0 and SQLcl
26.3.0.0 (build 26.3.0.260.1620). The existing SQLcl MCP process retained its
older 26.2.2.0 client; the qualifications used fresh CLI processes.

The measurements used disposable applications and an explicitly authorized
temporary developer. Those original fixtures and the developer were removed
through public APIs; the nine original application metadata records matched
the initial snapshot. That check did not compare original application bytes.

## Measured boundaries

| Boundary | Result | Template requirement |
| --- | --- | --- |
| Application locks | Acquisition/release need commit. Different-owner acquisition and wrong-owner release fail. Owner reentry succeeds and replaces the comment. | Acquire before the last drift check; refuse preexisting locks even for the same account. Check owner and run comment before release. This is not a process mutex. |
| Import enforcement | Raw full and page-only SQLcl imports succeed while another developer owns the application lock. Both preserve lock metadata. | Every template importer must cooperate with the native lock. Writers using raw SQLcl can bypass this workflow. |
| Selected deployment | Explicit `-deployment` applies its name, runtime flags and checksum salt. A conflicting `default.json` does not overlay it. | Verify source bytes and effective deployment values separately. Salt values never belong in logs. |
| Omitted salt | An identity-only descriptor did not prove preservation of a preceding selected salt in the next generated deployment export. | Specify and verify a salt when preservation is required. Do not infer a private database value. |
| Canonical conversion | Authentic `26.1.0+3102` example source became `26.2.0+3479`. Two exports matched all 23 files and preserved five PNG assets. | Replace the canonical tree to handle renamed/deleted files; preserve authored deployment descriptors independently. |
| Selected page import | SQLcl applied the selected existing home page and ignored an unselected dirty login page. | Reject unselected local dirt and merge selected edits into a fresh live snapshot before importing. |
| Application version | Generated-26.2 starter stamping changed cutoff metadata; authentic-26.1 fixture stamping changed only application source and omitted the cutoff. | Accept only the measured file sets, verify all other bytes/values, and verify the measured effects and runtime bookmark behavior described below. |
| Page locks | The public `APEX_APPLICATION_LOCKED_PAGES` view exposes owner/comment/time. No supported installed public page-lock setter or SQLcl lock command was found. | Read page ownership; arrange locks in Builder. Use the qualified guarded no-notice route only with pre-edit Builder locks and explicitly supplied live-matching salt/cutoff settings. |

The qualified page-import command is:

```text
apex import -input "<complete-staged-app>" -deployment "<explicit-dev.json>" -files "<absolute-selected-page.apx>"
```

The full staged application must validate against the current live shared
components. A selected page alone is insufficient verification. Full imports
remain the staging/production route. Working copies are deferred.

The authentic fixtures under `tests/fixtures/apex_26_2/` preserve the original
format markers; their generated salts were replaced with zeroes for tests.
The 26.2 layout splits breadcrumbs, lists and LOV files and omits the empty
deinstall script. Those initial fixtures cover the small example. Later translated-app and
subscription tests are recorded below. Executable supporting-object installation
remains refused; it requires separate reviewed migrations.

## Reusable probe

`tools/probe_apex_26_2.py` defaults to read-only identity checks. Supply the
actual saved connection, session user, current schema, workspace, service and
local server host. Obtain those identities from the developer's target;
never copy a container host from another checkout.

```bash
python3 tools/probe_apex_26_2.py --connection <saved-alias> \
  --expected-user <DB_USER> --schema <SCHEMA> --workspace <WORKSPACE> \
  --app-id <unused-numeric-id> --server-host <local-host> --service <service>
```

After human authorization of those exact disposable targets, add
`--allow-writes --source-26-1 <authentic-source-directory> --developers <first> <second>`.
Both developers must already exist with developer/admin privileges. The probe
does not create users. It checks the unused ID again in the first import
session, gives the fixture an opaque alias, and checks workspace/schema/alias
before subsequent operations. It refuses executable supporting-object scripts.

Reports and private SQLcl diagnostics remain under ignored
`.sync-state/apex-26.2-probe/`. Reports contain pass/fail/unavailable states and
no full logs or checksum salts. A native success line is required for imports;
a visibility marker alone cannot establish success. Unknown SQLcl commands fail.
The probe preserves its fixture and report after writes, including ambiguous
failures: it never automatically deletes an application. Reconcile an attempted
import and verify the exact recorded alias before authorized public-API cleanup.

The reusable probe now covers identity, authentic conversion/stable exports,
binary preservation, lock persistence, owner conflict/reentry, raw full/page
import lock preservation, native selected-page behavior, version-source effects,
and explicit deployment materialization including salt and default-overlay
checks. The 2026-10-08 complete run passed these checks; explicit cleanup then
verified removal of its fixture and the authorized temporary developer.

For explicit fixture cleanup, repeat the exact target arguments and add
`--allow-writes --cleanup-report <previous-report.json>`. Cleanup checks the
recorded alias and any owner/run comment; it cannot remove a different app or
break another operation's lock. It does not remove developers.

Native Bash and PowerShell-on-Linux full publish and explicit lock/unlock passed
on a separate disposable app. A real SIGTERM after the committed import preserved
the previous baseline and lock; a forced retry refused and exact owner-filtered
recovery succeeded. Wrong-owner acquisition/release refused in both wrappers.
Read-only doctor verified the canonical workspace developer. The fixture and the
explicitly authorized temporary developer were removed using exact-target guards.
Windows-native signal behavior is unavailable on this Linux host. The integrated selected-page and guarded no-notice workflows subsequently passed
both wrappers, as recorded below.

The shared descriptor verifier now supports explicit identity, application name,
Boolean debugging/logging and a 64-hex checksum salt. Native public debugging
values measured here are `Allowed` and `Not Allowed`; logging uses `Yes` and `No`.
The qualified descriptor also accepts ISO-second `allowUrlsCreatedAfter` and
`subscription.masterApps` mappings. Unknown properties, including unqualified
subscription modes, fail closed. Subscription remapping and translated-app
round trips passed both wrappers.

## Builder and runtime qualification on 2026-10-08

On a disposable DEMO app, Builder refused another developer's Save with
`ORA-20999` after DEMO acquired the page lock. The other developer had opened
the page editor and changed its draft before the lock was acquired. Public
page metadata confirmed that the refused save did not change the page.

A selected-page import preserved the exact Builder lock ID, owner, comment
and timestamp. Full application imports removed the Builder page lock;
application-level lock preservation is a separate result and must not be
substituted for page-lock preservation.

Actual Chrome runtime requests verified an application-level public bookmark
against a protected page item. The valid URL rendered the intended item value;
a URL with a tampered value failed checksum protection. The original valid URL
still worked after selected-page import and public application-version update.
This passed both on the converted app that omitted the exported URL cutoff and
on an explicitly configured cutoff. The explicit cutoff remained unchanged.
No session URLs or checksums are stored in the qualification reports.

Files-mode conversion of authentic 26.1 input did not preserve an optional
26.2 checksum-salt override. Verification refused and kept the prior local
baseline and native lock. The converter now rejects optional 26.2 overrides
on the 26.1 files path before writing. Identity-only conversion passed both
Bash and PowerShell; Builder conversion preserves authored descriptors for
later use with canonical 26.2 source.


## Completed wrapper qualification

All results below used disposable DEMO applications on the versions recorded
above. PowerShell results are PowerShell on Linux; Windows-native interruption
and process behavior remain unavailable on this host.

| Scenario | Bash | PowerShell | Verified result |
| --- | --- | --- | --- |
| Full publish and explicit application lock/unlock | Pass | Pass | Exact source/effective values, owner-filtered release and refusal of preexisting locks. |
| Existing-page partial publish | Pass | Pass | Selected edit applied; unrelated saved live login edit synchronized and preserved; whole-app verification and schema-2 baseline advanced. |
| Guarded `--no-team-notice` | Pass | Pass | Exact pre-edit Builder lock ID/owner/comment/time, explicit protection settings and unrelated page preserved; canonical source synchronized. |
| Subscription master remapping | Pass | Pass | Native list subscription moved to the replacement master; public subscribed-component evidence and precise source reference remap agreed. |
| Translated app full/partial publish | Pass | Pass | Seeded French-language mapping and generated translation SQL preserved; PNG bytes unchanged. |
| Builder/files canonical conversion | Pass | Pass | Stable complete exports, authored descriptors, translation artifacts and binary assets preserved; exact canonical-26.2 files verified. |

A cutoff-only no-notice descriptor was insufficient: a native selected import
advanced protection metadata when the salt was implicit. The command therefore
requires both an explicitly supplied 64-hex checksum salt and URL cutoff that
match live generated deployment evidence. Eligibility refuses before writing
when either is missing or differs. Native tests with both configured passed.
This is a policy for developers using separate workspace accounts and an agreed
guarded workflow; database checks cannot prove account exclusivity or prevent
raw SQLcl writers from bypassing locks.

Every application export now includes `-exptranslations`, which is required to
retain `generated-artifacts/translations.sql`. The translation fixture used a
real seeded/published French-language mapping, rather than a fully localized
production application. Generated translation SQL is compared as raw bytes.
Canonical files conversion additionally refuses ignored source edits; selected
page publishing refuses alias changes before importing. Alias changes use the
coordinated full workflow.

Private evidence is retained under ignored `.sync-state/` and `scratch/`:
subscription `apex-26.2-subscriptions-20261008/wrappers/report.json`, translation
`apex-26.2-translations-20261008/{wrappers,partial,files}/report.json`, and
no-notice `apex-26.2-browser-20261008/native-no-notice/report.json`. No salts,
credentials or session URLs are included in this document.

The final regression run passed 1,059 tests with 21 platform-specific skips.
Final review was performed inline against the uncommitted source, as requested;
there was no independent reviewer or Windows-native live qualification.


Final exact-target cleanup removed apps 942262, 944262, 945262, 946262 and
947262, the French translation mapping to 948263, and temporary developer
`APEX262_PROBE`. Public verification showed no remaining fixture/mapping/user,
restored the DEMO user inventory, and matched all nine original app metadata
records against the initial snapshot. Original app bytes were not compared.
Cleanup evidence is `.sync-state/apex-26.2-final-cleanup-20261008/verified/report.json`.
Three owned browser tabs were closed; Chrome required its last tab to remain,
so the last owned tab was cleared to `about:blank`.

Final checks also passed Python compilation, Ruff, Bash/PowerShell parsing,
example configuration validation, all repository JSON parsing, LF checks over
161 repository files, documentation contracts (18 tests) and manifest ownership
(8 tests). Inline review fixed two failures with regression coverage: ignored
canonical files edits now fail exact-source verification, and selected-page alias
changes refuse before writing. Project-owned files and original runtime source
were preserved; no custom team database objects or implicit migrations were added.
