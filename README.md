# GCS Autopilot — Phase 1

A working clone of the core of MI Digital Autopilot (MIDAP): the **Flow
Management System** and **Delegation** engine, plus the auditing, recurring-task
and scoring machinery that sits on top of them.

Built for GCS internally, but every table carries `org_id`, so the same codebase
becomes a multi-tenant product later without a rewrite.

---

## Run it

```bash
pip install -r requirements.txt
python seed.py --reset          # demo org, 5 companies, 13 users, 3 flows
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```

Demo logins (password `gcs1234`):

| Email | Role | Sees |
|---|---|---|
| `cmd@gcs.local` | owner | everything, all branches |
| `mis@gcs.local` | admin | everything + user/flow config |
| `bz.manager@gcs.local` | manager | Bodyzone only, can delegate & audit |
| `meena@gcs.local` | doer + auditor rights | all branches, can audit and flag false marking, cannot delegate |
| `amit@gcs.local` | doer | only his own tasks |

Companies seeded: Bodyzone Fitness & Spa, Spa Kora, BIPS School, GCS Jharkhand,
GCS HO.

Run the test suite (78 checks covering auth, roles and rights, the full task
lifecycle, the flow engine, the scoring maths, false marking and delete
guards):

```bash
python smoke_test.py
```

---

## What's built

**Delegation** — assign a task with priority, deadline, attachments and a
notes thread. The doer accepts, works, submits. Optionally a manager must
audit and score it (0–10) before it closes; rejecting sends it back with a
remark and re-notifies the doer.

