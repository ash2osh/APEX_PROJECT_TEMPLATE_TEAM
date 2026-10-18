#!/usr/bin/env python3
"""Install optional, pinned workflow skills in this repository only (stdlib + Git)."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from .upgrade_template import UpgradeError, safe_project_path
except ImportError:
    from upgrade_template import UpgradeError, safe_project_path

CONFIG = ".agent-skills.json"
LOCK = ".agent-skills.lock.json"
SELECTIONS = {"none": (), "superpowers": ("superpowers",), "mattpocock": ("mattpocock",),
              "both": ("superpowers", "mattpocock")}
CORE_SKILLS = {"apex-background", "apex-session-context", "apex-workflow-lifecycle", "apexlang-export-debugging",
               "building-apex-template-components", "chrome-devtools-mcp", "initialize-project", "optimizing-apex-task-inboxes",
               "safeguarding-apexlang-text-messages", "sqlcl-mcp-r0", "sqlcl-script-path-debugging"}
NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class SkillsError(ValueError):
    pass


def safe(root: Path, relative: str) -> Path:
    try:
        return safe_project_path(root, relative)
    except UpgradeError as exc:
        raise SkillsError(str(exc)) from exc


def read_json(root: Path, relative: str, default):
    path = safe(root, relative)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillsError(f"invalid {relative}: {exc}") from exc


def configuration(root: Path) -> dict:
    config = read_json(root, CONFIG, {"schemaVersion": 1, "selection": "none", "primary": "none"})
    if not isinstance(config, dict) or set(config) != {"schemaVersion", "selection", "primary"} or type(config["schemaVersion"]) is not int or config["schemaVersion"] != 1:
        raise SkillsError(f"invalid {CONFIG} schema")
    selected = config["selection"]
    primary = config["primary"]
    if not isinstance(selected, str) or selected not in SELECTIONS or not isinstance(primary, str):
        raise SkillsError(f"invalid {CONFIG} selection")
    if primary not in (SELECTIONS[selected] or ("none",)):
        raise SkillsError(f"invalid {CONFIG} primary workflow")
    return config


def skill_name(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in CORE_SKILLS:
        raise SkillsError("invalid or reserved optional skill name")
    return name


def installation(root: Path) -> dict:
    lock = read_json(root, LOCK, {"schemaVersion": 1, "packs": {}, "directories": {}})
    if not isinstance(lock, dict) or set(lock) != {"schemaVersion", "packs", "directories"} or type(lock["schemaVersion"]) is not int or lock["schemaVersion"] != 1:
        raise SkillsError(f"invalid {LOCK} schema")
    if not isinstance(lock["packs"], dict) or not isinstance(lock["directories"], dict):
        raise SkillsError(f"invalid {LOCK} inventory")
    expected = set()
    for pack, record in lock["packs"].items():
        if pack not in ("superpowers", "mattpocock") or not isinstance(record, dict):
            raise SkillsError(f"invalid {LOCK} pack")
        if not isinstance(record.get("commit"), str) or not SHA.fullmatch(record["commit"]) or not isinstance(record.get("repository"), str):
            raise SkillsError(f"invalid {LOCK} provenance")
        names = record.get("skills")
        if not isinstance(names, list) or not names:
            raise SkillsError(f"invalid {LOCK} skills")
        for name in names:
            skill_name(name)
            for agent in (".agents", ".claude"):
                relative = f"{agent}/skills/{name}"
                if relative in expected:
                    raise SkillsError(f"duplicate skill in {LOCK}")
                expected.add(relative)
    if set(lock["directories"]) != expected:
        raise SkillsError(f"invalid {LOCK} directory paths")
    for relative, files in lock["directories"].items():
        safe(root, relative)
        if not isinstance(files, dict) or "SKILL.md" not in files:
            raise SkillsError(f"invalid {LOCK} file inventory")
        for name, digest in files.items():
            safe(root, f"{relative}/{name}")
            if not isinstance(digest, str) or not HASH.fullmatch(digest):
                raise SkillsError(f"invalid {LOCK} file hash")
    return lock


def tree_hashes(folder: Path) -> dict:
    if not folder.is_dir():
        raise SkillsError(f"missing installed skill: {folder.name}")
    result = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        safe(folder, relative)
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def status(root: Path) -> dict:
    config = configuration(root)
    lock = installation(root)
    issues = []
    if safe(root, ".agent-skills/setup-lock").exists():
        issues.append("setup is running or was interrupted; inspect .agent-skills/ before recovery")
    if set(lock["packs"]) != set(SELECTIONS[config["selection"]]):
        issues.append("selection does not match installed packs; run skills setup with the desired selection")
    for relative, files in lock["directories"].items():
        try:
            if tree_hashes(safe(root, relative)) != files:
                issues.append(f"modified installed skill: {relative}")
        except SkillsError as exc:
            issues.append(str(exc))
    return {**config, "packs": lock["packs"], "issues": issues,
            "scope": "Repository-managed packs only; global skills and plugins are not inspected or disabled."}


def run_git(root: Path, *args: str, binary=False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        # Do not echo remote URLs or full Git logs: local Git configuration may contain credentials.
        raise SkillsError("Git operation failed; verify Git, repository access and the pinned commit")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def fetch_pack(stage: Path, pack: str, spec: dict) -> dict[str, Path]:
    """Fetch an exact commit and copy regular skill files; never execute upstream code."""
    commit = spec.get("commit")
    repository = spec.get("repository")
    skills = spec.get("skills")
    if not isinstance(commit, str) or not SHA.fullmatch(commit) or not isinstance(repository, str) or not repository or repository.startswith("-"):
        raise SkillsError(f"invalid catalog provenance: {pack}")
    if not isinstance(skills, dict) or not skills:
        raise SkillsError(f"invalid catalog skills: {pack}")
    repo = stage / f"git-{pack}"
    repo.mkdir()
    empty_hooks = stage / "empty-hooks"
    empty_hooks.mkdir(exist_ok=True)
    options = ("-c", f"core.hooksPath={empty_hooks}", "-c", "core.autocrlf=false")
    run_git(repo, *options, "init", "--bare", f"--template={empty_hooks}", "-q")
    run_git(repo, *options, "fetch", "--quiet", "--depth=1", "--no-tags", "--", repository, commit)
    if run_git(repo, "rev-parse", "FETCH_HEAD") != commit:
        raise SkillsError(f"pinned commit mismatch: {pack}")
    paths = []
    for name, source in skills.items():
        skill_name(name)
        if not isinstance(source, str) or not source.startswith("skills/"):
            raise SkillsError(f"invalid source path: {pack}")
        safe(stage, source)
        paths.append(source)
    archive = run_git(repo, "archive", "--format=tar", commit, "--", "LICENSE", *paths, binary=True)
    extracted = stage / f"source-{pack}"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            relative = member.name.rstrip("/")
            destination = safe(extracted, relative)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as stream:
                    destination.write_bytes(stream.read())
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise SkillsError(f"upstream skill contains a link or special file: {pack}")
    result = {}
    license_path = safe(extracted, "LICENSE")
    if not license_path.is_file():
        raise SkillsError(f"missing upstream license: {pack}")
    for name, source in skills.items():
        folder = safe(extracted, source)
        if not (folder / "SKILL.md").is_file():
            raise SkillsError(f"missing upstream SKILL.md: {pack}/{name}")
        if (folder / "UPSTREAM-LICENSE").exists():
            raise SkillsError(f"upstream license filename collision: {pack}/{name}")
        shutil.copyfile(license_path, folder / "UPSTREAM-LICENSE")
        result[name] = folder
    return result


def require_untracked(root: Path, directories) -> None:
    for relative in directories:
        if run_git(root, "ls-files", "--", relative):
            raise SkillsError(f"tracked skills cannot be changed by setup: {relative}; upgrade the template first")


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def remove_tree(path: Path) -> None:
    """Remove owned trees, allowing Windows to unlink Git's read-only object files."""
    def retry_readonly(function, filename, excinfo):
        error = excinfo[1]
        if not isinstance(error, PermissionError):
            raise error
        info = os.lstat(filename)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & stat.S_IWRITE:
            raise error
        os.chmod(filename, info.st_mode | stat.S_IWRITE)
        function(filename)

    shutil.rmtree(path, onerror=retry_readonly)


