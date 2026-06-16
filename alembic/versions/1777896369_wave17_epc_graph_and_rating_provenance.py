"""Wave 1.7: EPC certificate URL and rating provenance columns.

New columns on listing:
  epc_graph_url           VARCHAR  NULL  (first propertyData.epcGraphs URL)
  epc_rating_source       VARCHAR  NULL  (epc_api | vision_ocr)
  epc_rating_confidence   VARCHAR  NULL  (sap_derived | epc_api_match)

Revision ID: 1777896369
Revises: 1777868400
Create Date: 2026-05-04

"""

from alembic import op
import sqlalchemy as sa

revision = "1777896369"
down_revision = "1777868400"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("epc_graph_url", sa.VARCHAR(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("epc_rating_source", sa.VARCHAR(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("epc_rating_confidence", sa.VARCHAR(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        batch_op.drop_column("epc_rating_confidence")
        batch_op.drop_column("epc_rating_source")
        batch_op.drop_column("epc_graph_url")
