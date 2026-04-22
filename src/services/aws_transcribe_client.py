import asyncio
from typing import AsyncGenerator, Dict, Any, Optional

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

from src.config import settings
from src.models.transcription import StreamOptions
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


_END_SENTINEL = object()


class _QueueingHandler(TranscriptResultStreamHandler):
    """Pushes AWS Transcribe results into an asyncio.Queue as Soniox-shaped dicts."""

    def __init__(self, transcript_result_stream, queue: asyncio.Queue):
        super().__init__(transcript_result_stream)
        self._queue = queue

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        for result in transcript_event.transcript.results:
            is_final = not result.is_partial
            for alt in result.alternatives or []:
                tokens = []
                seen_word = False
                for item in alt.items or []:
                    content = item.content or ""
                    if not content:
                        continue
                    is_punct = getattr(item, "item_type", None) == "punctuation"
                    # AWS items don't carry whitespace; prepend a space before
                    # each word except the first, so "".join(...) downstream
                    # produces readable text. Punctuation attaches to the
                    # previous word.
                    if is_punct or not seen_word:
                        text = content
                    else:
                        text = " " + content
                    if not is_punct:
                        seen_word = True
                    tokens.append({
                        "text": text,
                        "start_time": float(item.start_time or 0.0),
                        "end_time": float(item.end_time or 0.0),
                        "confidence": float(item.confidence) if item.confidence is not None else 1.0,
                        "is_final": is_final,
                        "speaker": item.speaker,
                        "language": None,
                    })
                if tokens:
                    await self._queue.put({"tokens": tokens})


class AwsTranscribeClient:
    """Streaming client for AWS Transcribe, API-compatible with SonioxClient."""

    def __init__(self, region: str = None):
        self.region = region or settings.AWS_TRANSCRIBE_REGION or settings.AWS_REGION
        self._client: Optional[TranscribeStreamingClient] = None
        self._stream = None
        self._handler_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected = False
        self._eos_sent = False

    async def connect(self, options: StreamOptions):
        try:
            logger.info(f"Connecting to AWS Transcribe in region {self.region}")

            self._client = TranscribeStreamingClient(region=self.region)

            # Server setting is authoritative — this service is deployed for a
            # specific language and clients may send a default they don't control.
            language_code = settings.AWS_TRANSCRIBE_LANGUAGE_CODE or options.language
            vocabulary_name = None  # AWS requires a pre-registered vocabulary name

            if options.language and options.language != language_code:
                logger.info(
                    f"Ignoring client-supplied language={options.language!r}; "
                    f"using configured {language_code!r}"
                )

            logger.info(
                f"AWS Transcribe config: language_code={language_code}, "
                f"sample_rate={settings.AWS_TRANSCRIBE_SAMPLE_RATE}, "
                f"encoding={settings.AWS_TRANSCRIBE_MEDIA_ENCODING}"
            )

            kwargs = {
                "language_code": language_code,
                "media_sample_rate_hz": settings.AWS_TRANSCRIBE_SAMPLE_RATE,
                "media_encoding": settings.AWS_TRANSCRIBE_MEDIA_ENCODING,
            }
            if options.enable_speaker_diarization:
                kwargs["show_speaker_label"] = True
            if vocabulary_name:
                kwargs["vocabulary_name"] = vocabulary_name

            self._stream = await self._client.start_stream_transcription(**kwargs)

            handler = _QueueingHandler(self._stream.output_stream, self._queue)
            self._handler_task = asyncio.create_task(self._run_handler(handler))

            self._connected = True
            logger.info("Connected to AWS Transcribe successfully")

        except Exception as e:
            logger.error(f"Failed to connect to AWS Transcribe: {e}")
            self._connected = False
            raise ConnectionError(f"Failed to connect to AWS Transcribe: {e}")

    async def _run_handler(self, handler: _QueueingHandler):
        try:
            await handler.handle_events()
        except Exception as e:
            logger.error(f"AWS Transcribe event handler error: {e}")
        finally:
            await self._queue.put(_END_SENTINEL)

    async def send_audio(self, audio_chunk: bytes):
        if not self._connected or not self._stream:
            raise RuntimeError("AWS Transcribe stream not connected")
        try:
            await self._stream.input_stream.send_audio_event(audio_chunk=audio_chunk)
        except Exception as e:
            logger.error(f"Error sending audio chunk to AWS Transcribe: {e}")
            raise

    async def send_eos(self):
        if self._connected and self._stream and not self._eos_sent:
            try:
                await self._stream.input_stream.end_stream()
                self._eos_sent = True
                logger.info("Sent end-of-stream to AWS Transcribe")
            except Exception as e:
                logger.error(f"Error sending EOS to AWS Transcribe: {e}")

    async def receive_transcriptions(self) -> AsyncGenerator[Dict[str, Any], None]:
        if not self._connected:
            raise RuntimeError("AWS Transcribe stream not connected")

        logger.info("Starting to receive AWS Transcribe transcriptions")
        while True:
            item = await self._queue.get()
            if item is _END_SENTINEL:
                logger.info("AWS Transcribe stream finished")
                break
            yield item

    async def disconnect(self):
        try:
            await self.send_eos()
        except Exception:
            pass

        if self._handler_task:
            try:
                await asyncio.wait_for(self._handler_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._handler_task.cancel()
                try:
                    await self._handler_task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as e:
                logger.error(f"Error awaiting AWS handler task: {e}")

        self._stream = None
        self._client = None
        self._handler_task = None
        self._connected = False
        logger.info("Disconnected from AWS Transcribe")

    @property
    def is_connected(self) -> bool:
        return self._connected
