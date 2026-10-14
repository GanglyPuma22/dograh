"""Wake contract and interruption regressions, using only fake providers."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from test_game_companion_session import FakeLLM, FakeSTT, FakeTTS, fake_providers
from api.services.game_companion.persona import build_turn_messages
from api.services.game_companion.protocol import (
    AnnotationProposal, AudioEnd, AudioStart, Caption, ClientMessageOrder,
    ErrorMessage, MemoryQuery, ProtocolError, State, ToolCall, parse_client_message,
)
from api.services.game_companion.providers import LLMResult, LLMToolCall, PCMChunk
from api.services.game_companion.session import CompanionSession

WAKE_CONTEXT = {"companion_wake": {
    "activated": True, "phase": "awake", "first_activation_this_session": True,
}}
INTRO = "Oh—hello. I'm Aster. I can help you make sense of this place. Hold the talk key when you want to speak."
INVALID = [
    {}, {"companion_wake": "awake"}, {"companion_wake": []},
    *({"companion_wake": {**WAKE_CONTEXT["companion_wake"], key: value}}
      for key, value in [
          ("activated", False), ("activated", 1), ("activated", "true"),
          ("phase", "waking"), ("phase", None),
          ("first_activation_this_session", False),
          ("first_activation_this_session", 1),
          ("first_activation_this_session", "true"),
      ]),
    *({"companion_wake": {key: value for key, value in WAKE_CONTEXT["companion_wake"].items() if key != missing}}
      for missing in WAKE_CONTEXT["companion_wake"]),
]


def wake_message(context=WAKE_CONTEXT, event="aster_first_wake"):
    return parse_client_message({
        "type": "game_event", "turn_id": "wake", "event": event, "context": context,
    })


@pytest.mark.parametrize("context", INVALID)
def test_wake_requires_actual_booleans_and_fresh_completed_activation(context):
    with pytest.raises(ProtocolError):
        wake_message(context)


def test_wake_needs_no_assay_and_has_closed_protocol_input():
    order = ClientMessageOrder()
    order.accept(parse_client_message({
        "type": "hello", "protocol_version": 1, "client": "salvage",
        "save_id": "fresh-review", "capabilities": ["captions"],
    }))
    order.accept(wake_message())
    assert not order.input_open
    with pytest.raises(ProtocolError):
        order.accept_binary(2)
    with pytest.raises(ProtocolError):
        order.accept(parse_client_message({"type": "turn_end", "turn_id": "wake"}))
    assert order.accept(parse_client_message({
        "type": "turn_start", "turn_id": "reply", "context": {},
    })) == "wake"
    order.accept_binary(2)


@pytest.mark.asyncio
async def test_wake_uses_normal_caption_and_pcm_path_without_stt_or_tools():
    stt, llm, tts = FakeSTT(), FakeLLM([LLMResult(text=INTRO)]), FakeTTS()
    session = CompanionSession(providers=fake_providers(stt=stt, llm=llm, tts=tts))
    try:
        await session.start_game_event("wake", WAKE_CONTEXT, "aster_first_wake")
        with pytest.raises(ProtocolError):
            await session.append_audio("wake", b"\x00\x00")
        with pytest.raises(ProtocolError):
            await session.end_turn("wake")
        await session.wait_for_turn("wake")
        assert stt.calls == []
        assert len(llm.calls) == 1 and llm.calls[0]["tools"] == []
        prompt = llm.calls[0]["messages"][-1]["content"]
        assert "Game event: aster_first_wake. No player has spoken." in prompt
        assert INTRO in prompt and "Player speech:" not in prompt
        assert "Do not invent prior conversation" in prompt
        assert tts.calls == [INTRO]
        captions = [e for e in session.outbound_events if isinstance(e, Caption)]
        assert [(e.speaker, e.text) for e in captions] == [("assistant", INTRO)]
        assert any(isinstance(e, AudioStart) for e in session.outbound_events)
        assert b"\x01\x00\x02\x00" in session.outbound_events
        assert any(isinstance(e, AudioEnd) for e in session.outbound_events)
        assert not any(isinstance(e, (ToolCall, MemoryQuery, AnnotationProposal, ErrorMessage)) for e in session.outbound_events)
        assert not any(isinstance(e, State) and e.state == "listening" for e in session.outbound_events)
        # First ordinary reply still runs STT, emits a player caption and offers tools.
        llm.results.append(LLMResult(text="Hello to you too."))
        await session.start_turn("reply", {})
        await session.append_audio("reply", b"\x00\x00")
        await session.end_turn("reply")
        await session.wait_for_turn("reply")
        assert len(stt.calls) == 1 and llm.calls[-1]["tools"]
        assert any(isinstance(e, Caption) and e.turn_id == "reply" and e.speaker == "player" for e in session.outbound_events)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("event,context", [
    *(("aster_first_wake", context) for context in INVALID),
    ("unknown", WAKE_CONTEXT), ("station_assay_complete", WAKE_CONTEXT),
])
async def test_invalid_event_cannot_interrupt_active_speech(event, context):
    started, release = asyncio.Event(), asyncio.Event()

    class SpeakingTTS(FakeTTS):
        async def synthesize(self, text):
            yield PCMChunk(audio=b"\x01\x00", sample_rate=24000)
            started.set()
            await release.wait()
            yield PCMChunk(audio=b"\x02\x00", sample_rate=24000)

    session = CompanionSession(providers=fake_providers(tts=SpeakingTTS()))
    try:
        await session.start_turn("speaking", {})
        await session.append_audio("speaking", b"\x00\x00")
        await session.end_turn("speaking")
        await asyncio.wait_for(started.wait(), 1)
        before = list(session.outbound_events)
        with pytest.raises(ValidationError):
            await session.start_game_event("invalid", context, event)
        assert session.active_turn_id == "speaking"
        assert session.outbound_events == before and not session.cancelled_turn_ids
        release.set()
        await session.wait_for_turn("speaking")
        assert b"\x02\x00" in session.outbound_events
    finally:
        release.set()
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["llm", "tts"])
async def test_late_uncancellable_wake_output_cannot_leak_into_player_reply(provider):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    class LateLLM(FakeLLM):
        async def respond(self, messages, tools):
            if "Game event:" in messages[-1]["content"]:
                await hold()
                return LLMResult(text="STALE WAKE CAPTION")
            return LLMResult(text="Current reply")

    class LateTTS(FakeTTS):
        async def synthesize(self, text):
            if text == INTRO:
                await hold()
                yield PCMChunk(audio=b"\x55\x00", sample_rate=24000)
            else:
                yield PCMChunk(audio=b"\x33\x00", sample_rate=24000)

    llm = LateLLM() if provider == "llm" else FakeLLM([LLMResult(text=INTRO), LLMResult(text="Current reply")])
    tts = LateTTS() if provider == "tts" else FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    try:
        await session.start_game_event("wake", WAKE_CONTEXT, "aster_first_wake")
        await asyncio.wait_for(started.wait(), 1)
        await session.start_turn("reply", {})
        await asyncio.wait_for(cancelled.wait(), 1)
        cutoff = len(session.outbound_events)
        await session.append_audio("reply", b"\x00\x00")
        await session.end_turn("reply")
        release.set()
        await session.wait_for_turn("wake")
        await session.wait_for_turn("reply")
        events = session.outbound_events[cutoff:]
        assert not any(getattr(e, "turn_id", None) == "wake" for e in events)
        assert b"\x55\x00" not in events
        assert any(isinstance(e, Caption) and e.text == "Current reply" for e in events)
        assert session.active_turn_id == "reply"
        assert "wake" in session.cancelled_turn_ids
    finally:
        release.set()
        await session.close()


@pytest.mark.asyncio
async def test_wake_rejects_provider_tool_calls_without_dispatching_actions():
    llm = FakeLLM([LLMResult(tool_calls=(LLMToolCall(
        call_id="forbidden", name="request_assisted_landing", arguments={},
    ),))])
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    try:
        await session.start_game_event("wake", WAKE_CONTEXT, "aster_first_wake")
        await session.wait_for_turn("wake")
        assert not tts.calls
        assert not any(isinstance(e, (ToolCall, MemoryQuery, AnnotationProposal, Caption)) for e in session.outbound_events)
        assert any(isinstance(e, ErrorMessage) and e.code == "provider_failure" for e in session.outbound_events)
    finally:
        await session.close()


@pytest.mark.parametrize("key,event", [
    ("recent_aster_opening", "aster_first_wake"),
    ("recent_station_opening", "station_assay_complete"),
])
def test_returned_opening_caption_is_reported_dialogue_not_world_evidence(key, event):
    opening = {"event": event, "caption_received": "Actual returned caption",
               "delivery": "Audio may have been interrupted or muted."}
    messages = build_turn_messages("Hello", {"companion_wake": WAKE_CONTEXT["companion_wake"], key: opening})
    authoritative, dialogue = messages[-1]["content"].split("\n\nReported dialogue", 1)
    assert key not in authoritative and "companion_wake" in authoritative
    assert json.dumps(opening["caption_received"]) in dialogue and event in dialogue
    assert "not authoritative world evidence" in messages[0]["content"]
    assert "proof of what the player heard" in messages[0]["content"]
