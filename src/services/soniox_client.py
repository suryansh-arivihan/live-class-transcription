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

    async def connect(self, options: StreamOptions):
        """
        Establish WebSocket connection to Soniox and send configuration.

        Args:
            options: Stream transcription options

        Raises:
            ConnectionError: If connection fails
        """
        try:
            logger.info(f"Connecting to Soniox at {self.ws_url}")

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
            self._connected = True

            logger.info("Connected to Soniox WebSocket successfully")

        except Exception as e:
            logger.error(f"Failed to connect to Soniox: {e}")
            self._connected = False
            raise ConnectionError(f"Failed to connect to Soniox: {e}")

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
        except Exception as e:
            logger.error(f"Error sending audio chunk: {e}")
            raise

    async def send_eos(self):
        """Send end-of-stream signal to Soniox."""
        if self._connected and self.websocket:
            try:
                # Empty string signals end-of-audio
                await self.websocket.send("")
                logger.info("Sent end-of-stream signal")
            except Exception as e:
                logger.error(f"Error sending EOS: {e}")

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

        logger.info("Starting to receive transcriptions")

        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)

                    # Check for errors
                    if data.get("error_code"):
                        logger.error(f"Soniox error: {data.get('error_code')} - {data.get('error_message')}")
                        raise RuntimeError(f"Soniox API error: {data.get('error_message')}")

                    # Check for finished signal
                    if data.get("finished"):
                        logger.info("Transcription session finished")
                        break

                    # Yield transcription data if it contains tokens
                    if "tokens" in data and data["tokens"]:
                        yield data

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse Soniox response: {e}")
                    continue

        except websockets.exceptions.ConnectionClosed:
            logger.info("Soniox WebSocket connection closed")
        except Exception as e:
            logger.error(f"Error receiving transcriptions: {e}")
            raise

    async def disconnect(self):
        """Close WebSocket connection."""
        if self.websocket:
            try:
                await self.send_eos()
                await self.websocket.close()
                logger.info("Disconnected from Soniox")
            except Exception as e:
                logger.error(f"Error during disconnect: {e}")
            finally:
                self.websocket = None
                self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self.websocket is not None
