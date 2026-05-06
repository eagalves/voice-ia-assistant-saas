"""Issue 10 - Regras Clinicas do Perfil Medical."""

from assistente_voz.medical_rules import MedicalProfileEngine, REQUIRED_FIELDS


def _base_complete_case() -> dict[str, str]:
    return {
        "queixa_principal": "dor de cabeça",
        "inicio": "ontem",
        "duracao": "24 horas",
        "intensidade": "7/10",
        "sintomas_associados": "náusea",
        "medicacoes_em_uso": "dipirona",
        "alergias": "nenhuma",
        "sinais_de_alerta_atuais": "nenhum",
    }


def test_missing_fields_generate_partial_record_with_pending_notice():
    engine = MedicalProfileEngine()
    data = _base_complete_case()
    data["duracao"] = ""
    data["alergias"] = ""

    record = engine.build_record(data)

    assert record.status == "partial"
    assert "duracao" in record.missing_fields
    assert "alergias" in record.missing_fields
    assert "duracao" in record.sections["perguntas_faltantes"]


def test_red_flags_include_alert_section_and_emergency_guidance():
    engine = MedicalProfileEngine()
    data = _base_complete_case()
    data["sintomas_associados"] = "dor no peito e falta de ar"

    record = engine.build_record(data)

    assert record.red_flags_detected
    assert "dor no peito" in record.sections["sinais_de_alerta"]
    assert "emergência" in record.sections["orientacao_final_de_seguranca"].lower() or "emergencia" in record.sections["orientacao_final_de_seguranca"].lower()


def test_complete_case_generates_final_record_with_required_sections():
    engine = MedicalProfileEngine()
    data = _base_complete_case()

    record = engine.build_record(data)

    assert record.status == "final"
    assert record.missing_fields == []
    for section in [
        "resumo_do_caso",
        "perguntas_faltantes",
        "sinais_de_alerta",
        "hipoteses_possiveis",
        "exames_sugeridos_para_discutir",
        "orientacao_final_de_seguranca",
    ]:
        assert section in record.sections
