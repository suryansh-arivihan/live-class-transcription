from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class Word(BaseModel):
    """Represents a single transcribed word with timing and confidence information."""
    text: str
    start_time: float
    end_time: float
    confidence: float = Field(ge=0, le=1)
    speaker: Optional[str] = None
    language: Optional[str] = None


class TranscriptionSegment(BaseModel):
    """Represents a segment of transcribed audio with metadata."""
    unique_id: str
    segment_id: str
    timestamp: datetime
    stream_time: float
    text: str
    is_final: bool
    words: List[Word] = []
    metadata: Optional[dict] = {}


class StreamOptions(BaseModel):
    """Configuration options for transcription stream."""
    language: str = "en-US"
    include_word_timestamps: bool = True
    vocabulary: List[str] = []
    enable_punctuation: bool = True
    enable_numerals: bool = True
    enable_language_identification: bool = False
    enable_speaker_diarization: bool = False
    enable_endpoint_detection: bool = True
    language_hints: List[str] = ["en"]


class TranscriptionStartRequest(BaseModel):
    """Request model for starting a transcription session."""
    unique_id: str
    options: Optional[StreamOptions] = StreamOptions()


class TranscriptionStartResponse(BaseModel):
    """Response model for transcription start request."""
    session_id: str
    status: str
    stream_url: Optional[str] = None


class TranscriptionStopRequest(BaseModel):
    """Request model for stopping a transcription session."""
    unique_id: str


class HealthResponse(BaseModel):
    """Response model for health check endpoint."""
    status: str
    active_streams: int
    version: str
