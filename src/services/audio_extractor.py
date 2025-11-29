import asyncio
import ffmpeg
from typing import AsyncGenerator, Optional
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class AudioExtractor:
    """Extracts audio from HLS stream and converts to PCM format for transcription."""

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

    async def start(self) -> AsyncGenerator[bytes, None]:
        """
        Extract audio from HLS stream and yield chunks.

        Yields:
            Audio chunks as bytes in PCM s16le format

        Raises:
            RuntimeError: If audio extraction fails
        """
        self._running = True
        logger.info(f"Starting audio extraction from {self.hls_url}")

        try:
            # Build FFmpeg command
            # Input: HLS stream
            # Output: PCM s16le, mono, 16kHz to stdout
            cmd = [
                'ffmpeg',
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
            while self._running:
                chunk = await self.process.stdout.read(chunk_size)

                if not chunk:
                    logger.info("End of audio stream reached")
                    break

                yield chunk

        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            raise RuntimeError(f"Failed to extract audio: {e}")

        finally:
            await self.cleanup()

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
