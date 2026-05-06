"""Issue 11 - Perfis de Entrevista com Senioridade Configuravel."""

from pathlib import Path

import pytest
import yaml

from assistente_voz.bootstrap import bootstrap_app
from assistente_voz.errors import ConfigError
from assistente_voz.interview_flow import InterviewFlow


def _load_profile(profile_file: str) -> dict:
    root = Path(__file__).parent.parent
    path = root / "profiles" / profile_file
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_invalid_seniority_is_rejected_in_boot(monkeypatch):
    monkeypatch.setenv("ASSISTANT_PROFILE", "clinical_pharmacist_interviewer")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("SENIORITY_LEVEL", "staff")

    with pytest.raises(ConfigError) as exc:
        bootstrap_app(base_dir=Path.cwd())

    assert exc.value.code == "PROFILE_PARAM_INVALID"


def test_seniority_changes_question_requirements_and_weights():
    profile = _load_profile("clinical_pharmacist_interviewer.yaml")

    junior = InterviewFlow(profile_data=profile, seniority_level="junior")
    senior = InterviewFlow(profile_data=profile, seniority_level="senior")

    assert junior.minimum_questions() == 8
    assert senior.minimum_questions() == 12


def test_flow_asks_only_one_question_per_call():
    profile = _load_profile("general_interviewer.yaml")
    flow = InterviewFlow(profile_data=profile, seniority_level="junior")

    q1 = flow.next_question()
    q2 = flow.next_question()

    assert q1 is not None
    assert q2 is not None
    assert q1.text != q2.text


def test_final_report_contains_recommendation_and_required_sections():
    profile = _load_profile("clinical_nurse_interviewer.yaml")
    flow = InterviewFlow(profile_data=profile, seniority_level="pleno")

    result = flow.evaluate(
        {
            "conhecimento_tecnico_e_protocolos": 4.0,
            "seguranca_do_paciente": 4.0,
            "raciocinio_clinico": 3.5,
            "comunicacao_empatica": 4.0,
            "adaptabilidade_digital": 3.0,
        }
    )

    assert result.recommendation in {
        "aprovado",
        "aprovado_com_ressalvas",
        "banco_de_talentos",
        "nao_aprovado",
    }
    for section in [
        "resumo_do_candidato",
        "pontos_fortes",
        "pontos_de_risco",
        "aderencia_ao_nivel",
        "recomendacao_final",
        "perguntas_de_follow_up",
    ]:
        assert section in result.sections
