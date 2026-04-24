import asyncio
import json
from fastapi import APIRouter, HTTPException, status
from sse_starlette.sse import EventSourceResponse
from src.services.stream_manager import stream_manager
from src.utils.validators import validate_unique_id
from src.utils.logger import setup_logger, set_log_context

logger = setup_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sse"])


@router.get("/transcribe/{unique_id}/stream")
async def sse_transcribe(unique_id: str):
    """
    Server-Sent Events (SSE) endpoint for transcription streaming.

    Args:
        unique_id: Stream unique identifier

    Returns:
        EventSourceResponse streaming transcription segments

    Raises:
        HTTPException: If validation fails or session not found
    """
    set_log_context(unique_id=unique_id)
    logger.info(
        "SSE client connecting",
        extra={"unique_id": unique_id},
    )

    # Validate unique_id
    if not validate_unique_id(unique_id):
        logger.warning(
            "SSE rejected: invalid unique_id",
            extra={"unique_id": unique_id},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid unique_id format"
        )

    # Check if session exists
    session = await stream_manager.get_session(unique_id)
    if not session:
        logger.warning(
            "SSE rejected: session not found",
            extra={"unique_id": unique_id},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active transcription for stream {unique_id}"
        )

    set_log_context(session_id=session.session_id)

    async def event_generator():
        """
        Generate SSE events with transcription segments.

        Yields:
            Transcription segments as SSE events
        """
        set_log_context(unique_id=unique_id, session_id=session.session_id)
        queue = None
        event_count = 0
        try:
            # Send initial connection message
            yield {
                "event": "connected",
                "data": json.dumps({
                    "unique_id": unique_id,
                    "session_id": session.session_id,
                    "status": "connected"
                })
            }

            # Register a queue for this client
            queue = await stream_manager.register_queue(unique_id)
            logger.info(
                "SSE queue registered",
                extra={
                    "unique_id": unique_id,
                    "session_id": session.session_id,
                },
            )

            # Listen for segments from the queue
            while True:
                try:
                    # Wait for segment with timeout for heartbeat
                    segment = await asyncio.wait_for(queue.get(), timeout=5.0)

                    # Convert segment to JSON and send
                    data = segment.model_dump(mode='json')
                    yield {
                        "event": "transcription",
                        "data": json.dumps(data)
                    }
                    event_count += 1
                    if event_count <= 3 or event_count % 100 == 0:
                        logger.debug(
                            "SSE transcription event sent",
                            extra={
                                "unique_id": unique_id,
                                "session_id": session.session_id,
                                "event_count": event_count,
                            },
                        )

                except asyncio.TimeoutError:
                    # Send heartbeat if no segment received
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": "now"})
                    }

                    # Check if session is still active
                    current_session = await stream_manager.get_session(unique_id)
                    if not current_session:
                        logger.info(
                            "SSE session ended — closing stream",
                            extra={
                                "unique_id": unique_id,
                                "session_id": session.session_id,
                                "event_count": event_count,
                            },
                        )
                        yield {
                            "event": "end",
                            "data": json.dumps({
                                "unique_id": unique_id,
                                "status": "ended"
                            })
                        }
                        break

        except asyncio.CancelledError:
            logger.info(
                "SSE stream cancelled",
                extra={
                    "unique_id": unique_id,
                    "session_id": session.session_id,
                    "event_count": event_count,
                },
            )
        except Exception as e:
            logger.exception(
                "SSE error",
                extra={
                    "unique_id": unique_id,
                    "session_id": session.session_id,
                    "event_count": event_count,
                    "error": str(e),
                },
            )
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }
        finally:
            # Unregister queue
            if queue:
                await stream_manager.unregister_queue(unique_id, queue)
            logger.info(
                "SSE stream closed",
                extra={
                    "unique_id": unique_id,
                    "session_id": session.session_id,
                    "event_count": event_count,
                },
            )

    return EventSourceResponse(event_generator())
