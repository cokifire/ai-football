"""add_llm_score_drop_deprecated

Revision ID: 16f391a4464c
Revises: 662b02356fed
Create Date: 2026-06-03 09:13:10.674210

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16f391a4464c'
down_revision: Union[str, None] = '662b02356fed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('llm_score', sa.String(20), nullable=True))
    op.drop_column('predictions', 'goals_0_1')
    op.drop_column('predictions', 'goals_2_3')
    op.drop_column('predictions', 'goals_4_5')
    op.drop_column('predictions', 'goals_6p')
    op.drop_column('predictions', 'llm_over25')
    op.drop_column('predictions', 'llm_goals_range')
    op.drop_column('predictions', 'llm_confidence')
    op.drop_column('predictions', 'llm_reason')
    op.drop_column('predictions', 'similar_fixtures')
    op.drop_column('predictions', 'llm_against_model')


def downgrade() -> None:
    op.drop_column('predictions', 'llm_score')
    op.add_column('predictions', sa.Column('goals_0_1', sa.Float(), nullable=True))
    op.add_column('predictions', sa.Column('goals_2_3', sa.Float(), nullable=True))
    op.add_column('predictions', sa.Column('goals_4_5', sa.Float(), nullable=True))
    op.add_column('predictions', sa.Column('goals_6p', sa.Float(), nullable=True))
    op.add_column('predictions', sa.Column('llm_over25', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_goals_range', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_confidence', sa.Float(), nullable=True))
    op.add_column('predictions', sa.Column('llm_reason', sa.Text(), nullable=True))
    op.add_column('predictions', sa.Column('similar_fixtures', sa.JSON(), nullable=True))
    op.add_column('predictions', sa.Column('llm_against_model', sa.SmallInteger(), nullable=True))
