from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant", "uncertain"]

DEFAULT_MAX_MESSAGES = 50


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    participant: str | None = None  # e.g. "doctor", "patient", "candidate"


@dataclass
class ConversationState:
    """Rolling conversation history with configurable context window.

    Rules:
    - Keeps at most ``max_messages`` messages (oldest are dropped).
    - The system message is always kept and not counted in the window.
    - Serializes to the OpenAI message list format via ``to_llm_messages()``.
    """

    max_messages: int = DEFAULT_MAX_MESSAGES
    _system: Message | None = field(default=None, init=False, repr=False)
    _messages: deque[Message] = field(default_factory=deque, init=False, repr=False)

    # ── public API ────────────────────────────────────────────────────────────

    def set_system(self, content: str) -> None:
        """Set (or replace) the system prompt."""
        self._system = Message(role="system", content=content)

    def add(self, role: str, content: str, participant: str | None = None) -> None:
        """Append a message, evicting the oldest when the window is full."""
        if role == "system":
            self.set_system(content)
            return
        msg = Message(role=role, content=content, participant=participant)
        if len(self._messages) >= self.max_messages:
            self._messages.popleft()
        self._messages.append(msg)

    def messages(self) -> list[Message]:
        """Return all non-system messages in insertion order."""
        return list(self._messages)

    def to_llm_messages(self) -> list[dict]:
        """Serialize to OpenAI-compatible message list.

        Uncertain roles are sent as ``user`` to avoid API rejection,
        but the original role is preserved in ``Message.role``.
        """
        result: list[dict] = []
        if self._system:
            result.append({"role": "system", "content": self._system.content})
        for msg in self._messages:
            llm_role = "user" if msg.role == "uncertain" else msg.role
            result.append({"role": llm_role, "content": msg.content})
        return result

    def clear(self) -> None:
        """Remove all non-system messages."""
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)
