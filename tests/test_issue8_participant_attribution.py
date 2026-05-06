"""Issue 8 - Atribuicao Automatica de Participante."""

from assistente_voz.participant_attribution import ParticipantAttributor


def test_assigns_roles_automatically_in_multi_participant_session():
    attr = ParticipantAttributor(confidence_threshold=0.70, assisted_speaker_id="prof_1")

    r1 = attr.assign(speaker_id="prof_1", confidence=0.92)
    r2 = attr.assign(speaker_id="patient_1", confidence=0.90)

    assert r1.participant_role == "assisted"
    assert r2.participant_role == "other"


def test_low_confidence_marks_uncertain_role():
    attr = ParticipantAttributor(confidence_threshold=0.70, assisted_speaker_id="prof_1")

    r = attr.assign(speaker_id="unknown", confidence=0.42)

    assert r.participant_role == "uncertain"
    assert r.is_uncertain is True


def test_uncertain_role_does_not_interrupt_realtime_session():
    attr = ParticipantAttributor(confidence_threshold=0.70, assisted_speaker_id="prof_1")

    r = attr.assign(speaker_id="unknown", confidence=0.50)

    assert r.should_interrupt_realtime is False


def test_without_preconfigured_assisted_first_recognized_becomes_assisted():
    attr = ParticipantAttributor(confidence_threshold=0.70)

    first = attr.assign(speaker_id="speaker_a", confidence=0.88)
    second = attr.assign(speaker_id="speaker_b", confidence=0.89)

    assert first.participant_role == "assisted"
    assert second.participant_role == "other"
