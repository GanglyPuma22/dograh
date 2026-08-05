import asyncio
import wave
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest

from api.services.game_companion.protocol import MAX_BINARY_FRAME_BYTES
from api.services.game_companion.providers import (
    CooldownFallbackTTSAdapter,
    FishTTSAdapter,
    FishTTSSettings,
    GameCompanionProviderSettings,
    OpenRouterLLMAdapter,
    OpenRouterProviderSettings,
    OpenRouterSTTAdapter,
    OpenRouterTTSAdapter,
    PCMChunk,
    ProviderConfigurationError,
    ProviderError,
    ProviderSet,
    create_game_companion_provider_set,
    create_openrouter_provider_set,
    pcm_s16le_to_wav,
)


class FakeFishResponse:
    def __init__(
        self,
        chunks=(),
        *,
        status_code=200,
        stream_error=None,
        content_type="application/octet-stream",
    ):
        self.chunks = list(chunks)
        self.status_code = status_code
        self.headers = (
            {"content-type": content_type} if content_type is not None else {}
        )
        self.stream_error = stream_error
        self.enter_count = 0
        self.exit_count = 0
        self.completed = False
        self.iterated = False

    async def aiter_bytes(self):
        self.iterated = True
        try:
            for chunk in self.chunks:
                yield chunk
            if self.stream_error is not None:
                raise self.stream_error
        finally:
            self.completed = True


class BlockingFishResponse(FakeFishResponse):
    async def aiter_bytes(self):
        self.iterated = True
        try:
            await asyncio.Event().wait()
            yield b""  # pragma: no cover - keeps this an async generator.
        finally:
            self.completed = True


class FishResponseBlockingAfterFirstChunk(FakeFishResponse):
    async def aiter_bytes(self):
        self.iterated = True
        try:
            yield b"\x01\x00"
            await asyncio.Event().wait()
            yield b""  # pragma: no cover - keeps this an async generator.
        finally:
            self.completed = True


class FakeFishResponseContext:
    def __init__(self, response, enter_error=None):
        self.response = response
        self.enter_error = enter_error

    async def __aenter__(self):
        self.response.enter_count += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        self.response.exit_count += 1


class FakeFishHTTPClient:
    def __init__(self, response, *, enter_error=None):
        self.response = response
        self.enter_error = enter_error
        self.calls = []
        self.close_count = 0

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeFishResponseContext(self.response, self.enter_error)

    async def aclose(self):
        self.close_count += 1


class ScriptedTTS:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.close_count = 0

    async def synthesize(self, text):
        self.calls.append(text)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for chunk in outcome:
            yield chunk

    async def close(self):
        self.close_count += 1


def test_pcm_s16le_to_wav_wraps_complete_mono_audio():
    wav_audio = pcm_s16le_to_wav(
        b"\x00\x00\xff\x7f",
        sample_rate=16000,
        channels=1,
    )

    assert wav_audio.startswith(b"RIFF")
    with wave.open(BytesIO(wav_audio), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.readframes(2) == b"\x00\x00\xff\x7f"


async def test_stt_adapter_reuses_service_transcription_without_logging_text():
    class FakeService:
        def __init__(self):
            self.calls = []

        async def _transcribe(self, wav_audio):
            self.calls.append(wav_audio)
            return SimpleNamespace(text="hello from Salvage")

    service = FakeService()
    adapter = OpenRouterSTTAdapter(service)

    result = await adapter.transcribe(b"RIFF-complete-wav")

    assert result == "hello from Salvage"
    assert service.calls == [b"RIFF-complete-wav"]


@pytest.mark.parametrize(("provider_text", "expected"), [("", ""), ("   ", "")])
async def test_stt_adapter_returns_blank_transcripts_for_no_speech_handling(
    provider_text, expected
):
    class FakeService:
        async def _transcribe(self, _wav_audio):
            return SimpleNamespace(text=provider_text)

    result = await OpenRouterSTTAdapter(FakeService()).transcribe(b"RIFF-complete-wav")

    assert result == expected


async def test_stt_adapter_rejects_non_string_transcripts():
    class FakeService:
        async def _transcribe(self, _wav_audio):
            return SimpleNamespace(text=None)

    with pytest.raises(ProviderError, match="invalid transcript"):
        await OpenRouterSTTAdapter(FakeService()).transcribe(b"RIFF-complete-wav")


async def test_tts_adapter_maps_pipecat_pcm_frames():
    class FakeService:
        async def run_tts(self, text, context_id):
            assert text == "Course set."
            assert context_id
            yield SimpleNamespace(
                audio=b"\x01\x00",
                sample_rate=24000,
                num_channels=1,
            )

    chunks = [
        chunk
        async for chunk in OpenRouterTTSAdapter(FakeService()).synthesize("Course set.")
    ]

    assert len(chunks) == 1
    assert chunks[0].audio == b"\x01\x00"
    assert chunks[0].sample_rate == 24000
    assert chunks[0].channels == 1


async def test_tts_adapter_turns_error_frames_into_provider_errors():
    class FakeService:
        async def run_tts(self, text, context_id):
            yield SimpleNamespace(error="provider refused synthesis")

    with pytest.raises(ProviderError, match="provider refused synthesis"):
        async for _chunk in OpenRouterTTSAdapter(FakeService()).synthesize("No audio"):
            pass


async def test_provider_set_closes_underlying_openrouter_clients():
    class FakeClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    clients = [FakeClient(), FakeClient(), FakeClient()]
    providers = ProviderSet(
        stt=OpenRouterSTTAdapter(SimpleNamespace(_client=clients[0])),
        llm=OpenRouterLLMAdapter(SimpleNamespace(_client=clients[1])),
        tts=OpenRouterTTSAdapter(SimpleNamespace(_client=clients[2])),
    )

    await providers.close()

    assert all(client.closed for client in clients)


def completion_chunk(*, content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls)
            )
        ]
    )


