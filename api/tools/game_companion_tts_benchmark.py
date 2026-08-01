"""Compare the current buffered MP3 TTS path with OpenRouter raw PCM streaming."""

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from api.services.game_companion.providers import (
    OpenRouterProviderSettings,
    OpenRouterTTSAdapter,
    ProviderError,
    create_openrouter_provider_set,
)

FIXED_PROMPTS = {
    "short": "Navigation is ready, and all ship systems are operating normally.",
    "medium": (
        "Navigation is ready, and all ship systems are operating normally. "
        "The nearby moon remains available as a destination. "
        "I will keep monitoring the route while you fly."
    ),
    "long": (
        "Navigation is ready, and all ship systems are operating normally. "
        "The nearby moon remains available as a destination. "
        "Your expedition journal is stored with this save. "
        "I can summarize recent activity when you ask. "
        "I will report any request that cannot be completed. "
        "For now, the route is clear and the companion link is stable."
    ),
}


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    prompt: str
    format: str
    first_pcm_ms: float
    total_ms: float
    pcm_bytes: int


def summarize_observations(
    observations: list[BenchmarkObservation],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[BenchmarkObservation]] = defaultdict(list)
    for observation in observations:
        if (
            observation.first_pcm_ms <= 0
            or observation.total_ms <= 0
            or observation.pcm_bytes <= 0
        ):
            raise ValueError("benchmark measurements must be positive")
        if observation.total_ms < observation.first_pcm_ms:
            raise ValueError("total latency cannot precede first PCM")
        grouped[(observation.prompt, observation.format)].append(observation)

    prompt_order = {name: index for index, name in enumerate(FIXED_PROMPTS)}
    format_order = {"mp3": 0, "pcm": 1}
    summaries = []
    for (prompt, audio_format), values in sorted(
        grouped.items(),
        key=lambda item: (
            prompt_order.get(item[0][0], len(prompt_order)),
            format_order.get(item[0][1], len(format_order)),
        ),
    ):
        summaries.append(
            {
                "prompt": prompt,
                "format": audio_format,
                "runs": len(values),
                "first_pcm_ms_median": statistics.median(
                    value.first_pcm_ms for value in values
                ),
                "total_ms_median": statistics.median(
                    value.total_ms for value in values
                ),
                "pcm_bytes_median": statistics.median(
                    value.pcm_bytes for value in values
                ),
            }
        )
    return summaries


async def _benchmark_buffered_mp3(
    adapter: OpenRouterTTSAdapter,
    prompt_name: str,
    text: str,
) -> BenchmarkObservation:
    started = time.perf_counter()
    first_pcm_at = None
    pcm_bytes = 0
    async for chunk in adapter.synthesize(text):
        if first_pcm_at is None:
            first_pcm_at = time.perf_counter()
        pcm_bytes += len(chunk.audio)
    completed = time.perf_counter()
    if first_pcm_at is None or pcm_bytes == 0:
        raise ProviderError("buffered MP3 benchmark returned no PCM")
    return BenchmarkObservation(
        prompt=prompt_name,
        format="mp3",
        first_pcm_ms=(first_pcm_at - started) * 1000,
        total_ms=(completed - started) * 1000,
        pcm_bytes=pcm_bytes,
    )


async def _benchmark_streaming_pcm(
    adapter: OpenRouterTTSAdapter,
    prompt_name: str,
    text: str,
) -> BenchmarkObservation:
    service = adapter.service
    voice = service._settings.voice
    if voice is None:
        raise ProviderError("OpenRouter TTS voice must be specified")
    params = {
        "input": text,
        "model": service._settings.model,
        "voice": voice,
        "response_format": "pcm",
    }
    if service._settings.instructions:
        params["instructions"] = service._settings.instructions
    if service._settings.speed:
        params["speed"] = service._settings.speed

    started = time.perf_counter()
    first_pcm_at = None
    pcm_bytes = 0
    chunk_size = service.chunk_size or 8192
    async with service._client.audio.speech.with_streaming_response.create(
        **params
    ) as response:
        if response.status_code != 200:
            raise ProviderError(
                f"raw PCM benchmark failed with HTTP {response.status_code}"
            )
        async for chunk in response.iter_bytes(chunk_size):
            if not chunk:
                continue
            if first_pcm_at is None:
                first_pcm_at = time.perf_counter()
            pcm_bytes += len(chunk)
    completed = time.perf_counter()
    if first_pcm_at is None or pcm_bytes == 0:
        raise ProviderError("raw PCM benchmark returned no PCM")
    if pcm_bytes % 2:
        raise ProviderError("raw PCM benchmark returned an incomplete PCM16 sample")
    return BenchmarkObservation(
        prompt=prompt_name,
        format="pcm",
        first_pcm_ms=(first_pcm_at - started) * 1000,
        total_ms=(completed - started) * 1000,
        pcm_bytes=pcm_bytes,
    )


async def run_benchmark(runs: int) -> dict[str, Any]:
    if runs < 1 or runs > 10:
        raise ValueError("runs must be between 1 and 10")
    settings = OpenRouterProviderSettings.from_env()
    providers = create_openrouter_provider_set(settings)
    if not isinstance(providers.tts, OpenRouterTTSAdapter):
        raise TypeError("benchmark requires the OpenRouter TTS adapter")
    observations = []
    try:
        for run_index in range(runs):
            formats = ("mp3", "pcm") if run_index % 2 == 0 else ("pcm", "mp3")
            for prompt_name, prompt in FIXED_PROMPTS.items():
                for audio_format in formats:
                    if audio_format == "mp3":
                        observation = await _benchmark_buffered_mp3(
                            providers.tts, prompt_name, prompt
                        )
                    else:
                        observation = await _benchmark_streaming_pcm(
                            providers.tts, prompt_name, prompt
                        )
                    observations.append(observation)
                    print(json.dumps({"observation": asdict(observation)}), flush=True)
    finally:
        await providers.close()

    return {
        "model": settings.tts_model,
        "voice": settings.tts_voice,
        "runs_per_format": runs,
        "prompt_characters": {
            name: len(prompt) for name, prompt in FIXED_PROMPTS.items()
        },
        "summary": summarize_observations(observations),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    result = asyncio.run(run_benchmark(args.runs))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
