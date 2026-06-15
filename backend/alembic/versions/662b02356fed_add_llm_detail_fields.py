"""add_llm_detail_fields

Revision ID: 662b02356fed
Revises: 8990f718c8b8
Create Date: 2026-06-02 21:28:01.678229

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '662b02356fed'
down_revision: Union[str, None] = '8990f718c8b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('llm_brief', sa.String(200), nullable=True))
    op.add_column('predictions', sa.Column('llm_core_data', sa.Text(), nullable=True))
    op.add_column('predictions', sa.Column('llm_deep_report', sa.Text(), nullable=True))
    op.add_column('predictions', sa.Column('llm_handicap', sa.String(50), nullable=True))
    op.add_column('predictions', sa.Column('llm_over_under', sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column('predictions', 'llm_over_under')
    op.drop_column('predictions', 'llm_handicap')
    op.drop_column('predictions', 'llm_deep_report')
    op.drop_column('predictions', 'llm_core_data')
    op.drop_column('predictions', 'llm_brief')
