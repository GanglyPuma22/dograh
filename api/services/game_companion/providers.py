"""Provider-neutral interfaces and adapters for Dograh's OpenRouter services."""

import asyncio
import inspect
import json
import math
import os
import time
import uuid
import wave
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

import httpx
from pydantic import JsonValue

DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_STT_MODEL = "qwen/qwen3-asr-flash-2026-02-10"
DEFAULT_TTS_MODEL = "x-ai/grok-voice-tts-1.0"
DEFAULT_TTS_VOICE = "eve"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TTS_PROVIDER = "openrouter"
DEFAULT_FISH_BASE_URL = "https://api.fish.audio"
DEFAULT_FISH_MODEL = "s2.1-pro-free"
DEFAULT_FISH_SAMPLE_RATE = 24000
DEFAULT_FISH_LATENCY = "balanced"
DEFAULT_FISH_CHUNK_LENGTH = 150
DEFAULT_FISH_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_FISH_FALLBACK_COOLDOWN_SECONDS = 60.0
FISH_MODELS = frozenset({"s2.1-pro-free", "s2.1-pro"})
FISH_LATENCY_MODES = frozenset({"low", "balanced", "normal"})
TTS_PROVIDERS = frozenset({"openrouter", "fish"})
MAX_LLM_RESPONSE_BYTES = 64 * 1024
MAX_ANALYSIS_LLM_RESPONSE_BYTES = 16 * 1024
MAX_LLM_TOOL_FRAGMENTS = 256


class ProviderError(RuntimeError):
    """A provider failed or returned a malformed response."""


class ProviderConfigurationError(ValueError):
    """The local provider configuration is incomplete."""


@dataclass(frozen=True, slots=True)
class PCMChunk:
    audio: bytes
    sample_rate: int
    channels: int = 1


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str = ""
    tool_calls: tuple[LLMToolCall, ...] = ()


class STTAdapter(Protocol):
    async def transcribe(self, wav_audio: bytes) -> str: ...


class LLMAdapter(Protocol):
    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult: ...


