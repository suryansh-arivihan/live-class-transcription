import asyncio
import time
from typing import Optional
import aiohttp
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class SlackNotifier:
    """Sends alerts to Slack via incoming webhook with per-key cooldown."""

    def __init__(self):
        self._last_alert_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _is_enabled(self) -> bool:
        return bool(settings.SLACK_WEBHOOK_URL)

    async def _should_send(self, dedupe_key: Optional[str]) -> bool:
        if not dedupe_key:
            return True
        async with self._lock:
            now = time.monotonic()
            last = self._last_alert_at.get(dedupe_key)
            if last is not None and (now - last) < settings.SLACK_ALERT_COOLDOWN_SECONDS:
                return False
            self._last_alert_at[dedupe_key] = now
            return True

    async def send_alert(
        self,
        text: str,
        *,
        fields: Optional[dict] = None,
        dedupe_key: Optional[str] = None,
    ) -> bool:
        """
        Send an alert to Slack. No-op when SLACK_WEBHOOK_URL is unset.

        Args:
            text: Main message line shown as fallback / notification text.
            fields: Optional key/value pairs rendered as a structured block.
            dedupe_key: When provided, suppresses repeat alerts for the same
                key within SLACK_ALERT_COOLDOWN_SECONDS.

        Returns:
            True if the alert was POSTed successfully, False otherwise (incl.
            disabled or suppressed by cooldown).
        """
        if not self._is_enabled():
            logger.debug(
                "Slack alert skipped (webhook not configured)",
                extra={"text": text, "dedupe_key": dedupe_key},
            )
            return False

        if not await self._should_send(dedupe_key):
            logger.info(
                "Slack alert suppressed by cooldown",
                extra={
                    "dedupe_key": dedupe_key,
                    "cooldown_seconds": settings.SLACK_ALERT_COOLDOWN_SECONDS,
                },
            )
            return False

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if fields:
            field_text = "\n".join(f"*{k}:* {v}" for k, v in fields.items())
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": field_text}}
            )

        payload = {"text": text, "blocks": blocks}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    settings.SLACK_WEBHOOK_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status >= 400:
                        body = await response.text()
                        logger.error(
                            "Slack webhook returned error",
                            extra={
                                "http_status": response.status,
                                "response_body": body[:300],
                                "dedupe_key": dedupe_key,
                            },
                        )
                        return False
                    logger.info(
                        "Slack alert sent",
                        extra={"dedupe_key": dedupe_key, "text": text[:200]},
                    )
                    return True
        except Exception as e:
            logger.exception(
                "Error posting Slack alert",
                extra={"error": str(e), "dedupe_key": dedupe_key},
            )
            return False


slack_notifier = SlackNotifier()
