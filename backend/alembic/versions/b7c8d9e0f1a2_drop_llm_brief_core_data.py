"""drop_llm_brief_core_data

删除 predictions 表的 llm_brief / llm_core_data 两列。
LLM prompt 已不再要求输出 brief_analysis / core_data（与 deep_report 内容重复），
写入、读取、API 与前端展示均已同步移除，列本身不再有数据源。

Revision ID: b7c8d9e0f1a2
Revises: f4a6b7c8d9e0
Create Date: 2026-09-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'f4a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('predictions', 'llm_core_data')
    op.drop_column('predictions', 'llm_brief')


def downgrade() -> None:
    op.add_column('predictions', sa.Column('llm_brief', sa.String(200), nullable=True))
    op.add_column('predictions', sa.Column('llm_core_data', sa.Text(), nullable=True))
