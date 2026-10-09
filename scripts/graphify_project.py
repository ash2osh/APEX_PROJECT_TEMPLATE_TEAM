#!/usr/bin/env python3
"""Manage project-isolated optional Graphify environment and operations.

Provides isolated setup, read-only verification, and extraction within a dedicated
.venv-graphify environment, decoupling project tooling from global tools or other
repositories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Prevent bytecode caching
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILE = REPO_ROOT / "tools" / "graphify" / "requirements.txt"

try:
    from scripts.setup_graphify_apx import (
        CANONICAL_EXTRACTOR,
        invalidate_apx_cache,
        patch_graphify_dir,
        verify_installation,
    )
except ImportError:
    from setup_graphify_apx import (
        CANONICAL_EXTRACTOR,
        invalidate_apx_cache,
        patch_graphify_dir,
        verify_installation,
    )


def project_graphify_environment(repo_root: Path) -> Path:
    """Return the canonical project-isolated Graphify virtualenv path."""
    return repo_root / ".venv-graphify"


def environment_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def environment_graphify(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "graphify.exe"
    return env_dir / "bin" / "graphify"


def find_graphify_package_dir(env_dir: Path) -> Path | None:
    for pattern in ("lib/python*/site-packages/graphify", "Lib/site-packages/graphify"):
        matches = sorted(env_dir.glob(pattern))
        for m in matches:
            if m.is_dir() and (m / "__init__.py").is_file():
                return m
    return None


def scan_eligible_domain_files(repo_root: Path) -> set[str]:
    """Scan repo_root for eligible domain files according to .graphifyignore rules."""
    eligible: set[str] = set()

    def is_ignored(rel_str: str) -> bool:
        parts = rel_str.split("/")
        filename = parts[-1]
        if filename.startswith(".") and filename != ".gitkeep":
            return True
        if filename in (".gitkeep", "README.md", "manifest-code.txt", "manifest-tables.txt", "export.log", "export_err.log"):
            return True
        if ".apex" in parts or "deployments" in parts or "workspace-components" in parts:
            return True
        if filename == "static-files.apx" or "static-files" in parts:
            return True
        return False

    apps_dir = repo_root / "apps"
    if apps_dir.is_dir():
        for f in apps_dir.rglob("*.apx"):
            if f.is_file():
                rel = f.relative_to(repo_root).as_posix()
                if not is_ignored(rel):
                    eligible.add(rel)

    db_dir = repo_root / "database"
    if db_dir.is_dir():
        for f in db_dir.rglob("*.sql"):
            if f.is_file():
                rel = f.relative_to(repo_root).as_posix()
                if not is_ignored(rel):
                    eligible.add(rel)

    ctx_dir = repo_root / "app_context"
    if ctx_dir.is_dir():
        for f in ctx_dir.rglob("*.md"):
            if f.is_file():
                rel = f.relative_to(repo_root).as_posix()
                if not is_ignored(rel):
                    eligible.add(rel)

    return eligible


def get_installed_dist_version(sp_dir: Path, pkg_name: str) -> str | None:
    """Read version from .dist-info/METADATA without importing or executing."""
    norm_name = pkg_name.lower().replace("-", "_")
    for dist_dir in sp_dir.glob("*.dist-info"):
        if not dist_dir.is_dir():
            continue
        d_name = dist_dir.name.split("-")[0].lower().replace("-", "_")
        if d_name == norm_name:
            metadata_file = dist_dir / "METADATA"
            if metadata_file.is_file():
                try:
                    for line in metadata_file.read_text(encoding="utf-8").splitlines():
                        if line.startswith("Version:"):
                            return line.split(":", 1)[1].strip()
                except OSError:
                    pass
    return None


def inspect_graphify(repo_root: Path, target_env: Path | None = None) -> dict:
    """Inspect Graphify environment status and return status plus actionable diagnostics.

    Status is one of:
      - 'absent': environment directory not present (informational)
      - 'incompatible': environment or package structure unpatchable/invalid (attention)
      - 'stale': unpatched, outdated extractor, stale cache, or incomplete coverage (attention)
      - 'current': verified and ready for extraction
    """
    env_dir = target_env if target_env is not None else project_graphify_environment(repo_root)
    reasons: list[str] = []

    # 1. Symlink / escape validation
    if env_dir.is_symlink():
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": None,
            "reasons": [f"Graphify environment '{env_dir}' is a symbolic link, which is not supported for security isolation."],
            "is_configured": False,
            "is_package_ready": False,
            "is_graph_current": False,
        }
    try:
        env_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        if target_env is None:
            return {
                "status": "incompatible",
                "environment": env_dir,
                "package_path": None,
                "reasons": [f"Graphify environment '{env_dir}' resolves outside repository root '{repo_root}'."],
                "is_configured": False,
                "is_package_ready": False,
                "is_graph_current": False,
            }

    # 2. Presence check
    if not env_dir.is_dir():
        existing_graph = (repo_root / 'graphify-out').exists()
        return {
            "status": "stale" if existing_graph else "absent",
            "environment": env_dir,
            "package_path": None,
            "reasons": [f"Existing Graphify output needs a verified local environment at {env_dir}." if existing_graph else f"Optional Graphify environment is absent at {env_dir}."],
            "is_configured": existing_graph,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # Environment directory exists => configured

    # Check python interpreter launcher
    py_bin = environment_python(env_dir)
    if not py_bin.is_file():
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": None,
            "reasons": [f"Python interpreter launcher is missing in configured environment at '{py_bin}'."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # Check graphify launcher
    graphify_bin = environment_graphify(env_dir)
    if not graphify_bin.is_file():
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": None,
            "reasons": [f"Graphify CLI launcher is missing in configured environment at '{graphify_bin}'."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # Check pyvenv.cfg for Python >= 3.12 prerequisite
    cfg_file = env_dir / "pyvenv.cfg"
    if cfg_file.is_file():
        try:
            for line in cfg_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("version =") or line.startswith("version_info ="):
                    ver_part = line.split("=", 1)[1].strip()
                    m = re.match(r"^(\d+)\.(\d+)", ver_part)
                    if m:
                        major, minor = int(m.group(1)), int(m.group(2))
                        if (major, minor) < (3, 12):
                            return {
                                "status": "incompatible",
                                "environment": env_dir,
                                "package_path": None,
                                "reasons": [f"Python version {ver_part} in pyvenv.cfg does not satisfy Python >= 3.12 prerequisite for Graphify."],
                                "is_configured": True,
                                "is_package_ready": False,
                                "is_graph_current": False,
                            }
        except OSError:
            pass

    pkg_dir = find_graphify_package_dir(env_dir)
    if pkg_dir is None:
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": None,
            "reasons": [f"Graphify package is not installed in environment at {env_dir}."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # 3. Required package files check
    for req_file in ("detect.py", "extract.py"):
        if not (pkg_dir / req_file).is_file():
            return {
                "status": "incompatible",
                "environment": env_dir,
                "package_path": pkg_dir,
                "reasons": [f"Graphify installation at {pkg_dir} is missing required file '{req_file}'."],
                "is_configured": True,
                "is_package_ready": False,
                "is_graph_current": False,
            }
    if not (pkg_dir / "extractors").is_dir():
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": [f"Graphify installation at {pkg_dir} is missing 'extractors' directory."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # 4. Check package metadata & pinned dependencies read-only
    sp_dir = pkg_dir.parent
    g_ver = get_installed_dist_version(sp_dir, "graphifyy")
    if not g_ver:
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": ["Graphify package metadata (graphifyy-*.dist-info) is missing in environment."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }
    if g_ver != "0.9.75":
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": [f"Graphify package version {g_ver} does not match required pinned version 0.9.75."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    ts_ver = get_installed_dist_version(sp_dir, "tree_sitter_sql") or get_installed_dist_version(sp_dir, "tree-sitter-sql")
    if not ts_ver:
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": ["Required dependency 'tree-sitter-sql' metadata is missing from environment."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }
    if ts_ver != "0.3.11":
        return {
            "status": "incompatible",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": [f"tree-sitter-sql dependency version {ts_ver} does not match required pinned version 0.3.11."],
            "is_configured": True,
            "is_package_ready": False,
            "is_graph_current": False,
        }

    # 5. Installation verification (detector registered, dispatch routed, canonical extractor installed)
    # Read-only static check, run_smoke=False
    try:
        verified, verify_reason = verify_installation(pkg_dir, run_smoke=False)
    except Exception as exc:
        verified = False
        verify_reason = f"verification error: {exc}"

    if not verified:
        reasons.append(f"Graphify APEXlang integration is stale or incomplete: {verify_reason}")
        is_package_ready = False
    else:
        is_package_ready = True

    # 6. AST Cache Staleness & fail-closed Check
    ast_cache_dir = repo_root / "graphify-out" / "cache" / "ast"
    stale_cache_count = 0
    if ast_cache_dir.is_dir():
        canonical_mtime = CANONICAL_EXTRACTOR.stat().st_mtime if CANONICAL_EXTRACTOR.is_file() else 0
        db_mirror_dir = repo_root / "database"
        latest_db_mtime = 0
        if db_mirror_dir.is_dir():
            for sql_file in db_mirror_dir.rglob("*.sql"):
                try:
                    latest_db_mtime = max(latest_db_mtime, sql_file.stat().st_mtime)
                except OSError:
                    pass

        for cache_file in ast_cache_dir.rglob("*.json"):
            try:
                c_mtime = cache_file.stat().st_mtime
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                sources = [str(node.get("source_file", "")) for node in payload.get("nodes", []) if isinstance(node, dict)]
                if (canonical_mtime > 0 and c_mtime < canonical_mtime) or (latest_db_mtime > 0 and c_mtime < latest_db_mtime):
                    if any(s.casefold().endswith((".apx", ".sql")) for s in sources):
                        stale_cache_count += 1
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                reasons.append(f"AST cache entry at '{cache_file}' is unreadable or malformed JSON: {exc}")

    if stale_cache_count > 0:
        reasons.append(f"Stale APEXlang AST cache entries detected ({stale_cache_count} file(s)).")

    # 7. Domain coverage & graph schema check
    eligible_files = scan_eligible_domain_files(repo_root)
    graph_path = repo_root / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        if eligible_files:
            reasons.append("Graph at graphify-out/graph.json is missing.")
    else:
        try:
            graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
            if not isinstance(graph_data, dict) or not isinstance(graph_data.get("nodes"), list):
                reasons.append("Graph at graphify-out/graph.json has invalid schema: missing nodes list.")
            else:
                represented_sources = set()
                schema_valid = True
                for node in graph_data["nodes"]:
                    if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not isinstance(node.get("source_file"), str):
                        schema_valid = False
                        break
                    src = node["source_file"]
                    if src:
                        if not any(src.startswith(prefix) for prefix in ("apps/", "database/", "app_context/")):
                            reasons.append(f"Graph contains source outside domain allowlist: {src}")
                            break
                        represented_sources.add(src)
                if not schema_valid:
                    reasons.append("Graph at graphify-out/graph.json has invalid node schema (nodes must have 'id' and 'source_file').")
                else:
                    missing_eligible = sorted(eligible_files - represented_sources)
                    if missing_eligible:
                        reasons.append(f"Eligible domain files missing from graph ({len(missing_eligible)} file(s), e.g. '{missing_eligible[0]}').")
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            reasons.append(f"Graph at graphify-out/graph.json is unreadable or malformed JSON: {exc}")


    if reasons:
        return {
            "status": "stale",
            "environment": env_dir,
            "package_path": pkg_dir,
            "reasons": reasons,
            "is_configured": True,
            "is_package_ready": is_package_ready,
            "is_graph_current": False,
        }

    return {
        "status": "current",
        "environment": env_dir,
        "package_path": pkg_dir,
        "reasons": [],
        "is_configured": True,
        "is_package_ready": True,
        "is_graph_current": True,
    }


def setup_graphify(repo_root: Path, target_env: Path | None = None) -> int:
    """Explicitly create and configure the project-isolated Graphify virtual environment."""
    env_dir = target_env if target_env is not None else project_graphify_environment(repo_root)

    if env_dir.absolute() != project_graphify_environment(repo_root).absolute():
        print('Error: setup requires the canonical project .venv-graphify.', file=sys.stderr)
        return 1
    # Security check: must not escape or be symlink
    if env_dir.is_symlink():
        print(f"Error: Refusing to setup Graphify: environment path '{env_dir}' is a symbolic link.", file=sys.stderr)
        return 1
    try:
        env_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        print(f"Error: Refusing to setup Graphify: environment path '{env_dir}' resolves outside repository root '{repo_root}'.", file=sys.stderr)
        return 1
    # Check before invoking an installer: linked package directories could send
    # pip/uv writes into another project's or a shared installation.
    if env_dir.is_dir():
        for path in env_dir.rglob('*'):
            if path.is_symlink() and path.is_dir() and not path.resolve().is_relative_to(env_dir.resolve()):
                print(f"Error: Graphify environment contains an external directory link at '{path}'.", file=sys.stderr)
                return 1

    req_file = REQUIREMENTS_FILE if REQUIREMENTS_FILE.is_file() else (repo_root / "tools" / "graphify" / "requirements.txt")
    if not req_file.is_file():
        print(f"Error: Requirements file not found at '{req_file}'.", file=sys.stderr)
        return 1

    uv_bin = shutil.which("uv")
    py_bin = environment_python(env_dir)

    if not env_dir.is_dir() or not py_bin.is_file():
        # Find Python 3.12+
        py312 = None
        if uv_bin:
            try:
                res = subprocess.run([uv_bin, "python", "find", "3.12"], capture_output=True, text=True, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    py312 = res.stdout.strip()
            except Exception:
                pass
        if not py312:
            if sys.version_info >= (3, 12):
                py312 = sys.executable
            else:
                py_which = shutil.which("python3.12")
                if py_which:
                    py312 = py_which

        if not py312:
            print("Error: Python >= 3.12 is required for Graphify (numpy 2.5.3 / networkx 3.7 prerequisite). "
                  "Install Python 3.12+ or configure uv.", file=sys.stderr)
            return 1

        print(f"Creating isolated Graphify environment at '{env_dir}' using {py312}...")
        if uv_bin:
            cmd = [uv_bin, "venv", str(env_dir), "--python", py312, "--link-mode", "copy"]
        else:
            cmd = [py312, "-m", "venv", str(env_dir)]

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            print(f"Error: Failed to create virtual environment: {res.stderr.strip()}", file=sys.stderr)
            return 1

        py_bin = environment_python(env_dir)

    print(f"Installing pinned requirements from '{req_file}'...")
    if uv_bin:
        install_cmd = [uv_bin, "pip", "install", "--python", str(py_bin), "-r", str(req_file), "--link-mode", "copy"]
    else:
        install_cmd = [str(py_bin), "-m", "pip", "install", "-r", str(req_file)]

    install_res = subprocess.run(install_cmd, capture_output=True, text=True, check=False)
    if install_res.returncode != 0:
        print(f"Error: Failed to install requirements: {install_res.stderr.strip()}", file=sys.stderr)
        print("Note: Network/package failures are unavailable, not pass.", file=sys.stderr)
        return 1

    pkg_dir = find_graphify_package_dir(env_dir)
    if not pkg_dir:
        print(f"Error: Could not locate installed Graphify package in '{env_dir}'.", file=sys.stderr)
        return 1

    print(f"Patching Graphify package at '{pkg_dir}'...")
    if not patch_graphify_dir(pkg_dir, repo_root=repo_root):
        print(f"Error: Failed to patch Graphify package at '{pkg_dir}'.", file=sys.stderr)
        return 1

    removed = invalidate_apx_cache(repo_root / "graphify-out" / "cache" / "ast")
    if removed:
        print(f"Invalidated {removed} stale AST cache entr{'y' if removed == 1 else 'ies'}.")

    verified, reason = verify_installation(pkg_dir)
    if not verified:
        print(f"Error: Verification of patched Graphify package failed: {reason}", file=sys.stderr)
        return 1

    print(f"OK: Graphify project environment configured at {env_dir}")
    return 0


def verify_graphify(repo_root: Path, target_env: Path | None = None, output_format: str = "text") -> int:
    """Read-only verification of Graphify environment."""
    result = inspect_graphify(repo_root, target_env)
    if output_format == "json":
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] in ("current", "absent") else 1

    if result["status"] == "current":
        print(f"OK: Graphify environment is current at {result['environment']}")
        return 0
    elif result["status"] == "absent":
        reason = result["reasons"][0] if result["reasons"] else "Environment not configured"
        print(f"INFO: Graphify optional tooling is not configured ({reason}).")
        return 0
    else:  # incompatible or stale
        print(f"ATTENTION: Graphify environment is {result['status']}:")
        for r in result["reasons"]:
            print(f"  - {r}")
        print("Run 'python3 scripts/graphify_project.py setup' to configure or repair.")
        return 1


def extract_graphify(repo_root: Path, extra_args: list[str], target_env: Path | None = None) -> int:
    """Execute Graphify extraction inside the isolated environment."""
    if target_env is not None:
        print("Error: --target is not permitted for extract. Extract always uses project-isolated .venv-graphify.", file=sys.stderr)
        return 1

    # Security check: do not permit output arguments escaping repo graphify-out
    output_path = repo_root / 'graphify-out'
    for idx, arg in enumerate(extra_args):
        if arg in ("--output", "-o", "--out") and idx + 1 < len(extra_args):
            out_val = Path(extra_args[idx + 1])
            if not out_val.is_absolute(): out_val = repo_root / out_val
            try:
                out_val.resolve().relative_to((repo_root / "graphify-out").resolve())
            except ValueError:
                print(f"Error: Custom extract output path '{out_val}' escapes project 'graphify-out'.", file=sys.stderr)
                return 1
            output_path = out_val
        elif arg.startswith(("--output=", "-o=", "--out=")):
            out_val = Path(arg.split("=", 1)[1])
            if not out_val.is_absolute(): out_val = repo_root / out_val
            try:
                out_val.resolve().relative_to((repo_root / "graphify-out").resolve())
            except ValueError:
                print(f"Error: Custom extract output path '{out_val}' escapes project 'graphify-out'.", file=sys.stderr)
                return 1
            output_path = out_val
    if output_path.resolve() != (repo_root / 'graphify-out').absolute():
        print('Error: Extraction requires the canonical project graphify-out directory.', file=sys.stderr)
        return 1
    if output_path.is_symlink():
        print('Error: Graphify output cannot be a symbolic link.', file=sys.stderr)
        return 1

    inspection = inspect_graphify(repo_root)
    if not inspection.get("is_package_ready", False):
        print(f"Error: Graphify project environment is not ready (status: {inspection['status']}).", file=sys.stderr)
        for r in inspection["reasons"]:
            print(f"  - {r}", file=sys.stderr)
        print("Run 'python3 scripts/graphify_project.py setup' before extracting.", file=sys.stderr)
        return 1

    env_dir = inspection["environment"]
    py_bin = environment_python(env_dir)
    graphify_bin = environment_graphify(env_dir)

    if graphify_bin.is_file():
        args = [str(graphify_bin), "extract", "."]
    else:
        args = [str(py_bin), "-m", "graphify", "extract", "."]

    if extra_args:
        args.extend(extra_args)

    print(f"Running Graphify extraction in '{repo_root}'...")
    res = subprocess.run(args, cwd=repo_root, check=False)
    if res.returncode != 0:
        return res.returncode

    graph_path = output_path / "graph.json"
    if not graph_path.is_file():
        print('Error: Graphify did not produce graphify-out/graph.json.', file=sys.stderr)
        return 1
    checker = REPO_ROOT / "scripts" / "check_graphify_apx.py"
    check_res = subprocess.run([str(py_bin), str(checker), "--root", str(repo_root), "--graph", str(graph_path)], cwd=repo_root, check=False)
    if check_res.returncode != 0:
        return check_res.returncode
    current = inspect_graphify(repo_root)
    if current['status'] != 'current':
        print('Error: Extracted graph did not pass local coverage/cache verification.', file=sys.stderr)
        for reason in current['reasons']: print('  - ' + reason, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root path")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--target", type=Path, default=None, help="Explicit target environment (verify only)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("setup", help="Configure or repair the project-isolated Graphify environment")
    verify_parser = subparsers.add_parser("verify", help="Check status of Graphify environment (read-only)")
    verify_parser.add_argument("--format", choices=["text", "json"], default=None, help="Output format")
    verify_parser.add_argument("--target", type=Path, default=None, help="Explicit target environment (verify only)")
    extract_parser = subparsers.add_parser("extract", help="Run extraction within project-isolated environment")
    extract_parser.add_argument("extract_args", nargs=argparse.REMAINDER, help="Arguments passed to graphify extract")

    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.command == "setup":
        if args.target is not None:
            print("Error: --target is not permitted for setup. Setup always uses project-isolated .venv-graphify.", file=sys.stderr)
            return 1
        return setup_graphify(root)
    elif args.command == "verify":
        fmt = getattr(args, "format", None) or "text"
        tgt = getattr(args, "target", None)
        return verify_graphify(root, target_env=tgt, output_format=fmt)
    elif args.command == "extract":
        if args.target is not None:
            print("Error: --target is not permitted for extract. Extract always uses project-isolated .venv-graphify.", file=sys.stderr)
            return 1
        extra = args.extract_args or []
        if extra and extra[0] == "--":
            extra = extra[1:]
        return extract_graphify(root, extra_args=extra, target_env=args.target)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
