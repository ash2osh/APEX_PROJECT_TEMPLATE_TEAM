#!/usr/bin/env python3
"""Offline readiness verification and schema-1 report generator for local APEX team projects.

Validates:
- Literal credential-free environment configuration matching supported loaders.
- Template release/ref records and manifest compatibility.
- Application directory structures, numeric app IDs, physical repository containment,
  and deployment descriptors.
- Recorded unresolved lock recovery records and upgrade conflict copies.
- Native SQLcl Oracle skills registry freshness and catalog completeness.
- Optional --live check that runs doctor and captures summary status without log leaks.

Default execution reads local files only: no SQLcl launches, network calls, file repair,
or database mutations.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# Readiness must not create cache files even in a freshly cloned project.
sys.dont_write_bytecode = True

if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from scripts.deployment_descriptor import DescriptorError, read_descriptor
from scripts.local_config import ConfigError, read_project_env
from scripts.oracle_skills_status import inspect_oracle_skills
from scripts.graphify_project import inspect_graphify
from scripts.upgrade_template import UpgradeError, load_manifest, read_lock_with_hash
from scripts.validate_app_source import validate_app_source


def is_original_template(repo_root: Path, manifest: Mapping | None) -> bool:
    """Recognize the original repository from local Git metadata, without Git/network calls."""
    if not manifest or not isinstance(manifest.get('upstream'),str): return False
    configuration = configparser.ConfigParser(interpolation=None)
    try:
        configuration.read_string((repo_root / '.git' / 'config').read_text(encoding='utf-8'))
        origin=configuration.get('remote "origin"','url')
    except (OSError,UnicodeError,configparser.Error):
        return False
    return origin.rstrip('/').removesuffix('.git') == manifest['upstream'].rstrip('/').removesuffix('.git')


def run_live_doctor(repo_root: Path, env_file: Path | None = None) -> dict:
    """Run read-only team doctor and map result into a clean summary without raw logs."""
    team_sh = repo_root / "scripts" / "team.sh"
    try:
        from scripts.sqlcl_session import bash_command
        bash_cmd = bash_command()
    except ImportError:
        bash_cmd = "bash"

    cmd = [bash_cmd, str(team_sh), "doctor"]
    env = os.environ.copy()
    if env_file is not None:
        env["PROJECT_ENV_FILE"] = str(env_file)

    try:
        res = subprocess.run(cmd, cwd=repo_root, env=env, capture_output=True, text=True, check=False)
        stdout = res.stdout or ""
        stderr = res.stderr or ""
        if res.returncode == 0:
            return {
                "status": "pass",
                "message": "live database identity, APEX release and workspace access verified",
            }
        combined = (stdout + "\n" + stderr).lower()
        if (
            "command not found" in combined
            or "not found" in combined
            or "cannot find" in combined
            or "unknown" in combined
            or "unavailable" in combined
            or "no such file or directory" in combined
        ):
            return {
                "status": "unavailable",
                "message": "live doctor verification unavailable: SQLcl or database connection not available",
            }
        return {
            "status": "fail",
            "message": "live doctor check failed for configured targets",
        }
    except Exception:
        return {
            "status": "unavailable",
            "message": "live doctor invocation unavailable: tool launch failed",
        }


def build_report(
    repo_root: Path,
    values: Mapping[str, str],
    skills: dict,
    *,
    env_path: Path | None = None,
    live_result: dict | None = None,
    config_error: str | None = None,
) -> dict:

    """Build the schema-1 readiness report.

    Exit codes:
    - 0: all required checks pass.
    - 1: attention or unavailable without a hard failure.
    - 2: invalid configuration/contract or failed verification.
    """
    checks: list[dict[str, str]] = []

    # 1. Oracle Skills check
    skills_status = skills.get("status", "missing")
    skills_path = skills.get("path", "~/.dbtools/skills/skills.json")
    if skills_status == "current":
        checks.append({
            "check": "oracle_skills",
            "status": "pass",
            "path": skills_path,
            "message": f"Oracle skills are current; recorded installation date: {skills.get('oldestInstalledAt')}",
        })
    elif skills_status in ("stale", "missing"):
        issues_text = "; ".join(skills.get("issues", [])) or "Oracle skills refresh is due; run skills sync -force"
        checks.append({
            "check": "oracle_skills",
            "status": "attention",
            "path": skills_path,
            "message": issues_text,
        })
    else:  # invalid
        issues_text = "; ".join(skills.get("issues", [])) or "Oracle skills registry or installations are invalid"
        checks.append({
            "check": "oracle_skills",
            "status": "fail",
            "path": skills_path,
            "message": issues_text,
        })

    # 2. Environment Configuration check
    if env_path is not None:
        actual_env_path = env_path
    else:
        env_override = os.environ.get("PROJECT_ENV_FILE")
        if env_override:
            cand = Path(env_override)
            actual_env_path = cand if cand.is_absolute() else (repo_root / cand).resolve()
        else:
            actual_env_path = (repo_root / ".env").resolve()

    if config_error:
        checks.append({
            "check": "environment_config",
            "status": "fail",
            "path": str(actual_env_path),
            "message": config_error,
        })
    elif values:
        checks.append({
            "check": "environment_config",
            "status": "pass",
            "path": str(actual_env_path),
            "message": "environment configuration is valid",
        })
    else:
        checks.append({
            "check": "environment_config",
            "status": "attention",
            "path": str(actual_env_path),
            "message": f"configuration file not found: {actual_env_path} (copy .env.example to .env)",
        })


    # 3. Template Manifest check
    manifest_path = repo_root / "template-manifest.json"
    manifest: dict | None = None
    if not manifest_path.is_file():
        checks.append({
            "check": "template_manifest",
            "status": "fail",
            "path": str(manifest_path),
            "message": "template-manifest.json not found",
        })
    else:
        try:
            manifest = load_manifest(repo_root)
            checks.append({
                "check": "template_manifest",
                "status": "pass",
                "path": str(manifest_path),
                "message": f"template manifest schema {manifest.get('schemaVersion')} verified (release: {manifest.get('apexRelease', '26.1')})",
            })
        except UpgradeError as exc:
            checks.append({
                "check": "template_manifest",
                "status": "fail",
                "path": str(manifest_path),
                "message": f"template manifest invalid: {exc}",
            })

    # Discover real applications in apps/
    real_apps: list[tuple[str, str, Path]] = []  # (schema, app_id_str, app_dir)
    apps_root = repo_root / "apps"
    if apps_root.is_dir():
        for schema_candidate in sorted(apps_root.iterdir()):
            if not schema_candidate.is_dir() or schema_candidate.name.startswith(".") or schema_candidate.name == "templates":
                continue
            for app_candidate in sorted(schema_candidate.iterdir()):
                if app_candidate.is_dir() and not app_candidate.name.startswith("."):
                    real_apps.append((schema_candidate.name, app_candidate.name, app_candidate))

    # 4. Template Lock check
    lock_path = repo_root / ".template-lock.json"
    is_downstream = lock_path.is_file() or bool(real_apps) or (bool(values) and not is_original_template(repo_root,manifest))

    if not is_downstream:
        checks.append({
            "check": "template_lock",
            "status": "pass",
            "path": str(lock_path),
            "message": "original template repository (no downstream lock required)",
        })
    elif not lock_path.is_file():
        checks.append({
            "check": "template_lock",
            "status": "fail",
            "path": str(lock_path),
            "message": ".template-lock.json is required in a downstream project",
        })
    else:
        try:
            lock, _ = read_lock_with_hash(repo_root)
            if "templateRef" not in lock:
                checks.append({
                    "check": "template_lock",
                    "status": "fail",
                    "path": str(lock_path),
                    "message": "legacy template lock is missing templateRef; pass --ref to choose an explicit template ref",
                })
            elif "apexRelease" not in lock or lock["apexRelease"] not in ("26.1", "26.2"):
                checks.append({
                    "check": "template_lock",
                    "status": "fail",
                    "path": str(lock_path),
                    "message": "template lock is missing apexRelease or has invalid release",
                })
            elif manifest and manifest.get("apexRelease") and lock["apexRelease"] != manifest["apexRelease"]:
                checks.append({
                    "check": "template_lock",
                    "status": "fail",
                    "path": str(lock_path),
                    "message": f"template lock apexRelease mismatch (lock: {lock['apexRelease']}, manifest: {manifest['apexRelease']})",
                })
            else:
                checks.append({
                    "check": "template_lock",
                    "status": "pass",
                    "path": str(lock_path),
                    "message": f"template lock verified (ref: {lock['templateRef']}, release: {lock['apexRelease']})",
                })
        except UpgradeError as exc:
            checks.append({
                "check": "template_lock",
                "status": "fail",
                "path": str(lock_path),
                "message": f"template lock invalid: {exc}",
            })

    # 5. Application and Deployment Descriptors check
    selected_env = values.get("DB_ENVIRONMENT", "development")

    for _schema_name, app_id_str, app_dir in real_apps:
        if not re.fullmatch(r"[1-9][0-9]{0,17}", app_id_str):
            checks.append({
                "check": f"app_{app_id_str}_contract",
                "status": "fail",
                "path": str(app_dir),
                "message": f"application directory must be a numeric ID: {app_id_str}",
            })
            continue

        app_id = int(app_id_str)

        # Source physical containment and presence check
        try:
            validate_app_source(repo_root, app_dir)
            app_apx = app_dir / "application.apx"
            if not app_apx.is_file():
                checks.append({
                    "check": f"app_{app_id}_source",
                    "status": "fail",
                    "path": str(app_apx),
                    "message": f"missing required application.apx in app {app_id} source tree",
                })
            else:
                checks.append({
                    "check": f"app_{app_id}_source",
                    "status": "pass",
                    "path": str(app_dir),
                    "message": f"application source physically contained and application.apx present: {app_id}",
                })
        except ValueError as exc:
            checks.append({
                "check": f"app_{app_id}_source",
                "status": "fail",
                "path": str(app_dir),
                "message": f"application source validation failed: {exc}",
            })

        # DEV descriptor check
        dev_desc = app_dir / "deployments" / "dev.json"
        if not dev_desc.is_file():
            checks.append({
                "check": f"app_{app_id}_descriptor_dev",
                "status": "fail",
                "path": str(dev_desc),
                "message": f"missing DEV deployment descriptor: {dev_desc}",
            })
        else:
            try:
                dev_data = read_descriptor(dev_desc, app_id)
                dev_schema = dev_data.get("app", {}).get("databaseSession", {}).get("parsingSchema")
                if dev_schema != _schema_name:
                    checks.append({
                        "check": f"app_{app_id}_descriptor_dev",
                        "status": "fail",
                        "path": str(dev_desc),
                        "message": f"DEV deployment descriptor parsingSchema ({dev_schema}) does not match source directory schema ({_schema_name})",
                    })
                else:
                    apex_schemas = [s.strip() for s in values.get("APEX_PARSING_SCHEMA", "").split(",") if s.strip()]
                    if apex_schemas and dev_schema not in apex_schemas:
                        checks.append({
                            "check": f"app_{app_id}_descriptor_dev",
                            "status": "fail",
                            "path": str(dev_desc),
                            "message": f"DEV deployment descriptor parsingSchema ({dev_schema}) is not in configured APEX_PARSING_SCHEMA ({values.get('APEX_PARSING_SCHEMA')})",
                        })
                    else:
                        checks.append({
                            "check": f"app_{app_id}_descriptor_dev",
                            "status": "pass",
                            "path": str(dev_desc),
                            "message": f"DEV deployment descriptor verified for app {app_id}",
                        })
            except DescriptorError as exc:
                checks.append({
                    "check": f"app_{app_id}_descriptor_dev",
                    "status": "fail",
                    "path": str(dev_desc),
                    "message": f"DEV deployment descriptor invalid: {exc}",
                })


        # Staging and Prod descriptors check
        for env_target in ("staging", "prod"):
            desc_file = app_dir / "deployments" / f"{env_target}.json"
            is_selected = (selected_env == "staging" and env_target == "staging") or (selected_env == "production" and env_target == "prod")

            if is_selected and not desc_file.is_file():
                checks.append({
                    "check": f"app_{app_id}_descriptor_{env_target}",
                    "status": "fail",
                    "path": str(desc_file),
                    "message": f"missing {env_target.upper()} deployment descriptor for selected environment {selected_env}: {desc_file}",
                })
            elif desc_file.is_file():
                try:
                    read_descriptor(desc_file, app_id)
                    checks.append({
                        "check": f"app_{app_id}_descriptor_{env_target}",
                        "status": "pass",
                        "path": str(desc_file),
                        "message": f"{env_target.upper()} deployment descriptor verified for app {app_id}",
                    })
                except DescriptorError as exc:
                    checks.append({
                        "check": f"app_{app_id}_descriptor_{env_target}",
                        "status": "fail",
                        "path": str(desc_file),
                        "message": f"{env_target.upper()} deployment descriptor invalid: {exc}",
                    })

    # Configured app IDs in .env check
    if values.get("APEX_APP_ID"):
        for id_str in values["APEX_APP_ID"].split(","):
            if not any(a[1] == id_str for a in real_apps):
                checks.append({
                    "check": f"configured_app_{id_str}",
                    "status": "fail",
                    "path": str(apps_root),
                    "message": f"configured application {id_str} directory not found under apps/",
                })

    # 6. Lock Recovery Records check
    locks_root = repo_root / ".sync-state" / "application-locks"
    if locks_root.is_dir():
        recovery_files = list(locks_root.glob("*/*/recovery.json"))
        unresolved: list[tuple[Path, str]] = []
        for rec in recovery_files:
            try:
                rec_data = json.loads(rec.read_text(encoding="utf-8"))
                st = rec_data.get("status")
                if st != "released":
                    unresolved.append((rec, str(st)))
            except Exception:
                unresolved.append((rec, "unreadable"))

        if unresolved:
            paths_str = ", ".join(f"{p.parent.name}/{p.name} ({s})" for p, s in unresolved)
            checks.append({
                "check": "recovery_records",
                "status": "attention",
                "path": str(unresolved[0][0]),
                "message": f"unresolved lock recovery evidence present: {paths_str}",
            })
        elif recovery_files:
            checks.append({
                "check": "recovery_records",
                "status": "pass",
                "path": str(locks_root),
                "message": "all local recovery records indicate past released state",
            })

    # 7. Upgrade Conflicts check (*.template-new)
    conflicts: list[Path] = []
    for root_str, dirs, files in os.walk(repo_root):
        root_path = Path(root_str)
        try:
            rel_path = root_path.relative_to(repo_root)
        except ValueError:
            continue
        dirs[:] = [
            d for d in dirs
            if d not in (".git", "scratch", ".venv-graphify", "node_modules", ".venv")
            and not (rel_path / d).parts[0] in (".git", "scratch", ".venv-graphify", ".venv")
        ]
        for f in files:
            if f.endswith(".template-new"):
                conflicts.append(root_path / f)

    if conflicts:
        checks.append({
            "check": "upgrade_conflicts",
            "status": "attention",
            "path": str(conflicts[0]),
            "message": f"upgrade conflict copy present: {conflicts[0].name}",
        })
    else:
        checks.append({
            "check": "upgrade_conflicts",
            "status": "pass",
            "path": str(repo_root),
            "message": "no upgrade conflicts present",
        })

    # Optional tooling stays offline: inspect installed metadata and local graph only.
    try:
        graphify=inspect_graphify(repo_root)
        state=graphify['status']
        checks.append({'check':'graphify','status':'pass' if state in ('absent','current') else 'attention',
                       'path':str(graphify['environment']),
                       'message':'optional Graphify '+state+': '+'; '.join(graphify['reasons'])})
    except (ValueError,OSError,TypeError,KeyError,RuntimeError):
        checks.append({'check':'graphify','status':'attention','path':'.venv-graphify',
                       'message':'configured Graphify inspection unavailable'})

    # 9. Live Doctor check (optional)
    if live_result:
        checks.append({
            "check": "live_doctor",
            "status": live_result["status"],
            "path": "scripts/doctor.sql",
            "message": live_result["message"],
        })

    # Calculate overall exit code
    has_fail = any(c["status"] == "fail" for c in checks)
    has_attention_or_unavailable = any(c["status"] in ("attention", "unavailable") for c in checks)

    if has_fail:
        exit_code = 2
    elif has_attention_or_unavailable:
        exit_code = 1
    else:
        exit_code = 0

    return {
        "schemaVersion": 1,
        "checks": checks,
        "exitCode": exit_code,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["text", "json"], default="text", help="output format (default: text)")
    parser.add_argument("--skills-root", dest="skills_roots", action="append", type=Path, help="loaded skill root directory")
    parser.add_argument("--live", action="store_true", help="run existing read-only team doctor check")
    parser.add_argument("--project-root", "--repo-root", dest="repo_root", type=Path, default=None, help="repository root path")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path.cwd()).resolve()

    # Resolve env path (support PROJECT_ENV_FILE override)
    env_override = os.environ.get("PROJECT_ENV_FILE")
    if env_override:
        cand = Path(env_override)
        env_path = cand if cand.is_absolute() else (repo_root / cand).resolve()
    else:
        env_path = (repo_root / ".env").resolve()

    values: dict[str, str] = {}
    config_error: str | None = None
    if env_path.is_file():
        try:
            values = read_project_env(env_path)
        except ConfigError as exc:
            config_error = str(exc)

    # Inspect Oracle skills
    registry_path = Path("~/.dbtools/skills/skills.json")
    skills = inspect_oracle_skills(registry_path, args.skills_roots)
    skills["path"] = str(Path(os.path.expanduser(str(registry_path))))

    # Live doctor check if requested
    live_result: dict | None = None
    if args.live:
        live_result = run_live_doctor(repo_root, env_file=env_path)

    report = build_report(
        repo_root,
        values,
        skills,
        env_path=env_path,
        live_result=live_result,
        config_error=config_error,
    )


    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        for c in report["checks"]:
            tag = c["status"].upper()
            print(f"[{tag}] {c['check']}: {c['message']} ({c['path']})")
        print(f"Readiness exit code: {report['exitCode']}")

    return report["exitCode"]


if __name__ == "__main__":
    sys.exit(main())
