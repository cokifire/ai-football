"""从指定 fixture id 开始递减，循环拉取比赛并落库。

用法 (在 backend/ 目录下运行):
    python tools/backfill_fixtures_by_id.py --start-id 1200000
    python tools/backfill_fixtures_by_id.py            # 交互输入起始 id
    python tools/backfill_fixtures_by_id.py --start-id 1200000 --min-id 1190000 --sleep 2.0
    python tools/backfill_fixtures_by_id.py --start-id 1200000 --leagues 39,140
    python tools/backfill_fixtures_by_id.py --start-id 1200000 --max-requests 200   # 安全上限

逻辑:
    - 从 start-id 开始，每次 id-1 递减查询 GET /fixtures?id=<id>
    - 若比赛 league_id 在白名单内 -> upsert 到数据库
    - 否则丢弃
    - 当 API 额度耗尽 (x-ratelimit-requests-remaining=0 / 429 / 限额报错) -> 退出循环结束程序
    - --max-requests 提供硬性安全上限，避免付费 Key 下无限制刷爆额度/数据库
    - Ctrl+C 可随时中断，会打印本次统计

注意:
    - 免费版对 ?id= 的历史比赛可能返回 "free plan" 限制错误(无数据)，此时跳过并继续，
      直到额度耗尽自动退出。历史比赛稳定获取需要付费版 Key。
    - fixture id 并非连续，递减过程中大量 id 会返回空(无比赛)，这是该方式的固有低效。
    - 仅落库 fixture 主表；完赛子数据(事件/阵容/统计)可在之后用现有 refresh 端点补齐。
"""
import sys
from pathlib import Path

# 让脚本在 backend/ 下以 `python tools/xxx.py` 运行时也能 import app 包
# (直接运行脚本时 sys.path[0] 是 tools/ 目录，而非 backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime
import time
import argparse

import httpx
from app.db.session import SessionLocal
from app.core.config import settings
from app.services.fixture_service import _upsert_fixture

# 默认允许的联赛 id 白名单
DEFAULT_LEAGUES = {1, 2, 3, 5, 10, 39, 45, 61, 71, 78, 135, 140}

RETRY_MAX = 3
API_TIMEOUT = 30.0


def _normalize_errors(errors) -> str:
    if isinstance(errors, dict):
        return " ".join(str(v) for v in errors.values())
    if isinstance(errors, list):
        return " ".join(str(e) for e in errors)
    return str(errors)


def fetch_one(fid: int) -> dict:
    """拉取单个 fixture。返回带 status 的字典。

    status:
        ok        -> 有数据 (data["response"] 非空)
        empty     -> 该 id 无比赛数据
        free_plan -> 免费版限制，该比赛无法访问 (非额度问题)
        quota     -> 额度耗尽，应退出
        error     -> 其它错误
    """
    url = f"{settings.api_football_base_url}/fixtures"
    headers = {"x-apisports-key": settings.api_football_key}

    for attempt in range(RETRY_MAX):
        try:
            r = httpx.get(url, headers=headers, params={"id": str(fid)}, timeout=API_TIMEOUT)
        except httpx.TransportError as e:
            wait = 1.5 * (2 ** attempt)
            if attempt < RETRY_MAX - 1:
                time.sleep(wait)
                continue
            return {"status": "error", "message": f"网络错误: {e}"}

        # 额度耗尽: 剩余请求数为 0
        remaining = r.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None and remaining.strip() in ("0", "0.0"):
            return {"status": "quota", "message": "x-ratelimit-requests-remaining = 0"}

        # 限流: 429 重试，仍失败视为额度耗尽
        if r.status_code == 429:
            retry_after = r.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after else 1.5 * (2 ** attempt)
            except ValueError:
                wait = 1.5 * (2 ** attempt)
            if attempt < RETRY_MAX - 1:
                time.sleep(wait)
                continue
            return {"status": "quota", "message": "持续 429 限流，额度可能已耗尽"}

        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            return {"status": "error", "message": f"HTTP {e.response.status_code}"}

        data = r.json()

        # 解析错误字段
        errors = data.get("errors")
        if errors:
            text = _normalize_errors(errors)
            tl = text.lower()
            # 免费版窗口限制 (非额度问题) -> 跳过继续
            if "free plan" in tl or ("plan" in tl and "free" in tl) or "subscription" in tl:
                return {"status": "free_plan", "message": text}
            # 订阅/请求额度耗尽 -> 退出
            if "limit" in tl or "rate" in tl or "exceeded" in tl or "requests" in tl:
                return {"status": "quota", "message": text}
            return {"status": "error", "message": text}

        items = data.get("response", [])
        return {"status": "ok" if items else "empty", "data": data, "remaining": remaining}

    return {"status": "error", "message": "超过最大重试次数"}


