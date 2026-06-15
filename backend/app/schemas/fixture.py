from typing import Optional
from pydantic import BaseModel
from datetime import datetime


# ---------- 子表 schemas ----------

class FixtureEventSchema(BaseModel):
    id: int
    fixture_id: int
    elapsed: Optional[int] = None
    extra: Optional[int] = None
    type: Optional[str] = None
    detail: Optional[str] = None
    comments: Optional[str] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    assist_id: Optional[int] = None
    assist_name: Optional[str] = None

    class Config:
        from_attributes = True


class FixtureLineupSchema(BaseModel):
    id: int
    fixture_id: int
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    formation: Optional[str] = None
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    player_number: Optional[int] = None
    player_position: Optional[str] = None
    player_grid: Optional[str] = None
    is_substitute: bool = False

    class Config:
        from_attributes = True


class FixtureStatisticSchema(BaseModel):
    id: int
    fixture_id: int
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    stat_type: Optional[str] = None
    stat_value: Optional[str] = None

    class Config:
        from_attributes = True


class FixturePlayerStatSchema(BaseModel):
    id: int
    fixture_id: int
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    player_photo: Optional[str] = None
    games: Optional[dict] = None
    offsides: Optional[int] = None
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


# ---------- 主表 schema ----------

class FixtureSchema(BaseModel):
    id: int
    date: Optional[datetime] = None
    timestamp: Optional[int] = None
    timezone: Optional[str] = None
    referee: Optional[str] = None
    first_period: Optional[int] = None
    second_period: Optional[int] = None
    venue_id: Optional[int] = None
    venue_name: Optional[str] = None
    venue_city: Optional[str] = None
    status_short: Optional[str] = None
    status_long: Optional[str] = None
    status_elapsed: Optional[int] = None
    status_extra: Optional[int] = None
    league_id: Optional[int] = None
    league_name: Optional[str] = None
    season: Optional[int] = None
    round: Optional[str] = None
    home_id: Optional[int] = None
    home_name: Optional[str] = None
    home_logo: Optional[str] = None
    home_winner: Optional[bool] = None
    away_id: Optional[int] = None
    away_name: Optional[str] = None
    away_logo: Optional[str] = None
    away_winner: Optional[bool] = None
    goals_home: Optional[int] = None
    goals_away: Optional[int] = None
    halftime_home: Optional[int] = None
    halftime_away: Optional[int] = None
    fulltime_home: Optional[int] = None
    fulltime_away: Optional[int] = None
    extratime_home: Optional[int] = None
    extratime_away: Optional[int] = None
    penalty_home: Optional[int] = None
    penalty_away: Optional[int] = None
    sub_data_synced: bool = False
    category: Optional[str] = None

    class Config:
        from_attributes = True


class FixtureDetailSchema(FixtureSchema):
    events: list[FixtureEventSchema] = []
    lineups: list[FixtureLineupSchema] = []
    statistics: list[FixtureStatisticSchema] = []
    player_stats: list[FixturePlayerStatSchema] = []

    class Config:
        from_attributes = True
