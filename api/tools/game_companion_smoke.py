"""Provider-free WebSocket smoke server for the Salvage companion protocol."""

import argparse
import asyncio
import json
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, NoReturn

import uvicorn
from fastapi import FastAPI, WebSocket

from api.routes.game_companion import serve_game_companion
from api.services.game_companion.providers import (
    LLMResult,
    LLMToolCall,
    PCMChunk,
    ProviderError,
    ProviderSet,
)
from api.services.game_companion.session import (
    CompanionSession,
    EmitCallback,
)

INTERRUPT_TURN_ID = "smoke-interrupt"
AUTHORITY_TURN_ID = "smoke-authority"
NAVIGATION_CALL_ID = "smoke-nav-call"
MEMORY_QUERY_ID = "smoke-memory-query"
BODY_ID = "planet_01_moon"

INTERRUPT_INPUT_CHUNKS = (bytes.fromhex("0100"), bytes.fromhex("0200"))
AUTHORITY_INPUT_CHUNKS = (bytes.fromhex("0300"), bytes.fromhex("0400"))
INTERRUPT_OUTPUT_CHUNK = bytes.fromhex("11001200")
AUTHORITY_OUTPUT_CHUNKS = (
    bytes.fromhex("21002200"),
    bytes.fromhex("23002400"),
)

INTERRUPT_PLAYER_CAPTION = "interrupt me"
AUTHORITY_PLAYER_CAPTION = "set course and recall"
INTERRUPT_ASSISTANT_CAPTION = "This reply should stop after the first audio chunk."
FINAL_ASSISTANT_CAPTION = "Course set using one grounded activity record."

SMOKE_MEMORY_RECORD: dict[str, Any] = {
    "schema_version": 1,
    "event_id": "smoke-event-1",
    "event_type": "navigation_target_changed",
    "occurred_at_utc": "2026-08-01T00:00:00Z",
    "game_time": 42.0,
    "body_id": BODY_ID,
    "importance": "normal",
    "status": "completed",
    "summary": "Navigation target changed to the starting moon.",
}


@dataclass(slots=True)
class SmokeCoordinator:
    """Record bounded pass/fail evidence without retaining audio or transcripts."""

    sessions_created: int = 0
    sessions_closed: int = 0
    stt_turns: int = 0
    interrupted_tts_cancelled: bool = False
    tool_result_validated: bool = False
    memory_result_validated: bool = False
    provider_close_counts: dict[str, int] = field(
        default_factory=lambda: {"stt": 0, "llm": 0, "tts": 0}
    )
    failures: list[str] = field(default_factory=list)
    _interrupt_requested: bool = field(default=False, repr=False)
    _validated_pcm_turns: set[int] = field(default_factory=set, repr=False)

    @property
    def ordered_pcm_validated(self) -> bool:
        return self._validated_pcm_turns == {0, 1}

    def record_failure(self, code: str) -> None:
        if code not in self.failures:
            self.failures.append(code)

    def create_session(self, emit: EmitCallback) -> CompanionSession:
        self.sessions_created += 1
        providers = ProviderSet(
            stt=_SmokeSTT(self),
            llm=_SmokeLLM(self),
            tts=_SmokeTTS(self),
        )
        return _TrackingCompanionSession(
            coordinator=self,
            providers=providers,
            emit=emit,
            annotation_cooldown_seconds=0.0,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions_created": self.sessions_created,
            "sessions_closed": self.sessions_closed,
            "stt_turns": self.stt_turns,
            "ordered_pcm_validated": self.ordered_pcm_validated,
            "interrupted_tts_cancelled": self.interrupted_tts_cancelled,
            "tool_result_validated": self.tool_result_validated,
            "memory_result_validated": self.memory_result_validated,
            "provider_close_counts": dict(self.provider_close_counts),
            "failures": list(self.failures),
        }


class _TrackingCompanionSession(CompanionSession):
    def __init__(self, *, coordinator: SmokeCoordinator, **kwargs: Any):
        super().__init__(**kwargs)
        self._smoke_coordinator = coordinator
        self._smoke_close_recorded = False

    async def interrupt(self, turn_id: str) -> None:
        self._smoke_coordinator._interrupt_requested = True
        await super().interrupt(turn_id)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if not self._smoke_close_recorded:
                self._smoke_close_recorded = True
                self._smoke_coordinator.sessions_closed += 1


class _SmokeProvider:
    def __init__(self, coordinator: SmokeCoordinator, provider_name: str):
        self.coordinator = coordinator
        self.provider_name = provider_name
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.coordinator.provider_close_counts[self.provider_name] += 1

    def fail(self, code: str) -> NoReturn:
        self.coordinator.record_failure(code)
        raise ProviderError(f"provider-free smoke failure: {code}")


