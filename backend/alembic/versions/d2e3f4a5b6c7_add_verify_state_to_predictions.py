"""add_verify_state_to_predictions

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-18 13:00:00.000000

为赛后验证回填增加状态字段，避免无法完成的比赛（取消/腰斩/延期等）
被无限轮询，持续消耗 API-Football 配额。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'predictions',
        sa.Column('verify_attempts', sa.Integer(), nullable=False,
                  server_default='0',
                  comment='赛后验证尝试次数，超过上限后放弃'),
    )
    op.add_column(
        'predictions',
        sa.Column('verify_skipped', sa.SmallInteger(), nullable=False,
                  server_default='0',
                  comment='1=已放弃验证（比赛取消/腰斩/超过重试上限/超出时间窗）'),
    )
    op.add_column(
        'predictions',
        sa.Column('verify_note', sa.String(255), nullable=True,
                  comment='放弃验证的原因说明'),
    )
    # 回填扫描的主要过滤条件，建索引避免全表扫
    op.create_index(
        'ix_predictions_verify_pending',
        'predictions',
        ['verify_skipped', 'actual_home_goals', 'match_date'],
    )


def downgrade() -> None:
    op.drop_index('ix_predictions_verify_pending', table_name='predictions')
    op.drop_column('predictions', 'verify_note')
    op.drop_column('predictions', 'verify_skipped')
    op.drop_column('predictions', 'verify_attempts')
