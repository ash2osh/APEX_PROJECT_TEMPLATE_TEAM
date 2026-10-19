#!/usr/bin/env python3
"""Run the behavioral tests; rerun once a failure made only of known flaky tests.

CI runs this instead of calling unittest directly. A failure always fails the job unless
every failing test is listed in tests/known_flaky.txt; then only those tests run once more
and the job passes when that rerun passes. An unlisted failing test, a crash with no
failing test, or a failed rerun fails the job exactly as before. A rerun is reported as a
`::warning` annotation so it stays visible in the run.

`--jobs N` (default: the CI_TEST_JOBS variable, else 1) runs each test module in its own
process, N at a time, largest module first. Each module's output is printed whole, in
that order, and the job fails when any module fails.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWN_FLAKY = ROOT / "tests" / "known_flaky.txt"
FAILURE = re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)")
Runner = Callable[[Sequence[str]], tuple[int, str]]


def failed_test_ids(output: str) -> set[str]:
    """`module.Class.method` of every failing test named in unittest output (subtest failures included).

    Python 3.10 prints `FAIL: method (module.Class)`; 3.11 and later print `FAIL: method (module.Class.method)`.
    """
    ids = set()
    for line in output.splitlines():
        match = FAILURE.match(line)
        if match:
            method, owner = match.groups()
            ids.add(owner if owner.endswith("." + method) else f"{owner}.{method}")
    return ids


def load_known(path: Path) -> set[str]:
    """Test ids from the list file: one per line, `# reason` after it, blank and `#` lines ignored."""
    if not path.is_file():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.add(entry)
    return entries


def run_unittest(args: Sequence[str]) -> tuple[int, str]:
    """Run unittest in the repository root, echo its output as it arrives and return (exit code, output)."""
    process = subprocess.Popen(
        [sys.executable, "-m", "unittest", *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    lines = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return process.wait(), "".join(lines)


def ordered_modules(tests_dir: Path) -> list[str]:
    """Test module names, largest file first, so the long modules start early."""
    paths = sorted(tests_dir.glob("test_*.py"), key=lambda item: (-item.stat().st_size, item.name))
    return [path.stem for path in paths]


def run_modules(modules: Sequence[str]) -> tuple[int, str]:
    """Run test modules in one process, as `discover -s tests` would import them; return (exit code, output)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "tests"), str(ROOT), environment.get("PYTHONPATH", "")) if part)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *modules], cwd=ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout


def run_parallel(jobs: int, module_runner: Runner = run_modules, tests_dir: Path = ROOT / "tests") -> tuple[int, str]:
    """Run every test module in its own process, `jobs` at a time; print each one's output in order."""
    modules = ordered_modules(tests_dir)
    outputs: list[str] = []
    code = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for module, (module_code, output) in zip(modules, pool.map(lambda name: module_runner([name]), modules)):
            print(f"===== {module}: exit {module_code}", flush=True)
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
            outputs.append(output)
            code = code or module_code
    return code, "".join(outputs)


def parse_jobs(args: list[str]) -> tuple[int, list[str]]:
    """Take `--jobs N` out of the arguments; the default comes from CI_TEST_JOBS, else 1."""
    jobs = os.environ.get("CI_TEST_JOBS", "1")
    rest = []
    iterator = iter(args)
    for argument in iterator:
        if argument == "--jobs":
            jobs = next(iterator, "")
        elif argument.startswith("--jobs="):
            jobs = argument.split("=", 1)[1]
        else:
            rest.append(argument)
    if not jobs.isdigit() or int(jobs) < 1:
        raise SystemExit(f"--jobs needs a positive whole number, got {jobs!r}")
    return int(jobs), rest


def main(argv: Sequence[str] | None = None, *, runner: Runner = run_unittest, known_path: Path = KNOWN_FLAKY,
         parallel: Callable[[int], tuple[int, str]] = run_parallel) -> int:
    jobs, args = parse_jobs(list(argv) if argv is not None else sys.argv[1:])
    if jobs > 1 and not args:
        code, output = parallel(jobs)
    else:
        code, output = runner(["discover", "-s", "tests", "-v", *args])
    if code == 0:
        return 0
    failed = failed_test_ids(output)
    known = load_known(known_path)
    if not failed or not failed <= known:
        return code
    names = sorted(failed)
    print(f"::warning title=Known flaky test failed::{', '.join(names)}; rerunning once", flush=True)
    rerun = ["discover", "-s", "tests", "-v"]
    for test_id in names:
        rerun += ["-k", test_id.rsplit(".", 1)[1]]
    rerun_code, _ = runner(rerun)
    if rerun_code == 0:
        print("::warning title=Known flaky test passed on rerun::" + ", ".join(names), flush=True)
    return rerun_code


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
