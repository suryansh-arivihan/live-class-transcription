import asyncio
import time
from fastapi import APIRouter, HTTPException, status
from src.models.transcription import (
    TranscriptionStartRequest,
    TranscriptionStartResponse,
    TranscriptionStopRequest,
    HealthResponse
)
from src.services.stream_manager import stream_manager
from src.services.transcription import TranscriptionService
from src.services.chunk_buffer import chunk_buffer_manager, ChunkData
from src.services.dynamodb_client import dynamodb_client
from src.utils.validators import validate_unique_id, build_hls_url, validate_stream_availability
from src.utils.logger import setup_logger, set_log_context
from src.config import settings

logger = setup_logger(__name__)


async def _save_chunk_to_dynamodb(stream_id: str, session_id: str, chunk: ChunkData):
    """
    Callback to save a chunk to DynamoDB.

    Args:
        stream_id: Stream unique identifier
        session_id: Session ID
        chunk: ChunkData with aggregated transcription
    """
    chunk_timestamp = int(time.time() * 1000)  # Unix timestamp in milliseconds

    success = await dynamodb_client.save_chunk(
        stream_id=stream_id,
        session_id=session_id,
        chunk_timestamp=chunk_timestamp,
        start_time=chunk.start_time,
        end_time=chunk.end_time,
        text=chunk.text,  # UTF-8 encoded, supports Hindi/Devanagari
        words=chunk.words,
        is_final=True
    )

    if success:
        logger.info(
            "Saved 5s chunk to DynamoDB",
            extra={
                "stream_id": stream_id,
                "session_id": session_id,
                "chunk_timestamp": chunk_timestamp,
                "text_preview": chunk.text[:50] if chunk.text else "",
                "word_count": len(chunk.words) if chunk.words else 0,
            },
        )
    else:
        logger.error(
            "Failed to save chunk to DynamoDB",
            extra={
                "stream_id": stream_id,
                "session_id": session_id,
                "chunk_timestamp": chunk_timestamp,
            },
        )


router = APIRouter(prefix="/api/v1", tags=["transcription"])


