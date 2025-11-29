# Product Requirements Document (PRD)
## Live Stream Transcription Service

### 1. Executive Summary

Build a Python-based microservice that consumes HLS live streams from lecture broadcasts and provides real-time transcriptions using Soniox ASR API. The service will stream transcriptions via WebSocket/SSE with precise timestamps, enabling real-time captioning and future transcript retrieval capabilities.

### 2. Problem Statement

**Current State:**
- Live lectures are being broadcast via RTMP/HLS at `https://rtmp.arivihan.com:8888/{uniqueid}/`
- No real-time transcription capability exists
- Students/viewers cannot access live captions or searchable transcripts
- No infrastructure to store or retrieve historical transcriptions

**Desired State:**
- Automatic real-time transcription of any live stream
- Streaming transcription output with word-level timestamps
- Foundation for future transcript storage and retrieval system

### 3. Goals & Objectives

**Primary Goals:**
- Deliver real-time transcriptions with <2 second latency
- Achieve >90% transcription accuracy for clear audio
- Support multiple concurrent stream processing
- Provide both SSE and WebSocket output options

**Non-Goals (Phase 1):**
- Video processing or analysis
- Speaker diarization
- Multi-language support
- Transcript editing interface

### 4. User Stories

```
As a viewer
I want to see real-time captions for live lectures
So that I can follow along even with audio issues

As a developer
I want to integrate live transcriptions into my application
So that I can provide accessibility features

As a system administrator
I want to retrieve transcripts by timestamp and stream ID
So that I can provide searchable lecture content (Phase 2)
```

### 5. Functional Requirements

#### 5.1 Core Transcription Pipeline

```python
# Input
- unique_id: String identifier for the stream
- HLS URL format: https://rtmp.arivihan.com:8888/{unique_id}/index.m3u8

# Processing
1. Validate stream availability
2. Extract audio from HLS stream
3. Convert to Soniox-compatible format (16kHz, mono, PCM)
4. Stream audio chunks to Soniox WebSocket
5. Receive transcription results
6. Format with timestamps
7. Stream to clients

# Output
{
  "unique_id": "stream_123",
  "segment_id": "seg_456",
  "timestamp": "2024-01-15T10:30:45.123Z",
  "stream_time": 125.45,  # seconds since stream start
  "text": "Hello everyone, welcome to today's lecture",
  "is_final": true,
  "words": [
    {
      "text": "Hello",
      "start_time": 125.45,
      "end_time": 125.78,
      "confidence": 0.98
    }
  ]
}
```

#### 5.2 API Endpoints

```yaml
# Start transcription
POST /api/v1/transcribe/start
Request:
  {
    "unique_id": "lecture_physics_101",
    "options": {
      "language": "en-US",
      "include_word_timestamps": true,
      "vocabulary": ["physics", "quantum", "mechanics"]
    }
  }
Response:
  {
    "session_id": "sess_789",
    "status": "started",
    "stream_url": "wss://api.example.com/ws/sess_789"
  }

# Get transcription via SSE
GET /api/v1/transcribe/{unique_id}/stream
Response: Server-Sent Events stream

# WebSocket connection
WS /api/v1/ws/transcribe/{unique_id}

# Stop transcription
POST /api/v1/transcribe/stop
Request:
  {
    "unique_id": "lecture_physics_101"
  }

# Health check
GET /api/v1/health
Response:
  {
    "status": "healthy",
    "active_streams": 5,
    "version": "1.0.0"
  }
```

### 6. Technical Requirements

#### 6.1 Technology Stack

```yaml
Framework: FastAPI 0.104+
Server: Uvicorn with uvloop
Python: 3.11+

Core Dependencies:
  - fastapi[all]
  - websockets==12.0
  - aiohttp==3.9
  - ffmpeg-python==0.2.0
  - m3u8==4.0
  - sse-starlette==1.8
  - pydantic==2.5
  - python-dotenv==1.0

Dev Dependencies:
  - pytest==7.4
  - pytest-asyncio==0.21
  - black==23.0
  - pylint==3.0
  - mypy==1.7
```

