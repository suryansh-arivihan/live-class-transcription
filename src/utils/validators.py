import re
from typing import Optional
import aiohttp
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def validate_unique_id(unique_id: str) -> bool:
    """
    Validate unique_id format.

    Args:
        unique_id: Stream unique identifier

    Returns:
        True if valid, False otherwise
    """
    # Allow alphanumeric characters, hyphens, and underscores
    pattern = r'^[a-zA-Z0-9_-]+$'
    if not re.match(pattern, unique_id):
        logger.warning(f"Invalid unique_id format: {unique_id}")
        return False
    return True


def build_hls_url(unique_id: str) -> str:
    """
    Build HLS stream URL from unique_id.

    Args:
        unique_id: Stream unique identifier

    Returns:
        Complete HLS URL
    """
    return f"{settings.RTMP_BASE_URL}/{unique_id}/{unique_id}.m3u8"


async def validate_stream_availability(hls_url: str) -> bool:
    """
    Validate that HLS stream is available.

    Args:
        hls_url: HLS stream URL

    Returns:
        True if stream is available, False otherwise
    """
    try:
        # Create SSL context that doesn't verify certificates
        connector = aiohttp.TCPConnector(ssl=False)

        async with aiohttp.ClientSession(connector=connector) as session:
            # Try HEAD first
            try:
                async with session.head(hls_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        logger.info(f"Stream available at {hls_url} (HEAD)")
                        return True
                    elif response.status == 405:
                        # Method not allowed, try GET
                        logger.info(f"HEAD not supported, trying GET for {hls_url}")
                    else:
                        logger.warning(f"HEAD request failed with status {response.status}")
            except Exception as e:
                logger.warning(f"HEAD request failed: {e}, trying GET")

            # Fallback to GET request
            async with session.get(hls_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    logger.info(f"Stream available at {hls_url} (GET)")
                    return True
                else:
                    logger.warning(f"Stream not available at {hls_url}, status: {response.status}")
                    return False

    except aiohttp.ClientError as e:
        logger.error(f"Error checking stream availability: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking stream: {e}")
        return False


def validate_vocabulary(vocabulary: list) -> bool:
    """
    Validate custom vocabulary list.

    Args:
        vocabulary: List of vocabulary terms

    Returns:
        True if valid, False otherwise
    """
    if not isinstance(vocabulary, list):
        return False

    # Check each term is a non-empty string
    for term in vocabulary:
        if not isinstance(term, str) or not term.strip():
            logger.warning(f"Invalid vocabulary term: {term}")
            return False

    return True
