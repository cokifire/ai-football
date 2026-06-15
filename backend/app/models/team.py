from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base


class Venue(Base):
    __tablename__ = "venues"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="场馆 ID (来自 API)")
    name = Column(String(255), nullable=False, comment="场馆名称")
    name_zh = Column(String(255), nullable=True, comment="中文名")
    address = Column(String(500), nullable=True, comment="地址")
    city = Column(String(100), nullable=True, comment="城市")
    city_zh = Column(String(100), nullable=True, comment="城市中文名")
    country = Column(String(100), nullable=True, comment="国家")
    capacity = Column(Integer, nullable=True, comment="容量")
    surface = Column(String(50), nullable=True, comment="场地类型")
    image = Column(String(500), nullable=True, comment="图片 URL")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    teams = relationship("Team", back_populates="venue")


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, autoincrement=False, comment="球队 ID (来自 API)")
    name = Column(String(255), nullable=False, comment="球队名称")
    name_zh = Column(String(255), nullable=True, comment="中文名")
    code = Column(String(10), nullable=True, comment="球队代码")
    country = Column(String(100), nullable=True, comment="所在国家")
    country_zh = Column(String(100), nullable=True, comment="国家中文名")
    founded = Column(Integer, nullable=True, comment="成立年份")
    national = Column(Boolean, default=False, comment="是否国家队")
    logo = Column(String(500), nullable=True, comment="徽标 URL")
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=True, comment="主场馆 ID")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    venue = relationship("Venue", back_populates="teams")
