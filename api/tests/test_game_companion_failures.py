import asyncio
import json
from types import SimpleNamespace

import pytest

from api.routes.game_companion import _receive_frame, serve_game_companion
from api.services.game_companion import protocol as protocol_module
from api.services.game_companion import providers as providers_module
from api.services.game_companion import session as session_module
from api.services.game_companion.protocol import (
    MAX_BINARY_FRAME_BYTES,
    MAX_JSON_BYTES,
    MAX_TURN_AUDIO_BYTES,
    AudioEnd,
    AudioStart,
    Caption,
    ClientMessageOrder,
    ErrorMessage,
    Hello,
    MemoryQuery,
    MemoryResult,
    ProtocolError,
    ToolCall,
    ToolResult,
    TurnStart,
)
from api.services.game_companion.providers import (
    CooldownFallbackTTSAdapter,
    LLMResult,
    LLMToolCall,
    OpenRouterLLMAdapter,
    PCMChunk,
    ProviderError,
    ProviderSet,
)
from api.services.game_companion.session import CompanionSession, ProviderTimeouts

HELLO = {
    "type": "hello",
    "protocol_version": 1,
    "client": "salvage",
    "save_id": "phase_2_prototype",
    "capabilities": ["pcm_s16le", "captions", "tools", "memory"],
}


class FakeWebSocket:
    def __init__(self, events):
        self.events = list(events)
        self.accept_calls = 0
        self.sent_text = []
        self.sent_bytes = []
        self.close_calls = []

    async def accept(self):
        self.accept_calls += 1

    async def receive(self):
        if not self.events:
            return {"type": "websocket.disconnect", "code": 1000}
        return self.events.pop(0)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_bytes(self, audio):
        self.sent_bytes.append(audio)

    async def close(self, *, code, reason):
        self.close_calls.append((code, reason))


class FakeSTT:
    def __init__(self, text="Take me to the moon"):
        self.text = text
        self.close_calls = 0

    async def transcribe(self, wav_audio):
        return self.text

    async def close(self):
        self.close_calls += 1


class QueueLLM:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.close_calls = 0

    async def respond(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self.results.pop(0)

    async def close(self):
        self.close_calls += 1


class RecordingTTS:
    def __init__(self):
        self.calls = []
        self.close_calls = 0

    async def synthesize(self, text):
        self.calls.append(text)
        yield PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)

    async def close(self):
        self.close_calls += 1


class ChunkTTS(RecordingTTS):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks

    async def synthesize(self, text):
        self.calls.append(text)
        for chunk in self.chunks:
            yield chunk


class StaticLLM(QueueLLM):
    def __init__(self, result=None):
        super().__init__([])
        self.result = result or LLMResult(text="Course set.")

    async def respond(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self.result


def providers(*, stt=None, llm=None, tts=None):
    return ProviderSet(
        stt=stt or FakeSTT(),
        llm=llm or QueueLLM([LLMResult(text="Course set.")]),
        tts=tts or RecordingTTS(),
    )


async def wait_for_event(session, event_type, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while True:
            for event in session.outbound_events:
                if isinstance(event, event_type):
                    return event
            await asyncio.sleep(0)


async def begin_turn(session, turn_id="turn-1"):
    await session.start_turn(turn_id, {})
    await session.append_audio(turn_id, b"\x00\x00")
    await session.end_turn(turn_id)


def canonical_event(event_id, game_time):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "session_started",
        "occurred_at_utc": "2026-08-01T15:00:00Z",
        "game_time": game_time,
        "body_id": "planet_01_moon",
        "importance": "normal",
        "status": "completed",
        "summary": "Session started.",
    }


async def test_abrupt_disconnect_closes_session_and_providers_once():
    provider_set = providers()
    sessions = []

    def factory(emit):
        session = CompanionSession(providers=provider_set, emit=emit)
        sessions.append(session)
        return session

    websocket = FakeWebSocket(
        [
            {"type": "websocket.receive", "text": json.dumps(HELLO)},
            {"type": "websocket.disconnect", "code": 1006},
        ]
    )
    await serve_game_companion(websocket, session_factory=factory)
    await sessions[0].close()

    assert websocket.accept_calls == 1
    assert provider_set.stt.close_calls == 1
    assert provider_set.llm.close_calls == 1
    assert provider_set.tts.close_calls == 1


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("\ud800", "invalid_utf8"),
        ("{", "invalid_json"),
        ('{"type":"turn_end","turn_id":"one","turn_id":"two"}', "invalid_json"),
        (
            '{"type":"turn_start","turn_id":"turn-1","context":{"value":NaN}}',
            "invalid_json",
        ),
    ],
)
async def test_malformed_control_text_is_rejected(text, expected_code):
    websocket = FakeWebSocket([{"type": "websocket.receive", "text": text}])

    with pytest.raises(ProtocolError) as error:
        await _receive_frame(websocket)

    assert error.value.code == expected_code


