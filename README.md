# Live Stream Transcription Service

A Python-based microservice that consumes HLS live streams and provides real-time transcriptions using Soniox ASR API. Supports streaming transcriptions via WebSocket/SSE with word-level timestamps.

## Features

- Real-time transcription of HLS live streams
- WebSocket and Server-Sent Events (SSE) streaming
- Word-level timestamps and confidence scores
- Support for multiple concurrent streams
- Speaker diarization and language identification (configurable)
- Docker containerization for easy deployment
- RESTful API for stream management

## Architecture

```
HLS Stream → Audio Extractor (FFmpeg) → Soniox ASR → WebSocket/SSE → Clients
```

## Prerequisites

- Python 3.11+
- FFmpeg
- Docker & Docker Compose (for containerized deployment)
- Soniox API Key

## Project Structure

```
transcription-service/
├── src/
│   ├── models/          # Pydantic data models
│   ├── services/        # Core business logic
│   ├── api/             # API endpoints (REST, WebSocket, SSE)
│   ├── utils/           # Utilities and helpers
│   ├── config.py        # Configuration management
│   └── main.py          # FastAPI application
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── configs/
│   ├── .env.example
│   └── logging.yaml
├── tests/               # Test suite
├── requirements.txt
└── Makefile
```

## Quick Start

### Option 1: Docker (Recommended)

1. **Clone the repository and navigate to the project directory:**

```bash
cd live-transcription
```

2. **Configure environment variables:**

```bash
cp configs/.env.example .env
# Edit .env and add your SONIOX_API_KEY
```

3. **Build and start the service:**

```bash
make docker-build
make docker-up
```

4. **Check service health:**

```bash
curl http://localhost:8000/api/v1/health
```

### Option 2: Local Development

1. **Setup development environment:**

```bash
make setup
source venv/bin/activate
```

2. **Install FFmpeg (if not already installed):**

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Windows
# Download from https://ffmpeg.org/download.html
```

3. **Configure environment:**

```bash
cp configs/.env.example .env
# Edit .env and add your SONIOX_API_KEY
```

4. **Run the service:**

```bash
make run-dev
```

## API Documentation

Once the service is running, visit:

- **Interactive API Docs:** http://localhost:8000/docs
- **Alternative Docs:** http://localhost:8000/redoc

### Key Endpoints

#### Start Transcription

```bash
POST /api/v1/transcribe/start
Content-Type: application/json

{
  "unique_id": "lecture_101",
  "options": {
    "language": "en-US",
    "include_word_timestamps": true,
    "vocabulary": ["physics", "quantum"],
    "enable_speaker_diarization": false
  }
}
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "started",
  "stream_url": "ws://localhost:8000/api/v1/ws/transcribe/lecture_101"
}
```

#### WebSocket Connection

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/transcribe/lecture_101');

ws.onmessage = (event) => {
  const segment = JSON.parse(event.data);
  console.log('Transcription:', segment.text);
  console.log('Is Final:', segment.is_final);
  console.log('Words:', segment.words);
};
```

#### SSE Connection

```javascript
const eventSource = new EventSource('http://localhost:8000/api/v1/transcribe/lecture_101/stream');

eventSource.addEventListener('message', (event) => {
  const segment = JSON.parse(event.data);
  console.log('Transcription:', segment.text);
});
```

#### Stop Transcription

```bash
POST /api/v1/transcribe/stop
Content-Type: application/json

{
  "unique_id": "lecture_101"
}
```

#### Health Check

```bash
GET /api/v1/health
```

**Response:**
```json
{
  "status": "healthy",
  "active_streams": 5,
  "version": "1.0.0"
}
```

#### List Active Sessions

```bash
GET /api/v1/sessions
```

## Configuration

### Environment Variables

Edit `.env` file or set environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `SONIOX_API_KEY` | Soniox API key (required) | - |
| `RTMP_BASE_URL` | Base URL for HLS streams | `https://rtmp.arivihan.com:8888` |
| `HOST` | Service host | `0.0.0.0` |
| `PORT` | Service port | `8000` |
| `MAX_CONCURRENT_STREAMS` | Max concurrent transcriptions | `10` |
| `SONIOX_SAMPLE_RATE` | Audio sample rate | `16000` |
| `DEBUG` | Enable debug mode | `False` |
| `LOG_LEVEL` | Logging level | `INFO` |

### Stream Options

Configure transcription behavior per stream:

```python
{
  "language": "en-US",                      # Primary language
  "include_word_timestamps": true,          # Word-level timing
  "vocabulary": ["term1", "term2"],         # Custom vocabulary
  "enable_punctuation": true,               # Auto punctuation
  "enable_numerals": true,                  # Convert numbers
  "enable_language_identification": false,  # Detect language per word
  "enable_speaker_diarization": false,      # Identify speakers
  "enable_endpoint_detection": true,        # Detect speech endpoints
  "language_hints": ["en", "es"]           # Language hints for better accuracy
}
```

## Development

### Available Make Commands

```bash
make help           # Show all available commands
make setup          # Setup development environment
make run-dev        # Run service locally with hot-reload
make test           # Run test suite
make format         # Format code with black and isort
make lint           # Run linters (pylint, mypy)
make docker-build   # Build Docker image
make docker-up      # Start with Docker Compose
make docker-down    # Stop Docker services
make docker-logs    # View Docker logs
make clean          # Clean up generated files
```

### Running Tests

```bash
make test
```

### Code Formatting

```bash
make format
```

### Linting

```bash
make lint
```

## Transcription Output Format

```json
{
  "unique_id": "lecture_101",
  "segment_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2024-01-15T10:30:45.123Z",
  "stream_time": 125.45,
  "text": "Hello everyone, welcome to today's lecture",
  "is_final": true,
  "words": [
    {
      "text": "Hello",
      "start_time": 125.45,
      "end_time": 125.78,
      "confidence": 0.98,
      "speaker": "S1",
      "language": "en"
    }
  ],
  "metadata": {}
}
```

## Troubleshooting

### FFmpeg Not Found

Ensure FFmpeg is installed and available in PATH:

```bash
ffmpeg -version
```

### Stream Not Available

Verify the HLS stream is accessible:

```bash
curl -I https://rtmp.arivihan.com:8888/{unique_id}/index.m3u8
```

### Connection Issues

Check Soniox API key is correctly set:

```bash
echo $SONIOX_API_KEY
```

### Docker Issues

View logs for debugging:

```bash
make docker-logs
```

## Performance

- **Latency:** <2 seconds end-to-end
- **Accuracy:** >90% for clear audio
- **Concurrent Streams:** Up to 50+ (configurable)

## Production Deployment

1. **Set production environment variables:**

```bash
DEBUG=False
LOG_LEVEL=INFO
MAX_CONCURRENT_STREAMS=50
```

2. **Remove development volume mounts from docker-compose.yml**

3. **Use a reverse proxy (nginx/traefik) for HTTPS**

4. **Configure CORS appropriately in src/main.py**

5. **Set up monitoring and logging aggregation**

## API Rate Limits

Soniox API has rate limits. Monitor usage and implement appropriate backoff strategies for production use.

## License

[Your License Here]

## Support

For issues and questions:
- Create an issue in the repository
- Check the PRD.md for detailed specifications
- Review Soniox documentation: https://soniox.com/docs

## Roadmap

- [ ] Phase 2: Redis integration for transcript storage
- [ ] Multi-language support
- [ ] Transcript search and retrieval
- [ ] Advanced analytics (speaking rate, silence detection)
- [ ] Custom vocabulary management UI
