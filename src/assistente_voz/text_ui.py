from __future__ import annotations

from dataclasses import dataclass, field

from assistente_voz.orchestrator import TurnResult


@dataclass
class UISessionState:
    status: str = "idle"  # idle | listening | processing | error
    last_transcription: str = ""
    last_response: str = ""
    fallback_pending: bool = False
    pending_transcription: str = ""
    final_notes: list[str] = field(default_factory=list)


class TextUI:
    """Operational text UI state adapter for real-time updates and fallback."""

    def __init__(self) -> None:
        self.state = UISessionState()

    def on_listening_started(self) -> None:
        self.state.status = "listening"

    def on_processing(self) -> None:
        self.state.status = "processing"

    def on_turn_result(self, result: TurnResult) -> None:
        if result.ok:
            self.state.status = "listening"
            self.state.last_transcription = result.transcription or ""
            self.state.last_response = result.assistant_response or ""
            self.state.fallback_pending = False
            self.state.pending_transcription = ""
            return

        # Handle STT fallback path without ending the session.
        if result.error_code == "STT_LOW_CONFIDENCE":
            self.state.status = "listening"
            self.state.fallback_pending = True
            self.state.pending_transcription = result.transcription or ""
            return

        # Generic errors keep session alive in fail-soft mode.
        self.state.status = "error"

    def submit_manual_fallback(self, text: str, confirmed: bool) -> str:
        """Accept manual input only when short confirmation is explicit."""
        if not self.state.fallback_pending:
            raise ValueError("No fallback is pending")
        if not confirmed:
            raise ValueError("Manual fallback requires short confirmation")

        self.state.fallback_pending = False
        self.state.pending_transcription = ""
        self.state.last_transcription = text
        self.state.status = "listening"
        return text

    def apply_final_adjustments(self, notes: list[str]) -> None:
        """Store final session adjustments before artifact emission."""
        self.state.final_notes.extend(notes)

    def render_snapshot(self) -> dict[str, str | bool]:
        """Return a serializable UI snapshot for adapters/terminals."""
        return {
            "status": self.state.status,
            "transcription": self.state.last_transcription,
            "response": self.state.last_response,
            "fallback_pending": self.state.fallback_pending,
        }
