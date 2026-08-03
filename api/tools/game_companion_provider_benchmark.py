"""Compare production OpenRouter and direct Fish companion TTS adapters."""

import asyncio
import json
import math
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from api.services.game_companion.providers import (
    FishTTSAdapter,
    FishTTSSettings,
    OpenRouterProviderSettings,
    TTSAdapter,
    create_openrouter_provider_set,
)
from api.tools.game_companion_tts_benchmark import FIXED_PROMPTS

PROVIDER_ORDER = ("openrouter", "fish")
OBSERVATIONS_PER_PROMPT = 5


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    provider: str
    prompt: str
    first_pcm_ms: float
    total_ms: float
    pcm_bytes: int


def summarize_observations(
    observations: list[BenchmarkObservation],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[BenchmarkObservation]] = defaultdict(list)
    for observation in observations:
        _validate_observation(observation)
        grouped[(observation.provider, observation.prompt)].append(observation)

    provider_order = {name: index for index, name in enumerate(PROVIDER_ORDER)}
    prompt_order = {name: index for index, name in enumerate(FIXED_PROMPTS)}
    summaries = []
    for (provider, prompt), values in sorted(
        grouped.items(),
        key=lambda item: (
            provider_order[item[0][0]],
            prompt_order[item[0][1]],
        ),
    ):
        first_pcm_values = [value.first_pcm_ms for value in values]
        total_values = [value.total_ms for value in values]
        pcm_byte_values = [value.pcm_bytes for value in values]
        summaries.append(
            {
                "provider": provider,
                "prompt": prompt,
                "prompt_characters": len(FIXED_PROMPTS[prompt]),
                "runs": len(values),
                "first_pcm_ms_median": statistics.median(first_pcm_values),
                "first_pcm_ms_p95": _percentile(first_pcm_values, 0.95),
                "total_ms_median": statistics.median(total_values),
                "total_ms_p95": _percentile(total_values, 0.95),
                "pcm_bytes_median": statistics.median(pcm_byte_values),
                "pcm_bytes_p95": _percentile(pcm_byte_values, 0.95),
            }
        )
    return summaries


async def collect_observations(
    adapters: Mapping[str, TTSAdapter],
    *,
    runs: int = OBSERVATIONS_PER_PROMPT,
    monotonic: Callable[[], float] = time.perf_counter,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[BenchmarkObservation]:
    if set(adapters) != set(PROVIDER_ORDER):
        raise ValueError("benchmark requires openrouter and fish adapters")
    if runs < 1 or runs > 10:
        raise ValueError("benchmark runs must be between 1 and 10")

    observations = []
    for run_index in range(runs):
        provider_names = (
            PROVIDER_ORDER if run_index % 2 == 0 else tuple(reversed(PROVIDER_ORDER))
        )
        for prompt_name, text in FIXED_PROMPTS.items():
            for provider_name in provider_names:
                observation = await _measure_adapter(
                    adapters[provider_name],
                    provider_name=provider_name,
                    prompt_name=prompt_name,
                    text=text,
                    monotonic=monotonic,
                )
                observations.append(observation)
                if emit is not None:
                    emit(_safe_observation_record(observation))
    return observations


async def _measure_adapter(
    adapter: TTSAdapter,
    *,
    provider_name: str,
    prompt_name: str,
    text: str,
    monotonic: Callable[[], float],
) -> BenchmarkObservation:
    started_at = monotonic()
    first_pcm_at = None
    pcm_bytes = 0
    async for chunk in adapter.synthesize(text):
        if (
            not isinstance(chunk.audio, bytes)
            or not chunk.audio
            or len(chunk.audio) % 2
            or chunk.sample_rate != 24000
            or chunk.channels != 1
        ):
            raise ValueError("benchmark adapter returned invalid PCM16 audio")
        if first_pcm_at is None:
            first_pcm_at = monotonic()
        pcm_bytes += len(chunk.audio)
    completed_at = monotonic()
    if first_pcm_at is None:
        raise ValueError("benchmark adapter returned no PCM audio")
    observation = BenchmarkObservation(
        provider=provider_name,
        prompt=prompt_name,
        first_pcm_ms=(first_pcm_at - started_at) * 1000,
        total_ms=(completed_at - started_at) * 1000,
        pcm_bytes=pcm_bytes,
    )
    _validate_observation(observation)
    return observation


def _safe_observation_record(observation: BenchmarkObservation) -> dict[str, Any]:
    return {
        **asdict(observation),
        "prompt_characters": len(FIXED_PROMPTS[observation.prompt]),
    }


def _validate_observation(observation: BenchmarkObservation) -> None:
    if observation.provider not in PROVIDER_ORDER:
        raise ValueError("benchmark observation has an unknown provider")
    if observation.prompt not in FIXED_PROMPTS:
        raise ValueError("benchmark observation has an unknown prompt")
    if (
        not math.isfinite(observation.first_pcm_ms)
        or not math.isfinite(observation.total_ms)
        or observation.first_pcm_ms <= 0
        or observation.total_ms <= 0
        or observation.pcm_bytes <= 0
    ):
        raise ValueError("benchmark measurements must be positive and finite")
    if observation.total_ms < observation.first_pcm_ms:
        raise ValueError("benchmark total latency cannot precede first PCM")


def _percentile(values: list[float | int], quantile: float) -> float | int:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("benchmark percentile requires observations")
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return lower + (upper - lower) * (position - lower_index)


async def run_benchmark() -> dict[str, Any]:
    openrouter_settings = OpenRouterProviderSettings.from_env()
    fish_settings = FishTTSSettings.from_env()
    openrouter_providers = create_openrouter_provider_set(openrouter_settings)
    fish_adapter = FishTTSAdapter(fish_settings)
    try:
        observations = await collect_observations(
            {
                "openrouter": openrouter_providers.tts,
                "fish": fish_adapter,
            },
            emit=lambda record: print(json.dumps({"observation": record}), flush=True),
        )
    finally:
        await asyncio.gather(
            openrouter_providers.close(),
            fish_adapter.close(),
            return_exceptions=True,
        )

    return {
        "providers": {
            "openrouter": {
                "model": openrouter_settings.tts_model,
                "voice": openrouter_settings.tts_voice,
            },
            "fish": {
                "model": fish_settings.model,
                "reference_configured": fish_settings.reference_id is not None,
            },
        },
        "runs_per_provider_prompt": OBSERVATIONS_PER_PROMPT,
        "prompt_characters": {
            name: len(prompt) for name, prompt in FIXED_PROMPTS.items()
        },
        "summary": summarize_observations(observations),
    }


def main() -> None:
    result = asyncio.run(run_benchmark())
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
