"""外部数据源（api-football / firecrawl 等）的额度状态服务。

设计要点
--------
* **进程内缓存** 是当前进程的真理; **DB** 提供跨进程可见性与持久化。
  写路径先更缓存再 best-effort 写 DB; 读路径优先查缓存, 缓存未命中才查 DB
  并回填。如果 DB 不可用 (例如测试环境、临时故障), 服务降级运行 — 进程内
  标记仍然生效, 跨进程信息会停摆但不会因为 DB 抖动把 quota 状态搅乱。
* **跨天自动复位**: API-Football 免费额度按 **UTC 自然日** 重置, 因此
  ``exhausted_date`` 与 ``today`` 都用 ``datetime.now(timezone.utc).date()``
  计算。读取时若 ``exhausted_date < today`` (按 UTC) 则视作新的一天, 自动把
  ``quota_exhausted`` 置 0。比 server local 更稳: 部署机本地时区漂移或开发者
  从 Windows 跑脚本时, 不会因为"看似跨天但 UTC 还没跨"而提前放行, 也不会
  在 UTC 已经跨天后还把请求拦着。
* **Firecrawl 按月配额**: 托管套餐每月固定调用次数 (当前账号 1000 次/月,
  每月 9 号重置 — 以 credit-usage 接口的账期为准, 可用
  settings.firecrawl_quota_reset_day 覆盖), 不是按天。因此 firecrawl 的自动
  复位按"是否已到过当月
  重置日"判断, 撑不到重置日就一直闩锁 — 否则每天放行一次只会反复重试到
  再次耗尽, 白烧剩余额度。
* 调用约定:
    - ``is_quota_exhausted(provider)`` — 每条 API 请求前调用, 热路径 0 DB 访问。
    - ``mark_quota_exhausted(provider, reason)`` — 检测到额度耗尽时调用 (api-football 是当日, firecrawl 是当月配额周期)。
    - ``reset_provider(provider)`` — 每个调度 tick 开始时调用, 既强制清缓存又
      把 ``quota_exhausted`` 写回 DB。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.api_status import PROVIDER_API_FOOTBALL, PROVIDER_FIRECRAWL


# daily 复位基于 UTC 自然日 — 与 API-Football 实际重置节奏保持一致,
# 不依赖 server 本地时区或开发者机器时区. monthly 的边界计算同样以这个
# UTC 日期为基准 (见 _quota_reset_since).
def _utc_today():
    return datetime.now(timezone.utc).date()


# 进程内缓存: provider -> "True=已耗尽 / False=未耗尽". 默认按"未耗尽"处理。
_cache: dict[str, bool] = {}

# ── 各 provider 的额度重置节奏 ─────────────────────────────────
# 自动复位不能一律"跨天放行": api-football 免费额度按 UTC 自然日重置,
# 而 firecrawl 托管套餐是月度配额 (当前账号每月 1000 次, 每月 9 号重置) —
# 按月放行意味着"标记耗尽以来是否已经到过一个重置日", 撑不到重置日就一直
# 闩锁, 避免每天放行一次、反复重试到再次耗尽。
_RESET_CADENCE_DAILY = "daily"
_RESET_CADENCE_MONTHLY = "monthly"
_RESET_CADENCE: dict[str, str] = {
    PROVIDER_API_FOOTBALL: _RESET_CADENCE_DAILY,
    PROVIDER_FIRECRAWL: _RESET_CADENCE_MONTHLY,
}


def _last_monthly_reset_boundary(today, reset_day: int):
    """today 之前最近的一次月度重置日 (含今天)。

    reset_day=23 时: today=10-20 -> 09-23 (本月重置还没到);
    today=10-24 -> 10-23; today=11-05 -> 10-23 (10 月那次重置已发生)。
    """
    # 28 以后各月天数不一, 不展开; 账号重置日若 >28 由配置方自行收敛。
    reset_day = max(1, min(int(reset_day), 28))
    if today.day >= reset_day:
        return today.replace(day=reset_day)
    # 本月重置日还没到, 最近一次重置发生在上个月的同一天。
    last_of_prev_month = today.replace(day=1) - timedelta(days=1)
    return last_of_prev_month.replace(day=min(reset_day, last_of_prev_month.day))


def _quota_reset_since(provider: str, exhausted_date, today) -> bool:
    """自 exhausted_date 标记耗尽以来, 该 provider 的额度是否已重置过。

    daily:   exhausted_date < today (UTC 自然日翻页即放行)。
    monthly: 已经越过当月/上月重置日才放行; exhausted_date 恰好等于
             重置日时视作"这个周期就用光了", 不放行。
    """
    if exhausted_date is None:
        return False
    if _RESET_CADENCE.get(provider, _RESET_CADENCE_DAILY) == _RESET_CADENCE_MONTHLY:
        boundary = _last_monthly_reset_boundary(today, settings.firecrawl_quota_reset_day)
        return exhausted_date < boundary
    return exhausted_date < today


# 仅允许的 provider 名字典, 避免脚本拼错字符串写入脏行。
_PROVIDER_ALIASES: dict[str, str] = {
    PROVIDER_API_FOOTBALL: PROVIDER_API_FOOTBALL,
    "api_football": PROVIDER_API_FOOTBALL,
    PROVIDER_FIRECRAWL: PROVIDER_FIRECRAWL,
}

# 错误信息最大长度, 防止响应体很大时把表撑爆.
_LAST_ERROR_MAX_LEN = 1000


def _normalize(provider: str) -> str:
    """把脚本里散落的别名(如 'api_football')归一到表里写的主键('api-football')。"""
    return _PROVIDER_ALIASES.get(provider, provider)


def _safe_db(fn):
    """装饰器: 任何 DB 调用包一层 try/except, 失败只记日志不传播。

    配额状态属于"运维辅助信息": DB 抖动不应把热路径 (每个 API 请求都跑) 牵连
    起来报 SQLAlchemyError, 影响对核心功能的诊断。这里吞掉所有 ``SQLAlchemyError``
    与数据库连接错误, 进程内缓存仍然能保证单进程内的正确性, 跨进程一致性在
    DB 恢复后下一次标记时自动重新对齐。
    """

    def wrapped(*args, **kwargs):
        db = SessionLocal()
        try:
            return fn(db, *args, **kwargs)
        except (SQLAlchemyError, Exception) as exc:
            # 用 ``Exception`` 而不只是 ``SQLAlchemyError`` 是为了兼容"DB 不可用"
            # 这类连接层错误 (例如 ``pymysql.err.OperationalError`` 其实是
            # ``OperationalError`` 但 sqlalchemy 包装过; 而裸 ``pymysql`` 报 ``Exception``
            # 子类 — 都收进来).
            logger.debug(f"[api_status_service] DB 调用降级 (忽略): {exc}")
            return None
        finally:
            try:
                db.close()
            except Exception:
                pass

    return wrapped


def _ensure_row(db, provider: str) -> None:
    """幂等创建行。新 provider 第一次访问时插入 ``quota_exhausted=0`` 占位行。

    不用 ORM 是因为以后换数据源不希望这个模块跟着动; 用 raw SQL 把语义钉死在
    alembic 迁移里, 服务模块尽量无 ORM 依赖。
    """
    db.execute(
        text(
            "INSERT IGNORE INTO api_status "
            "(provider, quota_exhausted, created_at, updated_at) "
            "VALUES (:p, 0, :now, :now)"
        ),
        {"p": provider, "now": datetime.now()},
    )


def _load_from_db(provider: str) -> bool | None:
    """从 DB 读 quota_exhausted; 按 provider 节奏自动复位; 失败返回 None 让调用方走缓存默认.

    返回值含义:
        True  — DB 表示已耗尽
        False — DB 表示未耗尽
        None  — DB 不可用, 调用方应保留现有缓存值 (或默认 False)
    """
    db = SessionLocal()
    try:
        _ensure_row(db, provider)
        row = db.execute(
            text(
                "SELECT quota_exhausted, exhausted_date FROM api_status "
                "WHERE provider = :p"
            ),
            {"p": provider},
        ).fetchone()
        if row is None:
            return False

        exhausted = bool(row[0])
        exhausted_date = row[1]
        today = _utc_today()

        # 自动复位: 按 provider 的重置节奏判断"标记耗尽以来额度是否已重置"。
        # api-football 跨 UTC 天即放行; firecrawl 要撑到每月重置日 (默认 9 号)。
        if exhausted and _quota_reset_since(provider, exhausted_date, today):
            logger.info(
                f"[{provider}] 额度状态自动复位: exhausted_date={exhausted_date} "
                f"today={today}"
            )
            db.execute(
                text(
                    "UPDATE api_status SET quota_exhausted = 0, "
                    "exhausted_date = NULL, exhausted_at = NULL, "
                    "last_error = NULL, last_check_at = :now, updated_at = :now "
                    "WHERE provider = :p"
                ),
                {"p": provider, "now": datetime.now()},
            )
            db.commit()
            exhausted = False

        # 顺手写 last_check_at, 排查"为什么这次没拦住"用; 不影响其它字段.
        db.execute(
            text(
                "UPDATE api_status SET last_check_at = :now WHERE provider = :p"
            ),
            {"p": provider, "now": datetime.now()},
        )
        db.commit()

        return exhausted
    except (SQLAlchemyError, Exception) as exc:
        logger.debug(f"[api_status_service] DB 读取失败, 保留缓存默认: {exc}")
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def is_quota_exhausted(provider: str = PROVIDER_API_FOOTBALL) -> bool:
    """返回该 provider 额度是否已耗尽 (api-football 按 UTC 日, firecrawl 按月)。

    读路径:
    1. 命中进程内缓存 -> 直接返回, 0 次 DB 访问 (热路径)。
    2. 缓存失效 (进程刚启动 / 被 ``reset_provider`` 显式清掉) -> DB 查一次
       回填到缓存; 同时把 ``last_check_at`` 刷到 DB。
    3. DB 不可用时, 退化为"按现有缓存默认 False", 不让 quota 状态把热路径拖崩。
    """
    provider = _normalize(provider)

    # 进程内缓存命中——这是热路径, 每条 API 请求都会走这里.
    cached = _cache.get(provider)
    if cached is not None:
        return cached

    loaded = _load_from_db(provider)
    if loaded is None:
        # DB 不可用, 用一个"不知道是耗尽还是没耗尽"的保守默认——选 False
        # 是因为: 大多数日子 quota 都没耗尽, 让请求继续冲一次比误判耗尽更
        # 自然; 上游即便拿到 429 也有短时限流分支兜底。万一是真耗尽, 下次
        # 响应体里能再次识别并标记。
        _cache[provider] = False
        return False

    _cache[provider] = loaded
    return loaded


@_safe_db
def _persist_exhausted(db, provider: str, reason: str, now: datetime, today) -> bool:
    _ensure_row(db, provider)
    db.execute(
        text(
            "UPDATE api_status SET "
            "quota_exhausted = 1, "
            "exhausted_date = :d, "
            "exhausted_at = :now, "
            "last_check_at = :now, "
            "last_error = :err, "
            "updated_at = :now "
            "WHERE provider = :p"
        ),
        {"p": provider, "d": today, "now": now, "err": reason},
    )
    db.commit()
    return True


def mark_quota_exhausted(
    provider: str = PROVIDER_API_FOOTBALL, reason: str = "",
) -> None:
    """标记该 provider 额度已耗尽 (api-football 为当日, firecrawl 为当个配额周期)。

    写路径: 先更缓存再更 DB (best-effort)。
    缓存先更的语义: 即使 DB 写入失败, 当前进程也能立刻看到状态——避免在
    写入 DB 的瞬间, 其它请求仍走缓存拿到 False 继续冲一次。
    """
    provider = _normalize(provider)
    reason = (reason or "")[:_LAST_ERROR_MAX_LEN]
    _cache[provider] = True

    now = datetime.now()
    if _persist_exhausted(provider, reason, now, _utc_today()) is True:
        logger.warning(
            f"[{provider}] 额度已耗尽已写入 api_status: provider={provider} "
            f"reason={reason[:500]!r}"
        )
    else:
        logger.error(
            f"[{provider}] api_status 写入失败, 进程内已置 1, 跨进程同步待 DB 恢复: "
            f"provider={provider} reason={reason[:500]!r}"
        )


@_safe_db
def _persist_reset(db, provider: str) -> None:
    _ensure_row(db, provider)
    db.execute(
        text(
            "UPDATE api_status SET "
            "quota_exhausted = 0, "
            "exhausted_date = NULL, "
            "exhausted_at = NULL, "
            "last_error = NULL, "
            "last_check_at = :now, "
            "updated_at = :now "
            "WHERE provider = :p"
        ),
        {"p": provider, "now": datetime.now()},
    )
    db.commit()


def reset_provider(provider: str = PROVIDER_API_FOOTBALL) -> None:
    """显式复位: 清缓存 + 把 ``quota_exhausted`` 写回 0。

    调用方: 调度器每个 tick 开始时调用, 因为新的一天 API-Football 额度会恢复。
    与自动复位配合: 自动复位只解决"自标记耗尽以来额度已重置"这一种情况, 显式
    复位用于"今天日期还没变、但管理员手动把 key 改了 / 套餐升级了"的场景。
    """
    provider = _normalize(provider)
    _cache.pop(provider, None)
    _persist_reset(provider)


def get_status(provider: str = PROVIDER_API_FOOTBALL) -> dict:
    """读取该 provider 的全字段状态, 用于运维 / 调试 / API 展示。

    返回 dict 而不是 ORM 对象, 避免 session 关闭后访问属性的隐性坑.
    返回的字段是 DB 的最新值, 不读缓存 (避免热路径上的"读旧值"现象).
    """
    provider = _normalize(provider)
    db = SessionLocal()
    try:
        _ensure_row(db, provider)
        db.commit()
        row = db.execute(
            text(
                "SELECT provider, quota_exhausted, exhausted_date, exhausted_at, "
                "last_check_at, last_error, created_at, updated_at "
                "FROM api_status WHERE provider = :p"
            ),
            {"p": provider},
        ).fetchone()
        if row is None:
            return {"provider": provider, "quota_exhausted": False}
        out = dict(row._mapping)
        # 时间字段统一 isoformat, 方便 API 返回 JSON.
        for k in ("exhausted_date", "exhausted_at", "last_check_at", "created_at", "updated_at"):
            v = out.get(k)
            if v is not None and hasattr(v, "isoformat"):
                out[k] = v.isoformat()
        return out
    except (SQLAlchemyError, Exception) as exc:
        logger.debug(f"[api_status_service] get_status 失败, 返回缓存: {exc}")
        return {"provider": provider, "quota_exhausted": _cache.get(provider, False)}
    finally:
        try:
            db.close()
        except Exception:
            pass


def clear_cache_for_tests() -> None:
    """仅供测试使用: 清空进程内缓存, 让 ``is_quota_exhausted`` 走 DB. """
    _cache.clear()
