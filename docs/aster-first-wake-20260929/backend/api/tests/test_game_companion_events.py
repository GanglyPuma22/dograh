import asyncio
import pytest
from test_game_companion_session import FakeSTT, FakeLLM, FakeTTS, fake_providers
from api.services.game_companion.protocol import (
    parse_client_message, ClientMessageOrder, ProtocolError, Caption, ToolCall, ErrorMessage
)
from api.services.game_companion.session import CompanionSession
from api.services.game_companion.providers import LLMResult, LLMToolCall

CONTEXT = {"discovery": {"station_sample_state": "receiver_ready", "receiver_owned": True}}

def event(**overrides):
    data = dict(type="game_event", turn_id="bench-1", event="station_assay_complete", context=CONTEXT)
    data.update(overrides)
    return parse_client_message(data)

@pytest.mark.parametrize("overrides", [
    {"event": "invented_signal"}, {"context": {}},
    {"context": {"discovery": {"station_sample_state": "sample_collected", "receiver_owned": True}}},
    {"context": {"discovery": {"station_sample_state": "receiver_ready", "receiver_owned": False}}},
])
def test_only_earned_station_event_is_valid(overrides):
    with pytest.raises(ProtocolError): event(**overrides)

def test_event_has_no_audio_input_and_voice_can_interrupt():
    order = ClientMessageOrder()
    order.accept(parse_client_message(dict(type="hello",protocol_version=1,client="salvage",save_id="probe",capabilities=["captions"])))
    order.accept(event())
    with pytest.raises(ProtocolError): order.accept_binary(2)
    with pytest.raises(ProtocolError): order.accept(parse_client_message(dict(type="turn_end",turn_id="bench-1")))
    assert order.accept(parse_client_message(dict(type="turn_start",turn_id="voice-2",context={}))) == "bench-1"
    order.accept_binary(2)

@pytest.mark.asyncio
async def test_event_uses_llm_and_tts_without_stt_player_caption_or_actions():
    stt, llm, tts = FakeSTT(), FakeLLM([LLMResult(text="The assay is complete. Shall we look at it together?")]), FakeTTS()
    session = CompanionSession(providers=fake_providers(stt=stt,llm=llm,tts=tts))
    await session.start_game_event("bench-1", CONTEXT, "station_assay_complete")
    await session.wait_for_turn("bench-1")
    assert stt.calls == []
    assert len(llm.calls) == 1 and llm.calls[0]["tools"] == []
    assert "No player has spoken" in str(llm.calls[0]["messages"])
    assert len(tts.calls) == 1
    assert any(isinstance(e, bytes) for e in session.outbound_events)
    assert any(isinstance(e, Caption) and e.speaker == "assistant" for e in session.outbound_events)
    assert not any(isinstance(e, Caption) and e.speaker == "player" for e in session.outbound_events)
    assert not any(isinstance(e, ToolCall) for e in session.outbound_events)
    await session.close()

@pytest.mark.asyncio
async def test_interrupt_retires_pending_event_output():
    started, release = asyncio.Event(), asyncio.Event()
    class DelayedLLM(FakeLLM):
        async def respond(self, messages, tools):
            started.set()
            await release.wait()
            return LLMResult(text="Late station line")
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=DelayedLLM(),tts=tts))
    await session.start_game_event("bench-1", CONTEXT, "station_assay_complete")
    await asyncio.wait_for(started.wait(), 1)
    await session.start_turn("player-2", {})
    release.set()
    with pytest.raises(asyncio.CancelledError): await session.wait_for_turn("bench-1")
    assert not tts.calls
    await session.close()
