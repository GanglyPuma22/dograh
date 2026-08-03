"""Typed, transport-independent contract for game companion protocol v1."""

import json
from collections import deque
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

PROTOCOL_VERSION = 1
MAX_JSON_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 16 * 1024
MAX_BINARY_FRAME_BYTES = 64 * 1024
MAX_TURN_AUDIO_BYTES = 16 * 1024 * 1024
MAX_TEXT_LENGTH = 4096
MAX_RETIRED_TURN_IDS = 64

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
Capability = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
CaptionText = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)]
SummaryText = Annotated[str, Field(min_length=1, max_length=1024)]
Tag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class ProtocolError(ValueError):
    """A stable protocol failure suitable for an error control frame."""

    def __init__(self, code: str, message: str, *, recoverable: bool = False):
        self.code = code
        self.message = message
        self.recoverable = recoverable
        super().__init__(f"{code}: {message}")


class ControlMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class TurnMessage(ControlMessage):
    turn_id: Identifier


class Hello(ControlMessage):
    type: Literal["hello"]
    protocol_version: Literal[PROTOCOL_VERSION]
    client: Literal["salvage"]
    save_id: Identifier
    capabilities: list[Capability] = Field(min_length=1, max_length=16)


class TurnStart(TurnMessage):
    type: Literal["turn_start"]
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def context_is_bounded(cls, value: dict[str, JsonValue]):
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} bytes")
        return value


class TurnEnd(TurnMessage):
    type: Literal["turn_end"]


class Interrupt(TurnMessage):
    type: Literal["interrupt"]


class ToolResult(TurnMessage):
    type: Literal["tool_result"]
    call_id: Identifier
    ok: bool
    result: dict[str, JsonValue] | None = None
    error: Annotated[str, Field(min_length=1, max_length=1024)] | None = None

    @model_validator(mode="after")
    def result_matches_status(self):
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("ok tool_result requires result and forbids error")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed tool_result requires error and forbids result")
        return self


class MemoryResult(TurnMessage):
    type: Literal["memory_result"]
    query_id: Identifier
    ok: bool
    records: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)
    error: Annotated[str, Field(min_length=1, max_length=1024)] | None = None

    @model_validator(mode="after")
    def result_matches_status(self):
        if self.ok and self.error is not None:
            raise ValueError("ok memory_result forbids error")
        if not self.ok and (self.error is None or self.records):
            raise ValueError("failed memory_result requires error and forbids records")
        return self


class AudioFormat(ControlMessage):
    sample_rate: int = Field(ge=8000, le=48000)
    channels: Literal[1]
    format: Literal["pcm_s16le"]


class HelloAck(ControlMessage):
    type: Literal["hello_ack"]
    protocol_version: Literal[PROTOCOL_VERSION]
    session_id: Identifier
    companion: Literal["Aster"]
    audio: AudioFormat


class State(TurnMessage):
    type: Literal["state"]
    state: Literal["listening", "thinking", "speaking", "idle", "degraded"]


class Caption(TurnMessage):
    type: Literal["caption"]
    speaker: Literal["player", "assistant"]
    text: CaptionText
    final: bool


class AudioStart(TurnMessage):
    type: Literal["audio_start"]
    sample_rate: int = Field(ge=8000, le=48000)
    channels: Literal[1]
    format: Literal["pcm_s16le"]


class AudioEnd(TurnMessage):
    type: Literal["audio_end"]


class ToolCall(TurnMessage):
    type: Literal["tool_call"]
    call_id: Identifier
    name: Literal["set_navigation_target"]
    arguments: dict[str, JsonValue]


class MemoryQuery(TurnMessage):
    type: Literal["memory_query"]
    query_id: Identifier
    name: Literal[
        "recent_activity",
        "recent_journal_items",
        "activity_for_body",
        "unresolved_incidents",
        "prior_failure",
        "journal_item_sources",
    ]
    arguments: dict[str, JsonValue]


class AnnotationProposal(TurnMessage):
    type: Literal["annotation_proposal"]
    proposal_id: Identifier
    source_event_ids: list[Identifier] = Field(min_length=1, max_length=64)
    summary: SummaryText
    tags: list[Tag] = Field(default_factory=list, max_length=16)


class ErrorMessage(TurnMessage):
    type: Literal["error"]
    code: Identifier
    message: Annotated[str, Field(min_length=1, max_length=1024)]
    recoverable: bool


ClientMessage: TypeAlias = (
    Hello | TurnStart | TurnEnd | Interrupt | ToolResult | MemoryResult
)
ServerMessage: TypeAlias = (
    HelloAck
    | State
    | Caption
    | AudioStart
    | AudioEnd
    | ToolCall
    | MemoryQuery
    | AnnotationProposal
    | ErrorMessage
)

_ClientMessageAdapter = TypeAdapter(
    Annotated[ClientMessage, Field(discriminator="type")]
)
_ServerMessageAdapter = TypeAdapter(
    Annotated[ServerMessage, Field(discriminator="type")]
)
_CLIENT_TYPES = frozenset(
    {"hello", "turn_start", "turn_end", "interrupt", "tool_result", "memory_result"}
)
_SERVER_TYPES = frozenset(
    {
        "hello_ack",
        "state",
        "caption",
        "audio_start",
        "audio_end",
        "tool_call",
        "memory_query",
        "annotation_proposal",
        "error",
    }
)