#### 6.2 Project Structure

```
transcription-service/
├── src/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app
│   ├── config.py               # Configuration management
│   ├── models/
│   │   ├── __init__.py
│   │   ├── transcription.py   # Pydantic models
│   │   └── stream.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── stream_manager.py  # Stream lifecycle management
│   │   ├── audio_extractor.py # FFmpeg audio extraction
│   │   ├── soniox_client.py   # Soniox WebSocket client
│   │   └── transcription.py   # Main transcription logic
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py          # API endpoints
│   │   ├── websocket.py       # WebSocket handlers
│   │   └── sse.py             # SSE handlers
│   └── utils/
│       ├── __init__.py
│       ├── logger.py          # Logging configuration
│       └── validators.py      # Input validation
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── configs/
│   ├── .env.example
│   └── logging.yaml
├── requirements.txt
├── requirements-dev.txt
├── README.md
└── Makefile
```

#### 6.3 Core Components Implementation

```python
# config.py
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Service Configuration
    SERVICE_NAME: str = "transcription-service"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4
    
    # Stream Configuration
    RTMP_BASE_URL: str = "https://rtmp.arivihan.com:8888"
    MAX_CONCURRENT_STREAMS: int = 10
    STREAM_TIMEOUT: int = 300  # seconds
    
    # Soniox Configuration
    SONIOX_API_KEY: str
    SONIOX_WS_URL: str = "wss://api.soniox.com/transcribe-websocket"
    SONIOX_SAMPLE_RATE: int = 16000
    
    # Audio Configuration
    AUDIO_CHUNK_SIZE: int = 8000  # 0.5 seconds at 16kHz
    AUDIO_BUFFER_SIZE: int = 32000  # 2 seconds buffer
    
    # Redis Configuration (Phase 2)
    REDIS_URL: Optional[str] = None
    REDIS_TTL: int = 3600
    
    class Config:
        env_file = ".env"

# models/transcription.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class Word(BaseModel):
    text: str
    start_time: float
    end_time: float
    confidence: float = Field(ge=0, le=1)

class TranscriptionSegment(BaseModel):
    unique_id: str
    segment_id: str
    timestamp: datetime
    stream_time: float
    text: str
    is_final: bool
    words: List[Word]
    metadata: Optional[dict] = {}

class StreamOptions(BaseModel):
    language: str = "en-US"
    include_word_timestamps: bool = True
    vocabulary: List[str] = []
    enable_punctuation: bool = True
    enable_numerals: bool = True

# services/audio_extractor.py
import asyncio
import ffmpeg
from typing import AsyncGenerator
import logging

logger = logging.getLogger(__name__)

class AudioExtractor:
    def __init__(self, hls_url: str, sample_rate: int = 16000):
        self.hls_url = hls_url
        self.sample_rate = sample_rate
        self.process = None
        
    async def start(self) -> AsyncGenerator[bytes, None]:
        """Extract audio from HLS stream and yield chunks"""
        try:
            self.process = (
                ffmpeg
                .input(self.hls_url)
                .output(
                    'pipe:', 
                    format='s16le',
                    acodec='pcm_s16le',
                    ac=1,
                    ar=str(self.sample_rate),
                    loglevel='error'
                )
                .run_async(pipe_stdout=True, pipe_stderr=True)
            )
            
            chunk_size = 8000  # 0.5 seconds
            while True:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, self.process.stdout.read, chunk_size
                )
                if not chunk:
                    break
                yield chunk
                
        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            raise
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        if self.process:
            self.process.terminate()
            await asyncio.sleep(0.5)
            if self.process.poll() is None:
                self.process.kill()

# services/soniox_client.py
import websockets
import json
import logging
from typing import AsyncGenerator, Dict, Any

logger = logging.getLogger(__name__)

class SoniexClient:
    def __init__(self, api_key: str, ws_url: str):
        self.api_key = api_key
        self.ws_url = ws_url
        self.websocket = None
        
    async def connect(self, options: Dict[str, Any]):
        """Establish WebSocket connection to Soniox"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        self.websocket = await websockets.connect(
            self.ws_url,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=10
        )
        
        # Send configuration
        config = {
            "sample_rate": options.get("sample_rate", 16000),
            "include_nonfinal": True,
            "enable_punctuation": options.get("enable_punctuation", True),
            "enable_numerals": options.get("enable_numerals", True),
            "speech_context": {
                "entries": [
                    {"phrases": options.get("vocabulary", []), "boost": 10}
                ]
            }
        }
        
        await self.websocket.send(json.dumps(config))
        logger.info("Connected to Soniox WebSocket")
    
    async def send_audio(self, audio_chunk: bytes):
        """Send audio chunk to Soniox"""
        if self.websocket:
            await self.websocket.send(audio_chunk)
    
    async def receive_transcriptions(self) -> AsyncGenerator[Dict, None]:
        """Receive transcription results from Soniox"""
        if not self.websocket:
            raise RuntimeError("WebSocket not connected")
            
        async for message in self.websocket:
            try:
                data = json.loads(message)
                if "words" in data or "text" in data:
                    yield data
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Soniox response: {e}")
                continue
    
    async def disconnect(self):
        """Close WebSocket connection"""
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

# services/stream_manager.py
import asyncio
from typing import Dict, Optional
import uuid
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class StreamManager:
    def __init__(self, max_concurrent: int = 10):
        self.active_streams: Dict[str, StreamSession] = {}
        self.max_concurrent = max_concurrent
        self._lock = asyncio.Lock()
    
    async def create_session(self, unique_id: str) -> str:
        """Create new transcription session"""
        async with self._lock:
            if len(self.active_streams) >= self.max_concurrent:
                raise RuntimeError("Maximum concurrent streams reached")
            
            if unique_id in self.active_streams:
                raise ValueError(f"Stream {unique_id} already active")
            
            session_id = str(uuid.uuid4())
            session = StreamSession(
                session_id=session_id,
                unique_id=unique_id,
                started_at=datetime.utcnow()
            )
            
            self.active_streams[unique_id] = session
            logger.info(f"Created session {session_id} for stream {unique_id}")
            return session_id
    
    async def get_session(self, unique_id: str) -> Optional['StreamSession']:
        """Get active session by unique_id"""
        return self.active_streams.get(unique_id)
    
    async def remove_session(self, unique_id: str):
        """Remove session from active streams"""
        async with self._lock:
            if unique_id in self.active_streams:
                session = self.active_streams.pop(unique_id)
                await session.cleanup()
                logger.info(f"Removed session for stream {unique_id}")

class StreamSession:
    def __init__(self, session_id: str, unique_id: str, started_at: datetime):
        self.session_id = session_id
        self.unique_id = unique_id
        self.started_at = started_at
        self.clients = set()  # Connected WebSocket/SSE clients
        self.transcription_task = None
        
    async def cleanup(self):
        """Clean up session resources"""
        if self.transcription_task:
            self.transcription_task.cancel()
        
        # Notify all clients
        for client in self.clients:
            try:
                await client.close()
            except:
                pass
```

