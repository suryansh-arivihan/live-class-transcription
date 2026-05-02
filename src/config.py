from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Service Configuration
    SERVICE_NAME: str = "transcription-service"
    VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    PUBLIC_HOST: str = "localhost"  # Advertised host returned to clients (not the bind address)
    WORKERS: int = 4

    # Stream Configuration
    RTMP_BASE_URL: str = "https://antmedia.arivihan.com:5443/LiveApp/streams"
    MAX_CONCURRENT_STREAMS: int = 10
    STREAM_TIMEOUT: int = 300  # seconds
    STREAM_RECONNECT_TIMEOUT: int = 120  # seconds to poll for new HLS segments before closing session
    SESSION_END_POLL_INTERVAL: int = 10  # seconds between DynamoDB session-end checks
    SESSION_END_POLL_TIMEOUT: int = 3600  # max seconds to wait for session end signal (safety net)
    SESSION_ENDS_TABLE_NAME: str = "live-chat-session-ends"  # DynamoDB table for session-end entries

    # Soniox Configuration
    SONIOX_API_KEY: str
    SONIOX_WS_URL: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    SONIOX_SAMPLE_RATE: int = 16000
    SONIOX_MODEL: str = "stt-rt-v3"
    SONIOX_CONNECT_VERIFY_TIMEOUT: float = 2.0  # seconds to wait for config-reject frame after connect

    # AWS Transcribe Configuration (fallback)
    AWS_TRANSCRIBE_REGION: Optional[str] = None  # defaults to AWS_REGION if unset
    AWS_TRANSCRIBE_LANGUAGE_CODE: str = "hi-IN"
    AWS_TRANSCRIBE_SAMPLE_RATE: int = 16000
    AWS_TRANSCRIBE_MEDIA_ENCODING: str = "pcm"

    # Audio Configuration
    AUDIO_CHUNK_SIZE: int = 8000  # 0.5 seconds at 16kHz
    AUDIO_BUFFER_SIZE: int = 32000  # 2 seconds buffer

    # Redis Configuration (Phase 2)
    REDIS_URL: Optional[str] = None
    REDIS_TTL: int = 3600

    # AWS/DynamoDB Configuration
    AWS_REGION: str = "ap-south-1"
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    DYNAMODB_TABLE_NAME: str = "transcription_chunks"
    DYNAMODB_CHUNK_DURATION: int = 5  # seconds

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # "json" for structured (production), "text" for plain (local dev)

    # CloudWatch Logging
    CLOUDWATCH_ENABLED: bool = False
    CLOUDWATCH_LOG_GROUP: str = "/app/transcription-service"
    CLOUDWATCH_LOG_STREAM: Optional[str] = None  # Auto-generated from hostname if None
    CLOUDWATCH_REGION: Optional[str] = None  # Falls back to AWS_REGION if None

    # Slack Alerts
    SLACK_WEBHOOK_URL: Optional[str] = None  # Incoming webhook URL; alerts disabled when unset
    SLACK_ALERT_COOLDOWN_SECONDS: int = 600  # Min interval between alerts for the same unique_id

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore extra env vars


# Global settings instance
settings = Settings()