async def test_binary_before_turn_and_oversized_frames_fail_closed():
    order = ClientMessageOrder()
    with pytest.raises(ProtocolError, match="binary_outside_turn"):
        order.accept_binary(2)

    order.accept(Hello.model_validate(HELLO))
    order.accept(TurnStart(type="turn_start", turn_id="turn-1", context={}))
    with pytest.raises(ProtocolError, match="audio_frame_too_large"):
        order.accept_binary(MAX_BINARY_FRAME_BYTES + 1)

    for _ in range(MAX_TURN_AUDIO_BYTES // MAX_BINARY_FRAME_BYTES):
        order.accept_binary(MAX_BINARY_FRAME_BYTES)
    with pytest.raises(ProtocolError, match="turn_audio_too_large"):
        order.accept_binary(2)


async def test_oversized_json_is_rejected_before_decoding():
    websocket = FakeWebSocket(
        [{"type": "websocket.receive", "text": "x" * (MAX_JSON_BYTES + 1)}]
    )

    with pytest.raises(ProtocolError, match="message_too_large"):
        await _receive_frame(websocket)


class RouteSession:
    def __init__(self, emit):
        self.emit = emit
        self.active_turn_id = None
        self.close_calls = 0
        self.audio = []
        self.ended = []
        self.tool_results = []
        self.memory_results = []

    async def start_turn(self, turn_id, context):
        self.active_turn_id = turn_id

    async def append_audio(self, turn_id, audio):
        self.audio.append((turn_id, audio))

    async def end_turn(self, turn_id):
        self.ended.append(turn_id)

    async def interrupt(self, turn_id):
        self.active_turn_id = None

    async def submit_tool_result(self, result):
        self.tool_results.append(result)

    async def submit_memory_result(self, result):
        self.memory_results.append(result)

    async def close(self):
        self.close_calls += 1


@pytest.mark.parametrize(
    ("stale_result", "current_result", "result_attribute"),
    [
        (
            {
                "type": "tool_result",
                "turn_id": "old",
                "call_id": "late-call",
                "ok": False,
                "error": "late result",
            },
            {
                "type": "tool_result",
                "turn_id": "new",
                "call_id": "current-call",
                "ok": False,
                "error": "current result",
            },
            "tool_results",
        ),
        (
            {
                "type": "memory_result",
                "turn_id": "old",
                "query_id": "late-query",
                "ok": False,
                "error": "late result",
            },
            {
                "type": "memory_result",
                "turn_id": "new",
                "query_id": "current-query",
                "ok": False,
                "error": "current result",
            },
            "memory_results",
        ),
    ],
)
async def test_retired_turn_results_are_discarded_without_breaking_new_turn(
    stale_result,
    current_result,
    result_attribute,
):
    messages = [
        HELLO,
        {"type": "turn_start", "turn_id": "old", "context": {}},
        {"type": "turn_end", "turn_id": "old"},
        {"type": "turn_start", "turn_id": "new", "context": {}},
        stale_result,
        {"binary": True},
        {"type": "turn_end", "turn_id": "new"},
        current_result,
    ]
    events = []
    for message in messages:
        if message.get("binary"):
            events.append({"type": "websocket.receive", "bytes": b"\x00\x00"})
        else:
            events.append({"type": "websocket.receive", "text": json.dumps(message)})
    websocket = FakeWebSocket(events)
    sessions = []

    def factory(emit):
        session = RouteSession(emit)
        sessions.append(session)
        return session

    await serve_game_companion(websocket, session_factory=factory)

    control_frames = [json.loads(text) for text in websocket.sent_text]
    assert not any(frame["type"] == "error" for frame in control_frames)
    assert websocket.close_calls == []
    assert sessions[0].audio == [("new", b"\x00\x00")]
    assert sessions[0].ended == ["old", "new"]
    results = getattr(sessions[0], result_attribute)
    assert len(results) == 1
    assert results[0].turn_id == "new"
    assert sessions[0].close_calls == 1


async def test_retired_turn_tracking_is_bounded():
    order = ClientMessageOrder()
    order.accept(Hello.model_validate(HELLO))
    limit = protocol_module.MAX_RETIRED_TURN_IDS

    for index in range(limit + 1):
        order.accept(TurnStart(type="turn_start", turn_id=f"turn-{index}", context={}))

    assert order.retired_turn_count == limit
    late_result = ToolResult(
        type="tool_result",
        turn_id="turn-0",
        call_id="late-call",
        ok=False,
        error="late result",
    )
    assert order.should_discard_retired_result(late_result) is True

    with pytest.raises(ProtocolError, match="turn_history_exhausted"):
        order.accept(TurnStart(type="turn_start", turn_id="turn-overflow", context={}))

    assert order.retired_turn_count == limit
    assert order.should_discard_retired_result(late_result) is True
    with pytest.raises(ProtocolError, match="cannot reuse"):
        order.accept(TurnStart(type="turn_start", turn_id="turn-0", context={}))


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"body_id": ""},
        {"body_id": " planet_01_moon"},
        {"body_id": 7},
        {"body_id": "planet_01_moon", "extra": True},
    ],
)
async def test_invalid_navigation_arguments_fail_before_socket_emission(arguments):
    llm = QueueLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="call-1",
                        name="set_navigation_target",
                        arguments=arguments,
                    ),
                )
            )
        ]
    )
    session = CompanionSession(
        providers=providers(llm=llm),
        timeouts=ProviderTimeouts(stt=1.0, llm=1.0, tts=1.0, tool=0.01),
    )
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    assert not any(isinstance(event, ToolCall) for event in session.outbound_events)
    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_failure"
    await session.close()