@pytest.mark.parametrize(
    ("continuation_id", "continuation_name"),
    [
        (None, None),
        ("call-1", "set_navigation_target"),
    ],
)
async def test_llm_adapter_assembles_streamed_text_and_tool_arguments(
    continuation_id, continuation_name
):
    chunks = [
        completion_chunk(content="Checking "),
        completion_chunk(
            content="the route.",
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(
                        name="set_navigation_target",
                        arguments='{"body_id":"planet_',
                    ),
                )
            ],
        ),
        completion_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=continuation_id,
                    function=SimpleNamespace(
                        name=continuation_name, arguments='01_moon"}'
                    ),
                )
            ]
        ),
    ]

    class FakeStream:
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

    stream = FakeStream()

    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **params):
            self.calls.append(params)
            return stream

    completions = FakeCompletions()

    class FakeService:
        def __init__(self):
            self._client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )
            self.params = []

        def build_chat_completion_params(self, params):
            self.params.append(params)
            return {"model": "deepseek/test", "stream": True, **params}

    tools = [
        {
            "type": "function",
            "function": {
                "name": "set_navigation_target",
                "parameters": {"type": "object"},
            },
        }
    ]
    service = FakeService()
    result = await OpenRouterLLMAdapter(service).respond(
        [{"role": "user", "content": "Take me to the moon"}], tools
    )

    assert result.text == "Checking the route."
    assert result.tool_calls[0].call_id == "call-1"
    assert result.tool_calls[0].name == "set_navigation_target"
    assert result.tool_calls[0].arguments == {"body_id": "planet_01_moon"}
    assert service.params[0]["messages"][0]["role"] == "user"
    assert completions.calls[0]["tools"] == tools
    assert stream.closed is True


