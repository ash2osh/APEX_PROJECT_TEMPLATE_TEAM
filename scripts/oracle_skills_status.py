#!/usr/bin/env python3
"""Shared read-only inspection of SQLcl native Oracle skills registry and catalog.

Reads SQLcl's user-local registry (~/.dbtools/skills/skills.json) and synced catalog.
- Matches expanded targetPath values and validates all Oracle catalog roots.
- Deduplicates identical records for a shared path; marks conflicting duplicates invalid.
- Rejects future timestamps, naive timestamps, and malformed timestamps.
- Requires installed SKILL.md for every loaded installation.
- Refuses freshness if repository sync or any loaded installation is 7 days old or older.
- Never writes timestamp files, mutates registries, or infers freshness from file mtimes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_SKILL_ROOTS = (
    Path("~/.agents/skills"),
    Path("~/.codex/skills"),
)


def parse_utc_timestamp(ts_str: object) -> datetime:
    """Parse an ISO-8601 UTC timestamp string with timezone awareness."""
    if not isinstance(ts_str, str):
        raise ValueError("timestamp must be a string")
    ts_str = ts_str.strip()
    if not (ts_str.endswith("Z") or ts_str.endswith("+00:00") or ts_str.endswith("-00:00")):
        raise ValueError(f"timestamp must be in UTC: {ts_str!r}")

    cleaned = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str

    match = re.fullmatch(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})$", cleaned)
    if not match:
        raise ValueError(f"invalid ISO timestamp format: {ts_str!r}")

    base, frac, tz = match.groups()
    if frac:
        # Normalize fractional seconds to at most 6 digits for Python datetime
        frac = frac[:7].ljust(7, "0")
        cleaned = f"{base}{frac}{tz}"
    else:
        cleaned = f"{base}{tz}"

    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        raise ValueError("naive timestamp is not permitted")
    return dt.astimezone(timezone.utc)


def inspect_oracle_skills(
    registry: Path,
    skill_roots: Sequence[Path] | None = None,
    now: datetime | None = None,
) -> dict:
    """Return a read-only status, including inaccessible catalog/installations."""
    try:
        return _inspect_oracle_skills(registry, skill_roots, now)
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError):
        return {"status": "invalid", "oldestInstalledAt": None, "installationCount": 0,
                "issues": ["Oracle registry, catalog or installation could not be inspected"]}


def _inspect_oracle_skills(
    registry: Path,
    skill_roots: Sequence[Path] | None = None,
    now: datetime | None = None,
) -> dict:
    """Inspect Oracle skills registry and loaded roots read-only.

    Returns dict with keys:
    - status: "current" | "stale" | "missing" | "invalid"
    - oldestInstalledAt: str | None
    - installationCount: int
    - issues: list[str]
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    expanded_registry = Path(os.path.expanduser(str(registry)))
    if not expanded_registry.is_file():
        return {
            "status": "missing",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": [f"skills registry not found: {expanded_registry}"],
        }

    try:
        raw_data = json.loads(expanded_registry.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": [f"malformed skills registry: {expanded_registry}: {exc}"],
        }

    if not isinstance(raw_data, dict):
        return {
            "status": "invalid",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": ["registry root must be an object"],
        }

    issues: list[str] = []
    is_invalid = False
    is_stale = False

    # 1. Locate repository and discover catalog roots
    repositories = raw_data.get("repositories")
    if not isinstance(repositories, list):
        return {
            "status": "invalid",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": ["repositories in registry must be a list"],
        }

    oracle_repos = [repo for repo in repositories if isinstance(repo, dict) and repo.get("id") == "oracle-skills"]
    oracle_repo = oracle_repos[0] if oracle_repos else None
    if oracle_repo is None:
        return {
            "status": "invalid",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": ["oracle-skills repository not found in registry"],
        }
    if any(repo != oracle_repo for repo in oracle_repos[1:]):
        return {"status": "invalid", "oldestInstalledAt": None, "installationCount": 0,
                "issues": ["conflicting oracle-skills repository records"]}

    last_sync_raw = oracle_repo.get("lastSync")
    try:
        last_sync_dt = parse_utc_timestamp(last_sync_raw)
        if last_sync_dt > now:
            issues.append(f"future lastSync timestamp in repository: {last_sync_raw}")
            is_invalid = True
        elif (now - last_sync_dt) >= timedelta(days=7):
            issues.append(f"repository sync is stale (lastSync: {last_sync_raw})")
            is_stale = True
    except (ValueError, TypeError) as exc:
        issues.append(f"invalid repository lastSync timestamp: {last_sync_raw}: {exc}")
        is_invalid = True

    catalog_path_raw = oracle_repo.get("localPath")
    if not isinstance(catalog_path_raw, str) or not catalog_path_raw.strip():
        issues.append("oracle-skills repository missing or non-string localPath")
        return {"status": "invalid", "oldestInstalledAt": None, "installationCount": 0, "issues": issues}

    catalog_path = Path(os.path.expanduser(catalog_path_raw)).resolve()
    if not catalog_path.is_dir():
        issues.append(f"oracle-skills catalog directory missing: {catalog_path}")
        return {"status": "invalid", "oldestInstalledAt": None, "installationCount": 0, "issues": issues}

    # Discover catalog skills
    catalog_skills: list[str] = []
    for item in sorted(catalog_path.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            catalog_skills.append(item.name)
            if not (item / "SKILL.md").is_file():
                issues.append(f"Oracle catalog root {item.name} is missing SKILL.md in catalog")
                is_invalid = True
            else:
                (item / "SKILL.md").read_text(encoding="utf-8")

    if not catalog_skills:
        issues.append(f"no Oracle catalog skills found in {catalog_path}")
        return {"status": "invalid", "oldestInstalledAt": None, "installationCount": 0, "issues": issues}

    # 2. Process installations in registry (deduplicate identical, flag conflicting)
    raw_installations = raw_data.get("installations", [])
    if not isinstance(raw_installations, list):
        return {
            "status": "invalid",
            "oldestInstalledAt": None,
            "installationCount": 0,
            "issues": ["installations in registry must be a list"],
        }

    target_map: dict[str, dict] = {}
    roots_to_check = DEFAULT_SKILL_ROOTS if skill_roots is None else skill_roots
    resolved_roots = list(dict.fromkeys(Path(root).expanduser().resolve() for root in roots_to_check))
    expected_targets = {str((root / skill).resolve()): skill
                        for root in resolved_roots for skill in catalog_skills}
    for inst in raw_installations:
        if not isinstance(inst, dict):
            issues.append("installation entry in registry must be an object")
            is_invalid = True
            continue

        raw_target = inst.get("targetPath")
        if not isinstance(raw_target, str) or not raw_target.strip():
            issues.append("installation record missing or non-string targetPath")
            is_invalid = True
            continue
        norm_target = str(Path(raw_target).expanduser().resolve())
        if norm_target not in expected_targets:
            continue

        skill_name = inst.get("skillName")
        if not isinstance(skill_name, str) or not skill_name.strip():
            issues.append(f"installation record {raw_target} missing or non-string skillName")
            is_invalid = True
            continue
        if skill_name != expected_targets[norm_target]:
            issues.append(f"installation skillName does not match expected Oracle root: {norm_target}")
            is_invalid = True

        # Validate skillName and source / sourcePath correspondence
        raw_source = inst.get("source")
        raw_source_path = inst.get("sourcePath")
        if raw_source is not None and not isinstance(raw_source, str):
            issues.append(f"installation record {raw_target} non-string source")
            is_invalid = True
        if raw_source_path is not None:
            if not isinstance(raw_source_path, str) or not raw_source_path.strip():
                issues.append(f"installation record {raw_target} non-string sourcePath")
                is_invalid = True
            else:
                norm_source = Path(os.path.expanduser(raw_source_path)).resolve()
                if norm_source.name != skill_name or norm_source != (catalog_path / skill_name).resolve():
                    issues.append(
                        f"installation record skillName {skill_name!r} does not correspond to Oracle sourcePath {raw_source_path!r}"
                    )
                    is_invalid = True

        if norm_target in target_map:
            prev = target_map[norm_target]
            # Deduplicate only records identical across skillName, scope, source, sourcePath, installedAt
            prev_src_path = str(Path(os.path.expanduser(prev["sourcePath"])).resolve()) if isinstance(prev.get("sourcePath"), str) else prev.get("sourcePath")
            curr_src_path = str(Path(os.path.expanduser(inst["sourcePath"])).resolve()) if isinstance(inst.get("sourcePath"), str) else inst.get("sourcePath")
            prev_keys = (prev.get("skillName"), prev.get("scope"), prev.get("source"), prev_src_path, prev.get("installedAt"))
            curr_keys = (inst.get("skillName"), inst.get("scope"), inst.get("source"), curr_src_path, inst.get("installedAt"))
            if prev_keys != curr_keys:
                issues.append(f"conflicting duplicate installation records for {norm_target}")
                is_invalid = True
        else:
            target_map[norm_target] = inst


    # 3. Validate loaded skill roots
    oldest_dt: datetime | None = None
    oldest_str: str | None = None
    validated_targets: set[str] = set()

    for root_dir in resolved_roots:
        for skill_name in catalog_skills:
            target_path = root_dir / skill_name
            norm_target_str = str(target_path.resolve())

            if norm_target_str not in target_map:
                issues.append(f"missing installation record for {norm_target_str} ({skill_name})")
                is_invalid = True
                continue

            inst_record = target_map[norm_target_str]
            installed_at_raw = inst_record.get("installedAt")
            try:
                inst_dt = parse_utc_timestamp(installed_at_raw)
                if inst_dt > now:
                    issues.append(f"future installedAt timestamp for {norm_target_str}: {installed_at_raw}")
                    is_invalid = True
                elif (now - inst_dt) >= timedelta(days=7):
                    issues.append(f"installation for {norm_target_str} is stale ({installed_at_raw})")
                    is_stale = True

                if oldest_dt is None or inst_dt < oldest_dt:
                    oldest_dt = inst_dt
                    oldest_str = str(installed_at_raw)
            except (ValueError, TypeError) as exc:
                issues.append(f"invalid installedAt timestamp for {norm_target_str}: {installed_at_raw}: {exc}")
                is_invalid = True

            skill_file = target_path / "SKILL.md"
            if not skill_file.is_file():
                issues.append(f"installed SKILL.md missing: {skill_file}")
                is_invalid = True
            else:
                skill_file.read_text(encoding="utf-8")

            validated_targets.add(norm_target_str)

    if is_invalid:
        status = "invalid"
    elif is_stale:
        status = "stale"
        if oldest_str:
            issues.append(f"Oracle skills refresh is due (oldest: {oldest_str}); run skills sync -force")
    else:
        status = "current"

    return {
        "status": status,
        "oldestInstalledAt": oldest_str,
        "installationCount": len(validated_targets),
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("~/.dbtools/skills/skills.json"))
    parser.add_argument("--skills-root", dest="skills_roots", action="append", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = inspect_oracle_skills(args.registry, args.skills_roots)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = result["status"]
        if status == "current":
            print(f"Oracle skills are current; recorded installation date: {result['oldestInstalledAt']}.")
        elif status == "stale":
            print(f"Oracle skills refresh is due (oldest: {result['oldestInstalledAt']}); run skills sync -force.")
        else:
            print(f"Oracle skills status: {status}; issues: {'; '.join(result['issues'])}")
    return 0 if result["status"] == "current" else (1 if result["status"] == "stale" else 2)


if __name__ == "__main__":
    import sys
    sys.exit(main())
