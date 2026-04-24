import asyncio
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional, Union
from src.models.transcription import (
    TranscriptionSegment,
    Word,
    StreamOptions
)
from src.models.stream import StreamStatus
from src.services.audio_extractor import AudioExtractor, fetch_hls_playlist
from src.services.soniox_client import SonioxClient
from src.services.aws_transcribe_client import AwsTranscribeClient
from src.services.stream_manager import stream_manager
from src.services.dynamodb_client import dynamodb_client
from src.config import settings
from src.utils.logger import setup_logger, set_log_context

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
        self.stt_client: Optional[Union[SonioxClient, AwsTranscribeClient]] = None
        self._running = False
        self._start_time: Optional[datetime] = None
        self._active_provider: Optional[str] = None

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
        set_log_context(unique_id=self.unique_id)

        logger.info(
            "Starting transcription pipeline",
            extra={
                "unique_id": self.unique_id,
                "hls_url": self.hls_url,
                "language": getattr(self.options, "language", None),
            },
        )

        cycle_count = 0

        try:
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.STARTING
            )

            while self._running:
                cycle_count += 1
                logger.info(
                    "Starting pipeline cycle",
                    extra={
                        "unique_id": self.unique_id,
                        "cycle": cycle_count,
                    },
                )

                # --- Pipeline cycle: fresh components each time ---
                self.audio_extractor = AudioExtractor(self.hls_url, reconnect_timeout=0)
                self.stt_client = await self._connect_stt_with_fallback()

                pipeline_error = False
                try:
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
                        "Pipeline cycle failed — will poll for session end or stream resume",
                        extra={
                            "unique_id": self.unique_id,
                            "cycle": cycle_count,
                            "provider": self._active_provider,
                            "error": str(e),
                        },
                    )
                    pipeline_error = True

                # --- Pipeline ended (stream went silent or errored) ---
                # Clean up current cycle's resources
                if self.audio_extractor:
                    await self.audio_extractor.stop()
                if self.stt_client:
                    await self.stt_client.disconnect()

                if not self._running:
                    break

                # --- Wait for session end or stream resume ---
                await stream_manager.update_session_status(
                    self.unique_id, StreamStatus.WAITING
                )
                logger.info(
                    "Stream silent — polling for session end or stream resume",
                    extra={
                        "unique_id": self.unique_id,
                        "cycle": cycle_count,
                        "pipeline_error": pipeline_error,
                    },
                )

                result = await self._wait_for_session_end_or_resume()

                if result == "resumed":
                    logger.info(
                        "Stream resumed — restarting pipeline",
                        extra={"unique_id": self.unique_id, "cycle": cycle_count},
                    )
                    continue
                else:
                    logger.info(
                        "Session ending",
                        extra={
                            "unique_id": self.unique_id,
                            "cycle": cycle_count,
                            "reason": result,
                        },
                    )
                    break

        except Exception as e:
            logger.exception(
                "Transcription pipeline error",
                extra={
                    "unique_id": self.unique_id,
                    "cycles_completed": cycle_count,
                    "error": str(e),
                },
            )
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
            "Polling for session end or stream resume",
            extra={
                "unique_id": self.unique_id,
                "poll_interval_s": poll_interval,
                "poll_timeout_s": settings.SESSION_END_POLL_TIMEOUT,
            },
        )

        while self._running and time.monotonic() < deadline:
            # Check DynamoDB for session-end signal
            ended = await dynamodb_client.check_session_ended(self.unique_id)
            if ended:
                logger.info(
                    "Session-end entry found in DynamoDB",
                    extra={"unique_id": self.unique_id},
                )
                return "ended"

            # Check m3u8 for stream resume by detecting sequence advancement
            result = await fetch_hls_playlist(self.hls_url)
            if result is not None:
                sequence, is_final = result
                if not is_final and sequence is not None:
                    if prev_poll_sequence is not None and sequence > prev_poll_sequence:
                        logger.info(
                            "Stream resumed — HLS sequence advancing",
                            extra={
                                "unique_id": self.unique_id,
                                "prev_sequence": prev_poll_sequence,
                                "current_sequence": sequence,
                            },
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
            "Session-end poll timeout exceeded",
            extra={
                "unique_id": self.unique_id,
                "timeout_s": settings.SESSION_END_POLL_TIMEOUT,
            },
        )
        return "timeout"

    async def _connect_stt_with_fallback(self) -> Union[SonioxClient, AwsTranscribeClient]:
        """
        Try Soniox first; fall back to AWS Transcribe on connect failure.
        Fallback is per-cycle (non-sticky): next cycle retries Soniox.
        """
        soniox = SonioxClient()
        try:
            await soniox.connect(self.options)
            self._active_provider = "soniox"
            logger.info(
                "STT provider connected",
                extra={"unique_id": self.unique_id, "provider": "soniox"},
            )
            return soniox
        except Exception as e:
            logger.warning(
                "Soniox connect failed — falling back to AWS Transcribe",
                extra={
                    "unique_id": self.unique_id,
                    "soniox_error": str(e),
                },
            )
            try:
                await soniox.disconnect()
            except Exception:
                pass

            aws = AwsTranscribeClient()
            await aws.connect(self.options)
            self._active_provider = "aws_transcribe"
            logger.info(
                "STT provider connected (fallback)",
                extra={
                    "unique_id": self.unique_id,
                    "provider": "aws_transcribe",
                    "region": aws.region,
                },
            )
            return aws

    async def _stream_audio(self):
        """Stream audio from HLS to the active STT client."""
        try:
            logger.info(
                "Starting audio streaming",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                },
            )
            async for audio_chunk in self.audio_extractor.start():
                if not self._running:
                    break
                await self.stt_client.send_audio(audio_chunk)

            # Send end-of-stream signal
            await self.stt_client.send_eos()
            logger.info(
                "Audio streaming completed",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                },
            )

        except Exception as e:
            logger.exception(
                "Audio streaming error",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                    "error": str(e),
                },
            )
            raise

    async def _receive_transcriptions(self) -> AsyncGenerator[TranscriptionSegment, None]:
        """Receive and format transcriptions from the active STT client."""
        try:
            logger.info(
                "Starting transcription reception",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                },
            )
            received_count = 0

            async for result in self.stt_client.receive_transcriptions():
                if not self._running:
                    break

                segment = self._format_transcription(result)
                if segment:
                    received_count += 1
                    yield segment

            logger.info(
                "Transcription reception completed",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                    "segments_received": received_count,
                },
            )

        except Exception as e:
            logger.exception(
                "Transcription reception error",
                extra={
                    "unique_id": self.unique_id,
                    "provider": self._active_provider,
                    "error": str(e),
                },
            )
            raise

    def _format_transcription(self, result: dict) -> Optional[TranscriptionSegment]:
        """
        Format a token-bearing STT result into TranscriptionSegment.

        Accepts the Soniox token shape; AwsTranscribeClient emits the same
        shape so the active provider is transparent here.
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
        logger.info(
            "Stopping transcription pipeline",
            extra={
                "unique_id": self.unique_id,
                "provider": self._active_provider,
            },
        )
        self._running = False

        if self.audio_extractor:
            await self.audio_extractor.stop()

        if self.stt_client:
            await self.stt_client.disconnect()

        await stream_manager.update_session_status(
            self.unique_id, StreamStatus.STOPPED
        )

        logger.info(
            "Transcription pipeline stopped",
            extra={"unique_id": self.unique_id},
        )
