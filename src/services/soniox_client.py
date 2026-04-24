import asyncio
import websockets
import json
from typing import AsyncGenerator, Dict, Any, Optional
from src.config import settings
from src.models.transcription import StreamOptions
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class SonioxClient:
    """WebSocket client for Soniox real-time transcription API."""

    def __init__(self, api_key: str = None, ws_url: str = None):
        """
        Initialize Soniox client.

        Args:
            api_key: Soniox API key (defaults to settings)
            ws_url: WebSocket URL (defaults to settings)
        """
        self.api_key = api_key or settings.SONIOX_API_KEY
        self.ws_url = ws_url or settings.SONIOX_WS_URL
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._buffered_message: Optional[str] = None
        self._audio_bytes_sent = 0
        self._messages_received = 0

    async def connect(self, options: StreamOptions):
        """
        Establish WebSocket connection to Soniox and send configuration.

        Args:
            options: Stream transcription options

        Raises:
            ConnectionError: If connection fails
        """
        try:
            logger.info(
                "Connecting to Soniox",
                extra={
                    "ws_url": self.ws_url,
                    "model": settings.SONIOX_MODEL,
                    "sample_rate": settings.SONIOX_SAMPLE_RATE,
                },
            )

            # Connect to WebSocket
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10
            )

            # Build configuration message
            config = {
                "api_key": self.api_key,
                "model": settings.SONIOX_MODEL,
                "sample_rate": settings.SONIOX_SAMPLE_RATE,
                "num_channels": 1,
                "audio_format": "pcm_s16le",
                "enable_endpoint_detection": options.enable_endpoint_detection,
            }

            # Add language hints if provided
            if options.language_hints:
                config["language_hints"] = options.language_hints

            # Add language identification if enabled
            if options.enable_language_identification:
                config["enable_language_identification"] = True

            # Add speaker diarization if enabled
            if options.enable_speaker_diarization:
                config["enable_speaker_diarization"] = True

            # Add custom vocabulary/context if provided
            if options.vocabulary:
                config["context"] = {
                    "terms": options.vocabulary
                }

            # Send configuration
            await self.websocket.send(json.dumps(config))

            # Soniox validates the API key only after the config frame is sent
            # and reports rejection as a JSON frame (not a WS close), so the
            # connect() path looks successful otherwise. Peek once for an early
            # error so auth failures surface here and the caller's fallback
            # path can trigger.
            await self._verify_config_accepted()

            self._connected = True
            logger.info(
                "Connected to Soniox WebSocket",
                extra={
                    "model": settings.SONIOX_MODEL,
                    "language_hints": options.language_hints,
                    "speaker_diarization": options.enable_speaker_diarization,
                },
            )

        except Exception as e:
            logger.error(
                "Failed to connect to Soniox",
                extra={"ws_url": self.ws_url, "error": str(e)},
            )
            self._connected = False
            raise ConnectionError(f"Failed to connect to Soniox: {e}")

    async def _verify_config_accepted(self):
        """Wait briefly for a config-reject frame from Soniox.

        Timeout means the config was accepted (Soniox stays silent until audio
        arrives). A frame with error_code means the config was rejected.
        Anything else is buffered so receive_transcriptions can yield it.
        """
        try:
            message = await asyncio.wait_for(
                self.websocket.recv(),
                timeout=settings.SONIOX_CONNECT_VERIFY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self._buffered_message = message
            return

        if data.get("error_code"):
            logger.warning(
                "Soniox rejected config",
                extra={
                    "error_code": data.get("error_code"),
                    "error_message": data.get("error_message"),
                },
            )
            raise RuntimeError(
                f"Soniox rejected config: {data.get('error_code')} - "
                f"{data.get('error_message')}"
            )

        self._buffered_message = message

    async def send_audio(self, audio_chunk: bytes):
        """
        Send audio chunk to Soniox.

        Args:
            audio_chunk: Audio data in PCM format

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or not self.websocket:
            raise RuntimeError("WebSocket not connected")

        try:
            await self.websocket.send(audio_chunk)
            self._audio_bytes_sent += len(audio_chunk)
        except Exception as e:
            logger.exception(
                "Error sending audio chunk to Soniox",
                extra={
                    "chunk_size": len(audio_chunk),
                    "total_bytes_sent": self._audio_bytes_sent,
                    "error": str(e),
                },
            )
            raise

    async def send_eos(self):
        """Send end-of-stream signal to Soniox."""
        if self._connected and self.websocket:
            try:
                # Empty string signals end-of-audio
                await self.websocket.send("")
                logger.info(
                    "End-of-stream signal sent to Soniox",
                    extra={"total_bytes_sent": self._audio_bytes_sent},
                )
            except Exception as e:
                logger.exception(
                    "Error sending EOS to Soniox",
                    extra={"error": str(e)},
                )

    async def receive_transcriptions(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Receive transcription results from Soniox.

        Yields:
            Transcription result dictionaries

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or not self.websocket:
            raise RuntimeError("WebSocket not connected")

        logger.info("Starting to receive Soniox transcriptions")

        async def _stream():
            if self._buffered_message is not None:
                buffered, self._buffered_message = self._buffered_message, None
                yield buffered
            async for message in self.websocket:
                yield message

        try:
            async for message in _stream():
                try:
                    data = json.loads(message)
                    self._messages_received += 1

                    # Check for errors
                    if data.get("error_code"):
                        logger.error(
                            "Soniox API error",
                            extra={
                                "error_code": data.get("error_code"),
                                "error_message": data.get("error_message"),
                            },
                        )
                        raise RuntimeError(f"Soniox API error: {data.get('error_message')}")

                    # Check for finished signal
                    if data.get("finished"):
                        logger.info(
                            "Soniox transcription session finished",
                            extra={"messages_received": self._messages_received},
                        )
                        break

                    # Yield transcription data if it contains tokens
                    if "tokens" in data and data["tokens"]:
                        yield data

                except json.JSONDecodeError as e:
                    logger.error(
                        "Failed to parse Soniox response",
                        extra={"error": str(e)},
                    )
                    continue

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(
                "Soniox WebSocket connection closed",
                extra={
                    "code": getattr(e, "code", None),
                    "reason": getattr(e, "reason", None),
                    "messages_received": self._messages_received,
                },
            )
        except Exception as e:
            logger.exception(
                "Error receiving transcriptions from Soniox",
                extra={
                    "messages_received": self._messages_received,
                    "error": str(e),
                },
            )
            raise

    async def disconnect(self):
        """Close WebSocket connection."""
        if self.websocket:
            try:
                await self.send_eos()
                await self.websocket.close()
                logger.info(
                    "Disconnected from Soniox",
                    extra={
                        "total_bytes_sent": self._audio_bytes_sent,
                        "messages_received": self._messages_received,
                    },
                )
            except Exception as e:
                logger.exception(
                    "Error during Soniox disconnect",
                    extra={"error": str(e)},
                )
            finally:
                self.websocket = None
                self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self.websocket is not None
