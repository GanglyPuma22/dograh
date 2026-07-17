from hashlib import sha256
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import TranscriptionFrame, TTSTextFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from api.services.pipecat.realtime_feedback_observer import RealtimeFeedbackObserver


def _frame_pushed(frame, direction, *, source=None):
    return FramePushed(
        source=source or SimpleNamespace(),
        destination=SimpleNamespace(),
        frame=frame,
        direction=direction,
        timestamp=0,
    )


@pytest.mark.asyncio
async def test_observer_streams_upstream_only_transcription_frames():
    messages = []

    async def ws_sender(message):
        messages.append(message)

    observer = RealtimeFeedbackObserver(ws_sender=ws_sender)
    frame = TranscriptionFrame(
        "Hi there",
        user_id="user-1",
        timestamp="2026-01-01T00:00:00+00:00",
    )

    await observer.on_push_frame(_frame_pushed(frame, FrameDirection.UPSTREAM))

    assert messages == [
        {
            "type": "rtf-user-transcription",
            "payload": {
                "text": "Hi there",
                "final": True,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "user_id": "user-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_observer_ignores_upstream_broadcast_transcription_sibling():
    messages = []

    async def ws_sender(message):
        messages.append(message)

    observer = RealtimeFeedbackObserver(ws_sender=ws_sender)
    frame = TranscriptionFrame(
        "Hi there",
        user_id="user-1",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    frame.broadcast_sibling_id = 1234

    await observer.on_push_frame(_frame_pushed(frame, FrameDirection.UPSTREAM))

    assert messages == []


@pytest.mark.asyncio
async def test_observer_waits_for_tts_text_from_output_transport():
    messages = []

    async def ws_sender(message):
        messages.append(message)

    observer = RealtimeFeedbackObserver(ws_sender=ws_sender)
    frame = TTSTextFrame("Hello", aggregated_by="word")
    frame.pts = 123

    await observer.on_push_frame(_frame_pushed(frame, FrameDirection.DOWNSTREAM))
    assert messages == []

    output_transport = BaseOutputTransport(TransportParams())
    await observer.on_push_frame(
        _frame_pushed(
            frame,
            FrameDirection.DOWNSTREAM,
            source=output_transport,
        )
    )

    assert messages == [
        {
            "type": "rtf-bot-text",
            "payload": {"text": "Hello"},
        }
    ]


@pytest.mark.asyncio
async def test_jeeves_timing_uses_stable_trace_and_excludes_transcript_payload():
    messages = []

    async def ws_sender(message):
        messages.append(message)

    observer = RealtimeFeedbackObserver(
        ws_sender=ws_sender,
        workflow_run_id=42,
        now_unix_ms=lambda: 1_250,
        started_at_unix_ms=1_000,
    )
    await observer.emit_media_start()
    await observer.on_push_frame(
        _frame_pushed(
            TranscriptionFrame(
                "never persist this transcript",
                user_id="secret-user",
                timestamp="2026-01-01T00:00:00+00:00",
            ),
            FrameDirection.DOWNSTREAM,
        )
    )

    conversation_id = f"vs_{sha256(b'42').hexdigest()}"
    trace_id = sha256(conversation_id.encode()).hexdigest()
    timing = [message for message in messages if message["type"] == "rtf-jeeves-timing"]
    assert timing == [
        {
            "type": "rtf-jeeves-timing",
            "payload": {
                "schema_version": 1,
                "trace_id": trace_id,
                "stage": "media_start",
                "observed_at_unix_ms": 1_250,
                "elapsed_ms": 250,
            },
        },
        {
            "type": "rtf-jeeves-timing",
            "payload": {
                "schema_version": 1,
                "trace_id": trace_id,
                "stage": "stt_final",
                "observed_at_unix_ms": 1_250,
                "elapsed_ms": 250,
            },
        },
    ]
    assert "never persist this transcript" not in str(timing)
    assert "secret-user" not in str(timing)
