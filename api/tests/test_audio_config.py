import pytest

from api.services.pipecat.audio_config import AudioConfig


def make_audio_config() -> AudioConfig:
    return AudioConfig(
        transport_in_sample_rate=16000,
        transport_out_sample_rate=16000,
    )


def test_audio_config_uses_default_max_recording_duration_when_env_missing(
    monkeypatch,
):
    monkeypatch.delenv("DOGRAH_MAX_RECORDING_DURATION_SECONDS", raising=False)

    config = make_audio_config()

    assert config.max_recording_duration_seconds == 300.0


def test_audio_config_uses_max_recording_duration_from_env(monkeypatch):
    monkeypatch.setenv("DOGRAH_MAX_RECORDING_DURATION_SECONDS", "7200")

    config = make_audio_config()

    assert config.max_recording_duration_seconds == 7200.0


def test_audio_config_uses_default_for_blank_max_recording_duration(monkeypatch):
    monkeypatch.setenv("DOGRAH_MAX_RECORDING_DURATION_SECONDS", "   ")

    config = make_audio_config()

    assert config.max_recording_duration_seconds == 300.0


@pytest.mark.parametrize("invalid_value", ["-5", "not-a-number"])
def test_audio_config_uses_default_for_invalid_max_recording_duration(
    monkeypatch,
    invalid_value,
):
    monkeypatch.setenv("DOGRAH_MAX_RECORDING_DURATION_SECONDS", invalid_value)

    config = make_audio_config()

    assert config.max_recording_duration_seconds == 300.0
