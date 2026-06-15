"""add_scheduler_tasks

Revision ID: f1173d35c3c1
Revises: ba14b44197e8
Create Date: 2026-05-30 23:56:24.501113

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'f1173d35c3c1'
down_revision: Union[str, None] = 'ba14b44197e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('scheduler_tasks',
    sa.Column('id', sa.String(64), nullable=False),
    sa.Column('name', sa.String(100), nullable=False),
    sa.Column('interval_seconds', sa.Integer(), nullable=False),
    sa.Column('is_enabled', sa.Integer(), nullable=False, server_default=sa.text('1')),
    sa.Column('last_run', sa.DateTime(), nullable=True),
    sa.Column('next_run', sa.DateTime(), nullable=True),
    sa.Column('is_running', sa.Integer(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('scheduler_tasks')