async def test_invalid_tool_result_is_rejected_and_late_result_is_stale():
    llm = QueueLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="call-1",
                        name="set_navigation_target",
                        arguments={"body_id": "planet_01_moon"},
                    ),
                )
            ),
            LLMResult(text="The target is set."),
        ]
    )
    session = CompanionSession(providers=providers(llm=llm))
    await begin_turn(session, "old")
    await wait_for_event(session, ToolCall)
    invalid_result = ToolResult(
        type="tool_result",
        turn_id="old",
        call_id="call-1",
        ok=True,
        result={
            "body_id": "planet_01",
            "navigation_state": {
                "target_body_id": "planet_01",
                "status": "targeted",
            },
        },
    )
    try:
        caught = None
        try:
            await session.submit_tool_result(invalid_result)
        except ProtocolError as error:
            caught = error
        assert caught is not None
        assert caught.code == "invalid_tool_result"

        await session.start_turn("new", {})
        with pytest.raises(ProtocolError, match="stale_turn"):
            await session.submit_tool_result(invalid_result)
    finally:
        await session.close()


@pytest.mark.parametrize("game_time", [float("inf"), float("nan"), 10**400])
async def test_non_finite_or_overflowing_game_time_is_ignored_without_crashing(
    game_time,
):
    llm = QueueLLM(
        [
            LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id="query-1",
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            ),
            LLMResult(text="The record was read safely."),
        ]
    )
    tts = RecordingTTS()
    session = CompanionSession(providers=providers(llm=llm, tts=tts))
    await begin_turn(session)
    query = await wait_for_event(session, MemoryQuery)

    try:
        await session.submit_memory_result(
            MemoryResult.model_construct(
                type="memory_result",
                turn_id="turn-1",
                query_id=query.query_id,
                ok=True,
                records=[canonical_event("event-1", game_time)],
                error=None,
            )
        )
        await session.wait_for_turn("turn-1")

        assert tts.calls == ["The record was read safely."]
        assert len(llm.calls) == 2
    finally:
        await session.close()


