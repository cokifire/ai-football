from typing import Optional
from pydantic import BaseModel
from datetime import date


class PlayerStatsSchema(BaseModel):
    id: int
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    league_id: Optional[int] = None
    league_name: Optional[str] = None
    season: int
    games: Optional[dict] = None
    substitutes: Optional[dict] = None
    shots: Optional[dict] = None
    goals: Optional[dict] = None
    passes: Optional[dict] = None
    tackles: Optional[dict] = None
    duels: Optional[dict] = None
    dribbles: Optional[dict] = None
    fouls: Optional[dict] = None
    cards: Optional[dict] = None
    penalty: Optional[dict] = None

    class Config:
        from_attributes = True


class PlayerSchema(BaseModel):
    id: int
    name: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    age: Optional[int] = None
    nationality: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    injured: bool = False
    photo: Optional[str] = None
    birth_date: Optional[date] = None
    birth_place: Optional[str] = None
    birth_country: Optional[str] = None

    class Config:
        from_attributes = True


class PlayerDetailSchema(PlayerSchema):
    statistics: list[PlayerStatsSchema] = []

    class Config:
        from_attributes = True
