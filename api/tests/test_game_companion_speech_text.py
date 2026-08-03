from time import perf_counter
from types import SimpleNamespace

import pytest

from api.services.game_companion.protocol import Caption
from api.services.game_companion.providers import (
    FishTTSAdapter,
    FishTTSSettings,
    LLMResult,
    OpenRouterTTSAdapter,
    ProviderSet,
)
from api.services.game_companion.session import CompanionSession
from api.services.game_companion.speech_text import normalize_speech_text


class StaticSTT:
    async def transcribe(self, wav_audio: bytes) -> str:
        return "Report status"


class StaticLLM:
    def __init__(self, text: str):
        self.text = text

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        return LLMResult(text=self.text)


class RecordingOpenRouterService:
    def __init__(self):
        self.texts = []

    async def run_tts(self, text: str, context_id: str):
        self.texts.append(text)
        yield SimpleNamespace(
            audio=b"\x01\x00",
            sample_rate=24000,
            num_channels=1,
        )


class FakeFishResponse:
    status_code = 200

    async def aiter_bytes(self):
        yield b"\x01\x00"


class FakeFishResponseContext:
    async def __aenter__(self):
        return FakeFishResponse()

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class RecordingFishClient:
    def __init__(self):
        self.texts = []

    def stream(self, method: str, url: str, **kwargs):
        self.texts.append(kwargs["json"]["text"])
        return FakeFishResponseContext()

    async def aclose(self):
        return None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**Engines stable.**", "Engines stable."),
        ("*Captain*, proceed.", "Captain, proceed."),
        ("***Warning***", "Warning"),
        ("**bold and *urgent***", "bold and urgent"),
        ("Status: (**green**); continue.", "Status: (green); continue."),
        ("Course is set.", "Course is set."),
        ("Use * as the wildcard operator.", "Use * as the wildcard operator."),
        ("Two * three remains literal.", "Two * three remains literal."),
        ("Call sign A*B* remains literal.", "Call sign A*B* remains literal."),
        ("*a*a*", "*a*a*"),
        ("*a***", "*a***"),
        ("***a*", "***a*"),
        ("*****", "*"),
        (
            r"Escaped \*markers\* remain literal.",
            r"Escaped \*markers\* remain literal.",
        ),
        (r"*foo\* bar*", r"foo\* bar"),
        (r"*literal\*", r"*literal\*"),
    ],
)
def test_normalize_speech_text_handles_only_paired_markdown_emphasis(source, expected):
    assert normalize_speech_text(source) == expected


def test_normalize_speech_text_is_idempotent():
    source = "Report: (**engines stable**), *Captain*; proceed."

    normalized = normalize_speech_text(source)

    assert normalize_speech_text(normalized) == normalized


def test_normalize_speech_text_is_linear_for_unmatched_markers():
    source = " **a" * 16_000

    started = perf_counter()
    normalized = normalize_speech_text(source)
    elapsed = perf_counter() - started

    assert normalized == source
    assert elapsed < 1.0


@pytest.mark.parametrize("provider_name", ["openrouter", "fish"])
async def test_session_normalizes_same_tts_copy_for_both_provider_paths(provider_name):
    response_text = "Report: (**engines stable**), *Captain*; proceed."
    if provider_name == "openrouter":
        recorder = RecordingOpenRouterService()
        tts = OpenRouterTTSAdapter(recorder)
    else:
        recorder = RecordingFishClient()
        tts = FishTTSAdapter(
            FishTTSSettings(api_key="synthetic-test-key"),
            client=recorder,
        )
    session = CompanionSession(
        providers=ProviderSet(
            stt=StaticSTT(),
            llm=StaticLLM(response_text),
            tts=tts,
        )
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
    assert recorder.texts == ["Report: (engines stable), Captain; proceed."]
    await session.close()
