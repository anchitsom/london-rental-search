"""Add carpet count columns from the two-step pipeline.

Adds two columns to `listing` populated by detect_carpet_two_step
(2026-05-10):
  - bedrooms_with_carpet  INTEGER  count of unique bedrooms with carpet
  - has_living_area_carpet  BOOLEAN  majority-vote on living-area carpet

These supplement the existing carpet_in_bedroom and carpet_other_areas
booleans, which the two-step detector still populates for backward
compatibility.

Revision ID: 1778457600
Revises: 1777930000
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa


revision = "1778457600"
down_revision = "1777930000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        batch.add_column(sa.Column("bedrooms_with_carpet", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("has_living_area_carpet", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        batch.drop_column("has_living_area_carpet")
        batch.drop_column("bedrooms_with_carpet")
