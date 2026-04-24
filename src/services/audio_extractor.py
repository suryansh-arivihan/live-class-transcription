import asyncio
import time
import aiohttp
import m3u8
from typing import AsyncGenerator, Optional, Tuple
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


async def fetch_hls_playlist(hls_url: str) -> Optional[Tuple[Optional[int], bool]]:
    """
    Fetch media_sequence and is_endlist from an HLS m3u8 playlist URL.

    Args:
        hls_url: URL to the m3u8 playlist

    Returns:
        (media_sequence, is_endlist) tuple, or None on error
    """
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                hls_url,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    logger.debug(
                        "HLS playlist fetch non-200",
                        extra={"hls_url": hls_url, "http_status": response.status},
                    )
                    return None
                content = await response.text()
                playlist = m3u8.loads(content)
                return playlist.media_sequence, playlist.is_endlist
    except Exception as e:
        logger.debug(
            "HLS playlist fetch failed",
            extra={"hls_url": hls_url, "error": str(e)},
        )
        return None


class AudioExtractor:
    """Extracts audio from HLS stream and converts to PCM format for transcription."""

    # Retry configuration (for FFmpeg errors / network failures)
    MAX_RETRIES = 5
    INITIAL_RETRY_DELAY = 1.0  # seconds
    MAX_RETRY_DELAY = 30.0  # seconds
    BACKOFF_MULTIPLIER = 2.0

    # Polling interval when waiting for new HLS segments
    POLL_INTERVAL = 3.0  # seconds between m3u8 checks

    def __init__(self, hls_url: str, sample_rate: int = None, reconnect_timeout: Optional[int] = None):
        """
        Initialize audio extractor.

        Args:
            hls_url: URL to HLS stream
            sample_rate: Target sample rate (defaults to settings)
            reconnect_timeout: Seconds to poll m3u8 for new segments when
                stream goes silent. Defaults to settings.STREAM_RECONNECT_TIMEOUT.
                Pass 0 to skip the reconnect wait entirely.
        """
        self.hls_url = hls_url
        self.sample_rate = sample_rate or settings.SONIOX_SAMPLE_RATE
        self.reconnect_timeout = reconnect_timeout if reconnect_timeout is not None else settings.STREAM_RECONNECT_TIMEOUT
        self.process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._consecutive_failures = 0
        self._total_bytes_read = 0
        self._last_media_sequence: Optional[int] = None

    async def start(self) -> AsyncGenerator[bytes, None]:
        """
        Extract audio from HLS stream and yield chunks.

        When the stream goes silent (FFmpeg exits cleanly), polls the m3u8
        playlist for up to STREAM_RECONNECT_TIMEOUT seconds. If new segments
        appear the extraction restarts seamlessly; if the timeout is exceeded
        or the playlist carries #EXT-X-ENDLIST the generator stops.

        Yields:
            Audio chunks as bytes in PCM s16le format
        """
        self._running = True
        self._consecutive_failures = 0
        retry_delay = self.INITIAL_RETRY_DELAY

        logger.info(
            "Starting audio extraction",
            extra={
                "hls_url": self.hls_url,
                "sample_rate": self.sample_rate,
                "reconnect_timeout_s": self.reconnect_timeout,
            },
        )

        while self._running:
            try:
                async for chunk in self._extract_audio():
                    yield chunk
                    # Reset error counters on successful data
                    self._consecutive_failures = 0
                    retry_delay = self.INITIAL_RETRY_DELAY

                # FFmpeg exited cleanly — stream went silent.
                if self.reconnect_timeout <= 0:
                    # Caller handles reconnection at a higher level
                    logger.info(
                        "Audio stream went silent — exiting (reconnect handled externally)",
                        extra={"total_bytes_read": self._total_bytes_read},
                    )
                    break

                # Poll the m3u8 for new segments before giving up.
                logger.info(
                    "Audio stream went silent — polling for new HLS segments",
                    extra={
                        "reconnect_timeout_s": self.reconnect_timeout,
                        "total_bytes_read": self._total_bytes_read,
                    },
                )
                should_reconnect = await self._wait_for_new_segments()
                if not should_reconnect:
                    # Timed out or permanent end — exit gracefully
                    break
                # New segments detected — loop back and restart FFmpeg
                logger.info("New HLS segments detected — restarting audio extraction")

            except Exception as e:
                self._consecutive_failures += 1
                await self.cleanup()

                if self._consecutive_failures >= self.MAX_RETRIES:
                    logger.error(
                        "Audio extraction failed after max retries — stopping gracefully",
                        extra={
                            "max_retries": self.MAX_RETRIES,
                            "last_error": str(e),
                        },
                    )
                    break

                logger.warning(
                    "Audio extraction error — retrying",
                    extra={
                        "attempt": self._consecutive_failures,
                        "max_retries": self.MAX_RETRIES,
                        "retry_delay_s": round(retry_delay, 1),
                        "error": str(e),
                    },
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * self.BACKOFF_MULTIPLIER, self.MAX_RETRY_DELAY)

        await self.cleanup()

    async def _wait_for_new_segments(self) -> bool:
        """
        Poll the m3u8 playlist until new segments appear or the reconnect
        timeout is exceeded.

        Returns:
            True  — new segments found, FFmpeg should restart
            False — stream permanently ended or reconnect timeout exceeded
        """
        timeout = self.reconnect_timeout
        deadline = time.monotonic() + timeout

        # Snapshot the current sequence so we can detect forward movement
        self._last_media_sequence = await self._fetch_media_sequence()
        logger.info(
            "Polling HLS playlist for new segments",
            extra={
                "hls_url": self.hls_url,
                "poll_interval_s": self.POLL_INTERVAL,
                "last_sequence": self._last_media_sequence,
                "timeout_s": timeout,
            },
        )

        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)

            result = await self._fetch_playlist()
            if result is None:
                # Transient network error — keep polling
                continue

            sequence, is_final = result

            if is_final:
                logger.info(
                    "HLS playlist carries #EXT-X-ENDLIST — stream permanently ended",
                    extra={"hls_url": self.hls_url},
                )
                return False

            if sequence is not None and (
                self._last_media_sequence is None or sequence > self._last_media_sequence
            ):
                logger.info(
                    "New HLS segments detected",
                    extra={
                        "hls_url": self.hls_url,
                        "prev_sequence": self._last_media_sequence,
                        "current_sequence": sequence,
                    },
                )
                return True

        if not self._running:
            return False

        logger.info(
            "No new HLS segments within timeout — closing session gracefully",
            extra={"hls_url": self.hls_url, "timeout_s": timeout},
        )
        return False

    async def _fetch_playlist(self) -> Optional[Tuple[Optional[int], bool]]:
        """
        Fetch and parse the m3u8 playlist.

        Returns:
            (media_sequence, is_endlist) tuple, or None on network/parse error.
        """
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    self.hls_url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        logger.debug(
                            "m3u8 poll non-200",
                            extra={"hls_url": self.hls_url, "http_status": response.status},
                        )
                        return None

                    content = await response.text()
                    playlist = m3u8.loads(content)
                    return playlist.media_sequence, playlist.is_endlist

        except Exception as e:
            logger.debug(
                "Error polling m3u8",
                extra={"hls_url": self.hls_url, "error": str(e)},
            )
            return None

    async def _fetch_media_sequence(self) -> Optional[int]:
        """Return the current #EXT-X-MEDIA-SEQUENCE from the playlist."""
        result = await self._fetch_playlist()
        return result[0] if result else None

    async def _extract_audio(self) -> AsyncGenerator[bytes, None]:
        """
        Internal method to extract audio from HLS stream via FFmpeg.

        Yields:
            Audio chunks as bytes in PCM s16le format
        """
        cmd = [
            'ffmpeg',
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-f', 'hls',
            '-allowed_extensions', 'ALL',
            '-extension_picky', '0',
            '-i', self.hls_url,
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ac', '1',       # mono
            '-ar', str(self.sample_rate),
            '-loglevel', 'error',
            '-'               # output to stdout
        ]

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        logger.info(
            "FFmpeg process started",
            extra={
                "pid": self.process.pid,
                "hls_url": self.hls_url,
                "sample_rate": self.sample_rate,
            },
        )

        chunk_size = settings.AUDIO_CHUNK_SIZE
        empty_read_count = 0
        max_empty_reads = 10

        while self._running:
            try:
                chunk = await asyncio.wait_for(
                    self.process.stdout.read(chunk_size),
                    timeout=30.0
                )

                if not chunk:
                    empty_read_count += 1
                    if empty_read_count >= max_empty_reads:
                        if self.process.returncode is not None:
                            stderr = await self.process.stderr.read()
                            if stderr:
                                logger.warning(
                                    "FFmpeg stderr output",
                                    extra={
                                        "returncode": self.process.returncode,
                                        "stderr": stderr.decode(errors="replace")[:1000],
                                    },
                                )
                            logger.info(
                                "End of audio stream reached (FFmpeg exited)",
                                extra={
                                    "returncode": self.process.returncode,
                                    "total_bytes_read": self._total_bytes_read,
                                },
                            )
                            return
                        await asyncio.sleep(0.1)
                    continue

                empty_read_count = 0
                self._total_bytes_read += len(chunk)
                yield chunk

            except asyncio.TimeoutError:
                logger.warning(
                    "Audio read timeout — stream may be stalled",
                    extra={"total_bytes_read": self._total_bytes_read},
                )
                raise RuntimeError("Audio stream read timeout")

    async def stop(self):
        """Stop audio extraction."""
        logger.info(
            "Stopping audio extraction",
            extra={"total_bytes_read": self._total_bytes_read},
        )
        self._running = False
        await self.cleanup()

    async def cleanup(self):
        """Clean up FFmpeg process."""
        if self.process:
            pid = self.process.pid
            try:
                if self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "FFmpeg did not terminate gracefully — killing process",
                            extra={"pid": pid},
                        )
                        self.process.kill()
                        await self.process.wait()

                logger.info(
                    "FFmpeg process cleaned up",
                    extra={"pid": pid, "returncode": self.process.returncode},
                )
            except Exception as e:
                logger.exception(
                    "Error cleaning up FFmpeg process",
                    extra={"pid": pid, "error": str(e)},
                )

            self.process = None

    def get_stats(self) -> dict:
        """Get extraction statistics."""
        return {
            "total_bytes_read": self._total_bytes_read,
            "consecutive_failures": self._consecutive_failures,
            "is_running": self._running
        }
