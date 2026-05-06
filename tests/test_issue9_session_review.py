"""Issue 9 - Revisao Final de Trechos Incertos por Criticidade."""

from assistente_voz.session_review import SessionReview


def test_medical_profile_blocks_finalization_until_review_is_done():
    review = SessionReview(profile_id="medical")
    review.add_uncertain_segment(segment_id="s1", content="fala incerta")

    check = review.finalization_check()

    assert check.can_finalize is False
    assert check.reason == "CRITICAL_PROFILE_REVIEW_REQUIRED"


def test_non_critical_profile_allows_finalization_without_mandatory_review():
    review = SessionReview(profile_id="general_interviewer")
    review.add_uncertain_segment(segment_id="s1", content="fala incerta")

    check = review.finalization_check()

    assert check.can_finalize is True
    assert check.reason is None


def test_finalization_allowed_after_all_uncertain_segments_are_reviewed():
    review = SessionReview(profile_id="medical")
    review.add_uncertain_segment(segment_id="s1", content="fala 1")
    review.add_uncertain_segment(segment_id="s2", content="fala 2")

    review.review_segment(segment_id="s1", resolved_role="assisted", reviewer="clinician")
    review.review_segment(segment_id="s2", resolved_role="other", reviewer="clinician")

    check = review.finalization_check()

    assert check.can_finalize is True
    assert check.reason is None


def test_review_decisions_are_recorded_for_audit():
    review = SessionReview(profile_id="medical")
    review.add_uncertain_segment(segment_id="s1", content="fala")
    review.review_segment(
        segment_id="s1",
        resolved_role="assisted",
        reviewer="dr_house",
        note="confirmado por contexto",
    )

    trail = review.audit_trail()

    assert len(trail) == 1
    assert trail[0].segment_id == "s1"
    assert trail[0].resolved_role == "assisted"
    assert trail[0].reviewer == "dr_house"
    assert trail[0].note == "confirmado por contexto"
