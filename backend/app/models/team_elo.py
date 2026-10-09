from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base


class TeamElo(Base):
    """ClubElo 评分表：id/名称严格对齐 teams 表，elo 为 ClubElo 评分。"""

    __tablename__ = "team_elos"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="球队 ID (关联 teams.id)")
    name = Column(String(255), nullable=False, comment="球队名称 (与 teams.name 一致)")
    elo = Column(Integer, nullable=False, comment="ClubElo 评分")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    team = relationship("Team", foreign_keys="TeamElo.id", primaryjoin="TeamElo.id == Team.id")
