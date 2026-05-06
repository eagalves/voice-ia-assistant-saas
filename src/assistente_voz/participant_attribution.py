from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttributionResult:
    """Participant attribution output for one utterance."""

    participant_role: str  # assisted | other | uncertain
    is_uncertain: bool
    should_interrupt_realtime: bool


class ParticipantAttributor:
    """Assign roles in multi-participant sessions with fail-soft uncertainty.

    Behaviour:
    - If confidence is below threshold, returns role=uncertain.
    - If a known assisted speaker id is configured, that id maps to assisted.
    - Other recognized speakers map to other.
    - Without configured assisted id, first recognized speaker becomes assisted.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.70,
        assisted_speaker_id: str | None = None,
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._assisted_speaker_id = assisted_speaker_id
        self._auto_assisted_id: str | None = None

    def assign(self, speaker_id: str | None, confidence: float) -> AttributionResult:
        """Assign participant role from speaker identity confidence."""
        if confidence < self._confidence_threshold or not speaker_id:
            return AttributionResult(
                participant_role="uncertain",
                is_uncertain=True,
                should_interrupt_realtime=False,
            )

        assisted_id = self._assisted_speaker_id or self._auto_assisted_id
        if assisted_id is None:
            self._auto_assisted_id = speaker_id
            assisted_id = speaker_id

        if speaker_id == assisted_id:
            return AttributionResult(
                participant_role="assisted",
                is_uncertain=False,
                should_interrupt_realtime=False,
            )

        return AttributionResult(
            participant_role="other",
            is_uncertain=False,
            should_interrupt_realtime=False,
        )
