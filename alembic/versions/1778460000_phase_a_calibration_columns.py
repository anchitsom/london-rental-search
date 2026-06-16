"""Phase A calibration columns.

Adds:
  - photo_count          INTEGER  count of photos in the listing
  - floor_level_int      INTEGER  parsed/normalised floor level
  - size_sqft_credibility TEXT    trusted | conflict | missing
  - is_let_agreed        BOOLEAN  title-derived let-agreed flag

Revision ID: 1778460000
Revises: 1778457600
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa


revision = "1778460000"
down_revision = "1778457600"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        batch.add_column(sa.Column("photo_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("floor_level_int", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("size_sqft_credibility", sa.String(), nullable=True))
        batch.add_column(sa.Column("is_let_agreed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        batch.drop_column("is_let_agreed")
        batch.drop_column("size_sqft_credibility")
        batch.drop_column("floor_level_int")
        batch.drop_column("photo_count")
