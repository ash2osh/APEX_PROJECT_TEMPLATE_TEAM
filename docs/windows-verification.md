# Windows verification

`scripts/windows_check.ps1` is an offline readiness check of a template checkout, run on a Windows
machine. It checks the PowerShell scripts, the environment loader, the Python tools, the offline team
commands, the unit tests and the pre-push check. It does not connect to a database and does not run an
import or a migration.

It exists because most PowerShell checks in CI use PowerShell 7 (`pwsh`). The PowerShell parse step in CI uses
`pwsh`, and most PowerShell tests are gated on `pwsh`. Windows PowerShell 5.1 (`powershell.exe`) parses
code, handles encoding and runs native commands differently, so a machine that uses 5.1 needs its own
check. The `windows-powershell-51` job runs this script's fast steps under 5.1. See [CI checks](ci-checks.md)
for what CI runs.

The script was first run on Windows 11 (build 26300) on 2026-11-20, under Windows PowerShell 5.1.26100 and
PowerShell 7.7, in both templates. CI now runs its fast steps under Windows PowerShell 5.1 in the
`windows-powershell-51` job (see [CI checks](ci-checks.md)).

## How to run it

Under Windows PowerShell 5.1, the engine the team uses:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows_check.ps1
powershell.exe -NoProfile -File scripts\windows_check.ps1 -Skip tests,pre-push -ReportPath report.txt
```

Under PowerShell 7. The script refuses to run under `pwsh` unless `-AllowPowerShellCore` is given:

```powershell
pwsh -NoProfile -File scripts/windows_check.ps1 -AllowPowerShellCore
```

Parameters:

| Parameter | Meaning |
| --- | --- |
| `-Only` | Run only the named steps. Names are separated by commas or given as separate values. |
| `-Skip` | Run every step except the named ones. Same name format as `-Only`. |
| `-RepoRoot` | The checkout to check. Defaults to the parent of the `scripts` folder that holds the script. |
| `-AllowPowerShellCore` | Allow the run under PowerShell 6 or newer. Without it, `pwsh` is a FAIL. |
| `-ReportPath` | Also write the banner, the `RESULT` lines and the exit code to this file. The indented detail lines under each step are printed only, not written. |

`-Only` and `-Skip` cannot be combined. An unknown step name is a usage error.

## The steps

The steps run in this order.

| Step | What it checks |
| --- | --- |
| `environment` | Prints the engine version and edition. FAILs under PowerShell 6 or newer unless `-AllowPowerShellCore` is given. |
| `ascii` | Every `scripts/*.ps1` is pure ASCII, or starts with a UTF-8 byte order mark. A file with non-ASCII bytes and no byte order mark FAILs. |
| `parse` | Parses every `scripts/*.ps1` with the running engine's parser. Any syntax error FAILs. |
| `env-loader` | Writes five variants of a minimal `.env` (LF, CRLF, UTF-8 with a byte order mark, a non-ASCII comment, and UTF-16) and runs `scripts/load_env.ps1` on each. UTF-16 must be refused with the "save it as UTF-8" message. The other four must load without a refusal and set the DEV variables correctly. |
| `python-tools` | Finds Python 3.10 or newer (`python3`, `python` or `py -3`) and byte-compiles every `.py` file under `scripts/` with `python -I`. SKIPs when no suitable Python is found. |
| `team-offline` | Writes a DEV-only `.env` to a temporary folder, runs `scripts/team.ps1 targets` (it must list `SCHEMA1`) and then `verify-local`. See the next section for what counts as a pass. |
| `tests` | Runs `python -m unittest discover -s tests` from the checkout root and reports the `Ran` and `OK` or `FAILED` lines; only unittest's own summary lines count for the result detail. On a failure it lists the failing tests (the `FAIL:` and `ERROR:` headers, up to 50) before the last 60 lines of output. This takes about ten minutes or more, and prints nothing until it ends. |
| `pre-push` | Runs `python scripts/pre_push_check.py --all` from the checkout root. The check is the same as the `--all` form in [CI checks](ci-checks.md#before-you-push). |

The script uses the Python interpreter it finds, not the engine it runs in. Running the script under
`pwsh` does not change which Python the unit tests start.

## Result lines and exit codes

Each step ends with one line in this form:

```text
RESULT <step> PASS|FAIL|SKIP <detail>
```

The script prints a summary table after the steps, then exits with one of these codes:

- `0`: no step FAILed. A SKIP is allowed.
- `1`: at least one step FAILed, or the `-ReportPath` file could not be written.
- `2`: a usage error, such as an unknown step name or a missing repository root.

## The team-offline step on a bare template

A bare template ships no application under `apps/`. `verify-local` then reports the missing configured
application directory, and the step passes with this detail:

```text
bare template: no application under apps/, so configured_app_100 is expected to fail
```

The step passes only when every failing check in `verify-local` is a `configured_app_<id>` check. Any
other failure makes the step FAIL. The number in the detail is 100 on a bare template. A project that has
applications under `apps/` is checked against its lowest application id, and then the step passes only
when `verify-local` exits 0.

## Windows PowerShell 5.1 rules the script follows

The script is written to run on Windows PowerShell 5.1. Keep to these rules when you change it:

- No null-coalescing (`??`) or null-conditional (`?.`) operators.
- No ternary operator.
- No pipeline chain operators (`&&` and `||`).
- No `$IsWindows`, `ForEach-Object -Parallel`, `ProcessStartInfo.ArgumentList` or `-AsByteStream`.
- `#Requires -Version 5.1` on the first line.
- ASCII only, with LF line endings.

## Running the pwsh-gated tests under Windows PowerShell 5.1

21 test files get their PowerShell from `working_pwsh()` in `tests/_test_subprocess_safety.py`. Without
`pwsh`, those tests skip. Set `TEMPLATE_TEST_POWERSHELL=powershell` to make them use Windows PowerShell 5.1
instead. There is no fallback to `pwsh`, and any value other than `pwsh` or `powershell` is an error.
In PowerShell:

```powershell
$env:TEMPLATE_TEST_POWERSHELL = "powershell"
python -m unittest discover -s tests
Remove-Item Env:TEMPLATE_TEST_POWERSHELL
```

Off Windows, those tests skip under this setting.

## What the script does not do

- It does not connect to a database, and it does not import, migrate or deploy anything.
- The `tests` step is slow, and it prints nothing until it ends.
- The engine that runs the script does not change which Python interpreter the Python tests use.

## What to do with a FAIL

Many files under `scripts/`, `tools/` and `tests/` are shared core files. Fix a shared file in the core
repository, not in the template copy, then sync it back as described in
[CI checks](ci-checks.md#shared-core-files). Only a project-owned file is fixed in the project itself.

- `environment`: rerun with `powershell.exe` for the 5.1 check. Pass `-AllowPowerShellCore` only when you
  mean to check PowerShell 7.
- `ascii`: remove the non-ASCII characters from the named file, or save it with a UTF-8 byte order mark.
  Fix a shared script in the core repository.
- `parse`: fix the syntax error at the file and line the output names. Fix a shared script in the core
  repository.
- `env-loader`: read the variant that failed. A change in `scripts/load_env.ps1` is a core change.
- `python-tools`: the output names the file and the syntax error. Fix a shared file in the core
  repository, or a project file in the project.
- `team-offline`: read the `verify-local` lines printed above the FAIL. An application source or
  descriptor problem is fixed in the project. A problem in `scripts/team.ps1` is a core change.
- `tests`: rerun the failing module on its own with
  `python -m unittest discover -s tests -p "test_<module>.py"`, for example
  `-p "test_verify_local.py"`. `python -m unittest tests.<module>` does not work: the test
  modules import shared helpers such as `_test_subprocess_safety` as top-level modules. Fix a shared test
  in the core repository. Do not add a test to `tests/known_flaky.txt` without evidence that it passes on
  unchanged code.
- `pre-push`: run `python3 scripts/pre_push_check.py --all` on a machine that has the tools, and fix the
  files it names. Fix a shared file in the core repository.
