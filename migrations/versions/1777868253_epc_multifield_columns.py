"""Add 19 EPC multi-field enrichment columns to listing table.

Revision ID: 1777868253
Revises: (none -- first migration in this project)
Create Date: 2026-05-04

Adds the columns defined in spec section 9 of
docs/viewer-and-calibration-loop.md (EPC multi-field enrichment per section 8a).

Column list:
  total_floor_area_sqm       REAL    -- source for size_sqft fallback
  current_energy_efficiency  INTEGER -- SAP score 0-100 (current)
  potential_energy_efficiency INTEGER -- SAP score 0-100 (after upgrades)
  number_habitable_rooms     INTEGER -- cross-check vs Rightmove bedrooms
  number_heated_rooms        INTEGER -- cross-check
  built_form                 TEXT    -- Mid-Terrace, Detached, etc.
  property_type_epc          TEXT    -- cross-check vs Rightmove property_type
  construction_age_band      TEXT    -- build era
  tenure_epc                 TEXT    -- rental-private vs owner-occupied
  windows_description        TEXT    -- Single-glazed / fully double-glazed
  windows_energy_eff         TEXT    -- five-band rating
  walls_description          TEXT    -- wall construction summary
  walls_energy_eff           TEXT    -- five-band rating
  roof_description           TEXT    -- roof construction summary
  floor_level                TEXT    -- storey within the building
  mains_gas_flag             TEXT    -- Y/N gas hookup
  heating_cost_current       INTEGER -- annual heating cost in GBP
  epc_lodgement_date         TEXT    -- when the cert was filed
  epc_inspection_date        TEXT    -- when the assessor visited

Note: epc_rating already exists on the listing table from the original schema.
Note: size_sqft already exists on the listing table from the original schema.
"""

from alembic import op
import sqlalchemy as sa

# Alembic revision identifiers
revision = "1777868253"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("listing", sa.Column("total_floor_area_sqm", sa.Float(), nullable=True))
    op.add_column("listing", sa.Column("current_energy_efficiency", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("potential_energy_efficiency", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("number_habitable_rooms", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("number_heated_rooms", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("built_form", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("property_type_epc", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("construction_age_band", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("tenure_epc", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("windows_description", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("windows_energy_eff", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("walls_description", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("walls_energy_eff", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("roof_description", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("floor_level", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("mains_gas_flag", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("heating_cost_current", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("epc_lodgement_date", sa.Text(), nullable=True))
    op.add_column("listing", sa.Column("epc_inspection_date", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("listing", "epc_inspection_date")
    op.drop_column("listing", "epc_lodgement_date")
    op.drop_column("listing", "heating_cost_current")
    op.drop_column("listing", "mains_gas_flag")
    op.drop_column("listing", "floor_level")
    op.drop_column("listing", "roof_description")
    op.drop_column("listing", "walls_energy_eff")
    op.drop_column("listing", "walls_description")
    op.drop_column("listing", "windows_energy_eff")
    op.drop_column("listing", "windows_description")
    op.drop_column("listing", "tenure_epc")
    op.drop_column("listing", "construction_age_band")
    op.drop_column("listing", "property_type_epc")
    op.drop_column("listing", "built_form")
    op.drop_column("listing", "number_heated_rooms")
    op.drop_column("listing", "number_habitable_rooms")
    op.drop_column("listing", "potential_energy_efficiency")
    op.drop_column("listing", "current_energy_efficiency")
    op.drop_column("listing", "total_floor_area_sqm")