class _SmokeSTT(_SmokeProvider):
    def __init__(self, coordinator: SmokeCoordinator):
        super().__init__(coordinator, "stt")

    async def transcribe(self, wav_audio: bytes) -> str:
        turn_index = self.coordinator.stt_turns
        self.coordinator.stt_turns += 1
        expected_turns = (
            (b"".join(INTERRUPT_INPUT_CHUNKS), INTERRUPT_PLAYER_CAPTION),
            (b"".join(AUTHORITY_INPUT_CHUNKS), AUTHORITY_PLAYER_CAPTION),
        )
        if turn_index >= len(expected_turns):
            self.fail("unexpected_stt_turn")
        expected_audio, transcript = expected_turns[turn_index]

        try:
            with wave.open(BytesIO(wav_audio), "rb") as wav_file:
                valid_format = (
                    wav_file.getframerate() == 16000
                    and wav_file.getnchannels() == 1
                    and wav_file.getsampwidth() == 2
                    and wav_file.getcomptype() == "NONE"
                )
                pcm_audio = wav_file.readframes(wav_file.getnframes())
        except (EOFError, OSError, wave.Error):
            self.fail("invalid_input_wav")
        if not valid_format:
            self.fail("invalid_input_wav_format")
        if pcm_audio != expected_audio:
            self.fail(f"pcm_order_mismatch_turn_{turn_index + 1}")

        self.coordinator._validated_pcm_turns.add(turn_index)
        return transcript


class _SmokeLLM(_SmokeProvider):
    def __init__(self, coordinator: SmokeCoordinator):
        super().__init__(coordinator, "llm")

    async def respond(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        if not tools:
            return LLMResult(text="NO_ANALYSIS")

        tool_messages = [
            message for message in messages if message.get("role") == "tool"
        ]
        if tool_messages:
            self._validate_action_results(tool_messages)
            return LLMResult(text=FINAL_ASSISTANT_CAPTION)

        player_prompt = messages[-1].get("content") if messages else None
        if not isinstance(player_prompt, str):
            self.fail("missing_player_prompt")
        if player_prompt.endswith(f"Player speech:\n{INTERRUPT_PLAYER_CAPTION}"):
            return LLMResult(text=INTERRUPT_ASSISTANT_CAPTION)
        if player_prompt.endswith(f"Player speech:\n{AUTHORITY_PLAYER_CAPTION}"):
            return LLMResult(
                tool_calls=(
                    LLMToolCall(
                        call_id=NAVIGATION_CALL_ID,
                        name="set_navigation_target",
                        arguments={"body_id": BODY_ID},
                    ),
                    LLMToolCall(
                        call_id=MEMORY_QUERY_ID,
                        name="recent_activity",
                        arguments={"limit": 1},
                    ),
                )
            )
        self.fail("unexpected_player_prompt")

    def _validate_action_results(self, messages: Sequence[dict]) -> None:
        if [message.get("tool_call_id") for message in messages] != [
            NAVIGATION_CALL_ID,
            MEMORY_QUERY_ID,
        ]:
            self.fail("action_result_order_mismatch")
        try:
            navigation = json.loads(messages[0]["content"])
            memory = json.loads(messages[1]["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            self.fail("invalid_action_result_json")

        expected_navigation = {
            "ok": True,
            "result": {
                "body_id": BODY_ID,
                "navigation_state": {
                    "target_body_id": BODY_ID,
                    "status": "targeted",
                },
            },
            "error": None,
        }
        if navigation != expected_navigation:
            self.fail("navigation_result_mismatch")
        self.coordinator.tool_result_validated = True

        expected_memory = {
            "ok": True,
            "records": [SMOKE_MEMORY_RECORD],
            "error": None,
        }
        if memory != expected_memory:
            self.fail("memory_result_mismatch")
        self.coordinator.memory_result_validated = True


class _SmokeTTS(_SmokeProvider):
    def __init__(self, coordinator: SmokeCoordinator):
        super().__init__(coordinator, "tts")

    async def synthesize(self, text: str):
        if text == INTERRUPT_ASSISTANT_CAPTION:
            yield PCMChunk(
                audio=INTERRUPT_OUTPUT_CHUNK,
                sample_rate=24000,
                channels=1,
            )
            try:
                await asyncio.get_running_loop().create_future()
            except asyncio.CancelledError:
                if self.coordinator._interrupt_requested:
                    self.coordinator.interrupted_tts_cancelled = True
                else:
                    self.coordinator.record_failure("tts_cancelled_without_interrupt")
                raise
            return
        if text == FINAL_ASSISTANT_CAPTION:
            for audio in AUTHORITY_OUTPUT_CHUNKS:
                yield PCMChunk(audio=audio, sample_rate=24000, channels=1)
            return
        self.fail("unexpected_tts_text")
        if False:  # pragma: no cover - preserve the async-generator contract.
            yield PCMChunk(audio=b"", sample_rate=24000, channels=1)


def create_smoke_app(
    coordinator: SmokeCoordinator | None = None,
) -> FastAPI:
    smoke = coordinator or SmokeCoordinator()
    app = FastAPI(title="Dograh game companion provider-free smoke")
    app.state.smoke_coordinator = smoke

    @app.websocket("/api/v1/game-companion/ws")
    async def smoke_websocket(websocket: WebSocket) -> None:
        await serve_game_companion(
            websocket,
            session_factory=smoke.create_session,
        )

    @app.get("/smoke/health")
    async def smoke_health() -> dict[str, str]:
        return {"status": "ok", "providers": "fake"}

    @app.get("/smoke/status")
    async def smoke_status() -> dict[str, Any]:
        return smoke.snapshot()

    return app


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the provider-free Dograh companion smoke server."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument("--log-level", default="warning")
    arguments = parser.parse_args(argv)
    uvicorn.run(
        create_smoke_app(),
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
