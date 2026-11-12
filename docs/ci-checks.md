# CI checks

Why: over 2026-09-25 to 2026-10-19 about one run in four failed on the template repositories. The causes were
commits pushed straight to `main` without a check, lint errors found only in CI, three timing-sensitive tests
that fail on unchanged code, and Windows-only behaviour that cannot be reproduced on Linux. This page is the
agreed way to stop that.

## When CI runs

- On every pull request, once per commit (a newer commit cancels the older run).
- On every push to `main`, and by hand (`workflow_dispatch`).
- A branch pushed without a pull request runs nothing: open the pull request to get a result.

The jobs are `offline` (Linux, Python 3.10), `offline (latest Python 3.x)` (Linux, the newest Python 3.x, with the same
compile and behavioural-test steps but not lint or the other checks), `windows-offline` and `macos-installer`. The full
suite takes about 10 minutes on Linux and 20 to 30 on Windows.

The `offline` job also runs these two checks, after the shell syntax check and before its tests:

- **ShellCheck (error severity)** on `scripts/*.sh`. ShellCheck is preinstalled on `ubuntu-latest`. Only error-level
  findings fail the job; warnings are not gated.
- **PowerShell parse** of every `scripts/*.ps1`, with `[System.Management.Automation.Language.Parser]::ParseFile`. It
  finds syntax errors only, not runtime errors. It fails when a file has a parse error or when no `.ps1` file is found.
  `windows-offline` runs the same parse step.

## Before you push

```bash
python3 scripts/pre_push_check.py          # the files you changed
python3 scripts/pre_push_check.py --all    # everything CI lints
```

By default it checks only the files you changed (against the upstream branch or `origin/main`, plus uncommitted and
untracked files): Python syntax, Ruff, shell syntax, ShellCheck (error severity), the PowerShell parse and the test files
you changed, in about a second to a minute. Existing problems in other files do not block you, so it also works in a
project created from the template. `--all` checks every file under `scripts/`, `tools/` and `tests/`, as CI does; use it
before opening a pull request for a broad change. It does not run the whole suite or any Windows check.

ShellCheck runs only when `shellcheck` is on `PATH`, and the PowerShell parse only when `pwsh` is on `PATH`. When a tool
is missing, the check prints a one-line note and does not fail, so the local check stays usable on a machine without it;
CI still runs both. When a step fails, the output ends with `CHECKS FAILED:` and the names of the failed steps.

When a change touches a file this template shares with the 26.1 template, also run the parity report with the 26.1
checkout beside this one, and mark what it lists before you open the pull request; CI cannot run it, because it needs
both repositories. It must exit 0. See [template parity](superpowers/template-parity.md).

```bash
python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1
```

`python3 scripts/pre_push_check.py --install-hook` runs it on every `git push` (it never replaces an existing hook).
Install Ruff once with `python3 -m pip install 'ruff>=0.5'`.

## Skips that fail the run

Some tests skip when a tool is missing. On a CI runner that tool is always present, so a skip there means the check did
not run. `scripts/ci_run_tests.py --forbid-skip REGEX` (or the `CI_FORBID_SKIP` variable, which is the default; unset or
empty means off) fails the run when the reason of any skipped test matches `REGEX`, even when every test passed. It
prints the matching lines. The known-flaky rerun is checked too, so a rerun cannot hide a forbidden skip. The variable is
not passed to the test modules that the parallel run starts.

| Job | `CI_FORBID_SKIP` |
| --- | --- |
| `offline` | `PowerShell .*(not installed\|required)\|needs PowerShell\|requires PowerShell\|no PowerShell found` |
| `offline (latest Python 3.x)` | the same |
| `windows-offline` | `PowerShell .*(not installed\|required)\|needs PowerShell\|requires PowerShell\|no PowerShell found` |

Limits, by design:

- The pattern matches the reason text unittest prints (`skipped '<reason>'`), so it must match the message.
- `PowerShell Core is not installed` comes from a 30-second start probe of `pwsh` (`tests/_test_subprocess_safety.py`).
  On a slow runner that probe can fail, and then the job fails instead of skipping. Rerun the job before you act on it.
- The pattern covers every reason a test gives for a missing PowerShell ("... is not installed", "... is required", "needs PowerShell", "requires PowerShell").
- `no PowerShell found` comes only from Windows-only tests, where `powershell.exe` is always present.
- `PowerShell is not installed outside the sandbox-blocked snap launcher` is not in the list. It only fires when no
  `powershell.exe` and no `pwsh` outside `/snap/` exists, and the runner always has one. The `PowerShell 7 is not installed`,
  `needs PowerShell 7` and `requires PowerShell Core` reasons are also not in the list, so a missing `pwsh` there still skips
  silently. Add them to the pattern when you want them to fail too.

## Known flaky tests

`tests/known_flaky.txt` lists tests that fail on unchanged code and pass on a rerun. CI runs
`scripts/ci_run_tests.py`: if the suite fails and every failing test is in that list, only those tests run once
more and the job passes when the rerun passes. The run shows a `::warning` annotation, so each occurrence stays
visible. Any failing test that is not listed, or a rerun that fails, fails the job as before.
An abnormal process exit or a failing module without a completed unittest result also fails
the job, even when another module reports a listed flaky test. The same rule applies to a
single-process run that crashes after reporting a listed failure.

Add an entry only with evidence (the same code passing in another run) and a reason; remove it when the test is
fixed. Never add a test that fails every time.

## Parallel test runs

CI runs each test module in its own process, largest first: Linux runs 4 at a time and Windows runs 2
(`CI_TEST_JOBS`). The Windows suite starts many PowerShell processes and needs lower concurrency to stay
within the per-module timeout. On an 8 CPU Linux machine, `python3 scripts/ci_run_tests.py --jobs 8` is the
fast full-suite command; it prints a combined test/skip/failure count and exits non-zero if any module fails.
Use `--jobs 4` to match Linux CI or `--jobs 2` to match Windows CI. Without `--jobs` the runner uses one
process, and unittest arguments such as `-k` always use one process. The plain
`python3 -m unittest discover -s tests` command remains valid for a serial run.

On Windows a step before the tests sets `TEMP` and `TMP` to the long form of the runner's own temp
directory. The runner's default is a short 8.3 name (`RUNNER~1`) that `resolve()` expands, which broke
path comparisons in tests. The runner's own directory is kept rather than `runner.temp`, because that
one grants the Users group read access and a test checks that a report file carries no broad permission.
CI therefore does not cover a short temp path; code that compares paths still resolves both sides.

## Recommended repository settings

Set these in GitHub (Settings, Branches), they are not in the repository:

- Require a pull request before merging into `main`.
- Require the status checks `offline`, `offline (latest Python 3.x)`, `windows-offline` and `macos-installer` to pass.
  GitHub lists a check as a requirement only after it has run once, so add `offline (latest Python 3.x)` after the
  first run of the workflow that contains it.
- Do not allow administrators to bypass these rules, so no pull request merges before its checks pass.
- Allow auto-merge and turn it on for each pull request (`gh pr merge --auto --merge`): GitHub merges it when the
  checks pass, and nobody has to wait for the run.
- Automatically delete head branches after a merge.

AGENTS.md already says the template follows a normal branch and pull-request process; this makes it so.
