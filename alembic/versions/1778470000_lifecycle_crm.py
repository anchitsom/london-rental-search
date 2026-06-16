"""Lifecycle CRM schema.

Adds three columns on `listing` for the denormalised current state, and a
new `listing_lifecycle` history table for the audit trail of transitions.

State machine introduced 2026-05-10. States:
    new                 default
    contact_queued      user wants to contact agent
    contacted           agent replied / viewing booked
    shortlisted         viewed and approved (terminal-ish)
    triage_reject       rejected before contact (terminal)
    not_contacted       contact_queued but never reached (terminal)
    final_reject        viewed and rejected (terminal)

Revision ID: 1778470000
Revises: 1778460000
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa


revision = "1778470000"
down_revision = "1778460000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        batch.add_column(sa.Column("current_status", sa.String(), server_default="new", nullable=True))
        batch.add_column(sa.Column("current_status_reason", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_transition_at", sa.DateTime(), nullable=True))

    op.create_table(
        "listing_lifecycle",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("transitioned_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("transitioned_by", sa.String(), nullable=True),
    )
    op.create_index("idx_listing_lifecycle_uid", "listing_lifecycle", ["uid"])


def downgrade() -> None:
    op.drop_index("idx_listing_lifecycle_uid", table_name="listing_lifecycle")
    op.drop_table("listing_lifecycle")
    with op.batch_alter_table("listing") as batch:
        batch.drop_column("last_transition_at")
        batch.drop_column("current_status_reason")
        batch.drop_column("current_status")
