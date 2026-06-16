"""Add Wave 1.6 provenance columns and EPC multi-field columns.

Wave 1.6: floorplan vision enrichment + source provenance + sanity rules.

New columns on listing:
  size_sqft_source     VARCHAR  NULL  (structured|nlp|floorplan_vision|epc_direct|epc_interpolated)
  size_sqft_confidence REAL     NULL  (0.0-1.0)
  floorplan_data       TEXT     NULL  (JSON from floorplan vision extractor)

EPC multi-field columns (from Wave 1 Agent B, consolidated here since the
  original Agent B migration was in migrations/versions/ not alembic/versions/):
  total_floor_area_sqm         REAL     NULL
  current_energy_efficiency    INTEGER  NULL
  potential_energy_efficiency  INTEGER  NULL
  number_habitable_rooms       INTEGER  NULL
  number_heated_rooms          INTEGER  NULL
  built_form                   VARCHAR  NULL
  property_type_epc            VARCHAR  NULL
  construction_age_band        VARCHAR  NULL
  tenure_epc                   VARCHAR  NULL
  windows_description          VARCHAR  NULL
  windows_energy_eff           VARCHAR  NULL
  walls_description            VARCHAR  NULL
  walls_energy_eff             VARCHAR  NULL
  roof_description             VARCHAR  NULL
  floor_level                  VARCHAR  NULL
  mains_gas_flag               VARCHAR  NULL
  heating_cost_current         INTEGER  NULL
  epc_lodgement_date           VARCHAR  NULL
  epc_inspection_date          VARCHAR  NULL
  score_breakdown              VARCHAR  NULL
  region                       VARCHAR  NULL

Revision ID: 1777868400
Revises: 1777868359
Create Date: 2026-05-04

"""

from alembic import op
import sqlalchemy as sa

revision = "1777868400"
down_revision = "1777868359"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        # Provenance columns (Wave 1.6)
        batch_op.add_column(
            sa.Column("size_sqft_source", sa.VARCHAR(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("size_sqft_confidence", sa.REAL(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("floorplan_data", sa.TEXT(), nullable=True)
        )

        # EPC multi-field columns (Wave 1 Agent B -- consolidated)
        for col_name, col_type in [
            ("total_floor_area_sqm", sa.REAL()),
            ("current_energy_efficiency", sa.INTEGER()),
            ("potential_energy_efficiency", sa.INTEGER()),
            ("number_habitable_rooms", sa.INTEGER()),
            ("number_heated_rooms", sa.INTEGER()),
            ("built_form", sa.VARCHAR()),
            ("property_type_epc", sa.VARCHAR()),
            ("construction_age_band", sa.VARCHAR()),
            ("tenure_epc", sa.VARCHAR()),
            ("windows_description", sa.VARCHAR()),
            ("windows_energy_eff", sa.VARCHAR()),
            ("walls_description", sa.VARCHAR()),
            ("walls_energy_eff", sa.VARCHAR()),
            ("roof_description", sa.VARCHAR()),
            ("floor_level", sa.VARCHAR()),
            ("mains_gas_flag", sa.VARCHAR()),
            ("heating_cost_current", sa.INTEGER()),
            ("epc_lodgement_date", sa.VARCHAR()),
            ("epc_inspection_date", sa.VARCHAR()),
            ("score_breakdown", sa.TEXT()),
        ]:
            batch_op.add_column(sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listing", schema=None) as batch_op:
        for col_name in [
            "size_sqft_source",
            "size_sqft_confidence",
            "floorplan_data",
            "total_floor_area_sqm",
            "current_energy_efficiency",
            "potential_energy_efficiency",
            "number_habitable_rooms",
            "number_heated_rooms",
            "built_form",
            "property_type_epc",
            "construction_age_band",
            "tenure_epc",
            "windows_description",
            "windows_energy_eff",
            "walls_description",
            "walls_energy_eff",
            "roof_description",
            "floor_level",
            "mains_gas_flag",
            "heating_cost_current",
            "epc_lodgement_date",
            "epc_inspection_date",
            "score_breakdown",
        ]:
            batch_op.drop_column(col_name)
