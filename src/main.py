import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings
from src.api import routes, websocket, sse
from src.services.stream_manager import stream_manager
from src.utils.logger import setup_logger, set_log_context

logger = setup_logger(__name__)


# ---------------------------------------------------------------------------
# Request ID Middleware — generates a unique ID per request and logs
# request start / finish with duration.
# ---------------------------------------------------------------------------
class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        set_log_context(request_id=request_id)

        request.state.request_id = request_id

        logger.info(
            "Request started",
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "query_params": str(request.query_params) if request.query_params else None,
                "client_ip": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
        )

        start_time = time.monotonic()
        try:
            response: Response = await call_next(request)
        except Exception:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.exception(
                "Request failed with unhandled exception",
                extra={"duration_ms": duration_ms},
            )
            raise

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)

        response.headers["X-Request-ID"] = request_id

        logger.info(
            "Request completed",
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        return response


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(
        "Service starting",
        extra={
            "version": settings.VERSION,
            "debug": settings.DEBUG,
            "max_concurrent_streams": settings.MAX_CONCURRENT_STREAMS,
            "log_level": settings.LOG_LEVEL,
            "log_format": settings.LOG_FORMAT,
            "cloudwatch_enabled": settings.CLOUDWATCH_ENABLED,
            "cloudwatch_log_group": settings.CLOUDWATCH_LOG_GROUP if settings.CLOUDWATCH_ENABLED else None,
            "soniox_model": settings.SONIOX_MODEL,
            "aws_transcribe_language": settings.AWS_TRANSCRIBE_LANGUAGE_CODE,
            "aws_transcribe_region": settings.AWS_TRANSCRIBE_REGION or settings.AWS_REGION,
            "dynamodb_table": settings.DYNAMODB_TABLE_NAME,
            "aws_region": settings.AWS_REGION,
        },
    )

    yield

    # Shutdown
    logger.info("Service shutting down — cleaning up active sessions")
    await stream_manager.cleanup_all()
    logger.info("Shutdown cleanup completed")


# Create FastAPI application
app = FastAPI(
    title=settings.SERVICE_NAME,
    version=settings.VERSION,
    description="Live Stream Transcription Service using Soniox ASR API (with AWS Transcribe fallback)",
    lifespan=lifespan
)

# Add middleware
app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(routes.router)
app.include_router(websocket.router)
app.include_router(sse.router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": settings.SERVICE_NAME,
        "version": settings.VERSION,
        "status": "running"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )
