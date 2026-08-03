import asyncio
import json
import wave
from io import BytesIO

import pytest

from api.services.game_companion.persona import (
    ASTER_ANALYSIS_SYSTEM_PROMPT,
    ASTER_SYSTEM_PROMPT,
)
from api.services.game_companion.protocol import (
    MAX_TEXT_LENGTH,
    AnnotationProposal,
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


async def wait_for_turn_event(session, event_type, turn_id, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while True:
            for event in session.outbound_events:
                if isinstance(event, event_type) and event.turn_id == turn_id:
                    return event
            await asyncio.sleep(0)


def canonical_event(event_id="event-1", **overrides):
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "recovery_completed",
        "occurred_at_utc": "2026-08-01T15:00:00Z",
        "game_time": 42.0,
        "body_id": "planet_01_moon",
        "importance": "normal",
        "status": "completed",
        "summary": "Recovery completed on planet_01_moon.",
    }
    event.update(overrides)
    return event


def analysis_json(
    *, source_event_ids=None, summary="Recovery followed the impact.", tags=None
):
    return json.dumps(
        {
            "source_event_ids": source_event_ids or ["event-1"],
            "summary": summary,
            "tags": tags or [],
        },
        separators=(",", ":"),
    )


def memory_turn_results(
    *, suffix="1", narration="I noticed a recovery pattern.", analysis=None
):
    results = [
        LLMResult(
            tool_calls=(
                LLMToolCall(
                    call_id=f"query-{suffix}",
                    name="recent_activity",
                    arguments={"limit": 2},
                ),
            )
        ),
        LLMResult(text=narration),
    ]
    if analysis is not None:
        results.append(analysis)
    return results


async def submit_memory_turn(
    session,
    *,
    turn_id="turn-1",
    records=None,
    ok=True,
    error=None,
):
    await session.start_turn(turn_id, {"private_context": "SECRET_CONTEXT"})
    await session.append_audio(turn_id, b"\x00\x00")
    await session.end_turn(turn_id)
    query = await wait_for_turn_event(session, MemoryQuery, turn_id)
    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id=turn_id,
            query_id=query.query_id,
            ok=ok,
            records=records or [],
            error=error,
        )
    )


async def test_new_turn_cancels_previous_provider_work():
    session = CompanionSession(providers=fake_providers())

    await session.start_turn("old", {})
    await session.start_turn("new", {})

    assert session.active_turn_id == "new"
    assert "old" in session.cancelled_turn_ids
    await session.close()


async def test_empty_turn_is_rejected_before_stt():
    stt = FakeSTT()
    session = CompanionSession(providers=fake_providers(stt=stt))
    await session.start_turn("turn-empty", {})

    with pytest.raises(ProtocolError, match="invalid_audio_frame") as raised:
        await session.end_turn("turn-empty")

    assert raised.value.recoverable is True
    assert stt.calls == []
    await session.close()


async def test_empty_transcript_degrades_without_calling_llm_or_tts():
    llm = FakeLLM()
    tts = FakeTTS()
    session = CompanionSession(
        providers=fake_providers(stt=FakeSTT(text=""), llm=llm, tts=tts)
    )
    await session.start_turn("turn-silent", {})
    await session.append_audio("turn-silent", b"\x00\x00")
    await session.end_turn("turn-silent")
    await session.wait_for_turn("turn-silent")

    assert llm.calls == []
    assert tts.calls == []
    assert not any(
        isinstance(event, Caption) and event.speaker == "player"
        for event in session.outbound_events
    )
    assert any(
        isinstance(event, ErrorMessage) and event.code == "provider_failure"
        for event in session.outbound_events
    )
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