class BlockingAssistantCaption:
    def __init__(self):
        self.events = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event):
        self.events.append(event)
        if isinstance(event, Caption) and event.speaker == "assistant":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()


async def test_emit_rechecks_turn_ownership_after_cancellation_ignoring_callback():
    emit = BlockingAssistantCaption()
    tts = RecordingTTS()
    session = CompanionSession(providers=providers(tts=tts), emit=emit)
    await begin_turn(session, "old")
    async with asyncio.timeout(1.0):
        await emit.started.wait()

    await session.start_turn("new", {})
    emit.release.set()
    await session.wait_for_turn("old")

    assert tts.calls == []
    assert not any(
        isinstance(event, (AudioStart, AudioEnd)) and event.turn_id == "old"
        for event in emit.events
    )
    await session.close()


class BlockingSTT(FakeSTT):
    async def transcribe(self, wav_audio):
        await asyncio.Event().wait()


async def test_provider_timeout_remains_recoverable():
    session = CompanionSession(
        providers=providers(stt=BlockingSTT()),
        timeouts=ProviderTimeouts(stt=0.01, llm=1.0, tts=1.0, tool=1.0),
    )
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_timeout"
    assert error.recoverable is True
    await session.close()


class FailingTTS(RecordingTTS):
    async def synthesize(self, text):
        self.calls.append(text)
        yield PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
        raise RuntimeError("TTS failed")


async def test_tts_failure_closes_audio_then_emits_recoverable_error():
    session = CompanionSession(providers=providers(tts=FailingTTS()))
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    event_types = [type(event) for event in session.outbound_events]
    assert event_types.index(AudioStart) < event_types.index(bytes)
    assert event_types.index(bytes) < event_types.index(AudioEnd)
    assert event_types.index(AudioEnd) < event_types.index(ErrorMessage)
    await session.close()


async def test_fish_failure_is_recoverable_and_openrouter_resumes_next_turn():
    class FailedFishTTS(RecordingTTS):
        async def synthesize(self, text):
            self.calls.append(text)
            raise ProviderError("Fish unavailable")
            yield  # pragma: no cover - keeps this an async generator.

    fish = FailedFishTTS()
    openrouter = RecordingTTS()
    tts = CooldownFallbackTTSAdapter(
        primary=fish,
        fallback=openrouter,
        cooldown_seconds=60.0,
        monotonic=lambda: 100.0,
    )
    session = CompanionSession(
        providers=providers(llm=StaticLLM(), tts=tts),
    )

    await begin_turn(session, "fish-turn")
    await session.wait_for_turn("fish-turn")

    first_error = next(
        event
        for event in session.outbound_events
        if isinstance(event, ErrorMessage) and event.turn_id == "fish-turn"
    )
    assert first_error.code == "provider_failure"
    assert first_error.recoverable is True
    assert not any(
        isinstance(event, AudioStart) and event.turn_id == "fish-turn"
        for event in session.outbound_events
    )

    await begin_turn(session, "fallback-turn")
    await session.wait_for_turn("fallback-turn")

    assert fish.calls == ["Course set."]
    assert openrouter.calls == ["Course set."]
    assert any(
        isinstance(event, AudioStart) and event.turn_id == "fallback-turn"
        for event in session.outbound_events
    )
    await session.close()


