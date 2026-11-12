"""Reference-data export for the baseline export-data subcommand, moved out of scripts/baseline.py.

scripts/baseline.py keeps the CLI, the build and the helpers that the exports and the build share. Names that
stay there are read as baseline.<name> at call time, so patches on scripts.baseline still reach this code.
baseline re-exports the entry point that main() and the tests call.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _data_export_guard(output: Path):
    """Invalidate old data before capture and serialize writers for one schema."""
    baseline._reject_symlink_components(output)
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise baseline.BaselineError(f"cannot create baseline data directory {output}: {error}") from error
    baseline._reject_symlink_components(output)
    lock_path = output / baseline.DATA_EXPORT_LOCK_MARKER
    incomplete_path = output / baseline.DATA_EXPORT_INCOMPLETE_MARKER
    baseline._regular_file_info(lock_path, "baseline data export lock marker", missing_ok=True)
    if lock_path.exists() or lock_path.is_symlink():
        raise baseline.BaselineError(f"baseline data export is locked by {lock_path}; verify no exporter is active before removing it")

    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise baseline.BaselineError(f"baseline data export is locked by {lock_path}; verify no exporter is active before removing it") from error
    except OSError as error:
        raise baseline.BaselineError(f"cannot create baseline data export lock {lock_path}: {error}") from error
    lock_info = os.fstat(lock_fd)
    invalidated = False
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_fd)
        baseline._reject_symlink_components(output)
        incomplete_info = baseline._regular_file_info(incomplete_path, "baseline data incomplete marker", missing_ok=True)
        if incomplete_info is None:
            try:
                incomplete_fd = os.open(incomplete_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError as error:
                raise baseline.BaselineError(f"cannot create baseline data incomplete marker {incomplete_path}: {error}") from error
            else:
                try:
                    os.write(incomplete_fd, b"export in progress\n")
                    os.fsync(incomplete_fd)
                finally:
                    os.close(incomplete_fd)
        invalidated = True
        manifest_path = output / "manifest.json"
        manifest_info = baseline._regular_file_info(manifest_path, "baseline data manifest", missing_ok=True)
        if manifest_info is not None:
            try:
                manifest_path.unlink()
            except OSError as error:
                raise baseline.BaselineError(f"cannot invalidate the previous baseline data manifest {manifest_path}: {error}") from error
        yield
    finally:
        os.close(lock_fd)
        if invalidated:
            active_error = sys.exc_info()[0] is not None
            try:
                current_lock = baseline._regular_file_info(lock_path, "baseline data export lock marker", missing_ok=True)
                if current_lock is None or (current_lock.st_dev, current_lock.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                    raise baseline.BaselineError(f"baseline data export lock changed during capture: {lock_path}")
                lock_path.unlink()
            except (baseline.BaselineError, OSError) as error:
                if not active_error:
                    if isinstance(error, baseline.BaselineError):
                        raise
                    raise baseline.BaselineError(f"cannot remove baseline data export lock {lock_path}: {error}") from error


def _clear_data_incomplete_marker(output: Path) -> None:
    marker = output / baseline.DATA_EXPORT_INCOMPLETE_MARKER
    baseline._regular_file_info(marker, "baseline data incomplete marker")
    try:
        marker.unlink()
    except OSError as error:
        raise baseline.BaselineError(f"cannot clear baseline data incomplete marker {marker}: {error}") from error


def _export_data(config: Mapping, values: Mapping[str, str], environment: str, scratch: Path, schemas: Sequence[str] | None = None) -> None:
    tables = baseline._reference_tables(config)
    selected_schemas = schemas or config["schemas"]
    if not tables:
        print("No referenceData.tables allow-list is configured; writing identity-bound empty data manifests")
    for schema in selected_schemas:
        output = scratch / environment / "data" / schema
        with _data_export_guard(output):
            catalog = baseline._capture(
                values, environment, schema, ("baseline-data",), scratch / f"sqlcl-data-{environment}-{schema}",
                baseline_data_tables=tables,
            )
            baseline._complete(catalog, ("baseline-data",))
            source_identity = baseline._source_identity(catalog.get("identity"), f"baseline data source for {schema}")
            captured = catalog["sections"].get("baseline-data")
            if not isinstance(captured, list):
                raise baseline.BaselineError("baseline-data capture did not return a table array")
            by_name = {}
            for row in captured:
                name = row.get("name") if isinstance(row, Mapping) else None
                if not isinstance(name, str) or name in by_name:
                    raise baseline.BaselineError("baseline-data returned a malformed or duplicate table record")
                by_name[name] = row
            if set(by_name) != {table["name"] for table in tables}:
                raise baseline.BaselineError("baseline-data did not return every allow-listed reference table")
            total_rows = 0
            validated_tables: list[tuple[str, dict]] = []
            for specification in tables:
                data = baseline._validate_reference_capture(by_name[specification["name"]], specification, environment, schema)
                encoded = baseline._json_bytes(data)
                if len(encoded) > 128 * 1024 * 1024:
                    raise baseline.BaselineError(f"reference-data export for {specification['name']} exceeded the 128 MiB file cap")
                validated_tables.append((specification["name"], data))
                total_rows += data["rowCount"]

            manifest_tables = []
            for name, data in validated_tables:
                path = output / f"{name}.json"
                baseline._write_json(path, data)
                written, contents = baseline._read_json_object(path, f"reference-data export for {name}", parse_decimal=True)
                if contents != baseline._json_bytes(data):
                    raise baseline.BaselineError(f"reference-data export for {name} changed while being written")
                written = baseline._validate_reference_capture(written, next(table for table in tables if table["name"] == name), environment, schema)
                manifest_tables.append({
                    "name": name,
                    "file": path.name,
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "rowCount": written["rowCount"],
                })

            manifest = {
                "schemaVersion": baseline.DATA_EXPORT_MANIFEST_VERSION,
                "environment": environment,
                "schema": schema,
                "sourceIdentity": source_identity,
                "tableCount": len(manifest_tables),
                "rowCount": total_rows,
                "tables": manifest_tables,
                "complete": True,
            }
            baseline._write_json(output / "manifest.json", manifest)
            observed_manifest, contents = baseline._read_json_object(output / "manifest.json", "baseline data manifest")
            if observed_manifest != manifest or contents != baseline._json_bytes(manifest):
                raise baseline.BaselineError(f"baseline data manifest changed while being written: {output / 'manifest.json'}")
            _clear_data_incomplete_marker(output)


# Imported last: this module reads baseline.<name> only when it runs, so baseline can bind the entry point
# from this module at its own end without a load-time cycle.
from . import baseline
