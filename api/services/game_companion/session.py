"""Dependency-injected, single-turn orchestration for a companion connection."""

import asyncio
import json
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger
from pydantic import JsonValue

from api.services.game_companion.persona import (
    ASTER_TOOLS,
    build_analysis_messages,
    build_turn_messages,
)
from api.services.game_companion.protocol import (
    MAX_BINARY_FRAME_BYTES,
    MAX_TEXT_LENGTH,
    MAX_TURN_AUDIO_BYTES,
    AnnotationProposal,
    AudioEnd,
    AudioStart,
    Caption,
    ErrorMessage,
    MemoryQuery,
    MemoryResult,
    ProtocolError,
    ServerMessage,
    State,
    ToolCall,
    ToolResult,
)
from api.services.game_companion.providers import (
    MAX_ANALYSIS_LLM_RESPONSE_BYTES,
    LLMResult,
    PCMChunk,
    ProviderError,
    ProviderSet,
    pcm_s16le_to_wav,
)
from api.services.game_companion.speech_text import normalize_speech_text

OutboundEvent = ServerMessage | bytes
EmitCallback = Callable[..., Awaitable[None]]
MetricValue = str | int | float | bool
MetricCallback = Callable[[str, dict[str, MetricValue]], None]

MAX_MEMORY_QUERY_LIMIT = 20
DEFAULT_MEMORY_QUERY_LIMIT = 10
DEFAULT_ANNOTATION_COOLDOWN_SECONDS = 60.0
MAX_ANALYSIS_RECORDS = 20
MAX_ANALYSIS_CONTEXT_BYTES = 32 * 1024
MAX_ANALYSIS_RESPONSE_BYTES = MAX_ANALYSIS_LLM_RESPONSE_BYTES
MAX_ANNOTATION_SUMMARY_BYTES = 512
MAX_ANNOTATION_TAG_BYTES = 64
MAX_IDENTIFIER_BYTES = 128
MAX_OUTPUT_AUDIO_BYTES = 8 * 1024 * 1024
MAX_RETAINED_TURN_TASKS = 64
MAX_CANCELLED_TURN_IDS = 64
MAX_BACKGROUND_TURN_TASKS = 64
SESSION_CLOSE_TIMEOUT_SECONDS = 1.0
_UNSUPPORTED_PERSISTENCE_FALLBACK = "I found a pattern in the expedition record."
_PERSISTENCE_OBJECT_PATTERN = re.compile(
    r"\b(?:companion\s+analysis|analysis(?:\s+proposal)?|"
    r"annotation(?:\s+proposal)?|insight|note)\b",
    re.IGNORECASE,
)
_PERSISTENCE_VERB_PATTERN = re.compile(
    r"\b(?:sav(?:e|ed|es|ing)|stor(?:e|ed|es|ing)|accept(?:ed|s|ing)?|"
    r"add(?:ed|s|ing)?|write|writes|writing|wrote|written|"
    r"log(?:ged|s|ging)?|persist(?:ed|s|ing)?|creat(?:e|ed|es|ing)|"
    r"fil(?:e|ed|es|ing)|record(?:ed|s|ing)?|put|puts|putting|"
    r"append(?:ed|s|ing)?|updat(?:e|ed|es|ing)|commit(?:s|ted|ting)?|"
    r"archiv(?:e|ed|es|ing)|publish(?:ed|es|ing)?)\b",
    re.IGNORECASE,
)
_NEGATION_PATTERN = re.compile(
    r"(?:\bnever\b|\bnot\b|\bno\b|n(?:'|\u2019)t\b)[^.!?,;]{0,32}$",
    re.IGNORECASE,
)
_PERSISTENCE_CLAUSE_BREAK_PATTERN = re.compile(
    r"(?:[,;]|\bbut\b|\bhowever\b)",
    re.IGNORECASE,
)
CANONICAL_MEMORY_KEYS = (
    "schema_version",
    "event_id",
    "event_type",
    "occurred_at_utc",
    "game_time",
    "body_id",
    "importance",
    "status",
    "summary",
)
CANONICAL_MEMORY_DETAIL_KEYS = frozenset(
    {
        "attempt_id",
        "authority",
        "category",
        "current_body_id",
        "from_authority",
        "from_body_id",
        "incident_id",
        "navigation_state",
        "previous_body_id",
        "reason_code",
        "recovery_reason",
        "session_id",
        "target_body_id",
        "to_authority",
        "to_body_id",
    }
)
MEMORY_QUERY_NAMES = frozenset(
    {
        "recent_activity",
        "recent_journal_items",
        "activity_for_body",
        "unresolved_incidents",
        "prior_failure",
        "journal_item_sources",
    }
)
MEMORY_QUERY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "recent_activity",
            "description": "Read the most recent canonical gameplay activity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_QUERY_LIMIT,
                        "default": DEFAULT_MEMORY_QUERY_LIMIT,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_journal_items",
            "description": (
                "Read recent deterministic expedition episodes, player notes, and "
                "explicitly labelled Companion Analysis. Use this for summaries of "
                "what happened in prior play sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_QUERY_LIMIT,
                        "default": DEFAULT_MEMORY_QUERY_LIMIT,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activity_for_body",
            "description": "Read canonical activity for one stable body ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "body_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_QUERY_LIMIT,
                        "default": DEFAULT_MEMORY_QUERY_LIMIT,
                    },
                },
                "required": ["body_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unresolved_incidents",
            "description": "Read canonical unresolved incident activity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_QUERY_LIMIT,
                        "default": DEFAULT_MEMORY_QUERY_LIMIT,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prior_failure",
            "description": "Read prior failed activity for one stable category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_QUERY_LIMIT,
                        "default": DEFAULT_MEMORY_QUERY_LIMIT,
                    },
                },
                "required": ["category"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_item_sources",
            "description": "Read canonical source events for one journal item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "minLength": 1, "maxLength": 128}
                },
                "required": ["item_id"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True, slots=True)
