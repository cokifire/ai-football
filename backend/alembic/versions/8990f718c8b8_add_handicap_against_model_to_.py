"""add_handicap_against_model_to_predictions

Revision ID: 8990f718c8b8
Revises: ce0fa08af165
Create Date: 2026-06-02 09:48:09.594239

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8990f718c8b8'
down_revision: Union[str, None] = 'ce0fa08af165'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('handicap', sa.String(20), nullable=True))
    op.add_column('predictions', sa.Column('llm_against_model', sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('predictions', 'handicap')
    op.drop_column('predictions', 'llm_against_model')
