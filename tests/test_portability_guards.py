"""Ratchet lint for the portability bugs that failed CI while local runs passed.

The scanner walks every `*.py` under scripts/, tools/ and tests/ (sorted, posix relative paths, files that do not
parse are skipped, tests/fixtures/ is not scanned) and counts four rules per file:

* path-str-assert: an assertEqual-style call whose arguments contain `str(a / b)`. Compare Path objects instead.
* text-io-no-encoding: text I/O without an encoding (read_text, write_text, open in text mode, subprocess text=True).
* posix-only-api: POSIX-only os and signal attributes, and imports of POSIX-only modules.
* bare-bash (tests/ only): a subprocess argv that starts with the string "bash"; use fake_sqlcl.BASH.
* relative-path-str (tests/ only): `str(path.relative_to(root))`, which gives `a\\b` on Windows; use `.as_posix()`.

`tests/portability_baseline.json` holds today's count per rule and file. A file may not exceed its count; a new file
has a count of 0. After fixing old occurrences, regenerate the baseline with:

    python3 tests/test_portability_guards.py --write-baseline
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().with_name("portability_baseline.json")
SCAN_DIRS = ("scripts", "tools", "tests")
FIXTURES_PREFIX = "tests/fixtures/"

PATH_STR_ASSERT = "path-str-assert"
TEXT_IO = "text-io-no-encoding"
POSIX_ONLY = "posix-only-api"
BARE_BASH = "bare-bash"
RELATIVE_STR = "relative-path-str"
RULES = (PATH_STR_ASSERT, TEXT_IO, POSIX_ONLY, BARE_BASH, RELATIVE_STR)

HINTS = {
    PATH_STR_ASSERT: "compare Path objects, or .as_posix() on both sides",
    TEXT_IO: 'pass encoding="utf-8" (and errors= where the bytes may not be UTF-8)',
    POSIX_ONLY: "guard with os.name or hasattr and test both branches, or skip the test on Windows explicitly",
    BARE_BASH: "use fake_sqlcl.BASH",
    RELATIVE_STR: "use path.relative_to(root).as_posix() so keys and messages are the same on Windows",
}

ASSERT_METHODS = frozenset({
    "assertEqual", "assertNotEqual", "assertIn", "assertNotIn", "assertCountEqual",
    "assertListEqual", "assertDictEqual",
})
TEXT_PATH_METHODS = {"read_text": 1, "write_text": 2}  # positional count that already supplies encoding
TEXT_RUN_CALLS = frozenset({"run", "check_output", "Popen"})
ARGV_CALLS = frozenset({"run", "Popen", "check_output", "call"})
POSIX_OS_ATTRS = frozenset({"killpg", "getpgid", "setsid", "setpgrp", "getuid", "geteuid"})
POSIX_SIGNAL_ATTRS = frozenset({"SIGKILL", "SIGHUP", "SIGQUIT"})
POSIX_MODULES = frozenset({"fcntl", "pwd", "grp", "resource", "termios"})


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    return next((item for item in call.keywords if item.arg == name), None)


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_division_str(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and _is_name(node.func, "str") and len(node.args) == 1
        and isinstance(node.args[0], ast.BinOp) and isinstance(node.args[0].op, ast.Div)
    )


def _path_str_assert(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ASSERT_METHODS):
        return False
    arguments = [*node.args, *(item.value for item in node.keywords)]
    return any(_is_division_str(inner) for argument in arguments for inner in ast.walk(argument))


def _text_io(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in TEXT_PATH_METHODS:
        return not (_keyword(node, "encoding") or len(node.args) >= TEXT_PATH_METHODS[func.attr])
    if _is_name(func, "open") or (isinstance(func, ast.Attribute) and func.attr == "open" and _is_name(func.value, "io")):
        mode = node.args[1] if len(node.args) > 1 else None
        if mode is None and _keyword(node, "mode") is not None:
            mode = _keyword(node, "mode").value
        if mode is None:
            text = True
        elif isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            text = "b" not in mode.value
        else:
            return False  # a mode computed at run time is not judged
        return text and not (_keyword(node, "encoding") or len(node.args) >= 4)
    if isinstance(func, ast.Attribute) and func.attr in TEXT_RUN_CALLS and _is_name(func.value, "subprocess"):
        text_mode = _is_true(getattr(_keyword(node, "text"), "value", None)) or _is_true(
            getattr(_keyword(node, "universal_newlines"), "value", None))
        return text_mode and not _keyword(node, "encoding")
    return False


def _posix_only(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return (_is_name(node.value, "os") and node.attr in POSIX_OS_ATTRS) or (
            _is_name(node.value, "signal") and node.attr in POSIX_SIGNAL_ATTRS)
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] in POSIX_MODULES for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and bool(node.module) and node.module.split(".")[0] in POSIX_MODULES
    return False


def _argv_starts_with_bash(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "bash"
    )


def _bare_bash(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute) and func.attr in ARGV_CALLS and _is_name(func.value, "subprocess")
            and bool(node.args) and _argv_starts_with_bash(node.args[0])
        )
    if isinstance(node, ast.Assign):
        return _argv_starts_with_bash(node.value)
    if isinstance(node, ast.AnnAssign):
        return node.value is not None and _argv_starts_with_bash(node.value)
    return False


def _relative_str(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and _is_name(node.func, "str") and len(node.args) == 1
        and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Attribute)
        and node.args[0].func.attr == "relative_to"
    )


def scan_source(source: str, relpath: str) -> list[tuple[str, int]]:
    """(rule, line) for every finding in one file's source; empty when the source does not parse."""
    try:
        tree = ast.parse(source, filename=relpath)
    except (SyntaxError, ValueError):
        return []
    in_tests = relpath.startswith("tests/")
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if _path_str_assert(node):
            found.append((PATH_STR_ASSERT, line))
        if _text_io(node):
            found.append((TEXT_IO, line))
        if _posix_only(node):
            found.append((POSIX_ONLY, line))
        if in_tests and _bare_bash(node):
            found.append((BARE_BASH, line))
        if in_tests and _relative_str(node):
            found.append((RELATIVE_STR, line))
    return sorted(found, key=lambda item: (item[1], item[0]))


