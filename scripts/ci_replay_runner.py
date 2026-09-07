#!/usr/bin/env python3
"""Reference replay adapter used by the disposable CI contract.

The adapter intentionally requires saved SQLcl aliases supplied by the
provisioner/runner environment. It probes every profile through run_sqlcl and
applies the selected migration members only to the disposable target. APEX
application/browser checks remain project declarations and are not inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from teamlib.config import load_config, profile_target
from teamlib.control_store import SqlControlStore
from teamlib.deploy import deploy_app
from teamlib.live_inventory import inventory_target
from teamlib.migration_store import SqlMigrationStore
from teamlib.migrate import apply_plan
from teamlib.app_checks import AppCheckError, verify_candidate_apps
from teamlib.release import ReleaseError, release_app_trees, release_migration_files, verify_release
from teamlib.sqlcl import run_sqlcl
from teamlib.trees import read_git_tree


def _source_app_aliases(repo: Path, ref: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref, "--", "apps"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "could not inspect selected source applications")
    aliases = []
    for line in result.stdout.splitlines():
        parts = line.split("/")
        if len(parts) >= 3 and parts[0] == "apps" and parts[1]:
            aliases.append(parts[1])
    return tuple(sorted(set(aliases)))


def _master_first(aliases: tuple[str, ...], repo: Path) -> tuple[str, ...]:
    contract_path = repo / "targets" / "masters.json"
    masters: list[str] = []
    if contract_path.is_file():
        try:
            value = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit("master contract is unreadable") from exc
        entries = value.get("masters", []) if isinstance(value, dict) else []
        if not isinstance(entries, list):
            raise SystemExit("master contract masters must be a list")
        masters = [item["alias"] for item in entries if isinstance(item, dict) and isinstance(item.get("alias"), str)]
    return tuple(alias for alias in masters if alias in aliases) + tuple(alias for alias in aliases if alias not in masters)


def _check_declarations(repo: Path, aliases: tuple[str, ...]) -> dict[str, Path]:
    declarations: dict[str, Path] = {}
    root = repo / "ci" / "app-checks"
    for alias in aliases:
        path = root / f"{alias}.json"
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"candidate application declaration is missing: {path}")
        declarations[alias] = path
    return declarations


def _materialize_migrations(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=False)
    for relative, data in files.items():
        path = root / relative
        if path.parent != root or not relative.endswith((".sql", ".verify.sql")):
            raise SystemExit(f"unsafe packaged migration member: {relative}")
        path.write_bytes(data)
    return root


def _assert_exact_checkout(repo: Path, ref: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != ref:
        raise SystemExit("replay runner checkout is not the exact selected source SHA")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise SystemExit("replay runner checkout status could not be read")
    allowed_ignored = ("scratch/", ".sync-state/", ".env", ".env.")
    unexpected: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if code == "!!" and (
            path in allowed_ignored
            or path.startswith(allowed_ignored)
            or "__pycache__/" in path
            or path.endswith(".pyc")
        ):
            continue
        unexpected.append(line)
    if unexpected:
        raise SystemExit("replay runner checkout is not clean; refusing to mix local files with the selected SHA")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-replay-runner")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--target-json", required=True)
    parser.add_argument("--previous")
    args = parser.parse_args(argv)
    target_data = json.loads(Path(args.target_json).read_text(encoding="utf-8"))
    if target_data.get("status") != "disposable" or target_data.get("source_commit") != args.ref:
        raise SystemExit("replay target is not a disposable instance for the selected source")
    env_path = Path(target_data["env_path"])
    config = load_config(env_path, require_verify=True)
    if config.role != "replay" or config.environment == "production":
        raise SystemExit("replay environment must use the replay role and a non-production environment")
    work = Path(tempfile.mkdtemp(prefix="team-ci-replay-"))
    try:
        from teamlib.apex import _default_repo
        repo = _default_repo(Path.cwd())
        _assert_exact_checkout(repo, args.ref)
        identity_driver = repo / "scripts" / "sql" / "identity.sql"
        for profile in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY"):
            target = profile_target(config, profile, alias=next(iter(config.apps)) if profile == "APEX" else None)
            run_sqlcl(target, "read", identity_driver, work)
        migration_root = repo / "migrations"
        if not migration_root.is_dir():
            raise SystemExit("selected source has no migrations directory")
        metadata = profile_target(config, "METADATA")
        migration_store = SqlMigrationStore(profile_target(config, "METADATA"), work_root=work / "metadata")
        schema_set_digest = hashlib.sha256(
            f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
        ).hexdigest()
        migration_store.bootstrap(metadata, schema_set_digest=schema_set_digest)

        def run_migration_source(source_root: Path, source_commit: str):
            def execute(migration):
                payload_target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
                run_sqlcl(payload_target, "write", migration.sql_path, work)

            def verify(migration):
                if migration.verify_path is not None and migration.verify_bytes.strip():
                    run_sqlcl(profile_target(config, "VERIFY"), "read", migration.verify_path, work)
                return True

            def observe(_migration, phase):
                return inventory_target(
                    profile_target(config, "TABLES"),
                    config.tables_schema,
                    config.code_schema,
                    work / "inventory" / source_commit / phase,
                    schema_set_digest=schema_set_digest,
                )

            return apply_plan(
                source_root,
                {
                    "target": metadata,
                    "store": migration_store,
                    "bootstrap": False,
                    "execute": execute,
                    "verify": verify,
                    "observe": observe,
                    "require_observation": True,
                    "schema_set_digest": schema_set_digest,
                    "source_commit": source_commit,
                    "applied_by": "ci-replay",
                },
            )

        previous_report = None
        previous_apps = {}
        if args.previous:
            try:
                previous_manifest = verify_release(args.previous)
                previous_files = release_migration_files(args.previous)
                previous_apps = release_app_trees(args.previous)
            except ReleaseError as exc:
                raise SystemExit(f"previous release is invalid: {exc}") from exc
            previous_root = _materialize_migrations(work / "previous-migrations", previous_files)
            previous_report = run_migration_source(previous_root, previous_manifest.source_commit)

        current_report = run_migration_source(migration_root, args.ref)

        def deploy_tree(alias, tree, source_commit, control_store):
            target = profile_target(config, "APEX", alias=alias)
            return deploy_app(
                target,
                tree,
                source_commit,
                {"verified": True, "target_key": target.physical_key},
                repo=repo,
                root=work / "application-state",
                control_store=control_store,
            )

        aliases = _source_app_aliases(repo, args.ref)
        if previous_apps and set(previous_apps) != set(aliases):
            raise SystemExit("previous-release application set differs from selected source; explicit app removal adapter is required")
        app_report = {
            "status": "PASS",
            "coverage": {"apps": [], "pages": {}, "checks": 0, "unknown": 0},
            "checks_digest": "none",
            "results": [],
        }
        if aliases:
            declarations = _check_declarations(repo, aliases)
            control_store = SqlControlStore(metadata, work_root=work / "metadata")
            app_targets = [profile_target(config, "APEX", alias=alias) for alias in aliases]
            control_store.setup_state(app_targets)
            for alias in _master_first(aliases, repo):
                if previous_apps:
                    deploy_tree(alias, previous_apps[alias], previous_manifest.source_commit, control_store)
                deploy_tree(alias, read_git_tree(repo, args.ref, alias), args.ref, control_store)
            replay_target = {
                **target_data,
                "role": config.role,
                "environment": config.environment,
                "instance_id": config.profiles["TABLES"].expected_instance_id,
                "app_ids": config.apps,
            }
            try:
                report = verify_candidate_apps(
                    {"commit": args.ref, "apps": {alias: {"app_id": config.apps.get(alias)} for alias in aliases}},
                    replay_target,
                    declarations,
                )
            except AppCheckError as exc:
                raise SystemExit(str(exc)) from exc
            app_report = report.as_dict()

        print(json.dumps({
            "status": "PASS",
            "source_commit": args.ref,
            "fresh": "PASS",
            "upgrade": "PASS" if args.previous else "NOT_APPLICABLE_INITIAL_RELEASE",
            "migrations": list(current_report.applied),
            "previous_migrations": list(previous_report.applied) if previous_report else [],
            "application_checks": app_report,
        }, sort_keys=True))
        return 0

    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