class ProviderTimeouts:
    stt: float = 30.0
    llm: float = 45.0
    tts: float = 45.0
    tool: float = 30.0


@dataclass(frozen=True, slots=True)
class _PendingMemoryResult:
    future: asyncio.Future[MemoryResult]
    name: str
    max_records: int
    started_at: float


@dataclass(frozen=True, slots=True)
class _PendingToolResult:
    future: asyncio.Future[ToolResult]
    name: str
    arguments: dict[str, JsonValue]


@dataclass(slots=True)
class _TurnRuntime:
    turn_id: str
    generation: int
    context: dict[str, JsonValue]
    audio: bytearray = field(default_factory=bytearray)
    input_closed: bool = False
    task: asyncio.Task | None = None
    pending_tool_results: dict[str, _PendingToolResult] = field(default_factory=dict)
    pending_memory_results: dict[str, _PendingMemoryResult] = field(
        default_factory=dict
    )
    timed_out_tool_ids: set[str] = field(default_factory=set)
    timed_out_memory_ids: set[str] = field(default_factory=set)
    used_action_ids: set[str] = field(default_factory=set)
    analysis_records: list[dict[str, JsonValue]] = field(default_factory=list)
    analysis_event_ids: set[str] = field(default_factory=set)
    analysis_attempted: bool = False
    processing_started_at: float = 0.0


