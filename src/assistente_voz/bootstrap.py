gitfrom dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv
import yaml

from assistente_voz.errors import ConfigError


@dataclass(frozen=True)
class BootstrapResult:
    profile_id: str
    profile_path: Path
    llm_provider: str


def _load_env() -> None:
    load_dotenv(override=False)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError("CONFIG_ENV_MISSING", f"Missing required env var: {name}")
    return value


def _resolve_profile(profile_id: str, profiles_dir: Path) -> Path:
    if not profiles_dir.exists():
        raise ConfigError("PROFILE_DIR_MISSING", f"Profiles directory not found: {profiles_dir}")

    for path in sorted(profiles_dir.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        current_id = str((data.get("profile") or {}).get("id") or "").strip()
        if current_id == profile_id:
            return path

    raise ConfigError("PROFILE_NOT_FOUND", f"Profile id not found: {profile_id}")


def bootstrap_app(base_dir: Path | None = None) -> BootstrapResult:
    _load_env()

    profile_id = _required_env("ASSISTANT_PROFILE")
    llm_provider = _required_env("LLM_PROVIDER")

    if llm_provider == "api":
        _required_env("LLM_API_KEY")

    root = base_dir or Path.cwd()
    profile_path = _resolve_profile(profile_id=profile_id, profiles_dir=root / "profiles")

    return BootstrapResult(
        profile_id=profile_id,
        profile_path=profile_path,
        llm_provider=llm_provider,
    )
