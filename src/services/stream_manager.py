import asyncio
import uuid
from typing import Dict, Optional, Set
from datetime import datetime
from src.models.stream import StreamSession, StreamStatus, StreamInfo
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class StreamSessionManager:
    """Manages lifecycle of active transcription sessions."""

    def __init__(self, max_concurrent: int = None):
        """
        Initialize stream session manager.

        Args:
            max_concurrent: Maximum concurrent streams (defaults to settings)
        """
        self.max_concurrent = max_concurrent or settings.MAX_CONCURRENT_STREAMS
        self.active_sessions: Dict[str, StreamSession] = {}
        self.session_tasks: Dict[str, asyncio.Task] = {}
        self.session_clients: Dict[str, Set] = {}  # Track connected clients per session
        self.session_queues: Dict[str, list] = {}  # Queues for broadcasting segments
        self._lock = asyncio.Lock()

    async def create_session(self, unique_id: str, hls_url: str) -> StreamSession:
        """
        Create new transcription session.

        Args:
            unique_id: Unique stream identifier
            hls_url: HLS stream URL

        Returns:
            Created StreamSession

        Raises:
            RuntimeError: If max concurrent streams reached
            ValueError: If stream already active
        """
        async with self._lock:
            # Check if already exists
            if unique_id in self.active_sessions:
                raise ValueError(f"Stream {unique_id} already has an active session")

            # Check concurrent limit
            if len(self.active_sessions) >= self.max_concurrent:
                raise RuntimeError(
                    f"Maximum concurrent streams reached ({self.max_concurrent})"
                )

            # Create new session
            session = StreamSession(
                session_id=str(uuid.uuid4()),
                unique_id=unique_id,
                status=StreamStatus.PENDING,
                started_at=datetime.utcnow(),
                hls_url=hls_url
            )

            self.active_sessions[unique_id] = session
            self.session_clients[unique_id] = set()

            logger.info(f"Created session {session.session_id} for stream {unique_id}")
            return session

    async def get_session(self, unique_id: str) -> Optional[StreamSession]:
        """
        Get active session by unique_id.

        Args:
            unique_id: Stream unique identifier

        Returns:
            StreamSession if found, None otherwise
        """
        return self.active_sessions.get(unique_id)

    async def update_session_status(
        self, unique_id: str, status: StreamStatus, error: str = None
    ):
        """
        Update session status.

        Args:
            unique_id: Stream unique identifier
            status: New status
            error: Error message if status is ERROR
        """
        session = self.active_sessions.get(unique_id)
        if session:
            session.status = status
            if error:
                session.error = error
            if status in [StreamStatus.STOPPED, StreamStatus.ERROR]:
                session.stopped_at = datetime.utcnow()

            logger.info(f"Session {unique_id} status updated to {status}")

    async def remove_session(self, unique_id: str):
        """
        Remove session from active sessions.

        Args:
            unique_id: Stream unique identifier
        """
        async with self._lock:
            if unique_id in self.active_sessions:
                session = self.active_sessions.pop(unique_id)

                # Cancel associated task if exists
                if unique_id in self.session_tasks:
                    task = self.session_tasks.pop(unique_id)
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                # Remove clients
                if unique_id in self.session_clients:
                    self.session_clients.pop(unique_id)

                logger.info(f"Removed session {session.session_id} for stream {unique_id}")

    async def set_session_task(self, unique_id: str, task: asyncio.Task):
        """
        Associate a task with a session.

        Args:
            unique_id: Stream unique identifier
            task: Asyncio task
        """
        self.session_tasks[unique_id] = task

    async def add_client(self, unique_id: str, client):
        """
        Add a connected client to session.

        Args:
            unique_id: Stream unique identifier
            client: Client connection object
        """
        if unique_id in self.session_clients:
            self.session_clients[unique_id].add(client)
            logger.info(
                f"Client added to session {unique_id}. "
                f"Total clients: {len(self.session_clients[unique_id])}"
            )

    async def remove_client(self, unique_id: str, client):
        """
        Remove a client from session.

        Args:
            unique_id: Stream unique identifier
            client: Client connection object
        """
        if unique_id in self.session_clients:
            self.session_clients[unique_id].discard(client)
            logger.info(
                f"Client removed from session {unique_id}. "
                f"Remaining clients: {len(self.session_clients[unique_id])}"
            )

    async def get_client_count(self, unique_id: str) -> int:
        """
        Get number of connected clients for a session.

        Args:
            unique_id: Stream unique identifier

        Returns:
            Number of connected clients
        """
        return len(self.session_clients.get(unique_id, set()))

    async def register_queue(self, unique_id: str) -> asyncio.Queue:
        """
        Register a new queue for receiving transcription segments.

        Args:
            unique_id: Stream unique identifier

        Returns:
            Queue for receiving segments
        """
        if unique_id not in self.session_queues:
            self.session_queues[unique_id] = []

        queue = asyncio.Queue()
        self.session_queues[unique_id].append(queue)
        logger.info(f"Registered new queue for session {unique_id}")
        return queue

    async def unregister_queue(self, unique_id: str, queue: asyncio.Queue):
        """
        Unregister a queue from session.

        Args:
            unique_id: Stream unique identifier
            queue: Queue to unregister
        """
        if unique_id in self.session_queues:
            try:
                self.session_queues[unique_id].remove(queue)
                logger.info(f"Unregistered queue from session {unique_id}")
            except ValueError:
                pass

    async def broadcast_segment(self, unique_id: str, segment):
        """
        Broadcast a transcription segment to all registered queues.

        Args:
            unique_id: Stream unique identifier
            segment: Transcription segment to broadcast
        """
        if unique_id in self.session_queues:
            for queue in self.session_queues[unique_id]:
                try:
                    await queue.put(segment)
                except Exception as e:
                    logger.error(f"Error broadcasting segment to queue: {e}")

    async def get_all_sessions(self) -> list[StreamInfo]:
        """
        Get information about all active sessions.

        Returns:
            List of StreamInfo objects
        """
        sessions = []
        for unique_id, session in self.active_sessions.items():
            uptime = None
            if session.started_at:
                uptime = (datetime.utcnow() - session.started_at).total_seconds()

            sessions.append(
                StreamInfo(
                    session_id=session.session_id,
                    unique_id=session.unique_id,
                    status=session.status,
                    started_at=session.started_at,
                    uptime_seconds=uptime,
                    connected_clients=await self.get_client_count(unique_id)
                )
            )
        return sessions

    async def cleanup_all(self):
        """Clean up all active sessions."""
        logger.info("Cleaning up all active sessions")
        unique_ids = list(self.active_sessions.keys())
        for unique_id in unique_ids:
            await self.remove_session(unique_id)


# Global stream manager instance
stream_manager = StreamSessionManager()
