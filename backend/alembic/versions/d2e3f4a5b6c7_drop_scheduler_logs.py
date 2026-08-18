"""drop_scheduler_logs

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-18 09:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # scheduler_logs 表已不再使用（任务执行改为仅记录到日志文件/SSE 流），删除之
    op.drop_table('scheduler_logs')


def downgrade() -> None:
    op.create_table(
        'scheduler_logs',
        op.Column('id', op.Integer(), nullable=False),
        op.Column('task_id', op.String(length=64), nullable=True),
        op.Column('task_name', op.String(length=128), nullable=True),
        op.Column('status', op.String(length=16), nullable=True),
        op.Column('message', op.Text(), nullable=True),
        op.Column('started_at', op.DateTime(), nullable=True),
        op.Column('finished_at', op.DateTime(), nullable=True),
        op.PrimaryKeyConstraint('id'),
    )
