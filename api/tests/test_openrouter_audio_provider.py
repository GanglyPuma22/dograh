from types import SimpleNamespace

import pytest
from openai.types.audio import Transcription
from pipecat.frames.frames import TTSAudioRawFrame

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    REGISTRY,
    OpenRouterSTTConfiguration,
    OpenRouterTTSConfiguration,
    ServiceProviders,
    ServiceType,
)
from api.services.pipecat.service_factory import (
    OpenRouterSTTService,
    OpenRouterTTSService,
    create_stt_service,
    create_tts_service,
)


def test_openrouter_tts_registered_with_openrouter_defaults():
    assert ServiceProviders.OPENROUTER in REGISTRY[ServiceType.TTS]
    assert (
        REGISTRY[ServiceType.TTS][ServiceProviders.OPENROUTER]
        is OpenRouterTTSConfiguration
    )

    cfg = OpenRouterTTSConfiguration(api_key="sk-or-v1-test")

    assert cfg.provider == ServiceProviders.OPENROUTER
    assert cfg.model == "x-ai/grok-voice-tts-1.0"
    assert cfg.voice == "default"
    assert cfg.speed == 1.0
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_openrouter_stt_registered_with_openrouter_defaults():
    assert ServiceProviders.OPENROUTER in REGISTRY[ServiceType.STT]
    assert (
        REGISTRY[ServiceType.STT][ServiceProviders.OPENROUTER]
        is OpenRouterSTTConfiguration
    )

    cfg = OpenRouterSTTConfiguration(api_key="sk-or-v1-test")

    assert cfg.provider == ServiceProviders.OPENROUTER
    assert cfg.model == "qwen/qwen3-asr-flash-2026-02-10"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_openrouter_api_key_validation_accepts_openrouter_key_for_audio():
    validator = UserConfigurationValidator()

    assert validator._check_openrouter_api_key("tts", "sk-or-v1-test") is True
    assert validator._check_openrouter_api_key("stt", "sk-or-v1-test") is True


def test_create_openrouter_stt_service_uses_openrouter_base_url():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.OPENROUTER.value,
            api_key="sk-or-v1-test",
            model="qwen/qwen3-asr-flash-2026-02-10",
            base_url="https://openrouter.ai/api/v1",
        )
    )

    service = create_stt_service(user_config, audio_config=None)

    assert isinstance(service, OpenRouterSTTService)
    assert service._settings.model == "qwen/qwen3-asr-flash-2026-02-10"


@pytest.mark.asyncio
async def test_openrouter_stt_transcribe_posts_json_input_audio_body():
    service = OpenRouterSTTService(
        api_key="sk-or-v1-test",
        settings=OpenRouterSTTService.Settings(
            model="qwen/qwen3-asr-flash-2026-02-10",
        ),
        base_url="https://openrouter.ai/api/v1",
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, path, *, cast_to, body):
            self.calls.append({"path": path, "cast_to": cast_to, "body": body})
            return Transcription(text="hello")

    fake_client = FakeClient()
    service._client = fake_client

    response = await service._transcribe(b"wav-bytes")

    assert response.text == "hello"
    assert fake_client.calls == [
        {
            "path": "/audio/transcriptions",
            "cast_to": Transcription,
            "body": {
                "model": "qwen/qwen3-asr-flash-2026-02-10",
                "input_audio": {
                    "data": "d2F2LWJ5dGVz",
                    "format": "wav",
                },
                "language": "en",
            },
        }
    ]


def test_create_openrouter_tts_service_uses_openrouter_base_url_and_speed():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.OPENROUTER.value,
            api_key="sk-or-v1-test",
            model="x-ai/grok-voice-tts-1.0",
            voice="default",
            speed=1.1,
            base_url="https://openrouter.ai/api/v1",
        )
    )

    service = create_tts_service(user_config, audio_config=None)

    assert isinstance(service, OpenRouterTTSService)
    assert service._settings.model == "x-ai/grok-voice-tts-1.0"
    assert service._settings.voice == "default"
    assert service._settings.speed == 1.1


@pytest.mark.asyncio
async def test_openrouter_tts_requests_mp3_and_yields_pcm(monkeypatch):
    service = OpenRouterTTSService(
        api_key="sk-or-v1-test",
        base_url="https://openrouter.ai/api/v1",
        settings=OpenRouterTTSService.Settings(
            model="x-ai/grok-voice-tts-1.0",
            voice="default",
            speed=1.0,
        ),
    )

    calls = []

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_bytes(self, chunk_size):
            yield b"mp3-bytes"

    def fake_create(**kwargs):
        calls.append(kwargs)
        return FakeResponse()

    async def fake_convert(audio, target_sample_rate):
        calls.append({"audio": audio, "target_sample_rate": target_sample_rate})
        return b"\x01\x02\x03\x04"

    service._client = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(
                with_streaming_response=SimpleNamespace(create=fake_create)
            )
        )
    )
    monkeypatch.setattr(
        "api.services.pipecat.service_factory._convert_audio_bytes_to_pcm",
        fake_convert,
    )

    frames = [
        frame
        async for frame in service.run_tts(
            "OpenRouter audio smoke test.",
            "ctx-openrouter",
        )
    ]

    assert calls[0]["model"] == "x-ai/grok-voice-tts-1.0"
    assert calls[0]["voice"] == "default"
    assert calls[0]["response_format"] == "mp3"
    assert calls[1] == {
        "audio": b"mp3-bytes",
        "target_sample_rate": 24000,
    }
    assert len(frames) == 1
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert frames[0].audio == b"\x01\x02\x03\x04"
    assert frames[0].sample_rate == 24000
