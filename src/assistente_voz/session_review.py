from __future__ import annotations

from dataclasses import dataclass, field


CRITICAL_PROFILES = {"medical"}


@dataclass(frozen=True)
class UncertainSegment:
    segment_id: str
    original_role: str = "uncertain"
    content: str = ""


@dataclass(frozen=True)
class ReviewDecision:
    segment_id: str
    resolved_role: str
    reviewer: str
    note: str = ""


@dataclass(frozen=True)
class FinalizationCheck:
    can_finalize: bool
    reason: str | None


@dataclass
class SessionReview:
    """Manage final review of uncertain role segments before session close."""

    profile_id: str
    uncertain_segments: list[UncertainSegment] = field(default_factory=list)
    decisions: list[ReviewDecision] = field(default_factory=list)

    def add_uncertain_segment(self, segment_id: str, content: str = "") -> None:
        self.uncertain_segments.append(
            UncertainSegment(segment_id=segment_id, content=content)
        )

    def review_segment(
        self,
        segment_id: str,
        resolved_role: str,
        reviewer: str,
        note: str = "",
    ) -> None:
        self.decisions.append(
            ReviewDecision(
                segment_id=segment_id,
                resolved_role=resolved_role,
                reviewer=reviewer,
                note=note,
            )
        )

    def finalization_check(self) -> FinalizationCheck:
        """Check whether the session can be finalized under profile criticality."""
        if self.profile_id not in CRITICAL_PROFILES:
            return FinalizationCheck(can_finalize=True, reason=None)

        if not self.uncertain_segments:
            return FinalizationCheck(can_finalize=True, reason=None)

        pending_ids = {
            seg.segment_id
            for seg in self.uncertain_segments
            if seg.segment_id not in {d.segment_id for d in self.decisions}
        }
        if pending_ids:
            return FinalizationCheck(
                can_finalize=False,
                reason="CRITICAL_PROFILE_REVIEW_REQUIRED",
            )

        return FinalizationCheck(can_finalize=True, reason=None)

    def audit_trail(self) -> list[ReviewDecision]:
        """Return immutable audit history entries for role review decisions."""
        return list(self.decisions)
