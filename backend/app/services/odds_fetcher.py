"""赔率获取的"API → Flashscore 兜底"统一入口。

为什么需要这个模块
------------------
原本 predict.py / api/predictions.py / value_bet_service.py 等多个地方都重复
写着「先调 API-Football 的 ``/odds``，失败再走 ``fetch_flashscore_odds``」的同
一段逻辑。问题:

1. 同一个 fallback 模式散落 3 处, 改一处忘一处的概率不低;
2. 与 ``api_status`` 表耦合弱 — 调用方不一定知道要先 ``is_quota_exhausted()``
   再决定走哪条路径, 实际上原有 ``_api_get`` 是把这个责任接过去的, 但 fast-path
   上是"API 返回空 dict 再降级", 一旦 fallback 中断就看不出是 quota 还是网络。

本模块封装统一的入口 ``fetch_odds_with_fallback(fixture, db, ...)``:

* **入口检查**: 先查 ``api_status`` 表 (``api-football`` 额度)。如果已耗尽, 直接走
  Flashscore, 不再冲 API。
* **API 路径**: 调用 predict.py 的 ``_fetch_odds``。它内部也已封装 ``is_quota_exhausted``
  但与本服务的检查"双保险"无副作用 — 两次都查的是同一个 DB 行;
* **降级路径**: API 没拿到 ``odds_data`` (被 quota 拦截 / 接口返回 error / 数据为
  空) 时调用 ``predict._fetch_flashscore_odds``。
* **错误路径**: 两条路径都拿不到时, 抛 ``OddsUnavailableError`` 由调用方决定是
  截图 / 记录 / 直接冒泡。

调用方迁移
----------
* ``prediction.predict.predict_fixture`` 里 1311-1316 行那段手写 fallback 应该
  走 ``fetch_odds_with_fallback`` (后续 PR 跟进, 这次先建模块并暴露);
* ``app.api.predictions.fetch_and_save_odds`` 同理;
* ``app.services.value_bet_service.evaluate_value_bet`` 同理。

兼容 / 不破坏现有行为
* 完全等价替换 — 返回结构与 ``predict._fetch_odds`` 一致 (``{"odds_data": [...]}"``)。
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from app.services.api_status_service import (
    PROVIDER_API_FOOTBALL,
    is_quota_exhausted,
)


class OddsUnavailableError(Exception):
    """API-Football 与 Flashscore 两条路径都没拿到可用赔率。

    应被调用方捕获后决定是否落预测失败、是否提示用户、是否等次日再试。
    """


def fetch_odds_with_fallback(
    fixture: dict,
    db: Optional["object"] = None,
) -> dict:
    """统一的赔率抓取入口: API-Football → Flashscore 兜底。

    参数
    ----
    fixture:
        ``predict_fixture`` 风格的 dict，至少包含 ``id`` / ``date`` / ``home_id``
        / ``away_id`` / ``home_name`` / ``away_name`` 这几个字段。
        - ``_fetch_odds`` 使用 id / date
        - ``_fetch_flashscore_odds`` 使用 home_id / away_id 反查 flashscore 哈希
          并结合 home_name / away_name 用于页面校验
    db:
        备用参数 (留作将来 ``_fetch_flashscore_odds`` 演进时把 json map 移到 DB
        时兼容)。现有实现不用 db session。

    返回
    ----
    ``{"odds_data": [{"bookmaker": ..., "entries": [...]}]}`` 与
    ``predict._fetch_odds`` 同构。

    抛出
    ----
    ``OddsUnavailableError`` — 两条路径都没拿到。
    """
    # import 推迟到函数内避免循环依赖（predict 模块反过来也会 import 本 service）
    from prediction.predict import (
        _fetch_odds,
        _fetch_flashscore_odds,
    )

    fixture_id = fixture.get("id")
    match_date = fixture.get("date")

    # ── 入口检查：api_status 配额已耗尽就别再冲 API 了 ──
    quota_already_exhausted = is_quota_exhausted(PROVIDER_API_FOOTBALL)
    if quota_already_exhausted:
        logger.info(
            f"[赔率] api_status 标记 api-football 当日额度已耗尽, 跳过 API 直接走 Flashscore: "
            f"fixture_id={fixture_id}"
        )
        odds = _fetch_flashscore_odds(fixture)
        if not (odds and odds.get("odds_data")):
            raise OddsUnavailableError(
                f"fixture_id={fixture_id}: api-football 配额已耗尽且 Flashscore 兜底未抓到赔率"
            )
        return odds

    # ── API-Football 路径 ──
    odds = _fetch_odds(fixture_id, match_date)
    # _fetch_odds 在 quota / 限流时返回 {} 或 {"__api_error__": ...} 哨兵
    if odds and odds.get("odds_data"):
        return odds

    if isinstance(odds, dict) and odds.get("__api_error__") is not None:
        # API 显式报 quota error, fallback 是合理选择
        logger.info(
            f"[赔率] api-football 报接口错误, 改走 Flashscore 兜底: fixture_id={fixture_id}, "
            f"errors={odds.get('__api_error__')}"
        )

    # ── Flashscore 兜底 ──
    flashscore_odds = _fetch_flashscore_odds(fixture)
    if flashscore_odds and flashscore_odds.get("odds_data"):
        return flashscore_odds

    # ── 两条路径都拿不到 ──
    raise OddsUnavailableError(
        f"fixture_id={fixture_id}: api-football 与 flashscore 均未抓到可用赔率"
    )
