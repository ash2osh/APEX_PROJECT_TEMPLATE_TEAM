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

`--forbid-skip REGEX` (default: the CI_FORBID_SKIP variable; unset or empty means off) fails the run when the
reason of any skipped test matches REGEX, even if every test passed. Every run this script makes is checked,
including the flaky rerun, so a rerun cannot hide a forbidden skip.
"""
from __future__ import annotations

import ast
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
FAILED_SUMMARY = re.compile(r"^FAILED \([^\r\n]+\)\s*\Z", re.MULTILINE)
RAN_SUMMARY = re.compile(r"^Ran (\d+) tests? in [\d.]+s$", re.MULTILINE)
OUTCOME_SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^\r\n]*)\))?$", re.MULTILINE)
# unittest -v prints `... skipped '<repr of reason>'`, for a method, a setUpClass or a module-level SkipTest.
SKIPPED_REASON = re.compile(r"""\bskipped ('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\Z""")
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


def completed_failure(output: str) -> bool:
    """Whether unittest finished reporting a named failure, rather than crashing mid-run."""
    return bool(failed_test_ids(output) and FAILED_SUMMARY.search(output))


def skip_reason(line: str) -> str | None:
    """The decoded reason of a `skipped '...'` line, or None when the line is not a skip."""
    match = SKIPPED_REASON.search(line)
    if not match:
        return None
    try:
        reason = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return match.group(1)
    return reason if isinstance(reason, str) else match.group(1)


def forbidden_skips(output: str, pattern: re.Pattern[str] | None) -> list[str]:
    """Output lines whose skip reason matches the forbidden pattern; always empty when the option is off."""
    if pattern is None:
        return []
    matches = []
    for line in output.splitlines():
        reason = skip_reason(line)
        if reason is not None and pattern.search(reason):
            matches.append(line)
    return matches


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


def _partial_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _write_output(text: str, *, end: str = "") -> None:
    """Write captured output, escaping characters unsupported by the active console encoding."""
    stream = sys.stdout
    value = text + end
    try:
        stream.write(value)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            raise
        escaped = value.encode(encoding, "backslashreplace").decode(encoding)
        stream.write(escaped)
    stream.flush()


def run_unittest(args: Sequence[str]) -> tuple[int, str]:
    """Run unittest in the repository root with closed stdin and a suite-level timeout."""
    timeout = 900
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *args], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        output = result.stdout or ""
        _write_output(output)
        return result.returncode, output
    except subprocess.TimeoutExpired as error:
        output = _partial_text(error.stdout)
        output += f"\nunittest timed out after {timeout} seconds\n"
        _write_output(output)
        return 124, output


def ordered_modules(tests_dir: Path) -> list[str]:
    """Test module names, largest file first, so the long modules start early."""
    paths = sorted(tests_dir.glob("test_*.py"), key=lambda item: (-item.stat().st_size, item.name))
    return [path.stem for path in paths]


