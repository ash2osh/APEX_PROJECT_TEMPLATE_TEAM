# CI checks

Why: over 2026-09-25 to 2026-10-19 about one run in four failed on the template repositories. The causes were
commits pushed straight to `main` without a check, lint errors found only in CI, three timing-sensitive tests
that fail on unchanged code, and Windows-only behaviour that cannot be reproduced on Linux. This page is the
agreed way to stop that.

## When CI runs

- On every pull request, once per commit (a newer commit cancels the older run).
- On every push to `main`, and by hand (`workflow_dispatch`).
- A branch pushed without a pull request runs nothing: open the pull request to get a result.

The jobs are `offline` (Linux), `windows-offline` and `macos-installer`. The full suite takes about 10 minutes on
Linux and 20 to 30 on Windows.

## Before you push

```bash
python3 scripts/pre_push_check.py          # the files you changed
python3 scripts/pre_push_check.py --all    # everything CI lints
```

By default it checks only the files you changed (against the upstream branch or `origin/main`, plus uncommitted and
untracked files): Python syntax, Ruff, shell syntax and the test files you changed, in about a second to a minute. Existing
problems in other files do not block you, so it also works in a project created from the template. `--all` checks every
file under `scripts/`, `tools/` and `tests/`, as CI does; use it before opening a pull request for a broad change. It does not
run the whole suite or any Windows check. `python3 scripts/pre_push_check.py --install-hook` runs it on every `git push`
(it never replaces an existing hook). Install Ruff once with `python3 -m pip install 'ruff>=0.5'`.

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

CI runs each test module in its own process, 4 at a time (`CI_TEST_JOBS`), largest first.
Locally, `python3 scripts/ci_run_tests.py --jobs 4` does the same; without `--jobs` it runs one
process as before, and a run with unittest arguments such as `-k` always uses one process.

On Windows a step before the tests sets `TEMP` and `TMP` to the long form of the runner's own temp
directory. The runner's default is a short 8.3 name (`RUNNER~1`) that `resolve()` expands, which broke
path comparisons in tests. The runner's own directory is kept rather than `runner.temp`, because that
one grants the Users group read access and a test checks that a report file carries no broad permission.
CI therefore does not cover a short temp path; code that compares paths still resolves both sides.

## Recommended repository settings

Set these in GitHub (Settings, Branches), they are not in the repository:

- Require a pull request before merging into `main`.
- Require the status checks `offline`, `windows-offline` and `macos-installer` to pass.
- Do not allow administrators to bypass these rules, so no pull request merges before its checks pass.
- Allow auto-merge and turn it on for each pull request (`gh pr merge --auto --merge`): GitHub merges it when the
  checks pass, and nobody has to wait for the run.
- Automatically delete head branches after a merge.

AGENTS.md already says the template follows a normal branch and pull-request process; this makes it so.
