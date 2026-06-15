"""drop_users_orders

Revision ID: b6d2a8c9f001
Revises: aa8539407817
Create Date: 2026-06-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b6d2a8c9f001"
down_revision: Union[str, None] = "aa8539407817"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orders")
    op.execute("DROP TABLE IF EXISTS users")


def downgrade() -> None:
    # 用户/支付系统已移除，迁移回滚不重建废弃表。
    pass
