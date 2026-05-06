from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

from assistente_voz.errors import ConfigError
from assistente_voz.profile_loader import ProfileEntry, load_catalog


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


def _resolve_profile_entry(profile_id: str, profiles_dir: Path) -> ProfileEntry:
    catalog = load_catalog(profiles_dir)
    if profile_id not in catalog:
        raise ConfigError("PROFILE_NOT_FOUND", f"Profile id not found: {profile_id}")
    return catalog[profile_id]


def _validate_profile_params(profile: ProfileEntry) -> None:
    profile_id = profile.id

    # Interview profiles must validate SENIORITY_LEVEL strictly.
    if "interviewer" in profile_id:
        seniority = os.getenv("SENIORITY_LEVEL", "").strip().lower()
        allowed = {"junior", "pleno", "senior"}
        if seniority not in allowed:
            raise ConfigError(
                "PROFILE_PARAM_INVALID",
                "SENIORITY_LEVEL must be one of: junior, pleno, senior",
            )

    # Medical profile supports explicit session mode.
    if profile_id == "medical":
        mode = os.getenv("MEDICAL_SESSION_MODE", "consultation").strip().lower()
        allowed_modes = {"consultation", "doctor_followup"}
        if mode not in allowed_modes:
            raise ConfigError(
                "PROFILE_PARAM_INVALID",
                "MEDICAL_SESSION_MODE must be one of: consultation, doctor_followup",
            )


def bootstrap_app(base_dir: Path | None = None) -> BootstrapResult:
    _load_env()

    profile_id = _required_env("ASSISTANT_PROFILE")
    llm_provider = _required_env("LLM_PROVIDER")

    if llm_provider == "api":
        _required_env("LLM_API_KEY")

    root = base_dir or Path.cwd()
    profile_entry = _resolve_profile_entry(profile_id=profile_id, profiles_dir=root / "profiles")
    _validate_profile_params(profile_entry)

    return BootstrapResult(
        profile_id=profile_id,
        profile_path=profile_entry.path,
        llm_provider=llm_provider,
    )
