"""create_users_table

Revision ID: 112dda7fa9e6
Revises: e01ca8690057
Create Date: 2026-06-03 20:49:30.795726

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '112dda7fa9e6'
down_revision: Union[str, None] = 'e01ca8690057'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('phone', sa.String(20), nullable=False),
        sa.Column('username', sa.String(100), nullable=True),
        sa.Column('role', sa.String(20), server_default='user', nullable=True),
        sa.Column('vip_expires_at', sa.DateTime(), nullable=True),
        sa.Column('avatar', sa.String(500), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(),
                  onupdate=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('phone'),
    )


def downgrade() -> None:
    op.drop_table('users')
