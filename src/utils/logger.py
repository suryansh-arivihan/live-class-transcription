import logging
import sys
import socket
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from pythonjsonlogger import jsonlogger

from src.config import settings

# ---------------------------------------------------------------------------
# Context variables — automatically propagated through asyncio tasks
# ---------------------------------------------------------------------------
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
unique_id_var: ContextVar[str] = ContextVar("unique_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")

_HOSTNAME = socket.gethostname()


def set_log_context(
    *,
    request_id: Optional[str] = None,
    unique_id: Optional[str] = None,
    session_id: Optional[str] = None,
):
    """Set one or more context variables for the current async scope."""
    if request_id is not None:
        request_id_var.set(request_id)
    if unique_id is not None:
        unique_id_var.set(unique_id)
    if session_id is not None:
        session_id_var.set(session_id)


# ---------------------------------------------------------------------------
# Logging filter — injects context vars into every log record automatically
# ---------------------------------------------------------------------------
class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        record.unique_id = unique_id_var.get("-")
        record.session_id = session_id_var.get("-")
        record.service = settings.SERVICE_NAME
        record.hostname = _HOSTNAME
        return True


# ---------------------------------------------------------------------------
# JSON formatter — produces structured JSON lines for CloudWatch parsing
# ---------------------------------------------------------------------------
class StructuredJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict):
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        )
        log_record["level"] = record.levelname
        log_record["service"] = getattr(record, "service", settings.SERVICE_NAME)
        log_record["version"] = settings.VERSION
        log_record["hostname"] = getattr(record, "hostname", _HOSTNAME)
        log_record["logger"] = record.name
        log_record["module"] = record.module
        log_record["function"] = record.funcName
        log_record["line"] = record.lineno
        log_record["request_id"] = getattr(record, "request_id", "-")
        log_record["unique_id"] = getattr(record, "unique_id", "-")
        log_record["session_id"] = getattr(record, "session_id", "-")

        if record.exc_info and not log_record.get("exc_info"):
            log_record["exc_info"] = self.formatException(record.exc_info)


# ---------------------------------------------------------------------------
# Plain-text formatter — for local development readability
# ---------------------------------------------------------------------------
_TEXT_FMT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "req=%(request_id)s uid=%(unique_id)s sid=%(session_id)s | "
    "[%(filename)s:%(lineno)d] %(message)s"
)


# ---------------------------------------------------------------------------
# CloudWatch handler (watchtower) — optional, enabled via config
# ---------------------------------------------------------------------------
def _create_cloudwatch_handler() -> Optional[logging.Handler]:
    if not settings.CLOUDWATCH_ENABLED:
        return None

    try:
        import watchtower
        import boto3

        region = settings.CLOUDWATCH_REGION or settings.AWS_REGION
        log_stream = settings.CLOUDWATCH_LOG_STREAM or f"{settings.SERVICE_NAME}/{_HOSTNAME}"

        boto3_kwargs = {"region_name": region}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            boto3_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            boto3_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY

        cw_client = boto3.client("logs", **boto3_kwargs)

        handler = watchtower.CloudWatchLogHandler(
            log_group_name=settings.CLOUDWATCH_LOG_GROUP,
            log_stream_name=log_stream,
            boto3_client=cw_client,
            create_log_group=True,
            send_interval=5,
            max_batch_count=100,
        )
        handler.setFormatter(
            StructuredJsonFormatter("%(message)s")
        )
        handler.addFilter(ContextFilter())
        return handler
    except Exception as e:
        print(f"[WARN] Failed to initialize CloudWatch handler: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------
_configured_loggers: set = set()


def setup_logger(name: str = None) -> logging.Logger:
    """
    Create a structured, context-aware logger.

    Every log record automatically includes request_id, unique_id,
    session_id, timestamp, service name, and hostname.

    Args:
        name: Logger name (typically ``__name__`` of the calling module)
    """
    logger = logging.getLogger(name)

    if name in _configured_loggers:
        return logger

    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    if logger.hasHandlers():
        logger.handlers.clear()

    # --- stdout handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    if settings.LOG_FORMAT.lower() == "json":
        console_handler.setFormatter(
            StructuredJsonFormatter("%(message)s")
        )
    else:
        console_handler.setFormatter(
            logging.Formatter(fmt=_TEXT_FMT, datefmt="%Y-%m-%d %H:%M:%S")
        )

    console_handler.addFilter(ContextFilter())
    logger.addHandler(console_handler)

    # --- CloudWatch handler (optional) ---
    cw_handler = _create_cloudwatch_handler()
    if cw_handler:
        cw_handler.setLevel(log_level)
        logger.addHandler(cw_handler)

    logger.propagate = False
    _configured_loggers.add(name)
    return logger
