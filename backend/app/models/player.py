from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="球员 ID (来自 API)")
    name = Column(String(255), nullable=False, comment="全名")
    name_zh = Column(String(255), nullable=True, comment="中文名")
    firstname = Column(String(100), nullable=True, comment="名")
    lastname = Column(String(100), nullable=True, comment="姓")
    age = Column(Integer, nullable=True, comment="年龄")
    nationality = Column(String(100), nullable=True, comment="国籍")
    height = Column(String(20), nullable=True, comment="身高")
    weight = Column(String(20), nullable=True, comment="体重")
    injured = Column(Boolean, default=False, comment="是否受伤")
    photo = Column(String(500), nullable=True, comment="头像 URL")
    birth_date = Column(Date, nullable=True, comment="出生日期")
    birth_place = Column(String(200), nullable=True, comment="出生地")
    birth_country = Column(String(100), nullable=True, comment="出生国家")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    statistics = relationship("PlayerStats", back_populates="player", cascade="all, delete-orphan")


class PlayerStats(Base):
    __tablename__ = "player_stats"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False, comment="球员 ID")
    team_id = Column(Integer, nullable=True, comment="球队 ID")
    league_id = Column(Integer, nullable=True, comment="联赛 ID")
    season = Column(Integer, nullable=False, comment="赛季年份")

    games = Column(JSON, nullable=True, comment="出场/首发/分钟/位置/评分/队长")
    substitutes = Column(JSON, nullable=True, comment="替补上场/被换下/替补席")
    shots = Column(JSON, nullable=True, comment="射门统计")
    goals = Column(JSON, nullable=True, comment="进球/失球/助攻/扑救")
    passes = Column(JSON, nullable=True, comment="传球/关键传球/准确数")
    tackles = Column(JSON, nullable=True, comment="抢断/封堵/拦截")
    duels = Column(JSON, nullable=True, comment="对抗统计")
    dribbles = Column(JSON, nullable=True, comment="过人统计")
    fouls = Column(JSON, nullable=True, comment="犯规/被犯规")
    cards = Column(JSON, nullable=True, comment="黄牌/红牌")
    penalty = Column(JSON, nullable=True, comment="点球统计")

    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    player = relationship("Player", back_populates="statistics")
    team = relationship("Team", primaryjoin="PlayerStats.team_id == Team.id", foreign_keys=[team_id], viewonly=True)
    league = relationship("League", primaryjoin="PlayerStats.league_id == League.id", foreign_keys=[league_id], viewonly=True)

    @property
    def team_name(self):
        return self.team.name if self.team else None

    @property
    def league_name(self):
        return self.league.name if self.league else None
