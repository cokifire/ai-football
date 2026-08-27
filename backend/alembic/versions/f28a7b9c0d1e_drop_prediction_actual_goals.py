"""drop duplicated actual score columns from predictions

Revision ID: f28a7b9c0d1e
Revises: d2e3f4a5b6c7
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f28a7b9c0d1e'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_predictions_verify_pending', table_name='predictions')
    op.drop_column('predictions', 'actual_home_goals')
    op.drop_column('predictions', 'actual_away_goals')


def downgrade() -> None:
    raise RuntimeError(
        'Cannot restore prediction actual scores: the canonical source is fixtures.fulltime_*.'
    )