def python_files(root: Path = ROOT) -> list[tuple[str, Path]]:
    """(posix relative path, absolute path) of every scanned `*.py`, sorted; folders a template lacks are skipped."""
    files: list[tuple[str, Path]] = []
    for directory in SCAN_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if not path.is_file():
                continue
            relpath = path.relative_to(root).as_posix()
            if not relpath.startswith(FIXTURES_PREFIX):
                files.append((relpath, path))
    return sorted(files)


def scan_tree(root: Path = ROOT) -> dict[str, dict[str, list[int]]]:
    """rule -> posix relative path -> sorted list of finding lines, for every rule (empty dicts when clean)."""
    result: dict[str, dict[str, list[int]]] = {rule: {} for rule in RULES}
    for relpath, path in python_files(root):
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for rule, line in scan_source(source, relpath):
            result[rule].setdefault(relpath, []).append(line)
    return result


def baseline_from(found: Mapping[str, Mapping[str, list[int]]]) -> dict[str, dict[str, int]]:
    """The counts the baseline file stores: rule -> path -> count, zero counts left out."""
    return {
        rule: {path: len(lines) for path, lines in sorted(found.get(rule, {}).items()) if lines}
        for rule in RULES
    }


def regressions(found: Mapping[str, Mapping[str, list[int]]], baseline: Mapping[str, Mapping[str, int]]) -> list[str]:
    """`path:line: rule: fix` for every finding of each file whose count exceeds its baseline (a new file has 0)."""
    messages: list[str] = []
    for rule in RULES:
        allowed = baseline.get(rule, {})
        for path, lines in sorted(found.get(rule, {}).items()):
            if len(lines) <= allowed.get(path, 0):
                continue
            messages.extend(f"{path}:{line}: {rule}: {HINTS[rule]}" for line in lines)
    return messages


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, int]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(found: Mapping[str, Mapping[str, list[int]]], path: Path = BASELINE_PATH) -> dict[str, dict[str, int]]:
    baseline = baseline_from(found)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return baseline


