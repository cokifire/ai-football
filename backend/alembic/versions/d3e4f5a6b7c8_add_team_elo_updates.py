"""add idempotent Elo update ledger

Revision ID: d3e4f5a6b7c8
Revises: a1b2c3d4e5f6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Elo changes are fractional; retaining only an INT would discard the
    # learning signal after every completed fixture.
    op.alter_column("team_elos", "elo", existing_type=sa.Integer(), type_=sa.Numeric(8, 2), nullable=False)
    op.create_table(
        "team_elo_updates",
        sa.Column("fixture_id", sa.Integer(), primary_key=True),
        sa.Column("fixture_date", sa.DateTime(), nullable=False),
        sa.Column("home_team_id", sa.Integer(), nullable=False),
        sa.Column("away_team_id", sa.Integer(), nullable=False),
        sa.Column("home_elo_before", sa.Float(), nullable=False),
        sa.Column("away_elo_before", sa.Float(), nullable=False),
        sa.Column("home_elo_after", sa.Float(), nullable=False),
        sa.Column("away_elo_after", sa.Float(), nullable=False),
        sa.Column("home_delta", sa.Float(), nullable=False),
        sa.Column("away_delta", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_team_elo_updates_fixture_date", "team_elo_updates", ["fixture_date"])


def downgrade() -> None:
    op.drop_index("ix_team_elo_updates_fixture_date", table_name="team_elo_updates")
    op.drop_table("team_elo_updates")
    op.alter_column("team_elos", "elo", existing_type=sa.Numeric(8, 2), type_=sa.Integer(), nullable=False)
