"""add_llm_detail_splits

Revision ID: 2e1e3ff28115
Revises: 112dda7fa9e6
Create Date: 2026-06-04 21:12:15.548202

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2e1e3ff28115'
down_revision: Union[str, None] = '112dda7fa9e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('llm_win_pct', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_handicap_num', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_handicap_team', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_handicap_pct', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_ou_line', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_ou_type', sa.String(10), nullable=True))
    op.add_column('predictions', sa.Column('llm_ou_pct', sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column('predictions', 'llm_ou_pct')
    op.drop_column('predictions', 'llm_ou_type')
    op.drop_column('predictions', 'llm_ou_line')
    op.drop_column('predictions', 'llm_handicap_pct')
    op.drop_column('predictions', 'llm_handicap_team')
    op.drop_column('predictions', 'llm_handicap_num')
    op.drop_column('predictions', 'llm_win_pct')
