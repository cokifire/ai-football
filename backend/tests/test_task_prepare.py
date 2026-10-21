"""任务执行前准备逻辑的回归测试。

覆盖的 bug：定时路径 (``app.services.scheduler.run_task``) 与手动触发路径
(``app.api.scheduler._run_task_with_stream``) 曾各自实现"执行前占位"，手动路径
漏掉了 ``reset_quota_latch``。后果是当天早些时候真的撞到日额度并被标记后，
``api_status.quota_exhausted`` 会一直是 1（UTC 自然日不翻页不自动复位），
之后所有手动触发一进来就被"额度已耗尽"拦住 —— 哪怕控制面板显示额度仍有剩余。

修复方式不是"手动路径补一行"，而是抽出 ``_prepare_task_run`` 让两条路径共用
同一段准备逻辑，避免以后再次分叉。本文件锁住这个不变量。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import scheduler as sched


def test_prepare_resets_latch_when_claimed(monkeypatch):
    """拿到执行权时应复位额度闩锁（给本次运行一次探测机会）。"""
    monkeypatch.setattr(sched, "_claim_task", lambda *a, **kw: True)
    resets = []
    monkeypatch.setattr(sched, "reset_quota_latch", lambda: resets.append(1))

    assert sched._prepare_task_run("league_sync", "联赛数据同步") is True
    assert len(resets) == 1


def test_prepare_skips_reset_when_not_claimed(monkeypatch):
    """没拿到执行权（任务已在运行）时不应复位闩锁。"""
    monkeypatch.setattr(sched, "_claim_task", lambda *a, **kw: False)
    resets = []
    monkeypatch.setattr(sched, "reset_quota_latch", lambda: resets.append(1))

    assert sched._prepare_task_run("league_sync", "联赛数据同步") is False
    assert len(resets) == 0


def test_run_task_resets_latch(monkeypatch):
    """``run_task``（定时路径）必须经 ``_prepare_task_run`` 触发复位。"""
    monkeypatch.setattr(sched, "_claim_task", lambda *a, **kw: True)
    monkeypatch.setattr(sched, "_update_db", lambda *a, **kw: None)
    resets = []
    monkeypatch.setattr(sched, "reset_quota_latch", lambda: resets.append(1))

    ran = {"n": 0}
    ok = sched.run_task("league_sync", "联赛数据同步", lambda db: ran.__setitem__("n", 1), db=object())

    assert ok is True
    assert ran["n"] == 1
    assert len(resets) == 1


def test_run_task_skips_when_not_claimed(monkeypatch):
    """占位失败时 ``run_task`` 既不复位也不执行任务。"""
    monkeypatch.setattr(sched, "_claim_task", lambda *a, **kw: False)
    resets = []
    monkeypatch.setattr(sched, "reset_quota_latch", lambda: resets.append(1))

    ran = {"n": 0}
    ok = sched.run_task("league_sync", "联赛数据同步", lambda db: ran.__setitem__("n", 1), db=object())

    assert ok is False
    assert ran["n"] == 0
    assert len(resets) == 0


def test_stream_path_uses_shared_prepare(monkeypatch):
    """手动触发路径必须复用 ``_prepare_task_run``，而不是直连 ``_claim_task``。

    这条测试对"手动路径漏掉复位"的 bug 形态敏感：如果 ``_run_task_with_stream``
    仍直连 ``_claim_task``，那么把 ``_prepare_task_run`` 换成返回 False 也不会
    阻止任务执行，``ran["n"]`` 会变成 1。
    """
    from app.api import scheduler as api_sched

    seen = []
    monkeypatch.setattr(
        api_sched, "_prepare_task_run",
        lambda task_id, task_name: (seen.append((task_id, task_name)), False)[1],
    )
    # 陷阱：让底层占位"成功"。若 _run_task_with_stream 绕过 _prepare_task_run 直连
    # _claim_task（bug 形态），就会拿到 True 继续执行；只有真正复用 prepare 才会在
    # False 时提前退出。注意 _claim_task 现在只挂在 services.scheduler 上，
    # api.scheduler 已不再直接导入它。
    monkeypatch.setattr(sched, "_claim_task", lambda *a, **kw: True)

    ran = {"n": 0}
    api_sched._run_task_with_stream(
        "league_sync", "联赛数据同步", lambda db: ran.__setitem__("n", 1)
    )

    assert seen == [("league_sync", "联赛数据同步")]
    assert ran["n"] == 0


def test_legacy_endpoints_delegate_to_run_task(monkeypatch):
    """兼容旧接口必须走 ``run_task``（共享占位 + 复位），而不是直连 ``sync_fn``。

    这条对"遗留端点漏复位"的形态敏感：一旦有人把它们改回 ``sync_fn(db)``，
    ``run_task`` 就不会被调用，``calls`` 为空。
    """
    from app.api import scheduler as api_sched

    calls = []
    monkeypatch.setattr(
        api_sched, "run_task",
        lambda tid, name, fn, db=None: (calls.append((tid, name)), True)[1],
    )

    for tid, endpoint in (
        ("league_sync", api_sched.trigger_league_sync),
        ("team_sync", api_sched.trigger_team_sync),
        ("player_sync", api_sched.trigger_player_sync),
        ("standing_sync", api_sched.trigger_standing_sync),
        ("fixture_live", api_sched.trigger_live_sync),
        ("fixture_daily", api_sched.trigger_fixture_sync),
    ):
        asyncio.run(endpoint(_=None, db=None))

    assert [tid for tid, _ in calls] == [
        "league_sync", "team_sync", "player_sync",
        "standing_sync", "fixture_live", "fixture_daily",
    ]
    # 任务名应取自 TASK_MAP，避免再出现第三份硬编码映射
    for tid, name in calls:
        assert name == api_sched.TASK_MAP[tid][0]


def test_legacy_fixture_sync_keeps_409_on_conflict(monkeypatch):
    """``/scheduler/fixtures/sync`` 原本对"正在运行"返回 409，改走 run_task 后要保留。"""
    from fastapi import HTTPException

    from app.api import scheduler as api_sched

    monkeypatch.setattr(api_sched, "run_task", lambda *a, **kw: False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_sched.trigger_fixture_sync(_=None, db=None))

    assert exc.value.status_code == 409
