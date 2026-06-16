"""Add region VARCHAR NULL to listing table.

Wave 1 Agent D: region resolution via neighbourhood centroids.

Revision ID: 1777868359
Revises:
Create Date: 2026-05-04

"""

from alembic import op
import sqlalchemy as sa

# Alembic revision identifiers
revision = "1777868359"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.add_column(sa.Column("region", sa.VARCHAR(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.drop_column("region")
