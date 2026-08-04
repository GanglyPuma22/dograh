"""Local, versioned WebSocket transport for the Salvage companion."""

import asyncio
import hmac
import uuid
from collections.abc import Callable
from contextlib import suppress

from fastapi import APIRouter, WebSocket
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from api.constants import (
    DOGRAH_GAME_COMPANION_ENABLED,
    DOGRAH_GAME_COMPANION_TOKEN,
)
from api.services.game_companion.protocol import (
    MAX_BINARY_FRAME_BYTES,
    MAX_JSON_BYTES,
    PROTOCOL_VERSION,
    AudioFormat,
    ClientMessageOrder,
    ErrorMessage,
    Hello,
    HelloAck,
    Interrupt,
    MemoryResult,
    ProtocolError,
    ToolResult,
    TurnEnd,
    TurnStart,
    decode_control_json,
    parse_client_message,
)
from api.services.game_companion.providers import create_game_companion_provider_set
from api.services.game_companion.session import (
    CompanionSession,
    EmitCallback,
    OutboundEvent,
)

router = APIRouter(prefix="/game-companion")
SessionFactory = Callable[[EmitCallback], CompanionSession]
_OVERSIZE_CODES = {
    "message_too_large",
    "audio_frame_too_large",
    "turn_audio_too_large",
}


def create_companion_session(emit: EmitCallback) -> CompanionSession:
    return CompanionSession(providers=create_game_companion_provider_set(), emit=emit)


@router.websocket("/ws")
async def game_companion_websocket(websocket: WebSocket) -> None:
    if not DOGRAH_GAME_COMPANION_ENABLED:
        await websocket.close(code=1008, reason="game_companion_disabled")
        return
    expected_token = DOGRAH_GAME_COMPANION_TOKEN
    authorization = websocket.headers.get("authorization", "")
    scheme, separator, supplied_token = authorization.partition(" ")
    if (
        not expected_token
        or separator != " "
        or scheme.casefold() != "bearer"
        or not hmac.compare_digest(
            supplied_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    ):
        await websocket.close(code=1008, reason="game_companion_unauthorized")
        return
    await serve_game_companion(
        websocket,
        session_factory=create_companion_session,
    )


async def serve_game_companion(
    websocket: WebSocket,
    *,
    session_factory: SessionFactory,
) -> None:
    order = ClientMessageOrder()
    session: CompanionSession | None = None
    send_lock = asyncio.Lock()

    async def emit(
        event: OutboundEvent,
        still_owned: Callable[[], bool] | None = None,
    ) -> None:
        async with send_lock:
            if still_owned is not None and not still_owned():
                return
            if isinstance(event, bytes):
                if not event:
                    raise ProtocolError(
                        "invalid_audio_frame", "outbound binary frame is empty"
                    )
                if len(event) > MAX_BINARY_FRAME_BYTES:
                    raise ProtocolError(
                        "audio_frame_too_large",
                        f"outbound binary frame exceeds {MAX_BINARY_FRAME_BYTES} bytes",
                    )
                await websocket.send_bytes(event)
                return
            encoded = event.model_dump_json()
            if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
                raise ProtocolError(
                    "message_too_large",
                    f"outbound JSON exceeds {MAX_JSON_BYTES} bytes",
                )
            await websocket.send_text(encoded)

    emit.checks_turn_ownership = True  # type: ignore[attr-defined]

    await websocket.accept()
    try:
        while True:
            order_checkpoint = None
            error_turn_id = None
            try:
                frame = await _receive_frame(websocket)
                if isinstance(frame, bytes):
                    order.accept_binary(len(frame))
                    if session is None or order.active_turn_id is None:
                        raise ProtocolError(
                            "hello_required", "hello must precede binary audio"
                        )
                    await session.append_audio(order.active_turn_id, frame)
                    continue

                message = parse_client_message(frame)
                if order.should_discard_retired_result(message):
                    continue
                order_checkpoint = order.checkpoint()
                error_turn_id = getattr(message, "turn_id", None)
                order.accept(message)
                if isinstance(message, Hello):
                    session = session_factory(emit)
                    session.set_client_capabilities(message.capabilities)
                    await emit(
                        HelloAck(
                            type="hello_ack",
                            protocol_version=PROTOCOL_VERSION,
                            session_id=str(uuid.uuid4()),
                            companion="Aster",
                            audio=AudioFormat(
                                sample_rate=16000,
                                channels=1,
                                format="pcm_s16le",
                            ),
                        )
                    )
                    continue

                if session is None:
                    raise ProtocolError(
                        "hello_required", "hello must be the first frame"
                    )
                if isinstance(message, TurnStart):
                    await session.start_turn(message.turn_id, message.context)
                elif isinstance(message, TurnEnd):
                    await session.end_turn(message.turn_id)
                elif isinstance(message, Interrupt):
                    await session.interrupt(message.turn_id)
                elif isinstance(message, ToolResult):
                    await session.submit_tool_result(message)
                elif isinstance(message, MemoryResult):
                    await session.submit_memory_result(message)
            except ProtocolError as exc:
                if not exc.recoverable:
                    raise
                if order_checkpoint is not None:
                    order.restore(order_checkpoint)
                # Recoverable errors are currently restricted to turn-scoped
                # control messages, whose turn ID identifies the error frame.
                if error_turn_id is None:
                    raise
                await _emit_protocol_error(emit, error_turn_id, exc)
    except WebSocketDisconnect:
        pass
    except ProtocolError as exc:
        if order.active_turn_id is not None and exc.code not in _OVERSIZE_CODES:
            await _emit_protocol_error(emit, order.active_turn_id, exc)
        close_code = 1009 if exc.code in _OVERSIZE_CODES else 1008
        await _close_socket(websocket, close_code, exc.code)
    except Exception:  # noqa: BLE001 - terminate the transport at its SDK boundary.
        logger.exception("Game companion WebSocket session failed")
        await _close_socket(websocket, 1011, "companion_session_failure")
    finally:
        if session is not None:
            with suppress(Exception):
                await session.close()


async def _receive_frame(websocket: WebSocket) -> object | bytes:
    event = await websocket.receive()
    if not isinstance(event, dict):
        raise ProtocolError("invalid_message", "invalid WebSocket event")
    event_type = event.get("type")
    if event_type == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=event.get("code", 1000),
            reason=event.get("reason", ""),
        )
    if event_type != "websocket.receive":
        raise ProtocolError("invalid_message", "invalid WebSocket event")

    binary = event.get("bytes")
    if binary is not None:
        return binary
    text = event.get("text")
    if not isinstance(text, str):
        raise ProtocolError("invalid_message", "control frame must be UTF-8 JSON")
    return decode_control_json(text)


async def _emit_protocol_error(
    emit: EmitCallback,
    turn_id: str,
    error: ProtocolError,
) -> None:
    with suppress(Exception):
        await emit(
            ErrorMessage(
                type="error",
                turn_id=turn_id,
                code=error.code,
                message=error.message[:1024],
                recoverable=error.recoverable,
            )
        )


async def _close_socket(websocket: WebSocket, code: int, reason: str) -> None:
    with suppress(Exception):
        await websocket.close(code=code, reason=reason)
