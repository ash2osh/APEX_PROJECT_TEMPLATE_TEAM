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
python3 scripts/pre_push_check.py
```

It runs Python syntax, Ruff, shell syntax and the test files you changed, in about a second to a minute. It does not
run the whole suite or any Windows check. `python3 scripts/pre_push_check.py --install-hook` runs it on every
`git push` (it never replaces an existing hook). Install Ruff once with `python3 -m pip install 'ruff>=0.5'`.

## Known flaky tests

`tests/known_flaky.txt` lists tests that fail on unchanged code and pass on a rerun. CI runs
`scripts/ci_run_tests.py`: if the suite fails and every failing test is in that list, only those tests run once
more and the job passes when the rerun passes. The run shows a `::warning` annotation, so each occurrence stays
visible. Any failing test that is not listed, or a rerun that fails, fails the job as before.

Add an entry only with evidence (the same code passing in another run) and a reason; remove it when the test is
fixed. Never add a test that fails every time.

## Recommended repository settings

Set these in GitHub (Settings, Branches), they are not in the repository:

- Require a pull request before merging into `main`.
- Require the status checks `offline`, `windows-offline` and `macos-installer` to pass.

AGENTS.md already says the template follows a normal branch and pull-request process; this makes it so.