def setup(root: Path, selection: str, *, primary: str | None = None, catalog: dict | None = None) -> dict:
    root = root.resolve()
    if selection not in SELECTIONS:
        raise SkillsError("unknown pack selection")
    selected = SELECTIONS[selection]
    primary = primary or (selected[0] if selected else "none")
    if primary not in (selected or ("none",)):
        raise SkillsError("primary workflow must be one of the selected packs")
    config = {"schemaVersion": 1, "selection": selection, "primary": primary}
    cache = safe(root, ".agent-skills")
    cache.mkdir(exist_ok=True)
    mutex = safe(root, ".agent-skills/setup-lock")
    try:
        mutex.mkdir()
    except FileExistsError as exc:
        raise SkillsError("setup is running or was interrupted; inspect .agent-skills/ before recovery") from exc
    stage = None
    preserve_stage = False
    try:
        stage = Path(tempfile.mkdtemp(prefix="setup-", dir=cache))
        # Read the inventory only after acquiring the local setup lock.
        configuration(root)  # Never silently repair malformed project configuration.
        old = installation(root)
        for relative, hashes in old["directories"].items():
            if tree_hashes(safe(root, relative)) != hashes:
                raise SkillsError(f"modified installed skill: {relative}; preserve/reconcile edits before setup")
        require_untracked(root, old["directories"])
        if catalog is None and selected:
            catalog = read_json(root, "tools/workflow-skills/catalog.json", None)
        if selected and not isinstance(catalog, dict):
            raise SkillsError("workflow skill catalog is missing or invalid")
        folders = {}
        packs = {}
        for pack in selected:
            if pack not in catalog:
                raise SkillsError(f"missing catalog pack: {pack}")
            spec = catalog[pack]
            if not isinstance(spec, dict):
                raise SkillsError(f"invalid catalog pack: {pack}")
            for name, folder in fetch_pack(stage, pack, spec).items():
                if name in folders:
                    raise SkillsError(f"pack skill name collision: {name}")
                folders[name] = folder
            packs[pack] = {"repository": spec["repository"], "commit": spec["commit"], "skills": sorted(spec["skills"])}
        new = {"schemaVersion": 1, "packs": packs, "directories": {}}
        for name, folder in folders.items():
            for agent in (".agents", ".claude"):
                relative = f"{agent}/skills/{name}"
                path = safe(root, relative)
                if path.exists() and relative not in old["directories"]:
                    raise SkillsError(f"unmanaged skill would be overwritten: {relative}")
                new["directories"][relative] = tree_hashes(folder)
        require_untracked(root, new["directories"])
        # Prepare everything before touching discovery directories or the selection.
        staged = stage / "install"
        for relative in new["directories"]:
            shutil.copytree(folders[PurePosixPath(relative).name], staged / relative)
        write_json(stage / CONFIG, config)
        write_json(stage / LOCK, new)
        moved, installed = [], []
        saved = {name: safe(root, name).read_bytes() if safe(root, name).exists() else None for name in (CONFIG, LOCK)}
        try:
            # Recheck edits made while the download was in progress.
            for relative, hashes in old["directories"].items():
                if tree_hashes(safe(root, relative)) != hashes:
                    raise SkillsError(f"modified installed skill: {relative}")
            require_untracked(root, set(old["directories"]) | set(new["directories"]))
            for relative in old["directories"]:
                backup = stage / "backup" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                safe(root, relative).rename(backup)
                moved.append(relative)
            for relative in new["directories"]:
                target = safe(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise SkillsError(f"unmanaged skill appeared during setup: {relative}")
                (staged / relative).rename(target)
                installed.append(relative)
            for name in (CONFIG, LOCK):
                os.replace(stage / name, safe(root, name))
        except BaseException:
            try:
                for relative in reversed(installed):
                    remove_tree(safe(root, relative))
                for relative in reversed(moved):
                    (stage / "backup" / relative).rename(safe(root, relative))
                for name, contents in saved.items():
                    path = safe(root, name)
                    if contents is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(contents)
            except BaseException as exc:
                preserve_stage = True
                raise SkillsError(f"setup recovery incomplete; preserve {stage} and inspect docs/workflow-skills.md") from exc
            raise
    finally:
        if not preserve_stage:
            if stage is not None:
                remove_tree(stage)
            mutex.rmdir()
    return status(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("status", help="inspect local selection and installation without network access")
    inspect.add_argument("--format", choices=("text", "json"), default="text")
    install = commands.add_parser("setup", help="install selected pinned packs, or remove managed packs with none")
    install.add_argument("selection", choices=tuple(SELECTIONS))
    install.add_argument("--primary", choices=("superpowers", "mattpocock"))
    args = parser.parse_args(argv)
    try:
        result = status(args.repo_root) if args.command == "status" else setup(args.repo_root, args.selection, primary=args.primary)
        if getattr(args, "format", "text") == "json":
            print(json.dumps(result, indent=2))
        else:
            print(f"Workflow packs: {result['selection']}; primary: {result['primary']}")
            for name, pack in result["packs"].items():
                print(f"  {name}: {pack['commit']} ({len(pack['skills'])} skills in each agent directory)")
            for issue in result["issues"]:
                print(f"  ATTENTION: {issue}")
            print(result["scope"])
            if args.command == "setup" and "mattpocock" in result["packs"]:
                print("Next: run /setup-matt-pocock-skills; place project guidance in AGENTS.project.md (see docs/workflow-skills.md).")
        return 2 if result["issues"] else 0
    except (SkillsError, OSError, tarfile.TarError) as exc:
        print(f"skills error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
