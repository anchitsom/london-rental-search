"""Add let_available_iso column."""

from alembic import op
import sqlalchemy as sa


revision = "1778500000"
down_revision = "1778480000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("let_available_iso", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("listing", "let_available_iso")
