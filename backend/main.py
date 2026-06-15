from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.log_config import setup_logger
from app.api.leagues import router as leagues_router
from app.api.teams import router as teams_router
from app.api.players import router as players_router
from app.api.standings import router as standings_router
from app.api.fixtures import router as fixtures_router
from app.api.scheduler import router as scheduler_router
from app.api.predictions import router as predictions_router
from app.services.scheduler import init_scheduler, shutdown_scheduler

# 初始化日志
setup_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 注册定时任务"""
    # 注意: 禁用了 alembic 自动迁移，因为 autogenerate 会删除没有 SQLAlchemy 模型的表
    # （predictions、scheduler_tasks、scheduler_logs 使用 raw SQL 管理）
    # 新增表请手动建表或手动编写迁移

    logger.info("正在注册定时任务...")
    init_scheduler()

    try:
        yield
    except BaseException:
        pass
    finally:
        await shutdown_scheduler()
        logger.info("定时任务已取消")


app = FastAPI(
    title="Football API Proxy",
    description="API-Football v3.9.3 代理服务",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(leagues_router, prefix="/api", tags=["Leagues"])
app.include_router(teams_router, prefix="/api", tags=["Teams"])
app.include_router(players_router, prefix="/api", tags=["Players"])
app.include_router(standings_router, prefix="/api", tags=["Standings"])
app.include_router(fixtures_router, prefix="/api", tags=["Fixtures"])
app.include_router(scheduler_router, prefix="/api", tags=["Scheduler"])
app.include_router(predictions_router, prefix="/api", tags=["Predictions"])


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok"}
