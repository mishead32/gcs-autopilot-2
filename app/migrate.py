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
        ("decision", "VARCHAR(10)"),
        ("held_from", "VARCHAR(20)"),
        ("covers_day", "DATE"),
        # The human reference: DEL-01, CL-01, FMS-01. Backfilled below.
        ("ref", "VARCHAR(20)"),
        ("ref_n", "INTEGER"),
    ],
    "recurring_rules": [
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
        # Everything a schedule needs that a single number could not hold.
        # All nullable: an existing rule keeps running off day_of exactly as
        # it did, and nothing has to be re-entered.
        ("weekdays", "VARCHAR(20)"),
        ("month_days", "VARCHAR(60)"),
        ("weeks_of_month", "VARCHAR(20)"),
        ("start_month", "INTEGER"),
        ("anchor_on", "DATE"),
    ],
    "branches": [
        # Existing companies get Sunday off, which is true of every one of
        # them except the gym and the spa — those two are switched off by
        # hand on the Branches page, where it is visible.
        ("weekly_off", "INTEGER DEFAULT 6"),
    ],
    "flow_instances": [
        ("held_at", "DATETIME"),
        ("held_by_id", "INTEGER"),
        ("hold_reason", "TEXT"),
        ("held_days", "INTEGER DEFAULT 0"),
        ("cancelled_at", "DATETIME"),
        ("cancelled_by_id", "INTEGER"),
        ("cancel_reason", "TEXT"),
    ],
    "flows": [
        ("start_fields", "TEXT"),
        ("start_form", "TEXT"),
    ],
    "flow_steps": [
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
        ("tat_unit", "VARCHAR(10) DEFAULT 'hours'"),
        ("tat_value", "INTEGER DEFAULT 24"),
        ("due_from_pos", "INTEGER"),
        # Left NULL on purpose for flows built before routing existed — the
        # engine reads NULL as "whatever comes next in order", so every
        # existing flow keeps running exactly as it did.
        ("next_step_pos", "INTEGER"),
        ("fail_step_pos", "INTEGER"),
        ("is_decision", "BOOLEAN DEFAULT 0"),
        ("pass_label", "VARCHAR(60)"),
        ("fail_label", "VARCHAR(60)"),
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

# Values added to an enum AFTER the live database was first built.
#
# PostgreSQL makes these real types. create_all() builds the type correctly on
# a brand-new database and then never touches it again, so a value added to
# the Python enum later simply does not exist in a database that already has
# the type — and the first person to use it gets
#   invalid input value for enum taskstatus: "ON_HOLD"
# which reaches them as a blank 500 page.
#
# SQLite stores the text and does not care, which is exactly why this is easy
# to miss in development. Same trap as the priority rename above.
#
# type name -> labels that must exist. Labels are the enum MEMBER names,
# which is what SQLAlchemy stores by default (ON_HOLD, not on_hold).
ENUM_VALUES = {
    "taskstatus": ["ON_HOLD"],
    "recurrence": ["YEARLY", "FORTNIGHTLY", "QUARTERLY"],
    "auditstate": ["WAITING"],
}


def _extend_enums(dialect: str) -> list[str]:
    """Teach an existing PostgreSQL enum about values added since it was made.

    Runs on its own AUTOCOMMIT connection: ALTER TYPE ... ADD VALUE cannot be
    used later in the same transaction that added it, and on older servers
    cannot run inside a transaction at all.
    """
    if dialect != "postgresql":
        return []
    done = []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for type_name, labels in ENUM_VALUES.items():
            exists = conn.execute(text(
                "SELECT 1 FROM pg_type WHERE typname = :t"), {"t": type_name}
            ).scalar()
            if not exists:
                continue                      # create_all will build it whole
            have = {r[0] for r in conn.execute(text(
                "SELECT e.enumlabel FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid WHERE t.typname = :t"),
                {"t": type_name}).all()}
            for label in labels:
                if label in have:
                    continue
                conn.execute(text(
                    f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'"))
                done.append(f"{type_name} += {label}")
    return done


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
    dialect_name = engine.dialect.name
    # Before create_all: a table being created now would reference the type,
    # and a type that already exists has to learn the new value first.
    pre = _extend_enums(dialect_name)

    Base.metadata.create_all(engine)
    applied = list(pre)
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

        # A task flagged for audit used to read "Audit pending" from the
        # moment it was created, so the auditor's list was full of work
        # nobody had started. Anything still unfinished goes back to
        # WAITING; anything the doer has finished stays pending, because
        # that is exactly what it is.
        if "tasks" in existing_tables:
            moved = conn.execute(text(
                "UPDATE tasks SET audit_state = 'WAITING' "
                "WHERE UPPER(CAST(audit_state AS VARCHAR)) = 'PENDING' "
                "AND UPPER(CAST(status AS VARCHAR)) NOT IN "
                "('SUBMITTED', 'COMPLETED')"))
            if moved.rowcount:
                applied.append(
                    f"{moved.rowcount} unfinished task(s): audit pending -> waiting")

        # "Weekdays" is no longer offered: Monday to Friday is Weekly with
        # five days ticked, and two names for one schedule only ever made
        # people pick the one that would not let them add Saturday. Existing
        # rules are converted rather than left as an orphan value nothing in
        # the page can show.
        if "recurring_rules" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("recurring_rules")}
            if "weekdays" in cols:
                moved = conn.execute(text(
                    "UPDATE recurring_rules SET frequency = :weekly, "
                    "weekdays = '0,1,2,3,4' "
                    "WHERE UPPER(CAST(frequency AS VARCHAR)) = 'WEEKDAYS'"),
                    {"weekly": "WEEKLY"})
                if moved.rowcount:
                    applied.append(f"{moved.rowcount} weekdays rule(s) -> weekly Mon-Fri")

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

            # Turnaround time grew a unit. Everything that existed before was
            # expressed in hours, so that is exactly what it becomes — no
            # deadline shifts, the same number simply gains its unit.
            if "flow_steps" in existing_tables:
                conn.execute(text(
                    "UPDATE flow_steps SET tat_unit = 'hours' WHERE tat_unit IS NULL"))
                # Not "WHERE tat_value IS NULL": the ALTER above carries
                # DEFAULT 24, so every upgraded row already reads 24 and a
                # NULL check silently matches nothing — a 4-hour step would
                # quietly become 24. For an hours step the two columns must
                # agree, and tat_hours is the one that was really set, so it
                # wins. Safe to run for ever: rows that already agree are
                # untouched, and a step in days/weeks/months is not matched.
                conn.execute(text(
                    "UPDATE flow_steps SET tat_value = tat_hours "
                    "WHERE tat_unit = 'hours' AND tat_value <> tat_hours"))

            # There is no "accept the task" step any more — work starts when
            # it is assigned. Anything still sitting in PENDING was waiting on
            # a button that no longer exists, so move it on.
            conn.execute(text(
                "UPDATE tasks SET status = 'IN_PROGRESS', "
                "started_at = COALESCE(started_at, created_at) "
                "WHERE status = 'PENDING'"))

        # A fresh inspector: the ALTERs above ran on this same
        # connection, and the one made at the top of run() was
        # taken before them, so it still believes ref does not
        # exist and the backfill would quietly do nothing.
        applied += _backfill_refs(conn, inspect(conn), existing_tables)
    return applied


def _backfill_refs(conn, insp, existing_tables: set) -> list[str]:
    """Give every task that predates the reference column its number.

    Numbered per company and per kind of work, oldest first, so the reference
    order matches the order the work actually appeared. Runs on every boot
    and does nothing once every row has one, which also means a task created
    by some path that bypassed the session listener still gets numbered on
    the next restart rather than staying blank for ever.
    """
    if "tasks" not in existing_tables:
        return []
    cols = {c["name"] for c in insp.get_columns("tasks")}
    if "ref" not in cols or "ref_n" not in cols:
        return []

    prefixes = {src.name: pref for src, pref in _models.REF_PREFIX.items()}
    done = []
    rows = conn.execute(text(
        "SELECT DISTINCT org_id, CAST(source AS VARCHAR) FROM tasks")).all()
    for org_id, src_raw in rows:
        # SQLite stores the member name, PostgreSQL the same; be forgiving
        # about case either way rather than silently skipping a whole series.
        key = (src_raw or "DELEGATION").upper()
        prefix = prefixes.get(key) or prefixes.get(key.replace("-", "_")) or "TSK"
        used = conn.execute(text(
            "SELECT COALESCE(MAX(ref_n), 0) FROM tasks "
            "WHERE org_id = :o AND CAST(source AS VARCHAR) = :s"),
            {"o": org_id, "s": src_raw}).scalar() or 0
        blank = conn.execute(text(
            "SELECT id FROM tasks WHERE org_id = :o "
            "AND CAST(source AS VARCHAR) = :s AND ref IS NULL "
            "ORDER BY created_at, id"), {"o": org_id, "s": src_raw}).all()
        for (task_id,) in blank:
            used += 1
            conn.execute(text(
                "UPDATE tasks SET ref = :r, ref_n = :n WHERE id = :i"),
                {"r": _models.ref_text(prefix, used), "n": used, "i": task_id})
        if blank:
            done.append(f"{len(blank)} task(s) numbered {prefix}-…")
    return done
