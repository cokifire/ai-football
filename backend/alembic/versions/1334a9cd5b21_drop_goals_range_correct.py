"""drop_goals_range_correct

Revision ID: 1334a9cd5b21
Revises: 09335e945514
Create Date: 2026-06-03 09:50:05.679499

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1334a9cd5b21'
down_revision: Union[str, None] = '09335e945514'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('predictions', 'goals_range_correct')


def downgrade() -> None:
    op.add_column('predictions', sa.Column('goals_range_correct', sa.SmallInteger(), nullable=True))
