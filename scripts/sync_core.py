#!/usr/bin/env python3
"""Keep the shared scripts/, tools/ and tests/ files of the APEX templates identical.

The core repository keeps one copy of each shared file under files/, listed with
its sha256 in core-manifest.json. Commands:

  seed   rebuild files/ and core-manifest.json from committed template refs
  sync   copy the manifest files into a template at the same paths and write
         .core-stamp.json (core commit id and per-file sha256)
  check  verify a template against its stamp, offline; --core also compares the
         stamp with core-manifest.json at the stamped commit
  diff   show what the next sync would change (writes no template files)

Exit codes: 0 ok; 1 drift found, or refused before any write; 2 usage or
internal error. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
MANIFEST_NAME = "core-manifest.json"
FILES_DIR = "files"
STAMP_NAME = ".core-stamp.json"
DEFAULT_PREFIXES = ("scripts/", "tools/", "tests/")
# Files the core owns. seed copies them from the core root, never from a template.
# Each entry is (source at the core root, shared path in files/ and in templates).
CORE_OWNED = (("sync_core.py", "scripts/sync_core.py"),)
REGULAR_MODES = frozenset({"100644", "100755"})
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FORBIDDEN_CHARS = frozenset('<>:"|?*\\\x00')
_GIT_ENV_DROP = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE"})
_STATUS_CHUNK = 200


class UsageError(Exception):
    """Bad arguments or unusable input. Exit 2."""


class Refused(Exception):
    """A safety rule stopped the command before anything was written. Exit 1."""


class UnsafePath(ValueError):
    """A path that could escape the template or cannot be stored on every platform."""


class StampError(Exception):
    """The template's .core-stamp.json is missing or not usable."""


# --- paths and git -----------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_relpath(rel: str) -> str:
    """Return rel when it is a plain relative path; raise UnsafePath otherwise."""
    if not rel or rel.startswith("/") or any(ch in _FORBIDDEN_CHARS for ch in rel):
        raise UnsafePath(rel)
    for part in rel.split("/"):
        if part in ("", ".", "..") or part.lower() == ".git" or part.endswith((" ", ".")):
            raise UnsafePath(rel)
    return rel


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROP}
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, check=False)
    except FileNotFoundError as exc:
        raise UsageError("git was not found on PATH") from exc


