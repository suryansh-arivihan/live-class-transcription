import asyncio
import time
from typing import Dict, List, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from src.models.transcription import TranscriptionSegment
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class ChunkData:
    """Data for a 5-second chunk."""
    start_time: float
    end_time: float
    segments: List[TranscriptionSegment] = field(default_factory=list)
    text: str = ""
    words: List[dict] = field(default_factory=list)


class ChunkBuffer:
    """
    Buffer that aggregates transcription segments into 5-second chunks.

    Accumulates segments and flushes them every 5 seconds to a callback.
    """

    def __init__(
        self,
        stream_id: str,
        session_id: str,
        chunk_duration: float = 5.0,
        on_chunk_ready: Optional[Callable[[str, str, ChunkData], Awaitable[None]]] = None
    ):
        """
        Initialize chunk buffer.

        Args:
            stream_id: Stream unique identifier
            session_id: Transcription session ID
            chunk_duration: Duration of each chunk in seconds (default 5.0)
            on_chunk_ready: Async callback when a chunk is ready to be saved
        """
        self.stream_id = stream_id
        self.session_id = session_id
        self.chunk_duration = chunk_duration
        self.on_chunk_ready = on_chunk_ready

        self._current_chunk: Optional[ChunkData] = None
        self._chunk_start_time: Optional[float] = None
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the chunk buffer and periodic flush task."""
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info(f"Chunk buffer started for stream {self.stream_id}")

    async def stop(self):
        """Stop the chunk buffer and flush remaining data."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Flush any remaining data
        await self._flush_current_chunk()
        logger.info(f"Chunk buffer stopped for stream {self.stream_id}")

    async def add_segment(self, segment: TranscriptionSegment):
        """
        Add a transcription segment to the buffer.

        Args:
            segment: TranscriptionSegment to add
        """
        async with self._lock:
            current_time = time.time()

            # Initialize chunk if needed
            if self._current_chunk is None:
                self._chunk_start_time = current_time
                self._current_chunk = ChunkData(
                    start_time=segment.stream_time,
                    end_time=segment.stream_time
                )

            # Add segment to current chunk
            self._current_chunk.segments.append(segment)
            self._current_chunk.end_time = segment.stream_time

            # Update aggregated text (use final text or latest partial)
            if segment.is_final:
                # For final segments, append to text
                if self._current_chunk.text:
                    self._current_chunk.text += " " + segment.text.strip()
                else:
                    self._current_chunk.text = segment.text.strip()

                # Add words
                for word in segment.words:
                    self._current_chunk.words.append({
                        'text': word.text,
                        'start_time': word.start_time,
                        'end_time': word.end_time,
                        'confidence': word.confidence
                    })
            else:
                # For partial segments, keep the latest text
                # We'll finalize when flushing
                pass

    async def _periodic_flush(self):
        """Periodically flush chunks every chunk_duration seconds."""
        while self._running:
            try:
                await asyncio.sleep(self.chunk_duration)
                await self._flush_current_chunk()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic flush: {e}")

    async def _flush_current_chunk(self):
        """Flush the current chunk to the callback."""
        async with self._lock:
            if self._current_chunk is None or not self._current_chunk.segments:
                return

            chunk = self._current_chunk

            # If we have no final text, use the last segment's text
            if not chunk.text and chunk.segments:
                last_segment = chunk.segments[-1]
                chunk.text = last_segment.text.strip()
                for word in last_segment.words:
                    chunk.words.append({
                        'text': word.text,
                        'start_time': word.start_time,
                        'end_time': word.end_time,
                        'confidence': word.confidence
                    })

            # Reset for next chunk
            self._current_chunk = None
            self._chunk_start_time = None

        # Call the callback outside the lock
        if self.on_chunk_ready and chunk.text:
            try:
                await self.on_chunk_ready(self.stream_id, self.session_id, chunk)
                logger.debug(f"Flushed chunk for stream {self.stream_id}: {chunk.text[:50]}...")
            except Exception as e:
                logger.error(f"Error in chunk callback: {e}")


class ChunkBufferManager:
    """Manages chunk buffers for multiple streams."""

    def __init__(self):
        self._buffers: Dict[str, ChunkBuffer] = {}
        self._lock = asyncio.Lock()

    async def create_buffer(
        self,
        stream_id: str,
        session_id: str,
        on_chunk_ready: Callable[[str, str, ChunkData], Awaitable[None]]
    ) -> ChunkBuffer:
        """
        Create and start a chunk buffer for a stream.

        Args:
            stream_id: Stream unique identifier
            session_id: Transcription session ID
            on_chunk_ready: Callback when chunk is ready

        Returns:
            ChunkBuffer instance
        """
        async with self._lock:
            if stream_id in self._buffers:
                # Stop existing buffer
                await self._buffers[stream_id].stop()

            buffer = ChunkBuffer(
                stream_id=stream_id,
                session_id=session_id,
                on_chunk_ready=on_chunk_ready
            )
            await buffer.start()
            self._buffers[stream_id] = buffer
            return buffer

    async def get_buffer(self, stream_id: str) -> Optional[ChunkBuffer]:
        """Get buffer for a stream."""
        async with self._lock:
            return self._buffers.get(stream_id)

    async def remove_buffer(self, stream_id: str):
        """Stop and remove buffer for a stream."""
        async with self._lock:
            if stream_id in self._buffers:
                await self._buffers[stream_id].stop()
                del self._buffers[stream_id]
                logger.info(f"Removed chunk buffer for stream {stream_id}")


# Global instance
chunk_buffer_manager = ChunkBufferManager()
