"""drop_verify_state_columns

_drop predictions 表的 verify_attempts / verify_skipped / verify_note 三列。
这些列原用于赛后回填的轮询保护，现改为直接依赖 fixtures.status_short
（UNPLAYABLE 集合）在扫描 SQL 中过滤已放弃的比赛，无需单列维护状态。

Revision ID: f4a6b7c8d9e0
Revises: e3f5a6b7c8d9
Create Date: 2026-08-28 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a6b7c8d9e0'
down_revision: Union[str, None] = 'e3f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('predictions', 'verify_attempts')
    op.drop_column('predictions', 'verify_skipped')
    op.drop_column('predictions', 'verify_note')


def downgrade() -> None:
    op.add_column('predictions', sa.Column('verify_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('predictions', sa.Column('verify_skipped', sa.SmallInteger(), nullable=False, server_default='0'))
    op.add_column('predictions', sa.Column('verify_note', sa.String(255), nullable=True))
