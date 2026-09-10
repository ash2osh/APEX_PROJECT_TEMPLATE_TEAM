"""Strict local configuration and target-contract parsing.

The module is deliberately independent of SQLcl and the database.  Online
commands consume the validated objects defined here; offline commands can use
the same parser helpers without loading a project environment at all.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from collections.abc import Mapping


class ConfigError(ValueError):
    """Raised when local configuration or a target contract is unsafe."""


PROFILE_NAMES = ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
PROFILE_SUFFIXES = (
    "SQLCL_CONNECTION",
    "EXPECTED_USER",
    "EXPECTED_CURRENT_SCHEMA",
    "EXPECTED_DB_NAME",
    "EXPECTED_SERVICE",
    "EXPECTED_INSTANCE_ID",
)
BASE_KEYS = {
    "PROJECT_NAME",
    "TARGET_ROLE",
    "DB_ENVIRONMENT",
    "APEX_APPS",
    "TABLES_SCHEMA",
    "CODE_SCHEMA",
    "APEX_PARSING_SCHEMA",
    "METADATA_SCHEMA",
    "APEX_WORKSPACE_ID",
    "APP_OWNERSHIP_MODE",
}
PROFILE_KEYS = {
    f"{profile}_{suffix}"
    for profile in PROFILE_NAMES
    for suffix in PROFILE_SUFFIXES
}
KNOWN_KEYS = BASE_KEYS | PROFILE_KEYS

# These commands accept only explicit local inputs and must not load .env or
# database profiles.  Keep this list in one place for team.py and tests.
OFFLINE_COMMANDS = frozenset(
    {
        "new-migration",
        "add-dependency",
        "migration-plan",
        "build-release",
        "verify-release",
        "plan-release",
        "apply-release",
        "gen-runbook",
        "explain-conflict",
        "ci-doctor",
        "ci-replay",
        "snapshot",
        "verify-history",
        "replay",
        "adopt-baseline",
        "prune-scratch",
    }
)

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ORACLE_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_CONNECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Profile:
    name: str
    connection: str
    expected_user: str
    expected_current_schema: str
    expected_db_name: str
    expected_service: str
    expected_instance_id: str


@dataclass(frozen=True)
class Config:
    values: Mapping[str, str]
    profiles: Mapping[str, Profile]
    apps: Mapping[str, int]
    project: str
    role: str
    environment: str
    tables_schema: str
    code_schema: str
    apex_parsing_schema: str
    metadata_schema: str
    workspace_id: int
    ownership_mode: str


@dataclass(frozen=True)
class Target:
    project: str
    role: str
    environment: str
    connection: str
    instance_id: str
    db_name: str
    service: str
    session_user: str
    current_schema: str
    alias: str | None
    workspace_id: int | None
    app_id: int | None
    parsing_schema: str | None
    ownership_mode: str
    binding_digest: str

    @property
    def state_key(self) -> str:
        """Identity for local baselines/checkpoints, including the binding."""
        payload = {
            "key_version": "state-v1",
            "project": self.project,
            "role": self.role,
            "environment": self.environment,
            "connection": self.connection,
            "instance_id": self.instance_id,
            "db_name": self.db_name,
            "service": self.service,
            "session_user": self.session_user,
            "current_schema": self.current_schema,
            "alias": self.alias,
            "workspace_id": self.workspace_id,
            "app_id": self.app_id,
            "parsing_schema": self.parsing_schema,
            "ownership_mode": self.ownership_mode,
            "binding_digest": self.binding_digest,
        }
        return _digest(payload)

    @property
    def physical_key(self) -> str:
        """Identity for the shared physical application mutex."""
        if self.workspace_id is None or self.app_id is None:
            raise ConfigError("physical application identity requires workspace and app IDs")
        return _digest(
            ["app-lock-v1", self.instance_id, self.workspace_id, self.app_id]
        )


@dataclass(frozen=True)
class RecoveryOwner:
    role: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class TargetContract:
    version: int
    project: str
    role: str
    environment: str
    instance_id: str | None
    db_name: str | None
    service: str | None
    session_user: str | None
    current_schema: str | None
    workspace_id: int | None
    app_ids: Mapping[str, int]
    recovery_owner: RecoveryOwner
    binding: Mapping[str, Any]


def _digest(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _require_text(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        raise ConfigError(f"{key} is required")
    return value


def _validate_oracle_identifier(key: str, value: str) -> str:
    if not _ORACLE_IDENTIFIER_RE.fullmatch(value):
        raise ConfigError(f"{key} must be an uppercase Oracle identifier")
    return value


def _validate_connection(key: str, value: str) -> str:
    if not _CONNECTION_RE.fullmatch(value):
        raise ConfigError(f"{key} contains unsupported characters")
    return value


def _validate_positive_int(key: str, value: str) -> int:
    if not _POSITIVE_INT_RE.fullmatch(value):
        raise ConfigError(f"{key} must be a positive integer")
    return int(value)


def parse_apps(value: str) -> dict[str, int]:
    """Parse the strict lowercase alias-to-positive-ID contract."""
    if not value or value.strip() != value:
        raise ValueError("APEX_APPS must not be empty or contain surrounding whitespace")
    result: dict[str, int] = {}
    seen_ids: set[int] = set()
    for item in value.split(","):
        if not item or item.count(":") != 1:
            raise ValueError("APEX_APPS must be alias:id comma-separated values")
        alias, raw_id = item.split(":", 1)
        if not _ALIAS_RE.fullmatch(alias):
            raise ValueError(f"invalid application alias: {alias!r}")
        if alias.casefold() in {known.casefold() for known in result}:
            raise ValueError(f"duplicate application alias: {alias}")
        if not _POSITIVE_INT_RE.fullmatch(raw_id):
            raise ValueError(f"invalid application ID for {alias}: {raw_id!r}")
        app_id = int(raw_id)
        if app_id in seen_ids:
            raise ValueError(f"duplicate application ID: {app_id}")
        result[alias] = app_id
        seen_ids.add(app_id)
    if not result:
        raise ValueError("APEX_APPS must contain at least one application")
    return result


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_env_text(text: str, *, source: str = "<memory>") -> dict[str, str]:
    """Parse literal KEY=VALUE lines without evaluating shell syntax."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r")
        if not line or line.lstrip().startswith("#"):
            continue
        if _CONTROL_RE.search(line):
            raise ConfigError(f"control character in {source}:{line_number}")
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise ConfigError(f"invalid setting in {source}:{line_number}")
        key, raw_value = match.groups()
        if not _KEY_RE.fullmatch(key) or key not in KNOWN_KEYS:
            raise ConfigError(f"unsupported setting in {source}:{line_number}: {key}")
        if key in values:
            raise ConfigError(f"duplicate setting in {source}:{line_number}: {key}")
        value = _unquote(raw_value)
        if value == "":
            raise ConfigError(f"empty setting in {source}:{line_number}: {key}")
        if _CONTROL_RE.search(value):
            raise ConfigError(f"control character in {source}:{line_number}: {key}")
        values[key] = value
    return values


