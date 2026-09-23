"""Offline SQLcl APEXlang validation.

Runs `apex validate -input <directory>` with SQLcl `/nolog` against an exact
materialized tree in a temporary directory, without connecting to a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal
from collections.abc import Mapping


@dataclass(frozen=True)
class ValidationReport:
    success: bool
    status: Literal["SUCCESS", "FAILED"]
    log: str
    errors: tuple[str, ...]


_ERROR_LINE_PATTERNS = [
    re.compile(r"\b(?:error|syntax error|validation failed|ora-\d+|pls-\d+)\b", re.IGNORECASE),
    re.compile(r"^Directory input does not contain APEXlang files", re.IGNORECASE | re.MULTILINE),
]


def validate_apexlang_tree(
    tree: Mapping[str, bytes],
    *,
    sqlcl_bin: str = "sql",
) -> ValidationReport:
    """Validate an APEXlang tree offline using SQLcl."""
    if not tree:
        return ValidationReport(
            success=False,
            status="FAILED",
            log="",
            errors=("APEXlang tree is empty; no files to validate",),
        )

    with tempfile.TemporaryDirectory(prefix="team-apex-validate-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        # Materialize the tree
        for path_str, content in tree.items():
            rel_path = Path(path_str)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                return ValidationReport(
                    success=False,
                    status="FAILED",
                    log="",
                    errors=(f"Unsafe path in tree: {path_str}",),
                )
            target_file = temp_dir / rel_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(content)

        # Run SQLcl apex validate offline (/nolog)
        command = [sqlcl_bin, "-L", "/nolog"]
        script_input = f"apex validate -input {temp_dir}\nexit\n"
        try:
            result = subprocess.run(
                command,
                input=script_input,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return ValidationReport(
                success=False,
                status="FAILED",
                log="",
                errors=(f"Failed to execute SQLcl ({sqlcl_bin}): {exc}",),
            )

        log = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        errors: list[str] = []

        # Find error lines
        for line in log.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            for pattern in _ERROR_LINE_PATTERNS:
                if pattern.search(line_stripped):
                    errors.append(line_stripped)
                    break

        if result.returncode != 0:
            errors.append(f"SQLcl apex validate exited with returncode {result.returncode}")

        has_success_marker = "validation successful" in (result.stdout or "").lower()
        if not has_success_marker:
            errors.append("Validation successful marker was not found in SQLcl output")

        success = (result.returncode == 0) and has_success_marker and (len(errors) == 0)
        status: Literal["SUCCESS", "FAILED"] = "SUCCESS" if success else "FAILED"
        return ValidationReport(
            success=success,
            status=status,
            log=log,
            errors=tuple(errors),
        )
