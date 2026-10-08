#!/usr/bin/env python3
"""Strict, shared contract for qualified APEX 26.2 deployment overrides.

The selected file stands alone. Generated defaults supply observations, never
another layer of import overrides. Error messages name paths without values.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


class DescriptorError(ValueError):
    """The descriptor or effective deployment could not be verified."""


SCHEMA = {
    "workspace": {"name": "text"},
    "subscription": {"masterApps": "mapping"},
    "app": {
        "id": "id", "name": "text",
        "databaseSession": {"parsingSchema": "schema"},
        "runtime": {"debugging": "bool", "logging": "bool"},
        "sessionStateProtection": {"checksumSalt": "salt", "allowUrlsCreatedAfter": "timestamp"},
    },
}


def _check(value, contract, path):
    if isinstance(contract, dict):
        if not isinstance(value, dict):
            raise DescriptorError(f"deployment {path or 'root'} must be an object")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key not in contract:
                raise DescriptorError(f"unsupported deployment property: {child_path}")
            _check(child, contract[key], child_path)
        return
    if contract == "mapping":
        if not isinstance(value, dict) or any(not isinstance(key, str) or re.fullmatch(r'[1-9][0-9]{0,17}', key) is None or type(destination) is not int or not 0 < destination < 10**18 for key, destination in value.items()):
            raise DescriptorError(f"deployment {path} must map positive application ID strings to numeric application IDs")
        return
    if contract == "id":
        valid = type(value) is int and 0 < value < 10**18
    elif contract == "bool":
        valid = type(value) is bool
    elif contract == "schema":
        valid = isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", value)
    elif contract == "salt":
        valid = isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
    elif contract == 'timestamp':
        valid = isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', value)
        if valid:
            try:
                datetime.fromisoformat(value)
            except ValueError:
                valid = False
    else:
        valid = isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 255 and not any(ord(c) < 32 for c in value)
    if not valid:
        suffix = {"id": "must be a number (positive integer)", "schema": "must be an uppercase Oracle identifier",
                  "salt": "must contain exactly 64 hexadecimal characters", "bool": "must be a JSON boolean", 'timestamp': 'must use the qualified YYYY-MM-DDTHH:MM:SS format'}.get(contract, "must be a non-empty single-line string")
        raise DescriptorError(f"deployment {path} {suffix}")


def _get(data, path):
    try:
        for key in path.split("."):
            data = data[key]
        return data
    except (KeyError, TypeError):
        raise DescriptorError(f"deployment {path} is required") from None


def validate_descriptor(data: dict, app_id: int) -> None:
    try:
        _check(data, SCHEMA, "")
    except DescriptorError as exc:
        if str(exc).startswith("deployment app.id must"):
            raise DescriptorError(f'{exc}: set "id": {app_id} in the descriptor') from None
        raise
    for path in ("workspace.name", "app.id", "app.databaseSession.parsingSchema"):
        _get(data, path)
    if _get(data, "app.id") != app_id:
        raise DescriptorError(f'deployment app.id is {_get(data, "app.id")} but this is application {app_id}: set "id": {app_id} in the descriptor')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DescriptorError("invalid deployment descriptor: duplicate JSON property")
        result[key] = value
    return result


def _constant(_value):
    raise DescriptorError("invalid deployment descriptor: nonfinite JSON number")


def read_json(path: Path) -> dict:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise DescriptorError("deployment descriptor must not be a symbolic link")
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise DescriptorError("invalid deployment descriptor: it starts with a UTF-8 byte-order mark; save it without one")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DescriptorError("invalid deployment descriptor: cannot read strict UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise DescriptorError("deployment descriptor root must be an object")
    return data


def read_descriptor(path: Path, app_id: int) -> dict:
    data = read_json(path)
    validate_descriptor(data, app_id)
    return data


def verify_effective_deployment(expected: dict, observed: dict) -> None:
    validate_descriptor(expected, _get(expected, "app.id"))

    def compare(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                compare(value, path)
            else:
                actual = _get(observed, path)
                if path.endswith(".checksumSalt") and isinstance(actual, str):
                    actual, value = actual.upper(), value.upper()
                if type(actual) is not type(value) or actual != value:
                    raise DescriptorError(f"effective deployment mismatch: {path}")
    compare(expected)


def observe_deployment(public_state: Path, generated: Path, expected: dict) -> dict:
    """Combine public identity/runtime observations with generated salt evidence."""
    observed = read_json(public_state)
    if expected['app'].get('sessionStateProtection'):
        default = read_json(generated)
        for key in expected['app']['sessionStateProtection']:
            path = 'app.sessionStateProtection.' + key
            value = _get(default, path)
            _check(value, SCHEMA['app']['sessionStateProtection'][key], path)
            observed.setdefault('app', {}).setdefault('sessionStateProtection', {})[key] = value
    mappings = expected.get('subscription', {}).get('masterApps')
    if mappings is not None:
        default = read_json(generated)
        exported = default.get('subscription', {}).get('masterApps', {})
        _check(exported, 'mapping', 'subscription.masterApps')
        masters = observed.get('subscription', {}).get('masterApplicationIds')
        if not isinstance(masters, list) or any(type(identifier) not in (int, float) for identifier in masters):
            raise DescriptorError('public subscription destination evidence is required')
        for destination in mappings.values():
            if destination not in masters or exported.get(str(destination)) != destination:
                raise DescriptorError('effective deployment mismatch: subscription.masterApps')
        observed['subscription'] = {'masterApps': dict(mappings)}
    verify_effective_deployment(expected, observed)
    return observed


def remap_subscription_source(raw: bytes, expected: dict) -> bytes:
    """Materialize only the native qualified subscription/master reference group.

    SQL/PLSQL fenced text and all unrelated source remain exact bytes. Expected
    source is remapped once; observed source is never normalized through a map.
    """
    mappings = expected.get('subscription', {}).get('masterApps', {})
    if not mappings:
        return raw
    lines = raw.splitlines(keepends=True)
    fenced = False
    for index, line in enumerate(lines):
        if re.search(rb'(?:^|\s)```[A-Za-z0-9_-]*\s*$', line):
            fenced = not fenced
        if fenced or index == 0 or index + 1 >= len(lines):
            continue
        match = re.fullmatch(rb'( +)master: @/([1-9][0-9]{0,17})(/[^\r\n]+)(\r?\n)', line)
        if match is None:
            continue
        indent = match.group(1)[:-4]
        if lines[index - 1].strip() != b'subscription {' or lines[index + 1].strip() != b'}' or not indent:
            continue
        destination = mappings.get(match.group(2).decode('ascii'))
        if destination is not None:
            lines[index] = match.group(1) + b'master: @/' + str(destination).encode('ascii') + match.group(3) + match.group(4)
    return b''.join(lines)


def source_projection(raw: bytes, expected: dict) -> bytes:
    """Ignore only the qualified top-level name materialized by a selected override.

    Runtime flags live outside the exported application source in measured 26.2
    exports. Salt is verified from generated deployment evidence. All remaining
    application bytes and every other source file retain exact comparison.
    """
    if "name" not in expected["app"]:
        return raw
    lines = raw.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if re.match(rb"^    name: ", line)]
    if len(matches) != 1:
        raise DescriptorError("cannot isolate the qualified application name override")
    del lines[matches[0]]
    return b"".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("app_id", type=int)
    parser.add_argument("--json", action="store_true", help="emit validated JSON for wrapper consumption")
    args = parser.parse_args(argv)
    try:
        data = read_descriptor(args.descriptor, args.app_id)
        if args.json:
            print(json.dumps(data, ensure_ascii=True))
        else:
            print(data["workspace"]["name"] + "\t" + data["app"]["databaseSession"]["parsingSchema"])
        return 0
    except DescriptorError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
