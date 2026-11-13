"""Tiny idempotent migrator.

Adds any column that exists in the models but not yet in the database, so an
existing midap.db keeps its data across upgrades. Runs automatically on
startup. (Swap for Alembic when the schema starts changing in ways that need
real up/down migrations.)
"""
from sqlalchemy import bindparam, text, inspect
from sqlalchemy import Enum as SAEnum

from .db import engine, Base
from . import models as _models      # noqa: F401 — registers every table on Base

# table -> [(column, SQL type + default)]
ADDITIONS = {
    "users": [
        ("rights", "TEXT DEFAULT ''"),
        # The other companies somebody covers, on top of their own.
        ("also_branch_ids", "TEXT"),
        ("bm_delegation", "INTEGER DEFAULT 60"),
        ("bm_checklist", "INTEGER DEFAULT 20"),
        ("bm_fms", "INTEGER DEFAULT 20"),
    ],
    "attachments": [
        ("content_type", "VARCHAR(120) DEFAULT ''"),
        # A file answered on an FMS start form, and the question it answers.
        ("flow_instance_id", "INTEGER"),
        ("start_field", "VARCHAR(160)"),
        ("storage", "VARCHAR(10) DEFAULT 'local'"),
        ("data", "BLOB"),
        # Handed to the doer with the work, rather than produced by them as
        # proof of it. FALSE for every existing row: delegation had no such
        # marker, so there is nothing to backfill — the files that were
        # briefings are indistinguishable from the ones that were proof.
        ("briefing", "BOOLEAN DEFAULT 0"),
    ],
    "tasks": [
        ("how_to", "TEXT"),
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
        # An FMS step whose turn has not come round yet. Every existing task
        # is live work, so FALSE is the right answer for all of them.
        ("flow_waiting", "BOOLEAN DEFAULT 0"),
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
    "organizations": [
        # Escalation, group-wide switches. Off for an existing database on
        # purpose: the ladder has to be filled in before anything is sent,
        # and an upgrade that starts emailing the CMD by itself is not an
        # upgrade.
        ("esc_on", "BOOLEAN DEFAULT 0"),
        ("esc_working_days", "BOOLEAN DEFAULT 1"),
        ("esc_after_top", "VARCHAR(10) DEFAULT 'stop'"),
        ("esc_scope", "VARCHAR(10) DEFAULT 'chased'"),
        ("esc_hour", "INTEGER DEFAULT 9"),
        # Days overdue before the first level is told. 0 keeps what every
        # existing site already does, so nothing changes under anybody.
        ("esc_grace", "INTEGER DEFAULT 0"),
        # Off by default: an email to every doer every morning is a real
        # change in what the software does to people, not a tidy-up.
        ("esc_doer_daily", "BOOLEAN DEFAULT 0"),
        # Blank = every day, so nothing changes for a site that had no
        # such setting until it is asked for.
        ("esc_days", "VARCHAR(20) DEFAULT ''"),
        ("esc_doer_whatsapp", "BOOLEAN DEFAULT 0"),
        ("esc_last_run", "DATE"),
        # This site's address, for the links inside an escalation email.
        # Filled in the first time the Escalation page is opened.
        ("site_url", "VARCHAR(200)"),
        # The quiet-FMS report: flows nobody is starting. Off for an
        # existing database for the same reason escalation is — an upgrade
        # that starts emailing the CMD by itself is not an upgrade.
        ("quiet_on", "BOOLEAN DEFAULT 0"),
        ("quiet_gap", "INTEGER DEFAULT 2"),
        ("quiet_to", "TEXT"),
        ("quiet_cc", "TEXT"),
        ("quiet_hour", "INTEGER DEFAULT 11"),
        ("quiet_last_run", "DATE"),
        ("appr_sheet_id", "VARCHAR(120)"),
        ("appr_tab", "VARCHAR(120)"),
        ("appr_start_row", "INTEGER"),
        ("appr_read_at", "DATETIME"),
        ("appr_error", "TEXT"),
    ],
    "escalation_mails": [
        # The HTML version of the message. NULL on a row written before the
        # email had one, which still reads back as the plain text it was.
        ("body_html", "TEXT"),
    ],
    "escalations": [
        # Escalations are grouped into one email per person per morning.
        # NULL on a row written before that, which carried its own message
        # in its own subject/body columns — and still reads as it was sent.
        ("mail_id", "INTEGER"),
    ],
    "branches": [
        # Existing companies get Sunday off, which is true of every one of
        # them except the gym and the spa — those two are switched off by
        # hand on the Branches page, where it is visible.
        ("weekly_off", "INTEGER DEFAULT 6"),
    ],
    "flow_instances": [
        # The submission's own number — FMS-01, FMS-02. Backfilled below for
        # every run that predates it.
        ("ref", "VARCHAR(20)"),
        ("ref_n", "INTEGER"),
        # Which company a run is for. NULL on every run started before this
        # existed, which is the truth: nobody was asked.
        ("branch_id", "INTEGER"),
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
    "outbound_messages": [
        # Why a message did not arrive, in the provider's own words.
        ("error", "VARCHAR(400)"),
    ],
    "flow_steps": [
        ("requires_attachment", "BOOLEAN DEFAULT 1"),
        ("tat_unit", "VARCHAR(10) DEFAULT 'hours'"),
        ("tat_value", "INTEGER DEFAULT 24"),
        ("due_from_pos", "INTEGER"),
        ("due_from_field", "VARCHAR(160)"),
        # A deadline measured from a date on the start form: how the gap is
        # counted, and which side of that date it falls. Only meaningful
        # when tat_unit is the form-date unit; harmless on every other step,
        # which is why they are plain defaults rather than nullable flags.
        ("form_span", "VARCHAR(10) DEFAULT 'days'"),
        ("form_way", "VARCHAR(6) DEFAULT 'after'"),
        ("extra_doer_ids", "TEXT"),
        # A step assigned to a position rather than a person.
        ("doer_dept", "VARCHAR(120)"),
        # A step done by whoever a start-form question named, which is the
        # only way a step's doer can differ from run to run.
        ("doer_field", "VARCHAR(160)"),
        ("how_to", "TEXT"),
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
    "aiverdict": ["OK", "WEAK", "UNRELATED", "NO_PROOF", "ERROR"],
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


# --------------------------------------------------------------- indexes --
# The columns everything filters on. None of these mattered while this was
# a few hundred rows in a file on the same machine: SQLite reads the whole
# table in a blink and nobody notices. They matter enormously at ten
# thousand rows with the database across the network, because every
# unindexed filter becomes a full scan whose cost is paid in round trips.
#
# Written as plain CREATE INDEX IF NOT EXISTS rather than through the
# models, so an existing database gets them on the next restart without
# anybody running anything — and so this list is readable as what it is:
# the shape of the questions this software actually asks.
#
# Composite where the pair is always asked together. "Everything open for
# this organisation" and "what is on this person's plate" are the two
# queries behind most pages here.
INDEXES = [
    ("tasks", "ix_tasks_org_status", "(org_id, status)"),
    ("tasks", "ix_tasks_doer_status", "(doer_id, status)"),
    ("tasks", "ix_tasks_org_due", "(org_id, due_at)"),
    ("tasks", "ix_tasks_branch", "(branch_id)"),
    ("tasks", "ix_tasks_source", "(source)"),
    ("tasks", "ix_tasks_assigner", "(assigner_id)"),
    # Every FMS run page and every progress bar reads the tasks of one
    # run. Unindexed, that was a scan of the whole tasks table per run,
    # and the FMS list draws fifty progress bars.
    ("tasks", "ix_tasks_flow_instance", "(flow_instance_id)"),
    ("tasks", "ix_tasks_rule_day", "(rule_id, covers_day)"),
    ("tasks", "ix_tasks_closed", "(closed_at)"),
    ("tasks", "ix_tasks_submitted", "(submitted_at)"),
    # A task's own page reads its comments and its files. Neither column
    # was indexed, so opening one task scanned both whole tables.
    ("task_comments", "ix_task_comments_task", "(task_id)"),
    ("attachments", "ix_attachments_task", "(task_id)"),
    ("flow_instances", "ix_flow_instances_flow", "(flow_id)"),
    ("flow_instances", "ix_flow_instances_org_done", "(org_id, completed_at)"),
    ("flow_instances", "ix_flow_instances_branch", "(branch_id)"),
    ("flow_steps", "ix_flow_steps_flow", "(flow_id)"),
    ("escalation_mails", "ix_escalation_mails_org_day", "(org_id, day)"),
    ("followups", "ix_followups_org_day", "(org_id, day)"),
    ("outbound_messages", "ix_outbound_org_made", "(org_id, created_at)"),
    # The CMD board reads one window: what is open now, plus what falls
    # in the period. Both halves start from the org and the kind of work,
    # so this is the index that keeps the heaviest page off a full scan
    # as the history grows.
    ("tasks", "ix_tasks_org_source_due", "(org_id, source, due_at)"),
    ("tasks", "ix_tasks_org_source_status", "(org_id, source, status)"),
    # "Who has been holding work for a month" — open work, oldest first.
    ("tasks", "ix_tasks_org_status_due", "(org_id, status, due_at)"),
]


def _indexes(conn, existing_tables: set) -> list[str]:
    """Create any missing index. Safe to run on every start — see INDEXES."""
    done = []
    for table, name, cols in INDEXES:
        if table not in existing_tables:
            continue
        try:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}"))
            done.append(name)
        except Exception as exc:                      # noqa: BLE001
            # A column renamed away, or a dialect that will not take it.
            # An index is an optimisation: never fail a deploy for one.
            print(f"Index {name} not created: {type(exc).__name__}: {exc}")
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
        # After create_all, so a table made on this very run gets them too.
        # Reported separately: a list of twenty index names in the deploy
        # log every restart would bury the migrations that matter.
        _made = _indexes(conn, set(inspect(conn).get_table_names()))
        if _made:
            print(f"Indexes checked: {len(_made)} present")

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

        # `insp` was taken before those ALTERs ran and still describes the
        # database as it was, so every data fix below that asks "does this
        # column exist yet?" would answer no on the one run that matters —
        # the deploy that adds it. It would then do its work on the NEXT
        # restart, which is a migration that silently takes two deploys and
        # a day of wrong-looking data in between. Caught doing exactly that
        # to the form-date conversion. A fresh inspector on this connection
        # sees the columns that were just added.
        insp = inspect(conn)

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

        # Three ways of saying one thing, folded into one. "Take timestamp
        # from form", "Days before a date on the form" and "Days after a
        # date on the form" were three dropdown entries for the same
        # setting with the number fixed at 0, negative and positive — so
        # somebody choosing read all three to find out they were the same.
        # They are now one entry with the number, the span and the side
        # chosen separately.
        #
        # Converted rather than left alone, so a step saved last month and
        # one saved today are the same shape on the page and in the
        # database. The deadlines do not move: each old unit maps to the
        # exact arithmetic it already did.
        if "flow_steps" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("flow_steps")}
            if {"form_span", "form_way"} <= cols:
                for was, value, way in (("formtime", "0", "after"),
                                        ("daysbefore", None, "before"),
                                        ("daysafter", None, "after")):
                    sets = ["tat_unit = 'formdate'", "form_span = 'days'",
                            f"form_way = '{way}'"]
                    if value is not None:
                        sets.append(f"tat_value = {value}")
                    moved = conn.execute(text(
                        f"UPDATE flow_steps SET {', '.join(sets)} "
                        "WHERE tat_unit = :was"), {"was": was})
                    if moved.rowcount:
                        applied.append(
                            f"{moved.rowcount} step(s): {was} -> formdate")

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
        # After the task backfill, because it re-points FMS steps at the
        # number their submission has just been given.
        applied += _number_runs(conn, inspect(conn), existing_tables)
        applied += _dedupe_checklist_tasks(conn, existing_tables)
        applied += _cancel_phantom_flow_steps(conn, existing_tables)

    applied += _lock_checklist_days(existing_tables)
    return applied


# Everything that points at a task and would otherwise hold a deleted row in
# place. Listed rather than relied on through the ORM because this runs as
# plain SQL on a connection, before any session exists.
TASK_CHILDREN = [
    ("task_comments", "task_id", "DELETE"),
    ("attachments", "task_id", "DELETE"),
    ("followups", "task_id", "DELETE"),
    ("ai_audits", "task_id", "DELETE"),
    ("outbound_messages", "task_id", "NULL"),
    ("help_tickets", "task_id", "NULL"),
]


def _cancel_phantom_flow_steps(conn, existing_tables: set) -> list[str]:
    """Clear the copies left behind by the replan bug.

    Any step of a run may be marked early, and people do — step 5 gets
    done on Monday because the paperwork is in front of them. Redrawing
    the plan asked "does this step have a task?" and counted only LIVE
    work, so a step somebody had finished looked unplanned and was given
    a fresh task: pending, new deadline, for work that was done. Again
    every time the run moved. Fixed in replan; this clears what it made.

    Deliberately narrow, because the one thing worse than a phantom task
    is deleting a real one:

      * same run, same step, SAME PERSON — a second doer's task is their
        own work, not a copy of somebody else's
      * both rows still sitting on the PLAN (flow_waiting) — a step the
        run has already walked through and come back to is a rework
        loop, and its new task is real
      * the finished one really is finished, and the copy is untouched:
        nothing handed in, nothing closed, no note, no auditor, no
        comment, no file

    Cancelled rather than deleted. A cancelled task is parked: it chases
    nobody, reddens nothing and counts against no score, but the row is
    still there to be looked at if anybody asks what happened.
    """
    if "tasks" not in existing_tables:
        return []
    cols = {c["name"] for c in inspect(conn).get_columns("tasks")}
    if not {"flow_instance_id", "flow_step_id", "flow_waiting"} <= cols:
        return []

    rows = conn.execute(text(
        "SELECT t.id FROM tasks t "
        "WHERE t.flow_instance_id IS NOT NULL "
        "  AND t.flow_step_id IS NOT NULL "
        "  AND t.flow_waiting = :yes "
        "  AND UPPER(CAST(t.status AS VARCHAR)) IN ('PENDING', 'IN_PROGRESS') "
        "  AND t.submitted_at IS NULL AND t.closed_at IS NULL "
        "  AND t.completion_note IS NULL AND t.auditor_id IS NULL "
        "  AND NOT EXISTS (SELECT 1 FROM task_comments c "
        "                  WHERE c.task_id = t.id) "
        "  AND NOT EXISTS (SELECT 1 FROM attachments a "
        "                  WHERE a.task_id = t.id) "
        "  AND EXISTS (SELECT 1 FROM tasks d "
        "              WHERE d.flow_instance_id = t.flow_instance_id "
        "                AND d.flow_step_id = t.flow_step_id "
        "                AND d.doer_id = t.doer_id "
        "                AND d.id <> t.id "
        "                AND d.flow_waiting = :yes "
        "                AND UPPER(CAST(d.status AS VARCHAR)) "
        "                    IN ('COMPLETED', 'SUBMITTED'))"),
        {"yes": True}).all()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    for n in range(0, len(ids), 200):
        chunk = ids[n:n + 200]
        conn.execute(
            text("UPDATE tasks SET status = 'CANCELLED' WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": chunk})
    return [f"{len(ids)} duplicate FMS step task(s) cancelled — the step "
            "was already finished by the same person"]


def _dedupe_checklist_tasks(conn, existing_tables: set) -> list[str]:
    """Remove the second copy of a checklist job, then stop it happening again.

    The spawner has always checked before inserting, but three things can run
    it at once — the midnight loop, the cron ping, and the Run spawner now
    button — and all three can look, see nothing, and insert. The doer is
    then shown the same job twice on the same day with no way to tell the
    copies apart, which is how a checklist stops being believed.

    Only an untouched copy is removed: nothing started, nothing submitted,
    no note, no proof, no audit. If every copy has been worked on, they are
    all left exactly where they are and the unique index is not created —
    a constraint is never worth deleting somebody's evidence for.
    """
    if "tasks" not in existing_tables:
        return []
    done = []

    groups = conn.execute(text(
        "SELECT rule_id, covers_day FROM tasks "
        "WHERE rule_id IS NOT NULL AND covers_day IS NOT NULL "
        "GROUP BY rule_id, covers_day HAVING COUNT(*) > 1")).all()

    removed = 0
    for rule_id, covers in groups:
        # started_at is deliberately NOT one of the signals: a task is in
        # progress from the moment it is handed out, so every copy carries
        # one and nothing would ever be cleaned up. What counts is evidence
        # that a person did something — handed it in, closed it, wrote a
        # note, was audited, or attached proof.
        rows = conn.execute(text(
            "SELECT id, submitted_at, closed_at, completion_note, auditor_id "
            "FROM tasks WHERE rule_id = :r AND covers_day = :d "
            "ORDER BY id"), {"r": rule_id, "d": covers}).all()
        # The one that is kept: the first that somebody has actually worked
        # on, otherwise the oldest. Keeping the worked-on copy means the
        # doer's effort survives the clean-up even when they happened to
        # open the second one.
        worked = [r for r in rows
                  if r[1] or r[2] or r[3] or r[4]
                  or conn.execute(text(
                      "SELECT 1 FROM attachments WHERE task_id = :i LIMIT 1"),
                      {"i": r[0]}).scalar()]
        if len(worked) > 1:
            continue                     # two real ones; a person must decide
        keep = worked[0][0] if worked else rows[0][0]
        for r in rows:
            if r[0] == keep:
                continue
            for table, col, how in TASK_CHILDREN:
                if table not in existing_tables:
                    continue
                if how == "DELETE":
                    conn.execute(text(f"DELETE FROM {table} WHERE {col} = :i"),
                                 {"i": r[0]})
                else:
                    conn.execute(text(
                        f"UPDATE {table} SET {col} = NULL WHERE {col} = :i"),
                        {"i": r[0]})
            conn.execute(text("DELETE FROM tasks WHERE id = :i"), {"i": r[0]})
            removed += 1
    if removed:
        done.append(f"{removed} duplicate checklist task(s) removed")

    return done


def _lock_checklist_days(existing_tables: set) -> list[str]:
    """Stop two runs ever creating the same checklist day twice.

    create_all() only builds constraints for a table it is creating, so an
    existing database never gets this unless it is asked for by name. On its
    OWN connection on purpose: a database that still holds duplicates refuses
    the index, and in PostgreSQL a failed statement poisons the whole
    transaction it is in — taking the real migrations down with it.
    """
    if "tasks" not in existing_tables:
        return []
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_rule_day "
                "ON tasks (rule_id, covers_day)"))
        return []
    except Exception as exc:
        return [f"could not lock checklist days ({exc.__class__.__name__}) — "
                "duplicate checklist tasks are still there and somebody has "
                "worked on more than one copy"]


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
        if key == "FLOW":
            # An FMS step has no series of its own: it carries its
            # submission's number, handed out by _number_runs. Numbering it
            # here as well would give the same step two different answers
            # depending on which ran last.
            continue
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


