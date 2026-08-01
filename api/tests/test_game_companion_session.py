import asyncio
import json
import wave
from io import BytesIO

import pytest

from api.services.game_companion.persona import ASTER_SYSTEM_PROMPT
from api.services.game_companion.protocol import (
    AudioEnd,
    AudioStart,
    Caption,
    ErrorMessage,
    MemoryQuery,
    MemoryResult,
    ProtocolError,
    State,
    ToolCall,
    ToolResult,
)
from api.services.game_companion.providers import (
    LLMResult,
    LLMToolCall,
    PCMChunk,
    ProviderSet,
)
from api.services.game_companion.session import CompanionSession, ProviderTimeouts


class FakeSTT:
    def __init__(self, text="Take me to the moon"):
        self.text = text
        self.calls = []

    async def transcribe(self, wav_audio: bytes) -> str:
        self.calls.append(wav_audio)
        return self.text


class FakeLLM:
    def __init__(self, results=None):
        self.results = list(
            results or [LLMResult(text="Setting a course for the moon.")]
        )
        self.calls = []

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        self.calls.append({"messages": messages, "tools": tools})
        return self.results.pop(0)


class FakeTTS:
    def __init__(self):
        self.calls = []

    async def synthesize(self, text: str):
        self.calls.append(text)
        yield PCMChunk(audio=b"\x01\x00\x02\x00", sample_rate=24000, channels=1)


class CloseTrackingProvider:
    def __init__(self):
        self.close_calls = 0

    async def transcribe(self, wav_audio: bytes) -> str:
        return "closed"

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        return LLMResult(text="closed")

    async def synthesize(self, text: str):
        yield PCMChunk(audio=b"\x00\x00", sample_rate=24000, channels=1)

    async def close(self):
        self.close_calls += 1


def fake_providers(*, stt=None, llm=None, tts=None):
    return ProviderSet(
        stt=stt or FakeSTT(),
        llm=llm or FakeLLM(),
        tts=tts or FakeTTS(),
    )


async def wait_for_event(session, event_type, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while True:
            for event in session.outbound_events:
                if isinstance(event, event_type):
                    return event
            await asyncio.sleep(0)


async def test_new_turn_cancels_previous_provider_work():
    session = CompanionSession(providers=fake_providers())

    await session.start_turn("old", {})
    await session.start_turn("new", {})

    assert session.active_turn_id == "new"
    assert "old" in session.cancelled_turn_ids
    await session.close()


async def test_close_releases_provider_resources_once():
    stt = CloseTrackingProvider()
    llm = CloseTrackingProvider()
    tts = CloseTrackingProvider()
    session = CompanionSession(
        providers=ProviderSet(stt=stt, llm=llm, tts=tts),
    )

    await session.close()
    await session.close()

    assert stt.close_calls == 1
    assert llm.close_calls == 1
    assert tts.close_calls == 1


class CancellationIgnoringSTT:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, wav_audio: bytes) -> str:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
        return "stale transcript"


async def test_stale_provider_output_is_discarded_after_new_turn():
    stt = CancellationIgnoringSTT()
    session = CompanionSession(providers=fake_providers(stt=stt))
    await session.start_turn("old", {})
    await session.append_audio("old", b"\x00\x00")
    await session.end_turn("old")
    await stt.started.wait()

    await session.start_turn("new", {})
    stt.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not any(
        isinstance(event, Caption) and event.turn_id == "old"
        for event in session.outbound_events
    )
    await session.close()


class HoldingSTT:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, wav_audio: bytes) -> str:
        self.started.set()
        await self.release.wait()
        return "buffer released"


async def test_raw_audio_buffer_is_released_before_provider_wait():
    stt = HoldingSTT()
    session = CompanionSession(providers=fake_providers(stt=stt))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00\x01\x00")
    await session.end_turn("turn-1")
    await stt.started.wait()

    try:
        assert session.buffered_audio_bytes == 0
    finally:
        stt.release.set()
        await session.wait_for_turn("turn-1")
        await session.close()


