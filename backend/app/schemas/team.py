from typing import Optional
from pydantic import BaseModel


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
    code: Optional[str] = None
    country: Optional[str] = None
    founded: Optional[int] = None
    national: bool = False
    logo: Optional[str] = None
    venue_id: Optional[int] = None

    class Config:
        from_attributes = True


class TeamDetailSchema(TeamSchema):
    venue: Optional[VenueSchema] = None

    class Config:
        from_attributes = True
