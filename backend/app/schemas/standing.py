from typing import Optional
from pydantic import BaseModel


class StandingSchema(BaseModel):
    id: int
    league_id: int
    season: int
    group_name: Optional[str] = None
    rank: int
    team_id: int
    team_name: str
    team_logo: Optional[str] = None
    points: Optional[int] = None
    goals_diff: Optional[int] = None
    form: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None

    all_played: Optional[int] = None
    all_win: Optional[int] = None
    all_draw: Optional[int] = None
    all_lose: Optional[int] = None
    all_goals_for: Optional[int] = None
    all_goals_against: Optional[int] = None

    home_played: Optional[int] = None
    home_win: Optional[int] = None
    home_draw: Optional[int] = None
    home_lose: Optional[int] = None
    home_goals_for: Optional[int] = None
    home_goals_against: Optional[int] = None

    away_played: Optional[int] = None
    away_win: Optional[int] = None
    away_draw: Optional[int] = None
    away_lose: Optional[int] = None
    away_goals_for: Optional[int] = None
    away_goals_against: Optional[int] = None

    class Config:
        from_attributes = True
