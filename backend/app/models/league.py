from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, JSON, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base


class League(Base):
    __tablename__ = "leagues"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="联赛 ID (来自 API)")
    name = Column(String(255), nullable=False, comment="联赛名称")
    name_zh = Column(String(255), nullable=True, comment="中文名")
    type = Column(String(20), nullable=True, comment="类型: League / Cup")
    logo = Column(String(500), nullable=True, comment="联赛徽标 URL")
    country_name = Column(String(100), nullable=True, comment="国家名称")
    country_name_zh = Column(String(100), nullable=True, comment="国家中文名")
    country_code = Column(String(10), nullable=True, comment="国家代码")
    country_flag = Column(String(500), nullable=True, comment="国旗 URL")
    enabled = Column(Boolean, default=False, comment="是否启用（白名单）")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    seasons = relationship("Season", back_populates="league", cascade="all, delete-orphan")


class Season(Base):
    __tablename__ = "seasons"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增 ID")
    league_id = Column(Integer, ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False, comment="联赛 ID")
    year = Column(Integer, nullable=False, comment="赛季年份")
    start_date = Column(Date, nullable=True, comment="赛季开始日期")
    end_date = Column(Date, nullable=True, comment="赛季结束日期")
    is_current = Column(Boolean, default=False, comment="是否当前赛季")
    coverage = Column(JSON, nullable=True, comment="数据覆盖范围")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    league = relationship("League", back_populates="seasons")
