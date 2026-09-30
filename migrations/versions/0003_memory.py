"""0003_memory: memory_items, commitments, service_meta.

memory_items rows are the typed episodic store. Status transitions are
enforced by the MemoryService; the schema only pins value domains with
CHECK constraints.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "kind",
            sa.Text,
            sa.CheckConstraint("kind IN ('fact', 'preference', 'project_note')"),
            nullable=False,
        ),
        sa.Column(
            "title",
            sa.Text,
            sa.CheckConstraint("length(title) BETWEEN 1 AND 120"),
            nullable=False,
        ),
        sa.Column(
            "content",
            sa.Text,
            sa.CheckConstraint("length(content) BETWEEN 1 AND 4000"),
            nullable=False,
        ),
        sa.Column(
            "provenance",
            sa.Text,
            sa.CheckConstraint(
                "provenance IN ('user_explicit', 'system_derived', 'llm_inferred', 'imported')"
            ),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.Float,
            sa.CheckConstraint("confidence >= 0.0 AND confidence <= 1.0"),
            nullable=False,
        ),
        sa.Column(
            "sensitivity",
            sa.Text,
            sa.CheckConstraint("sensitivity IN ('standard', 'sensitive')"),
            nullable=False,
            server_default="standard",
        ),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('pending', 'active', 'superseded', 'expired', 'revoked', 'deleted')"
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id", ondelete="SET NULL")),
        sa.Column("source_message_id", sa.Text, sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("superseded_by", sa.Text, sa.ForeignKey("memory_items.id")),
        sa.Column("valid_from", sa.Text, nullable=True),
        sa.Column("valid_until", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_index("ix_memory_kind_status", "memory_items", ["kind", "status"])
    op.create_index("ix_memory_created", "memory_items", ["created_at"])
    op.create_index(
        "ix_memory_valid_until",
        "memory_items",
        ["valid_until"],
        sqlite_where=sa.text("status = 'active' AND valid_until IS NOT NULL"),
    )
    op.create_table(
        "commitments",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "title",
            sa.Text,
            sa.CheckConstraint("length(title) BETWEEN 1 AND 200"),
            nullable=False,
        ),
        sa.Column(
            "detail",
            sa.Text,
            sa.CheckConstraint("detail IS NULL OR length(detail) <= 2000"),
            nullable=True,
        ),
        sa.Column("due_at", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint("status IN ('open', 'fulfilled', 'expired', 'cancelled')"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("created_by", sa.Text, nullable=False, server_default="local_user"),
        sa.Column("source_message_id", sa.Text, sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("fulfilled_at", sa.Text, nullable=True),
        sa.Column("last_surfaced_at", sa.Text, nullable=True),
    )
    op.create_index("ix_commitments_status_due", "commitments", ["status", "due_at"])
    op.create_table(
        "service_meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("service_meta")
    op.drop_index("ix_commitments_status_due", table_name="commitments")
    op.drop_table("commitments")
    op.drop_index("ix_memory_valid_until", table_name="memory_items")
    op.drop_index("ix_memory_created", table_name="memory_items")
    op.drop_index("ix_memory_kind_status", table_name="memory_items")
    op.drop_table("memory_items")
