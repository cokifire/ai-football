"""``elo_update`` 定时任务挂载 Flashscore xG 补缺脚本的回归测试。

锁住三件事：脚本挂在已有的 elo_update 入口上（不新增调度项）、按北京时间
同时覆盖上月与当月、xG 抓取失败不能带垮 Elo 落账。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services import elo_service as elo
from app.services import scheduler as sched


def _freeze_now(monkeypatch, value: datetime):
    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr(elo, "datetime", _FrozenDatetime)


def test_elo_update_task_uses_daily_wrapper():
    assert sched._DEFAULT_TASKS["elo_update"]["fn"] is elo.run_daily_elo_update


def test_wrapper_backfills_previous_and_current_month(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 11, 8, 9, 30))
    monkeypatch.setattr(elo, "update_finished_elos", lambda db: 3)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout="无需补缺, 结束。", returncode=0)

    monkeypatch.setattr(elo.subprocess, "run", fake_run)

    assert elo.run_daily_elo_update(object()) == 3
    assert [cmd[2:] for cmd, _ in calls] == [
        ["--year", "2026", "--month", "10"],
        ["--year", "2026", "--month", "11"],
    ]
    for cmd, kwargs in calls:
        assert cmd[1] == "tools/backfill_xg.py"
        assert kwargs["cwd"] == str(elo.BACKEND_ROOT)
        assert kwargs["timeout"] == elo.XG_BACKFILL_TIMEOUT


def test_wrapper_handles_year_boundary(monkeypatch):
    _freeze_now(monkeypatch, datetime(2027, 1, 2, 9, 30))
    monkeypatch.setattr(elo, "update_finished_elos", lambda db: 0)

    calls = []
    monkeypatch.setattr(
        elo.subprocess, "run",
        lambda cmd, **kwargs: (calls.append(cmd[2:]), SimpleNamespace(stdout="", returncode=0))[1],
    )

    elo.run_daily_elo_update(object())
    assert calls == [
        ["--year", "2026", "--month", "12"],
        ["--year", "2027", "--month", "1"],
    ]


def test_xg_failure_does_not_break_elo_update(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 11, 8, 9, 30))
    monkeypatch.setattr(elo, "update_finished_elos", lambda db: 7)

    def boom(cmd, **kwargs):
        raise OSError("python not found")

    monkeypatch.setattr(elo.subprocess, "run", boom)

    assert elo.run_daily_elo_update(object()) == 7
