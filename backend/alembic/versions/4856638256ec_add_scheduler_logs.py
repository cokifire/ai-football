"""add_scheduler_logs

Revision ID: 4856638256ec
Revises: f1173d35c3c1
Create Date: 2026-05-31 00:26:19.190748

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '4856638256ec'
down_revision: Union[str, None] = 'f1173d35c3c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('scheduler_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(64), nullable=False),
        sa.Column('task_name', sa.String(100), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='running'),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('scheduler_logs')