async def test_session_emits_separate_content_free_stage_metrics():
    class StepClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 0.01
            return self.value

    metrics = []
    session = CompanionSession(
        providers=fake_providers(),
        monotonic=StepClock(),
        metric=lambda event, fields: metrics.append((event, fields)),
    )

    await session.start_turn("turn-metrics", {})
    await session.append_audio("turn-metrics", b"\x00\x00")
    await session.end_turn("turn-metrics")
    await session.wait_for_turn("turn-metrics")

    events = [event for event, _fields in metrics]
    assert events == [
        "stt_complete",
        "llm_round_complete",
        "llm_complete",
        "first_audio",
        "tts_complete",
    ]
    first_audio = dict(metrics)["first_audio"]
    assert first_audio["tts_elapsed_ms"] > 0
    assert first_audio["turn_elapsed_ms"] > first_audio["tts_elapsed_ms"]
    tts = dict(metrics)["tts_complete"]
    assert tts["pcm_bytes"] == 4
    assert tts["elapsed_ms"] >= first_audio["tts_elapsed_ms"]
    serialized = json.dumps(metrics)
    assert "Take me to the moon" not in serialized
    assert "Setting a course" not in serialized
    assert "transcript" not in serialized
    assert "response_text" not in serialized
    await session.close()


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
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)

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
    await asyncio.wait_for(stt.started.wait(), timeout=1.0)

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


async def test_long_transcript_and_narration_are_bounded_only_in_captions():
    transcript = "p" * (MAX_TEXT_LENGTH + 100)
    narration = "a" * (MAX_TEXT_LENGTH + 100)
    tts = FakeTTS()
    session = CompanionSession(
        providers=fake_providers(
            stt=FakeSTT(transcript),
            llm=FakeLLM([LLMResult(text=narration)]),
            tts=tts,
        )
    )
    await session.start_turn("turn-long", {})
    await session.append_audio("turn-long", b"\x00\x00")
    await session.end_turn("turn-long")
    await session.wait_for_turn("turn-long")

    captions = [
        event for event in session.outbound_events if isinstance(event, Caption)
    ]
    assert [len(caption.text) for caption in captions] == [
        MAX_TEXT_LENGTH,
        MAX_TEXT_LENGTH,
    ]
    assert tts.calls == [narration]
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    await session.close()


async def test_session_normalizes_markdown_emphasis_only_for_tts():
    response_text = "Report: (**engines stable**), *Captain*; proceed."
    tts = FakeTTS()
    metrics = []
    session = CompanionSession(
        providers=fake_providers(
            llm=FakeLLM(results=[LLMResult(text=response_text)]),
            tts=tts,
        ),
        metric=lambda event, fields: metrics.append((event, fields)),
    )
    await session.start_turn("turn-markup", {})
    await session.append_audio("turn-markup", b"\x00\x00")
    await session.end_turn("turn-markup")
    await session.wait_for_turn("turn-markup")

    assistant_caption = next(
        event
        for event in session.outbound_events
        if isinstance(event, Caption) and event.speaker == "assistant"
    )
    assert assistant_caption.text == response_text
    assert tts.calls == ["Report: (engines stable), Captain; proceed."]

    event_types = [type(event) for event in session.outbound_events]
    assert event_types.index(AudioStart) < event_types.index(bytes)
    assert event_types.index(bytes) < event_types.index(AudioEnd)
    metric_names = [event for event, _fields in metrics]
    assert "first_audio" in metric_names
    assert "tts_complete" in metric_names
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


async def test_late_tool_result_after_timeout_is_discarded():
    llm = FakeLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="call-late",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1, llm=1, tts=1, tool=0.01),
    )
    await session.start_turn("turn-late", {})
    await session.append_audio("turn-late", b"\x00\x00")
    await session.end_turn("turn-late")
    await wait_for_event(session, ToolCall)
    await session.wait_for_turn("turn-late")

    await session.submit_tool_result(
        ToolResult(
            type="tool_result",
            turn_id="turn-late",
            call_id="call-late",
            ok=False,
            error="client completed after timeout",
        )
    )
    await session.close()


async def test_late_memory_result_after_timeout_is_discarded():
    llm = FakeLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-late",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1, llm=1, tts=1, tool=0.01),
    )
    await session.start_turn("turn-late", {})
    await session.append_audio("turn-late", b"\x00\x00")
    await session.end_turn("turn-late")
    await wait_for_event(session, MemoryQuery)
    await session.wait_for_turn("turn-late")

    await session.submit_memory_result(
        MemoryResult(
            type="memory_result",
            turn_id="turn-late",
            query_id="query-late",
            ok=True,
            records=[],
        )
    )
    await session.close()