def main():
    parser = argparse.ArgumentParser(description="从 fixture id 递减回填比赛数据")
    parser.add_argument("--start-id", type=int, default=None, help="起始 fixture id (不传则交互输入)")
    parser.add_argument("--min-id", type=int, default=1, help="递减下限，到达即停止 (默认 1)")
    parser.add_argument("--sleep", type=float, default=2.0, help="每次请求间隔秒数 (默认 2.0)")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="硬性请求上限, 0=不限制(仅靠额度/下限停止) (默认 0)")
    parser.add_argument("--leagues", type=str, default=None,
                        help="允许的 league id 列表, 逗号分隔 (默认使用内置白名单)")
    args = parser.parse_args()

    if not settings.api_football_key:
        print("错误: 未配置 API_FOOTBALL_KEY，请在 backend/.env 中设置")
        sys.exit(1)
    if not settings.api_football_base_url:
        print("错误: 未配置 API_FOOTBALL_BASE_URL，请在 backend/.env 中设置")
        sys.exit(1)

    # 解析联赛白名单
    if args.leagues:
        try:
            allowed = {int(x) for x in args.leagues.split(",") if x.strip()}
        except ValueError:
            print("错误: --leagues 必须是逗号分隔的整数, 如 39,140")
            sys.exit(1)
        if not allowed:
            print("错误: --leagues 解析后为空")
            sys.exit(1)
    else:
        allowed = DEFAULT_LEAGUES

    # 起始 id
    if args.start_id is not None:
        fid = args.start_id
    else:
        try:
            fid = int(input("请输入起始 fixture id: ").strip())
        except (ValueError, EOFError):
            print("无效的 id")
            sys.exit(1)

    if args.max_requests:
        print(f"[安全上限] 最多处理 {args.max_requests} 个请求")

    print(f"起始 id={fid}, 下限={args.min_id}, 间隔={args.sleep}s, 联赛白名单={sorted(allowed)}")
    print("按 Ctrl+C 可随时中断。")

    checked = saved = skipped_plan = skipped_league = empty = 0
    started = datetime.now()
    db = SessionLocal()
    try:
        while fid >= args.min_id:
            checked += 1
            res = fetch_one(fid)

            # 额度/硬性上限检查
            if res["status"] == "quota":
                print(f"\n额度耗尽，停止。最后检查的 id={fid} :: {res['message']}")
                break
            if res["status"] == "error":
                print(f"\n发生错误，停止。id={fid} :: {res['message']}")
                break
            if args.max_requests and checked >= args.max_requests:
                print(f"\n已达到 --max-requests 上限 ({args.max_requests})，停止。")
                break

            # 低额度预警
            rem = res.get("remaining")
            if rem is not None:
                try:
                    if int(rem) <= 5:
                        print(f"  ⚠️  API 剩余额度较低: {rem}")
                except ValueError:
                    pass

            if res["status"] == "free_plan":
                skipped_plan += 1
                if skipped_plan % 20 == 0:
                    print(f"  id={fid}: 免费版限制(无数据)，已跳过 {skipped_plan} 场 "
                          f"(历史比赛需付费版 Key 才能用 ?id= 获取)")
            elif res["status"] == "empty":
                empty += 1
            elif res["status"] == "ok":
                try:
                    for row in res["data"]["response"]:
                        lid = (row.get("league") or {}).get("id")
                        if lid in allowed:
                            _upsert_fixture(db, row)
                            saved += 1
                        else:
                            skipped_league += 1
                    db.commit()
                except Exception as e:
                    db.rollback()
                    print(f"\n数据库写入失败 id={fid}: {e}")
                    break

            # 进度输出
            if checked % 25 == 0 or (saved and checked % 5 == 0):
                elapsed = (datetime.now() - started).total_seconds() / 60
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 已查 {checked} | "
                      f"保存 {saved} | 跳过(计划){skipped_plan} | "
                      f"跳过(联赛){skipped_league} | 空 {empty} | id={fid} ({elapsed:.1f}min)")

            fid -= 1
            if fid >= args.min_id:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\n用户中断 (Ctrl+C)")
    finally:
        db.close()

    elapsed = (datetime.now() - started).total_seconds() / 60
    print("=" * 50)
    print(f"结束。耗时 {elapsed:.1f} 分钟")
    print(f"  检查: {checked}")
    print(f"  保存: {saved}")
    print(f"  跳过(免费版限制): {skipped_plan}")
    print(f"  跳过(非白名单联赛): {skipped_league}")
    print(f"  空数据: {empty}")


if __name__ == "__main__":
    main()
