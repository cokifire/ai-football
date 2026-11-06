'''异步预测任务服务

把原本同步阻塞的 predict_fixture 包装成后台任务,通过 DB 表 background_jobs
持久化任务状态,前端以 job_id 轮询进度,避免长 HTTP 连接被代理/网关中断。
'''

import json
import threading
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
JOB_TYPE_PREDICT_BATCH = "predict_batch"

# 进程内锁：同一时刻只允许一个批量预测任务在跑，避免重复消耗 LLM/情报抓取额度。
# 用内存锁而非 DB 状态，服务重启后自动释放，不会留下卡死的 running 任务。
_BATCH_LOCK = threading.Lock()


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


# ──── 批量预测任务 ────

def recover_stale_jobs() -> int:
    """服务启动时把残留的 pending/running 任务标记为失败。

    后台任务随进程存活，进程重启后不可能再完成；不复位的话前端会一直轮询
    一个永远不会结束的 job（尤其是耗时很长的批量任务）。
    """
    db = SessionLocal()
    try:
        res = db.execute(
            text(
                """UPDATE background_jobs
                   SET status = :failed,
                       error = '服务重启，任务中断',
                       finished_at = :now
                   WHERE status IN ('pending', 'running')"""
            ),
            {"failed": JOB_STATUS_FAILED, "now": datetime.now()},
        )
        db.commit()
        count = res.rowcount
        if count:
            logger.warning(f"复位 {count} 个中断的后台预测任务（服务重启）")
        return count
    finally:
        db.close()


def _new_batch_progress(fixture_ids: list[int]) -> dict:
    """批量任务的进度快照（写入 background_jobs.result，供前端轮询）。"""
    return {
        "total": len(fixture_ids),
        "done": 0,
        "current_fixture_id": None,
        "succeeded": [],
        "failed": [],
        "skipped": [],
    }


def create_batch_job(fixture_ids: list[int]) -> str:
    """创建批量预测后台任务，初始状态 pending，并预置一份空进度。"""
    job_id = _generate_job_id()
    db = SessionLocal()
    try:
        db.execute(
            text(
                """INSERT INTO background_jobs
                   (id, job_type, status, payload, result, created_at)
                   VALUES (:id, :job_type, :status, :payload, :result, :created_at)"""
            ),
            {
                "id": job_id,
                "job_type": JOB_TYPE_PREDICT_BATCH,
                "status": JOB_STATUS_PENDING,
                "payload": json.dumps({"fixture_ids": fixture_ids}),
                "result": json.dumps(_new_batch_progress(fixture_ids)),
                "created_at": datetime.now(),
            },
        )
        db.commit()
    finally:
        db.close()
    logger.info(f"创建批量预测任务 job_id={job_id} 共 {len(fixture_ids)} 场")
    return job_id


def _already_predicted(fixture_id: int) -> bool:
    """该场次是否已有预测记录。批量任务耗时长，期间可能已被单场预测，跳过以免重复消耗额度。"""
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT 1 FROM predictions WHERE fixture_id = :fid LIMIT 1"),
            {"fid": fixture_id},
        ).first() is not None
    finally:
        db.close()


def run_batch_prediction_job(job_id: str, fixture_ids: list[int]) -> None:
    """串行执行批量预测：上一场预测结束后立即开始下一场，不插入额外等待。

    每场结果写入 job.result（succeeded / failed / skipped / current_fixture_id），
    前端据此展示逐场进度；单场失败不影响后续场次。
    """
    if not _BATCH_LOCK.acquire(blocking=False):
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            error="已有批量预测任务正在运行，请等待其结束",
            finished_at=datetime.now(),
        )
        logger.warning(f"批量预测任务被拒绝（已有任务运行）job_id={job_id}")
        return

    logger.info(f"开始执行批量预测任务 job_id={job_id} 共 {len(fixture_ids)} 场")
    _update_job(job_id, status=JOB_STATUS_RUNNING, started_at=datetime.now())
    progress = _new_batch_progress(fixture_ids)
    _update_job(job_id, result=progress)

    try:
        for idx, fid in enumerate(fixture_ids):
            # 已有预测记录的场次直接跳过，避免重复跑昂贵的 codex 预测。
            if _already_predicted(fid):
                logger.info(f"批量预测跳过（已预测）fixture={fid} [{idx+1}/{len(fixture_ids)}]")
                progress["skipped"].append(fid)
                progress["done"] = idx + 1
                progress["current_fixture_id"] = None
                _update_job(job_id, result=progress)
                continue

            progress["current_fixture_id"] = fid
            _update_job(job_id, result=progress)
            logger.info(f"批量预测 [{idx+1}/{len(fixture_ids)}] fixture={fid}")

            try:
                predict_fixture(fid)
                progress["succeeded"].append(fid)
                logger.info(f"批量预测完成 fixture={fid}")
            except Exception as e:
                progress["failed"].append({"fixture_id": fid, "error": str(e)})
                logger.warning(f"批量预测失败 fixture={fid}: {e}")

            progress["done"] = idx + 1
            progress["current_fixture_id"] = None
            _update_job(job_id, result=progress)

        _update_job(
            job_id,
            status=JOB_STATUS_DONE,
            result=progress,
            finished_at=datetime.now(),
        )
        logger.info(
            f"批量预测任务完成 job_id={job_id} "
            f"成功 {len(progress['succeeded'])} / 失败 {len(progress['failed'])} "
            f"/ 跳过 {len(progress['skipped'])}"
        )
    except Exception as e:
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            error=f"批量预测异常: {e}",
            finished_at=datetime.now(),
        )
        logger.exception(f"批量预测任务异常 job_id={job_id}: {e}")
    finally:
        _BATCH_LOCK.release()
