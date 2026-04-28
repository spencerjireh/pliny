"""initial schema

Creates the v1 Postgres schema described in spec.md "Data Model (Postgres)" plus
two clarifying tables not explicitly listed there:

- image_phashes: stores per-image pHash as bigint so Postgres bit_count() can
  compute Hamming distance for the spec's near-duplicate check.
- llm_spend_daily: the cost-cap circuit breaker described in spec.md
  "LLM Provider Abstraction > Rate limiting".

HNSW indexes on embeddings_1536 are deferred to the embed slice (build-order
step 6); the partial-index predicate requires a model_name we don't yet pin.

Revision ID: 0001
Revises:
Create Date: 2026-04-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    op.create_table(
        "items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("snapshot_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extract_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summarize_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embed_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entities_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("graph_sync_version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_index("items_captured_at_idx", "items", ["captured_at"])
    op.create_index(
        "items_canonical_url_idx",
        "items",
        ["canonical_url"],
        postgresql_where=sa.text("canonical_url IS NOT NULL"),
    )
    op.create_index(
        "items_user_id_idx",
        "items",
        ["user_id"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    for col in (
        "extract_version",
        "summarize_version",
        "chunk_version",
        "embed_version",
        "entities_version",
        "graph_sync_version",
        "snapshot_version",
    ):
        op.create_index(f"items_{col}_idx", "items", [col])

    op.create_table(
        "item_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "item_sources_source_ref_uniq",
        "item_sources",
        ["source", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )
    op.create_index(
        "item_sources_item_id_captured_at_idx",
        "item_sources",
        ["item_id", "captured_at"],
    )

    op.create_table(
        "item_redirects",
        sa.Column("from_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "to_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "merged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
    )

    op.create_table(
        "content",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("extract_version", sa.Integer(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(extracted_text, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index("content_tsv_idx", "content", ["tsv"], postgresql_using="gin")

    op.create_table(
        "chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("item_id", "chunk_index", "chunker_version", name="chunks_unique"),
    )

    op.create_table(
        "embeddings_1536",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chunks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("granularity", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("vector", Vector(1536), nullable=False),
        sa.Column(
            "embedded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "granularity",
            "item_id",
            "chunk_id",
            "model_name",
            "model_version",
            name="embeddings_1536_unique",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "entities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("aliases", JSONB(), nullable=True),
        sa.UniqueConstraint("canonical_name", "type", name="entities_unique"),
    )

    op.create_table(
        "item_entities",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "entity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("mention_text", sa.Text(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("entities_version", sa.Integer(), nullable=False),
    )

    op.create_table(
        "tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
    )

    op.create_table(
        "item_tags",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "processing_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("pool", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("item_id", "stage", name="processing_jobs_item_stage_unique"),
    )
    op.create_index(
        "processing_jobs_status_pool_next_idx",
        "processing_jobs",
        ["status", "pool", "next_attempt_at"],
    )

    op.create_table(
        "image_phashes",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("phash", sa.BigInteger(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "llm_spend_daily",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column(
            "usd_spent",
            sa.Numeric(12, 4),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_spend_daily")
    op.drop_table("image_phashes")
    op.drop_index("processing_jobs_status_pool_next_idx", table_name="processing_jobs")
    op.drop_table("processing_jobs")
    op.drop_table("item_tags")
    op.drop_table("tags")
    op.drop_table("item_entities")
    op.drop_table("entities")
    op.drop_table("embeddings_1536")
    op.drop_table("chunks")
    op.drop_index("content_tsv_idx", table_name="content")
    op.drop_table("content")
    op.drop_table("item_redirects")
    op.drop_index("item_sources_source_ref_uniq", table_name="item_sources")
    op.drop_index("item_sources_item_id_captured_at_idx", table_name="item_sources")
    op.drop_table("item_sources")
    op.drop_table("items")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto;")
    op.execute("DROP EXTENSION IF EXISTS vector;")