async def test_all_late_sibling_results_after_timeout_are_idempotently_discarded():
    llm = FakeLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="call-late",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                    LLMToolCall(
                        call_id="query-late",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1, llm=1, tts=1, tool=0.01),
    )
    await session.start_turn("turn-late", {})
    await session.append_audio("turn-late", b"\x00\x00")
    await session.end_turn("turn-late")
    await wait_for_event(session, ToolCall)
    await wait_for_event(session, MemoryQuery)
    await session.wait_for_turn("turn-late")

    late_tool = ToolResult(
        type="tool_result",
        turn_id="turn-late",
        call_id="call-late",
        ok=False,
        error="client completed after timeout",
    )
    late_memory = MemoryResult(
        type="memory_result",
        turn_id="turn-late",
        query_id="query-late",
        ok=True,
        records=[],
    )
    await session.submit_tool_result(late_tool)
    await session.submit_tool_result(late_tool)
    await session.submit_memory_result(late_memory)
    await session.submit_memory_result(late_memory)
    await session.close()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("recent_activity", {"limit": 10}),
        ("recent_journal_items", {"limit": 10}),
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
        ("recent_journal_items", {}, {"limit": 10}),
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
        ("recent_journal_items", {"limit": 0}),
        ("recent_journal_items", {"limit": 21}),
        ("recent_journal_items", {"kind": "fact"}),
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


async def test_post_speech_analysis_is_private_grounded_and_typed():
    secret_transcript = "SECRET_TRANSCRIPT_DO_NOT_ANALYZE"
    secret_narration = "SECRET_NARRATION_DO_NOT_ANALYZE"
    llm = FakeLLM(
        results=memory_turn_results(
            narration=secret_narration,
            analysis=LLMResult(
                text=analysis_json(
                    summary="  Recovery   followed the impact.  ",
                    tags=[" Recovery ", "landing", "RECOVERY"],
                )
            ),
        )
    )
    tts = FakeTTS()
    session = CompanionSession(
        providers=fake_providers(
            stt=FakeSTT(secret_transcript),
            llm=llm,
            tts=tts,
        ),
        proposal_id_factory=lambda: "proposal-fixed",
    )
    record = canonical_event(
        private_payload="SECRET_MEMORY_FIELD",
        data={"transcript": "SECRET_NESTED_MEMORY"},
    )
    invalid_record = {"event_id": "invalid-event", "summary": "SECRET_INVALID"}
    await submit_memory_turn(session, records=[record, invalid_record])
    await session.wait_for_turn("turn-1")

    proposal = next(
        event
        for event in session.outbound_events
        if isinstance(event, AnnotationProposal)
    )
    assert proposal.proposal_id == "proposal-fixed"
    assert proposal.source_event_ids == ["event-1"]
    assert proposal.summary == "Recovery followed the impact."
    assert proposal.tags == ["landing", "recovery"]
    audio_end_index = next(
        index
        for index, event in enumerate(session.outbound_events)
        if isinstance(event, AudioEnd)
    )
    assert audio_end_index < session.outbound_events.index(proposal)
    assert tts.calls == [secret_narration]

    assert len(llm.calls) == 3
    normal_tool_names = {
        tool["function"]["name"] for call in llm.calls[:2] for tool in call["tools"]
    }
    assert "propose_companion_analysis" not in normal_tool_names
    analysis_call = llm.calls[2]
    assert analysis_call["tools"] == []
    analysis_messages = json.dumps(analysis_call["messages"], ensure_ascii=False)
    assert secret_transcript not in analysis_messages
    assert "SECRET_CONTEXT" not in analysis_messages
    assert secret_narration not in analysis_messages
    assert "SECRET_MEMORY_FIELD" not in analysis_messages
    assert "SECRET_NESTED_MEMORY" not in analysis_messages
    assert "SECRET_INVALID" not in analysis_messages
    assert "event-1" in analysis_messages
    assert "recovery_completed" in analysis_messages
    await session.close()


async def test_no_analysis_sentinel_is_silent_and_does_not_fail_spoken_turn():
    llm = FakeLLM(
        results=memory_turn_results(analysis=LLMResult(text="  NO_ANALYSIS  "))
    )
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    await submit_memory_turn(session, records=[canonical_event()])
    await session.wait_for_turn("turn-1")

    assert tts.calls == ["I noticed a recovery pattern."]
    assert not any(
        isinstance(event, AnnotationProposal) for event in session.outbound_events
    )
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    assert llm.calls[-1]["tools"] == []
    await session.close()


