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
    WORKERS: int = 4

    # Stream Configuration
    RTMP_BASE_URL: str = "https://antmedia.arivihan.com:5443/LiveApp/streams"
    MAX_CONCURRENT_STREAMS: int = 10
    STREAM_TIMEOUT: int = 300  # seconds

    # Soniox Configuration
    SONIOX_API_KEY: str
    SONIOX_WS_URL: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    SONIOX_SAMPLE_RATE: int = 16000
    SONIOX_MODEL: str = "stt-rt-v3"

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

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore extra env vars


# Global settings instance
settings = Settings()
