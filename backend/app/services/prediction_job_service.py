'''异步预测任务服务

把原本同步阻塞的 predict_fixture 包装成后台任务,通过 DB 表 background_jobs
持久化任务状态,前端以 job_id 轮询进度,避免长 HTTP 连接被代理/网关中断。
'''

import json
import uuid
from datetime import datetime

from sqlalchemy import text
from loguru import logger
from fastapi.encoders import jsonable_encoder

from app.db.session import SessionLocal
from prediction.predict import (
    predict_fixture,
    PredictionDataError,
    PredictionLLMError,
)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"

JOB_TYPE_PREDICT = "predict_match"


def _generate_job_id() -> str:
    return uuid.uuid4().hex[:16]


def create_job(fixture_id: int) -> str:
    '''创建一个预测后台任务,初始状态为 pending。'''
    job_id = _generate_job_id()
    db = SessionLocal()
    try:
        db.execute(
            text(
                """INSERT INTO background_jobs
                   (id, job_type, status, payload, created_at)
                   VALUES (:id, :job_type, :status, :payload, :created_at)"""
            ),
            {
                "id": job_id,
                "job_type": JOB_TYPE_PREDICT,
                "status": JOB_STATUS_PENDING,
                "payload": json.dumps({"fixture_id": fixture_id}),
                "created_at": datetime.now(),
            },
        )
        db.commit()
    finally:
        db.close()
    logger.info(f"创建预测任务 job_id={job_id} fixture_id={fixture_id}")
    return job_id


def get_job(job_id: str) -> dict | None:
    '''读取任务当前状态与结果。'''
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT * FROM background_jobs WHERE id = :id"),
            {"id": job_id},
        ).fetchone()
        if not row:
            return None
        return {
            "job_id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "payload": _load_json(row.payload),
            "result": _load_json(row.result),
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
    finally:
        db.close()


def _load_json(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _update_job(job_id: str, **kwargs):
    '''更新任务字段;result/error 自动 JSON 序列化。'''
    if not kwargs:
        return
    allowed = {"status", "result", "error", "started_at", "finished_at"}
    sets = []
    params = {"id": job_id}
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k in ("result", "payload") and v is not None:
            v = json.dumps(jsonable_encoder(v), ensure_ascii=False)
        sets.append(f"{k} = :{k}")
        params[k] = v
    if not sets:
        return
    db = SessionLocal()
    try:
        db.execute(
            text(f"UPDATE background_jobs SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        db.commit()
    finally:
        db.close()


def run_prediction_job(job_id: str, fixture_id: int) -> None:
    '''在后台线程中执行预测,并更新 job 状态。'''
    logger.info(f"开始执行预测任务 job_id={job_id} fixture_id={fixture_id}")
    _update_job(job_id, status=JOB_STATUS_RUNNING, started_at=datetime.now())

    try:
        result = predict_fixture(fixture_id)
        _update_job(
            job_id,
            status=JOB_STATUS_DONE,
            result=result,
            finished_at=datetime.now(),
        )
        logger.info(f"预测任务完成 job_id={job_id} fixture_id={fixture_id}")
    except PredictionDataError as e:
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            error=f"数据不足: {e}",
            finished_at=datetime.now(),
        )
        logger.warning(f"预测任务数据不足 job_id={job_id} fixture_id={fixture_id}: {e}")
    except PredictionLLMError as e:
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            error=f"LLM 生成失败: {e}",
            finished_at=datetime.now(),
        )
        logger.warning(f"预测任务 LLM 失败 job_id={job_id} fixture_id={fixture_id}: {e}")
    except Exception as e:
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            error=f"预测异常: {e}",
            finished_at=datetime.now(),
        )
        logger.exception(f"预测任务异常 job_id={job_id} fixture_id={fixture_id}: {e}")
