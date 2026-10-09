"""add_team_elos

Revision ID: a1b2c3d4e5f6
Revises: c2d3e4f5a6b7
Create Date: 2026-10-09 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('team_elos',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False, comment='球队 ID (关联 teams.id)'),
        sa.Column('name', sa.String(length=255, collation='utf8mb4_0900_ai_ci'), nullable=False, comment='球队名称 (与 teams.name 一致)'),
        sa.Column('elo', sa.Integer(), nullable=False, comment='ClubElo 评分'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['id'], ['teams.id'], name='fk_team_elos_id_teams')
    )
    op.create_index('ix_team_elos_elo', 'team_elos', ['elo'])


def downgrade() -> None:
    op.drop_index('ix_team_elos_elo', table_name='team_elos')
    op.drop_table('team_elos')