async def test_llm_adapter_counts_repeated_tool_metadata_toward_response_limit():
    async def stream():
        yield completion_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(
                        name="set_navigation_target",
                        arguments='{"body_id":"planet_01_moon"}',
                    ),
                )
            ]
        )
        yield completion_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="x" * (70 * 1024),
                    function=SimpleNamespace(name=None, arguments=None),
                )
            ]
        )

    class FakeCompletions:
        async def create(self, **params):
            return stream()

    service = SimpleNamespace(
        _client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
        build_chat_completion_params=lambda params: params,
    )

    with pytest.raises(ProviderError, match="response is too large"):
        await OpenRouterLLMAdapter(service).respond(
            [{"role": "user", "content": "Take me to the moon"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "set_navigation_target",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )


def test_provider_settings_use_explicit_companion_defaults():
    settings = OpenRouterProviderSettings.from_env(
        {"OPENROUTER_API_KEY": "sk-or-v1-test"}
    )

    assert settings.llm_model == "deepseek/deepseek-v4-flash"
    assert settings.stt_model == "qwen/qwen3-asr-flash-2026-02-10"
    assert settings.tts_model == "x-ai/grok-voice-tts-1.0"
    assert settings.tts_voice == "eve"
    assert settings.base_url == "https://openrouter.ai/api/v1"


def test_provider_settings_require_openrouter_key():
    with pytest.raises(ProviderConfigurationError, match="OPENROUTER_API_KEY"):
        OpenRouterProviderSettings.from_env({})


def test_provider_settings_reprs_redact_api_keys():
    openrouter = OpenRouterProviderSettings.from_env(
        {"OPENROUTER_API_KEY": "secret-openrouter-key"}
    )
    fish = FishTTSSettings.from_env({"FISH_API_KEY": "secret-fish-key"})

    assert "secret-openrouter-key" not in repr(openrouter)
    assert "secret-fish-key" not in repr(fish)


def test_game_companion_settings_default_to_openrouter_without_fish_credentials():
    settings = GameCompanionProviderSettings.from_env(
        {"OPENROUTER_API_KEY": "openrouter-test-secret"}
    )

    assert settings.tts_provider == "openrouter"
    assert settings.openrouter.api_key == "openrouter-test-secret"
    assert settings.fish is None


def test_game_companion_settings_load_direct_fish_defaults_when_selected():
    settings = GameCompanionProviderSettings.from_env(
        {
            "OPENROUTER_API_KEY": "openrouter-test-secret",
            "DOGRAH_GAME_COMPANION_TTS_PROVIDER": "fish",
            "FISH_API_KEY": "fish-test-secret",
        }
    )

    assert settings.tts_provider == "fish"
    assert settings.fish == FishTTSSettings(
        api_key="fish-test-secret",
        base_url="https://api.fish.audio",
        model="s2.1-pro-free",
        reference_id=None,
        sample_rate=24000,
        latency="balanced",
        chunk_length=150,
        request_timeout_seconds=30.0,
        fallback_cooldown_seconds=60.0,
    )


def test_game_companion_settings_keep_paid_fish_model_explicitly_configurable():
    settings = GameCompanionProviderSettings.from_env(
        {
            "OPENROUTER_API_KEY": "openrouter-test-secret",
            "DOGRAH_GAME_COMPANION_TTS_PROVIDER": "fish",
            "FISH_API_KEY": "fish-test-secret",
            "DOGRAH_GAME_COMPANION_FISH_MODEL": "s2.1-pro",
            "DOGRAH_GAME_COMPANION_FISH_REFERENCE_ID": "voice-reference-id",
        }
    )

    assert settings.fish is not None
    assert settings.fish.model == "s2.1-pro"
    assert settings.fish.reference_id == "voice-reference-id"


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("DOGRAH_GAME_COMPANION_FISH_SAMPLE_RATE", "44100", "sample rate"),
        ("DOGRAH_GAME_COMPANION_FISH_LATENCY", "fastest", "latency"),
        ("DOGRAH_GAME_COMPANION_FISH_CHUNK_LENGTH", "99", "chunk length"),
        ("DOGRAH_GAME_COMPANION_FISH_CHUNK_LENGTH", "301", "chunk length"),
        ("DOGRAH_GAME_COMPANION_FISH_REQUEST_TIMEOUT_SECONDS", "0", "timeout"),
        (
            "DOGRAH_GAME_COMPANION_FISH_REQUEST_TIMEOUT_SECONDS",
            "45",
            "timeout",
        ),
        (
            "DOGRAH_GAME_COMPANION_FISH_FALLBACK_COOLDOWN_SECONDS",
            "0",
            "cooldown",
        ),
        (
            "DOGRAH_GAME_COMPANION_FISH_FALLBACK_COOLDOWN_SECONDS",
            "601",
            "cooldown",
        ),
        ("DOGRAH_GAME_COMPANION_FISH_MODEL", "s2-pro", "model"),
    ],
)
def test_fish_settings_reject_invalid_bounded_values_without_leaking_secrets(
    name, value, match
):
    fish_secret = "fish-secret-must-not-leak"
    environment = {
        "OPENROUTER_API_KEY": "openrouter-secret-must-not-leak",
        "DOGRAH_GAME_COMPANION_TTS_PROVIDER": "fish",
        "FISH_API_KEY": fish_secret,
        name: value,
    }

    with pytest.raises(ProviderConfigurationError, match=match) as exc_info:
        GameCompanionProviderSettings.from_env(environment)

    assert fish_secret not in str(exc_info.value)
    assert environment["OPENROUTER_API_KEY"] not in str(exc_info.value)