@pytest.mark.parametrize(
    "analysis_text",
    [
        "",
        "not json",
        "```json\n{}\n```",
        "{}",
        '{"source_event_ids":["event-1"],"summary":"Analysis","tags":[],"extra":true}',
        '{"source_event_ids":["event-1"],"summary":"first","summary":"second","tags":[]}',
    ],
)
async def test_invalid_analysis_json_is_silent_and_never_retried(analysis_text):
    llm = FakeLLM(results=memory_turn_results(analysis=LLMResult(text=analysis_text)))
    tts = FakeTTS()
    session = CompanionSession(providers=fake_providers(llm=llm, tts=tts))
    await submit_memory_turn(session, records=[canonical_event()])
    await session.wait_for_turn("turn-1")

    assert len(llm.calls) == 3
    assert tts.calls == ["I noticed a recovery pattern."]
    assert not any(
        isinstance(event, (AnnotationProposal, ErrorMessage))
        for event in session.outbound_events
    )
    await session.close()


@pytest.mark.parametrize(
    "analysis_text",
    [
        analysis_json(summary="é" * 257),
        analysis_json(tags=["é" * 33]),
        analysis_json(source_event_ids=["é" * 65]),
        analysis_json(source_event_ids=["event-1", "event-1"]),
        analysis_json(tags=[f"tag-{index}" for index in range(17)]),
    ],
)
async def test_analysis_enforces_salvage_utf8_byte_and_collection_bounds(
    analysis_text,
):
    llm = FakeLLM(results=memory_turn_results(analysis=LLMResult(text=analysis_text)))
    session = CompanionSession(providers=fake_providers(llm=llm))
    await submit_memory_turn(session, records=[canonical_event()])
    await session.wait_for_turn("turn-1")

    assert not any(
        isinstance(event, AnnotationProposal) for event in session.outbound_events
    )
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    await session.close()


async def test_analysis_accepts_salvage_utf8_byte_boundaries():
    event_id = "é" * 64
    summary = "é" * 256
    tag = "é" * 32
    llm = FakeLLM(
        results=memory_turn_results(
            analysis=LLMResult(
                text=analysis_json(
                    source_event_ids=[event_id],
                    summary=summary,
                    tags=[tag],
                )
            )
        )
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        proposal_id_factory=lambda: "proposal-boundary",
    )
    await submit_memory_turn(session, records=[canonical_event(event_id)])
    await session.wait_for_turn("turn-1")

    proposal = next(
        event
        for event in session.outbound_events
        if isinstance(event, AnnotationProposal)
    )
    assert proposal.source_event_ids == [event_id]
    assert proposal.summary == summary
    assert proposal.tags == [tag]
    await session.close()


async def test_generated_proposal_id_obeys_salvage_utf8_byte_bound():
    llm = FakeLLM(
        results=memory_turn_results(
            analysis=LLMResult(text=analysis_json()),
        )
    )
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        proposal_id_factory=lambda: "é" * 65,
    )
    await submit_memory_turn(session, records=[canonical_event()])
    await session.wait_for_turn("turn-1")

    assert not any(
        isinstance(event, AnnotationProposal) for event in session.outbound_events
    )
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    await session.close()


@pytest.mark.parametrize(
    ("records", "ok", "error"),
    [
        ([{"event_id": "event-1", "summary": "not canonical"}], True, None),
        ([], False, "memory unavailable"),
    ],
)
async def test_analysis_requires_successful_canonical_memory_records(
    records, ok, error
):
    llm = FakeLLM(results=memory_turn_results())
    session = CompanionSession(providers=fake_providers(llm=llm))
    await submit_memory_turn(session, records=records, ok=ok, error=error)
    await session.wait_for_turn("turn-1")

    assert len(llm.calls) == 2
    assert not any(
        isinstance(event, AnnotationProposal) for event in session.outbound_events
    )
    await session.close()


class BlockingAnalysisLLM(FakeLLM):
    def __init__(self):
        super().__init__(results=memory_turn_results())
        self.analysis_started = asyncio.Event()

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        if self.results:
            return await super().respond(messages, tools)
        self.calls.append({"messages": messages, "tools": tools})
        self.analysis_started.set()
        await asyncio.Event().wait()


