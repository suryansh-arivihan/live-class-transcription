import asyncio
import ffmpeg
from typing import AsyncGenerator, Optional
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class AudioExtractor:
    """Extracts audio from HLS stream and converts to PCM format for transcription."""

    # Retry configuration
    MAX_RETRIES = 5
    INITIAL_RETRY_DELAY = 1.0  # seconds
    MAX_RETRY_DELAY = 30.0  # seconds
    BACKOFF_MULTIPLIER = 2.0

    def __init__(self, hls_url: str, sample_rate: int = None):
        """
        Initialize audio extractor.

        Args:
            hls_url: URL to HLS stream
            sample_rate: Target sample rate (defaults to settings)
        """
        self.hls_url = hls_url
        self.sample_rate = sample_rate or settings.SONIOX_SAMPLE_RATE
        self.process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._consecutive_failures = 0
        self._total_bytes_read = 0

    async def start(self) -> AsyncGenerator[bytes, None]:
        """
        Extract audio from HLS stream and yield chunks with exponential backoff retry.

        Yields:
            Audio chunks as bytes in PCM s16le format

        Raises:
            RuntimeError: If audio extraction fails after all retries
        """
        self._running = True
        self._consecutive_failures = 0
        retry_delay = self.INITIAL_RETRY_DELAY

        logger.info(f"Starting audio extraction from {self.hls_url}")

        while self._running:
            try:
                async for chunk in self._extract_audio():
                    yield chunk
                    # Reset failures on successful read
                    self._consecutive_failures = 0
                    retry_delay = self.INITIAL_RETRY_DELAY

                # Clean exit - stream ended normally
                logger.info("Audio stream ended gracefully")
                break

            except Exception as e:
                self._consecutive_failures += 1
                await self.cleanup()

                if self._consecutive_failures >= self.MAX_RETRIES:
                    logger.error(
                        f"Audio extraction failed after {self.MAX_RETRIES} retries. "
                        f"Stopping gracefully. Last error: {e}"
                    )
                    break

                logger.warning(
                    f"Audio extraction error (attempt {self._consecutive_failures}/{self.MAX_RETRIES}): {e}. "
                    f"Retrying in {retry_delay:.1f}s..."
                )

                await asyncio.sleep(retry_delay)

                # Exponential backoff
                retry_delay = min(retry_delay * self.BACKOFF_MULTIPLIER, self.MAX_RETRY_DELAY)

        await self.cleanup()

    async def _extract_audio(self) -> AsyncGenerator[bytes, None]:
        """
        Internal method to extract audio from HLS stream.

        Yields:
            Audio chunks as bytes in PCM s16le format
        """
        # Build FFmpeg command
        # Input: HLS stream
        # Output: PCM s16le, mono, 16kHz to stdout
        cmd = [
            'ffmpeg',
            '-reconnect', '1',  # Reconnect on network errors
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', self.hls_url,
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ac', '1',  # mono
            '-ar', str(self.sample_rate),
            '-loglevel', 'error',
            '-'  # output to stdout
        ]

        # Start FFmpeg process
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        logger.info("FFmpeg process started successfully")

        # Read audio chunks from stdout
        chunk_size = settings.AUDIO_CHUNK_SIZE
        empty_read_count = 0
        max_empty_reads = 10  # Allow some empty reads before considering stream ended

        while self._running:
            try:
                chunk = await asyncio.wait_for(
                    self.process.stdout.read(chunk_size),
                    timeout=30.0  # 30 second timeout for each read
                )

                if not chunk:
                    empty_read_count += 1
                    if empty_read_count >= max_empty_reads:
                        # Check if process is still running
                        if self.process.returncode is not None:
                            stderr = await self.process.stderr.read()
                            if stderr:
                                logger.warning(f"FFmpeg stderr: {stderr.decode()}")
                            logger.info("End of audio stream reached (process exited)")
                            return
                        # Process still running but no data - might be buffering
                        await asyncio.sleep(0.1)
                        continue
                    continue

                empty_read_count = 0
                self._total_bytes_read += len(chunk)
                yield chunk

            except asyncio.TimeoutError:
                logger.warning("Audio read timeout - stream may be stalled")
                raise RuntimeError("Audio stream read timeout")

    async def stop(self):
        """Stop audio extraction."""
        logger.info("Stopping audio extraction")
        self._running = False
        await self.cleanup()

    async def cleanup(self):
        """Clean up FFmpeg process."""
        if self.process:
            try:
                if self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.warning("FFmpeg did not terminate gracefully, killing process")
                        self.process.kill()
                        await self.process.wait()

                logger.info("FFmpeg process cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg process: {e}")

            self.process = None

    def get_stats(self) -> dict:
        """Get extraction statistics."""
        return {
            "total_bytes_read": self._total_bytes_read,
            "consecutive_failures": self._consecutive_failures,
            "is_running": self._running
        }