@pytest.mark.parametrize(
    "chunk",
    [
        PCMChunk(audio=b"\x01\x00", sample_rate=1, channels=1),
        PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=2),
        PCMChunk(audio=b"\x01", sample_rate=24000, channels=1),
    ],
)
async def test_invalid_first_tts_chunk_does_not_open_or_close_audio(chunk):
    session = CompanionSession(providers=providers(tts=ChunkTTS([chunk])))
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    assert not any(
        isinstance(event, (AudioStart, AudioEnd, bytes))
        for event in session.outbound_events
    )
    error = next(
        event for event in session.outbound_events if isinstance(event, ErrorMessage)
    )
    assert error.code == "provider_failure"
    assert error.recoverable is True
    await session.close()


@pytest.mark.parametrize(
    "invalid_chunk",
    [
        PCMChunk(audio=b"\x02\x00", sample_rate=1, channels=1),
        PCMChunk(audio=b"\x02\x00", sample_rate=24000, channels=2),
        PCMChunk(audio=b"\x02", sample_rate=24000, channels=1),
    ],
)
async def test_invalid_midstream_tts_chunk_closes_only_started_audio(invalid_chunk):
    valid_chunk = PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
    session = CompanionSession(
        providers=providers(tts=ChunkTTS([valid_chunk, invalid_chunk]))
    )
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    assert sum(isinstance(event, AudioStart) for event in session.outbound_events) == 1
    assert sum(isinstance(event, AudioEnd) for event in session.outbound_events) == 1
    assert [event for event in session.outbound_events if isinstance(event, bytes)] == [
        valid_chunk.audio
    ]
    assert any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    await session.close()


