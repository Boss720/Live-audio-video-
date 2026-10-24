"""Chronological ordering and interruption semantics for ``core.turn_state``."""

from __future__ import annotations

from google.genai import types

from core.events import AgentEvent, SessionError, TranscriptSource, TurnComplete
from core.turn_state import TurnState


def _sc(
    *,
    input_text: str | None = None,
    output_text: str | None = None,
    turn_complete: bool = False,
    interrupted: bool = False,
) -> types.LiveServerContent:
    return types.LiveServerContent(
        input_transcription=types.Transcription(text=input_text)
        if input_text is not None
        else None,
        output_transcription=types.Transcription(text=output_text)
        if output_text is not None
        else None,
        turn_complete=turn_complete,
        interrupted=interrupted,
    )


def _completes(events: list[AgentEvent]) -> list[TurnComplete]:
    return [e for e in events if isinstance(e, TurnComplete)]


def test_user_turn_complete_precedes_model_turn_complete() -> None:
    ts = TurnState(coalesce_ms=0.0)
    ts.consume_server_content(_sc(input_text="question"))
    ts.consume_server_content(_sc(output_text="answer"))
    done = _completes(ts.consume_server_content(_sc(turn_complete=True)))
    assert [(c.source, c.text) for c in done] == [
        (TranscriptSource.USER, "question"),
        (TranscriptSource.MODEL, "answer"),
    ]
    assert not any(c.interrupted for c in done)


def test_interrupt_persists_heard_model_partial_and_advances_turn_id() -> None:
    ts = TurnState(coalesce_ms=0.0)
    ts.consume_server_content(_sc(input_text="Q1"))
    ts.consume_server_content(_sc(output_text="half an ans"))
    events = ts.consume_server_content(_sc(interrupted=True))

    done = _completes(events)
    assert [(c.source, c.text, c.interrupted) for c in done] == [
        (TranscriptSource.USER, "Q1", False),
        (TranscriptSource.MODEL, "half an ans", True),
    ]
    assert any(isinstance(e, SessionError) and e.message == "interrupted" for e in events)

    ts.consume_server_content(_sc(input_text="Q2"))
    ts.consume_server_content(_sc(output_text="A2"))
    done2 = _completes(ts.consume_server_content(_sc(turn_complete=True)))
    assert [(c.source, c.text, c.turn_id) for c in done2] == [
        (TranscriptSource.USER, "Q2", 1),
        (TranscriptSource.MODEL, "A2", 1),
    ]


def test_user_text_does_not_accumulate_across_interruptions() -> None:
    ts = TurnState(coalesce_ms=0.0)
    ts.consume_server_content(_sc(input_text="one"))
    first = _completes(ts.consume_server_content(_sc(interrupted=True)))
    ts.consume_server_content(_sc(input_text="two"))
    second = _completes(ts.consume_server_content(_sc(interrupted=True)))
    assert [c.text for c in first] == ["one"]
    assert [c.text for c in second] == ["two"]


def test_late_input_after_turn_complete_belongs_to_next_user_turn() -> None:
    ts = TurnState(coalesce_ms=0.0)
    ts.consume_server_content(_sc(input_text="Q1"))
    ts.consume_server_content(_sc(output_text="A1", turn_complete=True))
    ts.consume_server_content(_sc(input_text="late tail"))
    done = _completes(ts.consume_server_content(_sc(output_text="A2", turn_complete=True)))
    users = [c for c in done if c.source == TranscriptSource.USER]
    assert [(c.text, c.turn_id) for c in users] == [("late tail", 1)]


def test_interrupt_without_model_text_emits_no_model_turn() -> None:
    ts = TurnState(coalesce_ms=0.0)
    done = _completes(ts.consume_server_content(_sc(interrupted=True)))
    assert done == []
