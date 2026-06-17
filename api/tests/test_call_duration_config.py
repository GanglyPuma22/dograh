import pytest

from api.services.pipecat.call_duration_config import (
    DEFAULT_MAX_CALL_DURATION_SECONDS,
    MAX_CALL_DURATION_ENV_VAR,
    resolve_max_call_duration_seconds,
)


def test_call_duration_uses_workflow_config_when_present(monkeypatch):
    monkeypatch.setenv(MAX_CALL_DURATION_ENV_VAR, "7200")

    assert resolve_max_call_duration_seconds(600) == 600


def test_call_duration_uses_env_when_workflow_config_missing(monkeypatch):
    monkeypatch.setenv(MAX_CALL_DURATION_ENV_VAR, "7200")

    assert resolve_max_call_duration_seconds(None) == 7200


@pytest.mark.parametrize("invalid_value", ["", "   ", "not-a-number", "-1", "0"])
def test_call_duration_uses_default_for_invalid_env(monkeypatch, invalid_value):
    monkeypatch.setenv(MAX_CALL_DURATION_ENV_VAR, invalid_value)

    assert resolve_max_call_duration_seconds(None) == DEFAULT_MAX_CALL_DURATION_SECONDS