async def test_tts_audio_accepts_exact_cumulative_limit():
    limit = session_module.MAX_OUTPUT_AUDIO_BYTES
    chunk = PCMChunk(
        audio=b"\x00" * MAX_BINARY_FRAME_BYTES,
        sample_rate=24000,
        channels=1,
    )
    chunks = [chunk] * (limit // len(chunk.audio))
    remainder = limit % len(chunk.audio)
    if remainder:
        chunks.append(
            PCMChunk(
                audio=b"\x00" * remainder,
                sample_rate=24000,
                channels=1,
            )
        )
    session = CompanionSession(providers=providers(tts=ChunkTTS(chunks)))
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    audio = [event for event in session.outbound_events if isinstance(event, bytes)]
    assert sum(map(len, audio)) == limit
    assert not any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    assert sum(isinstance(event, AudioEnd) for event in session.outbound_events) == 1
    await session.close()


async def test_tts_audio_rejects_one_sample_over_cumulative_limit():
    limit = session_module.MAX_OUTPUT_AUDIO_BYTES
    chunk = PCMChunk(
        audio=b"\x00" * MAX_BINARY_FRAME_BYTES,
        sample_rate=24000,
        channels=1,
    )
    chunks = [chunk] * (limit // len(chunk.audio))
    remainder = limit % len(chunk.audio)
    if remainder:
        chunks.append(
            PCMChunk(
                audio=b"\x00" * remainder,
                sample_rate=24000,
                channels=1,
            )
        )
    chunks.append(PCMChunk(audio=b"\x00\x00", sample_rate=24000, channels=1))
    session = CompanionSession(providers=providers(tts=ChunkTTS(chunks)))
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    audio = [event for event in session.outbound_events if isinstance(event, bytes)]
    assert sum(map(len, audio)) == limit
    assert any(isinstance(event, ErrorMessage) for event in session.outbound_events)
    assert sum(isinstance(event, AudioEnd) for event in session.outbound_events) == 1
    await session.close()


class CancellationIgnoringSTT(FakeSTT):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, wav_audio):
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        return "stale transcript"


class RepeatedCancellationIgnoringSTT(FakeSTT):
    def __init__(self):
        super().__init__()
        self.started_count = 0
        self.release = asyncio.Event()

    async def transcribe(self, wav_audio):
        self.started_count += 1
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        return "stale transcript"


async def test_repeated_cancellation_resistant_turns_fail_closed_at_backlog_bound(
    monkeypatch,
):
    backlog_limit = 3
    monkeypatch.setattr(
        session_module,
        "MAX_BACKGROUND_TURN_TASKS",
        backlog_limit,
        raising=False,
    )
    monkeypatch.setattr(
        session_module,
        "SESSION_CLOSE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    stt = RepeatedCancellationIgnoringSTT()
    session = CompanionSession(providers=providers(stt=stt))

    for index in range(backlog_limit):
        await begin_turn(session, f"blocked-{index}")
        async with asyncio.timeout(0.5):
            while stt.started_count < index + 1:
                await asyncio.sleep(0)

    assert session.background_turn_task_count == backlog_limit
    overflow_error = None
    try:
        await session.start_turn("blocked-overflow", {})
    except ProtocolError as exc:
        overflow_error = exc

    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    closed_in_time = close_task.done()
    stt.release.set()
    await asyncio.wait_for(close_task, timeout=0.5)
    async with asyncio.timeout(0.5):
        while session.background_turn_task_count:
            await asyncio.sleep(0)

    assert overflow_error is not None
    assert overflow_error.code == "provider_backlog_exhausted"
    assert overflow_error.recoverable is True
    assert closed_in_time is True


async def test_close_is_bounded_when_active_provider_ignores_cancellation(monkeypatch):
    monkeypatch.setattr(
        session_module,
        "SESSION_CLOSE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    stt = CancellationIgnoringSTT()
    session = CompanionSession(providers=providers(stt=stt))
    await begin_turn(session)
    await stt.started.wait()

    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    closed_in_time = close_task.done()
    stt.release.set()
    await asyncio.wait_for(close_task, timeout=0.5)
    async with asyncio.timeout(0.5):
        while session.background_turn_task_count:
            await asyncio.sleep(0)

    assert closed_in_time is True
    assert not any(isinstance(event, Caption) for event in session.outbound_events)


class HangingCloseSTT(FakeSTT):
    def __init__(self):
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self):
        self.close_started.set()
        while not self.release_close.is_set():
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                continue
        self.close_finished.set()


async def test_close_is_bounded_when_provider_close_ignores_cancellation(monkeypatch):
    monkeypatch.setattr(
        session_module,
        "SESSION_CLOSE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    stt = HangingCloseSTT()
    session = CompanionSession(providers=providers(stt=stt))

    close_task = asyncio.create_task(session.close())
    await stt.close_started.wait()
    await asyncio.sleep(0.05)
    closed_in_time = close_task.done()
    stt.release_close.set()
    await asyncio.wait_for(close_task, timeout=0.5)
    await asyncio.wait_for(stt.close_finished.wait(), timeout=0.5)

    assert closed_in_time is True


async def test_analysis_stream_is_bounded_while_provider_accumulates_chunks():
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="x" * 8192, tool_calls=[])
                )
            ]
        )
        for _ in range(4)
    ]

    class Stream:
        def __init__(self):
            self.closed = False
            self.yielded = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not chunks:
                raise StopAsyncIteration
            self.yielded += 1
            return chunks.pop(0)

        async def close(self):
            self.closed = True

    stream = Stream()

    class Completions:
        async def create(self, **params):
            return stream

    service = SimpleNamespace(
        _client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        build_chat_completion_params=lambda params: params,
    )

    with pytest.raises(ProviderError, match="response.*too large"):
        await OpenRouterLLMAdapter(service).respond(
            [{"role": "user", "content": "canonical records"}], []
        )

    assert stream.yielded == 3
    assert stream.closed is True


