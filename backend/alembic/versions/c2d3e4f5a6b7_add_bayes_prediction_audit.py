"""add auditable Bayesian prediction fields

Revision ID: c2d3e4f5a6b7
Revises: b7c8d9e0f1a2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("bayes_version", sa.String(32), nullable=True))
    op.add_column("predictions", sa.Column("bayes_p0", sa.JSON(), nullable=True))
    op.add_column("predictions", sa.Column("bayes_p1", sa.JSON(), nullable=True))
    op.add_column("predictions", sa.Column("bayes_updates", sa.JSON(), nullable=True))
    op.add_column("predictions", sa.Column("bayes_evidence", sa.JSON(), nullable=True))
    op.add_column("predictions", sa.Column("bayes_risk", sa.JSON(), nullable=True))


def downgrade() -> None:
    for name in ("bayes_risk", "bayes_evidence", "bayes_updates", "bayes_p1", "bayes_p0", "bayes_version"):
        op.drop_column("predictions", name)
