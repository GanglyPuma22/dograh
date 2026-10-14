import pytest

from api.services.game_companion.providers import PCMChunk
from api.tools.game_companion_provider_benchmark import (
    FIXED_PROMPTS,
    BenchmarkObservation,
    collect_observations,
    summarize_observations,
)


class RecordingBenchmarkTTS:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    async def synthesize(self, text):
        self.calls.append((self.name, text))
        yield PCMChunk(audio=b"\x01\x00", sample_rate=24000, channels=1)


class StepClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.001
        return self.value


def test_provider_benchmark_uses_fixed_short_medium_and_long_prompts():
    assert tuple(FIXED_PROMPTS) == ("short", "medium", "long")
    assert len(FIXED_PROMPTS["short"]) < len(FIXED_PROMPTS["medium"])
    assert len(FIXED_PROMPTS["medium"]) < len(FIXED_PROMPTS["long"])


async def test_provider_benchmark_runs_five_alternating_observations_per_prompt():
    calls = []
    emitted = []
    adapters = {
        "openrouter": RecordingBenchmarkTTS("openrouter", calls),
        "fish": RecordingBenchmarkTTS("fish", calls),
    }

    observations = await collect_observations(
        adapters,
        runs=5,
        monotonic=StepClock(),
        emit=emitted.append,
    )

    assert len(observations) == 30
    for prompt_name in FIXED_PROMPTS:
        for provider in adapters:
            assert (
                sum(
                    observation.prompt == prompt_name
                    and observation.provider == provider
                    for observation in observations
                )
                == 5
            )
    calls_per_run = len(FIXED_PROMPTS) * len(adapters)
    assert calls[0][0] == "openrouter"
    assert calls[calls_per_run][0] == "fish"
    assert calls[calls_per_run * 2][0] == "openrouter"
    assert all("text" not in record and "audio" not in record for record in emitted)
    assert all(
        set(record)
        == {
            "provider",
            "prompt",
            "prompt_characters",
            "first_pcm_ms",
            "total_ms",
            "pcm_bytes",
        }
        for record in emitted
    )


def test_provider_benchmark_summary_reports_median_and_p95():
    observations = [
        BenchmarkObservation("fish", "short", value, value + 100, 1000 + value)
        for value in (100, 200, 300, 400, 500)
    ]

    summary = summarize_observations(observations)

    assert summary == [
        {
            "provider": "fish",
            "prompt": "short",
            "prompt_characters": len(FIXED_PROMPTS["short"]),
            "runs": 5,
            "first_pcm_ms_median": 300,
            "first_pcm_ms_p95": 480.0,
            "total_ms_median": 400,
            "total_ms_p95": 580.0,
            "pcm_bytes_median": 1300,
            "pcm_bytes_p95": 1480.0,
        }
    ]


@pytest.mark.parametrize(
    "observation",
    [
        BenchmarkObservation("fish", "short", 0, 100, 1000),
        BenchmarkObservation("fish", "short", 200, 100, 1000),
        BenchmarkObservation("fish", "short", 100, 200, 0),
        BenchmarkObservation("unknown", "short", 100, 200, 1000),
        BenchmarkObservation("fish", "unknown", 100, 200, 1000),
    ],
)
def test_provider_benchmark_rejects_invalid_observations(observation):
    with pytest.raises(ValueError, match="benchmark"):
        summarize_observations([observation])