class TTSAdapter(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[PCMChunk]: ...


@dataclass(frozen=True, slots=True)
class ProviderSet:
    stt: STTAdapter
    llm: LLMAdapter
    tts: TTSAdapter

    async def close(self) -> None:
        await asyncio.gather(
            *(
                _close_async_resource(provider)
                for provider in (self.stt, self.llm, self.tts)
            ),
            return_exceptions=True,
        )


@dataclass(frozen=True, slots=True)
class OpenRouterProviderSettings:
    api_key: str
    llm_model: str = DEFAULT_LLM_MODEL
    stt_model: str = DEFAULT_STT_MODEL
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = DEFAULT_TTS_VOICE
    base_url: str = DEFAULT_OPENROUTER_BASE_URL

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "OpenRouterProviderSettings":
        values = os.environ if environ is None else environ
        api_key = values.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise ProviderConfigurationError("OPENROUTER_API_KEY is required")
        return cls(
            api_key=api_key,
            llm_model=values.get("DOGRAH_GAME_COMPANION_LLM_MODEL", DEFAULT_LLM_MODEL),
            stt_model=values.get("DOGRAH_GAME_COMPANION_STT_MODEL", DEFAULT_STT_MODEL),
            tts_model=values.get("DOGRAH_GAME_COMPANION_TTS_MODEL", DEFAULT_TTS_MODEL),
            tts_voice=values.get("DOGRAH_GAME_COMPANION_TTS_VOICE", DEFAULT_TTS_VOICE),
            base_url=values.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
        )


@dataclass(frozen=True, slots=True)
class FishTTSSettings:
    api_key: str
    base_url: str = DEFAULT_FISH_BASE_URL
    model: str = DEFAULT_FISH_MODEL
    reference_id: str | None = None
    sample_rate: int = DEFAULT_FISH_SAMPLE_RATE
    latency: str = DEFAULT_FISH_LATENCY
    chunk_length: int = DEFAULT_FISH_CHUNK_LENGTH
    request_timeout_seconds: float = DEFAULT_FISH_REQUEST_TIMEOUT_SECONDS
    fallback_cooldown_seconds: float = DEFAULT_FISH_FALLBACK_COOLDOWN_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FishTTSSettings":
        values = os.environ if environ is None else environ
        api_key = values.get("FISH_API_KEY", "").strip()
        if not api_key:
            raise ProviderConfigurationError(
                "FISH_API_KEY is required when Fish TTS is selected"
            )

        base_url = values.get(
            "DOGRAH_GAME_COMPANION_FISH_BASE_URL", DEFAULT_FISH_BASE_URL
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ProviderConfigurationError("Fish base URL must use http or https")

        model = values.get(
            "DOGRAH_GAME_COMPANION_FISH_MODEL", DEFAULT_FISH_MODEL
        ).strip()
        if model not in FISH_MODELS:
            raise ProviderConfigurationError(
                "Fish model must be s2.1-pro-free or s2.1-pro"
            )

        sample_rate = _bounded_env_int(
            values,
            "DOGRAH_GAME_COMPANION_FISH_SAMPLE_RATE",
            DEFAULT_FISH_SAMPLE_RATE,
            label="Fish sample rate",
            minimum=DEFAULT_FISH_SAMPLE_RATE,
            maximum=DEFAULT_FISH_SAMPLE_RATE,
        )
        latency = values.get(
            "DOGRAH_GAME_COMPANION_FISH_LATENCY", DEFAULT_FISH_LATENCY
        ).strip()
        if latency not in FISH_LATENCY_MODES:
            raise ProviderConfigurationError(
                "Fish latency must be low, balanced, or normal"
            )
        chunk_length = _bounded_env_int(
            values,
            "DOGRAH_GAME_COMPANION_FISH_CHUNK_LENGTH",
            DEFAULT_FISH_CHUNK_LENGTH,
            label="Fish chunk length",
            minimum=100,
            maximum=300,
        )
        request_timeout_seconds = _bounded_env_float(
            values,
            "DOGRAH_GAME_COMPANION_FISH_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_FISH_REQUEST_TIMEOUT_SECONDS,
            label="Fish request timeout",
            minimum=1.0,
            maximum=120.0,
        )
        fallback_cooldown_seconds = _bounded_env_float(
            values,
            "DOGRAH_GAME_COMPANION_FISH_FALLBACK_COOLDOWN_SECONDS",
            DEFAULT_FISH_FALLBACK_COOLDOWN_SECONDS,
            label="Fish fallback cooldown",
            minimum=1.0,
            maximum=600.0,
        )
        reference_id = (
            values.get("DOGRAH_GAME_COMPANION_FISH_REFERENCE_ID", "").strip() or None
        )
        return cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            reference_id=reference_id,
            sample_rate=sample_rate,
            latency=latency,
            chunk_length=chunk_length,
            request_timeout_seconds=request_timeout_seconds,
            fallback_cooldown_seconds=fallback_cooldown_seconds,
        )


@dataclass(frozen=True, slots=True)
class GameCompanionProviderSettings:
    openrouter: OpenRouterProviderSettings
    tts_provider: str = DEFAULT_TTS_PROVIDER
    fish: FishTTSSettings | None = None

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "GameCompanionProviderSettings":
        values = os.environ if environ is None else environ
        openrouter = OpenRouterProviderSettings.from_env(values)
        tts_provider = (
            values.get("DOGRAH_GAME_COMPANION_TTS_PROVIDER", DEFAULT_TTS_PROVIDER)
            .strip()
            .lower()
        )
        if tts_provider not in TTS_PROVIDERS:
            raise ProviderConfigurationError(
                "game companion TTS provider must be openrouter or fish"
            )
        fish = FishTTSSettings.from_env(values) if tts_provider == "fish" else None
        return cls(
            openrouter=openrouter,
            tts_provider=tts_provider,
            fish=fish,
        )


def _bounded_env_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ProviderConfigurationError(f"{label} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ProviderConfigurationError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_env_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ProviderConfigurationError(f"{label} must be a number") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ProviderConfigurationError(
            f"{label} must be between {minimum:g} and {maximum:g} seconds"
        )
    return value


def pcm_s16le_to_wav(
    pcm_audio: bytes,
    *,
    sample_rate: int,
    channels: int,
) -> bytes:
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError("sample_rate must be between 8000 and 48000")
    if channels != 1:
        raise ValueError("game companion input must be mono")
    if len(pcm_audio) % (2 * channels):
        raise ValueError("PCM16 audio must contain complete samples")

    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_audio)
    return output.getvalue()


class OpenRouterSTTAdapter:
    def __init__(self, service: Any):
        self.service = service

    async def transcribe(self, wav_audio: bytes) -> str:
        response = await self.service._transcribe(wav_audio)
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenRouter STT returned no transcript")
        return text.strip()

    async def close(self) -> None:
        await _close_async_resource(self.service._client)


