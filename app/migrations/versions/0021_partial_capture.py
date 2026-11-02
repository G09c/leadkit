"""Capturas parciais isoladas das submissões completas."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_partial_capture"
down_revision = "0020_draft_test_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "partial_captures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("form_slug", sa.String(64), nullable=False),
        sa.Column("form_version_number", sa.Integer(), nullable=False),
        sa.Column("create_key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("sealed_token", sa.Text(), nullable=False),
        sa.Column("answers", JSONB(), nullable=False),
        sa.Column("consent", JSONB(), nullable=False),
        sa.Column("tracking", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("last_request_id", sa.Uuid(), nullable=False),
        sa.Column("last_digest", sa.String(64), nullable=False),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_partial_captures_form_slug", "partial_captures", ["form_slug"])
    op.create_index("ix_partial_captures_expires_at", "partial_captures", ["expires_at"])

    op.create_table(
        "partial_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "partial_id",
            sa.Uuid(),
            sa.ForeignKey("partial_captures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("destination_id", sa.Uuid(), nullable=False),
        sa.Column("destination_revision", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("partial_id", "event"),
    )


def downgrade():
    raise RuntimeError("downgrade destrutivo não é suportado")
