from types import SimpleNamespace

import pytest
from openai.types.audio import Transcription
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

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
async def test_openrouter_tts_streams_pcm_before_response_completes(monkeypatch):
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

        def __init__(self):
            self.finished = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_bytes(self, chunk_size):
            yield b"\x01\x02"
            yield b"\x03"
            yield b"\x04"
            self.finished = True

    response = FakeResponse()

    def fake_create(**kwargs):
        calls.append(kwargs)
        return response

    service._client = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(
                with_streaming_response=SimpleNamespace(create=fake_create)
            )
        )
    )
    frames = service.run_tts(
        "OpenRouter audio smoke test.",
        "ctx-openrouter",
    )
    first_frame = await anext(frames)

    assert calls[0]["model"] == "x-ai/grok-voice-tts-1.0"
    assert calls[0]["voice"] == "default"
    assert calls[0]["response_format"] == "pcm"
    assert response.finished is False
    assert isinstance(first_frame, TTSAudioRawFrame)
    assert first_frame.audio == b"\x01\x02"
    assert first_frame.sample_rate == 24000

    remaining_frames = [frame async for frame in frames]

    assert response.finished is True
    assert [frame.audio for frame in remaining_frames] == [b"\x03\x04"]


@pytest.mark.asyncio
async def test_openrouter_tts_resamples_provider_pcm_to_pipeline_rate(monkeypatch):
    service = OpenRouterTTSService(
        api_key="sk-or-v1-test",
        base_url="https://openrouter.ai/api/v1",
        sample_rate=8000,
        settings=OpenRouterTTSService.Settings(
            model="x-ai/grok-voice-tts-1.0",
            voice="default",
        ),
    )
    service._sample_rate = 8000
    calls = []

    class FakeResampler:
        async def resample(self, audio, in_rate, out_rate):
            calls.append((audio, in_rate, out_rate))
            return b"\x09\x00"

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_bytes(self, chunk_size):
            yield b"\x01\x00\x02\x00"

    monkeypatch.setattr(
        "api.services.pipecat.service_factory.create_stream_resampler",
        lambda: FakeResampler(),
        raising=False,
    )
    service._client = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(
                with_streaming_response=SimpleNamespace(
                    create=lambda **kwargs: FakeResponse()
                )
            )
        )
    )

    frames = [frame async for frame in service.run_tts("hello", "ctx")]

    assert calls == [(b"\x01\x00\x02\x00", 24000, 8000)]
    assert len(frames) == 1
    assert frames[0].audio == b"\x09\x00"
    assert frames[0].sample_rate == 8000


@pytest.mark.asyncio
async def test_openrouter_tts_flushes_real_stream_resampler_tail():
    service = OpenRouterTTSService(
        api_key="sk-or-v1-test",
        base_url="https://openrouter.ai/api/v1",
        sample_rate=8000,
        settings=OpenRouterTTSService.Settings(
            model="x-ai/grok-voice-tts-1.0",
            voice="default",
        ),
    )
    service._sample_rate = 8000
    provider_audio = b"\x01\x00" * 24000

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_bytes(self, chunk_size):
            yield provider_audio

    service._client = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(
                with_streaming_response=SimpleNamespace(
                    create=lambda **kwargs: FakeResponse()
                )
            )
        )
    )

    frames = [frame async for frame in service.run_tts("hello", "ctx")]
    audio = b"".join(
        frame.audio for frame in frames if isinstance(frame, TTSAudioRawFrame)
    )

    assert len(audio) == 16000
    assert audio[-64:] != b"\x00" * 64
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert all(
        frame.sample_rate == 8000
        for frame in frames
        if isinstance(frame, TTSAudioRawFrame)
    )