def test_game_companion_settings_require_fish_key_only_when_fish_is_selected():
    with pytest.raises(ProviderConfigurationError, match="FISH_API_KEY"):
        GameCompanionProviderSettings.from_env(
            {
                "OPENROUTER_API_KEY": "openrouter-test-secret",
                "DOGRAH_GAME_COMPANION_TTS_PROVIDER": "fish",
            }
        )


def test_game_companion_settings_reject_unknown_tts_provider_without_secrets():
    secret = "openrouter-secret-must-not-leak"

    with pytest.raises(ProviderConfigurationError, match="TTS provider") as exc_info:
        GameCompanionProviderSettings.from_env(
            {
                "OPENROUTER_API_KEY": secret,
                "DOGRAH_GAME_COMPANION_TTS_PROVIDER": "unknown",
            }
        )

    assert secret not in str(exc_info.value)


async def test_fish_tts_streams_first_pcm_chunk_before_response_completes():
    response = FakeFishResponse([b"\x01\x00", b"\x02\x00"])
    client = FakeFishHTTPClient(response)
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=client,
    )
    stream = adapter.synthesize("Course is set.")

    first = await anext(stream)

    assert first.audio == b"\x01\x00"
    assert first.sample_rate == 24000
    assert first.channels == 1
    assert response.completed is False
    await stream.aclose()
    assert response.exit_count == 1


async def test_fish_tts_accepts_fish_audio_pcm_response():
    response = FakeFishResponse(
        [b"\x01\x00", b"\x02\x00"],
        content_type="audio/pcm; rate=24000",
    )
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(response),
    )

    chunks = [chunk async for chunk in adapter.synthesize("Course is set.")]

    assert [chunk.audio for chunk in chunks] == [b"\x01\x00", b"\x02\x00"]
    assert all(chunk.sample_rate == 24000 for chunk in chunks)
    assert all(chunk.channels == 1 for chunk in chunks)


async def test_fish_tts_sends_bounded_pcm_request_without_logging_or_buffering():
    response = FakeFishResponse([b"\x01\x00"])
    client = FakeFishHTTPClient(response)
    settings = FishTTSSettings(
        api_key="fish-test-secret",
        reference_id="voice-reference-id",
        latency="low",
        chunk_length=100,
        request_timeout_seconds=12.0,
    )
    adapter = FishTTSAdapter(settings, client=client)

    chunks = [chunk async for chunk in adapter.synthesize("Private response text")]

    assert [chunk.audio for chunk in chunks] == [b"\x01\x00"]
    assert client.calls == [
        (
            "POST",
            "https://api.fish.audio/v1/tts",
            {
                "headers": {
                    "Authorization": "Bearer fish-test-secret",
                    "model": "s2.1-pro-free",
                    "Accept": "audio/pcm, application/octet-stream",
                },
                "json": {
                    "text": "Private response text",
                    "format": "pcm",
                    "sample_rate": 24000,
                    "latency": "low",
                    "chunk_length": 100,
                    "normalize": True,
                    "reference_id": "voice-reference-id",
                },
                "timeout": 12.0,
            },
        )
    ]


async def test_fish_tts_repairs_arbitrary_pcm16_http_chunk_boundaries():
    response = FakeFishResponse([b"\x01", b"\x00\x02", b"\x00\x03\x00"])
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(response),
    )

    chunks = [chunk async for chunk in adapter.synthesize("Boundary test")]

    assert b"".join(chunk.audio for chunk in chunks) == (b"\x01\x00\x02\x00\x03\x00")
    assert all(len(chunk.audio) % 2 == 0 for chunk in chunks)


