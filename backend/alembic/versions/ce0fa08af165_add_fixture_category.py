"""add_fixture_category

Revision ID: ce0fa08af165
Revises: 7aa46f446ddd
Create Date: 2026-06-01 09:47:39.956911

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce0fa08af165'
down_revision: Union[str, None] = '7aa46f446ddd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('fixtures', sa.Column('category', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('fixtures', 'category')
