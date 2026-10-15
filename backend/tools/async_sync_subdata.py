"""异步版子数据同步 - 高并发无死锁"""
import asyncio
import asyncmy
import httpx
from datetime import datetime
from loguru import logger

from app.core.config import settings
from app.core.log_config import setup_logger
from app.core.api_football import api_football_get_async, get_async_client

setup_logger()

CONCURRENT = 3
BATCH_SIZE = 200

db_config = {
    "host": settings.db_host,
    "port": settings.db_port,
    "user": settings.db_user,
    "password": settings.db_password,
    "database": settings.db_name,
    "charset": "utf8mb4",
}

async def fetch_json(client: httpx.AsyncClient, endpoint: str, fixture_id: int):
    return await api_football_get_async(client, f"/{endpoint}", {"fixture": fixture_id})


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

            # 任一子接口失败时保留未同步标记，让下一轮可以重试；成功的
            # 另一接口写入仍提交，不会因为单个接口限流而丢失。
            synced = all(not isinstance(data, Exception) for data in results)
            await cur.execute(
                "UPDATE fixtures SET sub_data_synced = %s WHERE id = %s",
                (1 if synced else 0, fixture_id),
            )
        await conn.commit()

    return synced


async def main():
    pool = await asyncmy.create_pool(**db_config, minsize=CONCURRENT + 3, maxsize=CONCURRENT + 8)
    sem = asyncio.Semaphore(CONCURRENT)
    # 所有 worker 共用连接池中的一个 HTTP client；请求节奏由统一 API
    # 客户端的进程级限流器控制，而不是靠并发数猜测 API 配额。
    client = get_async_client()

    async def worker(fid: int, idx: int, total: int):
        async with sem:
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
                """SELECT id FROM fixtures
                   WHERE status_short IN ('FT','AET','PEN','AWD','WO')
                     AND (sub_data_synced = 0
                          OR NOT EXISTS (SELECT 1 FROM fixture_lineups l WHERE l.fixture_id = fixtures.id)
                          OR NOT EXISTS (SELECT 1 FROM fixture_statistics s WHERE s.fixture_id = fixtures.id))
                   ORDER BY id"""
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

    await client.aclose()
    pool.close()
    await pool.wait_closed()
    elapsed = (datetime.now() - started).total_seconds() / 60
    final = f"完成! OK={ok} FAIL={fail} 总耗时={elapsed:.1f}分钟"
    logger.info(final)
    print(final)


if __name__ == "__main__":
    asyncio.run(main())
