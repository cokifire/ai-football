from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class VenueSchema(BaseModel):
    id: int
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    capacity: Optional[int] = None
    surface: Optional[str] = None
    image: Optional[str] = None

    class Config:
        from_attributes = True


class TeamSchema(BaseModel):
    id: int
    name: str
    name_zh: Optional[str] = None
    code: Optional[str] = None
    country: Optional[str] = None
    founded: Optional[int] = None
    national: bool = False
    logo: Optional[str] = None
    venue_id: Optional[int] = None

    class Config:
        from_attributes = True


class TeamRecentFixtureSchema(BaseModel):
    id: int
    date: Optional[datetime] = None
    league_name: Optional[str] = None
    home_id: Optional[int] = None
    home_name: Optional[str] = None
    home_logo: Optional[str] = None
    away_id: Optional[int] = None
    away_name: Optional[str] = None
    away_logo: Optional[str] = None
    goals_home: Optional[int] = None
    goals_away: Optional[int] = None
    status_short: Optional[str] = None

    class Config:
        from_attributes = True


class TeamDetailSchema(TeamSchema):
    venue: Optional[VenueSchema] = None
    recent_fixtures: list[TeamRecentFixtureSchema] = []

    class Config:
        from_attributes = True