async def test_llm_tool_call_id_fragments_cannot_bypass_response_byte_limit():
    fragment_size = 8192
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="x" * fragment_size,
                                function=None,
                            )
                        ],
                    )
                )
            ]
        )
        for _ in range(providers_module.MAX_LLM_RESPONSE_BYTES // fragment_size + 1)
    ]

    class Stream:
        def __init__(self):
            self.closed = False
            self.yielded = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not chunks:
                raise StopAsyncIteration
            self.yielded += 1
            return chunks.pop(0)

        async def close(self):
            self.closed = True

    stream = Stream()

    class Completions:
        async def create(self, **params):
            return stream

    service = SimpleNamespace(
        _client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        build_chat_completion_params=lambda params: params,
    )

    with pytest.raises(ProviderError, match="response.*too large"):
        await OpenRouterLLMAdapter(service).respond(
            [{"role": "user", "content": "set a target"}],
            [{"type": "function"}],
        )

    assert stream.closed is True


async def test_llm_empty_tool_fragments_are_count_bounded():
    limit = providers_module.MAX_LLM_TOOL_FRAGMENTS
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(index=index, id=None, function=None)
                        ],
                    )
                )
            ]
        )
        for index in range(limit + 1)
    ]

    class Stream:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not chunks:
                raise StopAsyncIteration
            return chunks.pop(0)

        async def close(self):
            self.closed = True

    stream = Stream()

    class Completions:
        async def create(self, **params):
            return stream

    service = SimpleNamespace(
        _client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        build_chat_completion_params=lambda params: params,
    )

    with pytest.raises(ProviderError, match="too many tool fragments"):
        await OpenRouterLLMAdapter(service).respond(
            [{"role": "user", "content": "set a target"}],
            [{"type": "function"}],
        )

    assert stream.closed is True


async def test_persistent_session_bookkeeping_is_bounded():
    async def discard(_event):
        return None

    session = CompanionSession(
        providers=providers(llm=StaticLLM()),
        emit=discard,
    )
    retained_limit = session_module.MAX_RETAINED_TURN_TASKS
    cancelled_limit = session_module.MAX_CANCELLED_TURN_IDS

    for index in range(retained_limit + 5):
        turn_id = f"completed-{index}"
        await begin_turn(session, turn_id)
        await session.wait_for_turn(turn_id)

    await asyncio.sleep(0)
    assert session.retained_turn_task_count == retained_limit
    assert session.background_turn_task_count == 0

    for index in range(cancelled_limit + 5):
        await session.start_turn(f"interrupted-{index}", {})

    assert len(session.cancelled_turn_ids) == cancelled_limit
    await session.close()


@pytest.mark.parametrize(
    "claim",
    [
        "Companion Analysis was saved and accepted.",
        "I wrote your analysis to the journal.",
        "Your annotation has been persisted.",
        "I've logged this insight.",
        "I put the analysis in your journal.",
        "I appended the analysis to your journal.",
        "I updated the analysis in your journal.",
        "I committed the annotation to memory.",
        "I archived the insight.",
        "I published the analysis.",
        "The annotation was not saved but was stored.",
    ],
)
async def test_unsupported_analysis_persistence_claim_is_not_spoken(claim):
    tts = RecordingTTS()
    session = CompanionSession(
        providers=providers(
            llm=QueueLLM([LLMResult(text=claim)]),
            tts=tts,
        )
    )
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    assert tts.calls == ["I found a pattern in the expedition record."]
    assistant_caption = next(
        event
        for event in session.outbound_events
        if isinstance(event, Caption) and event.speaker == "assistant"
    )
    assert assistant_caption.text == tts.calls[0]
    await session.close()


@pytest.mark.parametrize(
    "statement",
    [
        "The annotation was not saved.",
        "I did not write this analysis to the journal.",
        "I did not append this analysis to the journal.",
        "The annotation was never published.",
        "The annotation was not saved but was not stored.",
        "I found a pattern in the expedition record.",
    ],
)
async def test_non_persistence_statements_are_preserved(statement):
    tts = RecordingTTS()
    session = CompanionSession(
        providers=providers(
            llm=QueueLLM([LLMResult(text=statement)]),
            tts=tts,
        )
    )
    await begin_turn(session)
    await session.wait_for_turn("turn-1")

    assert tts.calls == [statement]
    await session.close()
