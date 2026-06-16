"""Wave 2.7 Agent L: cross-portal deduplication schema.

New table:
  listing_cluster      cluster identity for cross-portal duplicates

New column on listing:
  cluster_id           INTEGER NULL  references listing_cluster.cluster_id

Schema rationale:
  - centroid_lat / centroid_lng are nullable. Rightmove's stored coordinates
    are unreliable (Wave 2.7 Phase 1 finding: some rows are 1 to 2.5 km off
    the actual property). When member coordinates disagree by more than the
    geo-agreement radius the centroid is left null.
  - price_pcm_min / price_pcm_max record the price band the cluster spans.
  - primary_uid is the canonical viewer uid chosen by homehunt.dedup.choose_primary.
  - first_seen / last_seen track cluster lifetime as plain ISO strings to
    keep the migration backend-portable (SQLite stores TIMESTAMP loosely).

Revision ID: 1777920000
Revises: 1777896369
Create Date: 2026-05-08

"""

from alembic import op
import sqlalchemy as sa

revision = "1777920000"
down_revision = "1777896369"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listing_cluster",
        sa.Column("cluster_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("centroid_lat", sa.Float(), nullable=True),
        sa.Column("centroid_lng", sa.Float(), nullable=True),
        sa.Column("bedrooms", sa.Integer(), nullable=False),
        sa.Column("price_pcm_min", sa.Integer(), nullable=False),
        sa.Column("price_pcm_max", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("primary_uid", sa.String(), nullable=False),
        sa.Column("first_seen", sa.String(), nullable=False),
        sa.Column("last_seen", sa.String(), nullable=False),
    )

    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "cluster_id",
                sa.Integer(),
                sa.ForeignKey(
                    "listing_cluster.cluster_id",
                    name="fk_listing_cluster_id",
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.drop_column("cluster_id")
    op.drop_table("listing_cluster")
