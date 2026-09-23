from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import DATABASE_URL, SERVERLESS

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}, future=True
    )
else:
    # Serverless: every invocation may land on a fresh container, and pooled
    # connections go stale between them. NullPool opens and closes per request,
    # which is what a pooled Postgres endpoint (Neon/Supabase pgbouncer) wants.
    kwargs = {"pool_pre_ping": True, "future": True}
    if SERVERLESS:
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
    engine = create_engine(DATABASE_URL, **kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
