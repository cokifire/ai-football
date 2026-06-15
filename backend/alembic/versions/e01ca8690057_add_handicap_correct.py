"""add_handicap_correct

Revision ID: e01ca8690057
Revises: 1334a9cd5b21
Create Date: 2026-06-03 09:59:07.938930

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e01ca8690057'
down_revision: Union[str, None] = '1334a9cd5b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('handicap_correct', sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('predictions', 'handicap_correct')
