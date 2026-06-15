from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime

from app.db.base import Base


class Standing(Base):
    __tablename__ = "standings"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    league_id = Column(Integer, nullable=False, comment="联赛 ID")
    season = Column(Integer, nullable=False, comment="赛季年份")
    group_name = Column(String(100), nullable=True, comment="分组名称 (如小组赛/开幕/闭幕)")
    rank = Column(Integer, nullable=False, comment="排名")
    team_id = Column(Integer, nullable=False, comment="球队 ID")
    team_name = Column(String(255), nullable=False, comment="球队名称")
    team_logo = Column(String(500), nullable=True, comment="球队徽标")
    points = Column(Integer, nullable=True, comment="积分")
    goals_diff = Column(Integer, nullable=True, comment="净胜球")
    form = Column(String(20), nullable=True, comment="近况 (如 WWDLW)")
    status = Column(String(20), nullable=True, comment="状态 (same/up/down)")
    description = Column(String(200), nullable=True, comment="说明 (如晋级/降级)")

    # all
    all_played = Column(Integer, nullable=True)
    all_win = Column(Integer, nullable=True)
    all_draw = Column(Integer, nullable=True)
    all_lose = Column(Integer, nullable=True)
    all_goals_for = Column(Integer, nullable=True)
    all_goals_against = Column(Integer, nullable=True)

    # home
    home_played = Column(Integer, nullable=True)
    home_win = Column(Integer, nullable=True)
    home_draw = Column(Integer, nullable=True)
    home_lose = Column(Integer, nullable=True)
    home_goals_for = Column(Integer, nullable=True)
    home_goals_against = Column(Integer, nullable=True)

    # away
    away_played = Column(Integer, nullable=True)
    away_win = Column(Integer, nullable=True)
    away_draw = Column(Integer, nullable=True)
    away_lose = Column(Integer, nullable=True)
    away_goals_for = Column(Integer, nullable=True)
    away_goals_against = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