@router.post(
    "/transcribe/start",
    response_model=TranscriptionStartResponse,
    status_code=status.HTTP_200_OK
)
async def start_transcription(request: TranscriptionStartRequest):
    """
    Start transcription for a live stream.

    Args:
        request: Transcription start request with unique_id and options

    Returns:
        TranscriptionStartResponse with session details

    Raises:
        HTTPException: If validation fails or stream cannot be started
    """
    unique_id = request.unique_id
    options = request.options

    set_log_context(unique_id=unique_id)

    logger.info(
        "Received start transcription request",
        extra={
            "unique_id": unique_id,
            "language": getattr(options, "language", None),
        },
    )

    # Validate unique_id format
    if not validate_unique_id(unique_id):
        logger.warning(
            "Invalid unique_id format",
            extra={"unique_id": unique_id},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid unique_id format"
        )

    # Build HLS URL
    hls_url = build_hls_url(unique_id)

    # Validate stream availability
    is_available = await validate_stream_availability(hls_url)
    if not is_available:
        logger.warning(
            "Stream not available",
            extra={"unique_id": unique_id, "hls_url": hls_url},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stream not found or not available at {hls_url}"
        )

    # Check if session already exists
    existing_session = await stream_manager.get_session(unique_id)
    if existing_session:
        logger.warning(
            "Transcription already active for stream",
            extra={
                "unique_id": unique_id,
                "existing_session_id": existing_session.session_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transcription already active for stream {unique_id}"
        )

    try:
        # Create session
        session = await stream_manager.create_session(unique_id, hls_url)
        set_log_context(session_id=session.session_id)

        # Create chunk buffer for 5-second DynamoDB storage
        await chunk_buffer_manager.create_buffer(
            stream_id=unique_id,
            session_id=session.session_id,
            on_chunk_ready=_save_chunk_to_dynamodb
        )
        logger.info(
            "Created chunk buffer for DynamoDB storage",
            extra={
                "unique_id": unique_id,
                "session_id": session.session_id,
            },
        )

        # Start transcription service in background
        transcription_service = TranscriptionService(unique_id, hls_url, options)
        task = asyncio.create_task(_run_transcription(transcription_service, session.session_id))
        await stream_manager.set_session_task(unique_id, task)

        # Build stream URL using PUBLIC_HOST (not the bind address)
        stream_url = f"ws://{settings.PUBLIC_HOST}:{settings.PORT}/api/v1/ws/transcribe/{unique_id}"

        logger.info(
            "Started transcription session",
            extra={
                "unique_id": unique_id,
                "session_id": session.session_id,
                "hls_url": hls_url,
                "stream_url": stream_url,
            },
        )

        return TranscriptionStartResponse(
            session_id=session.session_id,
            status="started",
            stream_url=stream_url
        )

    except RuntimeError as e:
        logger.error(
            "Failed to start transcription (runtime)",
            extra={"unique_id": unique_id, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except Exception as e:
        logger.exception(
            "Unexpected error starting transcription",
            extra={"unique_id": unique_id, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start transcription"
        )


@router.post(
    "/transcribe/stop",
    status_code=status.HTTP_200_OK
)
async def stop_transcription(request: TranscriptionStopRequest):
    """
    Stop transcription for a stream.

    Args:
        request: Transcription stop request with unique_id

    Returns:
        Success message

    Raises:
        HTTPException: If stream not found
    """
    unique_id = request.unique_id

    set_log_context(unique_id=unique_id)

    logger.info(
        "Received stop transcription request",
        extra={"unique_id": unique_id},
    )

    # Check if session exists
    session = await stream_manager.get_session(unique_id)
    if not session:
        logger.warning(
            "No active transcription for stream",
            extra={"unique_id": unique_id},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active transcription found for stream {unique_id}"
        )

    set_log_context(session_id=session.session_id)

    try:
        # Remove chunk buffer (flushes remaining data)
        await chunk_buffer_manager.remove_buffer(unique_id)

        # Remove session (this will cancel the task)
        await stream_manager.remove_session(unique_id)

        logger.info(
            "Stopped transcription",
            extra={
                "unique_id": unique_id,
                "session_id": session.session_id,
            },
        )

        return {"status": "stopped", "unique_id": unique_id}

    except Exception as e:
        logger.exception(
            "Error stopping transcription",
            extra={"unique_id": unique_id, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to stop transcription"
        )


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK
)
async def health_check():
    """
    Health check endpoint.

    Returns:
        HealthResponse with service status
    """
    sessions = await stream_manager.get_all_sessions()

    return HealthResponse(
        status="healthy",
        active_streams=len(sessions),
        version=settings.VERSION
    )


@router.get(
    "/sessions",
    status_code=status.HTTP_200_OK
)
async def list_sessions():
    """
    List all active transcription sessions.

    Returns:
        List of active sessions
    """
    sessions = await stream_manager.get_all_sessions()
    return {"sessions": sessions}


async def _run_transcription(transcription_service: TranscriptionService, session_id: str):
    """
    Background task to run transcription service.

    Args:
        transcription_service: TranscriptionService instance
        session_id: Session ID for the transcription
    """
    unique_id = transcription_service.unique_id
    set_log_context(unique_id=unique_id, session_id=session_id)

    segment_count = 0
    logger.info(
        "Transcription task started",
        extra={"unique_id": unique_id, "session_id": session_id},
    )

    try:
        async for segment in transcription_service.start():
            # Broadcast segment to all connected clients
            await stream_manager.broadcast_segment(unique_id, segment)
            segment_count += 1
            if segment_count <= 5 or segment_count % 50 == 0:
                logger.debug(
                    "Broadcasted segment",
                    extra={
                        "unique_id": unique_id,
                        "session_id": session_id,
                        "segment_number": segment_count,
                    },
                )

            # Add segment to chunk buffer for DynamoDB storage
            buffer = await chunk_buffer_manager.get_buffer(unique_id)
            if buffer:
                await buffer.add_segment(segment)
    except asyncio.CancelledError:
        logger.info(
            "Transcription task cancelled",
            extra={
                "unique_id": unique_id,
                "session_id": session_id,
                "segments_processed": segment_count,
            },
        )
        raise
    except Exception as e:
        logger.exception(
            "Transcription task error",
            extra={
                "unique_id": unique_id,
                "session_id": session_id,
                "segments_processed": segment_count,
                "error": str(e),
            },
        )
    finally:
        logger.info(
            "Transcription task cleaning up",
            extra={
                "unique_id": unique_id,
                "session_id": session_id,
                "total_segments": segment_count,
            },
        )
        # Cleanup chunk buffer
        await chunk_buffer_manager.remove_buffer(unique_id)
        # Cleanup session
        await stream_manager.remove_session(unique_id)
        logger.info(
            "Transcription task finished and cleaned up",
            extra={"unique_id": unique_id, "session_id": session_id},
        )
