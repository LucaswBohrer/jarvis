"""0002_append_only_audit: audit_logs + triggers rejecting UPDATE/DELETE."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_NO_UPDATE = """
CREATE TRIGGER audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE rejected');
END;
"""

_NO_DELETE = """
CREATE TRIGGER audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE rejected');
END;
"""


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("occurred_at", sa.Text, nullable=False),
        sa.Column("correlation_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=True),
        sa.Column(
            "task_id",
            sa.Text,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("capability", sa.Text, nullable=True),
        sa.Column("tool_name", sa.Text, nullable=True),
        sa.Column("decision", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("request_summary", sa.Text, nullable=True),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("result_digest", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=True),
    )
    op.create_index("ix_audit_task_occurred", "audit_logs", ["task_id", "occurred_at"])
    op.create_index("ix_audit_correlation", "audit_logs", ["correlation_id"])
    op.execute(sa.text(_NO_UPDATE))
    op.execute(sa.text(_NO_DELETE))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_no_delete"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_no_update"))
    op.drop_index("ix_audit_correlation", table_name="audit_logs")
    op.drop_index("ix_audit_task_occurred", table_name="audit_logs")
    op.drop_table("audit_logs")
