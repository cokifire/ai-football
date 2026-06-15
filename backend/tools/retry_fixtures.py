"""重试失败的联赛+赛季"""
from app.db.session import SessionLocal
from app.services.fixture_service import sync_fixtures
import httpx
from app.core.config import settings
from app.models.fixture import Fixture
from app.services.fixture_service import _upsert_fixture

FAILED = [
    (141, 2024),  # 西乙
]

db = SessionLocal()
try:
    for league_id, year in FAILED:
        print(f"拉取联赛 {league_id} 赛季 {year}...", end=" ")
        try:
            response = httpx.get(
                f"{settings.api_football_base_url}/fixtures",
                headers={"x-apisports-key": settings.api_football_key},
                params={"league": str(league_id), "season": str(year)},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("response", [])
            for row in items:
                _upsert_fixture(db, row)
            db.commit()
            print(f"{len(items)} 场 OK")
        except Exception as e:
            print(f"FAILED: {e}")
finally:
    db.close()
