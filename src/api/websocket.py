import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from src.services.stream_manager import stream_manager
from src.services.transcription import TranscriptionService
from src.utils.validators import validate_unique_id, build_hls_url
from src.utils.logger import setup_logger

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
    await websocket.accept()
    logger.info(f"WebSocket client connected for stream {unique_id}")

    # Validate unique_id
    if not validate_unique_id(unique_id):
        await websocket.send_json({
            "error": "Invalid unique_id format",
            "code": "INVALID_ID"
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        # Check if session exists
        session = await stream_manager.get_session(unique_id)

        if not session:
            await websocket.send_json({
                "error": f"No active transcription for stream {unique_id}",
                "code": "SESSION_NOT_FOUND"
            })
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Add client to session
        await stream_manager.add_client(unique_id, websocket)

        # Register a queue for this client
        queue = await stream_manager.register_queue(unique_id)

        # Start a task to send messages from queue to WebSocket
        send_task = asyncio.create_task(
            _send_transcriptions(websocket, queue)
        )

        # Wait for either task to complete or connection to close
        try:
            # Keep connection alive and listen for client messages
            while True:
                try:
                    # Check if session is still active
                    current_session = await stream_manager.get_session(unique_id)
                    if not current_session:
                        logger.info(f"Session {unique_id} ended")
                        break

                    # Wait for a short period
                    await asyncio.sleep(1)

                except WebSocketDisconnect:
                    logger.info(f"WebSocket client disconnected for {unique_id}")
                    break

        finally:
            # Cancel send task
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"WebSocket error for {unique_id}: {e}")
        await websocket.send_json({
            "error": "Internal server error",
            "code": "SERVER_ERROR"
        })

    finally:
        # Unregister queue and remove client from session
        await stream_manager.unregister_queue(unique_id, queue)
        await stream_manager.remove_client(unique_id, websocket)
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"WebSocket connection closed for {unique_id}")


async def _send_transcriptions(websocket: WebSocket, queue: asyncio.Queue):
    """
    Send transcription segments from queue to WebSocket.

    Args:
        websocket: WebSocket connection
        queue: Queue to get transcription segments from
    """
    try:
        while True:
            segment = await queue.get()

            # Convert segment to JSON
            data = segment.model_dump(mode='json')

            # Send to client
            await websocket.send_json(data)

    except asyncio.CancelledError:
        logger.info("Transcription sender cancelled")
    except Exception as e:
        logger.error(f"Error sending transcription: {e}")
