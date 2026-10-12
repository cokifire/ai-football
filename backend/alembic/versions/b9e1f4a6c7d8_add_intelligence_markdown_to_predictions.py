"""add intelligence_markdown to predictions

外部比赛情报原本只随 POST /predict 的响应实时返回，不落库。一旦请求在网关/代理
层超时，前端会走「按 fixture 查库恢复」的分支重建弹窗，情报内容就丢了；预测中心
的详情弹窗同样读不到。这里把情报落库，供两处展示。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9e1f4a6c7d8'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'predictions',
        sa.Column('intelligence_markdown', sa.Text(), nullable=True,
                  comment='外部比赛情报 Markdown'),
    )


def downgrade() -> None:
    op.drop_column('predictions', 'intelligence_markdown')
