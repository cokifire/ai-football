"""add_weekday_to_scheduler_tasks

Revision ID: c1d2e3f4a5b6
Revises: b6d2a8c9f001
Create Date: 2026-08-18 08:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b6d2a8c9f001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # weekday: 0=周一 ... 6=周日；NULL/缺省表示每天执行
    op.add_column(
        'scheduler_tasks',
        sa.Column('weekday', sa.SmallInteger(), nullable=True,
                  comment='每周执行日 0=周一..6=周日，NULL=每天'),
    )


def downgrade() -> None:
    op.drop_column('scheduler_tasks', 'weekday')
