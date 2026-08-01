import asyncio
import wave
from io import BytesIO

from api.services.game_companion.persona import ASTER_SYSTEM_PROMPT
from api.services.game_companion.protocol import (
    AudioEnd,
    AudioStart,
    Caption,
    ErrorMessage,
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
