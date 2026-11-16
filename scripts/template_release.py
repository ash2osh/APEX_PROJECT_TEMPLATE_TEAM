"""Read the APEX release line of the template that holds this checkout.

The release is the `apexRelease` key of template-manifest.json. The 26.1 template
records no `apexRelease`, so its absence means 26.1 (the same rule as
template_drift.py and verify_local.py). A manifest that cannot be read, or that
names an unknown release, raises: nothing falls back silently.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_NAME = "template-manifest.json"
RELEASE_26_1 = "26.1"
RELEASE_26_2 = "26.2"
RELEASES = (RELEASE_26_1, RELEASE_26_2)


class TemplateReleaseError(RuntimeError):
    """The template manifest is unreadable or does not name a known APEX release."""


def read_template_release(root: Path) -> str:
    """Return "26.1" or "26.2" for the template rooted at root; raise TemplateReleaseError otherwise."""
    path = Path(root) / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateReleaseError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateReleaseError(f"{path} must hold a JSON object")
    if "apexRelease" not in payload:
        return RELEASE_26_1
    release = payload["apexRelease"]
    if not isinstance(release, str) or release not in RELEASES:
        raise TemplateReleaseError(f"{path}: apexRelease must be one of {', '.join(RELEASES)}, found {release!r}")
    return release


def is_apex_26_2(root: Path) -> bool:
    """True when the template rooted at root is the 26.2 release line."""
    return read_template_release(root) == RELEASE_26_2


__all__ = [
    "MANIFEST_NAME",
    "RELEASE_26_1",
    "RELEASE_26_2",
    "RELEASES",
    "TemplateReleaseError",
    "is_apex_26_2",
    "read_template_release",
]
