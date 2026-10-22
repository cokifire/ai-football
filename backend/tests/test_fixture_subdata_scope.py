"""赛程每日同步的子数据范围回归测试。"""

from app.services import fixture_service as service


class _Query:
    def __init__(self):
        self.calls = []

    def filter(self, *args):
        self.calls.append(("filter", args))
        return self

    def order_by(self, *args):
        self.calls.append(("order_by", args))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        return self

    def all(self):
        return []


class _DB:
    def __init__(self):
        self.query_obj = _Query()

    def query(self, _model):
        return self.query_obj


def test_scoped_subdata_sync_does_not_scan_200_historical_fixtures():
    db = _DB()

    service.sync_completed_sub_data(db, fixture_ids={101, 102})

    assert not any(kind == "limit" for kind, _ in db.query_obj.calls)
    filter_args = [arg for kind, args in db.query_obj.calls if kind == "filter" for arg in args]
    assert any(" IN " in str(arg) for arg in filter_args)


def test_unscoped_subdata_sync_keeps_backfill_limit():
    db = _DB()

    service.sync_completed_sub_data(db)

    assert ("limit", 200) in db.query_obj.calls
