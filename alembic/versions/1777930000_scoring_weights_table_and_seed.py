"""Pre-Wave-3 unblocker 2.2: scoring_weights table + seed row.

Ports the legacy non-alembic migration
`homehunt/migrations/20260504041646_scoring_weights_and_score_breakdown.py`
into the alembic chain so fresh DBs and the production DB acquire the
table via a single mechanism.

Note on `score_breakdown`: the legacy migration also added a
`score_breakdown` column to `listing`. That column is already present in
the production DB and in the SQLModel metadata, so it is not re-applied
here. This revision is purely about the `scoring_weights` table and its
seed row.

Seed row:
  source       = 'seed'
  weights_json = JSON of scoring.components.*.weight from
                 filter-scoring-config.yaml at the project root.

Revision ID: 1777930000
Revises: 1777920000
Create Date: 2026-05-08

"""

import json
import os
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
import yaml


revision = "1777930000"
down_revision = "1777920000"
branch_labels = None
depends_on = None


# Path to the unified config. Resolved relative to this migration file so the
# upgrade works from any working directory (alembic prepends the project root
# to sys.path; the file lives two levels up from versions/).
_CONFIG_YAML = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir,
        os.pardir,
        "filter-scoring-config.yaml",
    )
)


def _load_seed_weights() -> dict:
    """
    Read scoring.components.*.weight from filter-scoring-config.yaml.

    Returns a dict mapping component name to weight. The component names
    are the canonical seed shape (price, carpet_bedroom, carpet_other_areas,
    size, bedrooms, epc, transport, outside, region_tier).
    """
    with open(_CONFIG_YAML, "r") as fh:
        cfg = yaml.safe_load(fh) or {}
    components = (cfg.get("scoring") or {}).get("components") or {}
    return {k: v.get("weight", 0.0) for k, v in components.items()}


def upgrade() -> None:
    op.create_table(
        "scoring_weights",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("fit_metrics_json", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    seed_weights = _load_seed_weights()
    weights_json = json.dumps(seed_weights)
    created_at = datetime.now(timezone.utc).isoformat()

    op.execute(
        sa.text(
            "INSERT INTO scoring_weights "
            "(created_at, source, weights_json) "
            "VALUES (:created_at, :source, :weights_json)"
        ).bindparams(
            created_at=created_at,
            source="seed",
            weights_json=weights_json,
        )
    )


def downgrade() -> None:
    op.drop_table("scoring_weights")
