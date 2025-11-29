import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.exceptions import ClientError
from src.config import settings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    return obj

# Thread pool for running boto3 sync operations
_executor = ThreadPoolExecutor(max_workers=4)


class DynamoDBClient:
    """Client for storing transcription chunks in DynamoDB."""

    def __init__(self):
        self.table_name = settings.DYNAMODB_TABLE_NAME
        self.region = settings.AWS_REGION
        self._client = None
        self._table = None

    def _get_client(self):
        """Get or create DynamoDB client."""
        if self._client is None:
            self._client = boto3.resource(
                'dynamodb',
                region_name=self.region
            )
            self._table = self._client.Table(self.table_name)
        return self._table

    async def save_chunk(
        self,
        stream_id: str,
        session_id: str,
        chunk_timestamp: int,
        start_time: float,
        end_time: float,
        text: str,
        words: List[dict],
        is_final: bool = True
    ) -> bool:
        """
        Save a transcription chunk to DynamoDB.

        Args:
            stream_id: Stream unique identifier
            session_id: Transcription session ID
            chunk_timestamp: Unix timestamp in milliseconds
            start_time: Stream time start (seconds)
            end_time: Stream time end (seconds)
            text: Combined transcription text (UTF-8, supports Hindi/Devanagari)
            words: List of word objects
            is_final: Whether this is finalized text

        Returns:
            True if successful, False otherwise
        """
        chunk_id = str(uuid.uuid4())
        created_at = datetime.utcnow().isoformat()

        # Convert floats to Decimal for DynamoDB compatibility
        words_converted = convert_floats_to_decimal(words)

        item = {
            'stream_id': stream_id,
            'chunk_timestamp': chunk_timestamp,
            'chunk_id': chunk_id,
            'session_id': session_id,
            'start_time': Decimal(str(start_time)),
            'end_time': Decimal(str(end_time)),
            'text': text,  # UTF-8 encoded, supports Devanagari/Hindi
            'words': words_converted,
            'is_final': is_final,
            'created_at': created_at
        }

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                _executor,
                self._put_item,
                item
            )
            logger.info(f"Saved chunk {chunk_id} for stream {stream_id} at {chunk_timestamp}")
            return True
        except ClientError as e:
            logger.error(f"Failed to save chunk to DynamoDB: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error saving chunk: {e}")
            return False

    def _put_item(self, item: dict):
        """Synchronous put_item for thread executor."""
        table = self._get_client()
        table.put_item(Item=item)

    async def get_chunks_by_stream(
        self,
        stream_id: str,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None
    ) -> List[dict]:
        """
        Get transcription chunks for a stream.

        Args:
            stream_id: Stream unique identifier
            start_timestamp: Optional start timestamp filter
            end_timestamp: Optional end timestamp filter

        Returns:
            List of chunk items
        """
        try:
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(
                _executor,
                self._query_by_stream,
                stream_id,
                start_timestamp,
                end_timestamp
            )
            return items
        except Exception as e:
            logger.error(f"Failed to query chunks: {e}")
            return []

    def _query_by_stream(
        self,
        stream_id: str,
        start_timestamp: Optional[int],
        end_timestamp: Optional[int]
    ) -> List[dict]:
        """Synchronous query for thread executor."""
        table = self._get_client()

        key_condition = boto3.dynamodb.conditions.Key('stream_id').eq(stream_id)

        if start_timestamp and end_timestamp:
            key_condition = key_condition & boto3.dynamodb.conditions.Key('chunk_timestamp').between(
                start_timestamp, end_timestamp
            )
        elif start_timestamp:
            key_condition = key_condition & boto3.dynamodb.conditions.Key('chunk_timestamp').gte(start_timestamp)
        elif end_timestamp:
            key_condition = key_condition & boto3.dynamodb.conditions.Key('chunk_timestamp').lte(end_timestamp)

        response = table.query(KeyConditionExpression=key_condition)
        return response.get('Items', [])


# Global instance
dynamodb_client = DynamoDBClient()
