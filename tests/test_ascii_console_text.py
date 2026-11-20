"""Text that a script prints or returns must be plain ASCII.

Python on Windows writes redirected or captured stdout and stderr in the ANSI code
page, not UTF-8. A character such as an em dash then reaches the terminal as a
replacement character, and a character outside that code page raises
UnicodeEncodeError. Report text that a script returns is printed the same way later.

This test parses every scripts/*.py and tools/*.py file and fails when:
- a string literal that is not a docstring has a non-ASCII character in its source
  text (an escape for a non-ASCII character is ASCII source and passes; a raw em dash
  in the source does not), or
- a string passed to print(), sys.stdout.write(), sys.stderr.write(), sys.exit() or an
  exception constructor in a raise has a non-ASCII value, however it is spelled.

Comments are not string literals, so they are never checked. A line whose non-ASCII
text must stay can carry a `# console-non-ascii-ok` comment, or be listed in ALLOWED.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import _no_real_sqlcl  # noqa: F401 (keeps tests away from a real SQLcl)

# This file sits in files/tests/ in the core repository and in tests/ in a template,
# so scripts/ and tools/ are its siblings in both layouts.
REPO = Path(__file__).resolve().parent.parent
FOLDERS = ("scripts", "tools")
CONSOLE_CALLS = frozenset({"print", "sys.exit", "sys.stdout.write", "sys.stderr.write"})
MARKER = "console-non-ascii-ok"
# "folder/file.py:line" entries that may keep non-ASCII text. Keep this empty.
ALLOWED: frozenset[str] = frozenset()


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _console_arguments(tree: ast.AST):
    """Yield each argument expression of a console call or of a raise that builds an exception."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func) in CONSOLE_CALLS:
            call = node
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            call = node.exc
        else:
            continue
        yield from call.args
        yield from (keyword.value for keyword in call.keywords)


def _docstrings(tree: ast.AST) -> set[ast.AST]:
    """The string constants that are the first statement of a module, class or function."""
    found: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(first.value)
    return found


def _source_text(byte_lines: list[bytes], node: ast.AST) -> str:
    """The node's source text, sliced by the UTF-8 column offsets that ast reports."""
    first, last = node.lineno - 1, node.end_lineno - 1
    if first == last:
        return byte_lines[first][node.col_offset:node.end_col_offset].decode("utf-8")
    parts = [byte_lines[first][node.col_offset:], *byte_lines[first + 1:last], byte_lines[last][:node.end_col_offset]]
    return b"\n".join(parts).decode("utf-8")


def _non_ascii_text(name: str, source: str) -> tuple[list[tuple[str, int, str]], set[str]]:
    """Return ((name, line, text) findings, ALLOWED entries that suppressed a finding).

    The text is the literal's source text when that has a non-ASCII character, or the
    printed value when only the value does (an escape inside a console call).
    """
    tree = ast.parse(source, filename=name)
    lines = source.split("\n")  # ast line numbers count \n, \r\n and \r; splitlines() also splits on form feeds
    byte_lines = [line.encode("utf-8") for line in lines]
    docstrings = _docstrings(tree)
    fstring_parts: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            fstring_parts.update(part for part in node.values if isinstance(part, ast.Constant))
    console_nodes: set[ast.AST] = set()
    for argument in _console_arguments(tree):
        console_nodes.update(ast.walk(argument))
    findings: set[tuple[str, int, str]] = set()
    used: set[str] = set()

    def record(node: ast.AST, text: str) -> None:
        key = f"{name}:{node.lineno}"
        if key in ALLOWED:
            used.add(key)
        elif not any(MARKER in line for line in lines[node.lineno - 1:node.end_lineno]):
            findings.add((name, node.lineno, text))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node in docstrings or node in fstring_parts:
                continue
            source_text = _source_text(byte_lines, node)
            if not source_text.isascii():
                record(node, source_text)
            elif node in console_nodes and not node.value.isascii():
                record(node, node.value)
        elif isinstance(node, ast.JoinedStr):
            source_text = _source_text(byte_lines, node)
            if not source_text.isascii():
                record(node, source_text)
            elif node in console_nodes:
                value = "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
                if not value.isascii():
                    record(node, value)
    return sorted(findings), used


def _shared_python_files() -> list[tuple[str, Path]]:
    return [(f"{folder}/{path.name}", path) for folder in FOLDERS for path in sorted((REPO / folder).glob("*.py"))]


class ConsoleTextIsAsciiTests(unittest.TestCase):
    def test_scripts_and_tools_have_only_ascii_console_and_report_text(self) -> None:
        files = _shared_python_files()
        self.assertTrue(any(name.startswith("scripts/") for name, _ in files), f"no scripts/*.py under {REPO}")
        findings: list[tuple[str, int, str]] = []
        used: set[str] = set()
        for name, path in files:
            found, suppressed = _non_ascii_text(name, path.read_text(encoding="utf-8"))
            findings.extend(found)
            used |= suppressed
        problems = [f"{name}:{line}: {ascii(text)}" for name, line, text in findings]
        self.assertEqual([], problems, "non-ASCII text in a script or tool; use ASCII equivalents (see the module docstring)")
        self.assertEqual(set(), set(ALLOWED) - used, "ALLOWED lists a line that no longer has a finding")

    def test_detector_flags_literals_and_console_values_and_ignores_comments_docstrings_escapes_and_marked_lines(self) -> None:
        em, en, arrow, nbsp = chr(0x2014), chr(0x2013), chr(0x2192), chr(0x00A0)
        backslash = chr(92)  # the escape text is built here so that this file stays ASCII
        # The sample is Python source. Placeholders keep this test file itself ASCII.
        sample = r'''
# A comment with an em dash <EM> is never checked.
"""A module docstring with an em dash <EM> is never checked."""
DATA = "<EM>"
print("<EM> plain")
print(f"step {count} <EM> done")
print(", ".join(["ok", "<EN>"]))
print("<EM> marked")  # console-non-ascii-ok
sys.stdout.write("<ARROW>\n")
sys.stderr.write("x" + "<NBSP>")
sys.exit("<EM> exit")
raise ValueError("bad <EM> value")
raise RuntimeError(f"bad {count} <EM>")
print("plain")
print("<ESC_EM> escaped in a console call")
FEFF = "<ESC_BOM>"
'''
        sample = (
            sample.replace("<EM>", em)
            .replace("<EN>", en)
            .replace("<ARROW>", arrow)
            .replace("<NBSP>", nbsp)
            .replace("<ESC_EM>", backslash + "u2014")
            .replace("<ESC_BOM>", backslash + "ufeff")
        )
        findings, used = _non_ascii_text("scripts/sample.py", sample)
        expected = [
            ("scripts/sample.py", 4, '"' + em + '"'),
            ("scripts/sample.py", 5, '"' + em + ' plain"'),
            ("scripts/sample.py", 6, 'f"step {count} ' + em + ' done"'),
            ("scripts/sample.py", 7, '"' + en + '"'),
            ("scripts/sample.py", 9, '"' + arrow + '\\n"'),
            ("scripts/sample.py", 10, '"' + nbsp + '"'),
            ("scripts/sample.py", 11, '"' + em + ' exit"'),
            ("scripts/sample.py", 12, '"bad ' + em + ' value"'),
            ("scripts/sample.py", 13, 'f"bad {count} ' + em + '"'),
            ("scripts/sample.py", 15, em + " escaped in a console call"),
        ]
        self.assertEqual(expected, findings)
        self.assertEqual(set(), used)


if __name__ == "__main__":
    unittest.main()
