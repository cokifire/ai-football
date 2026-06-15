from typing import Generic, TypeVar, Optional
from pydantic import BaseModel, field_validator
from datetime import date, datetime


class CoverageSchema(BaseModel):
    fixtures_events: bool = False
    fixtures_lineups: bool = False
    fixtures_statistics_fixtures: bool = False
    fixtures_statistics_players: bool = False
    standings: bool = False
    players: bool = False
    top_scorers: bool = False
    top_assists: bool = False
    top_cards: bool = False
    injuries: bool = False
    predictions: bool = False
    odds: bool = False


class SeasonSchema(BaseModel):
    year: int
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    is_current: bool = False
    coverage: Optional[dict] = None

    class Config:
        from_attributes = True


class LeagueSchema(BaseModel):
    id: int
    name: str
    type: Optional[str] = None
    logo: Optional[str] = None
    country_name: Optional[str] = None
    country_code: Optional[str] = None
    country_flag: Optional[str] = None
    enabled: Optional[bool] = False

    @field_validator("enabled", mode="before")
    @classmethod
    def coerce_enabled(cls, v):
        """数据库可能为 NULL，兼容处理"""
        if v is None:
            return False
        return v

    class Config:
        from_attributes = True


class LeagueDetailSchema(LeagueSchema):
    seasons: list[SeasonSchema] = []

    class Config:
        from_attributes = True


T = TypeVar("T")

class PaginatedResponse(BaseModel, Generic[T]):
    data: list[T]
    total: int
    page: int
    page_size: int
