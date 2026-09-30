#!/usr/bin/env python3
"""
Ensures tree-sitter-sql is installed and patches the local Graphify package
to natively recognize .apx (Oracle APEX export) files as AST code files.
"""
import glob
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_EXTRACTOR = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"
# The exact text this installer writes; the only proof that .apx is ours.
DETECT_MARKER = "'.sql', '.apx',"
# Graphify's code-extension set, one line: CODE_EXTENSIONS = {..., '.sql', ...}
CODE_EXTENSIONS_RE = re.compile(r"^CODE_EXTENSIONS\s*=\s*\{[^}\n]*'\.sql',[^}\n]*\}", re.MULTILINE)
SQL_LINK_IMPORT = "from graphify.extractors.apexlang import extract_sql_linked  # noqa: F401"


def graphify_console_interpreter() -> str | None:
    """Return the interpreter behind the `graphify` console script on PATH.

    A `uv tool install` gives Graphify its own isolated interpreter, so
    importing graphify in *this* process usually finds nothing, or finds a
    different copy. The console script's shebang names the right one. Windows
    shims are compiled .exe launchers with no shebang to read.
    """
    graphify_bin = shutil.which("graphify")
    if not graphify_bin or not os.path.exists(graphify_bin):
        return None
    try:
        with open(graphify_bin, encoding="utf-8") as handle:
            first_line = handle.readline()
    except (UnicodeDecodeError, OSError):
        return None
    if not first_line.startswith("#!"):
        return None
    interpreter = first_line.strip()[2:].strip()
    return interpreter if os.path.exists(interpreter) else None