async def test_captions_precede_assistant_audio():
    stt = FakeSTT("Take me to the moon")
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(stt=stt, tts=tts))
    await session.start_turn("turn-1", {"current_body_id": "planet_01"})
    await session.append_audio("turn-1", b"\x00\x00\x01\x00")
    await session.end_turn("turn-1")
    await session.wait_for_turn("turn-1")

    player_caption = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, Caption) and event.speaker == "player"
    )
    assistant_caption = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, Caption) and event.speaker == "assistant"
    )
    audio_start = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, AudioStart)
    )
    audio_end = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, AudioEnd)
    )
    binary_audio = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, bytes)
    )

    assert player_caption < assistant_caption < audio_start < binary_audio < audio_end
    assert tts.calls == ["Setting a course for the moon."]
    with wave.open(BytesIO(stt.calls[0]), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.readframes(2) == b"\x00\x00\x01\x00"
    await session.close()


async def test_tool_call_pauses_final_narration_until_typed_result():
    llm = FakeLLM(
        results=[
            LLMResult(
                text="I will check that route.",
                tool_calls=(
                    LLMToolCall(
                        call_id="call-1",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                ),
            ),
            LLMResult(text="The starting moon is now your navigation target."),
        ]
    )
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")

    tool_call = await wait_for_event(session, ToolCall)
    assert tool_call.call_id == "call-1"
    assert not any(
        isinstance(event, Caption) and event.speaker == "assistant"
        for event in session.outbound_events
    )
    assert tts.calls == []

    await session.submit_tool_result(
        ToolResult(
            type="tool_result",
            turn_id="turn-1",
            call_id="call-1",
            ok=True,
            result={
                "body_id": "planet_01_moon",
                "navigation_state": "targeted",
            },
        )
    )
    await session.wait_for_turn("turn-1")

    assert tts.calls == ["The starting moon is now your navigation target."]
    assert len(llm.calls) == 2
    assert any(message["role"] == "tool" for message in llm.calls[1]["messages"])
    await session.close()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("recent_activity", {"limit": 10}),
        ("activity_for_body", {"body_id": "planet_01_moon", "limit": 8}),
        ("unresolved_incidents", {"limit": 5}),
        ("prior_failure", {"category": "landing", "limit": 3}),
        ("journal_item_sources", {"item_id": "episode-1"}),
    ],
)
async def test_registered_memory_query_pauses_interpretation_until_typed_result(
    name, arguments
):
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name=name,
                        arguments=arguments,
                    ),
                )
            ),
            LLMResult(text="That is what the expedition record says."),
        ]
    )
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")

    query = await wait_for_event(session, MemoryQuery)
    assert query.query_id == "query-1"
    assert query.name == name
    assert query.arguments == arguments
    assert not any(
        isinstance(event, Caption) and event.speaker == "assistant"
        for event in session.outbound_events
    )

    records = [
        {
            "schema_version": 1,
            "event_id": "event-1",
            "event_type": "navigation_target_changed",
            "body_id": "planet_01_moon",
            "summary": "Navigation target changed to the starting moon.",
        }
    ]
    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-1",
            query_id="query-1",
            ok=True,
            records=records,
        )
    )
    await session.wait_for_turn("turn-1")

    assert tts.calls == ["That is what the expedition record says."]
    assert len(llm.calls) == 2
    offered_names = {tool["function"]["name"] for tool in llm.calls[0]["tools"]}
    assert name in offered_names
    memory_message = llm.calls[1]["messages"][-1]
    assert memory_message["role"] == "tool"
    assert memory_message["tool_call_id"] == "query-1"
    assert json.loads(memory_message["content"]) == {
        "ok": True,
        "records": records,
        "error": None,
    }
    assert ": " not in memory_message["content"]
    assert ", " not in memory_message["content"]
    await session.close()


@pytest.mark.parametrize(
    ("name", "arguments", "expected_arguments"),
    [
        ("recent_activity", {}, {"limit": 10}),
        (
            "activity_for_body",
            {"body_id": "planet_01_moon"},
            {"body_id": "planet_01_moon", "limit": 10},
        ),
        ("unresolved_incidents", {}, {"limit": 10}),
        (
            "prior_failure",
            {"category": "landing"},
            {"category": "landing", "limit": 10},
        ),
        (
            "journal_item_sources",
            {"item_id": "episode-1"},
            {"item_id": "episode-1"},
        ),
    ],
)
async def test_memory_queries_materialize_default_limits(
    name, arguments, expected_arguments
):
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name=name,
                        arguments=arguments,
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1.0, llm=1.0, tts=1.0, tool=0.01),
    )
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")

    query = await wait_for_event(session, MemoryQuery)

    assert query.arguments == expected_arguments
    await session.close()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("recent_activity", {"limit": 0}),
        ("recent_activity", {"limit": 21}),
        ("recent_activity", {"limit": True}),
        ("recent_activity", {"limit": 1, "extra": "not allowed"}),
        ("activity_for_body", {"limit": 1}),
        ("activity_for_body", {"body_id": " planet_01", "limit": 1}),
        ("activity_for_body", {"body_id": "\U0001f30c" * 33, "limit": 1}),
        ("unresolved_incidents", {"body_id": "planet_01"}),
        ("prior_failure", {"category": "", "limit": 1}),
        ("journal_item_sources", {}),
        ("journal_item_sources", {"item_id": "episode-1", "limit": 1}),
    ],
)
async def test_invalid_memory_query_arguments_fail_before_socket_emission(
    name, arguments
):
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name=name,
                        arguments=arguments,
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1.0, llm=1.0, tts=1.0, tool=0.01),
    )
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await session.wait_for_turn("turn-1")

    assert not any(isinstance(event, MemoryQuery) for event in session.outbound_events)
    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_failure"
    await session.close()