def _profile(values: Mapping[str, str], name: str) -> Profile:
    required = {f"{name}_{suffix}" for suffix in PROFILE_SUFFIXES}
    missing = sorted(required - values.keys())
    if missing:
        raise ConfigError(f"incomplete {name} profile; missing: {', '.join(missing)}")
    return Profile(
        name=name,
        connection=_validate_connection(
            f"{name}_SQLCL_CONNECTION", values[f"{name}_SQLCL_CONNECTION"]
        ),
        expected_user=_validate_oracle_identifier(
            f"{name}_EXPECTED_USER", values[f"{name}_EXPECTED_USER"]
        ),
        expected_current_schema=_validate_oracle_identifier(
            f"{name}_EXPECTED_CURRENT_SCHEMA", values[f"{name}_EXPECTED_CURRENT_SCHEMA"]
        ),
        expected_db_name=_require_text(values, f"{name}_EXPECTED_DB_NAME"),
        expected_service=_require_text(values, f"{name}_EXPECTED_SERVICE"),
        expected_instance_id=_require_text(values, f"{name}_EXPECTED_INSTANCE_ID"),
    )


def load_config(path: str | Path, *, require_verify: bool = False) -> Config:
    """Load and validate the online profile set from a literal env file."""
    env_path = Path(path)
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read configuration: {env_path}") from exc
    values = parse_env_text(text, source=str(env_path))

    project = _require_text(values, "PROJECT_NAME")
    role = _require_text(values, "TARGET_ROLE")
    if role not in {"developer", "integration", "test", "replay", "operator", "production"}:
        raise ConfigError("TARGET_ROLE must be developer, integration, test, replay, operator, or production")
    environment = _require_text(values, "DB_ENVIRONMENT")
    if environment not in {"development", "test", "staging", "production"}:
        raise ConfigError("DB_ENVIRONMENT must be development, test, staging, or production")
    # The production classification must be unambiguous: run_sqlcl gates writes
    # on environment while the workflow commands gate on role, so a mismatched
    # pair weakens one of the two guards without any warning.
    if (role == "production") != (environment == "production"):
        raise ConfigError(
            "TARGET_ROLE and DB_ENVIRONMENT must both be production or neither"
        )
    apps = parse_apps(_require_text(values, "APEX_APPS"))

    tables_schema = _validate_oracle_identifier(
        "TABLES_SCHEMA", _require_text(values, "TABLES_SCHEMA")
    )
    code_schema = _validate_oracle_identifier(
        "CODE_SCHEMA", _require_text(values, "CODE_SCHEMA")
    )
    apex_schema = _validate_oracle_identifier(
        "APEX_PARSING_SCHEMA", _require_text(values, "APEX_PARSING_SCHEMA")
    )
    metadata_schema = _validate_oracle_identifier(
        "METADATA_SCHEMA", _require_text(values, "METADATA_SCHEMA")
    )
    if metadata_schema in {tables_schema, code_schema}:
        raise ConfigError("METADATA_SCHEMA must differ from TABLES_SCHEMA and CODE_SCHEMA")

    workspace_id = _validate_positive_int(
        "APEX_WORKSPACE_ID", _require_text(values, "APEX_WORKSPACE_ID")
    )
    ownership_mode = values.get("APP_OWNERSHIP_MODE", "shared")
    if ownership_mode not in {"shared", "single"}:
        raise ConfigError("APP_OWNERSHIP_MODE must be shared or single")

    profiles: dict[str, Profile] = {}
    for name in PROFILE_NAMES[:-1]:
        profiles[name] = _profile(values, name)
    verify_keys = {f"VERIFY_{suffix}" for suffix in PROFILE_SUFFIXES}
    present_verify = verify_keys.intersection(values)
    if present_verify and present_verify != verify_keys:
        missing = sorted(verify_keys - present_verify)
        raise ConfigError(f"incomplete VERIFY profile; missing: {', '.join(missing)}")
    if require_verify:
        profiles["VERIFY"] = _profile(values, "VERIFY")
    elif present_verify:
        profiles["VERIFY"] = _profile(values, "VERIFY")

    instances = {profile.expected_instance_id for profile in profiles.values()}
    if len(instances) != 1:
        raise ConfigError("all configured profiles must use the same EXPECTED_INSTANCE_ID")

    return Config(
        values=values,
        profiles=profiles,
        apps=apps,
        project=project,
        role=role,
        environment=environment,
        tables_schema=tables_schema,
        code_schema=code_schema,
        apex_parsing_schema=apex_schema,
        metadata_schema=metadata_schema,
        workspace_id=workspace_id,
        ownership_mode=ownership_mode,
    )


