import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from pliny.storage.blob import BlobStore


@dataclass
class StageContext:
    item_id: uuid.UUID
    stage: str
    attempt: int
    claim_token: uuid.UUID
    db: AsyncSession
    blob: BlobStore
    llm: object | None  # typed as LLM once pliny.llm.base ships in chunk 9
    logger: logging.LoggerAdapter[logging.Logger]
    neo4j: object | None = None  # neo4j.AsyncDriver; used by graph_sync
