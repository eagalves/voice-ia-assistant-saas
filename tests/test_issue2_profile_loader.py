"""Issue 2 – Validação Estrita de Perfis no Boot."""
import shutil
from pathlib import Path

import pytest

from assistente_voz.errors import ConfigError
from assistente_voz.profile_loader import load_catalog

# ── fixtures ──────────────────────────────────────────────────────────────────

SCHEMA_DIR = Path(__file__).parent.parent / "profiles" / "schema"
VALID_PROFILE = SCHEMA_DIR / "valid_profile_minimal.yaml"
INVALID_PROFILE = SCHEMA_DIR / "invalid_profile_example.yaml"


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_profiles_dir(tmp_path: Path, *yaml_files: Path) -> Path:
    """Copy yaml_files into tmp_path and return it."""
    for src in yaml_files:
        shutil.copy(src, tmp_path / src.name)
    return tmp_path


# ── tests ─────────────────────────────────────────────────────────────────────

def test_valid_profile_minimal_loads(tmp_path):
    """valid_profile_minimal.yaml must parse and validate without errors."""
    profiles_dir = _make_profiles_dir(tmp_path, VALID_PROFILE)
    catalog = load_catalog(profiles_dir)
    assert "valid_profile_minimal" in catalog


def test_invalid_profile_fails_with_schema_invalid(tmp_path):
    """invalid_profile_example.yaml must raise PROFILE_SCHEMA_INVALID."""
    profiles_dir = _make_profiles_dir(tmp_path, INVALID_PROFILE)
    with pytest.raises(ConfigError) as exc_info:
        load_catalog(profiles_dir)
    assert exc_info.value.code == "PROFILE_SCHEMA_INVALID"


def test_duplicate_profile_id_fails(tmp_path):
    """Two files sharing the same profile.id must raise PROFILE_ID_DUPLICATE."""
    src = VALID_PROFILE
    copy1 = tmp_path / "copy1.yaml"
    copy2 = tmp_path / "copy2.yaml"
    shutil.copy(src, copy1)
    shutil.copy(src, copy2)

    with pytest.raises(ConfigError) as exc_info:
        load_catalog(tmp_path)
    assert exc_info.value.code == "PROFILE_ID_DUPLICATE"


def test_missing_profiles_dir_raises(tmp_path):
    """A non-existent profiles directory must raise PROFILE_DIR_MISSING."""
    missing = tmp_path / "no_such_dir"
    with pytest.raises(ConfigError) as exc_info:
        load_catalog(missing)
    assert exc_info.value.code == "PROFILE_DIR_MISSING"


def test_yaml_parse_error_raises(tmp_path):
    """A malformed YAML file must raise PROFILE_YAML_INVALID."""
    bad_yaml = tmp_path / "broken.yaml"
    bad_yaml.write_text("key: [\nnot closed", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_catalog(tmp_path)
    assert exc_info.value.code == "PROFILE_YAML_INVALID"
