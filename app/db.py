from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import DATABASE_URL, SERVERLESS

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}, future=True
    )

    # SQLite ignores foreign keys unless asked, every connection, every time.
    #
    # This is not a detail. The live site runs on PostgreSQL, which enforces
    # them; development and the whole test suite run on SQLite, which by
    # default does not. So a delete that leaves a row pointing at nothing
    # passes every test here and then fails with a 500 in front of a real
    # person — which is exactly how deleting a task that had been chased by
    # the follow-up desk got as far as the live site.
    #
    # With this on, the two behave the same and the tests can catch it.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
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
