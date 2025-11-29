from pydantic import BaseModel
from typing import Optional, Set
from datetime import datetime
from enum import Enum


class StreamStatus(str, Enum):
    """Enum for stream session status."""
    PENDING = "pending"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class StreamSession(BaseModel):
    """Represents an active stream transcription session."""
    session_id: str
    unique_id: str
    status: StreamStatus = StreamStatus.PENDING
    started_at: datetime
    stopped_at: Optional[datetime] = None
    error: Optional[str] = None
    hls_url: str

    class Config:
        use_enum_values = True


class StreamInfo(BaseModel):
    """Information about a stream session."""
    session_id: str
    unique_id: str
    status: StreamStatus
    started_at: datetime
    uptime_seconds: Optional[float] = None
    connected_clients: int = 0
