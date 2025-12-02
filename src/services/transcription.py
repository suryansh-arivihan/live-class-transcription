import asyncio
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional
from src.models.transcription import (
    TranscriptionSegment,
    Word,
    StreamOptions
)
from src.models.stream import StreamStatus
from src.services.audio_extractor import AudioExtractor
from src.services.soniox_client import SonioxClient
from src.services.stream_manager import stream_manager
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class TranscriptionService:
    """Core service for managing transcription pipeline."""

    def __init__(self, unique_id: str, hls_url: str, options: StreamOptions):
        """
        Initialize transcription service.

        Args:
            unique_id: Unique stream identifier
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
        Start transcription pipeline.

        Yields:
            TranscriptionSegment objects with transcribed text

        Raises:
            RuntimeError: If transcription fails to start
        """
        self._running = True
        self._start_time = datetime.utcnow()

        logger.info(f"Starting transcription for stream {self.unique_id}")

        try:
            # Update session status
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.STARTING
            )

            # Initialize components
            self.audio_extractor = AudioExtractor(self.hls_url)
            self.soniox_client = SonioxClient()

            # Connect to Soniox
            await self.soniox_client.connect(self.options)

            # Update session to active
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.ACTIVE
            )

            # Start audio streaming in background task
            audio_task = asyncio.create_task(self._stream_audio())

            # Iterate over transcription results and yield segments
            try:
                async for segment in self._receive_transcriptions():
                    if not self._running:
                        break
                    yield segment
            finally:
                # Clean up audio task
                audio_task.cancel()
                try:
                    await audio_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"Transcription error for stream {self.unique_id}: {e}")
            await stream_manager.update_session_status(
                self.unique_id, StreamStatus.ERROR, str(e)
            )
            raise RuntimeError(f"Transcription failed: {e}")

        finally:
            await self.stop()

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

                # Process tokens from Soniox response
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

        # Extract words
        words = []
        text_parts = []
        is_final = False

        for token in tokens:
            token_text = token.get("text", "")
            if not token_text:
                continue

            text_parts.append(token_text)

            # Check if any token is final
            if token.get("is_final"):
                is_final = True

            # Create Word object
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

        # Calculate stream time
        stream_time = 0.0
        if self._start_time:
            stream_time = (datetime.utcnow() - self._start_time).total_seconds()

        # Create segment
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

        # Cleanup audio extractor
        if self.audio_extractor:
            await self.audio_extractor.stop()

        # Cleanup Soniox client
        if self.soniox_client:
            await self.soniox_client.disconnect()

        # Update session status
        await stream_manager.update_session_status(
            self.unique_id, StreamStatus.STOPPED
        )

        logger.info(f"Transcription stopped for stream {self.unique_id}")