class ScanSourceTests(unittest.TestCase):
    def rules(self, source: str, relpath: str = "scripts/module.py") -> list[str]:
        return [rule for rule, _line in scan_source(source, relpath)]

    def test_path_str_assert_flags_str_of_a_division_inside_an_assert(self):
        source = 'self.assertEqual(str(root / "a.txt"), "x")\n'
        self.assertEqual(self.rules(source, "tests/test_a.py"), [PATH_STR_ASSERT])
        self.assertEqual(scan_source(source, "tests/test_a.py"), [(PATH_STR_ASSERT, 1)])

    def test_path_str_assert_flags_the_str_in_every_listed_assert_and_keyword_position(self):
        source = (
            "self.assertIn(str(a / 'b'), items)\n"
            "self.assertNotIn(str(a / 'b'), items)\n"
            "self.assertCountEqual([str(a / 'b')], items)\n"
            "self.assertListEqual([str(a / 'b')], items)\n"
            "self.assertDictEqual(msg=str(a / 'b'), d={})\n"
            "self.assertNotEqual(x, str(a / 'b'))\n"
        )
        self.assertEqual(self.rules(source, "tests/test_a.py"), [PATH_STR_ASSERT] * 6)

    def test_path_str_assert_ignores_path_comparison_and_str_outside_asserts(self):
        source = (
            "self.assertEqual(root / 'a.txt', expected)\n"
            "self.assertEqual(str(root), 'x')\n"
            "print(str(root / 'a.txt'))\n"
            "self.assertTrue(str(root / 'a'))\n"
        )
        self.assertEqual(self.rules(source, "tests/test_a.py"), [])

    def test_text_io_flags_read_and_write_text_without_encoding(self):
        source = "path.read_text()\npath.write_text('x')\n"
        self.assertEqual(scan_source(source, "scripts/a.py"), [(TEXT_IO, 1), (TEXT_IO, 2)])

    def test_text_io_accepts_encoding_as_keyword_or_position(self):
        source = "path.read_text(encoding='utf-8')\npath.write_text('x', encoding='utf-8')\npath.read_text('utf-8')\npath.write_text('x', 'utf-8')\n"
        self.assertEqual(self.rules(source), [])

    def test_text_io_flags_open_in_text_mode_without_encoding(self):
        source = "open('f')\nopen('f', 'w')\nopen('f', mode='r')\nio.open('f', 'r')\n"
        self.assertEqual(self.rules(source), [TEXT_IO] * 4)

    def test_text_io_ignores_binary_open_encoded_open_and_run_time_modes(self):
        source = (
            "open('f', 'rb')\nopen('f', 'wb')\nopen('f', 'r', encoding='utf-8')\n"
            "open('f', 'r', -1, 'utf-8')\nopen('f', mode)\nopen('f', mode='rb')\n"
        )
        self.assertEqual(self.rules(source), [])

    def test_text_io_flags_subprocess_text_mode_without_encoding(self):
        source = (
            "subprocess.run(cmd, text=True)\n"
            "subprocess.check_output(cmd, universal_newlines=True)\n"
            "subprocess.Popen(cmd, text=True, stdout=PIPE)\n"
        )
        self.assertEqual(self.rules(source), [TEXT_IO] * 3)

    def test_text_io_ignores_subprocess_calls_with_encoding_or_bytes(self):
        source = (
            "subprocess.run(cmd, text=True, encoding='utf-8')\n"
            "subprocess.run(cmd)\n"
            "subprocess.run(cmd, text=False)\n"
            "subprocess.run(cmd, text=flag)\n"
        )
        self.assertEqual(self.rules(source), [])

    def test_posix_only_flags_os_and_signal_attributes(self):
        source = (
            "os.killpg(pid, sig)\nos.getpgid(0)\nos.setsid()\nos.setpgrp()\nos.getuid()\nos.geteuid()\n"
            "signal.SIGKILL\nsignal.SIGHUP\nsignal.SIGQUIT\n"
        )
        self.assertEqual(self.rules(source), [POSIX_ONLY] * 9)

    def test_posix_only_ignores_guarded_names_and_portable_os_calls(self):
        source = "if hasattr(os, 'killpg'):\n    pass\nos.kill(pid, 0)\nsignal.SIGTERM\nos.getcwd()\n"
        self.assertEqual(self.rules(source), [])

    def test_posix_only_flags_plain_and_from_imports_of_posix_modules(self):
        source = "import fcntl\nimport pwd, json\nfrom grp import getgrnam\nimport termios.foo\nfrom resource import getrlimit\n"
        self.assertEqual(self.rules(source), [POSIX_ONLY] * 5)

    def test_posix_only_ignores_portable_and_relative_imports(self):
        source = "import json\nfrom os import path\nfrom . import fcntl\nfrom .pwd import x\n"
        self.assertEqual(self.rules(source), [])

    def test_bare_bash_flags_a_subprocess_argv_that_starts_with_bash(self):
        source = (
            "subprocess.run(['bash', '-c', 'x'])\n"
            "subprocess.Popen(('bash', 'x'))\n"
            "subprocess.check_output(['bash'])\n"
            "subprocess.call(['bash'])\n"
        )
        self.assertEqual(self.rules(source, "tests/test_a.py"), [BARE_BASH] * 4)

    def test_bare_bash_flags_an_argv_list_assigned_with_bash_first(self):
        source = "argv = ['bash', '-c', 'x']\nargv2: list = ['bash']\n"
        self.assertEqual(self.rules(source, "tests/test_a.py"), [BARE_BASH] * 2)

    def test_bare_bash_ignores_the_helper_and_other_programs_and_is_limited_to_tests(self):
        source = "subprocess.run([BASH, '-c', 'x'])\nsubprocess.run(['git', 'bash'])\nsubprocess.run(['bash'])\nx = ['bash']\n"
        self.assertEqual(self.rules(source, "tests/test_a.py"), [BARE_BASH] * 2)
        self.assertEqual(self.rules(source, "scripts/a.py"), [])

    def test_a_source_that_does_not_parse_is_skipped(self):
        self.assertEqual(scan_source("def broken(:\n", "tests/test_a.py"), [])
        self.assertEqual(scan_source("x = '\0'\n", "tests/test_a.py"), [])

    def test_findings_are_sorted_by_line(self):
        source = "os.getuid()\npath.read_text()\n"
        self.assertEqual(scan_source(source, "scripts/a.py"), [(POSIX_ONLY, 1), (TEXT_IO, 2)])


