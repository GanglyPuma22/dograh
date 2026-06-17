import os
from typing import Optional

from loguru import logger


DEFAULT_MAX_CALL_DURATION_SECONDS = 300
MAX_CALL_DURATION_ENV_VAR = "DOGRAH_MAX_CALL_DURATION_SECONDS"


def resolve_max_call_duration_seconds(configured_value: Optional[int]) -> int:
    if configured_value is not None:
        return int(configured_value)

    raw_value = os.getenv(MAX_CALL_DURATION_ENV_VAR)
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_MAX_CALL_DURATION_SECONDS

    try:
        resolved_value = int(float(raw_value))
    except ValueError:
        logger.warning(
            f"Invalid {MAX_CALL_DURATION_ENV_VAR}={raw_value!r}; "
            f"using default {DEFAULT_MAX_CALL_DURATION_SECONDS}s"
        )
        return DEFAULT_MAX_CALL_DURATION_SECONDS

    if resolved_value <= 0:
        logger.warning(
            f"Invalid {MAX_CALL_DURATION_ENV_VAR}={raw_value!r}; "
            f"using default {DEFAULT_MAX_CALL_DURATION_SECONDS}s"
        )
        return DEFAULT_MAX_CALL_DURATION_SECONDS

    return resolved_value

