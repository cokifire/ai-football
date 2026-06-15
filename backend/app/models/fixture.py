from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base


class Fixture(Base):
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="比赛 ID (来自 API)")
    date = Column(DateTime, nullable=True, comment="比赛日期时间 (UTC)")
    timestamp = Column(Integer, nullable=True, comment="比赛时间戳")
    timezone = Column(String(50), nullable=True, comment="时区")
    referee = Column(String(255), nullable=True, comment="裁判")
    first_period = Column(Integer, nullable=True, comment="上半场开始时间戳")
    second_period = Column(Integer, nullable=True, comment="下半场开始时间戳")

    venue_id = Column(Integer, nullable=True, comment="场馆 ID")
    venue_name = Column(String(255), nullable=True, comment="场馆名称")
    venue_city = Column(String(100), nullable=True, comment="场馆城市")

    status_short = Column(String(10), nullable=True, comment="状态短码 (FT/1H/NS...)")
    status_long = Column(String(50), nullable=True, comment="状态长描述")
    status_elapsed = Column(Integer, nullable=True, comment="已进行分钟数")
    status_extra = Column(Integer, nullable=True, comment="补时分钟数")

    league_id = Column(Integer, nullable=True, comment="联赛 ID")
    league_name = Column(String(255), nullable=True, comment="联赛名称")
    season = Column(Integer, nullable=True, comment="赛季年份")
    round = Column(String(100), nullable=True, comment="轮次")

    home_id = Column(Integer, nullable=True, comment="主队 ID")
    home_name = Column(String(255), nullable=True, comment="主队名称")
    home_logo = Column(String(500), nullable=True, comment="主队徽标")
    home_winner = Column(Boolean, nullable=True, comment="主队是否获胜")

    away_id = Column(Integer, nullable=True, comment="客队 ID")
    away_name = Column(String(255), nullable=True, comment="客队名称")
    away_logo = Column(String(500), nullable=True, comment="客队徽标")
    away_winner = Column(Boolean, nullable=True, comment="客队是否获胜")

    goals_home = Column(Integer, nullable=True, comment="主队进球")
    goals_away = Column(Integer, nullable=True, comment="客队进球")

    halftime_home = Column(Integer, nullable=True)
    halftime_away = Column(Integer, nullable=True)
    fulltime_home = Column(Integer, nullable=True)
    fulltime_away = Column(Integer, nullable=True)
    extratime_home = Column(Integer, nullable=True)
    extratime_away = Column(Integer, nullable=True)
    penalty_home = Column(Integer, nullable=True)
    penalty_away = Column(Integer, nullable=True)

    sub_data_synced = Column(Boolean, default=False, comment="子数据(events/lineups/stats/players)是否已同步")
    category = Column(String(20), nullable=True, comment="分类标签，如 jingzu")

    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    events = relationship("FixtureEvent", back_populates="fixture", cascade="all, delete-orphan")
    lineups = relationship("FixtureLineup", back_populates="fixture", cascade="all, delete-orphan")
    statistics = relationship("FixtureStatistic", back_populates="fixture", cascade="all, delete-orphan")
    player_stats = relationship("FixturePlayerStat", back_populates="fixture", cascade="all, delete-orphan")


class FixtureEvent(Base):
    __tablename__ = "fixture_events"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    fixture_id = Column(Integer, ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False, comment="比赛 ID")
    elapsed = Column(Integer, nullable=True, comment="事件发生分钟")
    extra = Column(Integer, nullable=True, comment="补时分钟")
    type = Column(String(20), nullable=True, comment="事件类型: Goal/Card/Subst/Var")
    type_zh = Column(String(20), nullable=True, comment="事件类型中文")
    detail = Column(String(100), nullable=True, comment="事件详情")
    detail_zh = Column(String(100), nullable=True, comment="事件详情中文")
    comments = Column(String(500), nullable=True, comment="备注")
    team_id = Column(Integer, nullable=True, comment="球队 ID")
    team_name = Column(String(255), nullable=True, comment="球队名称")
    player_id = Column(Integer, nullable=True, comment="球员 ID")
    player_name = Column(String(255), nullable=True, comment="球员姓名")
    assist_id = Column(Integer, nullable=True, comment="助攻球员 ID")
    assist_name = Column(String(255), nullable=True, comment="助攻球员姓名")

    created_at = Column(DateTime, default=datetime.now)

    fixture = relationship("Fixture", back_populates="events")


class FixtureLineup(Base):
    __tablename__ = "fixture_lineups"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    fixture_id = Column(Integer, ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False, comment="比赛 ID")
    team_id = Column(Integer, nullable=True, comment="球队 ID")
    team_name = Column(String(255), nullable=True, comment="球队名称")
    formation = Column(String(20), nullable=True, comment="阵型 (如 4-2-3-1)")
    player_id = Column(Integer, nullable=True, comment="球员 ID")
    player_name = Column(String(255), nullable=True, comment="球员姓名")
    player_number = Column(Integer, nullable=True, comment="球衣号码")
    player_position = Column(String(20), nullable=True, comment="位置")
    player_grid = Column(String(20), nullable=True, comment="网格坐标")
    is_substitute = Column(Boolean, default=False, comment="false=首发 true=替补")

    created_at = Column(DateTime, default=datetime.now)

    fixture = relationship("Fixture", back_populates="lineups")


class FixtureStatistic(Base):
    __tablename__ = "fixture_statistics"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    fixture_id = Column(Integer, ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False, comment="比赛 ID")
    team_id = Column(Integer, nullable=True, comment="球队 ID")
    team_name = Column(String(255), nullable=True, comment="球队名称")
    stat_type = Column(String(100), nullable=True, comment="统计类型 (如 Shots on Goal)")
    stat_type_zh = Column(String(100), nullable=True, comment="统计类型中文")
    stat_value = Column(String(50), nullable=True, comment="统计值 (可为数字或百分比字符串)")

    created_at = Column(DateTime, default=datetime.now)

    fixture = relationship("Fixture", back_populates="statistics")


class FixturePlayerStat(Base):
    __tablename__ = "fixture_player_stats"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    fixture_id = Column(Integer, ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False, comment="比赛 ID")
    team_id = Column(Integer, nullable=True, comment="球队 ID")
    team_name = Column(String(255), nullable=True, comment="球队名称")
    player_id = Column(Integer, nullable=True, comment="球员 ID")
    player_name = Column(String(255), nullable=True, comment="球员姓名")
    player_photo = Column(String(500), nullable=True, comment="球员头像")

    games = Column(JSON, nullable=True, comment="出场信息")
    offsides = Column(Integer, nullable=True, comment="越位")
    shots = Column(JSON, nullable=True)
    goals = Column(JSON, nullable=True)
    passes = Column(JSON, nullable=True)
    tackles = Column(JSON, nullable=True)
    duels = Column(JSON, nullable=True)
    dribbles = Column(JSON, nullable=True)
    fouls = Column(JSON, nullable=True)
    cards = Column(JSON, nullable=True)
    penalty = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.now)

    fixture = relationship("Fixture", back_populates="player_stats")