def profile_target(
    config: Config,
    profile_name: str,
    *,
    alias: str | None = None,
    app_id: int | None = None,
) -> Target:
    """Bind a validated profile to an optional application alias."""
    name = profile_name.upper()
    if name not in config.profiles:
        raise ConfigError(f"profile is not configured: {name}")
    profile = config.profiles[name]
    if name == "APEX":
        if alias is None:
            raise ConfigError("APEX targets require an application alias")
        if alias not in config.apps:
            raise ConfigError(f"unknown application alias: {alias}")
        bound_app_id = config.apps[alias]
        if app_id is not None and app_id != bound_app_id:
            raise ConfigError(f"application ID does not match alias {alias}")
        target_app_id = bound_app_id
        workspace_id: int | None = config.workspace_id
        parsing_schema: str | None = config.apex_parsing_schema
    else:
        if alias is not None or app_id is not None:
            raise ConfigError(f"{name} targets cannot bind an application alias")
        target_app_id = None
        workspace_id = None
        parsing_schema = None

    binding = {
        "profile": name,
        "connection": profile.connection,
        "instance_id": profile.expected_instance_id,
        "db_name": profile.expected_db_name,
        "service": profile.expected_service,
        "session_user": profile.expected_user,
        "current_schema": profile.expected_current_schema,
        "alias": alias,
        "app_id": target_app_id,
        "workspace_id": workspace_id,
        "parsing_schema": parsing_schema,
    }
    return Target(
        project=config.project,
        role=config.role,
        environment=config.environment,
        connection=profile.connection,
        instance_id=profile.expected_instance_id,
        db_name=profile.expected_db_name,
        service=profile.expected_service,
        session_user=profile.expected_user,
        current_schema=profile.expected_current_schema,
        alias=alias,
        workspace_id=workspace_id,
        app_id=target_app_id,
        parsing_schema=parsing_schema,
        ownership_mode=config.ownership_mode,
        binding_digest=_digest(binding),
    )


