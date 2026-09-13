"""数据库引擎与会话管理。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    # 常规接口 + AI 短事务的并发峰值；chat_sse 已改为分段短事务持有连接，
    # 不再随流式问答时长独占连接（此前默认 pool_size=5 在并发流式下会耗尽）
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()