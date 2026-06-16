"""Add latent vision tags.

Adds nullable latent visual tag columns to `listing` for viewer-side filtering,
plus raw caption audit storage from the two-stage Ollama tagger.

Revision ID: 1778480000
Revises: 1778470000
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa


revision = "1778480000"
down_revision = "1778470000"
branch_labels = None
depends_on = None


TAG_COLUMNS = [
    "tag_window_style",
    "tag_ceiling_height",
    "tag_exposed_brick",
    "tag_exposed_beams_or_ducts",
    "tag_building_era",
    "tag_kitchen_finish",
    "tag_bathroom_finish",
    "tag_flooring",
    "tag_natural_light",
    "tag_wall_palette",
    "tag_open_plan",
    "tag_view",
    "tag_outdoor_access",
]

INDEXES = {
    "idx_listing_tag_window_style": "tag_window_style",
    "idx_listing_tag_building_era": "tag_building_era",
    "idx_listing_tag_flooring": "tag_flooring",
    "idx_listing_tag_outdoor_access": "tag_outdoor_access",
}


def upgrade() -> None:
    with op.batch_alter_table("listing") as batch:
        for column_name in TAG_COLUMNS:
            batch.add_column(sa.Column(column_name, sa.String(), nullable=True))
        batch.add_column(sa.Column("tags_extracted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("tag_raw_captions", sa.Text(), nullable=True))

    for index_name, column_name in INDEXES.items():
        op.create_index(index_name, "listing", [column_name])


def downgrade() -> None:
    for index_name in reversed(INDEXES):
        op.drop_index(index_name, table_name="listing")

    with op.batch_alter_table("listing") as batch:
        batch.drop_column("tag_raw_captions")
        batch.drop_column("tags_extracted_at")
        for column_name in reversed(TAG_COLUMNS):
            batch.drop_column(column_name)
