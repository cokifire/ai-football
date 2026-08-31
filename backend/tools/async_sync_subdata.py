"""异步版子数据同步 - 高并发无死锁"""
import asyncio
import asyncmy
import httpx
from datetime import datetime
from loguru import logger

from app.core.config import settings
from app.core.log_config import setup_logger

setup_logger()

CONCURRENT = 10
BATCH_SIZE = 200

db_config = {
    "host": settings.db_host,
    "port": settings.db_port,
    "user": settings.db_user,
    "password": settings.db_password,
    "database": settings.db_name,
    "charset": "utf8mb4",
}

API_BASE = settings.api_football_base_url
API_KEY = settings.api_football_key


async def fetch_json(client: httpx.AsyncClient, endpoint: str, fixture_id: int):
    r = await client.get(
        f"/{endpoint}",
        headers={"x-apisports-key": API_KEY},
        params={"fixture": fixture_id},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


async def sync_one_fixture(client: httpx.AsyncClient, pool: asyncmy.Pool, fixture_id: int, idx: int, total: int):
    logger.debug(f"[{idx}/{total}] fixture={fixture_id} 开始拉取")

    # 并行拉取启用的 2 个端点；events/players 接口已禁用。
    results = await asyncio.gather(
        fetch_json(client, "fixtures/lineups", fixture_id),
        fetch_json(client, "fixtures/statistics", fixture_id),
        return_exceptions=True,
    )

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # lineups
            data = results[0]
            if isinstance(data, Exception):
                logger.warning(f"lineups 拉取失败 fixture={fixture_id}: {data}")
            else:
                await cur.execute("SELECT 1 FROM fixture_lineups WHERE fixture_id = %s LIMIT 1", (fixture_id,))
                if not await cur.fetchone():
                    rows = []
                    for team in data.get("response", []):
                        t = team.get("team") or {}
                        fm = team.get("formation", "")
                        tid, tname = t.get("id"), t.get("name")
                        for xi in team.get("startXI", []):
                            p = xi.get("player") or {}
                            rows.append((fixture_id, tid, tname, fm, p.get("id"), p.get("name"),
                                         p.get("number"), p.get("pos"), p.get("grid"), False))
                        for sub in team.get("substitutes", []):
                            p = sub.get("player") or {}
                            rows.append((fixture_id, tid, tname, fm, p.get("id"), p.get("name"),
                                         p.get("number"), p.get("pos"), p.get("grid"), True))
                    if rows:
                        sql = "INSERT INTO fixture_lineups (fixture_id, team_id, team_name, formation, player_id, player_name, player_number, player_position, player_grid, is_substitute) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                        await cur.executemany(sql, rows)

            # statistics
            data = results[1]
            if isinstance(data, Exception):
                logger.warning(f"statistics 拉取失败 fixture={fixture_id}: {data}")
            else:
                await cur.execute("SELECT 1 FROM fixture_statistics WHERE fixture_id = %s LIMIT 1", (fixture_id,))
                if not await cur.fetchone():
                    rows = []
                    for team in data.get("response", []):
                        t = team.get("team") or {}
                        tid, tname = t.get("id"), t.get("name")
                        for s in team.get("statistics", []):
                            val = s.get("value")
                            rows.append((fixture_id, tid, tname, s.get("type"),
                                         str(val) if val is not None else None))
                    if rows:
                        sql = "INSERT INTO fixture_statistics (fixture_id, team_id, team_name, stat_type, stat_value) VALUES (%s,%s,%s,%s,%s)"
                        await cur.executemany(sql, rows)

            await cur.execute("UPDATE fixtures SET sub_data_synced = 1 WHERE id = %s", (fixture_id,))
        await conn.commit()

    return True


async def main():
    pool = await asyncmy.create_pool(**db_config, minsize=CONCURRENT + 3, maxsize=CONCURRENT + 8)
    sem = asyncio.Semaphore(CONCURRENT)

    async def worker(fid: int, idx: int, total: int):
        async with sem:
            async with httpx.AsyncClient(base_url=API_BASE.rstrip("/")) as client:
                for attempt in range(3):
                    try:
                        return await sync_one_fixture(client, pool, fid, idx, total)
                    except Exception as e:
                        if attempt == 2:
                            logger.error(f"fixture={fid} 失败(重试{attempt+1}次): {e}")
                            return False
                        await asyncio.sleep(1 * (attempt + 1))
                return False

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM fixtures WHERE status_short IN ('FT','AET','PEN','AWD','WO') AND sub_data_synced = 0 ORDER BY id"
            )
            ids = [row[0] for row in await cur.fetchall()]

    total = len(ids)
    logger.info(f"待处理: {total} 场, 并发={CONCURRENT}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 待处理: {total} 场, 并发={CONCURRENT}")
    started = datetime.now()
    ok = fail = 0

    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i : i + BATCH_SIZE]
        tasks = [worker(fid, i + j + 1, total) for j, fid in enumerate(chunk)]
        results = await asyncio.gather(*tasks)
        ok += sum(1 for r in results if r)
        fail += sum(1 for r in results if not r)
        elapsed = (datetime.now() - started).total_seconds() / 60
        done = ok + fail
        pct = done / total * 100 if total else 0
        etc = elapsed / done * (total - done) if done else 0
        msg = f"[{datetime.now().strftime('%H:%M:%S')}] {done}/{total} ({pct:.1f}%) OK={ok} FAIL={fail} 已用={elapsed:.1f}分钟 剩余≈{etc:.0f}分钟"
        logger.info(msg)
        print(msg)

    pool.close()
    await pool.wait_closed()
    elapsed = (datetime.now() - started).total_seconds() / 60
    final = f"完成! OK={ok} FAIL={fail} 总耗时={elapsed:.1f}分钟"
    logger.info(final)
    print(final)


if __name__ == "__main__":
    asyncio.run(main())