class CompanionSession:
    """Own one active turn and discard output that loses turn ownership."""

    def __init__(
        self,
        *,
        providers: ProviderSet,
        emit: EmitCallback | None = None,
        timeouts: ProviderTimeouts | None = None,
        input_sample_rate: int = 16000,
        annotation_cooldown_seconds: float = DEFAULT_ANNOTATION_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        metric: MetricCallback | None = None,
        proposal_id_factory: Callable[[], str] | None = None,
    ):
        if annotation_cooldown_seconds < 0:
            raise ValueError("annotation_cooldown_seconds cannot be negative")
        self.providers = providers
        self.timeouts = timeouts or ProviderTimeouts()
        self.input_sample_rate = input_sample_rate
        self.annotation_cooldown_seconds = annotation_cooldown_seconds
        self.outbound_events: list[OutboundEvent] = []
        self.cancelled_turn_ids: deque[str] = deque(maxlen=MAX_CANCELLED_TURN_IDS)
        self._emit_callback = emit
        self._monotonic = monotonic
        self._metric = metric or _log_session_metric
        self._proposal_id_factory = proposal_id_factory or (lambda: str(uuid.uuid4()))
        self._last_annotation_at: float | None = None
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

    @property
    def retained_turn_task_count(self) -> int:
        return len(self._turn_tasks)

    @property
    def background_turn_task_count(self) -> int:
        return len(self._background_tasks)

    async def start_turn(self, turn_id: str, context: dict[str, JsonValue]) -> None:
        self._require_open()
        self._require_provider_capacity()
        old_runtime = self._active
        self._generation += 1
        runtime = _TurnRuntime(
            turn_id=turn_id,
            generation=self._generation,
            context=dict(context),
        )
        self._active = runtime
        if old_runtime is not None:
            if old_runtime.task is None or not old_runtime.task.done():
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
        if not runtime.audio:
            raise ProtocolError(
                "invalid_audio_frame",
                "turn contains no audio",
                recoverable=True,
            )
        if len(runtime.audio) % 2:
            raise ProtocolError(
                "invalid_audio_frame", "PCM16 turn ended with an incomplete sample"
            )
        runtime.input_closed = True
        runtime.task = asyncio.create_task(
            self._run_turn(runtime),
            name=f"game-companion-{turn_id}",
        )
        self._retain_turn_task(turn_id, runtime.task)
        self._track_background_task(runtime.task)

    async def wait_for_turn(self, turn_id: str) -> None:
        task = self._turn_tasks.get(turn_id)
        if task is None:
            raise ProtocolError("invalid_turn_order", "turn has no provider task")
        await task

    async def submit_tool_result(self, result: ToolResult) -> None:
        runtime = self._require_active(result.turn_id)
        pending = runtime.pending_tool_results.get(result.call_id)
        if pending is None and result.call_id in runtime.timed_out_tool_ids:
            return
        if pending is None or pending.future.done():
            raise ProtocolError(
                "unexpected_tool_result",
                f"no pending tool call owns call_id {result.call_id!r}",
            )
        if not _tool_result_matches_request(pending, result):
            raise ProtocolError(
                "invalid_tool_result",
                "tool result does not match the authorized request",
                recoverable=True,
            )
        pending.future.set_result(result)

    async def submit_memory_result(self, result: MemoryResult) -> None:
        runtime = self._require_active(result.turn_id)
        pending = runtime.pending_memory_results.get(result.query_id)
        if pending is None and result.query_id in runtime.timed_out_memory_ids:
            return
        if pending is None or pending.future.done():
            raise ProtocolError(
                "unexpected_memory_result",
                f"no pending memory query owns query_id {result.query_id!r}",
            )
        if len(result.records) > pending.max_records:
            raise ProtocolError(
                "memory_result_too_large",
                "memory result exceeds the originating query limit",
            )
        if result.ok:
            _remember_analysis_records(runtime, result.records)
        logger.info(
            "Game companion memory result: name={} ok={} records={} error={} "
            "elapsed_ms={:.1f}",
            pending.name,
            result.ok,
            len(result.records),
            result.error or "none",
            (self._monotonic() - pending.started_at) * 1000.0,
        )
        pending.future.set_result(result)

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
        await self._wait_for_cleanup(tasks)
        self._turn_tasks.clear()
        provider_close = asyncio.create_task(
            self.providers.close(),
            name="game-companion-provider-close",
        )
        await self._wait_for_cleanup([provider_close])

    async def _run_turn(self, runtime: _TurnRuntime) -> None:
        try:
            runtime.processing_started_at = self._monotonic()
            wav_audio = pcm_s16le_to_wav(
                bytes(runtime.audio),
                sample_rate=self.input_sample_rate,
                channels=1,
            )
            runtime.audio.clear()
            stt_started_at = self._monotonic()
            async with asyncio.timeout(self.timeouts.stt):
                transcript = await self.providers.stt.transcribe(wav_audio)
            self._record_metric(
                "stt_complete",
                runtime,
                elapsed_ms=_elapsed_ms(stt_started_at, self._monotonic()),
            )
            if not self._owns(runtime):
                return
            if not transcript.strip():
                await self._fail(
                    runtime,
                    code="no_speech_detected",
                    message="No speech was detected.",
                )
                return
            if not await self._emit(
                runtime,
                Caption(
                    type="caption",
                    turn_id=runtime.turn_id,
                    speaker="player",
                    text=_caption_text(transcript),
                    final=True,
                ),
            ):
                return
            if not await self._emit(
                runtime,
                State(type="state", turn_id=runtime.turn_id, state="thinking"),
            ):
                return

            messages = build_turn_messages(transcript, runtime.context)
            response_text = await self._respond_with_tools(runtime, messages)
            if not self._owns(runtime):
                return
            if not response_text.strip():
                raise ProviderError("OpenRouter LLM returned no final narration")
            response_text = _filter_unsupported_persistence_claims(response_text)
            await self._speak(runtime, response_text)
            await self._maybe_propose_annotation(runtime)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._fail(
                runtime,
                code="provider_timeout",
                message="A companion provider timed out.",
            )
        except ProviderError:
            logger.exception("Game companion provider returned an invalid response")
            await self._fail(
                runtime,
                code="provider_failure",
                message="A companion provider returned an invalid response.",
            )
        except Exception:  # noqa: BLE001 - provider SDKs expose varied failures.
            logger.exception("Game companion provider failed")
            await self._fail(
                runtime,
                code="provider_failure",
                message="A companion provider failed.",
            )
        finally:
            self._cancel_pending_results(runtime, retire_late_results=True)
            runtime.analysis_records.clear()
            runtime.analysis_event_ids.clear()

    async def _respond_with_tools(
        self, runtime: _TurnRuntime, messages: list[dict]
    ) -> str:
        sequence_started_at = self._monotonic()
        provider_elapsed_ms = 0.0
        for round_index in range(4):
            round_started_at = self._monotonic()
            async with asyncio.timeout(self.timeouts.llm):
                result = await self.providers.llm.respond(
                    messages, ASTER_TOOLS + MEMORY_QUERY_TOOLS
                )
            round_elapsed_ms = _elapsed_ms(round_started_at, self._monotonic())
            provider_elapsed_ms += round_elapsed_ms
            self._record_metric(
                "llm_round_complete",
                runtime,
                round=round_index + 1,
                elapsed_ms=round_elapsed_ms,
                tool_calls=len(result.tool_calls),
            )
            if not self._owns(runtime):
                return ""
            if not result.tool_calls:
                self._record_metric(
                    "llm_complete",
                    runtime,
                    rounds=round_index + 1,
                    provider_elapsed_ms=provider_elapsed_ms,
                    orchestration_elapsed_ms=_elapsed_ms(
                        sequence_started_at, self._monotonic()
                    ),
                )
                return result.text.strip()
            action_results = await self._request_actions(runtime, result)
            messages.extend(_action_result_messages(result, action_results))
        raise ProviderError("OpenRouter LLM exceeded the tool-call limit")

    async def _maybe_propose_annotation(self, runtime: _TurnRuntime) -> None:
        if runtime.analysis_attempted:
            return
        runtime.analysis_attempted = True
        if not runtime.analysis_records or not self._owns(runtime):
            return
        if (
            self._last_annotation_at is not None
            and self._monotonic() - self._last_annotation_at
            < self.annotation_cooldown_seconds
        ):
            return

        try:
            async with asyncio.timeout(self.timeouts.llm):
                result = await self.providers.llm.respond(
                    build_analysis_messages(runtime.analysis_records), []
                )
            if not self._owns(runtime):
                return
            proposal = _parse_grounded_annotation(
                turn_id=runtime.turn_id,
                proposal_id_factory=self._proposal_id_factory,
                result=result,
                grounded_source_event_ids=runtime.analysis_event_ids,
            )
            if proposal is None:
                return
            if await self._emit(runtime, proposal):
                self._last_annotation_at = self._monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - optional analysis cannot fail a spoken turn.
            logger.exception("Game companion optional annotation analysis failed")
            return

    async def _request_actions(
        self, runtime: _TurnRuntime, result: LLMResult
    ) -> list[ToolResult | MemoryResult]:
        loop = asyncio.get_running_loop()
        tool_arguments: dict[str, dict[str, JsonValue]] = {}
        memory_arguments: dict[str, dict[str, JsonValue]] = {}
        for call in result.tool_calls:
            if call.call_id in runtime.used_action_ids:
                raise ProviderError("OpenRouter LLM repeated a tool call ID")
            runtime.used_action_ids.add(call.call_id)
            if call.name == "set_navigation_target":
                arguments = _normalize_navigation_arguments(call.arguments)
                tool_arguments[call.call_id] = arguments
                if (
                    call.call_id in runtime.pending_tool_results
                    or call.call_id in runtime.pending_memory_results
                ):
                    raise ProviderError("OpenRouter LLM repeated a tool call ID")
                runtime.pending_tool_results[call.call_id] = _PendingToolResult(
                    future=loop.create_future(),
                    name=call.name,
                    arguments=arguments,
                )
            elif call.name in MEMORY_QUERY_NAMES:
                if (
                    call.call_id in runtime.pending_tool_results
                    or call.call_id in runtime.pending_memory_results
                ):
                    raise ProviderError("OpenRouter LLM repeated a tool call ID")
                arguments = _normalize_memory_query_arguments(call.name, call.arguments)
                memory_arguments[call.call_id] = arguments
                runtime.pending_memory_results[call.call_id] = _PendingMemoryResult(
                    future=loop.create_future(),
                    name=call.name,
                    max_records=arguments.get("limit", MAX_MEMORY_QUERY_LIMIT),
                    started_at=self._monotonic(),
                )
            else:
                raise ProviderError("OpenRouter LLM requested an unregistered tool")

        for call in result.tool_calls:
            if call.name == "set_navigation_target":
                event: ToolCall | MemoryQuery = ToolCall(
                    type="tool_call",
                    turn_id=runtime.turn_id,
                    call_id=call.call_id,
                    name="set_navigation_target",
                    arguments=tool_arguments[call.call_id],
                )
            else:
                logger.info(
                    "Game companion memory query: name={} limit={}",
                    call.name,
                    memory_arguments[call.call_id].get("limit", "none"),
                )
                event = MemoryQuery(
                    type="memory_query",
                    turn_id=runtime.turn_id,
                    query_id=call.call_id,
                    name=call.name,
                    arguments=memory_arguments[call.call_id],
                )
            if not await self._emit(runtime, event):
                raise asyncio.CancelledError

        results: list[ToolResult | MemoryResult] = []
        for call in result.tool_calls:
            if call.name == "set_navigation_target":
                future = runtime.pending_tool_results[call.call_id].future
            else:
                future = runtime.pending_memory_results[call.call_id].future
            try:
                async with asyncio.timeout(self.timeouts.tool):
                    action_result = await future
                results.append(action_result)
            except TimeoutError:
                if call.name == "set_navigation_target":
                    runtime.timed_out_tool_ids.add(call.call_id)
                else:
                    runtime.timed_out_memory_ids.add(call.call_id)
                raise
            finally:
                if call.name == "set_navigation_target":
                    runtime.pending_tool_results.pop(call.call_id, None)
                else:
                    runtime.pending_memory_results.pop(call.call_id, None)
        return results

    async def _speak(self, runtime: _TurnRuntime, text: str) -> None:
        if not await self._emit(
            runtime,
            Caption(
                type="caption",
                turn_id=runtime.turn_id,
                speaker="assistant",
                text=_caption_text(text),
                final=True,
            ),
        ):
            return
        if not await self._emit(
            runtime,
            State(type="state", turn_id=runtime.turn_id, state="speaking"),
        ):
            return

        first_chunk: PCMChunk | None = None
        audio_open = False
        output_audio_bytes = 0
        tts_started_at = self._monotonic()
        speech_text = normalize_speech_text(text)
        try:
            async with asyncio.timeout(self.timeouts.tts):
                async for chunk in self.providers.tts.synthesize(speech_text):
                    if not self._owns(runtime):
                        return
                    _validate_tts_chunk(chunk)
                    next_audio_bytes = output_audio_bytes + len(chunk.audio)
                    if next_audio_bytes > MAX_OUTPUT_AUDIO_BYTES:
                        raise ProviderError("TTS PCM response is too large")
                    if first_chunk is None:
                        first_audio_at = self._monotonic()
                        self._record_metric(
                            "first_audio",
                            runtime,
                            tts_elapsed_ms=_elapsed_ms(tts_started_at, first_audio_at),
                            turn_elapsed_ms=_elapsed_ms(
                                runtime.processing_started_at, first_audio_at
                            ),
                        )
                        if not await self._emit(
                            runtime,
                            AudioStart(
                                type="audio_start",
                                turn_id=runtime.turn_id,
                                sample_rate=chunk.sample_rate,
                                channels=chunk.channels,
                                format="pcm_s16le",
                            ),
                        ):
                            return
                        first_chunk = chunk
                        audio_open = True
                    elif (
                        chunk.sample_rate != first_chunk.sample_rate
                        or chunk.channels != first_chunk.channels
                    ):
                        raise ProviderError(
                            "TTS PCM format changed within one response"
                        )
                    if not await self._emit(runtime, chunk.audio):
                        return
                    output_audio_bytes = next_audio_bytes

            if first_chunk is None:
                raise ProviderError("OpenRouter TTS returned no audio")
            self._record_metric(
                "tts_complete",
                runtime,
                elapsed_ms=_elapsed_ms(tts_started_at, self._monotonic()),
                pcm_bytes=output_audio_bytes,
            )
        finally:
            if audio_open and self._owns(runtime):
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
        if not await self._emit(
            runtime,
            ErrorMessage(
                type="error",
                turn_id=runtime.turn_id,
                code=code,
                message=message,
                recoverable=True,
            ),
        ):
            return
        await self._emit(
            runtime,
            State(type="state", turn_id=runtime.turn_id, state="degraded"),
        )

    async def _emit(self, runtime: _TurnRuntime, event: OutboundEvent) -> bool:
        if not self._owns(runtime):
            return False
        if self._emit_callback is None:
            self.outbound_events.append(event)
        elif getattr(self._emit_callback, "checks_turn_ownership", False):
            await self._emit_callback(event, lambda: self._owns(runtime))
        else:
            await self._emit_callback(event)
        return self._owns(runtime)

    def _owns(self, runtime: _TurnRuntime) -> bool:
        return (
            not self._closed
            and self._active is runtime
            and self._generation == runtime.generation
        )

    def _record_metric(
        self,
        event: str,
        runtime: _TurnRuntime,
        **fields: MetricValue,
    ) -> None:
        values: dict[str, MetricValue] = {"turn_id": runtime.turn_id, **fields}
        try:
            self._metric(event, values)
        except Exception:  # noqa: BLE001 - metrics cannot break a voice turn.
            logger.warning("Game companion metric sink failed: event={}", event)

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
        self._cancel_pending_results(runtime)
        if runtime.task is not None and not runtime.task.done():
            runtime.task.cancel()

    @staticmethod
    def _cancel_pending_results(
        runtime: _TurnRuntime,
        *,
        retire_late_results: bool = False,
    ) -> None:
        if retire_late_results:
            runtime.timed_out_tool_ids.update(runtime.pending_tool_results)
            runtime.timed_out_memory_ids.update(runtime.pending_memory_results)
        for pending in runtime.pending_tool_results.values():
            if not pending.future.done():
                pending.future.cancel()
        runtime.pending_tool_results.clear()
        for pending in runtime.pending_memory_results.values():
            if not pending.future.done():
                pending.future.cancel()
        runtime.pending_memory_results.clear()

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def discard(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            with suppress(asyncio.CancelledError, Exception):
                completed.exception()

        task.add_done_callback(discard)

    def _require_provider_capacity(self) -> None:
        for task in tuple(self._background_tasks):
            if task.done():
                self._background_tasks.discard(task)
                _consume_task_result(task)
        if len(self._background_tasks) >= MAX_BACKGROUND_TURN_TASKS:
            raise ProtocolError(
                "provider_backlog_exhausted",
                "provider work did not stop; reconnect before starting another turn",
                recoverable=True,
            )

    def _retain_turn_task(self, turn_id: str, task: asyncio.Task) -> None:
        self._turn_tasks.pop(turn_id, None)
        self._turn_tasks[turn_id] = task
        while len(self._turn_tasks) > MAX_RETAINED_TURN_TASKS:
            oldest_turn_id = next(iter(self._turn_tasks))
            self._turn_tasks.pop(oldest_turn_id)

    async def _wait_for_cleanup(self, tasks: list[asyncio.Task]) -> None:
        pending = {task for task in tasks if not task.done()}
        if pending:
            done, pending = await asyncio.wait(
                pending,
                timeout=SESSION_CLOSE_TIMEOUT_SECONDS,
            )
        else:
            done = set(tasks)
        for task in done:
            _consume_task_result(task)
        for task in pending:
            task.cancel()
            if task not in self._background_tasks:
                task.add_done_callback(_consume_task_result)


def _normalize_navigation_arguments(
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    if set(arguments) != {"body_id"}:
        raise ProviderError("navigation tool requires only body_id")
    body_id = arguments.get("body_id")
    if (
        not isinstance(body_id, str)
        or body_id != body_id.strip()
        or _normalize_identifier(body_id) is None
    ):
        raise ProviderError("navigation tool requires a valid body_id")
    return {"body_id": body_id}


def _validate_tts_chunk(chunk: PCMChunk) -> None:
    if not isinstance(chunk, PCMChunk):
        raise ProviderError("TTS returned an invalid PCM chunk")
    if (
        type(chunk.sample_rate) is not int
        or chunk.sample_rate < 8000
        or chunk.sample_rate > 48000
    ):
        raise ProviderError("TTS returned an invalid PCM sample rate")
    if type(chunk.channels) is not int or chunk.channels != 1:
        raise ProviderError("TTS returned non-mono PCM audio")
    if not isinstance(chunk.audio, bytes) or not chunk.audio:
        raise ProviderError("TTS returned an invalid PCM audio frame")
    if len(chunk.audio) > MAX_BINARY_FRAME_BYTES:
        raise ProviderError("TTS PCM audio frame is too large")
    if len(chunk.audio) % 2:
        raise ProviderError("TTS PCM audio frame contains an incomplete sample")


def _consume_task_result(task: asyncio.Task) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


def _tool_result_matches_request(
    pending: _PendingToolResult,
    result: ToolResult,
) -> bool:
    if not result.ok:
        return True
    if pending.name != "set_navigation_target" or result.result is None:
        return False
    if set(result.result) != {"body_id", "navigation_state"}:
        return False
    expected_body_id = pending.arguments.get("body_id")
    if result.result.get("body_id") != expected_body_id:
        return False
    navigation_state = result.result.get("navigation_state")
    if isinstance(navigation_state, str):
        return _normalize_identifier(navigation_state) is not None
    if not isinstance(navigation_state, dict) or set(navigation_state) != {
        "target_body_id",
        "status",
    }:
        return False
    return (
        navigation_state.get("target_body_id") == expected_body_id
        and _normalize_identifier(navigation_state.get("status")) is not None
    )


def _filter_unsupported_persistence_claims(text: str) -> str:
    supported_sentences: list[str] = []
    removed_sentence = False
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        unsupported = False
        if _PERSISTENCE_OBJECT_PATTERN.search(sentence) is None:
            supported_sentences.append(sentence)
            continue
        for verb in _PERSISTENCE_VERB_PATTERN.finditer(sentence):
            prefix = sentence[max(0, verb.start() - 40) : verb.start()]
            prefix = _PERSISTENCE_CLAUSE_BREAK_PATTERN.split(prefix)[-1]
            if _NEGATION_PATTERN.search(prefix) is None:
                unsupported = True
                break
        if not unsupported:
            supported_sentences.append(sentence)
        else:
            removed_sentence = True
    if not removed_sentence:
        return text
    return " ".join(supported_sentences) or _UNSUPPORTED_PERSISTENCE_FALLBACK


def _caption_text(text: str) -> str:
    return text[:MAX_TEXT_LENGTH]


def _normalize_memory_query_arguments(
    name: str, arguments: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    identifier_name: str | None = None
    if name == "activity_for_body":
        identifier_name = "body_id"
    elif name == "prior_failure":
        identifier_name = "category"
    elif name == "journal_item_sources":
        identifier_name = "item_id"

    supports_limit = name != "journal_item_sources"
    allowed_names = {identifier_name} if identifier_name else set()
    if supports_limit:
        allowed_names.add("limit")
    if set(arguments) != set(arguments).intersection(allowed_names):
        raise ProviderError("memory query contains unsupported arguments")

    normalized: dict[str, JsonValue] = {}
    if identifier_name is not None:
        identifier = arguments.get(identifier_name)
        if (
            not isinstance(identifier, str)
            or not identifier
            or len(identifier.encode("utf-8")) > 128
            or identifier.strip() != identifier
        ):
            raise ProviderError(f"memory query requires a valid {identifier_name}")
        normalized[identifier_name] = identifier

    if supports_limit:
        limit = arguments.get("limit", DEFAULT_MEMORY_QUERY_LIMIT)
        if type(limit) is not int or limit < 1 or limit > MAX_MEMORY_QUERY_LIMIT:
            raise ProviderError(
                f"memory query limit must be between 1 and {MAX_MEMORY_QUERY_LIMIT}"
            )
        normalized["limit"] = limit
    return normalized


def _remember_analysis_records(
    runtime: _TurnRuntime,
    records: list[dict[str, JsonValue]],
) -> None:
    for record in records:
        if len(runtime.analysis_records) >= MAX_ANALYSIS_RECORDS:
            return
        canonical = _canonical_analysis_record(record)
        if canonical is None:
            continue
        event_id = canonical["event_id"]
        if not isinstance(event_id, str) or event_id in runtime.analysis_event_ids:
            continue
        candidate = [*runtime.analysis_records, canonical]
        try:
            encoded = json.dumps(
                {"canonical_memory_records": candidate},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            continue
        if len(encoded) > MAX_ANALYSIS_CONTEXT_BYTES:
            continue
        runtime.analysis_records.append(canonical)
        runtime.analysis_event_ids.add(event_id)


def _canonical_analysis_record(
    record: dict[str, JsonValue],
) -> dict[str, JsonValue] | None:
    if any(key not in record for key in CANONICAL_MEMORY_KEYS):
        return None
    schema_version = record["schema_version"]
    event_id = _normalize_identifier(record["event_id"])
    event_type = _normalize_identifier(record["event_type"])
    occurred_at_utc = record["occurred_at_utc"]
    game_time = record["game_time"]
    body_id = record["body_id"]
    importance = record["importance"]
    status = record["status"]
    summary = record["summary"]
    if type(schema_version) is not int or schema_version != 1:
        return None
    if event_id is None or event_type is None:
        return None
    if not isinstance(occurred_at_utc, str) or not _valid_timestamp(occurred_at_utc):
        return None
    if not _is_finite_nonnegative_number(game_time):
        return None
    if not isinstance(body_id, str):
        return None
    if body_id:
        normalized_body_id = _normalize_identifier(body_id)
        if normalized_body_id is None:
            return None
    else:
        normalized_body_id = ""
    if not isinstance(importance, str) or importance not in {
        "low",
        "normal",
        "high",
        "critical",
    }:
        return None
    if not isinstance(status, str) or status not in {
        "started",
        "completed",
        "failed",
        "unresolved",
        "cancelled",
    }:
        return None
    if not isinstance(summary, str):
        return None
    normalized_summary = summary.strip()
    if not normalized_summary or not _fits_utf8(normalized_summary, 512):
        return None

    canonical: dict[str, JsonValue] = {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at_utc": occurred_at_utc,
        "game_time": game_time,
        "body_id": normalized_body_id,
        "importance": importance,
        "status": status,
        "summary": normalized_summary,
    }
    for key in CANONICAL_MEMORY_DETAIL_KEYS:
        if key not in record:
            continue
        value = record[key]
        if not _is_bounded_memory_scalar(value):
            return None
        canonical[key] = value
    return canonical


def _parse_grounded_annotation(
    *,
    turn_id: str,
    proposal_id_factory: Callable[[], str],
    result: LLMResult,
    grounded_source_event_ids: set[str],
) -> AnnotationProposal | None:
    if result.tool_calls or not isinstance(result.text, str):
        return None
    text = result.text.strip()
    if not text or text == "NO_ANALYSIS":
        return None
    if not _fits_utf8(text, MAX_ANALYSIS_RESPONSE_BYTES):
        return None
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "source_event_ids",
        "summary",
        "tags",
    }:
        return None

    raw_source_ids = payload["source_event_ids"]
    raw_summary = payload["summary"]
    raw_tags = payload["tags"]
    if (
        not isinstance(raw_source_ids, list)
        or not 1 <= len(raw_source_ids) <= 64
        or not isinstance(raw_summary, str)
        or not isinstance(raw_tags, list)
        or len(raw_tags) > 16
        or any(not isinstance(source_id, str) for source_id in raw_source_ids)
        or any(not isinstance(tag, str) for tag in raw_tags)
    ):
        return None

    source_event_ids: list[str] = []
    for raw_source_id in raw_source_ids:
        source_id = _normalize_identifier(raw_source_id)
        if source_id is None or source_id in source_event_ids:
            return None
        source_event_ids.append(source_id)
    if not set(source_event_ids).issubset(grounded_source_event_ids):
        return None
    summary = " ".join(raw_summary.split())
    if not summary or not _fits_utf8(summary, MAX_ANNOTATION_SUMMARY_BYTES):
        return None

    tags: list[str] = []
    for raw_tag in raw_tags:
        tag = " ".join(raw_tag.split()).lower()
        if not tag or not _fits_utf8(tag, MAX_ANNOTATION_TAG_BYTES):
            return None
        if tag not in tags:
            tags.append(tag)
    tags.sort()
    proposal_id = _normalize_identifier(proposal_id_factory())
    if proposal_id is None:
        return None
    try:
        return AnnotationProposal(
            type="annotation_proposal",
            turn_id=turn_id,
            proposal_id=proposal_id,
            source_event_ids=source_event_ids,
            summary=summary,
            tags=tags,
        )
    except (TypeError, ValueError):
        return None


def _normalize_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or not _fits_utf8(normalized, MAX_IDENTIFIER_BYTES):
        return None
    return normalized


def _fits_utf8(value: str, maximum: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _valid_timestamp(value: str) -> bool:
    if len(value) != 20 or not value.endswith("Z"):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return False
    return True


def _is_bounded_memory_scalar(value: JsonValue) -> bool:
    if isinstance(value, bool) or type(value) is int:
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and _fits_utf8(value, MAX_IDENTIFIER_BYTES)


def _is_finite_nonnegative_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0.0


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _action_result_messages(
    llm_result: LLMResult, action_results: list[ToolResult | MemoryResult]
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
    for result in action_results:
        if isinstance(result, ToolResult):
            content = {
                "ok": result.ok,
                "result": result.result,
                "error": result.error,
            }
            result_id = result.call_id
        else:
            content = {
                "ok": result.ok,
                "records": result.records,
                "error": result.error,
            }
            result_id = result.query_id
        messages.append(
            {
                "role": "tool",
                "tool_call_id": result_id,
                "content": json.dumps(content, separators=(",", ":")),
            }
        )
    return messages


def _elapsed_ms(started_at: float, completed_at: float) -> float:
    return max((completed_at - started_at) * 1000.0, 0.0)


def _log_session_metric(event: str, fields: dict[str, MetricValue]) -> None:
    logger.info(
        "Game companion metric: event={} fields={}",
        event,
        json.dumps(fields, separators=(",", ":"), sort_keys=True),
    )
