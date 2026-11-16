#!/usr/bin/env python3
"""Terminal-only answers for release prompts, and the explicit flag that replaces them.

A `[y/N]` prompt or a typed acknowledgment reads its answer only from a terminal: redirected
or piped input is refused, so a piped answer cannot confirm a deployment or close an attempt.
The recorded way to skip a prompt is `-y` / `--yes`; on production a run also needs
`--confirm-target <db_unique_name>`. For the acknowledgments `--yes` is recorded as
`acknowledgedBy: --yes`.
"""

from __future__ import annotations

import sys
from typing import TextIO

YES_REFUSAL = (
    "refused: this confirmation is read from a terminal; piped or redirected answers are not accepted. "
    "Run the command in a terminal, or pass --yes (-y) to accept the prompt explicitly and on the record."
)
ACKNOWLEDGMENT_REFUSAL = "refused: this acknowledgment is typed at a terminal; redirected input is not accepted."
PRODUCTION_NAME_REQUIRED = "production also needs --confirm-target <db_unique_name>"


def stdin_is_terminal(stream: TextIO | None = None) -> bool:
    """True when the answer can be typed: standard input (or the given stream) is a terminal."""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def may_read_yes_no() -> bool:
    """Gate a `[y/N]` prompt. Refuses, printing YES_REFUSAL, unless standard input is a terminal."""
    if stdin_is_terminal():
        return True
    print(YES_REFUSAL, file=sys.stderr)
    return False


def may_read_acknowledgment() -> bool:
    """Gate a typed acknowledgment. Refuses, printing ACKNOWLEDGMENT_REFUSAL, unless standard input is a terminal."""
    if stdin_is_terminal():
        return True
    print(ACKNOWLEDGMENT_REFUSAL, file=sys.stderr)
    return False


def confirmed_by(yes: bool, confirm_target: str | None) -> str:
    """The value recorded as `confirmedBy`; "terminal" when a typed answer confirmed the run."""
    flags = [flag for flag, on in (("--yes", yes), ("--confirm-target", confirm_target is not None)) if on]
    return " ".join(flags) if flags else "terminal"


def confirmed_line(yes: bool, confirm_target: str | None) -> str:
    """The one line printed when a flag replaces the prompt."""
    parts = []
    if yes:
        parts.append("--yes")
    if confirm_target is not None:
        parts.append(f"--confirm-target {confirm_target}")
    return f"Confirmed by {' and '.join(parts)}; the interactive prompt was skipped."