async def test_fish_tts_splits_large_http_chunks_into_protocol_sized_frames():
    audio = b"\x01\x00" * ((MAX_BINARY_FRAME_BYTES // 2) + 2)
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(FakeFishResponse([audio])),
    )

    chunks = [chunk async for chunk in adapter.synthesize("Large chunk")]

    assert [len(chunk.audio) for chunk in chunks] == [MAX_BINARY_FRAME_BYTES, 4]
    assert b"".join(chunk.audio for chunk in chunks) == audio


async def test_fish_total_request_timeout_opens_next_utterance_fallback():
    pcm = PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
    fish = FishTTSAdapter(
        FishTTSSettings(
            api_key="fish-test-secret",
            request_timeout_seconds=0.01,
        ),
        client=FakeFishHTTPClient(BlockingFishResponse()),
    )
    fallback = ScriptedTTS([[pcm]])
    adapter = CooldownFallbackTTSAdapter(
        primary=fish,
        fallback=fallback,
        cooldown_seconds=60.0,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(ProviderError, match="timed out"):
        async with asyncio.timeout(0.1):
            async for _chunk in adapter.synthesize("Timed out Fish utterance"):
                pass

    assert [chunk async for chunk in adapter.synthesize("Fallback utterance")] == [pcm]
    assert fallback.calls == ["Fallback utterance"]


async def test_fish_deadline_never_cancels_downstream_chunk_delivery():
    pcm = PCMChunk(audio=b"\x02\x00", sample_rate=24000, channels=1)
    fish = FishTTSAdapter(
        FishTTSSettings(
            api_key="fish-test-secret",
            request_timeout_seconds=0.01,
        ),
        client=FakeFishHTTPClient(FishResponseBlockingAfterFirstChunk()),
    )
    fallback = ScriptedTTS([[pcm]])
    adapter = CooldownFallbackTTSAdapter(
        primary=fish,
        fallback=fallback,
        cooldown_seconds=60.0,
        monotonic=lambda: 100.0,
    )
    stream = adapter.synthesize("Slow downstream delivery")

    assert (await anext(stream)).audio == b"\x01\x00"
    await asyncio.sleep(0.02)
    with pytest.raises(ProviderError, match="timed out"):
        await anext(stream)

    assert [chunk async for chunk in adapter.synthesize("Fallback utterance")] == [pcm]
    assert fallback.calls == ["Fallback utterance"]


async def test_fish_tts_rejects_http_failure_without_reading_or_exposing_body():
    response = FakeFishResponse(
        [b'{"message":"private provider body"}'], status_code=429
    )
    client = FakeFishHTTPClient(response)
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=client,
    )

    with pytest.raises(ProviderError, match="status 429") as exc_info:
        async for _chunk in adapter.synthesize("Private response text"):
            pass

    assert response.iterated is False
    assert response.exit_count == 1
    assert "private provider body" not in str(exc_info.value)
    assert "fish-test-secret" not in str(exc_info.value)
    assert "Private response text" not in str(exc_info.value)


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "application/json; charset=utf-8",
        "text/plain; charset=utf-8",
        "audio/mpeg",
    ],
)
async def test_fish_tts_rejects_non_pcm_success_response_before_streaming(
    content_type,
):
    response = FakeFishResponse(
        [b'{"error":"proxy failure"}'],
        content_type=content_type,
    )
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(response),
    )

    with pytest.raises(ProviderError, match="content type"):
        _ = [chunk async for chunk in adapter.synthesize("Hello")]

    assert response.iterated is False


async def test_fish_tts_maps_request_timeout_to_secret_safe_provider_error():
    response = FakeFishResponse()
    client = FakeFishHTTPClient(
        response,
        enter_error=httpx.ReadTimeout("private timeout detail"),
    )
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=client,
    )

    with pytest.raises(ProviderError, match="timed out") as exc_info:
        async for _chunk in adapter.synthesize("Private response text"):
            pass

    assert "private timeout detail" not in str(exc_info.value)
    assert "fish-test-secret" not in str(exc_info.value)


async def test_fish_tts_rejects_empty_response():
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(FakeFishResponse()),
    )

    with pytest.raises(ProviderError, match="no audio"):
        async for _chunk in adapter.synthesize("Empty response"):
            pass


async def test_fish_tts_rejects_incomplete_final_pcm_sample():
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(FakeFishResponse([b"\x01\x00\x02"])),
    )

    stream = adapter.synthesize("Incomplete sample")
    first = await anext(stream)

    assert first.audio == b"\x01\x00"
    with pytest.raises(ProviderError, match="incomplete PCM sample"):
        await anext(stream)


async def test_fish_tts_propagates_cancellation_and_closes_response():
    response = FakeFishResponse(stream_error=asyncio.CancelledError())
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=FakeFishHTTPClient(response),
    )

    with pytest.raises(asyncio.CancelledError):
        async for _chunk in adapter.synthesize("Cancelled response"):
            pass

    assert response.exit_count == 1


