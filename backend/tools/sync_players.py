"""只同步联赛 848 的球员"""
from app.db.session import SessionLocal
from app.services.player_service import sync_players_by_league

db = SessionLocal()
try:
    sync_players_by_league(db, 848)
finally:
    db.close()