### 7. Implementation Plan

#### Phase 1: Core Transcription (Week 1-2)
```yaml
Sprint 1.1:
  - Project setup and configuration
  - Basic FastAPI application structure
  - Audio extraction from HLS streams
  - Unit tests for audio extraction

Sprint 1.2:
  - Soniox WebSocket integration
  - Basic transcription pipeline
  - Error handling and retry logic
  - Integration tests
```

#### Phase 2: Streaming Output (Week 3)
```yaml
Sprint 2.1:
  - WebSocket endpoint implementation
  - SSE endpoint implementation
  - Client connection management
  - Real-time streaming tests
```

#### Phase 3: Production Readiness (Week 4)
```yaml
Sprint 3.1:
  - Monitoring and logging
  - Performance optimization
  - Docker containerization
  - Deployment scripts
  - Load testing
```

### 8. Testing Strategy

```python
# tests/unit/test_audio_extractor.py
import pytest
from services.audio_extractor import AudioExtractor

@pytest.mark.asyncio
async def test_audio_extraction():
    """Test audio extraction from sample HLS stream"""
    extractor = AudioExtractor("test_stream.m3u8")
    chunks = []
    
    async for chunk in extractor.start():
        chunks.append(chunk)
        if len(chunks) >= 10:  # Get 10 chunks
            break
    
    assert len(chunks) == 10
    assert all(len(chunk) == 8000 for chunk in chunks)

# tests/integration/test_transcription_pipeline.py
@pytest.mark.asyncio
async def test_end_to_end_transcription():
    """Test complete transcription pipeline"""
    # Mock Soniox responses
    # Test with sample HLS stream
    # Verify transcription output format
    pass

# Load testing script
# locustfile.py
from locust import HttpUser, task, between

class TranscriptionUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def start_transcription(self):
        self.client.post("/api/v1/transcribe/start", json={
            "unique_id": f"test_stream_{self.user_id}"
        })
```