def find_graphify_dirs():
    # 1. The interpreter behind `graphify` on PATH is the installation that
    #    actually runs. Patch only that one when it can be resolved: sweeping
    #    every globbed path makes an orphaned environment fail the whole setup.
    interpreter = graphify_console_interpreter()
    if interpreter:
        try:
            located = subprocess.run(
                [
                    interpreter,
                    "-B",
                    "-c",
                    "import graphify, os; print(os.path.dirname(graphify.__file__))",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if located.returncode == 0 and located.stdout.strip():
                candidate = located.stdout.strip()
                if os.path.isdir(candidate):
                    return [candidate]
        except (OSError, subprocess.SubprocessError):
            pass

    dirs = []
    # 2. Fall back to this interpreter, then to a filesystem sweep.
    try:
        previous_dont_write_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            import graphify
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
        dirs.append(os.path.dirname(graphify.__file__))
    except Exception:
        pass

    # 3. Search common uv / virtualenv locations (Linux/macOS)
    user_home = os.path.expanduser("~")
    uv_paths = glob.glob(os.path.join(user_home, ".local/share/uv/tools/graphify*/lib/python*/site-packages/graphify"))
    dirs.extend(uv_paths)

    pip_paths = glob.glob(os.path.join(user_home, ".local/lib/python*/site-packages/graphify"))
    dirs.extend(pip_paths)

    # 4. Search common uv / pip user-install locations (Windows)
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        dirs.extend(glob.glob(os.path.join(appdata, "uv", "tools", "graphify*", "Lib", "site-packages", "graphify")))
        dirs.extend(glob.glob(os.path.join(appdata, "Python", "Python3*", "site-packages", "graphify")))
    if localappdata:
        dirs.extend(glob.glob(os.path.join(localappdata, "uv", "tools", "graphify*", "Lib", "site-packages", "graphify")))

    dirs = sorted(set(dirs))
    if len(dirs) > 1:
        print("Warning: could not resolve the active Graphify from PATH; "
              f"patching {len(dirs)} candidate installation(s)")
    return dirs


def _patched_detector(text: str) -> tuple[str | None, str]:
    """Register .apx beside .sql, failing closed on unrecognized handling.

    A bare ".apx" presence check is not proof of our patch: a future Graphify
    could mention the extension for its own reasons and we would report
    success without having registered anything.
    """
    declaration = CODE_EXTENSIONS_RE.search(text)
    if declaration is None:
        return None, "CODE_EXTENSIONS declaration with '.sql' not found"
    if DETECT_MARKER in declaration.group(0):
        return text, "already registered"
    if "'.apx'" in text or '".apx"' in text:
        return None, "unrecognized pre-existing .apx handling; refusing to patch"
    # Patch inside the declaration only: an earlier '.sql', in a comment or
    # another collection would otherwise take the marker and prove nothing.
    patched = declaration.group(0).replace("'.sql',", DETECT_MARKER, 1)
    return text[: declaration.start()] + patched + text[declaration.end():], "registered"


def _apx_registered(detect_text: str) -> bool:
    declaration = CODE_EXTENSIONS_RE.search(detect_text)
    return declaration is not None and DETECT_MARKER in declaration.group(0)


def _patched_dispatch(text: str) -> str | None:
    import_line = "from graphify.extractors.apexlang import extract_apexlang  # noqa: F401"
    if import_line not in text:
        sql_import = "from graphify.extractors.sql import extract_sql  # noqa: F401"
        if sql_import not in text:
            return None
        text = text.replace(sql_import, f"{sql_import}\n{import_line}", 1)

    if '".apx": extract_sql,' in text:
        text = text.replace('".apx": extract_sql,', '".apx": extract_apexlang,', 1)
    elif '".apx": extract_apexlang,' not in text:
        sql_route = '".sql": extract_sql,'
        if sql_route not in text:
            return None
        text = text.replace(
            sql_route,
            f'{sql_route}\n    ".apx": extract_apexlang,',
            1,
        )

    # The project-owned APEXlang parser is standard-library-only. A legacy
    # setup mapped .apx to the optional SQL dependency; remove that false gate.
    text = text.replace('    ".apx": "sql",\n', "")

    # Route .sql through the wrapper that links foreign keys to mirrored tables.
    # The optional-dependency gate for .sql stays: the wrapper still calls the
    # tree-sitter based SQL extractor.
    if SQL_LINK_IMPORT not in text:
        if import_line not in text:
            return None
        text = text.replace(import_line, f"{import_line}\n{SQL_LINK_IMPORT}", 1)
    if '".sql": extract_sql,' in text:
        text = text.replace('".sql": extract_sql,', '".sql": extract_sql_linked,', 1)
    elif '".sql": extract_sql_linked,' not in text:
        return None
    return text


def _smoke_test_extractor(extractor_path: Path) -> tuple[bool, str]:
    smoke_root = Path(tempfile.mkdtemp(prefix="graphify-apexlang-smoke."))
    module_name = f"graphify_apexlang_smoke_{os.getpid()}_{id(extractor_path)}"
    module_was_present = module_name in sys.modules
    previous_module = sys.modules.get(module_name)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    try:
        fixture = smoke_root / "apps" / "DEMO" / "102" / "pages" / "p00004-home.apx"
        fixture.parent.mkdir(parents=True)
        fixture.write_text(
            "page 4 (\n"
            "    name: Home\n"
            "    region orders (\n"
            "        source {\n"
            "            sqlQuery: select id from sample_restaurant_orders\n"
            "        }\n"
            "    )\n"
            ")\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location(module_name, extractor_path)
        if spec is None or spec.loader is None:
            return False, "could not create an import specification"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
        if not callable(getattr(module, "extract_sql_linked", None)):
            return False, "extractor does not define extract_sql_linked"
        result = module.extract_apexlang(fixture)
        if result.get("error"):
            return False, f"smoke extraction failed: {result['error']}"
        relations = {edge.get("relation") for edge in result.get("edges", [])}
        if not {"contains", "reads_from"}.issubset(relations):
            return False, "smoke extraction did not emit containment and database edges"
        return True, "ok"
    except Exception as exc:
        return False, f"smoke extraction raised {type(exc).__name__}: {exc}"
    finally:
        if module_was_present:
            sys.modules[module_name] = previous_module
        else:
            sys.modules.pop(module_name, None)
        sys.dont_write_bytecode = previous_dont_write_bytecode
        shutil.rmtree(smoke_root, ignore_errors=True)


def verify_installation(base: Path) -> tuple[bool, str]:
    installed = base / "extractors" / "apexlang.py"
    detect_path = base / "detect.py"
    extract_path = base / "extract.py"
    if not installed.is_file():
        return False, "installed extractor is missing"
    if installed.read_bytes() != CANONICAL_EXTRACTOR.read_bytes():
        return False, "installed extractor differs from canonical source"
    detect = detect_path.read_text(encoding="utf-8")
    extract = extract_path.read_text(encoding="utf-8")
    if not _apx_registered(detect):
        return False, ".apx is not registered beside .sql as a code extension"
    if "from graphify.extractors.apexlang import extract_apexlang" not in extract:
        return False, "APEXlang extractor import is missing"
    if '".apx": extract_apexlang,' not in extract:
        return False, ".apx is not routed to extract_apexlang"
    if '".apx": extract_sql,' in extract:
        return False, "legacy .apx SQL route is still present"
    if SQL_LINK_IMPORT not in extract or '".sql": extract_sql_linked,' not in extract:
        return False, ".sql is not routed through extract_sql_linked"
    return _smoke_test_extractor(installed)


def invalidate_apx_cache(cache_root: Path) -> int:
    """Remove AST cache records produced from .apx and .sql source files.

    The cache is keyed by file content alone. An .apx result names the database
    objects it reads and a .sql result names its foreign-key parents, so results
    cached before the extractor (or the database mirror) changed keep stale
    stubs until their files happen to change.
    """
    if not cache_root.is_dir():
        return 0
    removed = 0
    for cache_file in cache_root.rglob("*.json"):
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        source_files = {
            str(node.get("source_file", ""))
            for node in payload.get("nodes", [])
            if isinstance(node, dict)
        }
        if any(source.casefold().endswith((".apx", ".sql")) for source in source_files):
            try:
                cache_file.unlink()
                removed += 1
            except OSError as exc:
                print(f"Warning: could not invalidate APEXlang cache '{cache_file}': {exc}")
    return removed

def patch_graphify_dir(base: Path) -> bool:
    """Install APEXlang support into one Graphify package, failing closed."""
    detect_path = base / "detect.py"
    extract_path = base / "extract.py"
    extractor_dir = base / "extractors"
    installed_path = extractor_dir / "apexlang.py"
    missing = [
        path
        for path in (CANONICAL_EXTRACTOR, detect_path, extract_path, extractor_dir)
        if not (path.is_dir() if path == extractor_dir else path.is_file())
    ]
    if missing:
        print(f"Warning: Graphify at '{base}' is missing required module(s): "
              + ", ".join(path.name for path in missing))
        return False

    detect_bytes = detect_path.read_bytes()
    extract_bytes = extract_path.read_bytes()
    old_installed = installed_path.read_bytes() if installed_path.exists() else None
    try:
        patched_detect, detect_reason = _patched_detector(detect_bytes.decode("utf-8"))
        patched_extract = _patched_dispatch(extract_bytes.decode("utf-8"))
    except UnicodeError as exc:
        print(f"Warning: could not decode Graphify package at '{base}': {exc}")
        return False
    if patched_detect is None:
        print(f"Warning: could not patch {detect_path}; {detect_reason}")
        return False
    if patched_extract is None:
        print(f"Warning: could not patch {extract_path}; extractor routing anchors not found")
        return False

    try:
        detect_path.write_text(patched_detect, encoding="utf-8", newline="")
        extract_path.write_text(patched_extract, encoding="utf-8", newline="")
        shutil.copyfile(CANONICAL_EXTRACTOR, installed_path)
        verified, reason = verify_installation(base)
        if not verified:
            raise RuntimeError(reason)
    except Exception as exc:
        try:
            detect_path.write_bytes(detect_bytes)
            extract_path.write_bytes(extract_bytes)
            if old_installed is None:
                installed_path.unlink(missing_ok=True)
            else:
                installed_path.write_bytes(old_installed)
        except OSError as rollback_exc:
            print(f"Warning: rollback failed for Graphify at '{base}': {rollback_exc}")
        print(f"Warning: Graphify APEXlang setup failed at '{base}': {exc}")
        return False

    print(f"Graphify at '{base}' is configured with the project APEXlang extractor")
    return True


def setup_graphify_apx() -> bool:
    print("Checking Graphify & tree-sitter-sql setup...")

    # Best-effort: the supported path is `uv tool install graphifyy --with
    # tree-sitter-sql`. Report what happened rather than discarding it -- a
    # silent failure here shows up much later as an unindexable database/ tree.
    py_path = graphify_console_interpreter()
    if py_path:
        installed = False
        for command in (
            [py_path, "-m", "pip", "install", "tree-sitter-sql"],
            ["uv", "pip", "install", "--python", py_path, "tree-sitter-sql"],
        ):
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=300
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode == 0:
                installed = True
                break
        if not installed:
            print("Note: could not install tree-sitter-sql into Graphify's interpreter.\n"
                  "      Without it, database/ and supporting-objects/*.sql cannot be indexed.\n"
                  "      Install it with Graphify instead:\n"
                  "        uv tool install graphifyy --with tree-sitter-sql --force")

    g_dirs = find_graphify_dirs()
    if not g_dirs:
        print("Warning: Graphify installation not found. Install it first:\n"
              "  uv tool install graphifyy --with tree-sitter-sql")
        return False

    results = {base: patch_graphify_dir(Path(base)) for base in g_dirs}
    for base, patched in results.items():
        if not patched:
            print(f"Warning: Graphify at '{base}' was not configured")
    if not any(results.values()):
        return False
    # A cache invalidation skipped because some *other* installation failed is
    # how the graph ends up with cached .apx results from the former SQL route:
    # zero architectural relationships, no error. Invalidate whenever any
    # installation was actually patched.
    removed = invalidate_apx_cache(REPO_ROOT / "graphify-out" / "cache" / "ast")
    if removed:
        print(f"Invalidated {removed} stale APEXlang AST cache entr{'y' if removed == 1 else 'ies'}")
    return all(results.values())


def main(argv: list[str]) -> int:
    """Entry point. `--verify` checks the installation without changing it."""
    if "--verify" in argv:
        bases = find_graphify_dirs()
        if not bases:
            print("Graphify installation not found")
            return 1
        failed = False
        for base in bases:
            try:
                verified, reason = verify_installation(Path(base))
            except Exception as exc:
                verified = False
                reason = f"verification raised {type(exc).__name__}: {exc}"
            print(f"{'OK  ' if verified else 'FAIL'} {base}: {reason}")
            failed = failed or not verified
        return 1 if failed else 0
    return 0 if setup_graphify_apx() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
