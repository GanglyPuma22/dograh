import wave
from io import BytesIO
from types import SimpleNamespace

import pytest

from api.services.game_companion.providers import (
    FishTTSSettings,
    GameCompanionProviderSettings,
    OpenRouterLLMAdapter,
    OpenRouterProviderSettings,
    OpenRouterSTTAdapter,
    OpenRouterTTSAdapter,
    ProviderConfigurationError,
    ProviderError,
    ProviderSet,
    create_openrouter_provider_set,
    pcm_s16le_to_wav,
)


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


async def test_llm_adapter_assembles_streamed_text_and_tool_arguments():
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
                    id=None,
                    function=SimpleNamespace(name=None, arguments='01_moon"}'),
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
            "121",
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
