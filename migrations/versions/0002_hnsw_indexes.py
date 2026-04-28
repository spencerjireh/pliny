"""partial HNSW indexes per (granularity, model_name) for embeddings_1536

One pair of indexes per active embedding model. Adding a new model = new
migration. Filter recall stays at 1.0 because every search query targets
exactly one partial index by predicate.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_MODEL = "text-embedding-3-small"
SAFE = "text_embedding_3_small"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX embeddings_1536_chunk_{SAFE}
          ON embeddings_1536 USING hnsw (vector vector_cosine_ops)
          WHERE granularity = 'chunk' AND model_name = '{DEFAULT_MODEL}'
        """
    )
    op.execute(
        f"""
        CREATE INDEX embeddings_1536_summary_{SAFE}
          ON embeddings_1536 USING hnsw (vector vector_cosine_ops)
          WHERE granularity = 'summary' AND model_name = '{DEFAULT_MODEL}'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS embeddings_1536_summary_{SAFE}")
    op.execute(f"DROP INDEX IF EXISTS embeddings_1536_chunk_{SAFE}")