def _parse_message(
    payload: object,
    *,
    adapter: TypeAdapter,
    supported_types: frozenset[str],
) -> ControlMessage:
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_message", "message must be a JSON object")

    try:
        encoded_size = len(
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError(
            "invalid_message", "message must contain JSON values"
        ) from exc
    if encoded_size > MAX_JSON_BYTES:
        raise ProtocolError(
            "message_too_large", f"JSON message exceeds {MAX_JSON_BYTES} bytes"
        )

    message_type = payload.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("invalid_message", "type is required")
    if message_type not in supported_types:
        raise ProtocolError(
            "unsupported_message_type", f"unsupported message type: {message_type}"
        )
    if message_type in {"hello", "hello_ack"}:
        version = payload.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol",
                f"protocol_version must be {PROTOCOL_VERSION}",
            )

    try:
        return adapter.validate_python(payload)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first_error["loc"])
        detail = first_error["msg"]
        raise ProtocolError("invalid_message", f"{location}: {detail}") from exc


def parse_client_message(payload: object) -> ClientMessage:
    return _parse_message(
        payload,
        adapter=_ClientMessageAdapter,
        supported_types=_CLIENT_TYPES,
    )


def parse_server_message(payload: object) -> ServerMessage:
    return _parse_message(
        payload,
        adapter=_ServerMessageAdapter,
        supported_types=_SERVER_TYPES,
    )


def decode_control_json(text: str) -> object:
    """Decode one bounded, strict UTF-8 JSON control frame."""
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError("invalid_utf8", "control frame is not valid UTF-8") from exc
    if encoded_size > MAX_JSON_BYTES:
        raise ProtocolError(
            "message_too_large", f"JSON message exceeds {MAX_JSON_BYTES} bytes"
        )
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("invalid_json", "control frame is not valid JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class ClientMessageOrder:
    """Validate connection-level JSON and binary frame ordering."""

    def __init__(self):
        self.hello_received = False
        self.active_turn_id: str | None = None
        self.input_open = False
        self.turn_audio_bytes = 0
        self._retired_turn_ids: deque[str] = deque(maxlen=MAX_RETIRED_TURN_IDS)

    @property
    def retired_turn_count(self) -> int:
        return len(self._retired_turn_ids)

    def should_discard_retired_result(self, message: ClientMessage) -> bool:
        return (
            isinstance(message, (ToolResult, MemoryResult))
            and message.turn_id in self._retired_turn_ids
        )

    def accept(self, message: ClientMessage) -> str | None:
        if isinstance(message, Hello):
            if self.hello_received:
                raise ProtocolError("invalid_turn_order", "hello was already received")
            self.hello_received = True
            return None

        if not self.hello_received:
            raise ProtocolError("hello_required", "hello must be the first frame")

        if isinstance(message, TurnStart):
            if message.turn_id == self.active_turn_id:
                raise ProtocolError(
                    "invalid_turn_order", "turn_start repeated the active turn_id"
                )
            if message.turn_id in self._retired_turn_ids:
                raise ProtocolError(
                    "invalid_turn_order", "turn_start cannot reuse a retired turn_id"
                )
            interrupted_turn_id = self.active_turn_id
            if interrupted_turn_id is not None:
                self._retire_turn(interrupted_turn_id)
            self.active_turn_id = message.turn_id
            self.input_open = True
            self.turn_audio_bytes = 0
            return interrupted_turn_id

        self._require_active_turn(message.turn_id)

        if isinstance(message, TurnEnd):
            if not self.input_open:
                raise ProtocolError(
                    "invalid_turn_order", "turn_end requires open audio input"
                )
            self.input_open = False
        elif isinstance(message, Interrupt):
            self._retire_turn(message.turn_id)
            self.active_turn_id = None
            self.input_open = False
            self.turn_audio_bytes = 0
        elif isinstance(message, (ToolResult, MemoryResult)) and self.input_open:
            raise ProtocolError(
                "invalid_turn_order", f"{message.type} cannot precede turn_end"
            )
        return None

    def accept_binary(self, frame_size: int) -> None:
        if (
            not self.hello_received
            or self.active_turn_id is None
            or not self.input_open
        ):
            raise ProtocolError(
                "binary_outside_turn",
                "binary audio is allowed only between turn_start and turn_end",
            )
        if frame_size <= 0:
            raise ProtocolError("invalid_audio_frame", "binary audio frame is empty")
        if frame_size > MAX_BINARY_FRAME_BYTES:
            raise ProtocolError(
                "audio_frame_too_large",
                f"binary audio frame exceeds {MAX_BINARY_FRAME_BYTES} bytes",
            )
        self.turn_audio_bytes += frame_size
        if self.turn_audio_bytes > MAX_TURN_AUDIO_BYTES:
            raise ProtocolError(
                "turn_audio_too_large",
                f"turn audio exceeds {MAX_TURN_AUDIO_BYTES} bytes",
            )

    def _retire_turn(self, turn_id: str) -> None:
        if turn_id in self._retired_turn_ids:
            return
        self._retired_turn_ids.append(turn_id)

    def _require_active_turn(self, turn_id: str) -> None:
        if self.active_turn_id is None:
            raise ProtocolError(
                "invalid_turn_order", "turn-scoped message requires an active turn"
            )
        if turn_id != self.active_turn_id:
            raise ProtocolError(
                "stale_turn",
                f"message turn_id {turn_id!r} does not own the active turn",
            )
