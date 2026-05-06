from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import ValidationError, validate

from assistente_voz.errors import ConfigError

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "profiles" / "schema" / "profile.schema.json"


@dataclass(frozen=True)
class ProfileEntry:
    id: str
    path: Path
    data: dict


def load_catalog(profiles_dir: Path, schema_path: Path | None = None) -> dict[str, ProfileEntry]:
    """Scan profiles_dir, validate each file against JSON schema, return catalog by profile.id.

    Raises ConfigError with codes:
        PROFILE_YAML_INVALID   – file is not valid YAML
        PROFILE_SCHEMA_INVALID – file fails JSON schema validation
        PROFILE_ID_DUPLICATE   – two files share the same profile.id
    """
    if not profiles_dir.exists():
        raise ConfigError("PROFILE_DIR_MISSING", f"Profiles directory not found: {profiles_dir}")

    resolved_schema = schema_path or _SCHEMA_PATH
    schema = json.loads(resolved_schema.read_text(encoding="utf-8"))

    catalog: dict[str, ProfileEntry] = {}

    for path in sorted(profiles_dir.glob("*.yaml")):
        # --- parse YAML ---
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(
                "PROFILE_YAML_INVALID",
                f"YAML parse error in {path.name}: {exc}",
            ) from exc

        # --- validate against schema ---
        try:
            validate(instance=data, schema=schema)
        except ValidationError as exc:
            raise ConfigError(
                "PROFILE_SCHEMA_INVALID",
                f"Schema validation failed for {path.name}: {exc.message}",
            ) from exc

        profile_id = str((data.get("profile") or {}).get("id") or "").strip()

        # --- duplicate id check ---
        if profile_id in catalog:
            raise ConfigError(
                "PROFILE_ID_DUPLICATE",
                f"Duplicate profile.id '{profile_id}' found in {path.name} "
                f"(already loaded from {catalog[profile_id].path.name})",
            )

        catalog[profile_id] = ProfileEntry(id=profile_id, path=path, data=data)

    return catalog
