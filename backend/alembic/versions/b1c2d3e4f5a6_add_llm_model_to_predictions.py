"""add llm_model to predictions

一条预测最终由哪个 LLM 算出，此前只出现在日志里，库里无从回溯；主模型失败
切到备用模型后更是完全看不出来。这里把实际出结果的模型名落库。

值取自 ``prediction_llm_model``，主模型失败且由备用模型出结果时取
``prediction_fallback_llm_model``——记的是真正产出结论的那个，不是配置项。
只记分析链路上出结果的 LLM；情报整理用的 LLM 不在此列。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'predictions',
        sa.Column('llm_model', sa.String(length=64), nullable=True,
                  comment='生成本次预测的 LLM 模型名'),
    )


def downgrade() -> None:
    op.drop_column('predictions', 'llm_model')
