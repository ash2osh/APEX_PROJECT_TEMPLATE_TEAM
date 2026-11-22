#!/usr/bin/env python3
"""Offline feature scaffolding, DEV stored-unit capture, review diffs, and capsule cleanup."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import sqlcl_pool
from scripts import feature_claims, feature_convert, feature_doctor, feature_sources, release_capsule, release_plan, release_state, rollout, terminal_confirm
from scripts.feature_manifest import FEATURE_RE, SCHEMA_RE, FeatureManifestError, load_feature_checkpoint
from scripts.path_utils import relative_to_root


class FeatureCliError(ValueError):
    """A feature command is incomplete, unsafe, or unavailable."""


def _root(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise FeatureCliError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise FeatureCliError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise FeatureCliError("repository root must be a directory")
    return root


def _init_feature(repo_root: Path, feature: str, schema: str) -> tuple[Path, ...]:
    if FEATURE_RE.fullmatch(feature) is None:
        raise FeatureCliError("feature must be a lowercase hyphenated slug")
    if SCHEMA_RE.fullmatch(schema) is None:
        raise FeatureCliError("schema must be an uppercase Oracle identifier")
    root = _root(repo_root)
    schema_dir, _scratch_dir = feature_sources._feature_scope(root, feature, schema)
    feature_dir = root / "features" / feature
    readme_path = feature_dir / "README.md"
    checks_path = schema_dir / "checks.json"
    readme_contents = feature_sources._ordinary_file(readme_path, root)
    existing_checks = feature_sources._ordinary_file(checks_path, root)
    if existing_checks is not None:
        raise FeatureCliError(
            f"feature scaffold refuses to overwrite existing file: {relative_to_root(checks_path, root)}"
        )

    outputs: dict[Path, bytes] = {}
    if readme_contents is None:
        outputs[readme_path] = (
            f"# {feature}\n\n"
            f"Feature source for schema `{schema}`. Keep stored source files whole and edit them in place.\n"
            "Replace the scaffold check with a reviewed condition before preparing a release.\n"
        ).encode()
    outputs[checks_path] = (
        b'{\n'
        b'  "schemaVersion": 1,\n'
        b'  "preconditions": [],\n'
        b'  "postconditions": [\n'
        b'    {\n'
        b'      "id": "replace-me",\n'
        b'      "sql": "SELECT 1 FROM dual WHERE 1 = 0",\n'
        b'      "expected": 1\n'
        b'    }\n'
        b'  ]\n'
        b'}\n'
    )

    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        feature_sources._ensure_directory(feature_dir, root)
        feature_sources._ensure_directory(schema_dir, root)
        for path, raw in outputs.items():
            stream = path.open("xb")
            with stream:
                descriptor_metadata = os.fstat(stream.fileno())
                path_metadata = path.lstat()
                file_identity = (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                if file_identity != (path_metadata.st_dev, path_metadata.st_ino):
                    raise FeatureCliError(f"feature scaffold path changed during creation: {relative_to_root(path, root)}")
                created.append((path, file_identity))
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as error:
        for path, file_identity in reversed(created):
            try:
                metadata = path.lstat()
                if (
                    feature_sources._is_reparse(path, metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != file_identity
                ):
                    continue
                path.unlink()
            except OSError:
                pass
        if isinstance(error, (FeatureCliError, feature_sources.FeatureSourcesError)):
            raise
        raise FeatureCliError(f"feature scaffold failed; created files were removed: {error}") from error
    return tuple(path for path, _identity in created)


def _freeze_gate(
    root: Path,
    environ: Mapping[str, str],
    steps: Sequence[Mapping],
    no_live: str | None,
) -> tuple[int | None, dict]:
    """Ownership and DEV-drift checks before a capsule is built. Returns an exit code to stop with, or None."""
    if no_live is not None and not no_live.strip():
        raise FeatureCliError("--no-live needs a reason")
    selected = {
        PurePosixPath(path).parts[1]
        for step in steps if step["type"] == "feature-migrate"
        for path in step["inputs"] if len(PurePosixPath(path).parts) > 1 and PurePosixPath(path).parts[0] == "features"
    }
    claims, unanalysed = feature_claims.collect_claims(root)
    record: dict = {"features": sorted(selected)}
    ownership = feature_doctor.ownership_findings(claims, selected) + feature_doctor.same_feature_findings(claims, selected)
    findings = list(ownership)
    record["ownership"] = "refused" if ownership else "no object is held by two selected features or defined twice by one of them"
    # A file that cannot be analysed has unknown objects, so no claim set built from the features can be trusted.
    analysis = feature_doctor.unanalysed_findings(unanalysed)
    findings.extend(analysis)
    record["analysis"] = (
        f"refused: {len(analysis)} feature file(s) could not be analysed" if analysis else "every feature SQL file was analysed"
    )
    live_schemas = sorted({
        claim.schema for claim in claims
        if claim.feature in selected and claim.kind in feature_doctor.STORED_SOURCE_KINDS
    })
    if analysis:
        record["live"] = "not run: a feature file could not be analysed"
    elif no_live is not None:
        record["live"] = f"skipped: {no_live.strip()}"
    elif not live_schemas:
        record["live"] = "not run: no stored source files in the selected features"
    else:
        usable = [schema for schema in live_schemas if feature_doctor._dev_profile_available(environ, schema)]
        skipped = sorted(set(live_schemas) - set(usable))
        if not usable:
            record["live"] = "not run: no DEV profile for " + ", ".join(skipped)
            print(f"note: live comparison with DEV {record['live']}", file=sys.stderr)
        else:
            try:
                live_list, compared, accepted = feature_doctor.live_findings(root, environ, claims, selected, set(usable))
            except feature_doctor.FeatureDoctorError as error:
                print(
                    f"feature error: {error}\nfreeze did not compare stored source files with DEV; fix the connection or pass "
                    "--no-live \"<reason>\"",
                    file=sys.stderr,
                )
                return 2, record
            findings.extend(live_list)
            record["live"] = f"{compared} stored source file(s) compared with DEV, {accepted} accepted difference(s)"
            if skipped:
                record["live"] += "; not compared (no DEV profile): " + ", ".join(skipped)
                print(
                    f"note: no DEV profile for {', '.join(skipped)}; their stored source files were not compared with DEV",
                    file=sys.stderr,
                )
    if any(finding.level == feature_doctor.ERROR for finding in findings):
        print(feature_doctor.render(findings, [
            f"freeze refused: ownership - {record['ownership']}; analysis - {record['analysis']}; live - {record.get('live', 'not run')}",
        ]))
        return 1, record
    return None, record


def _write_freeze_record(capsule_root: Path, record: dict) -> None:
    """Keep what the freeze checked beside the capsules (outside the capsule and its digest).

    Not inside scratch/release-capsules: the rollout looks there for the entry named after a payload digest.
    """
    target = capsule_root.parent.parent / "freeze-checks" / f"{capsule_root.name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Freeze checks: ownership - {record['ownership']}; analysis - {record['analysis']}; live - {record['live']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create an offline feature scaffold")
    init_parser.add_argument("feature", help="lowercase feature slug")
    init_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")

    capture_parser = subparsers.add_parser("capture", help="capture a complete stored unit from DEV")
    capture_parser.add_argument("feature", help="lowercase feature slug")
    unit_parser = capture_parser.add_mutually_exclusive_group(required=True)
    unit_parser.add_argument("--package", metavar="NAME", help="package specification and body")
    unit_parser.add_argument("--trigger", metavar="NAME", help="trigger")
    unit_parser.add_argument("--procedure", metavar="NAME", help="standalone procedure")
    unit_parser.add_argument("--function", metavar="NAME", help="standalone function")
    unit_parser.add_argument("--type", metavar="NAME", help="object type specification and optional body")
    capture_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")
    capture_parser.add_argument("--env", required=True, action="append", choices=("dev",), help="stored-unit capture is DEV-only")
    capture_parser.add_argument("--refresh", action="store_true", help="replace source and its retained DEV baseline after review")
    capture_parser.add_argument("--move-from", metavar="FEATURE", help="take the unit over from the feature that holds it")
    capture_parser.add_argument(
        "--discard-unapplied", action="store_true",
        help="allow a refresh or move to erase lines the file has and DEV does not",
    )
    capture_parser.add_argument(
        "--keep-file", action="store_true",
        help="with --move-from, move the old file unchanged instead of capturing from DEV",
    )

    diff_parser = subparsers.add_parser("diff", help="write stored source diffs against the retained DEV capture")
    diff_parser.add_argument("feature", help="lowercase feature slug")
    diff_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")
    diff_parser.add_argument("--day", default=date.today().isoformat(), help="review directory date, YYYY-MM-DD")

    apply_parser = subparsers.add_parser("apply", help="prepare or execute a guarded DEV feature plan")
    apply_parser.add_argument("feature", help="lowercase feature slug")
    apply_parser.add_argument("--schema", required=True, action="append", help="one Oracle schema")
    apply_parser.add_argument("--env", required=True, action="append", choices=("dev",))
    apply_mode = apply_parser.add_mutually_exclusive_group(required=True)
    apply_mode.add_argument("--prepare", action="store_true", help="read DEV and write a private plan")
    apply_mode.add_argument("--execute-plan", metavar="FILE", help="apply the reviewed plan to DEV")
    apply_mode.add_argument("--status", action="store_true", help="read retained DEV attempts offline")
    apply_parser.add_argument("--unit", action="append", default=[], metavar="TYPE:NAME")
    apply_parser.add_argument("--checks", action="append", default=[], metavar="PATH")
    apply_parser.add_argument("--accept-live-baseline", metavar="SHA256")

    freeze_parser = subparsers.add_parser("freeze", help="validate committed inputs and build a frozen release capsule")
    freeze_parser.add_argument("manifest", type=Path, help="schema-version 2 rollout manifest")
    freeze_parser.add_argument(
        "--no-live", metavar="REASON",
        help="skip the comparison of stored source files with DEV; the reason is recorded next to the capsule",
    )

    doctor_parser = subparsers.add_parser("doctor", help="check feature ownership, orphan checks and, with --live, drift against DEV")
    doctor_parser.add_argument("--feature", help="limit the checks to one feature slug")
    doctor_parser.add_argument("--live", action="store_true", help="compare stored source files with the DEV source (read-only)")

    clean_parser = subparsers.add_parser("clean-capsule", help="preview or remove an eligible local capsule")
    clean_parser.add_argument("payload_digest", help="64-character capsule payload digest")
    clean_parser.add_argument("-y", "--yes", action="store_true", help="apply eligible local cleanup")
    reconcile_parser = subparsers.add_parser(
        "reconcile-run",
        help="close an unfinished rollout run that has no checkpoint to recover, after an acknowledgment",
    )
    reconcile_parser.add_argument("run_id", help="rollout run id from the local release state")
    reconcile_parser.add_argument("--env", required=True, choices=("dev", "staging", "prod"), help="the run's environment")
    reconcile_parser.add_argument("--evidence", type=Path, help="reviewed evidence file for DEV reconciliation")
    reconcile_parser.add_argument("-y", "--yes", action="store_true", help="accept the acknowledgment without typing at a terminal")
    convert_parser = subparsers.add_parser(
        "convert-plan",
        help="offline, read-only plan for moving dated migration folders into feature files",
    )
    convert_parser.add_argument("folders", nargs="+", metavar="migration-folder", help="migrations/<dated-folder> or migrations/<SCHEMA>/<dated-folder>")
    convert_parser.add_argument("--feature", metavar="SLUG", help="feature slug to print in the capture commands (default: <slug>)")
    convert_parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    plan_parser = subparsers.add_parser(
        "release-plan",
        help="offline plan of the next schema-version 2 release manifest from committed changes",
    )
    plan_source = plan_parser.add_mutually_exclusive_group()
    plan_source.add_argument("--since", metavar="REF", help="git ref of the last shipped release (default: newest release/* tag)")
    plan_source.add_argument("--all", action="store_true", help="first release: every tracked feature, migration and app file")
    plan_parser.add_argument("--output", type=Path, default=release_plan.DEFAULT_OUTPUT, help="manifest path inside the repository")
    plan_parser.add_argument("--force", action="store_true", help="replace an existing output file")
    plan_parser.add_argument("--format", choices=("text", "json"), default="text", help="summary format")
    return parser


def _reconcile_ack_from_terminal(expected: str) -> str:
    print(
        f"Type {expected} to acknowledge that the original process and database operation have ended. "
        "Without --yes, only the developer types it at a terminal; nothing else closes the run.",
        file=sys.stderr,
    )
    if not terminal_confirm.may_read_acknowledgment():
        return ""
    try:
        return input("Acknowledgment: ")
    except (EOFError, OSError):
        return ""


def _render_run(run: Mapping) -> str:
    lines = [
        f"Run {run['runId']} ({run['environment']})",
        f"  payload SHA-256: {run['payloadDigest']}",
        f"  plan SHA-256: {run['planDigest']}",
        f"  state: {run['state']}; databaseState: {run['databaseState']}",
        f"  started: {run['startedAt']}; finished: {run['finishedAt'] or 'not finished'}",
    ]
    lines.extend(f"  target scope: {json.dumps(scope, ensure_ascii=False, sort_keys=True)}" for scope in run["targetScopes"])
    for step in run["steps"]:
        receipt = f"; receipt {step['receiptPath']}" if step.get("receiptPath") else ""
        lines.append(f"  step {step['step']}: {step['status']}{receipt}")
    lines.append("  per-run evidence is under scratch/rollout-runs/, and the run's report is named in the rollout output")
    return "\n".join(lines)


def _reconcile_run(
    root: Path,
    run_id: str,
    environment: str,
    read_acknowledgment: Callable[[str], str],
    *,
    yes: bool = False,
) -> int:
    """Close an unfinished run after the acknowledgment: 0 closed, 1 declined or wrong, 2 refused or error."""
    try:
        with release_state.run_reconciliation(root, run_id, environment) as session:
            print(_render_run(session.run))
            expected = release_state.RUN_RECONCILE_PREFIX + run_id
            if yes:
                ack = expected
                acknowledged_by = "--yes"
            else:
                ack = read_acknowledgment(expected) or ""
                acknowledged_by = "terminal"
            closed = session.reconcile(ack, acknowledged_by=acknowledged_by)
    except release_state.AcknowledgmentError as error:
        print(f"feature error: {error}", file=sys.stderr)
        return 1
    except release_state.ReleaseStateError as error:
        print(f"feature error: {error}", file=sys.stderr)
        return 2
    print(
        f"Reconciled run {run_id}: state {closed['state']}, databaseState {closed['databaseState']}. "
        "It no longer occupies its targets, and it is not proof of completion.",
    )
    return 0


@sqlcl_pool.with_deployment_sessions
def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
    read_acknowledgment: Callable[[str], str] | None = None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    environment = os.environ if environ is None else environ
    try:
        root = _root(args.repo_root if args.repo_root is not None else repo_root)
        if args.command == "init":
            created = _init_feature(root, args.feature, args.schema)
            for path in created:
                print(f"Created {relative_to_root(path, root).as_posix()}")
            print("Replace the scaffold postcondition with a reviewed check before release.")
            return 0
        if args.command == "capture":
            if len(args.env) != 1:
                raise FeatureCliError("feature capture accepts only one --env value")
            report: list[str] = []
            options = {
                "refresh": args.refresh, "move_from": args.move_from, "discard_unapplied": args.discard_unapplied,
                "keep_file": args.keep_file, "report": report,
            }
            if args.package is not None:
                paths = feature_sources.capture_package(root, args.feature, args.schema, args.package, environ=environment, **options)
            else:
                group, unit = next(
                    (name, value) for name, value in (
                        ("trigger", args.trigger), ("procedure", args.procedure), ("function", args.function), ("type", args.type),
                    ) if value is not None
                )
                paths = feature_sources.capture_unit(root, args.feature, args.schema, group, unit, environ=environment, **options)
            for path in paths:
                print(f"Captured {relative_to_root(path, root).as_posix()}")
            for line in report:
                print(line)
            return 0
        if args.command == "doctor":
            if args.feature is not None and FEATURE_RE.fullmatch(args.feature) is None:
                raise FeatureCliError("feature must be a lowercase hyphenated slug")
            findings, summary = feature_doctor.doctor(root, environment, feature=args.feature, live=args.live)
            print(feature_doctor.render(findings, summary))
            return feature_doctor.exit_code(findings)
        if args.command == "diff":
            artifacts = feature_sources.diff_captured_package(
                root, args.feature, args.schema, day=args.day,
            )
            for path in artifacts:
                print(f"Wrote {relative_to_root(path, root).as_posix()}")
            return 0
        if args.command == "apply":
            from scripts import feature_dev

            if len(args.env) != 1 or len(args.schema) != 1:
                raise FeatureCliError("feature apply requires exactly one --env dev and one --schema")
            if not args.prepare and (args.unit or args.checks or args.accept_live_baseline is not None):
                raise FeatureCliError("--unit, --checks and --accept-live-baseline require --prepare")
            try:
                if args.status:
                    return feature_dev.dev_apply_status(root, args.feature, args.schema[0])
                if args.execute_plan:
                    return feature_dev.execute_dev_apply(root, args.feature, args.schema[0], args.execute_plan, environ=environment)
                path = feature_dev.prepare_dev_apply(
                    root, args.feature, args.schema[0], args.unit, environ=environment,
                    check_paths=args.checks, accepted_baseline_sha256=args.accept_live_baseline,
                )
                plan = json.loads(path.read_bytes())
            except feature_dev.DevPrepareRefusal as error:
                print(f"feature prepare refused: {error}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                return feature_dev.interrupted_execute_status(root, args.execute_plan) if args.execute_plan else 130
            except (OSError, ValueError) as error:
                raise FeatureCliError(str(error)) from error
            target, scope = plan["target"], plan["targetScope"]
            print(f"DEV {target['profile']} profile via {target['connection']} as {target['expectedUser']}")
            print(f"Target {scope['dbUniqueName']} / {scope['containerId']} / {scope['schema']} / {scope['edition']}")
            if target["configurationFile"] is not None:
                print(f"Configuration {target['configurationFile']['path']}")
            for unit, guard in zip(plan["units"], plan["sourceGuards"], strict=True):
                print(f"{unit['type']} {unit['name']} ({'no-op' if guard['noOp'] else 'apply planned'})")
            for owner, name, kind, subobject in plan["structuralPlan"]["prerequisiteKeys"]:
                print(f"Dependency {kind} {owner}.{name}" + (f".{subobject}" if subobject else ""))
            print(f"Plan SHA-256 {plan['planSha256']}")
            print(f"Plan {relative_to_root(path, root).as_posix()}")
            print(f"Source and settings review {relative_to_root(path.parent / plan['reviewPath'], root).as_posix()}")
            return 0
        if args.command == "freeze":
            manifest = rollout.load_manifest(args.manifest, root, version=2)
            checkpoints = tuple(
                load_feature_checkpoint(root, step)
                for step in manifest.steps
                if step["type"] == "feature-migrate"
            )
            refused, record = _freeze_gate(root, environment, manifest.steps, args.no_live)
            if refused is not None:
                return refused
            capsule = release_capsule.build_capsule(root, manifest.path, checkpoints, {})
            record["migrationOnly"] = not checkpoints
            record["migrationSteps"] = sum(step["type"] == "migrate" for step in manifest.steps)
            _write_freeze_record(capsule.root, record)
            print(f"Payload SHA-256: {capsule.payload_digest}")
            print(f"Workflow engine SHA-256: {capsule.engine_digest}")
            print(f"Source commit: {capsule.source_commit}")
            print(f"Capsule: {relative_to_root(capsule.root, root).as_posix()}")
            return 0
        if args.command == "clean-capsule":
            result = release_state.cleanup_capsule(root, args.payload_digest, apply=args.yes)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.command == "reconcile-run":
            if args.env == "dev":
                from scripts import feature_dev

                if args.evidence is None:
                    raise FeatureCliError("DEV reconciliation requires --evidence")
                try:
                    return feature_dev.reconcile_dev_apply(
                        root, args.run_id, args.evidence, environ=environment,
                        read_acknowledgment=read_acknowledgment or _reconcile_ack_from_terminal, yes=args.yes,
                    )
                except KeyboardInterrupt:
                    return 130
                except (OSError, ValueError) as error:
                    raise FeatureCliError(str(error)) from error
            if args.evidence is not None:
                raise FeatureCliError("--evidence is available only for DEV reconciliation")
            return _reconcile_run(
                root, args.run_id, args.env, read_acknowledgment or _reconcile_ack_from_terminal,
                yes=args.yes,
            )
        if args.command == "release-plan":
            plan = release_plan.create_release_plan(
                root, since=args.since, include_all=args.all, output=args.output, force=args.force,
            )
            print(release_plan.render(plan, args.format))
            return 0
        if args.command == "convert-plan":
            if args.feature is not None and FEATURE_RE.fullmatch(args.feature) is None:
                raise FeatureCliError("feature must be a lowercase hyphenated slug")
            report = feature_convert.convert_plan(root, args.folders, feature=args.feature)
            print(feature_convert.render(report, args.format))
            return feature_convert.exit_code(report)
        raise FeatureCliError(f"unsupported feature command: {args.command}")
    except (
        FeatureCliError, feature_sources.FeatureSourcesError, FeatureManifestError, feature_doctor.FeatureDoctorError,
        release_state.ReleaseStateError, release_capsule.ReleaseCapsuleError, rollout.RolloutError,
        release_plan.ReleasePlanError,
    ) as error:
        print(f"feature error: {error}", file=sys.stderr)
        if isinstance(error, release_state.ReleaseStateError) and str(error).startswith("capsule cleanup refused:"):
            return 1
        return 2


if __name__ == "__main__":
    raise SystemExit(main(repo_root=Path(__file__).resolve().parents[1]))
