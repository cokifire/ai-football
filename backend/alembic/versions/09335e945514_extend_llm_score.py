"""extend_llm_score

Revision ID: 09335e945514
Revises: 16f391a4464c
Create Date: 2026-06-03 09:23:49.386172

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '09335e945514'
down_revision: Union[str, None] = '16f391a4464c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('predictions', 'llm_score', type_=sa.String(50), existing_type=sa.String(20), nullable=True)


def downgrade() -> None:
    op.alter_column('predictions', 'llm_score', type_=sa.String(20), existing_type=sa.String(50), nullable=True)
