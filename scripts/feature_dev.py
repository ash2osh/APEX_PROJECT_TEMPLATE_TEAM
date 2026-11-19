"""Prepare a private, read-only DEV plan for selected feature source."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import feature_claims, feature_dev_views, feature_manifest, feature_preflight, feature_release, feature_sources
from . import migration_attempts
from . import migration_checks, migration_manifest, migration_revision, release_capsule, release_ownership, release_state
from .db_targets import migration_profile_keys, resolve_target
from .feature_manifest import FeatureCheckpoint, FeatureInput
from .feature_release import _canonical_bytes, _sha256
from .path_utils import relative_to_root


class DevPrepareRefusal(ValueError):
    """Current evidence or a verified precondition declined preparation."""


PLAN_ID = re.compile(r"prepare-[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
ATTEMPT_DIRECTORY = "releases/.dev-apply-attempts"
PLAN_FIELDS = {
    "schemaVersion", "planId", "feature", "target", "observedIdentity", "targetScope", "units",
    "checks", "engine", "beforeState", "beforeStateSha256", "sourceGuards", "structuralPlan", "noOp",
    "reviewPath", "reviewSha256", "planSha256",
}


def _attempts(root):
    directory = root / ATTEMPT_DIRECTORY
    try:
        directory.lstat()
    except FileNotFoundError:
        return {}
    feature_release._safe_repo_path(root, directory, "DEV attempt directory")
    if not directory.is_dir():
        raise ValueError("DEV attempt directory is not a directory")
    attempts = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            record = migration_manifest.decode_json(feature_sources._ordinary_file(path, root), "DEV attempt marker")
            if (
                not isinstance(record, dict) or type(record.get("schemaVersion")) is not int
                or record["schemaVersion"] != 1 or not PLAN_ID.fullmatch(path.stem)
                or record.get("planId") != path.stem or record.get("state") not in {"pending", "verified", "not-started", "reconciled"}
                or not feature_release.HASH_RE.fullmatch(record["planSha256"])
                or not feature_manifest.FEATURE_RE.fullmatch(record["feature"])
                or not isinstance(record.get("evidencePaths"), list) or not record["evidencePaths"]
            ):
                raise ValueError("malformed state, identity or digest")
            scope = release_ownership._scope_record(record["targetScope"])
            plan_path = f"scratch/feature-dev/{path.stem}/plan.json"
            if record["planPath"] != plan_path or plan_path != record["evidencePaths"][0]:
                raise ValueError("marker has a mismatched plan path")
            for evidence in record["evidencePaths"]:
                parts = feature_manifest._safe_parts(evidence, "DEV attempt evidence")
                if parts[:3] != ("scratch", "feature-dev", path.stem):
                    raise ValueError("marker evidence is outside its plan directory")
            if not feature_manifest.SCHEMA_RE.fullmatch(scope["schema"]):
                raise ValueError("marker has an invalid target schema")
            if record["state"] == "verified" and (
                type(record.get("noOp")) is not bool or not feature_release.HASH_RE.fullmatch(record["reportSha256"])
            ):
                raise ValueError("verified marker is missing its receipt fields")
            if record["state"] == "reconciled" and (
                record.get("acknowledgedBy") not in ("terminal", "--yes")
                or record.get("conclusion") not in ("before", "intended")
                or not feature_release.HASH_RE.fullmatch(record["evidenceSha256"])
            ):
                raise ValueError("reconciled marker is missing its closure evidence")
        except (KeyError, TypeError, OSError, ValueError) as error:
            raise ValueError(f"unreadable DEV attempt marker {path.name}: {error}") from error
        attempts[path.stem] = record
    return attempts


def _assert_dev_available(root, scope, plan_id=None):
    attempts = _attempts(root)
    if plan_id in attempts:
        raise ValueError(f"DEV plan {plan_id} is consumed or unresolved; prepare a fresh plan")
    for attempt in attempts.values():
        if attempt["targetScope"] == scope and attempt["state"] == "pending":
            raise ValueError(f"unresolved DEV apply {attempt['planId']}; inspect feature apply --status and reconcile it")
    _assert_no_pending_release(root, scope)


def dev_apply_status(repo_root, feature, schema):
    """Report every local attempt for the schema without selecting a saved connection."""
    root = feature_release._root(repo_root)
    feature_sources._feature_scope(root, feature, schema)
    attempts = [record for record in _attempts(root).values() if record["targetScope"]["schema"] == schema]
    if not attempts:
        print(f"No retained DEV feature attempts for {schema}.")
    for record in attempts:
        scope = record["targetScope"]
        print(f"{record['planId']} {record['state']} {record['feature']} {scope['dbUniqueName']}/{scope['containerId']}/{schema}/{scope['edition']}")
        for evidence in record["evidencePaths"]:
            if feature_sources._ordinary_file(root / evidence, root) is None:
                print(f"  Missing evidence {evidence}")
    return int(any(record["state"] == "pending" for record in attempts))


def interrupted_execute_status(root, plan_path):
    try:
        marker = _attempts(root).get(Path(plan_path).parent.name)
    except (OSError, ValueError):
        return 2
    if marker is None or marker["state"] == "not-started":
        return 130
    if marker["state"] == "verified":
        return 0
    return 2


def _selected_checkpoint(root, feature, schema, selectors, check_paths):
    feature_dir, _ = feature_sources._feature_scope(root, feature, schema)
    feature_release._safe_repo_path(root, feature_dir, "feature schema directory")
    if not selectors or isinstance(selectors, str):
        raise ValueError("prepare requires at least one --unit TYPE:NAME")
    units = []
    for selector in selectors:
        kind, _, spelling = selector.partition(":")
        kind = " ".join(kind.upper().split())
        if kind != "VIEW" and kind not in feature_manifest.STORED_SOURCE_TYPES:
            raise ValueError(f"select a supported unit type in --unit TYPE:NAME: {kind}")
        unit = (schema, kind, feature_sources._unit_name(spelling, "unit"))
        if unit in units:
            raise ValueError(f"cannot repeat a selected unit: {selector}")
        units.append(unit)

    claims, unanalysed = feature_claims.collect_claims(root)
    for report in unanalysed:
        if Path(report.path).parts[2] == schema:
            raise ValueError(f"cannot determine feature ownership for {report.path}: {report.reason}")
    by_unit = {}
    inputs = []
    for unit in units:
        matching = [claim for claim in claims if claim.key == unit]
        if len(matching) != 1 or matching[0].feature != feature:
            raise ValueError(f"expected one file owned by {feature} for {unit[1]} {unit[2]}; review feature ownership")
        relative, path = feature_manifest._resolve_file(root, matching[0].path, "feature source")
        raw = feature_manifest._read_feature_source(path, relative)
        item = _selected_input(relative, raw, unit)
        by_unit[unit] = item
        inputs.append(item)

    # Move a selected specification ahead of its body if the user named the body first.
    selected = {}
    for item in inputs:
        owner, kind, name = item.unit
        spec = by_unit.get((owner, kind.removesuffix(" BODY"), name)) if kind.endswith(" BODY") else None
        if spec:
            selected[spec.path] = spec
        selected[item.path] = item

    paths = tuple(check_paths) or (f"features/{feature}/{schema}/checks.json",)
    if len(set(paths)) != len(paths):
        raise ValueError("cannot repeat a selected check file")
    checks = []
    check_hashes = []
    for requested in paths:
        slug, relative, path = feature_manifest._feature_path(requested, schema, "feature checks", extension=".json")
        tail = path.parts[3:]
        if slug != feature or (tail != ("checks.json",) and tail[0] != "checks"):
            raise ValueError("select checks.json or checks/*.json within the selected feature and schema")
        _, path = feature_manifest._resolve_file(root, relative, "feature checks")
        checks.append((slug, relative, path))
        check_hashes.append({"path": relative, "sha256": _sha256(path.read_bytes())})
    pre, post = feature_manifest._load_namespaced_checks(root, checks)
    if not post or any(check.id.endswith("-replace-me") for check in post):
        raise ValueError("replace the scaffold with reviewed postconditions before preparing DEV")
    checkpoint = FeatureCheckpoint(schema, tuple(selected.values()), pre, post, tuple(dict.fromkeys((schema, unit[2]) for unit in units)))
    return checkpoint, check_hashes, {claim.key for claim in claims if claim.feature == feature}


def _selected_input(relative, raw, unit):
    if unit[1] == "VIEW":
        return feature_dev_views.selected_view(relative, raw, unit)
    item = FeatureInput(relative, raw, _sha256(raw), unit, "stored-source")
    if feature_manifest._source_unit(item, unit[0]) != unit:
        raise ValueError(f"selected source must define exactly one stored unit: {relative}")
    return item


def _driver_bytes(item):
    if item.unit[1] == "VIEW":
        return feature_dev_views.view_driver(item)
    return release_capsule._stored_source_driver(item)


def _config_binding(root, values, target):
    path = values.get("PROJECT_ENV_FILE")
    configuration = None
    if path:
        requested = Path(path)
        if ".." in requested.parts:
            raise ValueError("effective project environment file cannot contain .. components")
        candidate = root / requested
        try:
            relative = relative_to_root(candidate, root).as_posix()
        except ValueError as error:
            raise ValueError("DEV feature apply requires PROJECT_ENV_FILE inside the checkout") from error
        _, path = feature_manifest._resolve_file(root, relative, "effective project environment file")
        configuration = {"path": relative, "sha256": _sha256(path.read_bytes())}
    return {
        "environment": "dev", "profile": "migration" if migration_profile_keys(values, "dev") else "code",
        "connection": target.connection, "expectedUser": target.expected_user,
        "schema": target.schema, "classification": target.classification, "configurationFile": configuration,
    }


def _engine_binding(root):
    return {
        "promotion": release_capsule.capsule_engine_digest(root),
        "featureDev": _sha256(Path(__file__).read_bytes()),
        "featureDevViews": _sha256(Path(feature_dev_views.__file__).read_bytes()),
    }


def _assert_no_pending_release(root, scope):
    index = release_state._load_index(root)
    if any(
        scope in run["targetScopes"] and release_state._run_blocks_target(run)
        and not release_state._rollout_is_operator_reconciled(run)
        for run in index["rollouts"]
    ) or any(
        row["targetScope"] == scope and release_state._is_unresolved(
            row, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"],
        )
        for row in index["attempts"]
    ):
        raise ValueError("unresolved release write evidence on this target; reconcile it before preparing DEV")


def _assert_retained_attempts_verified(root, message):
    """With no active migration folders, a retained attempt blocks only unless its run and every migration in it are verified.

    The folders that the attempt evidence would be read from are archived, so the run manifest is the only record left.
    An unreadable or malformed manifest, or any state other than verified, refuses with the given message.
    """
    for attempt in sorted((root / "scratch").glob("migration-attempt-*")):
        manifest_path = attempt / "run-manifest.json"
        try:
            manifest = migration_manifest.decode_json(manifest_path.read_bytes(), str(manifest_path))
        except (OSError, ValueError) as exc:
            raise ValueError(message) from exc
        migrations = manifest.get("migrations") if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or not isinstance(migrations, list):
            raise ValueError(message)
        if manifest.get("state") != "verified" or any(
            not isinstance(item, dict) or item.get("state") != "verified" for item in migrations
        ):
            raise ValueError(message)


def _assert_no_pending_migration(root, schema):
    # Legacy evidence may have only an environment and schema, so use its existing scoped reader.
    closed = {"no-attempt-or-receipt", "receipt:verified:dev", "attempt:frozen:dev", "attempt:verified:dev"}
    closed.update(f"attempt-event:{state}:dev" for state in migration_revision.RESOLUTION_STATES | {"verified", "apply-not-started"})
    closed.update(f"resolution:{state}:dev" for state in migration_revision.RESOLUTION_STATES)
    closed.add("attempt:apply-not-started:dev")
    migrations = root / "migrations"
    if not migrations.exists() and not migrations.is_symlink():
        _assert_retained_attempts_verified(root, "cannot verify retained migration attempts without their migrations directory")
        return
    folders = migration_manifest.list_migration_folders(root)
    if not folders:
        _assert_retained_attempts_verified(root, "cannot verify retained migration attempts without their migration folders")
    for owner in (None, schema):
        index = migrations / owner / ".archived.json" if owner else migrations / ".archived.json"
        for archived in migration_manifest.load_archived_index(index, owner):
            evidence = archived.get("evidence", ())
            if "unresolved (preserved)" in evidence:
                raise ValueError(f"archived migration {archived['folder']} has unresolved evidence; review it before adopting a DEV baseline")
    for folder in folders:
        relative = relative_to_root(folder, root)
        if len(relative.parts) == 3 and relative.parts[1] != schema:
            continue
        lock = migration_revision.inspect_migration_lock(root, relative.as_posix(), environment="dev", schema=schema)
        if set(lock.states) - closed:
            raise ValueError(f"pending or unreadable DEV migration evidence for {relative.as_posix()}; reconcile before adopting a baseline")


def _retained_source(root, item, baseline_path, scope):
    raw = feature_sources._ordinary_file(baseline_path, root)
    legacy = raw is None
    if legacy:
        legacy_path = baseline_path.with_name(f"{_sha256(_canonical_bytes(item.unit))}.json")
        raw = feature_sources._ordinary_file(legacy_path, root)
    if raw is not None:
        record = migration_manifest.decode_json(raw, "retained DEV baseline")
        if (
            not isinstance(record, dict) or set(record) != {"schemaVersion", "unit", "targetScope", "state"}
            or type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1
        ):
            raise ValueError(f"retained DEV baseline is malformed for {item.path}")
        saved_scope = release_ownership._scope_record(record["targetScope"])
        if record["unit"] != list(item.unit) or (not legacy and saved_scope != scope):
            raise ValueError(f"retained DEV baseline belongs to a different unit or physical target: {item.path}")
        state = record["state"]
        if legacy and saved_scope != scope:
            return None
        if state is None:
            return None
        if item.unit[1] == "VIEW":
            if (
                not isinstance(state, dict) or set(state) != {"ddl", "text", "valid"} or state["valid"] is not True
                or any(not isinstance(state[field], str) or not state[field].strip() for field in ("ddl", "text"))
            ):
                raise ValueError(f"retained DEV view baseline has incomplete state for {item.path}")
            return state
        if (
            not isinstance(state, dict) or set(state) != {"lines", "settings", "valid"}
            or state["valid"] is not True or not isinstance(state["settings"], dict)
        ):
            raise ValueError(f"retained DEV baseline has incomplete state for {item.path}")
        return state

    if item.unit[1] == "VIEW":
        return None

    retained = feature_release._captured_dev_baseline(root, item)
    if retained is None:
        return None
    text, identity = retained
    if release_state.target_scope_from_identity(identity) != scope:
        return None
    stored, settings = feature_release._stored_source(replace(item, source=text.encode("utf-8")))
    return {"lines": stored["lines"], "settings": settings}


def _before_state(feature, scope, checkpoint, state):
    return {
        "feature": feature, "targetScope": scope,
        "units": [
            {"unit": list(item.unit), "state": state["sourceUnits"].get(f"{item.unit[1]}:{item.unit[2]}")}
            for item in checkpoint.inputs
        ],
    }


def _baseline_path(root, feature, unit, scope):
    key = _sha256(_canonical_bytes([unit, scope]))
    return root / "scratch" / "feature-dev" / "baselines" / feature / unit[0] / f"{key}.json"


def _baseline_record(unit, scope, state):
    return {"schemaVersion": 1, "unit": list(unit), "targetScope": scope, "state": state}


def _before_and_reviews(root, run_dir, feature, checkpoint, state, guards, scope, accepted):
    schema = checkpoint.schema
    review = []
    adoption = {}
    changed = []
    for item, guard in zip(checkpoint.inputs, guards, strict=True):
        _, kind, name = item.unit
        current = state["sourceUnits"].get(f"{kind}:{name}")
        present = current is not None
        unit_path = _baseline_path(root, feature, item.unit, scope)
        previous = feature_sources._ordinary_file(unit_path, root)
        retained = _retained_source(root, item, unit_path, scope)
        matches = not present and retained is None
        if retained is not None:
            if kind == "VIEW":
                matches = present and (current["ddl"], current["text"]) == (retained["ddl"], retained["text"])
            else:
                lines = retained["lines"]
                if not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines):
                    raise ValueError(f"retained source is incomplete for {kind} {name}")
                settings = feature_release._validated_settings(name, kind, retained["settings"])
                matches = present and feature_release._same_stored_source(current["lines"], lines) and feature_release._same_settings(current["settings"], settings)
        if not matches:
            if present and current["valid"] is not True:
                raise ValueError(f"cannot adopt an invalid DEV unit: {kind} {name}")
            adoption[unit_path] = (previous, _baseline_record(item.unit, scope, current))
            if retained is not None:
                changed.append(f"{kind} {name}")
                old_source = retained["ddl"] if kind == "VIEW" else "".join(retained["lines"])
                live_source = (current["ddl"] if kind == "VIEW" else "".join(current["lines"])) if present else ""
                review.append(f"\nChanges since the retained DEV baseline for {kind} {name}\n")
                if kind != "VIEW":
                    review.append(f"  Retained compiler settings: {retained['settings']}\n")
                review.append(feature_sources._review_diff(old_source, live_source, feature, schema, (kind, name)).decode("utf-8"))
        label = f"{kind} {name}"
        review.append(f"\n{label}\n  {item.path}\n  {'No-op' if guard['noOp'] else 'Apply planned'}\n")
        if kind == "VIEW":
            if guard["observedCollation"] is not None:
                review.append(f"  Observed view collation: {guard['observedCollation']}\n")
            if guard["defaultCollation"] is not None:
                review.append(f"  Planned session/schema default collation: {guard['defaultCollation']}\n")
            before = current["ddl"] if present else ""
            after = item.source.decode("utf-8")
        else:
            review.append(f"  Observed compiler settings: {current['settings'] if present else 'absent'}\n")
            review.append(f"  Proposed compiler settings: {guard['applySettings']}\n")
            before = "".join(current["lines"]) if present else ""
            after = "".join(feature_release._stored_source(item)[0]["lines"])
        review.append(feature_sources._review_diff(before, after, feature, schema, (kind, name)).decode("utf-8"))

    observed = _before_state(feature, scope, checkpoint, state)
    observed_digest = _sha256(_canonical_bytes(observed))
    release_capsule._write_stage_file(run_dir, "source-review.diff", "".join(review).encode("utf-8"))
    if adoption:
        _assert_no_pending_migration(root, schema)
        release_capsule._write_stage_file(run_dir, "before-state.json", _canonical_bytes(observed) + b"\n")
        if accepted != observed_digest:
            relative = relative_to_root(run_dir, root).as_posix()
            drift = f"live DEV source or settings changed since the retained baseline for {', '.join(changed)}. " if changed else ""
            raise DevPrepareRefusal(
                f"{drift}Review the live definition in {relative}/before-state.json and the diff in {relative}/source-review.diff. "
                f"To adopt this live baseline, prepare again with --accept-live-baseline {observed_digest}"
            )
    elif accepted is not None:
        raise ValueError("--accept-live-baseline requires a live state without a matching retained baseline")
    return observed, observed_digest, adoption


def _structural_plan(checkpoint, state):
    return feature_preflight.prepare_checkpoint(
        checkpoint, state, (),
        guarded_view_replacements=frozenset(
            item.unit[2] for item in checkpoint.inputs
            if item.unit[1] == "VIEW" and f"VIEW:{item.unit[2]}" in state["sourceUnits"]
        ),
    )


def _source_and_structure_guards(checkpoint, claims, state):
    view_guards = {}
    for item in checkpoint.inputs:
        owner, kind, name = item.unit
        if kind != "VIEW":
            continue
        observed = state["sourceUnits"].get(f"VIEW:{name}")
        if observed is None and any(row.get("name") == name for row in state["structural"]["views"]):
            raise ValueError(f"complete view catalog text is unavailable for {name}")
        if observed is not None and (
            type(observed.get("valid")) is not bool
            or any(not isinstance(observed.get(field), str) or not observed[field].strip() for field in ("ddl", "text"))
        ):
            raise ValueError(f"complete view catalog text or validity is unavailable for {name}")
        default_collation = None
        if feature_dev_views.definition(item.source.decode("utf-8"), owner)["collation"] is None:
            default_collation = state.get("effectiveViewCollation")
            if not isinstance(default_collation, str) or not default_collation:
                raise ValueError(f"effective default collation is unavailable for {name}")
        observed_collation = state.get("viewCollations", {}).get(name)
        view_guards[item.unit] = {
            "unit": {"owner": owner, "type": kind, "name": name},
            "targetExists": observed is not None,
            "defaultCollation": default_collation, "observedCollation": observed_collation,
            "noOp": observed is not None and feature_dev_views.matches(
                item, observed, default_collation=default_collation, actual_collation=observed_collation,
            ),
        }
    structural = feature_preflight.plan_to_dict(_structural_plan(checkpoint, state))
    guards = feature_release._source_guards(checkpoint, state, 1)
    selected = {item.unit for item in checkpoint.inputs}
    on_target = {(row.get("type"), row.get("name")) for row in state["structural"]["stored-code"]}
    for guard in guards:
        kind, name = guard["unit"]["type"], guard["unit"]["name"]
        if kind in {"PACKAGE", "TYPE"} and not guard["noOp"]:
            body = (checkpoint.schema, kind + " BODY", name)
            if body not in selected and (body in claims or (body[1], name) in on_target):
                raise DevPrepareRefusal(f"the changed {kind} {name} must select its {body[1]} too")
    try:
        feature_release._refuse_disabled_triggers(checkpoint, state, guards, "dev")
    except feature_release.DisabledTriggerError as error:
        raise DevPrepareRefusal(str(error)) from error
    by_unit = {(row["unit"]["owner"], row["unit"]["type"], row["unit"]["name"]): row for row in guards}
    by_unit.update(view_guards)
    return [by_unit[item.unit] for item in checkpoint.inputs], structural


def _read_checks(target, checks, directory, identity, phase):
    report = migration_checks.run_checks(target, checks, directory, phase=phase)
    if checks and not report.coverage.get("sessions"):
        raise ValueError(f"DEV {phase} did not observe a target session")
    feature_release._runtime_check_identity(replace(report, passed=True), identity, f"DEV {phase}")
    if not report.passed:
        failed = ", ".join(report.coverage.get("failedCheckIds", ()))
        raise DevPrepareRefusal(f"verified DEV {phase} failed" + (f": {failed}" if failed else ""))
    return report


def prepare_dev_apply(
    repo_root: Path, feature: str, schema: str, selectors: Sequence[str], *,
    environ: Mapping[str, str], check_paths: Sequence[str] = (), accepted_baseline_sha256: str | None = None,
) -> Path:
    """Validate working-tree inputs and read DEV without starting a write session."""
    root = feature_release._root(repo_root)
    if accepted_baseline_sha256 is not None and not feature_release.HASH_RE.fullmatch(accepted_baseline_sha256):
        raise ValueError("--accept-live-baseline expects a lowercase SHA-256 digest")
    checkpoint, checks, claims = _selected_checkpoint(root, feature, schema, selectors, check_paths)
    target = resolve_target(environ, "dev", "migration", schema)
    binding = _config_binding(root, environ, target)
    engine = _engine_binding(root)
    scratch = root / "scratch" / "feature-dev"
    feature_release._secure_directory(scratch, root)
    run_dir = Path(tempfile.mkdtemp(prefix="prepare-", dir=scratch))
    state = feature_release._capture_selected_state(target, checkpoint, run_dir / "catalog")
    identity = feature_release._identity(target, state["identity"])
    scope = release_state.target_scope_from_identity(identity)
    for item in checkpoint.inputs:
        _, kind, name = item.unit
        section = "triggers" if kind == "TRIGGER" else "stored-code"
        if f"{kind}:{name}" not in state["sourceUnits"] and any(
            row.get("name") == name and (kind == "TRIGGER" or row.get("type") == kind)
            for row in state["structural"].get(section, [])
        ):
            raise ValueError(f"live DEV source is incomplete for {kind} {name}")

    with release_ownership.target_writer_lease(root, scope, {"purpose": "feature-dev-prepare", "feature": feature}):
        _assert_dev_available(root, scope)
        guards, structural = _source_and_structure_guards(checkpoint, claims, state)
        before, before_digest, adopted = _before_and_reviews(
            root, run_dir, feature, checkpoint, state, guards, scope, accepted_baseline_sha256,
        )
        pre, _ = feature_release._checkpoint_runtime_checks(checkpoint, {"structuralPlan": structural, "sourceGuards": guards})
        _read_checks(target, pre, run_dir / "preconditions", identity, "preconditions")

        units = []
        for index, item in enumerate(checkpoint.inputs, 1):
            driver, delivery = _driver_bytes(item)
            driver_path = f"drivers/{index:03d}.sql"
            release_capsule._write_stage_file(run_dir, driver_path, driver)
            release_capsule._write_stage_file(run_dir, f"sources/{index:03d}.sql", item.source)
            units.append({
                "type": item.unit[1], "name": item.unit[2], "sourcePath": item.path, "sourceSha256": item.sha256,
                "driverPath": driver_path, "driverSha256": _sha256(driver), "delivery": delivery,
            })
        for index, record in enumerate(checks, 1):
            _, path = feature_manifest._resolve_file(root, record["path"], "DEV plan checks")
            checks_raw = path.read_bytes()
            if _sha256(checks_raw) != record["sha256"]:
                raise ValueError(f"DEV plan input changed during preparation: {record['path']}")
            release_capsule._write_stage_file(run_dir, f"checks/{index:03d}.json", checks_raw)
        for record in ({"path": unit["sourcePath"], "sha256": unit["sourceSha256"]} for unit in units):
            _, path = feature_manifest._resolve_file(root, record["path"], "DEV plan input")
            if _sha256(path.read_bytes()) != record["sha256"]:
                raise ValueError(f"DEV plan input changed during preparation: {record['path']}")
        if binding != _config_binding(root, environ, target):
            raise ValueError("effective project configuration changed during preparation")
        if engine != _engine_binding(root):
            raise ValueError("DEV preparation engine changed during preparation")

        document = {
            "schemaVersion": 1, "planId": run_dir.name, "feature": feature, "target": binding, "observedIdentity": identity,
            "targetScope": scope, "units": units, "checks": checks, "engine": engine,
            "beforeState": before, "beforeStateSha256": before_digest, "sourceGuards": guards,
            "structuralPlan": structural, "noOp": all(guard["noOp"] for guard in guards),
            "reviewPath": "source-review.diff",
            "reviewSha256": _sha256((run_dir / "source-review.diff").read_bytes()),
        }
        document["planSha256"] = _sha256(_canonical_bytes(document))
        plan_bytes = _canonical_bytes(document) + b"\n"
        if adopted:
            with release_state._writer_lock(root):
                if any(feature_sources._ordinary_file(path, root) != previous for path, (previous, _) in adopted.items()):
                    raise ValueError("a retained DEV baseline changed during preparation; review it and prepare again")
                feature_release._secure_directory(next(iter(adopted)).parent, root)
                outputs = {path: _canonical_bytes(record) + b"\n" for path, (_, record) in adopted.items()}
                outputs[run_dir / "plan.json"] = plan_bytes
                feature_sources._atomic_group_write(root, outputs)
        else:
            release_capsule._write_stage_file(run_dir, "plan.json", plan_bytes)
    return run_dir / "plan.json"


def _plan_path(root, requested):
    path = Path(requested)
    if ".." in path.parts:
        raise ValueError("DEV plan path cannot contain .. components")
    relative = relative_to_root(root / path, root)
    if (
        len(relative.parts) != 4 or relative.parts[:2] != ("scratch", "feature-dev")
        or relative.name != "plan.json" or not PLAN_ID.fullmatch(relative.parts[2])
    ):
        raise ValueError("select a saved scratch/feature-dev/<plan-id>/plan.json")
    return relative


def _read_plan(root, relative):
    _, path = feature_manifest._resolve_file(root, relative.as_posix(), "DEV apply plan")
    raw = path.read_bytes()
    document = migration_manifest.decode_json(raw, "DEV apply plan")
    if (
        not isinstance(document, dict) or set(document) != PLAN_FIELDS
        or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
    ):
        raise ValueError("unsupported DEV apply plan format")
    unsigned = {key: value for key, value in document.items() if key != "planSha256"}
    if document.get("planId") != relative.parent.name or document.get("planSha256") != _sha256(_canonical_bytes(unsigned)):
        raise ValueError("DEV apply plan identity or digest changed")
    return document, raw


def _read_review(root, relative, plan):
    if plan["reviewPath"] != "source-review.diff":
        raise ValueError("DEV plan review path is invalid")
    _, review = feature_manifest._resolve_file(root, (relative.parent / plan["reviewPath"]).as_posix(), "DEV source review")
    raw = review.read_bytes()
    if _sha256(raw) != plan["reviewSha256"]:
        raise ValueError("DEV source review changed after preparation")
    return raw


def _record_daily_review(root, relative, plan):
    if plan["noOp"]:
        return
    path = (root / "ai_generate" / date.today().isoformat() / "feature-diffs" / plan["feature"]
            / plan["targetScope"]["schema"] / "dev-apply" / f"{plan['planId']}.diff")
    try:
        raw = _read_review(root, relative, plan)
        previous = feature_sources._ordinary_file(path, root)
        if previous is None:
            feature_sources._atomic_group_write(root, {path: raw})
        elif previous != raw:
            raise ValueError(f"daily source review already exists with different content: {path}")
    except (OSError, ValueError, KeyboardInterrupt) as error:
        print(f"DEV outcome was verified. Retain the source review in the daily record separately: {error}", file=sys.stderr)
    else:
        print(f"Daily source review {relative_to_root(path, root).as_posix()}")


def _validated_plan_inputs(root, relative, plan, feature, schema, environ):
    if plan["feature"] != feature or plan["target"]["schema"] != schema:
        raise ValueError("DEV apply plan belongs to a different feature or schema")
    target = resolve_target(environ, "dev", "migration", schema)
    if plan["target"] != _config_binding(root, environ, target):
        raise ValueError("DEV target configuration changed after preparation")
    if plan["engine"] != _engine_binding(root):
        raise ValueError("DEV apply engine changed after preparation")
    identity = feature_release._identity(target, plan["observedIdentity"])
    scope = release_state.target_scope_from_identity(identity)
    if scope != plan["targetScope"]:
        raise ValueError("DEV apply plan scope conflicts with its observed identity")
    selectors = []
    for row in plan["units"]:
        quoted_name = row["name"].replace('"', '""')
        selectors.append(f'{row["type"]}:"{quoted_name}"')
    checkpoint, checks, claims = _selected_checkpoint(root, feature, schema, selectors, [row["path"] for row in plan["checks"]])
    if checks != plan["checks"]:
        raise ValueError("DEV apply check files changed after preparation")
    for number, (row, item) in enumerate(zip(plan["units"], checkpoint.inputs, strict=True), 1):
        driver, delivery = _driver_bytes(item)
        expected = f"drivers/{number:03d}.sql"
        if row != {
            "type": item.unit[1], "name": item.unit[2], "sourcePath": item.path, "sourceSha256": item.sha256,
            "driverPath": expected, "driverSha256": _sha256(driver), "delivery": delivery,
        }:
            raise ValueError(f"DEV source or driver changed after preparation: {item.path}")
        _, path = feature_manifest._resolve_file(root, (relative.parent / expected).as_posix(), "DEV apply driver")
        if path.read_bytes() != driver:
            raise ValueError(f"DEV apply driver changed after preparation: {expected}")
    _read_review(root, relative, plan)
    return target, identity, scope, checkpoint, claims


def _finish_attempt(root, marker, record, state, **fields):
    updated = {**record, "state": state, **fields}
    feature_sources._atomic_group_write(root, {marker: _canonical_bytes(updated) + b"\n"})
    _sync_attempt_directory(marker.parent)


def _sync_attempt_directory(directory):
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_attempt(root, plan, relative, state, evidence):
    directory = root / ATTEMPT_DIRECTORY
    feature_release._secure_directory(directory, root)
    _assert_dev_available(root, plan["targetScope"], plan["planId"])
    marker = directory / f"{plan['planId']}.json"
    record = {
        "schemaVersion": 1, "planId": plan["planId"], "planSha256": plan["planSha256"], "feature": plan["feature"],
        "planPath": relative.as_posix(), "targetScope": plan["targetScope"], "state": state,
        "evidencePaths": [relative.as_posix(), *evidence],
    }
    release_capsule._write_stage_file(directory, marker.name, _canonical_bytes(record) + b"\n")
    _sync_attempt_directory(directory)
    _sync_attempt_directory(directory.parent)
    return marker, record


def _retained_preconditions_refused(output, checks, identity):
    lines = set(output.splitlines())
    if (
        "CHECK_REHEARSAL_FAILED:preconditions" not in lines
        or not re.search(r"(?m)^ORA-20985\b", output)
        or "MIGRATION_APPLY_COMPLETED" in lines
        or any(line.startswith("MIGRATION_PAYLOAD_STARTED:") for line in lines)
    ):
        return False
    report = migration_checks._parse_check_output(output, checks, "preconditions")
    return report.complete and not report.passed and report.coverage.get("identity") == identity


def _refresh_baselines(root, feature, scope, checkpoint, state):
    outputs = {}
    for item in checkpoint.inputs:
        current = state["sourceUnits"][f"{item.unit[1]}:{item.unit[2]}"]
        path = _baseline_path(root, feature, item.unit, scope)
        outputs[path] = item, _canonical_bytes(_baseline_record(item.unit, scope, current)) + b"\n"
    with release_state._writer_lock(root):
        for path, (item, _) in outputs.items():
            feature_release._secure_directory(path.parent, root)
            if feature_sources._ordinary_file(path, root) is not None:
                _retained_source(root, item, path, scope)
        feature_sources._atomic_group_write(root, {path: raw for path, (_, raw) in outputs.items()})


def _intended_source_matches(checkpoint, observed, source_guards):
    for item in checkpoint.inputs:
        key = item.unit[1:]
        state = observed["sourceUnits"].get(":".join(key))
        if item.unit[1] == "VIEW":
            if state is None or not feature_dev_views.matches(
                item, state, default_collation=source_guards[key].get("defaultCollation"),
                actual_collation=observed.get("viewCollations", {}).get(item.unit[2]),
            ):
                return False
            continue
        expected, _ = feature_release._stored_source(item)
        guard = source_guards[key]
        if (
            state is None or state["valid"] is not True
            or not feature_release._same_stored_source(state["lines"], expected["lines"])
            or not feature_release._same_settings(state["settings"], guard["applySettings"])
        ):
            return False
    return True


def execute_dev_apply(repo_root: Path, feature: str, schema: str, plan_path: Path | str, *, environ: Mapping[str, str]) -> int:
    """Apply one unchanged prepared plan and retain proof across interruptions."""
    root = feature_release._root(repo_root)
    relative = _plan_path(root, plan_path)
    plan_id = relative.parent.name
    if plan_id in _attempts(root):
        raise ValueError(f"DEV plan {plan_id} is consumed or unresolved; prepare a fresh plan")
    plan, raw = _read_plan(root, relative)
    try:
        target, identity, scope, checkpoint, claims = _validated_plan_inputs(root, relative, plan, feature, schema, environ)
    except (KeyError, TypeError) as error:
        raise ValueError(f"DEV apply plan input record is malformed: {error}") from error
    with release_ownership.target_writer_lease(root, scope, {"purpose": "feature-dev-apply", "planId": plan_id}):
        _assert_dev_available(root, scope, plan_id)
        _assert_no_pending_migration(root, schema)
        if (root / relative).read_bytes() != raw:
            raise ValueError("DEV apply plan changed before acquiring its target lease")
        run_dir = Path(tempfile.mkdtemp(prefix="apply-", dir=(root / relative).parent))
        report_path = relative_to_root(run_dir / "checkpoint-report.json", root).as_posix()
        try:
            observed = feature_release._capture_selected_state(target, checkpoint, run_dir / "catalog-before")
            if feature_release._identity(target, observed["identity"]) != identity:
                raise ValueError("observed DEV identity changed after preparation")
            if _sha256(_canonical_bytes(_before_state(feature, scope, checkpoint, observed))) != plan["beforeStateSha256"]:
                raise DevPrepareRefusal("live DEV before-state changed after preparation")
            guards, structure = _source_and_structure_guards(checkpoint, claims, observed)
            if (
                _canonical_bytes(guards) != _canonical_bytes(plan["sourceGuards"])
                or _canonical_bytes(structure) != _canonical_bytes(plan["structuralPlan"])
            ):
                raise DevPrepareRefusal("live DEV source, settings or structural guards changed after preparation")
            guard = {"sourceGuards": guards, "structuralPlan": structure}
            pre, post = feature_release._checkpoint_runtime_checks(checkpoint, guard)
            pre_report = _read_checks(target, pre, run_dir / "preconditions", identity, "preconditions")
        except ValueError as error:
            feature_release._write_checkpoint_report(run_dir, {
                "planId": plan_id, "databaseState": "not-started", "error": str(error),
            })
            _write_attempt(root, plan, relative, "not-started", [report_path])
            raise
        source_guards = feature_release._checkpoint_source_guard_map(guard)
        drivers = []
        for number, item in enumerate(checkpoint.inputs, 1):
            source_guard = source_guards[item.unit[1:]]
            if source_guard["noOp"]:
                continue
            driver, _ = _driver_bytes(item)
            staged = f"payload/{number:03d}.sql"
            feature_release._stage_checkpoint_driver(run_dir, staged, driver, item.path)
            if item.unit[1] == "VIEW":
                drivers.append((staged, None, None))
            else:
                drivers.append((staged, feature_release._validated_source_prologue(source_guard), source_guard["settingsPrologueSha256"]))

        def recheck_inputs():
            if _read_plan(root, relative)[1] != raw:
                raise ValueError("DEV apply plan changed after verification")
            _validated_plan_inputs(root, relative, plan, feature, schema, environ)
            for staged, _, _ in drivers:
                number = int(Path(staged).stem)
                staged_file = feature_release._safe_repo_path(root, run_dir / staged, "staged DEV apply driver")
                if _sha256(staged_file.read_bytes()) != plan["units"][number - 1]["driverSha256"]:
                    raise ValueError(f"staged DEV apply driver changed: {staged}")

        print(f"DEV {scope['dbUniqueName']} / {scope['containerId']} / {schema} / {scope['edition']} via {target.connection} as {target.expected_user}", flush=True)
        for item in checkpoint.inputs:
            print(f"  {item.unit[1]} {item.unit[2]}", flush=True)
        recheck_inputs()
        if _recovery_checkpoint(root, relative, plan) != checkpoint:
            raise ValueError("retained DEV inputs do not match the selected checkpoint")
        trail = [report_path]
        if drivers:
            trail.append(relative_to_root(run_dir / "sqlcl-output.log", root).as_posix())
        trail.extend(
            (relative.parent / f"{group}/{index:03d}.{extension}").as_posix()
            for group, extension, records in (("sources", "sql", plan["units"]), ("checks", "json", plan["checks"]))
            for index, _ in enumerate(records, 1)
        )
        trail.append((relative.parent / plan["reviewPath"]).as_posix())
        marker, pending = _write_attempt(root, plan, relative, "pending", trail)
        started = time.perf_counter()
        crossed_boundary = False

        def before_run():
            nonlocal crossed_boundary
            recheck_inputs()
            crossed_boundary = True

        report = {
            "planId": plan_id, "observedIdentity": identity, "preconditions": pre_report.to_dict(),
            "units": plan["units"], "databaseState": "not-started", "postconditions": None,
        }
        try:
            apply_started = time.perf_counter()
            _, apply_sessions = feature_release._run_apply_session(
                checkpoint, target, identity, pre, run_dir, source_guards, drivers, before_run=before_run,
                exact_before=feature_dev_views.before_guards(checkpoint, observed),
            )
            report["applySeconds"] = round(time.perf_counter() - apply_started, 3)
            verification_started = time.perf_counter()
            verified = feature_release._capture_selected_state(target, checkpoint, run_dir / "catalog-after")
            if feature_release._identity(target, verified["identity"]) != identity or not _intended_source_matches(checkpoint, verified, source_guards):
                raise ValueError("DEV read-back did not prove the selected source, settings and validity")
            # Checking readiness in a separate session also protects no-op execution.
            post_report = _read_checks(target, post, run_dir / "postconditions", identity, "postconditions")
            report.update(
                databaseState="verified", postconditions=post_report.to_dict(),
                verificationSeconds=round(time.perf_counter() - verification_started, 3),
                sessionCount=2 + apply_sessions + pre_report.coverage["sessions"] + post_report.coverage["sessions"],
                elapsedSeconds=round(time.perf_counter() - started, 3),
            )
            feature_release._write_checkpoint_report(run_dir, report)
            _finish_attempt(
                root, marker, pending, "verified", noOp=not drivers,
                reportSha256=_sha256((run_dir / "checkpoint-report.json").read_bytes()),
            )
        except (Exception, KeyboardInterrupt) as error:
            interrupted = isinstance(error, KeyboardInterrupt) or isinstance(error.__cause__, feature_release.sqlcl_session.SqlclCancelled)
            log = run_dir / "sqlcl-output.log"
            known_no_write = not crossed_boundary
            check_refused = False
            if crossed_boundary and log.is_file():
                try:
                    output = log.read_text(encoding="utf-8")
                    check_refused = _retained_preconditions_refused(output, pre, identity)
                    lines = output.splitlines()
                    check_refused = check_refused or (
                        any(line.startswith("ORA-20984: FEATURE_VIEW_GUARD_REFUSED:") for line in lines)
                        and "MIGRATION_IDENTITY_VERIFIED" in lines
                        and not any(line.startswith(("MIGRATION_PAYLOAD_STARTED:", "MIGRATION_APPLY_COMPLETED")) for line in lines)
                    )
                    known_no_write = check_refused or migration_attempts.not_started_reason(output) is not None
                except (OSError, UnicodeError):
                    pass
            report.update(databaseState="not-started" if known_no_write else "unknown", error=str(error))
            try:
                feature_release._write_checkpoint_report(run_dir, report)
                if known_no_write:
                    _finish_attempt(root, marker, pending, "not-started")
            except (OSError, ValueError):
                known_no_write = False
            units = ", ".join(f"{row['type']} {row['name']}" for row in plan["units"])
            print(f"DEV apply {plan_id} for {units}: {error}", file=sys.stderr)
            if not known_no_write:
                print("Outcome requires reconciliation. Preserve the plan and SQLcl evidence and inspect the target.", file=sys.stderr)
            if known_no_write:
                return 130 if interrupted else 1 if check_refused else 2
            return 2

        try:
            _refresh_baselines(root, feature, scope, checkpoint, verified)
        except (OSError, ValueError, KeyboardInterrupt) as error:
            print(f"DEV apply was verified. Refresh retained DEV baselines separately: {error}", file=sys.stderr)
        _record_daily_review(root, relative, plan)
        print(f"Verified DEV {'apply' if drivers else 'no-op'} {plan_id}.")
        return 0


def _recovery_checkpoint(root, relative, plan):
    schema = plan["targetScope"]["schema"]
    inputs = []
    for index, row in enumerate(plan["units"], 1):
        path = relative.parent / f"sources/{index:03d}.sql"
        _, file = feature_manifest._resolve_file(root, path.as_posix(), "retained DEV source")
        raw = feature_manifest._read_feature_source(file, path.as_posix())
        unit = (schema, row["type"], row["name"])
        item = _selected_input(row["sourcePath"], raw, unit)
        if item.sha256 != row["sourceSha256"]:
            raise ValueError("retained DEV source differs from the pending plan")
        inputs.append(item)
    paths = []
    for index, row in enumerate(plan["checks"], 1):
        path = relative.parent / f"checks/{index:03d}.json"
        _, file = feature_manifest._resolve_file(root, path.as_posix(), "retained DEV checks")
        if _sha256(file.read_bytes()) != row["sha256"]:
            raise ValueError("retained DEV checks differ from the pending plan")
        paths.append((plan["feature"], row["path"], file))
    pre, post = feature_manifest._load_namespaced_checks(root, paths)
    keys = tuple(dict.fromkeys((schema, item.unit[2]) for item in inputs))
    return FeatureCheckpoint(schema, tuple(inputs), pre, post, keys)


def reconcile_dev_apply(repo_root, plan_id, evidence_path, *, environ, read_acknowledgment, yes=False):
    """Close a pending write after re-observing the reviewed state on its original target."""
    root = feature_release._root(repo_root)
    if not PLAN_ID.fullmatch(plan_id):
        raise ValueError("expected a DEV run ID from feature apply --status")
    record = _attempts(root).get(plan_id)
    if record is None or record["state"] != "pending":
        raise ValueError(f"DEV run {plan_id} is not pending")
    relative_evidence = relative_to_root(root / evidence_path, root).as_posix()
    _, evidence_file = feature_manifest._resolve_file(root, relative_evidence, "DEV reconciliation evidence")
    evidence_raw = evidence_file.read_bytes()
    evidence = migration_manifest.decode_json(evidence_raw, "DEV reconciliation evidence")
    required = {"schemaVersion", "planId", "planSha256", "targetScope", "conclusion", "establishedBy", "notes"}
    if (
        not isinstance(evidence, dict) or set(evidence) != required
        or type(evidence["schemaVersion"]) is not int or evidence["schemaVersion"] != 1
        or evidence["planId"] != plan_id or evidence["planSha256"] != record["planSha256"]
        or evidence["targetScope"] != record["targetScope"] or evidence["conclusion"] not in ("before", "intended")
        or not all(isinstance(evidence[key], str) and evidence[key].strip() for key in ("establishedBy", "notes"))
    ):
        raise ValueError("DEV reconciliation evidence must identify the pending run, same physical target and reviewed conclusion")
    relative = _plan_path(root, record["planPath"])
    plan, plan_raw = _read_plan(root, relative)
    scope = record["targetScope"]
    if plan["planSha256"] != record["planSha256"] or plan["targetScope"] != scope:
        raise ValueError("retained plan is not the plan named by the pending marker")
    target = resolve_target(environ, "dev", "migration", scope["schema"])
    if _config_binding(root, environ, target) != plan["target"]:
        raise ValueError("DEV reconciliation requires the prepared target profile and configuration")
    identity = feature_release._identity(target, plan["observedIdentity"])
    if release_state.target_scope_from_identity(identity) != scope:
        raise ValueError("pending plan has a conflicting physical target")
    with release_ownership.target_writer_lease(root, scope, {"purpose": "feature-dev-reconcile", "planId": plan_id}):
        if _attempts(root).get(plan_id) != record:
            raise ValueError("DEV attempt changed before reconciliation")
        if _read_plan(root, relative)[1] != plan_raw or evidence_file.read_bytes() != evidence_raw:
            raise ValueError("DEV reconciliation inputs changed before verification")
        checkpoint = _recovery_checkpoint(root, relative, plan)
        run_dir = Path(tempfile.mkdtemp(prefix="reconcile-", dir=(root / relative).parent))
        print(f"Reconcile {plan_id} as {evidence['conclusion']} on {scope['dbUniqueName']}/{scope['containerId']}/{scope['schema']}/{scope['edition']}.")
        expected = release_state.RUN_RECONCILE_PREFIX + plan_id
        if not yes and read_acknowledgment(expected) != expected:
            return 1
        if yes:
            print("Acknowledged by --yes; the acknowledgment was not typed.")
        observed = feature_release._capture_selected_state(target, checkpoint, run_dir / "catalog")
        if feature_release._identity(target, observed["identity"]) != identity:
            raise ValueError("DEV reconciliation observed a different target")
        pre, post = feature_release._checkpoint_runtime_checks(checkpoint, plan)
        if evidence["conclusion"] == "before":
            before = _before_state(plan["feature"], scope, checkpoint, observed)
            structural = _structural_plan(checkpoint, observed)
            view_collations = observed.get("viewCollations", {})
            if (
                _sha256(_canonical_bytes(before)) != plan["beforeStateSha256"]
                or structural.expected_before_sha256 != plan["structuralPlan"]["expectedBeforeSha256"]
                or any(row.get("observedCollation") is not None and view_collations.get(row["unit"]["name"]) != row["observedCollation"]
                       for row in plan["sourceGuards"] if row["unit"]["type"] == "VIEW")
            ):
                raise ValueError("DEV reconciliation could not prove the prepared before-state")
            checks, phase = pre, "preconditions"
        else:
            guards = feature_release._checkpoint_source_guard_map(plan)
            if not _intended_source_matches(checkpoint, observed, guards):
                raise ValueError("DEV reconciliation could not prove the intended source, settings and validity")
            checks, phase = post, "postconditions"
        try:
            report = _read_checks(target, checks, run_dir / phase, identity, phase)
        except DevPrepareRefusal as error:
            raise ValueError(f"DEV reconciliation could not prove the selected state: {error}") from error
        if _read_plan(root, relative)[1] != plan_raw or evidence_file.read_bytes() != evidence_raw:
            raise ValueError("DEV reconciliation inputs changed during verification")
        release_capsule._write_stage_file(run_dir, "reviewed-evidence.json", evidence_raw)
        release_capsule._write_stage_file(run_dir, "observation.json", _canonical_bytes({
            "identity": identity, "sourceUnits": observed["sourceUnits"], "checks": report.to_dict(),
        }) + b"\n")
        retained = [
            relative_to_root(run_dir / name, root).as_posix()
            for name in ("reviewed-evidence.json", "observation.json")
        ]
        marker = root / ATTEMPT_DIRECTORY / f"{plan_id}.json"
        _finish_attempt(
            root, marker, record, "reconciled", evidencePaths=[*record["evidencePaths"], *retained],
            conclusion=evidence["conclusion"], evidenceSha256=_sha256(evidence_raw),
            acknowledgedBy="--yes" if yes else "terminal",
        )
        if evidence["conclusion"] == "intended":
            try:
                _refresh_baselines(root, plan["feature"], scope, checkpoint, observed)
            except (OSError, ValueError, KeyboardInterrupt) as error:
                print(f"DEV reconciliation verified. Refresh retained DEV baselines separately: {error}", file=sys.stderr)
            _record_daily_review(root, relative, plan)
    print(f"Reconciled DEV run {plan_id}. Prepare a fresh plan before any further apply.")
    return 0
