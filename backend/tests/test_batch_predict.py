"""比赛中心「批量预测」的回归测试。

锁定几条不变量：
1. ``POST /predict/batch`` 必须注册在 ``POST /predict/{fixture_id}`` 之前，
   否则 ``batch`` 会被当成 fixture_id 命中参数路由并触发 int 校验失败。
2. 提交时去重、保序、限量；空列表直接拒绝。
3. 批量 worker 串行执行：上一场结束后立即开始下一场（不插入额外等待），
   已预测的场次跳过、单场失败不阻断后续场次、进度逐场落库。
4. 同一进程内只允许一个批量任务跑，避免重复消耗 LLM / 情报抓取额度。
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import predictions as api
from app.core.security import require_admin
from app.services import prediction_job_service as svc


def _make_client(monkeypatch):
    """构造一个不碰 DB / LLM 的测试 app，所有副作用函数都换成桩。"""
    monkeypatch.setattr(api, "create_job", lambda fid: "single_job")
    monkeypatch.setattr(api, "run_prediction_job", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_batch_job", lambda ids: "batch_job")
    monkeypatch.setattr(api, "run_batch_prediction_job", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[require_admin] = lambda: "admin"
    return TestClient(app)


def test_batch_route_is_not_captured_by_single_predict(monkeypatch):
    """/predict/batch 必须命中批量端点，而不是 /predict/{fixture_id}。"""
    client = _make_client(monkeypatch)
    res = client.post("/api/predict/batch", json={"fixture_ids": [1, 2, 1, 3]})

    assert res.status_code == 200
    body = res.json()
    assert body["job_id"] == "batch_job"
    assert body["total"] == 3  # 去重后 3 场


def test_batch_validates_empty_and_oversize(monkeypatch):
    client = _make_client(monkeypatch)

    assert client.post("/api/predict/batch", json={"fixture_ids": []}).status_code == 400
    too_many = list(range(api.MAX_BATCH_SIZE + 1))
    assert client.post("/api/predict/batch", json={"fixture_ids": too_many}).status_code == 400


def test_single_predict_route_still_works(monkeypatch):
    """新增批量端点后，原单场预测端点不受影响。"""
    client = _make_client(monkeypatch)
    res = client.post("/api/predict/12345", json={})
    assert res.status_code == 200
    assert res.json()["fixture_id"] == 12345
    assert res.json()["job_id"] == "single_job"


def test_batch_worker_is_serial_and_isolates_failures(monkeypatch):
    """串行、跳过已预测、单场失败继续、逐场进度快照、任务最终 done。"""
    updates = []
    predicted_order = []

    def fake_update(job_id, **kwargs):
        # 模拟真实 _update_job：result 在调用时就被序列化成快照
        result = kwargs.get("result")
        updates.append((
            kwargs.get("status"),
            json.loads(json.dumps(result)) if result is not None else None,
        ))

    def fake_predict(fid):
        predicted_order.append(fid)
        if fid == 3:
            raise RuntimeError("boom")

    monkeypatch.setattr(svc, "_update_job", fake_update)
    monkeypatch.setattr(svc, "_already_predicted", lambda fid: fid == 2)
    monkeypatch.setattr(svc, "predict_fixture", fake_predict)

    svc.run_batch_prediction_job("JOB1", [1, 2, 3, 4])

    # 串行顺序：1 跑、2 跳过、3 失败、4 仍继续跑
    assert predicted_order == [1, 3, 4]
    assert svc._BATCH_LOCK.acquire(blocking=False) is True  # 锁已释放
    svc._BATCH_LOCK.release()

    final_status, final_result = updates[-1]
    assert final_status == "done"
    assert final_result == {
        "total": 4,
        "done": 4,
        "current_fixture_id": None,
        "succeeded": [1, 4],
        "failed": [{"fixture_id": 3, "error": "boom"}],
        "skipped": [2],
    }
    # 进度是逐场递增写出的，而不是最后一次性写入
    done_counts = [r["done"] for _, r in updates if r]
    assert done_counts == sorted(done_counts)
    assert max(done_counts) == 4


def test_batch_worker_rejects_concurrent_run(monkeypatch):
    """已有批量任务在跑时，新任务直接标记失败且不触发任何预测。"""
    called = []
    monkeypatch.setattr(svc, "predict_fixture", lambda fid: called.append(fid))
    recorded = []
    monkeypatch.setattr(svc, "_update_job", lambda jid, **kw: recorded.append(kw))

    assert svc._BATCH_LOCK.acquire(blocking=False) is True
    try:
        svc.run_batch_prediction_job("JOB2", [1, 2])
        assert called == []
        assert recorded[0]["status"] == svc.JOB_STATUS_FAILED
        assert "正在运行" in recorded[0]["error"]
    finally:
        svc._BATCH_LOCK.release()