def git(repo: Path, *args: str) -> bytes:
    proc = _git_run(repo, *args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit status {proc.returncode}"
        raise UsageError(f"git {args[0]} failed in {repo}: {detail}")
    return proc.stdout


# --- template selection (seed) ----------------------------------------------


@dataclass(frozen=True)
class TemplateSpec:
    label: str
    path: Path
    ref: str


def parse_template_spec(text: str) -> TemplateSpec:
    label, sep, rest = text.partition("=")
    path_text, at, ref = rest.rpartition("@")
    if not sep or not at or not path_text or not ref:
        raise UsageError(f"--template expects LABEL=PATH@REF, got {text!r}")
    if not _LABEL_RE.match(label):
        raise UsageError(f"template label must match [A-Za-z0-9._-]+, got {label!r}")
    if ref.startswith("-"):
        raise UsageError(f"template ref must not start with '-': {ref!r}")
    return TemplateSpec(label, Path(path_text), ref)


def resolve_commit(repo: Path, ref: str) -> str:
    if _git_run(repo, "rev-parse", "--git-dir").returncode != 0:
        raise UsageError(f"{repo} is not a git repository")
    proc = _git_run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise UsageError(f"ref {ref!r} does not resolve to a commit in {repo}")
    return proc.stdout.decode("ascii").strip()


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    oid: str


def list_tree(repo: Path, commit: str) -> dict[str, TreeEntry]:
    """Every path in the commit's tree (committed content only, never the working tree)."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries: dict[str, TreeEntry] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        try:
            path = raw_path.decode("utf-8")
            mode, kind, oid = meta.decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError) as exc:
            raise UsageError(f"cannot read a tree entry in {repo}: {record[:80]!r}") from exc
        entries[path] = TreeEntry(mode, kind, oid)
    return entries


def normalize_prefix(text: str) -> str:
    prefix = text.replace("\\", "/")
    if not prefix.endswith("/"):
        prefix += "/"
    try:
        check_relpath(prefix[:-1])
    except UnsafePath as exc:
        raise UsageError(f"--prefix {text!r} is not a plain directory") from exc
    return prefix


def normalize_exclude(text: str) -> str:
    rule = text.replace("\\", "/")
    while rule.startswith("./"):
        rule = rule[2:]
    try:
        check_relpath(rule.rstrip("/"))
    except UnsafePath as exc:
        raise UsageError(f"--exclude {text!r} is not a plain path") from exc
    return rule


def rule_matches(path: str, rule: str) -> bool:
    """A rule ending in '/' excludes a directory; any other rule excludes that exact path."""
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule


@dataclass
class Classification:
    selected: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    not_regular: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)
    only_first: list[str] = field(default_factory=list)
    only_second: list[str] = field(default_factory=list)
    matched_rules: set[str] = field(default_factory=set)


def classify(first: dict[str, TreeEntry], second: dict[str, TreeEntry], prefixes: tuple[str, ...], rules: list[str], skip: set[str]) -> Classification:
    report = Classification()
    for path in sorted(p for p in set(first) | set(second) if p.startswith(prefixes) and p not in skip):
        matching = [rule for rule in rules if rule_matches(path, rule)]
        report.matched_rules.update(matching)
        a, b = first.get(path), second.get(path)
        if a is None:
            report.only_second.append(path)
        elif b is None:
            report.only_first.append(path)
        elif a.oid != b.oid or a.mode != b.mode:
            report.differing.append(path)
        elif a.mode not in REGULAR_MODES:
            report.not_regular.append(path)
        elif matching:
            report.excluded.append(path)
        else:
            report.selected.append(path)
    return report


def checked_contents(contents: list[tuple[str, bytes, bool]]) -> list[tuple[str, bytes, bool]]:
    seen: dict[str, str] = {}
    for rel, _, _ in contents:
        try:
            check_relpath(rel)
        except UnsafePath as exc:
            raise UsageError(f"{rel!r} is not a portable relative path; exclude it with --exclude") from exc
        key = rel.casefold()
        if key in seen:
            raise UsageError(f"{rel} and {seen[key]} differ only by case; exclude one of them with --exclude")
        seen[key] = rel
    return sorted(contents, key=lambda item: item[0].encode("utf-8"))


# --- manifest, stamp and core state -----------------------------------------


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str
    executable: bool


def parse_entries(items: object, where: str) -> list[Entry]:
    if not isinstance(items, list):
        raise UsageError(f"{where}: 'entries' must be a list")
    entries: list[Entry] = []
    seen: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) or not isinstance(item.get("executable"), bool):
            raise UsageError(f"{where}: malformed entry {item!r}")
        entry = Entry(item["path"], item["sha256"], item["executable"])
        if not _SHA256_RE.match(entry.sha256):
            raise UsageError(f"{where}: sha256 of {entry.path} is not a sha256 hex digest")
        key = entry.path.casefold()
        if key in seen:
            raise UsageError(f"{where}: {entry.path} duplicates or case-clashes with {seen[key]}")
        seen[key] = entry.path
        entries.append(entry)
    return sorted(entries, key=lambda e: e.path.encode("utf-8"))


def load_json(data: bytes, where: str) -> dict:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"{where} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != SCHEMA_VERSION:
        raise UsageError(f"{where}: expected schemaVersion {SCHEMA_VERSION}")
    return payload


def parse_manifest(data: bytes, where: str) -> tuple[dict, list[Entry]]:
    payload = load_json(data, where)
    return payload, parse_entries(payload.get("entries"), where)


def read_core_manifest(core: Path) -> list[Entry]:
    path = core / MANIFEST_NAME
    if not path.is_file():
        raise UsageError(f"{path} not found; run seed first")
    return parse_manifest(path.read_bytes(), str(path))[1]


def core_root(arg: Path | None, need_files: bool) -> Path:
    core = (arg if arg is not None else Path(__file__).resolve().parent).resolve()
    if not core.is_dir():
        raise UsageError(f"core directory not found: {core}")
    if not (core / "sync_core.py").is_file():
        raise UsageError(f"{core} is not the core repository (no sync_core.py); pass --core <core repository>")
    if need_files and not (core / FILES_DIR).is_dir():
        raise UsageError(f"{core} has no {FILES_DIR}/ directory; pass --core <core repository>")
    return core


def core_head(core: Path) -> str:
    proc = _git_run(core, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if proc.returncode != 0:
        raise UsageError(f"{core} is not a git repository with a commit")
    return proc.stdout.decode("ascii").strip()


def core_status(core: Path) -> str:
    return git(core, "--no-optional-locks", "status", "--porcelain").decode("utf-8", "replace").strip()


def require_clean_core(core: Path) -> str:
    head = core_head(core)
    status = core_status(core)
    if status:
        raise Refused("core working tree is not clean; commit or discard these first:\n  " + "\n  ".join(status.splitlines()))
    return head


def load_core_contents(core: Path, entries: list[Entry]) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for entry in entries:
        try:
            check_relpath(entry.path)
        except UnsafePath as exc:
            raise Refused(f"manifest path is not allowed: {entry.path!r}") from exc
        source = core / FILES_DIR / entry.path
        if not source.is_file() or source.is_symlink():
            raise UsageError(f"missing core file {FILES_DIR}/{entry.path}; run refresh or seed")
        data = source.read_bytes()
        if sha256_hex(data) != entry.sha256:
            raise UsageError(f"{FILES_DIR}/{entry.path} does not match core-manifest.json; run refresh after a hand edit, or seed to rebuild")
        contents[entry.path] = data
    return contents


@dataclass(frozen=True)
class Stamp:
    core_commit: str
    entries: list[Entry]


def render_stamp(core_commit: str, entries: list[Entry]) -> bytes:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "coreCommit": core_commit,
        "files": [{"path": e.path, "sha256": e.sha256, "executable": e.executable} for e in entries],
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def read_stamp(root: Path) -> Stamp:
    path = root / STAMP_NAME
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise StampError(f"no {STAMP_NAME}; run sync_core.py sync for this template") from exc
    if not stat.S_ISREG(st.st_mode):
        raise StampError(f"{STAMP_NAME} is not a regular file")
    try:
        payload = load_json(path.read_bytes(), STAMP_NAME)
        commit = payload.get("coreCommit")
        if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
            raise UsageError(f"{STAMP_NAME}: coreCommit is not a commit id")
        entries = parse_entries(payload.get("files"), STAMP_NAME)
    except UsageError as exc:
        raise StampError(str(exc)) from exc
    return Stamp(commit, entries)


# --- target template safety --------------------------------------------------


def template_root(path: Path) -> Path:
    if not path.is_dir():
        raise UsageError(f"template directory not found: {path}")
    top = Path(git(path, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(path)):
        raise UsageError(f"{path} is not the top of its git repository (repository root is {top})")
    return Path(os.path.realpath(path))


def _walk_problem(root: Path, rel: str) -> str | None:
    """Return why a symbolic link or file sits on the way to rel, or None."""
    parts = rel.split("/")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return None
        where = "/".join(parts[: index + 1])
        if stat.S_ISLNK(st.st_mode):
            return f"{rel}: {where} is a symbolic link"
        if index < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            return f"{rel}: {where} is not a directory"
    return None


def resolve_target(root: Path, rel: str) -> tuple[Path, bool]:
    """Return (path, exists) for a shared path, or raise Refused when writing there is unsafe."""
    try:
        check_relpath(rel)
    except UnsafePath as exc:
        raise Refused(f"path is not allowed in the template: {rel!r}") from exc
    problem = _walk_problem(root, rel)
    if problem:
        raise Refused(problem)
    target = root.joinpath(*rel.split("/"))
    real = os.path.normcase(os.path.realpath(target))
    if not _inside(real, os.path.normcase(str(root))):
        raise Refused(f"{rel} resolves outside the template")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return target, False
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"{rel} is a symbolic link")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{rel} is not a regular file")
    return target, True


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def dirty_paths(root: Path, rels: list[str]) -> set[str]:
    """Paths with uncommitted or untracked changes in the template (git status, no lock writes)."""
    dirty: set[str] = set()
    for start in range(0, len(rels), _STATUS_CHUNK):
        chunk = rels[start : start + _STATUS_CHUNK]
        out = git(root, "--no-optional-locks", "--literal-pathspecs", "status", "--porcelain", "-z", "--untracked-files=all", "--", *chunk)
        for record in out.split(b"\0"):
            if len(record) > 3:
                dirty.add(record[3:].decode("utf-8", "replace"))
    return dirty


def executable_matches(path: Path, executable: bool) -> bool:
    if os.name == "nt":  # Windows has no executable bit to compare
        return True
    return bool(os.stat(path).st_mode & 0o111) == executable


@dataclass(frozen=True)
class PlanItem:
    path: str
    action: str  # "same", "new", "update" or "blocked"
    reason: str
    executable: bool
    data: bytes


def build_plan(root: Path, entries: list[Entry], contents: dict[str, bytes], stamp_bytes: bytes) -> list[PlanItem]:
    """Read-only comparison of the template with what a sync would write. Stamp comes last."""
    dirty = dirty_paths(root, [e.path for e in entries] + [STAMP_NAME])
    wanted = [(e.path, e.executable, contents[e.path]) for e in entries] + [(STAMP_NAME, False, stamp_bytes)]
    plan: list[PlanItem] = []
    for rel, executable, data in wanted:
        try:
            target, exists = resolve_target(root, rel)
        except Refused as exc:
            plan.append(PlanItem(rel, "blocked", str(exc), executable, data))
            continue
        if rel in dirty:
            plan.append(PlanItem(rel, "blocked", "uncommitted or untracked changes in the template", executable, data))
            continue
        if not exists:
            action = "new"
        elif target.read_bytes() != data or not executable_matches(target, executable):
            action = "update"
        else:
            action = "same"
        plan.append(PlanItem(rel, action, "", executable, data))
    return plan


def stale_paths(root: Path, current: set[str]) -> list[str]:
    try:
        previous = read_stamp(root)
    except StampError:
        return []
    return sorted(e.path for e in previous.entries if e.path not in current)


def write_atomic(target: Path, data: bytes, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o755 if executable else 0o644)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def rebuild_files_dir(core: Path, contents: list[tuple[str, bytes, bool]]) -> None:
    final = core / FILES_DIR
    staging = core / ".files-staging"
    if final.is_symlink():
        raise UsageError(f"{final} is a symbolic link; refusing to replace it")
    if staging.is_symlink():
        staging.unlink()
    elif staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for rel, data, executable in contents:
            dest = staging.joinpath(*rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if os.name != "nt":
                os.chmod(dest, 0o755 if executable else 0o644)
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


# --- commands ----------------------------------------------------------------


def cmd_seed(args: argparse.Namespace) -> int:
    core = core_root(args.core, need_files=False)
    if len(args.template) != 2:
        raise UsageError("seed needs exactly two --template LABEL=PATH@REF arguments")
    specs = [parse_template_spec(text) for text in args.template]
    if specs[0].label == specs[1].label:
        raise UsageError("the two template labels must differ")
    prefixes = tuple(normalize_prefix(text) for text in (args.prefix or DEFAULT_PREFIXES))
    rules = [normalize_exclude(text) for text in args.exclude]

    sources: list[dict[str, str]] = []
    trees: list[dict[str, TreeEntry]] = []
    for spec in specs:
        if not spec.path.is_dir():
            raise UsageError(f"template directory not found: {spec.path}")
        commit = resolve_commit(spec.path, spec.ref)
        sources.append({"label": spec.label, "ref": spec.ref, "commit": commit})
        trees.append(list_tree(spec.path, commit))

    core_owned = {shared for _, shared in CORE_OWNED}
    report = classify(trees[0], trees[1], prefixes, rules, skip=core_owned)

    contents: list[tuple[str, bytes, bool]] = []
    for path in report.selected:
        entry = trees[0][path]
        contents.append((path, git(specs[0].path, "cat-file", "blob", entry.oid), entry.mode == "100755"))
    for source_name, shared in CORE_OWNED:
        source = core / source_name
        if not source.is_file():
            raise UsageError(f"core-owned file not found: {source}")
        contents.append((shared, source.read_bytes(), True))
    contents = checked_contents(contents)

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "sources": sources,
        "prefixes": list(prefixes),
        "excluded": sorted(report.excluded),
        "entries": [{"path": rel, "sha256": sha256_hex(data), "executable": executable} for rel, data, executable in contents],
    }
    rebuild_files_dir(core, contents)
    write_atomic(core / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), False)

    label_a, label_b = specs[0].label, specs[1].label
    print(f"Seeded {len(contents)} entries into {FILES_DIR}/ and {MANIFEST_NAME}: {len(report.selected)} template files and {len(CORE_OWNED)} core-owned file.")
    print("Sources: " + "; ".join(f"{s['label']} {s['ref']} {s['commit'][:12]}" for s in sources))
    print(f"identical: {len(report.selected)} selected, {len(report.excluded)} excluded by --exclude, {len(report.not_regular)} symlink or submodule skipped")
    print(f"differing: {len(report.differing)} (not in core; Phase 2)")
    print(f"one-sided: {len(report.only_first) + len(report.only_second)} ({label_a} only {len(report.only_first)}, {label_b} only {len(report.only_second)})")
    for rule in rules:
        if rule not in report.matched_rules:
            print(f"warning: --exclude {rule} matched no path", file=sys.stderr)
    for path in report.excluded:
        print(f"  excluded: {path}")
    for path in report.not_regular:
        print(f"  skipped (not a regular file): {path}")
    shadowed = sorted(p for p in core_owned if p in trees[0] or p in trees[1])
    for path in shadowed:
        print(f"  note: template copies of {path} are ignored; the core owns that file")
    if args.verbose:
        for path in report.differing:
            print(f"  differing: {path}")
        for path in report.only_first:
            print(f"  one-sided {label_a}: {path}")
        for path in report.only_second:
            print(f"  one-sided {label_b}: {path}")
    return EXIT_OK


@dataclass
class Prepared:
    core: Path
    head: str
    entries: list[Entry]
    root: Path
    plan: list[PlanItem]
    stale: list[str]


def prepare(core: Path, template_dir: Path, clean: bool) -> Prepared:
    entries = read_core_manifest(core)
    head = require_clean_core(core) if clean else core_head(core)
    contents = load_core_contents(core, entries)
    root = template_root(template_dir)
    stamp_bytes = render_stamp(head, entries)
    plan = build_plan(root, entries, contents, stamp_bytes)
    stale = stale_paths(root, {e.path for e in entries})
    return Prepared(core, head, entries, root, plan, stale)


def cmd_sync(args: argparse.Namespace) -> int:
    core = core_root(args.core, need_files=True)
    prep = prepare(core, args.template_dir, clean=True)
    blocked = [item for item in prep.plan if item.action == "blocked"]
    if blocked:
        lines = "\n  ".join(f"{item.path}: {item.reason}" for item in blocked)
        raise Refused(f"{len(blocked)} target path(s) cannot be synced; nothing was changed:\n  {lines}")
    for item in prep.plan:
        if item.action in ("new", "update"):
            write_atomic(prep.root.joinpath(*item.path.split("/")), item.data, item.executable)
    shared = [item for item in prep.plan if item.path != STAMP_NAME]
    written = sum(1 for item in shared if item.action in ("new", "update"))
    print(f"Synced {len(shared)} shared files from core {prep.head[:12]} into {prep.root}: {written} written, {len(shared) - written} already identical.")
    stamp_item = prep.plan[-1]
    print(f"Stamp: {STAMP_NAME} ({stamp_item.action}).")
    for path in prep.stale:
        print(f"Note: {path} is no longer in core; left in place, delete it by hand if it is obsolete.")
    print("Next: review with git status and git diff, then commit the changed files and .core-stamp.json before the next sync.")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    core = core_root(args.core, need_files=True)
    prep = prepare(core, args.template_dir, clean=False)
    if core_status(core):
        print("note: the core working tree has uncommitted changes; a sync would refuse until they are committed.")
    changed = 0
    for item in prep.plan:
        if item.action == "same":
            continue
        changed += 1
        if item.action == "blocked":
            print(f"blocked {item.path}: {item.reason}")
        else:
            print(f"{item.action} {item.path}")
    for path in prep.stale:
        print(f"stale {path}: no longer in core (sync leaves it in place)")
    unchanged = len(prep.plan) - changed
    print(f"{changed} change(s), {unchanged} unchanged, against core {prep.head[:12]}.")
    return EXIT_REFUSED if changed else EXIT_OK


def cmd_refresh(args: argparse.Namespace) -> int:
    """Re-hash files/ into the manifest after a hand fix. Adds and removes no paths."""
    core = core_root(args.core, need_files=True)
    manifest_path = core / MANIFEST_NAME
    if not manifest_path.is_file():
        raise UsageError(f"{manifest_path} not found; run seed first")
    payload, entries = parse_manifest(manifest_path.read_bytes(), str(manifest_path))
    listed = {e.path for e in entries}
    for source_name, shared in CORE_OWNED:
        if shared in listed:
            write_atomic(core.joinpath(FILES_DIR, *shared.split("/")), (core / source_name).read_bytes(), True)
    files_root = core / FILES_DIR
    on_disk: set[str] = set()
    for path in files_root.rglob("*"):
        if path.is_symlink():
            raise UsageError(f"{path} is a symbolic link; files/ must hold regular files only")
        if path.is_file():
            on_disk.add(path.relative_to(files_root).as_posix())
    extra = sorted(on_disk - listed)
    if extra:
        raise UsageError("files/ has paths the manifest does not list (seed adds paths): " + ", ".join(extra))
    missing = sorted(listed - on_disk)
    if missing:
        raise UsageError("manifest lists paths missing from files/: " + ", ".join(missing))
    refreshed: list[str] = []
    new_entries: list[dict[str, object]] = []
    for entry in entries:
        try:
            check_relpath(entry.path)
        except UnsafePath as exc:
            raise UsageError(f"manifest path is not allowed: {entry.path!r}") from exc
        source = files_root.joinpath(*entry.path.split("/"))
        data = source.read_bytes()
        executable = bool(os.stat(source).st_mode & 0o111) if os.name != "nt" else entry.executable
        new = Entry(entry.path, sha256_hex(data), executable)
        if new != entry:
            refreshed.append(entry.path)
        new_entries.append({"path": new.path, "sha256": new.sha256, "executable": new.executable})
    payload["entries"] = new_entries
    rendered = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    if rendered != manifest_path.read_bytes():
        write_atomic(manifest_path, rendered, False)
    print(f"Refreshed {MANIFEST_NAME}: {len(refreshed)} entr{'y' if len(refreshed) == 1 else 'ies'} changed" + (f": {', '.join(refreshed)}" if refreshed else "") + ".")
    return EXIT_OK


def file_problem(root: Path, entry: Entry) -> str | None:
    try:
        check_relpath(entry.path)
    except UnsafePath:
        return f"unsafe path in stamp: {entry.path}"
    problem = _walk_problem(root, entry.path)
    if problem:
        return f"symbolic link or non-directory on the path: {problem}"
    target = root.joinpath(*entry.path.split("/"))
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return f"missing: {entry.path}"
    if not stat.S_ISREG(st.st_mode):
        return f"not a regular file: {entry.path}"
    if sha256_hex(target.read_bytes()) != entry.sha256:
        return f"modified: {entry.path}"
    return None


def stamp_against_core(core: Path, stamp: Stamp) -> list[str]:
    if _git_run(core, "cat-file", "-e", f"{stamp.core_commit}^{{commit}}").returncode != 0:
        return [f"stamp commit {stamp.core_commit[:12]} is not in the core repository"]
    try:
        _, core_entries = parse_manifest(git(core, "cat-file", "blob", f"{stamp.core_commit}:{MANIFEST_NAME}"), f"{MANIFEST_NAME} at {stamp.core_commit[:12]}")
    except UsageError as exc:
        return [str(exc)]
    want = {(e.path, e.sha256, e.executable) for e in stamp.entries}
    have = {(e.path, e.sha256, e.executable) for e in core_entries}
    problems = [f"stamp differs from core at {stamp.core_commit[:12]}: {path} (stamp has sha256 {sha[:12]}, executable {exe})" for path, sha, exe in sorted(want - have)]
    problems += [f"core at {stamp.core_commit[:12]} has {path}, the stamp does not" for path, _, _ in sorted(have - want)]
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    root_arg = Path(args.template_dir)
    if not root_arg.is_dir():
        raise UsageError(f"template directory not found: {root_arg}")
    root = Path(os.path.realpath(root_arg))
    try:
        stamp = read_stamp(root)
    except StampError as exc:
        print(f"DRIFT: {exc}")
        return EXIT_REFUSED
    problems = [problem for problem in (file_problem(root, entry) for entry in stamp.entries) if problem]
    if args.core is not None:
        problems += stamp_against_core(core_root(args.core, need_files=False), stamp)
    if problems:
        for problem in problems:
            print(f"DRIFT: {problem}")
        print(f"{len(problems)} problem(s) in {root}")
        return EXIT_REFUSED
    basis = ", checked against core-manifest.json at the stamped commit" if args.core is not None else ""
    print(f"OK: {len(stamp.entries)} shared files match {STAMP_NAME} (core {stamp.core_commit[:12]}{basis}).")
    return EXIT_OK


# --- entry point -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sync_core.py", description="Keep the shared template files identical with the core repository.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    seed = sub.add_parser("seed", help="rebuild files/ and core-manifest.json from committed template refs")
    seed.add_argument("--template", action="append", default=[], required=True, metavar="LABEL=PATH@REF", help="give exactly two")
    seed.add_argument("--prefix", action="append", metavar="DIR/", help="directory to select (default: scripts/ tools/ tests/)")
    seed.add_argument("--exclude", action="append", default=[], metavar="PATH", help="exact path, or DIR/ for a directory")
    seed.add_argument("--core", type=Path, metavar="DIR", help="core repository (default: the directory of this script)")
    seed.add_argument("--verbose", action="store_true", help="list differing and one-sided paths")
    seed.set_defaults(handler=cmd_seed)

    refresh = sub.add_parser("refresh", help="re-hash files/ into core-manifest.json after a hand fix (adds and removes no paths)")
    refresh.add_argument("--core", type=Path, metavar="DIR", help="core repository (default: the directory of this script)")
    refresh.set_defaults(handler=cmd_refresh)

    commands = (
        ("sync", cmd_sync, "copy the shared files into a template and write its stamp"),
        ("check", cmd_check, "verify a template against its stamp (offline)"),
        ("diff", cmd_diff, "show what the next sync would change (writes nothing)"),
    )
    for name, handler, text in commands:
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("template_dir", type=Path, metavar="TEMPLATE_DIR", help="root of the template working tree")
        cmd.add_argument("--core", type=Path, metavar="DIR", help="core repository (sync and diff default to this script's directory)")
        cmd.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # last-resort guard: an unexpected failure must still exit 2
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
