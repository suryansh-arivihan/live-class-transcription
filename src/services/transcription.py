import asyncio
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional
from src.models.transcription import (
    TranscriptionSegment,
    Word,
    StreamOptions
)
from src.models.stream import StreamStatus
from src.services.audio_extractor import AudioExtractor, fetch_hls_playlist
from src.services.soniox_client import SonioxClient
from src.services.stream_manager import stream_manager
from src.services.dynamodb_client import dynamodb_client
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class TranscriptionService:
    """Core service for managing transcription pipeline."""

    def __init__(self, unique_id: str, hls_url: str, options: StreamOptions):
        """
        Initialize transcription service.

        Args:
            unique_id: Unique stream identifier (maps to roomId in DynamoDB)
            hls_url: HLS stream URL
            options: Transcription options
        """
        self.unique_id = unique_id
        self.hls_url = hls_url
        self.options = options
        self.audio_extractor: Optional[AudioExtractor] = None
        self.soniox_client: Optional[SonioxClient] = None
        self._running = False
        self._start_time: Optional[datetime] = None

    async def start(self) -> AsyncGenerator[TranscriptionSegment, None]:
        """
        Start transcription pipeline with automatic reconnection.

        When the audio stream goes silent and the HLS reconnect timeout expires,
        the service enters a WAITING state and polls DynamoDB for a session-end
        signal. If the stream resumes, a fresh pipeline cycle is started.

        Yields:
            TranscriptionSegment objects with transcribed text

        Raises:
            RuntimeError: If transcription fails to start
        """
        self._running = True
        self._start_time = datetime.utcnow()

        logger.info(f"Starting transcription for stream {self.unique_id}")

        try:
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.STARTING
            )

            while self._running:
                # --- Pipeline cycle: fresh components each time ---
                self.audio_extractor = AudioExtractor(self.hls_url, reconnect_timeout=0)
                self.soniox_client = SonioxClient()

                pipeline_error = False
                try:
                    await self.soniox_client.connect(self.options)
                    await stream_manager.update_session_status(
                        self.unique_id, StreamStatus.ACTIVE
                    )

                    audio_task = asyncio.create_task(self._stream_audio())

                    try:
                        async for segment in self._receive_transcriptions():
                            if not self._running:
                                break
                            yield segment
                    finally:
                        audio_task.cancel()
                        try:
                            await audio_task
                        except asyncio.CancelledError:
                            pass

                except Exception as e:
                    # Pipeline cycle failed (e.g. no audio reached Soniox,
                    # connection lost, etc.) — don't crash the session,
                    # fall through to WAITING state.
                    logger.warning(
                        f"Pipeline cycle failed for {self.unique_id}: {e}. "
                        f"Will poll for session end or stream resume."
                    )
                    pipeline_error = True

                # --- Pipeline ended (stream went silent or errored) ---
                # Clean up current cycle's resources
                if self.audio_extractor:
                    await self.audio_extractor.stop()
                if self.soniox_client:
                    await self.soniox_client.disconnect()

                if not self._running:
                    break

                # --- Wait for session end or stream resume ---
                await stream_manager.update_session_status(
                    self.unique_id, StreamStatus.WAITING
                )
                logger.info(
                    f"Stream silent for {self.unique_id}. "
                    f"Polling for session end or stream resume."
                )

                result = await self._wait_for_session_end_or_resume()

                if result == "resumed":
                    logger.info(
                        f"Stream resumed for {self.unique_id}, restarting pipeline."
                    )
                    continue
                else:
                    logger.info(
                        f"Session ended for {self.unique_id} (reason: {result})."
                    )
                    break

        except Exception as e:
            logger.error(f"Transcription error for stream {self.unique_id}: {e}")
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.ERROR, str(e)
            )
            raise RuntimeError(f"Transcription failed: {e}")

        finally:
            await self.stop()

    async def _wait_for_session_end_or_resume(self) -> str:
        """
        Poll DynamoDB and m3u8 until either the session has ended or the
        stream has resumed.

        Stream resume is detected by observing the media sequence advance
        between two consecutive polls, which means the HLS stream is actively
        producing new segments — regardless of whether the sequence number is
        higher or lower than the previous session's last known value.

        Returns:
            "ended"   — session-end entry found in DynamoDB
            "resumed" — new HLS segments detected (sequence advancing)
            "timeout" — safety timeout exceeded
        """
        deadline = time.monotonic() + settings.SESSION_END_POLL_TIMEOUT
        poll_interval = settings.SESSION_END_POLL_INTERVAL

        # Track the sequence seen on the *previous* poll iteration so we can
        # detect forward movement (i.e. new segments being produced right now).
        prev_poll_sequence: Optional[int] = None

        logger.info(
            f"Polling every {poll_interval}s for session end "
            f"(timeout: {settings.SESSION_END_POLL_TIMEOUT}s) "
            f"for {self.unique_id}"
        )

        while self._running and time.monotonic() < deadline:
            # Check DynamoDB for session-end signal
            ended = await dynamodb_client.check_session_ended(self.unique_id)
            if ended:
                logger.info(
                    f"Session-end entry found in DynamoDB for {self.unique_id}."
                )
                return "ended"

            # Check m3u8 for stream resume by detecting sequence advancement
            result = await fetch_hls_playlist(self.hls_url)
            if result is not None:
                sequence, is_final = result
                if not is_final and sequence is not None:
                    if prev_poll_sequence is not None and sequence > prev_poll_sequence:
                        logger.info(
                            f"Stream resumed for {self.unique_id} — "
                            f"HLS sequence advancing "
                            f"({prev_poll_sequence} → {sequence})."
                        )
                        return "resumed"
                    prev_poll_sequence = sequence
            else:
                # Playlist not reachable — reset so we need two consecutive
                # successful polls to confirm the stream is truly back.
                prev_poll_sequence = None

            await asyncio.sleep(poll_interval)

        if not self._running:
            return "stopped"

        logger.warning(
            f"Session-end poll timeout ({settings.SESSION_END_POLL_TIMEOUT}s) "
            f"exceeded for {self.unique_id}."
        )
        return "timeout"

    async def _stream_audio(self):
        """Stream audio from HLS to Soniox."""
        try:
            logger.info(f"Starting audio streaming for {self.unique_id}")
            async for audio_chunk in self.audio_extractor.start():
                if not self._running:
                    break
                await self.soniox_client.send_audio(audio_chunk)

            # Send end-of-stream signal
            await self.soniox_client.send_eos()
            logger.info(f"Audio streaming completed for {self.unique_id}")

        except Exception as e:
            logger.error(f"Audio streaming error: {e}")
            raise

    async def _receive_transcriptions(self) -> AsyncGenerator[TranscriptionSegment, None]:
        """Receive and format transcriptions from Soniox."""
        try:
            logger.info(f"Starting transcription reception for {self.unique_id}")

            async for result in self.soniox_client.receive_transcriptions():
                if not self._running:
                    break

                segment = self._format_transcription(result)
                if segment:
                    yield segment

            logger.info(f"Transcription reception completed for {self.unique_id}")

        except Exception as e:
            logger.error(f"Transcription reception error: {e}")
            raise

    def _format_transcription(self, result: dict) -> Optional[TranscriptionSegment]:
        """
        Format Soniox transcription result into TranscriptionSegment.

        Args:
            result: Raw result from Soniox

        Returns:
            TranscriptionSegment or None if no valid tokens
        """
        tokens = result.get("tokens", [])
        if not tokens:
            return None

        words = []
        text_parts = []
        is_final = False

        for token in tokens:
            token_text = token.get("text", "")
            if not token_text:
                continue

            text_parts.append(token_text)

            if token.get("is_final"):
                is_final = True

            word = Word(
                text=token_text,
                start_time=token.get("start_time", 0.0),
                end_time=token.get("end_time", 0.0),
                confidence=token.get("confidence", 1.0),
                speaker=token.get("speaker"),
                language=token.get("language")
            )
            words.append(word)

        if not text_parts:
            return None

        stream_time = 0.0
        if self._start_time:
            stream_time = (datetime.utcnow() - self._start_time).total_seconds()

        segment = TranscriptionSegment(
            unique_id=self.unique_id,
            segment_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            stream_time=stream_time,
            text="".join(text_parts),
            is_final=is_final,
            words=words,
            metadata={}
        )

        return segment

    async def stop(self):
        """Stop transcription pipeline."""
        logger.info(f"Stopping transcription for stream {self.unique_id}")
        self._running = False

        if self.audio_extractor:
            await self.audio_extractor.stop()

        if self.soniox_client:
            await self.soniox_client.disconnect()

        await stream_manager.update_session_status(
            self.unique_id, StreamStatus.STOPPED
        )

        logger.info(f"Transcription stopped for stream {self.unique_id}")