class TodaysFailuresTests(unittest.TestCase):
    """The two Windows CI failures of 2026-11-21 that a local run could not show."""

    def test_the_env_path_compared_as_a_string_is_found(self) -> None:
        source = 'self.assertEqual("ENV:" + str(root / ".env"), lines[-1])\n'
        self.assertEqual(scan_source(source, "tests/test_x.py"), [(PATH_STR_ASSERT, 1)])

    def test_a_snapshot_keyed_by_str_of_relative_to_is_found(self) -> None:
        source = 'snapshot = {str(path.relative_to(mirror)): path.read_bytes() for path in files}\n'
        self.assertEqual(scan_source(source, "tests/test_x.py"), [(RELATIVE_STR, 1)])

    def test_as_posix_keys_are_not_found(self) -> None:
        source = 'snapshot = {path.relative_to(mirror).as_posix(): 1 for path in files}\n'
        self.assertEqual(scan_source(source, "tests/test_x.py"), [])

    def test_relative_str_outside_tests_is_not_counted(self) -> None:
        self.assertEqual(scan_source('x = str(p.relative_to(r))\n', "scripts/x.py"), [])


class TreeScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relpath: str, source: str) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_a_template_without_some_directories_still_scans_the_others(self):
        self.write("scripts/a.py", "path.read_text()\n")
        found = scan_tree(self.root)
        self.assertEqual(found[TEXT_IO], {"scripts/a.py": [1]})
        self.assertEqual(sorted(found), sorted(RULES))

    def test_fixtures_and_unparseable_files_are_not_counted(self):
        self.write("tests/fixtures/test_fixture.py", "path.read_text()\n")
        self.write("tests/test_broken.py", "def (:\nos.killpg\n")
        self.write("scripts/notes.txt", "path.read_text()\n")
        found = scan_tree(self.root)
        self.assertEqual({rule: found[rule] for rule in RULES}, {rule: {} for rule in RULES})

    def test_paths_are_posix_relative_and_traversal_is_sorted(self):
        self.write("tools/z.py", "path.read_text()\n")
        self.write("scripts/sub/b.py", "path.read_text()\n")
        self.write("scripts/a.py", "path.read_text()\n")
        found = scan_tree(self.root)
        self.assertEqual(list(found[TEXT_IO]), ["scripts/a.py", "scripts/sub/b.py", "tools/z.py"])
        self.assertEqual(list(baseline_from(found)[TEXT_IO]), ["scripts/a.py", "scripts/sub/b.py", "tools/z.py"])

    def test_python_files_lists_each_scanned_module_once_in_order(self):
        self.write("tests/test_b.py", "")
        self.write("tests/test_a.py", "")
        self.write("docs/x.py", "")
        self.assertEqual([relpath for relpath, _path in python_files(self.root)], ["tests/test_a.py", "tests/test_b.py"])


