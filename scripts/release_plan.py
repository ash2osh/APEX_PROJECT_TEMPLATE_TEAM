#!/usr/bin/env python3
"""Plan the next schema-version 2 rollout manifest from committed changes since the last shipped release.

The plan reads Git only (no database) and writes only the manifest. Each step names
committed files under features/, migrations/ or apps/. The generated manifest is
checked by the rollout loader before it is written; freeze and rollout check it again.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts import feature_manifest, local_config, migration_manifest, migration_resolution, rollout
from scripts.feature_sources import UNIT_GROUPS
from scripts.path_utils import relative_to_root


DEFAULT_OUTPUT = Path("releases") / "current.json"
RELEASE_TAG_PREFIX = "release/"
DATABASE_OBJECT_BUDGET_SECONDS = 120
PATHSPEC = ("features", "migrations", "apps")
SCHEMA_ENV_KEYS = ("TABLES_SCHEMA", "CODE_SCHEMA", "MIGRATION_SCHEMA")
EVIDENCE_PATTERNS = ("status.*.json", "attempt-event.*", "resolution.*.json", "README.md")
RESOLUTION_PATTERN = "resolution.*.json"
NUMERIC_APP_ID_RE = re.compile(r"[1-9][0-9]{0,17}\Z", re.ASCII)
STEP_TYPE_ORDER = ("feature-migrate", "sql-script", "migrate", "app-deploy")
TRIGGER_GROUP = "trigger"
GROUP_BY_DIRECTORY = {group.directory: group for group in UNIT_GROUPS}


class ReleasePlanError(ValueError):
    """The release plan cannot be built, or the manifest it would write is refused."""


@dataclass(frozen=True)
class ReleasePlan:
    baseline: str
    baseline_commit: str | None
    steps: tuple[dict, ...]
    excluded: tuple[tuple[str, str], ...]
    removed: tuple[str, ...]
    not_planned: tuple[str, ...]
    uncommitted: tuple[str, ...]
    output: str | None
    written: bool

    @property
    def empty(self) -> bool:
        return not self.steps

    @property
    def counts(self) -> dict[str, int]:
        found = Counter(step["type"] for step in self.steps)
        return {kind: found[kind] for kind in STEP_TYPE_ORDER if found[kind]}


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    except OSError as error:
        raise ReleasePlanError(f"git is not available: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ReleasePlanError(f"git {args[0]} failed: {detail or 'exit ' + str(completed.returncode)}")
    return completed.stdout


def _split_nul(raw: bytes) -> list[str]:
    return [item for item in raw.decode("utf-8").split("\0") if item]


def _commit(root: Path, revision: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            capture_output=True, check=False,
        )
    except OSError as error:
        raise ReleasePlanError(f"git is not available: {error}") from error
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8").strip()
    return value or None


def _newest_release_tag(root: Path) -> str | None:
    lines = _git(root, "for-each-ref", "--sort=-creatordate", "--format=%(refname)", f"refs/tags/{RELEASE_TAG_PREFIX}")
    for ref in lines.decode("utf-8").splitlines():
        if ref.startswith(f"refs/tags/{RELEASE_TAG_PREFIX}"):
            return ref[len("refs/tags/"):]
    return None


def _repository_root(root: Path) -> Path:
    requested = Path(root)
    if requested.is_symlink():
        raise ReleasePlanError("repository root cannot be a symbolic link")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise ReleasePlanError(f"repository root is unavailable: {error}") from error
    if not resolved.is_dir():
        raise ReleasePlanError("repository root must be a directory")
    return resolved


def _output_path(root: Path, output: Path) -> Path:
    candidate = output if output.is_absolute() else root / output
    try:
        relative = relative_to_root(candidate, root)
    except ValueError as error:
        raise ReleasePlanError(f"output must be inside the repository: {output}") from error
    if not relative.parts:
        raise ReleasePlanError("output must name a file inside the repository")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReleasePlanError(f"output path contains a symbolic link: {current}")
    return candidate


def _schema_rank(root: Path) -> dict[str, int]:
    """Schemas in .env order: TABLES, CODE, MIGRATION, then alphabetical for the rest."""
    env_path = root / ".env"
    if not env_path.is_file():
        return {}
    try:
        values = local_config.read_project_env(env_path)
    except (local_config.ConfigError, OSError) as error:
        raise ReleasePlanError(f"cannot read .env for the schema order: {error}") from error
    ordered: list[str] = []
    for key in SCHEMA_ENV_KEYS:
        for item in values.get(key, "").split(","):
            schema = item.strip()
            if schema and schema not in ordered:
                ordered.append(schema)
    return {schema: index for index, schema in enumerate(ordered)}


def _rank(ranks: dict[str, int], schema: str) -> tuple[int, str]:
    return (ranks.get(schema, len(ranks)), schema)


def _is_evidence(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in EVIDENCE_PATTERNS)


def _resolution_reason(folder: Path, names: Sequence[str]) -> str:
    parts = []
    for name in names:
        try:
            outcome = migration_resolution.read_resolution(folder / name).get("outcome", "unknown")
        except (OSError, ValueError):
            outcome = "unreadable"
        parts.append(f"{name} (outcome: {outcome})")
    return "owner resolution " + ", ".join(parts) + "; excluded for review, not in the manifest"


def _feature_steps(
    changed: Sequence[str],
    head_files: set[str],
    ranks: dict[str, int],
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[str], list[str]]:
    """Return ddl, view, unit, trigger and data steps, plus unplanned and removed paths."""
    ddl: dict[tuple[str, str], list[str]] = {}
    views: dict[tuple[str, str], list[str]] = {}
    data: dict[tuple[str, str], list[str]] = {}
    units: dict[tuple[str, str], dict[str, tuple]] = {}
    triggers: dict[tuple[str, str], dict[str, tuple]] = {}
    checks: list[tuple[tuple[str, str], str]] = []
    not_planned: list[str] = []
    removed: list[str] = []
    for path in sorted(set(changed)):
        parts = path.split("/")
        if parts[0] != "features":
            continue
        if len(parts) == 3 and parts[2] == "README.md":
            continue
        if len(parts) < 4 or feature_manifest.FEATURE_RE.fullmatch(parts[1]) is None or feature_manifest.SCHEMA_RE.fullmatch(parts[2]) is None:
            not_planned.append(path)
            continue
        feature, schema, tail = parts[1], parts[2], parts[3:]
        key = (schema, feature)
        if path not in head_files:
            removed.append(path)
            continue
        if tail == ["ddl.sql"]:
            ddl.setdefault(key, []).append(path)
        elif len(tail) == 2 and tail[0] == "views" and tail[1].endswith(".sql"):
            views.setdefault(key, []).append(path)
        elif len(tail) == 2 and tail[0] == "data" and tail[1].endswith(".sql"):
            data.setdefault(key, []).append(path)
        elif tail == ["checks.json"] or tail[0] == "checks":
            checks.append((key, path))
        elif len(tail) == 2 and tail[0] in GROUP_BY_DIRECTORY:
            group = GROUP_BY_DIRECTORY[tail[0]]
            match = None
            for index, (_kind, suffix) in enumerate(group.types):
                marker = f".{suffix}.sql"
                if tail[1].endswith(marker) and len(tail[1]) > len(marker):
                    match = (index, tail[1][: -len(marker)])
                    break
            if match is None and len(group.types) == 1 and tail[1].endswith(".sql") and len(tail[1]) > len(".sql"):
                # A folder that holds one object type (triggers, procedures, functions) needs no type suffix:
                # older captures and hand-written files are named <name>.sql, and the freeze loader reads the
                # object from the file itself. Packages and types keep their spec/body suffixes.
                match = (0, tail[1][: -len(".sql")])
            if match is None:
                not_planned.append(path)
                continue
            index, stem = match
            target = triggers if group.name == TRIGGER_GROUP else units
            group_index = UNIT_GROUPS.index(group)
            # A specification and its body are one checkpoint: add the other half when it is committed.
            members = [(index, path)]
            for other_index, (_other_kind, other_suffix) in enumerate(group.types):
                if other_index != index:
                    companion = "/".join([*parts[:3], group.directory, f"{stem}.{other_suffix}.sql"])
                    if companion in head_files:
                        members.append((other_index, companion))
            bucket = target.setdefault(key, {})
            for member_index, member_path in members:
                bucket[member_path] = ((group_index, stem, member_index), member_path)
        else:
            not_planned.append(path)

    selected = set(ddl) | set(views) | set(data) | set(units) | set(triggers)
    not_planned.extend(path for key, path in checks if key not in selected)
    keys = sorted(selected, key=lambda item: (_rank(ranks, item[0]), item[1]))

    def feature_step(inputs: list[str], schema: str) -> dict:
        return {"type": "feature-migrate", "schema": schema, "inputs": inputs}

    ddl_steps = [feature_step(sorted(ddl[key]), key[0]) for key in keys if key in ddl]
    view_steps = [feature_step(sorted(views[key]), key[0]) for key in keys if key in views]
    unit_steps = [
        feature_step([path for _order, path in sorted(units[key].values())], key[0])
        for key in keys if key in units
    ]
    trigger_steps = [
        feature_step([path for _order, path in sorted(triggers[key].values())], key[0])
        for key in keys if key in triggers
    ]
    data_steps = sorted(
        ({"type": "sql-script", "file": path, "schema": key[0]} for key in keys if key in data for path in data[key]),
        key=lambda step: step["file"],
    )
    return ddl_steps, view_steps, unit_steps, trigger_steps, data_steps, sorted(not_planned), sorted(removed)


def _migration_steps(
    root: Path,
    changed: Sequence[str],
) -> tuple[list[dict], list[tuple[str, str]], list[str]]:
    """Return one migrate step per changed active folder, excluded folders with reasons, and removed folders."""
    migrations_dir = root / "migrations"
    if not migrations_dir.is_dir():
        return [], [], []
    try:
        discovered = {
            relative_to_root(path, root).as_posix(): path
            for path, _schema, _family, _revision in migration_manifest._migration_directories(root)
        }
    except migration_manifest.MigrationManifestError as error:
        raise ReleasePlanError(f"migration folders cannot be listed: {error}") from error

    changed_files: dict[str, list[str]] = {}
    for path in changed:
        parts = path.split("/")
        if parts[0] != "migrations" or len(parts) < 3:
            continue
        if len(parts) >= 4 and migration_manifest.SCHEMA_DIRECTORY_RE.fullmatch(parts[1]) and migration_manifest.FOLDER_RE.fullmatch(parts[2]):
            folder, rest = "/".join(parts[:3]), parts[3:]
        elif migration_manifest.FOLDER_RE.fullmatch(parts[1]):
            folder, rest = "/".join(parts[:2]), parts[2:]
        else:
            continue
        changed_files.setdefault(folder, []).append("/".join(rest))

    steps: list[dict] = []
    excluded: list[tuple[str, str]] = []
    removed: list[str] = []
    for folder in sorted(changed_files):
        if folder not in discovered:
            removed.append(folder)
            continue
        folder_path = discovered[folder]
        resolutions = sorted(
            entry.name for entry in folder_path.iterdir()
            if fnmatch.fnmatchcase(entry.name, RESOLUTION_PATTERN) and entry.is_file()
        )
        if resolutions:
            excluded.append((folder, _resolution_reason(folder_path, resolutions)))
            continue
        names = changed_files[folder]
        if all("/" not in name and _is_evidence(name) for name in names):
            continue
        steps.append({"type": "migrate", "folders": [folder]})
    steps.sort(key=lambda step: (step["folders"][0].rsplit("/", 1)[-1], step["folders"][0]))
    return steps, excluded, removed


def _app_steps(changed: Sequence[str]) -> list[dict]:
    app_ids: set[int] = set()
    for path in changed:
        parts = path.split("/")
        if parts[0] != "apps":
            continue
        if len(parts) >= 3 and NUMERIC_APP_ID_RE.fullmatch(parts[1]):
            app_id, rest = int(parts[1]), parts[2:]
        elif len(parts) >= 4 and migration_manifest.SCHEMA_DIRECTORY_RE.fullmatch(parts[1]) and NUMERIC_APP_ID_RE.fullmatch(parts[2]):
            app_id, rest = int(parts[2]), parts[3:]
        else:
            continue
        if not rest or rest[0] == "deployments":
            continue
        app_ids.add(app_id)
    return [{"type": "app-deploy", "appId": app_id} for app_id in sorted(app_ids)]


def _validate_document(root: Path, raw: bytes) -> None:
    """Run the rollout loader on the candidate bytes, from a private directory inside the repository."""
    staging = root / "scratch" / "release-plan"
    staging.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="candidate-", dir=staging))
    try:
        candidate = directory / "current.json"
        candidate.write_bytes(raw)
        rollout.load_manifest(candidate, root, version=2)
    except (
        rollout.RolloutError, feature_manifest.FeatureManifestError,
        migration_manifest.MigrationManifestError, OSError, ValueError,
    ) as error:
        raise ReleasePlanError(f"generated manifest refused by the rollout loader; nothing written: {error}") from error
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _write_document(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def create_release_plan(
    repo_root: Path,
    *,
    since: str | None = None,
    include_all: bool = False,
    output: Path | None = None,
    force: bool = False,
) -> ReleasePlan:
    """Build the release manifest from committed changes; write it only when it is non-empty and valid."""
    if since is not None and include_all:
        raise ReleasePlanError("--since and --all are mutually exclusive")
    if since is not None and (not since.strip() or since.startswith("-") or "\x00" in since):
        raise ReleasePlanError("--since needs a git ref that does not start with '-'")
    root = _repository_root(repo_root)
    output_path = _output_path(root, DEFAULT_OUTPUT if output is None else Path(output))
    if _commit(root, "HEAD") is None:
        raise ReleasePlanError("the repository has no commits to plan from")

    if include_all:
        baseline = "--all (first release: every tracked file under features/, migrations/ and apps/)"
        baseline_commit = None
        changed = _split_nul(_git(root, "ls-files", "-z", "--", *PATHSPEC))
    else:
        reference = since
        if reference is None:
            reference = _newest_release_tag(root)
            if reference is None:
                raise ReleasePlanError(
                    "no release/* tag found; pass --since REF for the last shipped release, or --all for the first release"
                )
        baseline_commit = _commit(root, reference)
        if baseline_commit is None:
            raise ReleasePlanError(f"baseline {reference!r} is not a commit")
        baseline = reference
        changed = _split_nul(_git(
            root, "diff", "-z", "--name-only", "--no-renames", baseline_commit, "HEAD", "--", *PATHSPEC,
        ))

    uncommitted = tuple(
        entry[3:] for entry in _split_nul(_git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", "--", *PATHSPEC))
    )
    head_files = set(_split_nul(_git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", "features")))
    ranks = _schema_rank(root)

    ddl_steps, view_steps, unit_steps, trigger_steps, data_steps, not_planned, removed = _feature_steps(
        changed, head_files, ranks,
    )
    migrate_steps, excluded, removed_folders = _migration_steps(root, changed)
    app_steps = _app_steps(changed)
    steps = tuple(ddl_steps + view_steps + unit_steps + trigger_steps + data_steps + migrate_steps + app_steps)
    all_removed = tuple(sorted({*removed, *removed_folders}))
    if not steps:
        return ReleasePlan(baseline, baseline_commit, (), tuple(excluded), all_removed, tuple(not_planned), uncommitted, None, False)

    if (output_path.is_symlink() or output_path.exists()) and not force:
        raise ReleasePlanError(f"refusing to overwrite {relative_to_root(output_path, root).as_posix()}; pass --force to replace it")
    document = {"schemaVersion": 2, "databaseObjectBudgetSeconds": DATABASE_OBJECT_BUDGET_SECONDS, "steps": list(steps)}
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _validate_document(root, raw)
    _write_document(output_path, raw)
    return ReleasePlan(
        baseline, baseline_commit, steps, tuple(excluded), all_removed, tuple(not_planned), uncommitted,
        relative_to_root(output_path, root).as_posix(), True,
    )


def render(plan: ReleasePlan, output_format: str = "text") -> str:
    if output_format == "json":
        return json.dumps({
            "baseline": plan.baseline,
            "baselineCommit": plan.baseline_commit,
            "stepCounts": plan.counts,
            "steps": list(plan.steps),
            "excludedForReview": [{"folder": folder, "reason": reason} for folder, reason in plan.excluded],
            "removed": list(plan.removed),
            "notPlanned": list(plan.not_planned),
            "uncommittedNotIncluded": list(plan.uncommitted),
            "output": plan.output,
            "written": plan.written,
        }, ensure_ascii=False, indent=2, sort_keys=True)

    lines: list[str] = []
    if plan.empty:
        lines.append(f"nothing changed since {plan.baseline}")
    else:
        commit = f" ({plan.baseline_commit[:12]})" if plan.baseline_commit else ""
        lines.append(f"Baseline: {plan.baseline}{commit}")
        lines.append(f"Steps: {len(plan.steps)}")
        lines.extend(f"  {kind}: {count}" for kind, count in plan.counts.items())
    if plan.excluded:
        lines.append("Excluded for review:")
        lines.extend(f"  {folder}: {reason}" for folder, reason in plan.excluded)
    if plan.removed:
        lines.append("Removed since the baseline (not in the manifest; review by hand):")
        lines.extend(f"  {path}" for path in plan.removed)
    if plan.not_planned:
        lines.append("Changed but not selected by release-plan (review by hand):")
        lines.extend(f"  {path}" for path in plan.not_planned)
    if plan.uncommitted:
        sample = ", ".join(plan.uncommitted[:5]) + (" ..." if len(plan.uncommitted) > 5 else "")
        lines.append(
            f"Warning: {len(plan.uncommitted)} uncommitted path(s) under features/, migrations/ or apps/ are not included "
            f"(freeze needs committed inputs): {sample}"
        )
    if plan.written and plan.output is not None:
        lines.append(f"Wrote {plan.output}")
        lines.append(f"Next: scripts/team.sh feature freeze {plan.output}")
    return "\n".join(lines)
