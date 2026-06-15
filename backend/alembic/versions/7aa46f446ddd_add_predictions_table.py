"""add_predictions_table

Revision ID: 7aa46f446ddd
Revises: 4856638256ec
Create Date: 2026-05-31 19:44:42.076482

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7aa46f446ddd'
down_revision: Union[str, None] = '4856638256ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'predictions',
        sa.Column('fixture_id', sa.Integer(), primary_key=True),
        sa.Column('home_name', sa.String(255)),
        sa.Column('away_name', sa.String(255)),
        sa.Column('home_logo', sa.String(500)),
        sa.Column('away_logo', sa.String(500)),
        sa.Column('league_name', sa.String(255)),
        sa.Column('match_date', sa.DateTime()),
        # XGBoost 输出
        sa.Column('model_group', sa.String(50)),
        sa.Column('win_home', sa.Float()),
        sa.Column('win_draw', sa.Float()),
        sa.Column('win_away', sa.Float()),
        sa.Column('over25_prob', sa.Float()),
        sa.Column('goals_0_1', sa.Float()),
        sa.Column('goals_2_3', sa.Float()),
        sa.Column('goals_4_5', sa.Float()),
        sa.Column('goals_6p', sa.Float()),
        sa.Column('top3_scores', sa.JSON()),
        sa.Column('lambda_home', sa.Float()),
        sa.Column('lambda_away', sa.Float()),
        # LLM 输出
        sa.Column('llm_win', sa.String(10)),
        sa.Column('llm_over25', sa.String(10)),
        sa.Column('llm_goals_range', sa.String(10)),
        sa.Column('llm_confidence', sa.Float()),
        sa.Column('llm_reason', sa.Text()),
        # RAG
        sa.Column('similar_fixtures', sa.JSON()),
        # 赛后验证
        sa.Column('actual_home_goals', sa.Integer()),
        sa.Column('actual_away_goals', sa.Integer()),
        sa.Column('win_correct', sa.SmallInteger()),
        sa.Column('over25_correct', sa.SmallInteger()),
        sa.Column('goals_range_correct', sa.SmallInteger()),
        sa.Column('score_in_top3', sa.SmallInteger()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(),
                  onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('predictions')
