#!/usr/bin/env python3
"""Copy authored environment deployment descriptors into a fresh export."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


DESCRIPTORS = ("dev.json", "staging.json", "prod.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("existing_app", type=Path)
    parser.add_argument("staged_app", type=Path)
    args = parser.parse_args()

    existing = args.existing_app / "deployments"
    if not existing.is_dir():
        return 0

    staged = args.staged_app / "deployments"
    for name in DESCRIPTORS:
        source = existing / name
        if source.is_symlink():
            parser.error(f"deployment descriptor must not be a symbolic link: {source}")
        if source.is_file():
            staged.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