class RatchetTests(unittest.TestCase):
    def found(self, **files: list[int]) -> dict[str, dict[str, list[int]]]:
        return {POSIX_ONLY: {name.replace("__", "/", 1) + ".py": lines for name, lines in files.items()}}

    def test_a_new_file_has_a_baseline_of_zero(self):
        messages = regressions(self.found(scripts__new=[3]), {})
        self.assertEqual(messages, [f"scripts/new.py:3: {POSIX_ONLY}: {HINTS[POSIX_ONLY]}"])

    def test_a_file_at_or_below_its_baseline_passes_silently(self):
        found = self.found(scripts__a=[1, 2])
        self.assertEqual(regressions(found, {POSIX_ONLY: {"scripts/a.py": 2}}), [])
        self.assertEqual(regressions(found, {POSIX_ONLY: {"scripts/a.py": 5}}), [])

    def test_a_file_above_its_baseline_lists_every_line_with_the_fix_hint(self):
        found = self.found(scripts__a=[1, 4, 9])
        messages = regressions(found, {POSIX_ONLY: {"scripts/a.py": 2}})
        self.assertEqual(messages, [
            f"scripts/a.py:1: {POSIX_ONLY}: {HINTS[POSIX_ONLY]}",
            f"scripts/a.py:4: {POSIX_ONLY}: {HINTS[POSIX_ONLY]}",
            f"scripts/a.py:9: {POSIX_ONLY}: {HINTS[POSIX_ONLY]}",
        ])

    def test_the_baseline_is_per_rule_and_per_file(self):
        found = {TEXT_IO: {"scripts/a.py": [1]}, POSIX_ONLY: {"scripts/a.py": [2]}}
        baseline = {TEXT_IO: {"scripts/a.py": 1}, POSIX_ONLY: {}}
        messages = regressions(found, baseline)
        self.assertEqual(messages, [f"scripts/a.py:2: {POSIX_ONLY}: {HINTS[POSIX_ONLY]}"])

    def test_baseline_from_keeps_only_nonzero_counts_in_sorted_order(self):
        found = {PATH_STR_ASSERT: {}, TEXT_IO: {"tests/b.py": [1, 2], "scripts/a.py": [7]}, POSIX_ONLY: {}, BARE_BASH: {"tests/c.py": []}}
        self.assertEqual(baseline_from(found), {
            PATH_STR_ASSERT: {}, TEXT_IO: {"scripts/a.py": 1, "tests/b.py": 2}, POSIX_ONLY: {}, BARE_BASH: {},
            RELATIVE_STR: {},
        })

    def test_write_baseline_writes_sorted_json_with_a_trailing_newline(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "baseline.json"
            write_baseline({TEXT_IO: {"tests/b.py": [1], "scripts/a.py": [2]}}, target)
            text = target.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text.splitlines()[1], '  "bare-bash": {},')
        self.assertEqual(json.loads(text)[TEXT_IO], {"scripts/a.py": 1, "tests/b.py": 1})


class PortabilityBaselineTests(unittest.TestCase):
    def test_no_file_exceeds_its_portability_baseline(self):
        self.assertTrue(BASELINE_PATH.is_file(), "tests/portability_baseline.json is missing; run python3 tests/test_portability_guards.py --write-baseline")
        messages = regressions(scan_tree(ROOT), load_baseline(BASELINE_PATH))
        self.assertEqual(messages, [], "\n" + "\n".join(messages))


def main(argv: Iterable[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--write-baseline" in arguments:
        baseline = write_baseline(scan_tree(ROOT), BASELINE_PATH)
        for rule in RULES:
            print(f"{rule}: {sum(baseline[rule].values())} in {len(baseline[rule])} files")
        print(f"wrote {BASELINE_PATH.relative_to(ROOT).as_posix()}")
        return 0
    unittest.main(argv=[sys.argv[0], *arguments])
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
