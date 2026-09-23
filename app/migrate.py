"""Tiny idempotent migrator.

Adds any column that exists in the models but not yet in the database, so an
existing midap.db keeps its data across upgrades. Runs automatically on
startup. (Swap for Alembic when the schema starts changing in ways that need
real up/down migrations.)
"""
from sqlalchemy import text, inspect
from sqlalchemy import Enum as SAEnum

from .db import engine, Base
from . import models as _models      # noqa: F401 — registers every table on Base

# table -> [(column, SQL type + default)]
ADDITIONS = {
    "users": [
        ("rights", "TEXT DEFAULT ''"),
        ("bm_delegation", "INTEGER DEFAULT 60"),
        ("bm_checklist", "INTEGER DEFAULT 20"),
        ("bm_fms", "INTEGER DEFAULT 20"),
    ],
    "attachments": [
        ("content_type", "VARCHAR(120) DEFAULT ''"),
        ("storage", "VARCHAR(10) DEFAULT 'local'"),
        ("data", "BLOB"),
    ],
    "tasks": [
        ("false_marked", "BOOLEAN DEFAULT 0"),
        ("false_marked_by_id", "INTEGER"),
        ("false_marked_at", "DATETIME"),
        ("false_mark_reason", "TEXT"),
        ("reopen_count", "INTEGER DEFAULT 0"),
        ("reopened_by_id", "INTEGER"),
        ("reopened_at", "DATETIME"),
        ("audit_state", "VARCHAR(20) DEFAULT 'NOT_REQUIRED'"),
        ("audited_at", "DATETIME"),
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
    ],
    "recurring_rules": [
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
    ],
    "flow_steps": [
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
    ],
}


# The column types above are written in SQLite's spelling. PostgreSQL rejects
# several of them outright — BLOB is BYTEA, DATETIME is TIMESTAMP, and a
# boolean default has to be TRUE/FALSE, not 1/0. Without this the first
# upgrade of a live Postgres database fails on the ALTER and the app will not
# start. (A brand-new database never notices, because create_all builds the
# columns correctly — which is exactly why this is easy to miss.)
PG_TYPES = [
    ("BLOB", "BYTEA"),
    ("DATETIME", "TIMESTAMP"),
    ("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE"),
    ("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
]


def _ddl_for(ddl: str, dialect: str) -> str:
    if dialect != "postgresql":
        return ddl
    for a, b in PG_TYPES:
        if ddl.upper().startswith(a):
            return b + ddl[len(a):]
    return ddl


PRIORITY_TABLES = ("tasks", "recurring_rules", "flow_steps")


def _detype_priority(conn, insp, dialect: str, existing_tables: set) -> list[str]:
    """Convert a native PostgreSQL enum priority column to plain text.

    Only PostgreSQL ever made it a real type; SQLite has always stored text.
    Without this the rename below fails, because the database type still only
    knows LOW / NORMAL / HIGH / CRITICAL and has never heard of MEDIUM.
    """
    if dialect != "postgresql":
        return []
    done = []
    for table in PRIORITY_TABLES:
        if table not in existing_tables:
            continue
        col = next((c for c in insp.get_columns(table)
                    if c["name"] == "priority"), None)
        if col is None:
            continue
        # Check the TYPE CLASS, not its string form: a native PostgreSQL enum
        # prints as "VARCHAR(8)", so a text check here silently skips exactly
        # the columns that need converting.
        if not isinstance(col["type"], SAEnum):
            continue                      # already plain text, nothing to do
        conn.execute(text(
            f"ALTER TABLE {table} ALTER COLUMN priority "
            "TYPE VARCHAR(20) USING priority::text"))
        done.append(f"{table}.priority -> text")
    return done


def run() -> list[str]:
    Base.metadata.create_all(engine)
    applied = []
    insp = inspect(engine)
    dialect = engine.dialect.name
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        applied += _detype_priority(conn, insp, dialect, existing_tables)

        for table, cols in ADDITIONS.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name in have:
                    continue
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {name} {_ddl_for(ddl, dialect)}"))
                applied.append(f"{table}.{name}")

        # backfill rights for users created before the rights model existed
        if "users" in existing_tables:
            conn.execute(text(
                "UPDATE users SET rights = '' WHERE rights IS NULL"
            ))

        # backfill the audit state for tasks that predate the column: anything
        # an auditor already touched is done, anything still flagged for audit
        # is pending, everything else never needed one.
        if "tasks" in existing_tables:
            conn.execute(text(
                "UPDATE tasks SET audit_state = 'NOT_REQUIRED' WHERE audit_state IS NULL"))
            conn.execute(text(
                "UPDATE tasks SET audit_state = 'PENDING' "
                "WHERE audit_state = 'NOT_REQUIRED' AND requires_audit "
                "AND auditor_id IS NULL"))
            conn.execute(text(
                "UPDATE tasks SET audit_state = 'COMPLETED' "
                "WHERE auditor_id IS NOT NULL AND audit_state <> 'COMPLETED'"))

            # Priority became three levels with a score weight: HIGH counts
            # as five tasks, MEDIUM two, LOW one. The old NORMAL becomes
            # MEDIUM and CRITICAL folds into HIGH — nothing is left pointing
            # at a level the software no longer knows.
            for table in PRIORITY_TABLES:
                if table in existing_tables:
                    conn.execute(text(
                        f"UPDATE {table} SET priority = 'MEDIUM' "
                        "WHERE priority = 'NORMAL'"))
                    conn.execute(text(
                        f"UPDATE {table} SET priority = 'HIGH' "
                        "WHERE priority = 'CRITICAL'"))

            # There is no "accept the task" step any more — work starts when
            # it is assigned. Anything still sitting in PENDING was waiting on
            # a button that no longer exists, so move it on.
            conn.execute(text(
                "UPDATE tasks SET status = 'IN_PROGRESS', "
                "started_at = COALESCE(started_at, created_at) "
                "WHERE status = 'PENDING'"))
    return applied
