"""Dependency-injected, single-turn orchestration for a companion connection."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from pydantic import JsonValue

from api.services.game_companion.persona import (
    ASTER_TOOLS,
    build_turn_messages,
)
from api.services.game_companion.protocol import (
    MAX_TURN_AUDIO_BYTES,
    AudioEnd,
    AudioStart,
    Caption,
    ErrorMessage,
    ProtocolError,
    ServerMessage,
    State,
    ToolCall,
    ToolResult,
)
from api.services.game_companion.providers import (
    LLMResult,
    PCMChunk,
    ProviderError,
    ProviderSet,
    pcm_s16le_to_wav,
)

OutboundEvent = ServerMessage | bytes
EmitCallback = Callable[[OutboundEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderTimeouts:
    stt: float = 30.0
    llm: float = 45.0
    tts: float = 45.0
    tool: float = 30.0


@dataclass(slots=True)
class _TurnRuntime:
    turn_id: str
    generation: int
    context: dict[str, JsonValue]
    audio: bytearray = field(default_factory=bytearray)
    input_closed: bool = False
    task: asyncio.Task | None = None
    pending_tool_results: dict[str, asyncio.Future[ToolResult]] = field(
        default_factory=dict
    )


class CompanionSession:
    """Own one active turn and discard output that loses turn ownership."""

    def __init__(
        self,
        *,
        providers: ProviderSet,
        emit: EmitCallback | None = None,
        timeouts: ProviderTimeouts | None = None,
        input_sample_rate: int = 16000,
    ):
        self.providers = providers
        self.timeouts = timeouts or ProviderTimeouts()
        self.input_sample_rate = input_sample_rate
        self.outbound_events: list[OutboundEvent] = []
        self.cancelled_turn_ids: list[str] = []
        self._emit_callback = emit
        self._generation = 0
        self._active: _TurnRuntime | None = None
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def active_turn_id(self) -> str | None:
        return self._active.turn_id if self._active else None

    @property
    def buffered_audio_bytes(self) -> int:
        return len(self._active.audio) if self._active else 0

    @property
    def pending_tool_result_count(self) -> int:
        return len(self._active.pending_tool_results) if self._active else 0

    async def start_turn(self, turn_id: str, context: dict[str, JsonValue]) -> None:
        self._require_open()
        old_runtime = self._active
        self._generation += 1
        runtime = _TurnRuntime(
            turn_id=turn_id,
            generation=self._generation,
            context=dict(context),
        )
        self._active = runtime
        if old_runtime is not None:
            self.cancelled_turn_ids.append(old_runtime.turn_id)
            self._cancel_runtime(old_runtime)
        await self._emit(
            runtime,
            State(type="state", turn_id=turn_id, state="listening"),
        )

    async def append_audio(self, turn_id: str, audio: bytes) -> None:
        runtime = self._require_active(turn_id)
        if runtime.input_closed:
            raise ProtocolError(
                "invalid_turn_order", "binary audio cannot follow turn_end"
            )
        if len(runtime.audio) + len(audio) > MAX_TURN_AUDIO_BYTES:
            raise ProtocolError(
                "turn_audio_too_large",
                f"turn audio exceeds {MAX_TURN_AUDIO_BYTES} bytes",
            )
        runtime.audio.extend(audio)

    async def end_turn(self, turn_id: str) -> None:
        runtime = self._require_active(turn_id)
        if runtime.input_closed:
            raise ProtocolError("invalid_turn_order", "turn_end was already received")
        if len(runtime.audio) % 2:
            raise ProtocolError(
                "invalid_audio_frame", "PCM16 turn ended with an incomplete sample"
            )
        runtime.input_closed = True
        runtime.task = asyncio.create_task(
            self._run_turn(runtime),
            name=f"game-companion-{turn_id}",
        )
        self._turn_tasks[turn_id] = runtime.task
        self._track_background_task(runtime.task)

    async def wait_for_turn(self, turn_id: str) -> None:
        task = self._turn_tasks.get(turn_id)
        if task is None:
            raise ProtocolError("invalid_turn_order", "turn has no provider task")
        await task

    async def submit_tool_result(self, result: ToolResult) -> None:
        runtime = self._require_active(result.turn_id)
        future = runtime.pending_tool_results.get(result.call_id)
        if future is None or future.done():
            raise ProtocolError(
                "unexpected_tool_result",
                f"no pending tool call owns call_id {result.call_id!r}",
            )
        future.set_result(result)

    async def interrupt(self, turn_id: str) -> None:
        runtime = self._require_active(turn_id)
        self.cancelled_turn_ids.append(turn_id)
        self._active = None
        self._cancel_runtime(runtime)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active is not None:
            self._cancel_runtime(self._active)
            self._active = None
        tasks = list(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._turn_tasks.clear()
        await self.providers.close()

    async def _run_turn(self, runtime: _TurnRuntime) -> None:
        try:
            wav_audio = pcm_s16le_to_wav(
                bytes(runtime.audio),
                sample_rate=self.input_sample_rate,
                channels=1,
            )
            runtime.audio.clear()
            async with asyncio.timeout(self.timeouts.stt):
                transcript = await self.providers.stt.transcribe(wav_audio)
            if not self._owns(runtime):
                return
            await self._emit(
                runtime,
                Caption(
                    type="caption",
                    turn_id=runtime.turn_id,
                    speaker="player",
                    text=transcript,
                    final=True,
                ),
            )
            await self._emit(
                runtime,
                State(type="state", turn_id=runtime.turn_id, state="thinking"),
            )

            messages = build_turn_messages(transcript, runtime.context)
            response_text = await self._respond_with_tools(runtime, messages)
            if not self._owns(runtime):
                return
            if not response_text:
                raise ProviderError("OpenRouter LLM returned no final narration")
            await self._speak(runtime, response_text)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._fail(
                runtime,
                code="provider_timeout",
                message="A companion provider timed out.",
            )
        except ProviderError:
            await self._fail(
                runtime,
                code="provider_failure",
                message="A companion provider returned an invalid response.",
            )
        except Exception:  # noqa: BLE001 - provider SDKs expose varied failures.
            await self._fail(
                runtime,
                code="provider_failure",
                message="A companion provider failed.",
            )
        finally:
            self._cancel_pending_tool_results(runtime)

    async def _respond_with_tools(
        self, runtime: _TurnRuntime, messages: list[dict]
    ) -> str:
        for _ in range(4):
            async with asyncio.timeout(self.timeouts.llm):
                result = await self.providers.llm.respond(messages, ASTER_TOOLS)
            if not self._owns(runtime):
                return ""
            if not result.tool_calls:
                return result.text.strip()
            tool_results = await self._request_tools(runtime, result)
            messages.extend(_tool_result_messages(result, tool_results))
        raise ProviderError("OpenRouter LLM exceeded the tool-call limit")

    async def _request_tools(
        self, runtime: _TurnRuntime, result: LLMResult
    ) -> list[ToolResult]:
        loop = asyncio.get_running_loop()
        for call in result.tool_calls:
            if call.name != "set_navigation_target":
                raise ProviderError("OpenRouter LLM requested an unregistered tool")
            if call.call_id in runtime.pending_tool_results:
                raise ProviderError("OpenRouter LLM repeated a tool call ID")
            runtime.pending_tool_results[call.call_id] = loop.create_future()

        for call in result.tool_calls:
            await self._emit(
                runtime,
                ToolCall(
                    type="tool_call",
                    turn_id=runtime.turn_id,
                    call_id=call.call_id,
                    name="set_navigation_target",
                    arguments=call.arguments,
                ),
            )

        results: list[ToolResult] = []
        for call in result.tool_calls:
            future = runtime.pending_tool_results[call.call_id]
            try:
                async with asyncio.timeout(self.timeouts.tool):
                    tool_result = await future
                results.append(tool_result)
            finally:
                runtime.pending_tool_results.pop(call.call_id, None)
        return results

    async def _speak(self, runtime: _TurnRuntime, text: str) -> None:
        await self._emit(
            runtime,
            Caption(
                type="caption",
                turn_id=runtime.turn_id,
                speaker="assistant",
                text=text,
                final=True,
            ),
        )
        await self._emit(
            runtime,
            State(type="state", turn_id=runtime.turn_id, state="speaking"),
        )

        first_chunk: PCMChunk | None = None
        try:
            async with asyncio.timeout(self.timeouts.tts):
                async for chunk in self.providers.tts.synthesize(text):
                    if not self._owns(runtime):
                        return
                    if first_chunk is None:
                        first_chunk = chunk
                        await self._emit(
                            runtime,
                            AudioStart(
                                type="audio_start",
                                turn_id=runtime.turn_id,
                                sample_rate=chunk.sample_rate,
                                channels=chunk.channels,
                                format="pcm_s16le",
                            ),
                        )
                    elif (
                        chunk.sample_rate != first_chunk.sample_rate
                        or chunk.channels != first_chunk.channels
                    ):
                        raise ProviderError(
                            "TTS PCM format changed within one response"
                        )
                    await self._emit(runtime, chunk.audio)

            if first_chunk is None:
                raise ProviderError("OpenRouter TTS returned no audio")
        finally:
            if first_chunk is not None and self._owns(runtime):
                await self._emit(
                    runtime,
                    AudioEnd(type="audio_end", turn_id=runtime.turn_id),
                )
        await self._emit(
            runtime,
            State(type="state", turn_id=runtime.turn_id, state="idle"),
        )

    async def _fail(self, runtime: _TurnRuntime, *, code: str, message: str) -> None:
        if not self._owns(runtime):
            return
        await self._emit(
            runtime,
            ErrorMessage(
                type="error",
                turn_id=runtime.turn_id,
                code=code,
                message=message,
                recoverable=True,
            ),
        )
        await self._emit(
            runtime,
            State(type="state", turn_id=runtime.turn_id, state="degraded"),
        )

    async def _emit(self, runtime: _TurnRuntime, event: OutboundEvent) -> bool:
        if not self._owns(runtime):
            return False
        if self._emit_callback is None:
            self.outbound_events.append(event)
        else:
            await self._emit_callback(event)
        return True

    def _owns(self, runtime: _TurnRuntime) -> bool:
        return (
            not self._closed
            and self._active is runtime
            and self._generation == runtime.generation
        )

    def _require_active(self, turn_id: str) -> _TurnRuntime:
        self._require_open()
        if self._active is None:
            raise ProtocolError(
                "invalid_turn_order", "turn-scoped operation requires an active turn"
            )
        if self._active.turn_id != turn_id:
            raise ProtocolError(
                "stale_turn", f"turn_id {turn_id!r} does not own the active turn"
            )
        return self._active

    def _require_open(self) -> None:
        if self._closed:
            raise ProtocolError("session_closed", "companion session is closed")

    def _cancel_runtime(self, runtime: _TurnRuntime) -> None:
        self._cancel_pending_tool_results(runtime)
        if runtime.task is not None and not runtime.task.done():
            runtime.task.cancel()

    @staticmethod
    def _cancel_pending_tool_results(runtime: _TurnRuntime) -> None:
        for future in runtime.pending_tool_results.values():
            if not future.done():
                future.cancel()
        runtime.pending_tool_results.clear()

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def discard(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            with suppress(asyncio.CancelledError, Exception):
                completed.exception()

        task.add_done_callback(discard)


def _tool_result_messages(
    llm_result: LLMResult, tool_results: list[ToolResult]
) -> list[dict]:
    assistant_tool_calls = [
        {
            "id": call.call_id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, separators=(",", ":")),
            },
        }
        for call in llm_result.tool_calls
    ]
    messages = [
        {
            "role": "assistant",
            "content": llm_result.text or None,
            "tool_calls": assistant_tool_calls,
        }
    ]
    for result in tool_results:
        content = {
            "ok": result.ok,
            "result": result.result,
            "error": result.error,
        }
        messages.append(
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": json.dumps(content, separators=(",", ":")),
            }
        )
    return messages