async def test_fish_tts_closes_response_and_http_client_exactly_once():
    response = FakeFishResponse([b"\x01\x00"])
    client = FakeFishHTTPClient(response)
    adapter = FishTTSAdapter(
        FishTTSSettings(api_key="fish-test-secret"),
        client=client,
    )

    assert [chunk.audio async for chunk in adapter.synthesize("Close resources")] == [
        b"\x01\x00"
    ]
    await adapter.close()
    await adapter.close()

    assert response.enter_count == 1
    assert response.exit_count == 1
    assert client.close_count == 1


async def test_fish_failure_never_substitutes_audio_until_the_next_utterance():
    pcm = PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
    primary = ScriptedTTS([ProviderError("Fish unavailable"), [pcm]])
    fallback = ScriptedTTS([[pcm]])
    now = [100.0]
    adapter = CooldownFallbackTTSAdapter(
        primary=primary,
        fallback=fallback,
        cooldown_seconds=60.0,
        monotonic=lambda: now[0],
    )

    with pytest.raises(ProviderError, match="Fish unavailable"):
        async for _chunk in adapter.synthesize("Failed Fish utterance"):
            pass

    assert fallback.calls == []
    assert [chunk async for chunk in adapter.synthesize("Cooldown utterance")] == [pcm]
    assert fallback.calls == ["Cooldown utterance"]

    now[0] += 60.0
    assert [chunk async for chunk in adapter.synthesize("Fish retry")] == [pcm]
    assert primary.calls == ["Failed Fish utterance", "Fish retry"]


async def test_fish_cancellation_does_not_open_fallback_cooldown():
    pcm = PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)
    primary = ScriptedTTS([asyncio.CancelledError(), [pcm]])
    fallback = ScriptedTTS([[pcm]])
    adapter = CooldownFallbackTTSAdapter(
        primary=primary,
        fallback=fallback,
        cooldown_seconds=60.0,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(asyncio.CancelledError):
        async for _chunk in adapter.synthesize("Cancelled Fish utterance"):
            pass

    assert [chunk async for chunk in adapter.synthesize("Fish remains primary")] == [
        pcm
    ]
    assert fallback.calls == []


async def test_fish_fallback_closes_both_adapters_exactly_once():
    primary = ScriptedTTS([])
    fallback = ScriptedTTS([])
    adapter = CooldownFallbackTTSAdapter(
        primary=primary,
        fallback=fallback,
        cooldown_seconds=60.0,
    )

    await adapter.close()
    await adapter.close()

    assert primary.close_count == 1
    assert fallback.close_count == 1


async def test_game_companion_factory_selects_fish_with_openrouter_fallback():
    settings = GameCompanionProviderSettings(
        openrouter=OpenRouterProviderSettings(api_key="openrouter-test-secret"),
        tts_provider="fish",
        fish=FishTTSSettings(api_key="fish-test-secret"),
    )

    providers = create_game_companion_provider_set(settings)
    try:
        assert isinstance(providers.tts, CooldownFallbackTTSAdapter)
        assert isinstance(providers.tts.primary, FishTTSAdapter)
        assert isinstance(providers.tts.fallback, OpenRouterTTSAdapter)
        assert providers.tts.cooldown_seconds == 60.0
    finally:
        await providers.close()


def test_provider_factory_wraps_existing_openrouter_services():
    from pipecat.services.openrouter.llm import OpenRouterLLMService

    from api.services.pipecat.service_factory import (
        OpenRouterSTTService,
        OpenRouterTTSService,
    )

    provider_set = create_openrouter_provider_set(
        OpenRouterProviderSettings(api_key="sk-or-v1-test")
    )

    assert isinstance(provider_set.stt.service, OpenRouterSTTService)
    assert isinstance(provider_set.llm.service, OpenRouterLLMService)
    assert isinstance(provider_set.tts.service, OpenRouterTTSService)
    assert provider_set.stt.service._settings.model == (
        "qwen/qwen3-asr-flash-2026-02-10"
    )
    assert provider_set.llm.service._settings.model == "deepseek/deepseek-v4-flash"
    assert provider_set.tts.service._settings.model == "x-ai/grok-voice-tts-1.0"
    assert provider_set.tts.service._settings.voice == "eve"