async def test_analysis_timeout_does_not_fail_completed_spoken_turn():
    llm = BlockingAnalysisLLM()
    tts = FakeTTS()
    session = CompanionSession(
        providers=fake_providers(llm=llm, tts=tts),
        timeouts=ProviderTimeouts(stt=1.0, llm=0.01, tts=1.0, tool=1.0),
    )
    await submit_memory_turn(session, records=[canonical_event()])
    await session.wait_for_turn("turn-1")

    assert llm.analysis_started.is_set()
    assert tts.calls == ["I noticed a recovery pattern."]
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    await session.close()


class CancellationIgnoringAnalysisLLM(FakeLLM):
    def __init__(self):
        super().__init__(results=memory_turn_results())
        self.analysis_started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        if self.results:
            return await super().respond(messages, tools)
        self.calls.append({"messages": messages, "tools": tools})
        self.analysis_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
        return LLMResult(text=analysis_json(summary="Stale analysis."))


async def test_new_turn_cancels_stale_analysis_output():
    llm = CancellationIgnoringAnalysisLLM()
    session = CompanionSession(providers=fake_providers(llm=llm))
    await submit_memory_turn(session, turn_id="old", records=[canonical_event()])
    async with asyncio.timeout(1.0):
        await asyncio.wait_for(llm.analysis_started.wait(), timeout=1.0)

    await session.start_turn("new", {})
    llm.release.set()
    await session.wait_for_turn("old")

    assert not any(
        isinstance(event, AnnotationProposal) and event.turn_id == "old"
        for event in session.outbound_events
    )
    await session.close()


async def test_annotation_cooldown_updates_only_after_emission_and_queues_nothing():
    now = [100.0]
    results = []
    results.extend(
        memory_turn_results(
            suffix="0",
            narration="Neutral narration zero.",
            analysis=LLMResult(text="NO_ANALYSIS"),
        )
    )
    results.extend(
        memory_turn_results(
            suffix="1",
            narration="Neutral narration one.",
            analysis=LLMResult(text=analysis_json(source_event_ids=["event-1"])),
        )
    )
    results.extend(memory_turn_results(suffix="2", narration="Neutral narration two."))
    results.extend(
        memory_turn_results(
            suffix="3",
            narration="Neutral narration three.",
            analysis=LLMResult(text=analysis_json(source_event_ids=["event-3"])),
        )
    )
    llm = FakeLLM(results=results)
    session = CompanionSession(
        providers=fake_providers(llm=llm),
        annotation_cooldown_seconds=60.0,
        monotonic=lambda: now[0],
        proposal_id_factory=lambda: f"proposal-{now[0]}",
    )

    await submit_memory_turn(session, turn_id="turn-0", records=[canonical_event()])
    await session.wait_for_turn("turn-0")
    await submit_memory_turn(session, turn_id="turn-1", records=[canonical_event()])
    await session.wait_for_turn("turn-1")
    await submit_memory_turn(
        session,
        turn_id="turn-2",
        records=[canonical_event("event-2")],
    )
    await session.wait_for_turn("turn-2")
    now[0] += 60.0
    await submit_memory_turn(
        session,
        turn_id="turn-3",
        records=[canonical_event("event-3")],
    )
    await session.wait_for_turn("turn-3")

    proposals = [
        event
        for event in session.outbound_events
        if isinstance(event, AnnotationProposal)
    ]
    assert [proposal.turn_id for proposal in proposals] == ["turn-1", "turn-3"]
    assert len([call for call in llm.calls if call["tools"] == []]) == 3
    assert not hasattr(session, "pending_annotation_proposals")
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
    analysis_prompt = ASTER_ANALYSIS_SYSTEM_PROMPT.lower()

    assert "aster" in prompt
    assert "salvage" in prompt
    assert "unknown" in prompt
    assert "supplied game context" in prompt
    assert "navigable_bodies" in prompt
    assert "aliases" in prompt
    assert "canonical body_id" in prompt
    assert "recent_journal_items" in prompt
    assert "canonical_episode" in prompt
    assert "companion_analysis" in prompt
    assert "you are currently" not in prompt
    assert "your ship is" not in prompt
    assert "companion analysis" in prompt
    assert "never say" in prompt
    assert "saved or accepted" in prompt
    assert "canonical gameplay records" in analysis_prompt
    assert "event_id" in analysis_prompt
    assert "no_analysis" in analysis_prompt
