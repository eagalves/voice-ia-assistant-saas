"""Issue 5 – Estado de Conversa com Role por Participante."""
import pytest

from assistente_voz.conversation_state import ConversationState, Message


# ── window limit ──────────────────────────────────────────────────────────────

def test_messages_above_limit_drop_oldest():
    state = ConversationState(max_messages=3)
    state.add("user", "msg1")
    state.add("assistant", "msg2")
    state.add("user", "msg3")
    state.add("assistant", "msg4")  # should evict msg1

    contents = [m.content for m in state.messages()]
    assert "msg1" not in contents
    assert contents == ["msg2", "msg3", "msg4"]
    assert len(state) == 3


# ── role and order preservation ───────────────────────────────────────────────

def test_messages_preserve_role_and_insertion_order():
    state = ConversationState()
    state.add("user", "hello", participant="patient")
    state.add("assistant", "hi there")
    state.add("uncertain", "something unclear", participant="unknown")

    msgs = state.messages()
    assert msgs[0] == Message(role="user", content="hello", participant="patient")
    assert msgs[1] == Message(role="assistant", content="hi there")
    assert msgs[2] == Message(role="uncertain", content="something unclear", participant="unknown")


# ── LLM serialisation ─────────────────────────────────────────────────────────

def test_to_llm_messages_preserves_roles():
    state = ConversationState()
    state.set_system("You are a helpful assistant.")
    state.add("user", "What is the dose?")
    state.add("assistant", "Take 500mg.")

    llm = state.to_llm_messages()
    assert llm[0] == {"role": "system", "content": "You are a helpful assistant."}
    assert llm[1] == {"role": "user", "content": "What is the dose?"}
    assert llm[2] == {"role": "assistant", "content": "Take 500mg."}


def test_to_llm_messages_maps_uncertain_to_user():
    state = ConversationState()
    state.add("uncertain", "mumbled something")

    llm = state.to_llm_messages()
    assert llm[0]["role"] == "user"


def test_system_message_excluded_from_window_limit():
    """System message must not count against max_messages."""
    state = ConversationState(max_messages=2)
    state.set_system("System prompt.")
    state.add("user", "a")
    state.add("assistant", "b")
    state.add("user", "c")  # evicts "a"

    assert len(state) == 2
    llm = state.to_llm_messages()
    assert llm[0]["role"] == "system"
    contents = [m["content"] for m in llm[1:]]
    assert "a" not in contents


# ── clear ─────────────────────────────────────────────────────────────────────

def test_clear_removes_non_system_messages():
    state = ConversationState()
    state.set_system("sys")
    state.add("user", "hello")
    state.clear()

    assert len(state) == 0
    llm = state.to_llm_messages()
    assert llm == [{"role": "system", "content": "sys"}]