def _number_runs(conn, insp, existing_tables: set) -> list[str]:
    """Give every FMS submission its FMS-nn, and point its steps at it.

    Two jobs, in this order, because the second depends on the first.

    1. Every run with no number gets one, per company, OLDEST FIRST — so
       FMS-01 is the first submission that company ever made and the order
       of the numbers is the order the work actually happened. Numbering by
       row id would come out nearly the same and be wrong the moment two
       companies' runs interleave, which they do.

    2. Every FMS task is re-pointed at its run's number. Those tasks used to
       carry a series of their own, so a four-step run showed four unrelated
       numbers and the submission itself had none. After this a step reads
       "FMS-12 · Step 2 of 4", which is how people already talk about it.

    Step 2 does change references that already exist, which is not something
    to do lightly: an old FMS-15 was a STEP and is now whatever its run is
    called. It is done anyway because the alternative is worse — two
    different things both called FMS-15, one a submission and one a step,
    with no way to tell which from the number. Nothing else in the system
    keys off these strings; they are what people read and say.

    Runs on every boot and does nothing once every row is right, so a run
    created by some path that bypassed the session listener still gets a
    number on the next restart rather than staying blank for ever.
    """
    if "flow_instances" not in existing_tables:
        return []
    cols = {c["name"] for c in insp.get_columns("flow_instances")}
    if "ref" not in cols or "ref_n" not in cols:
        return []

    done = []
    prefix = _models.RUN_PREFIX

    for (org_id,) in conn.execute(text(
            "SELECT DISTINCT org_id FROM flow_instances")).all():
        used = conn.execute(text(
            "SELECT COALESCE(MAX(ref_n), 0) FROM flow_instances "
            "WHERE org_id = :o"), {"o": org_id}).scalar() or 0
        blank = conn.execute(text(
            "SELECT id FROM flow_instances WHERE org_id = :o AND ref IS NULL "
            "ORDER BY started_at, id"), {"o": org_id}).all()
        for (run_id,) in blank:
            used += 1
            conn.execute(text(
                "UPDATE flow_instances SET ref = :r, ref_n = :n WHERE id = :i"),
                {"r": _models.ref_text(prefix, used), "n": used, "i": run_id})
        if blank:
            done.append(f"{len(blank)} FMS submission(s) numbered {prefix}-…")

    # And the steps. Only where it is actually wrong, so a restart with
    # nothing to do reports nothing to do.
    if "tasks" not in existing_tables:
        return done
    tcols = {c["name"] for c in insp.get_columns("tasks")}
    if "ref" not in tcols or "flow_instance_id" not in tcols:
        return done

    moved = conn.execute(text(
        "UPDATE tasks SET ref = (SELECT ref FROM flow_instances "
        "                        WHERE flow_instances.id = tasks.flow_instance_id), "
        "                 ref_n = (SELECT ref_n FROM flow_instances "
        "                          WHERE flow_instances.id = tasks.flow_instance_id) "
        "WHERE flow_instance_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM flow_instances "
        "            WHERE flow_instances.id = tasks.flow_instance_id "
        "            AND flow_instances.ref IS NOT NULL "
        "            AND (tasks.ref IS NULL OR tasks.ref <> flow_instances.ref))"))
    if moved.rowcount and moved.rowcount > 0:
        done.append(f"{moved.rowcount} FMS step(s) now carry their "
                    "submission's number")
    return done