async def test_memory_result_cannot_exceed_the_requested_record_limit():
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            ),
            LLMResult(text="One record was returned."),
        ]
    )
    session = CompanionSession(providers=fake_providers(llm=llm))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await wait_for_event(session, MemoryQuery)

    with pytest.raises(ProtocolError, match="memory_result_too_large"):
        await session.submit_memory_result(
            MemoryResult(
                type="memory_result",
                turn_id="turn-1",
                query_id="query-1",
                ok=True,
                records=[{"event_id": "event-1"}, {"event_id": "event-2"}],
            )
        )

    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-1",
            query_id="query-1",
            ok=True,
            records=[{"event_id": "event-1"}],
        )
    )
    await session.wait_for_turn("turn-1")
    await session.close()


async def test_failed_memory_result_is_interpreted_without_inventing_records():
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name="activity_for_body",
                        arguments={"body_id": "unknown_body", "limit": 2},
                    ),
                )
            ),
            LLMResult(text="I could not find that body in the expedition record."),
        ]
    )
    session = CompanionSession(providers=fake_providers(llm=llm))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await wait_for_event(session, MemoryQuery)

    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-1",
            query_id="query-1",
            ok=False,
            error="unknown body_id",
        )
    )
    await session.wait_for_turn("turn-1")

    memory_content = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert memory_content == {
        "ok": False,
        "records": [],
        "error": "unknown body_id",
    }
    await session.close()


async def test_memory_result_query_id_must_own_the_pending_request():
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            ),
            LLMResult(text="The record is empty."),
        ]
    )
    session = CompanionSession(providers=fake_providers(llm=llm))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await wait_for_event(session, MemoryQuery)

    with pytest.raises(ProtocolError, match="unexpected_memory_result"):
        await session.submit_memory_result(
            MemoryResult(
                type="memory_result",
                turn_id="turn-1",
                query_id="query-other",
                ok=True,
                records=[],
            )
        )

    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-1",
            query_id="query-1",
            ok=True,
            records=[],
        )
    )
    await session.wait_for_turn("turn-1")
    await session.close()


async def test_action_id_cannot_be_reused_in_a_later_llm_round():
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="action-1",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            ),
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="action-1",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                )
            ),
        ]
    )
    session = CompanionSession(providers=fake_providers(llm=llm))
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await wait_for_event(session, MemoryQuery)

    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-1",
            query_id="action-1",
            ok=True,
            records=[],
        )
    )
    await session.wait_for_turn("turn-1")

    assert (
        len(
            [
                event
                for event in session.outbound_events
                if isinstance(event, MemoryQuery)
            ]
        )
        == 1
    )
    assert not any(isinstance(event, ToolCall) for event in session.outbound_events)
    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_failure"
    await session.close()


async def test_tool_timeout_releases_all_pending_results():
    llm = FakeLLM(
        results=[
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="call-1",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01"},
                    ),
                    LLMToolCall(
                        call_id="call-2",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1.0, llm=1.0, tts=1.0, tool=0.01),
    )
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await session.wait_for_turn("turn-1")

    assert session.pending_tool_result_count == 0
    await session.close()


class BlockingSTT:
    async def transcribe(self, wav_audio: bytes) -> str:
        await asyncio.Event().wait()


async def test_provider_timeout_emits_recoverable_error():
    session = CompanionSession(
        providers=fake_providers(stt=BlockingSTT()),
        timeouts=ProviderTimeouts(stt=0.01, llm=1.0, tts=1.0, tool=1.0),
    )
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await session.wait_for_turn("turn-1")

    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_timeout"
    assert error.recoverable is True
    assert any(
        isinstance(event, State) and event.state == "degraded"
        for event in session.outbound_events
    )
    await session.close()


class FailingMidStreamTTS:
    async def synthesize(self, text: str):
        yield PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
        raise RuntimeError("provider stream failed")


async def test_tts_failure_closes_started_audio_before_recoverable_error():
    session = CompanionSession(
        providers=fake_providers(tts=FailingMidStreamTTS()),
    )
    await session.start_turn("turn-1", {})
    await session.append_audio("turn-1", b"\x00\x00")
    await session.end_turn("turn-1")
    await session.wait_for_turn("turn-1")

    event_types = [type(event) for event in session.outbound_events]
    assert event_types.index(AudioStart) < event_types.index(bytes)
    assert event_types.index(bytes) < event_types.index(AudioEnd)
    assert event_types.index(AudioEnd) < event_types.index(ErrorMessage)
    await session.close()


def test_aster_prompt_does_not_claim_unknown_game_facts():
    prompt = ASTER_SYSTEM_PROMPT.lower()

    assert "aster" in prompt
    assert "salvage" in prompt
    assert "unknown" in prompt
    assert "supplied game context" in prompt
    assert "you are currently" not in prompt
    assert "your ship is" not in prompt