**Flow Management System** — define a workflow once as an ordered list of
steps (title, default doer, TAT in hours, priority, fields to capture,
audit-required flag). Start a *run* against a reference (a member, a lead, an
invoice) and step 1's task is created automatically. Close it and step 2
spawns by itself, carrying the captured field values forward in the instance
context (MIDAP's "Split FMS" carryover). Progress bars show where every run is.

Three flows are seeded: New PT Member Onboarding (5 steps), BIPS Admission
Enquiry (4 steps), Spa Membership Renewal (3 steps).

**Recurring tasks** — daily / weekdays / weekly / monthly rules with a due
time. `run_spawn()` is idempotent per day, runs on app start, and can be
triggered from a cron. Six GCS rules are seeded (daily sales MIS, face-ID
report, spa occupancy, weekly PT review, monthly machine audit, BIPS lead
audit).

**Scoring & Performance** — every score starts at 100 and is pulled down by
three separate penalties, each shown in its own column so the cause is visible:

| Penalty | Formula | Example |
|---|---|---|
| Work not done | −(not completed ÷ planned × 100) | 100 planned, 50 done → **−50** |
| Done late | −(closed late ÷ completed × 100) | 50 closed, 25 late → **−50** |
| False marking | −10 per flagged task | 2 flags → **−20** |

Net score = 100 minus all three, floored at 0. The **gap** is `score − 100`,
so a score of 79 shows as **−21**.

Two date bases are kept separate: *planned* counts tasks whose DUE date falls
in the window (the denominator), *closed in window* counts tasks whose
COMPLETION date falls in it — so clearing old backlog still shows up.

The Performance page filters by company/branch and a from/to date range, and
the live score cards refresh themselves every 20 seconds from `/api/stats`
without a page reload.

**Audit status** — every task, whatever it came from, carries one of three
audit states: **Not required**, **Audit pending**, **Audit completed**. It is
shown as a chip on the task list and the task page, and anyone with the audit
right can click it to change it, with a remark. A remark is compulsory before
an audit can be marked completed — an audit with no finding recorded is not an
audit. Every change is written into the task's notes with who changed it and
when. Flagging a task for audit at creation starts it at *Audit pending*;
reopening or false-marking a task puts it back there. A dedicated **Audit
pending** view lists everything still waiting across Delegation, Checklist and
FMS.

**Attachments — upload or paste** — the doer can pick a file, drag one in, or
just press Ctrl+V. Most proof here is a screenshot, and pasting turns "save
it, find it, upload it" into one keystroke. A clipboard image arrives with no
filename, so it is named `screenshot-YYYYMMDD-HHMMSS.png` automatically.
Pasting works from anywhere on the task page, including from inside the
completion-note box; pasting *text* into a text box still behaves normally.

**Help Desk** — one employee asks another for help without going through a
manager. The request immediately becomes a Delegation task on the helper's
dashboard, so it is tracked, attachable and counted in the scores like any
other work. The helper can decline with a reason, which cancels that task and
shows the reason to both of them. Cancelled tasks are excluded from scoring
entirely, so declining costs the helper nothing.

**Notification queue** — every assignment, rejection and flow handover writes
a formatted WhatsApp message to `outbound_messages` with `status='queued'`.
Nothing sends yet; see below.

**Roles** — owner → admin → manager → doer, enforced at the route level *and*
in the query layer (a doer's task list is filtered to their own rows, a
manager's to their branch).

---

## Layout

```
app/
  models.py            all tables, org_id everywhere; Role + Right enums
  migrate.py           adds new columns to an existing db on startup
  security.py          pbkdf2 password hashing (no external dep)
  deps.py              session auth + role guards + row-level visibility
  templating.py        Jinja env, date filters
  routers/
    auth.py            login / logout
    dashboard.py       doer dashboard, /stats, recurring rules, outbox
    tasks.py           delegation, lifecycle, audit status, notes
    attachments.py     upload tickets, pasted screenshots, downloads
    help.py            Help Desk — a request becomes a delegation task
    flows.py           flow templates, start a run, instance view
    admin.py           users, rights, companies
  services/
    flows.py           start_flow / advance_flow — the FMS engine
    recurring.py       schedule matching + idempotent spawner
    scoring.py         penalties, net score, date-window resolution
    notify.py          message templates + queue writer
  templates/, static/
seed.py                GCS demo data
smoke_test.py          239 end-to-end checks
```

---

## Moving to production

**Schema changes** — `app/migrate.py` runs on startup and adds any missing
column to an existing database, so upgrading never loses data. It is a
stopgap: swap it for Alembic once columns start being renamed or dropped.

**Postgres** — set `DATABASE_URL=postgresql+psycopg://user:pass@host/db` and
uncomment `psycopg` in requirements. Nothing else changes; the model layer is
dialect-neutral.

**WhatsApp** — write one adapter that drains `outbound_messages`:

```python
def drain(db):
    for m in db.scalars(select(OutboundMessage).where(OutboundMessage.status=="queued")):
        provider.send(m.to_phone, m.body)   # Gupshup / WATI / Meta Cloud API
        m.status, m.sent_at = "sent", datetime.utcnow()
    db.commit()
```
Note that Meta requires pre-approved templates for business-initiated
messages — the `TEMPLATES` dict in `services/notify.py` is already shaped for
that, so registering them is a paperwork step, not a code change.

**Scheduler** — run `recurring.run_spawn(db)` and the message drain every 5
minutes (APScheduler in-process, or a cron hitting an internal endpoint). Add
a due-soon / overdue reminder sweep at the same time using the
`task_due_soon` and `task_overdue` templates already defined.

**Deploy** — `uvicorn app.main:app --host 0.0.0.0 --port 8000` behind nginx,
or gunicorn with uvicorn workers. Set `MIDAP_SECRET` to a real random value;
the default is a placeholder.

---

## Deliberately not built yet

Phase 1 stops at the operations core. Left for later, roughly in the order
they'd pay off for GCS:

1. **Attendance** — geo + photo punch-in, branch-wise, leave register with
   buddy re-assignment.
2. **KRA/KPI** — sales targets per person with verified achievement.
3. **Google Sheets trigger** — watch a sheet, fire a task or message on a
   condition. This is closest to your existing Apps Script work and would
   plug straight into the report pipelines you already run.
4. **Customer-facing "Amazon model"** — progress notifications to the member
   as their flow advances.
5. **Inventory**, **help tickets**, **project management**, **hiring flow**.
6. **Lead-source integrations** (IndiaMART / RUNO equivalents).
