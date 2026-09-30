"""0001_core_state: sessions, messages, tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint("status IN ('active','closed')"),
            nullable=False,
        ),
        sa.Column("locale", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "session_id",
            sa.Text,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text, nullable=True),
        sa.Column(
            "role",
            sa.Text,
            sa.CheckConstraint("role IN ('user','assistant','system')"),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_messages_session_created", "messages", ["session_id", "created_at"])
    op.create_index(
        "uq_messages_session_idemkey",
        "messages",
        ["session_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "session_id",
            sa.Text,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column(
            "state",
            sa.Text,
            sa.CheckConstraint(
                "state IN ('pending','planning','running','waiting_confirmation',"
                "'completed','failed','cancelled')"
            ),
            nullable=False,
        ),
        sa.Column("input_json", sa.Text, nullable=False),
        sa.Column("plan_json", sa.Text, nullable=True),
        sa.Column("result_json", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("current_step", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_index("ix_tasks_session_created", "tasks", ["session_id", "created_at"])
    op.create_index("ix_tasks_state", "tasks", ["state"])


def downgrade() -> None:
    op.drop_index("ix_tasks_state", table_name="tasks")
    op.drop_index("ix_tasks_session_created", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("uq_messages_session_idemkey", table_name="messages")
    op.drop_index("ix_messages_session_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("sessions")
