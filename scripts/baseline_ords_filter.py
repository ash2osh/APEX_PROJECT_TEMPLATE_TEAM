"""ORDS module filter for the baseline filter-ords subcommand, moved out of scripts/baseline.py.

scripts/baseline.py keeps the CLI. Names that stay there (ROOT and BaselineError) are read as baseline.<name>
at call time, so patches on scripts.baseline still reach this code. baseline re-exports the entry point that
main() calls.
"""

from __future__ import annotations

import os
import re

from .rollout import RolloutError, exclude_ords_modules


def _filter_ords(args) -> None:
    input_path = args.input if args.input.is_absolute() else baseline.ROOT / args.input
    output_path = args.output if args.output.is_absolute() else baseline.ROOT / args.output
    input_path = input_path.absolute()
    output_path = output_path.absolute()
    if input_path.resolve() == output_path.resolve():
        raise baseline.BaselineError("filter-ords input and output must be different files")
    try:
        raw = input_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise baseline.BaselineError("ORDS export must be UTF-8 without a byte-order mark")
        source = raw.decode("utf-8")
    except OSError as error:
        raise baseline.BaselineError(f"cannot read ORDS export {input_path}: {error}") from error
    except UnicodeError as error:
        raise baseline.BaselineError("ORDS export must be strict UTF-8") from error
    schemas = re.findall(r"(?im)^--[ \t]*Schema:[ \t]*([A-Z][A-Z0-9_$#]{0,127})(?:[ \t]+Date:.*)?[ \t]*$", source)
    if len(set(schemas)) != 1:
        raise baseline.BaselineError("ORDS export must contain one unambiguous '-- Schema: <OWNER>' header")
    if len(set(args.exclude_module)) != len(args.exclude_module):
        raise baseline.BaselineError("filter-ords cannot repeat an excluded module name")
    try:
        filtered, removed = exclude_ords_modules(source, args.exclude_module, schemas[0])
    except RolloutError as error:
        raise baseline.BaselineError(str(error)) from error
    if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
        raise baseline.BaselineError("filter-ords output must be a regular file and cannot be a symbolic link")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise baseline.BaselineError(f"filter-ords output has an unfinished temporary file: {temporary}")
    try:
        temporary.write_text(filtered, encoding="utf-8", newline="\n")
        os.chmod(temporary, 0o600)
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Removed ORDS modules: {', '.join(removed)}")
    print(f"Filtered ORDS export: {output_path}")


# Imported last: this module reads baseline.<name> only when it runs, so baseline can bind the entry point
# from this module at its own end without a load-time cycle.
from . import baseline