def run_modules(modules: Sequence[str]) -> tuple[int, str]:
    """Run test modules in one process, as `discover -s tests` would import them; return (exit code, output)."""
    environment = dict(os.environ)
    # A module that runs this script again (its own tests) must not start a second parallel run.
    environment["CI_TEST_JOBS"] = "1"
    # The forbidden-skip list belongs to the outer run, which checks the skip lines these children print.
    environment.pop("CI_FORBID_SKIP", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "tests"), str(ROOT), environment.get("PYTHONPATH", "")) if part)
    # `discover -s tests` imports every module into one process, so the guard that keeps tests away from a
    # real SQLcl (and makes write_text write LF on Windows) applies to all of them; load it for each module too.
    guard = "import _no_real_sqlcl, sys, unittest; sys.argv[0] = 'python -m unittest'; unittest.main(module=None)"
    timeout = 900
    try:
        result = subprocess.run(
            [sys.executable, "-c", guard, "-v", *modules], cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        return result.returncode, result.stdout or ""
    except subprocess.TimeoutExpired as error:
        output = _partial_text(error.stdout)
        output += f"\nmodule process timed out after {timeout} seconds\n"
        return 124, output


def run_parallel(jobs: int, module_runner: Runner = run_modules, tests_dir: Path = ROOT / "tests") -> tuple[int, str]:
    """Run every test module in its own process, `jobs` at a time; print each one's output in order."""
    modules = ordered_modules(tests_dir)
    outputs: list[str] = []
    tests_run = skipped = failures = errors = reported_modules = 0
    code = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for module, (module_code, output) in zip(modules, pool.map(lambda name: module_runner([name]), modules), strict=True):
            print(f"===== {module}: exit {module_code}", flush=True)
            _write_output(output, end="" if output.endswith("\n") else "\n")
            outputs.append(output)
            ran = RAN_SUMMARY.search(output)
            outcome = OUTCOME_SUMMARY.search(output)
            if ran:
                reported_modules += 1
                tests_run += int(ran.group(1))
            if outcome:
                details = outcome.group(2) or ""
                counts = {name: int(value) for name, value in re.findall(r"(skipped|failures|errors)=(\d+)", details)}
                skipped += counts.get("skipped", 0)
                failures += counts.get("failures", 0)
                errors += counts.get("errors", 0)
            if module_code:
                # A child can die without a named failure while another child
                # reports a known flaky test. Keep that incomplete run fatal;
                # concatenating their output must not make it retryable.
                outcome = 2 if module_code == 1 and not completed_failure(output) else module_code
                if code in (0, 1):
                    code = outcome
    incomplete = len(modules) - reported_modules
    print(
        f"===== Combined: Ran {tests_run} tests; skipped={skipped}; failures={failures}; "
        f"errors={errors}; incomplete_modules={incomplete}",
        flush=True,
    )
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


def parse_forbid_skip(args: list[str]) -> tuple[re.Pattern[str] | None, list[str]]:
    """Take `--forbid-skip REGEX` out of the arguments; the default comes from CI_FORBID_SKIP, where unset or empty means off."""
    pattern = os.environ.get("CI_FORBID_SKIP", "")
    rest = []
    iterator = iter(args)
    for argument in iterator:
        if argument == "--forbid-skip":
            pattern = next(iterator, "")
        elif argument.startswith("--forbid-skip="):
            pattern = argument.split("=", 1)[1]
        else:
            rest.append(argument)
            continue
        if not pattern:
            raise SystemExit("--forbid-skip needs a regular expression")
    if not pattern:
        return None, rest
    try:
        return re.compile(pattern), rest
    except re.error as error:
        raise SystemExit(f"--forbid-skip needs a valid regular expression: {error}") from error


def report_forbidden(lines: Sequence[str], code: int = 0) -> int:
    """Print each forbidden skip line. Any of them fails the run, whatever the tests' own result was."""
    if not lines:
        return code
    print(f"::error title=Forbidden skip::{len(lines)} skipped test line(s) match the forbidden-skip pattern", flush=True)
    for line in lines:
        print(line, flush=True)
    return code or 1


def main(argv: Sequence[str] | None = None, *, runner: Runner = run_unittest, known_path: Path = KNOWN_FLAKY,
         parallel: Callable[[int], tuple[int, str]] = run_parallel) -> int:
    forbid, rest = parse_forbid_skip(list(argv) if argv is not None else sys.argv[1:])
    jobs, args = parse_jobs(rest)
    parallel_run = jobs > 1 and not args
    if parallel_run:
        code, output = parallel(jobs)
    else:
        code, output = runner(["discover", "-s", "tests", "-v", *args])
    forbidden = forbidden_skips(output, forbid)
    if code == 0:
        return report_forbidden(forbidden)
    # unittest reports ordinary failures with exit 1. Signals, fatal teardown
    # errors and incomplete output are never eligible for the flaky retry.
    # Parallel children were individually checked before aggregation above.
    if code != 1 or (not parallel_run and not completed_failure(output)):
        return report_forbidden(forbidden, code)
    failed = failed_test_ids(output)
    known = load_known(known_path)
    if not failed or not failed <= known:
        return report_forbidden(forbidden, code)
    names = sorted(failed)
    print(f"::warning title=Known flaky test failed::{', '.join(names)}; rerunning once", flush=True)
    # -k matches substrings of method names, and method names repeat across modules, so each
    # failing module is rerun on its own file: -p selects the file exactly, and -k only
    # narrows within that one module.
    rerun_code = 0
    for module in sorted({test_id.split(".", 1)[0] for test_id in names}):
        rerun = ["discover", "-s", "tests", "-p", f"{module}.py", "-v"]
        for test_id in names:
            if test_id.split(".", 1)[0] == module:
                rerun += ["-k", test_id.rsplit(".", 1)[1]]
        code, rerun_output = runner(rerun)
        # The rerun's skips count as well: a known flaky test must not hide a forbidden skip.
        forbidden += forbidden_skips(rerun_output, forbid)
        if code != 0:
            rerun_code = code
    if rerun_code == 0:
        print("::warning title=Known flaky test passed on rerun::" + ", ".join(names), flush=True)
    return report_forbidden(forbidden, rerun_code)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