### 9. Monitoring & Metrics

```python
# Key metrics to track
metrics = {
    "active_streams": Gauge,
    "transcription_latency": Histogram,
    "words_per_minute": Counter,
    "error_rate": Rate,
    "soniox_api_calls": Counter,
    "websocket_connections": Gauge
}

# Logging structure
log_format = {
    "timestamp": "ISO-8601",
    "level": "INFO|ERROR|WARNING",
    "service": "transcription-service",
    "unique_id": "stream_id",
    "message": "descriptive message",
    "metadata": {}
}
```

### 10. Error Handling

```python
# Error types and handling
errors = {
    "StreamNotFound": "Return 404, log error",
    "SoniexConnectionError": "Retry with backoff, alert if persistent",
    "AudioExtractionError": "Stop session, notify clients",
    "RateLimitExceeded": "Queue or reject with 429",
    "InvalidFormat": "Return 400 with details"
}

# Retry strategy
retry_config = {
    "max_attempts": 3,
    "backoff_factor": 2,
    "max_delay": 30
}
```

### 11. Future Enhancements (Phase 2)

```yaml
Redis Integration:
  - Cache transcriptions with TTL
  - Enable timestamp-based retrieval
  - Support transcript search

Advanced Features:
  - Speaker diarization
  - Multi-language support
  - Custom vocabulary management
  - Punctuation restoration
  - Profanity filtering

Analytics:
  - Word frequency analysis
  - Speaking rate calculation
  - Silence detection
  - Quality scoring
```

### 12. Success Criteria

- **Performance**: <2 second end-to-end latency
- **Accuracy**: >90% word accuracy for clear audio
- **Reliability**: 99.9% uptime for service
- **Scalability**: Support 50+ concurrent streams
- **Developer Experience**: Full API documentation with examples

### 13. Development Commands

```bash
# Setup development environment
make setup

# Run locally
make run-dev

# Run tests
make test

# Build Docker image
make docker-build

# Deploy to staging
make deploy-staging

# Run load tests
make load-test
```

### 14. Initial Makefile

```makefile
.PHONY: setup run-dev test docker-build

setup:
	python -m venv venv
	./venv/bin/pip install -r requirements.txt
	./venv/bin/pip install -r requirements-dev.txt

run-dev:
	./venv/bin/uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

test:
	./venv/bin/pytest tests/ -v --asyncio-mode=auto

docker-build:
	docker build -f docker/Dockerfile -t transcription-service:latest .

format:
	./venv/bin/black src/ tests/
	./venv/bin/isort src/ tests/

lint:
	./venv/bin/pylint src/
	./venv/bin/mypy src/
```

---

**Next Steps for Claude Code:**
1. Create the project structure as defined
2. Implement the core components in order:
   - Configuration management
   - Audio extraction service  
   - Soniox client
   - Stream manager
   - API endpoints
3. Write comprehensive tests
4. Add monitoring and logging
5. Create Docker configuration
6. Document API with examples

This PRD provides a complete blueprint for building the transcription service with clear requirements, technical specifications, and implementation guidance.