class OpenRouterLLMAdapter:
    def __init__(self, service: Any):
        self.service = service

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        from openai import NOT_GIVEN

        invocation_params = {
            "messages": [dict(message) for message in messages],
            "tools": tools if tools else NOT_GIVEN,
            "tool_choice": NOT_GIVEN,
        }
        params = self.service.build_chat_completion_params(invocation_params)
        stream = await self.service._client.chat.completions.create(**params)
        text_parts: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        response_bytes = 0
        tool_fragment_count = 0
        response_limit = (
            MAX_ANALYSIS_LLM_RESPONSE_BYTES if not tools else MAX_LLM_RESPONSE_BYTES
        )
        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    response_bytes += _utf8_size(content)
                    _require_response_within_limit(response_bytes, response_limit)
                    text_parts.append(content)
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    tool_fragment_count += 1
                    if tool_fragment_count > MAX_LLM_TOOL_FRAGMENTS:
                        raise ProviderError(
                            "OpenRouter LLM returned too many tool fragments"
                        )
                    index = getattr(tool_call, "index", None)
                    if type(index) is not int or index < 0:
                        raise ProviderError(
                            "OpenRouter LLM returned an invalid tool fragment index"
                        )
                    fragment = tool_fragments.setdefault(
                        index,
                        {"call_id": "", "name": "", "arguments": []},
                    )
                    if getattr(tool_call, "id", None):
                        call_id = tool_call.id
                        response_bytes += _utf8_size(call_id)
                        _require_response_within_limit(response_bytes, response_limit)
                        fragment["call_id"] += call_id
                    function = getattr(tool_call, "function", None)
                    if function is None:
                        continue
                    if getattr(function, "name", None):
                        name = function.name
                        response_bytes += _utf8_size(name)
                        _require_response_within_limit(response_bytes, response_limit)
                        fragment["name"] += name
                    if getattr(function, "arguments", None):
                        arguments = function.arguments
                        response_bytes += _utf8_size(arguments)
                        _require_response_within_limit(response_bytes, response_limit)
                        fragment["arguments"].append(arguments)
        finally:
            await _close_async_resource(stream)

        tool_calls = tuple(
            _parse_tool_call(fragment) for _, fragment in sorted(tool_fragments.items())
        )
        return LLMResult(text="".join(text_parts).strip(), tool_calls=tool_calls)

    async def close(self) -> None:
        await _close_async_resource(self.service._client)


class OpenRouterTTSAdapter:
    def __init__(self, service: Any):
        self.service = service

    async def synthesize(self, text: str) -> AsyncIterator[PCMChunk]:
        frames = self.service.run_tts(text, f"game-companion-{uuid.uuid4()}")
        try:
            async for frame in frames:
                error = getattr(frame, "error", None)
                if error:
                    raise ProviderError(str(error))
                audio = getattr(frame, "audio", None)
                sample_rate = getattr(frame, "sample_rate", None)
                channels = getattr(frame, "num_channels", None)
                if not isinstance(audio, bytes) or not audio:
                    raise ProviderError("OpenRouter TTS returned a non-audio frame")
                if not isinstance(sample_rate, int) or not isinstance(channels, int):
                    raise ProviderError("OpenRouter TTS returned invalid PCM metadata")
                yield PCMChunk(
                    audio=audio,
                    sample_rate=sample_rate,
                    channels=channels,
                )
        finally:
            await _close_async_resource(frames)

    async def close(self) -> None:
        await _close_async_resource(self.service._client)


class FishTTSAdapter:
    def __init__(self, settings: FishTTSSettings, *, client: Any | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds
        )
        self._closed = False

    async def synthesize(self, text: str) -> AsyncIterator[PCMChunk]:
        payload: dict[str, JsonValue] = {
            "text": text,
            "format": "pcm",
            "sample_rate": self.settings.sample_rate,
            "latency": self.settings.latency,
            "chunk_length": self.settings.chunk_length,
            "normalize": True,
        }
        if self.settings.reference_id is not None:
            payload["reference_id"] = self.settings.reference_id

        try:
            async with self._client.stream(
                "POST",
                f"{self.settings.base_url}/v1/tts",
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "model": self.settings.model,
                    "Accept": "application/octet-stream",
                },
                json=payload,
                timeout=self.settings.request_timeout_seconds,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ProviderError(
                        f"Fish TTS request failed with status {response.status_code}"
                    )

                trailing_byte = b""
                emitted_audio = False
                async for http_chunk in response.aiter_bytes():
                    if not http_chunk:
                        continue
                    audio = trailing_byte + bytes(http_chunk)
                    complete_length = len(audio) - (len(audio) % 2)
                    trailing_byte = audio[complete_length:]
                    if complete_length == 0:
                        continue
                    emitted_audio = True
                    yield PCMChunk(
                        audio=audio[:complete_length],
                        sample_rate=self.settings.sample_rate,
                        channels=1,
                    )

                if trailing_byte:
                    raise ProviderError("Fish TTS returned an incomplete PCM sample")
                if not emitted_audio:
                    raise ProviderError("Fish TTS returned no audio")
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("Fish TTS request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Fish TTS request failed") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_async_resource(self._client)


