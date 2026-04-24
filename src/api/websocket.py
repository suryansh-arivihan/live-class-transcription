import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from src.services.stream_manager import stream_manager
from src.services.transcription import TranscriptionService
from src.utils.validators import validate_unique_id, build_hls_url
from src.utils.logger import setup_logger, set_log_context

logger = setup_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["websocket"])


@router.websocket("/ws/transcribe/{unique_id}")
async def websocket_transcribe(websocket: WebSocket, unique_id: str):
    """
    WebSocket endpoint for real-time transcription streaming.

    Args:
        websocket: WebSocket connection
        unique_id: Stream unique identifier

    The client will receive JSON messages with transcription segments.
    """
    set_log_context(unique_id=unique_id)
    client_ip = websocket.client.host if websocket.client else None

    await websocket.accept()
    logger.info(
        "WebSocket client connected",
        extra={"unique_id": unique_id, "client_ip": client_ip},
    )

    # Validate unique_id
    if not validate_unique_id(unique_id):
        logger.warning(
            "WebSocket rejected: invalid unique_id",
            extra={"unique_id": unique_id, "client_ip": client_ip},
        )
        await websocket.send_json({
            "error": "Invalid unique_id format",
            "code": "INVALID_ID"
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    queue = None
    try:
        # Check if session exists
        session = await stream_manager.get_session(unique_id)

        if not session:
            logger.warning(
                "WebSocket rejected: session not found",
                extra={"unique_id": unique_id, "client_ip": client_ip},
            )
            await websocket.send_json({
                "error": f"No active transcription for stream {unique_id}",
                "code": "SESSION_NOT_FOUND"
            })
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        set_log_context(session_id=session.session_id)

        # Add client to session
        await stream_manager.add_client(unique_id, websocket)

        # Register a queue for this client
        queue = await stream_manager.register_queue(unique_id)
        logger.info(
            "WebSocket queue registered",
            extra={
                "unique_id": unique_id,
                "session_id": session.session_id,
                "client_ip": client_ip,
            },
        )

        # Start a task to send messages from queue to WebSocket
        send_task = asyncio.create_task(
            _send_transcriptions(websocket, queue, unique_id, session.session_id)
        )

        # Wait for either task to complete or connection to close
        try:
            # Keep connection alive and listen for client messages
            while True:
                try:
                    # Check if session is still active
                    current_session = await stream_manager.get_session(unique_id)
                    if not current_session:
                        logger.info(
                            "Session ended",
                            extra={
                                "unique_id": unique_id,
                                "session_id": session.session_id,
                            },
                        )
                        break

                    # Wait for a short period
                    await asyncio.sleep(1)

                except WebSocketDisconnect:
                    logger.info(
                        "WebSocket client disconnected",
                        extra={
                            "unique_id": unique_id,
                            "session_id": session.session_id,
                            "client_ip": client_ip,
                        },
                    )
                    break

        finally:
            # Cancel send task
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.exception(
            "WebSocket error",
            extra={"unique_id": unique_id, "error": str(e)},
        )
        try:
            await websocket.send_json({
                "error": "Internal server error",
                "code": "SERVER_ERROR"
            })
        except Exception:
            pass

    finally:
        # Unregister queue and remove client from session
        if queue is not None:
            await stream_manager.unregister_queue(unique_id, queue)
        await stream_manager.remove_client(unique_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(
            "WebSocket connection closed",
            extra={"unique_id": unique_id, "client_ip": client_ip},
        )


async def _send_transcriptions(
    websocket: WebSocket,
    queue: asyncio.Queue,
    unique_id: str = None,
    session_id: str = None,
):
    """
    Send transcription segments from queue to WebSocket.

    Args:
        websocket: WebSocket connection
        queue: Queue to get transcription segments from
        unique_id: Stream unique identifier (for logging)
        session_id: Session identifier (for logging)
    """
    if unique_id or session_id:
        set_log_context(unique_id=unique_id, session_id=session_id)

    sent_count = 0
    try:
        while True:
            segment = await queue.get()

            # Convert segment to JSON
            data = segment.model_dump(mode='json')

            # Send to client
            await websocket.send_json(data)
            sent_count += 1
            if sent_count <= 3 or sent_count % 100 == 0:
                logger.debug(
                    "Sent transcription segment over WebSocket",
                    extra={
                        "unique_id": unique_id,
                        "session_id": session_id,
                        "sent_count": sent_count,
                    },
                )

    except asyncio.CancelledError:
        logger.info(
            "Transcription sender cancelled",
            extra={
                "unique_id": unique_id,
                "session_id": session_id,
                "sent_count": sent_count,
            },
        )
    except Exception as e:
        logger.exception(
            "Error sending transcription over WebSocket",
            extra={
                "unique_id": unique_id,
                "session_id": session_id,
                "sent_count": sent_count,
                "error": str(e),
            },
        )
