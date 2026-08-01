import pytest

from api.tools.game_companion_tts_benchmark import (
    FIXED_PROMPTS,
    BenchmarkObservation,
    summarize_observations,
)


def test_benchmark_uses_fixed_short_medium_and_long_prompts():
    assert tuple(FIXED_PROMPTS) == ("short", "medium", "long")
    assert len(FIXED_PROMPTS["short"]) < len(FIXED_PROMPTS["medium"])
    assert len(FIXED_PROMPTS["medium"]) < len(FIXED_PROMPTS["long"])


def test_summary_reports_first_pcm_and_total_latency_without_content():
    observations = [
        BenchmarkObservation("short", "mp3", 100, 500, 48000),
        BenchmarkObservation("short", "mp3", 200, 700, 50000),
        BenchmarkObservation("short", "pcm", 40, 400, 46000),
        BenchmarkObservation("short", "pcm", 60, 440, 47000),
    ]

    summary = summarize_observations(observations)

    assert summary == [
        {
            "prompt": "short",
            "format": "mp3",
            "runs": 2,
            "first_pcm_ms_median": 150.0,
            "total_ms_median": 600.0,
            "pcm_bytes_median": 49000.0,
        },
        {
            "prompt": "short",
            "format": "pcm",
            "runs": 2,
            "first_pcm_ms_median": 50.0,
            "total_ms_median": 420.0,
            "pcm_bytes_median": 46500.0,
        },
    ]
    assert all("text" not in row and "audio" not in row for row in summary)


def test_summary_rejects_incomplete_observations():
    with pytest.raises(ValueError, match="positive"):
        summarize_observations([BenchmarkObservation("short", "pcm", 0, 100, 1000)])