class CooldownFallbackTTSAdapter:
    def __init__(
        self,
        *,
        primary: TTSAdapter,
        fallback: TTSAdapter,
        cooldown_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.primary = primary
        self.fallback = fallback
        self.cooldown_seconds = cooldown_seconds
        self._monotonic = monotonic
        self._fallback_until = 0.0
        self._closed = False

    async def synthesize(self, text: str) -> AsyncIterator[PCMChunk]:
        use_fallback = self._monotonic() < self._fallback_until
        provider = self.fallback if use_fallback else self.primary
        try:
            async for chunk in provider.synthesize(text):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            if not use_fallback:
                self._fallback_until = self._monotonic() + self.cooldown_seconds
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            _close_async_resource(self.primary),
            _close_async_resource(self.fallback),
            return_exceptions=True,
        )


def _parse_tool_call(fragment: dict[str, Any]) -> LLMToolCall:
    call_id = fragment["call_id"]
    name = fragment["name"]
    if not call_id or not name:
        raise ProviderError("OpenRouter LLM returned an incomplete tool call")
    raw_arguments = "".join(fragment["arguments"]) or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ProviderError("OpenRouter LLM returned invalid tool arguments") from exc
    if not isinstance(arguments, dict):
        raise ProviderError("OpenRouter LLM tool arguments must be an object")
    return LLMToolCall(call_id=call_id, name=name, arguments=arguments)


def _utf8_size(value: Any) -> int:
    if not isinstance(value, str):
        raise ProviderError("OpenRouter LLM returned a non-text response fragment")
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProviderError("OpenRouter LLM returned invalid UTF-8 text") from exc


def _require_response_within_limit(size: int, limit: int) -> None:
    if size > limit:
        raise ProviderError("OpenRouter LLM response is too large")


async def _close_async_resource(resource: Any) -> None:
    close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def create_openrouter_provider_set(
    settings: OpenRouterProviderSettings | None = None,
) -> ProviderSet:
    if settings is None:
        settings = OpenRouterProviderSettings.from_env()

    from api.services.configuration.registry import ServiceProviders
    from api.services.pipecat.service_factory import (
        OpenRouterSTTService,
        OpenRouterTTSService,
        create_llm_service_from_provider,
    )

    stt_service = OpenRouterSTTService(
        api_key=settings.api_key,
        base_url=settings.base_url,
        settings=OpenRouterSTTService.Settings(model=settings.stt_model),
    )
    llm_service = create_llm_service_from_provider(
        ServiceProviders.OPENROUTER.value,
        settings.llm_model,
        settings.api_key,
        base_url=settings.base_url,
    )
    tts_service = OpenRouterTTSService(
        api_key=settings.api_key,
        base_url=settings.base_url,
        settings=OpenRouterTTSService.Settings(
            model=settings.tts_model,
            voice=settings.tts_voice,
            speed=1.0,
        ),
    )
    return ProviderSet(
        stt=OpenRouterSTTAdapter(stt_service),
        llm=OpenRouterLLMAdapter(llm_service),
        tts=OpenRouterTTSAdapter(tts_service),
    )


def create_game_companion_provider_set(
    settings: GameCompanionProviderSettings | None = None,
) -> ProviderSet:
    if settings is None:
        settings = GameCompanionProviderSettings.from_env()

    openrouter = create_openrouter_provider_set(settings.openrouter)
    if settings.tts_provider == "openrouter":
        return openrouter
    if settings.tts_provider != "fish" or settings.fish is None:
        raise ProviderConfigurationError("Fish TTS settings are required")

    fish = FishTTSAdapter(settings.fish)
    return ProviderSet(
        stt=openrouter.stt,
        llm=openrouter.llm,
        tts=CooldownFallbackTTSAdapter(
            primary=fish,
            fallback=openrouter.tts,
            cooldown_seconds=settings.fish.fallback_cooldown_seconds,
        ),
    )
