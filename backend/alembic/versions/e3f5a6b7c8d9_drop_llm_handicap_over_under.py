"""drop_llm_handicap_and_over_under

_llm_handicap / _llm_over_under 为早期自由文本列，已被结构化的
llm_handicap_num/_team/_pct 与 llm_ou_line/_type/_pct 取代。
LLM 输出 schema 不再返回 handicap / over_under 键，故这两列始终为 NULL，
无任何脚本依赖，予以删除。

Revision ID: e3f5a6b7c8d9
Revises: f28a7b9c0d1e
Create Date: 2026-08-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f5a6b7c8d9'
down_revision: Union[str, None] = 'f28a7b9c0d1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('predictions', 'llm_handicap')
    op.drop_column('predictions', 'llm_over_under')


def downgrade() -> None:
    op.add_column('predictions', sa.Column('llm_handicap', sa.String(50), nullable=True))
    op.add_column('predictions', sa.Column('llm_over_under', sa.String(50), nullable=True))