_TARGET_KEYS = {
    "version",
    "project",
    "role",
    "environment",
    "instance_id",
    "db_name",
    "service",
    "session_user",
    "current_schema",
    "workspace_id",
    "app_ids",
    "recovery_owner",
    "binding",
}
_BINDING_KEYS = {
    "connection", "sqlcl_connection", "profile", "schema", "instance_id",
    "db_name", "service", "session_user", "current_schema", "workspace_id",
    "app_id", "alias", "parsing_schema", "ownership_mode", "metadata_schema",
}
_SECRET_WORDS = re.compile(r"(?:password|passwd|secret|token|credential|wallet|private[_-]?key)", re.I)


def _reject_secret_keys(value: Any, path: str = "target") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _SECRET_WORDS.search(key_text):
                raise ConfigError(f"secret-bearing target field is prohibited: {path}.{key_text}")
            _reject_secret_keys(child, f"{path}.{key_text}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}[{index}]")
    elif isinstance(value, str) and _CONTROL_RE.search(value):
        raise ConfigError(f"control character in target contract: {path}")


def _contract_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"target contract requires non-empty {key}")
    return value


def parse_target_contract(
    source: Mapping[str, Any] | str | Path,
    *,
    expected_role: str | None = None,
) -> TargetContract:
    """Parse a credential-free tracked target JSON contract."""
    if isinstance(source, (str, Path)):
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid target contract: {source}") from exc
    else:
        data = dict(source)
    if not isinstance(data, dict):
        raise ConfigError("target contract must be a JSON object")
    _reject_secret_keys(data)
    unknown = sorted(set(data) - _TARGET_KEYS)
    if unknown:
        raise ConfigError(f"unsupported target contract fields: {', '.join(unknown)}")
    if data.get("version") != 1:
        raise ConfigError("target contract version must be 1")

    project = _contract_text(data, "project")
    role = _contract_text(data, "role")
    if role not in {"developer", "integration", "test", "replay", "operator", "production"}:
        raise ConfigError("target contract role is invalid")
    if expected_role is not None and role != expected_role:
        raise ConfigError(f"target contract role {role!r} does not match {expected_role!r}")
    environment = _contract_text(data, "environment")
    if environment not in {"development", "test", "staging", "production"}:
        raise ConfigError("target contract environment is invalid")
    if (role == "production") != (environment == "production"):
        raise ConfigError(
            "target contract role and environment must both be production or neither"
        )

    identity_keys = {"instance_id", "db_name", "service", "session_user", "current_schema"}
    present_identity = identity_keys.intersection(data)
    development_env_contract = role == "developer" and environment == "development"
    if development_env_contract and present_identity and present_identity != identity_keys:
        missing = sorted(identity_keys - present_identity)
        raise ConfigError(
            "development target identity belongs in .env; partial contract fields missing: "
            + ", ".join(missing)
        )
    if not development_env_contract and present_identity != identity_keys:
        missing = sorted(identity_keys - present_identity)
        raise ConfigError("target contract identity is incomplete: " + ", ".join(missing))
    instance_id = _contract_text(data, "instance_id") if "instance_id" in data else None
    db_name = _contract_text(data, "db_name") if "db_name" in data else None
    service = _contract_text(data, "service") if "service" in data else None
    session_user = (
        _validate_oracle_identifier("session_user", _contract_text(data, "session_user"))
        if "session_user" in data
        else None
    )
    current_schema = (
        _validate_oracle_identifier("current_schema", _contract_text(data, "current_schema"))
        if "current_schema" in data
        else None
    )

    workspace_raw = data.get("workspace_id")
    workspace_id: int | None
    if workspace_raw is None:
        workspace_id = None
    elif isinstance(workspace_raw, int) and workspace_raw > 0:
        workspace_id = workspace_raw
    else:
        raise ConfigError("workspace_id must be a positive integer when present")
    if not development_env_contract and workspace_id is None:
        raise ConfigError("workspace_id is required for non-development targets")

    app_ids_raw = data.get("app_ids", {})
    if not isinstance(app_ids_raw, dict):
        raise ConfigError("app_ids must be an object")
    app_ids: dict[str, int] = {}
    for alias, value in app_ids_raw.items():
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            raise ConfigError(f"invalid target application alias: {alias!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"invalid application ID for target alias {alias}")
        if value in app_ids.values():
            raise ConfigError(f"duplicate target application ID: {value}")
        app_ids[alias] = value
    if not development_env_contract and not app_ids:
        raise ConfigError("non-development targets require at least one application ID")

    owner = data.get("recovery_owner")
    if not isinstance(owner, dict):
        raise ConfigError("recovery_owner is required")
    owner_role = owner.get("role")
    members = owner.get("members")
    if (
        not isinstance(owner_role, str)
        or not owner_role
        or not isinstance(members, list)
        or len(members) < 2
        or any(not isinstance(member, str) or not member for member in members)
        or len(set(members)) != len(members)
    ):
        raise ConfigError("recovery_owner requires a role and at least two unique members")

    binding = data.get("binding", {})
    if not isinstance(binding, dict):
        raise ConfigError("binding must be an object when present")
    unknown_binding = sorted(set(binding) - _BINDING_KEYS)
    if unknown_binding:
        raise ConfigError("unsupported target binding fields: " + ", ".join(unknown_binding))
    connection = binding.get("connection", binding.get("sqlcl_connection"))
    if connection is not None:
        # Every other field in this parser is type-checked before it reaches a
        # regex. Without this, a JSON number here escapes the ConfigError
        # contract as a TypeError and team.py prints a traceback.
        if not isinstance(connection, str):
            raise ConfigError("target binding.connection must be a credential-free connection name")
        _validate_connection("binding.connection", connection)
    for key, actual in (
        ("instance_id", instance_id), ("db_name", db_name), ("service", service),
        ("session_user", session_user), ("current_schema", current_schema),
        ("workspace_id", workspace_id),
    ):
        if key in binding and actual is not None and binding[key] != actual:
            raise ConfigError(f"target binding.{key} does not match the target identity")
    if "schema" in binding:
        schema = binding["schema"]
        if not isinstance(schema, str):
            raise ConfigError("target binding.schema must be an uppercase Oracle identifier")
        _validate_oracle_identifier("binding.schema", schema)
        if current_schema is not None and schema != current_schema:
            raise ConfigError("target binding.schema does not match the target identity")
    if "app_id" in binding:
        alias = binding.get("alias")
        if not isinstance(alias, str) or alias not in app_ids or binding["app_id"] != app_ids[alias]:
            raise ConfigError("target binding app_id/alias does not match app_ids")
    if "ownership_mode" in binding and binding["ownership_mode"] not in {"shared", "single"}:
        raise ConfigError("target binding ownership_mode must be shared or single")
    return TargetContract(
        version=1,
        project=project,
        role=role,
        environment=environment,
        instance_id=instance_id,
        db_name=db_name,
        service=service,
        session_user=session_user,
        current_schema=current_schema,
        workspace_id=workspace_id,
        app_ids=app_ids,
        recovery_owner=RecoveryOwner(owner_role, tuple(members)),
        binding=binding,
    )


_LAUNCHER_NAMES = frozenset({"team.py", "team.sh", "team.ps1"})


def is_offline_command(command: str) -> bool:
    """Return whether a team command can run without loading database config.

    Accepts a bare command name, or a full invocation whose first token is a
    launcher -- with or without a directory prefix. The previous implementation
    stripped a path only from a token that already started with ``team.py``,
    which is exactly the token that needs no stripping, so every prefixed form
    reported False.
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    first = tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    if first in _LAUNCHER_NAMES:
        if len(tokens) < 2:
            return False
        first = tokens[1]
    return first in OFFLINE_COMMANDS
