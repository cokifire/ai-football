"""add_api_status

Revision ID: a2b3c4d5e6f7
Revises: b9e1f4a6c7d8
Create Date: 2026-10-21 12:00:00.000000

新增 ``api_status`` 表 — 用数据库持久化替代原先进程内的 ``_daily_quota_exhausted``
内存闩锁，使调度进程、手动工具、批量预测脚本等多个进程看到同一份额度耗尽状态。

字段说明
--------
* ``provider``        主键，例如 ``api-football``。后续要扩到 ``football-data`` 等
                       其他数据源时无需改表结构。
* ``quota_exhausted`` 当日额度是否已耗尽。每条 API 请求前要看这个字段。
* ``exhausted_date``  标记耗尽时对应的 **UTC 自然日**（API-Football 实际按这个
                       节奏重置），用于"到了 UTC 第二天自动复位"。跨 UTC 日后
                       再次检查时若日期不一致就视作新的一天、置 0。
* ``exhausted_at``    标记耗尽的本地时间戳。
* ``last_check_at``   最近一次被读取时间。便于排查"为什么这次没拦住"。
* ``last_error``      最近一次标记耗尽时的响应摘要，去 key、不含敏感信息。
* ``created_at``/``updated_at`` 通用时间戳。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'b9e1f4a6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'api_status',
        sa.Column('provider', sa.String(length=64), nullable=False, comment='数据源标识 (主键)'),
        sa.Column('quota_exhausted', sa.Boolean(), nullable=False, server_default=sa.text('0'), comment='当日额度是否已耗尽'),
        sa.Column('exhausted_date', sa.Date(), nullable=True, comment='标记耗尽时的 UTC 自然日 (API-Football 重置节奏)，用于次日自动复位'),
        sa.Column('exhausted_at', sa.DateTime(), nullable=True, comment='标记耗尽的本地时间'),
        sa.Column('last_check_at', sa.DateTime(), nullable=True, comment='最近一次读取时间'),
        sa.Column('last_error', sa.Text(), nullable=True, comment='最近一次标记耗尽的响应摘要'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
        sa.PrimaryKeyConstraint('provider'),
        mysql_engine='InnoDB',
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_0900_ai_ci',
    )


def downgrade() -> None:
    op.drop_table('api_status')
