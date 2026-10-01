"""MIDAP-style data model.

Every row that belongs to a customer carries `org_id` so the same codebase can
run GCS internally today and serve multiple tenants later without a rewrite.
"""
from __future__ import annotations

import enum
from datetime import datetime, date

from sqlalchemy import (
    String, Integer, DateTime, Date, Boolean, ForeignKey, Text, Float, Enum,
    LargeBinary, UniqueConstraint, event, select, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm import Session as SASession

from . import clock
from .db import Base


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class Role(str, enum.Enum):
    OWNER = "owner"        # CMD level - sees everything across branches
    ADMIN = "admin"        # can configure flows, users, branches
    MANAGER = "manager"    # can delegate and audit within their department
    DOER = "doer"          # executes assigned tasks


class Priority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# How much each priority counts for in the score. A HIGH task is worth five
# ordinary tasks, MEDIUM two, LOW one. The doer still sees one task on their
# list — the weight only changes what it is worth, never how many rows appear.
PRIORITY_WEIGHT = {
    Priority.HIGH: 5,
    Priority.MEDIUM: 2,
    Priority.LOW: 1,
}
PRIORITY_ORDER = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}


def weight_of(priority: "Priority") -> int:
    return PRIORITY_WEIGHT.get(priority, 1)


# Stored as plain text rather than a native PostgreSQL enum type. A native
# enum is a schema object in its own right: renaming a level then means
# ALTER TYPE, which cannot be done and used in the same transaction, and a
# live upgrade dies on "invalid input value for enum priority". Text costs
# nothing here and lets the levels change with an ordinary UPDATE.
PriorityCol = Enum(Priority, native_enum=False, length=20,
                   values_callable=lambda e: [m.name for m in e])


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"     # doer marked done, waiting on audit
    COMPLETED = "completed"
    REJECTED = "rejected"       # auditor sent it back
    REOPENED = "reopened"       # auditor pulled a closed task back open
    ON_HOLD = "on_hold"         # its FMS run is paused — nobody's problem yet
    CANCELLED = "cancelled"


# Work that is not on anybody's plate right now. A cancelled task was
# un-planned; a held one is frozen while its FMS run is paused. Neither may
# count as "not done" in a score, sit on the follow-up desk, or turn red as
# overdue — in each case it would punish somebody for a decision that was
# taken above them. Everywhere one of those questions is asked, this is the
# set to ask it about.
PARKED_STATES = (TaskStatus.CANCELLED, TaskStatus.ON_HOLD)


class AuditState(str, enum.Enum):
    """Where a task stands with the auditor, shown on every task.

    Kept separate from TaskStatus on purpose: a task can be COMPLETED and still
    be waiting for someone to audit it, and the doer's dashboard needs to show
    both facts at once.

    WAITING is the state that was missing. A task flagged for audit used to be
    "Audit pending" from the moment it was created, so the auditor's list was
    full of work nobody had started — and it was possible to audit a job that
    had not been done. Nothing is pending until the doer finishes it.
    """
    NOT_REQUIRED = "not_required"
    WAITING = "waiting"          # needs an audit, but the work is not done yet
    PENDING = "pending"          # finished, and now genuinely waiting on the auditor
    COMPLETED = "completed"


AUDIT_LABELS = {
    AuditState.NOT_REQUIRED: "Not required",
    AuditState.WAITING: "Audit after it's done",
    AuditState.PENDING: "Audit pending",
    AuditState.COMPLETED: "Audit completed",
}

# The states an auditor can actually set by hand. WAITING is not among them:
# it is what the software puts a task in until the doer finishes, and setting
# it from the outside would only be a way to hide finished work.
AUDIT_SETTABLE = [AuditState.PENDING, AuditState.COMPLETED,
                  AuditState.NOT_REQUIRED]


class HelpStatus(str, enum.Enum):
    """A help ticket one employee raises with another."""
    OPEN = "open"           # delegation task created, helper hasn't finished
    DECLINED = "declined"   # helper can't take it; the task is cancelled
    CLOSED = "closed"       # the delegation task was completed


class Right(str, enum.Enum):
    """Fine-grained rights, granted per user when the account is created.

    These sit *on top of* the role. A doer with EDIT_TASK can edit tasks they
    can already see; it never widens which rows they can see.
    """
    CREATE_TASK = "create_task"     # delegate work to others
    EDIT_TASK = "edit_task"         # change title/details/due/priority/doer
    DELETE_TASK = "delete_task"     # remove a task entirely
    AUDIT_TASK = "audit_task"       # approve / send back a submission
    REOPEN_TASK = "reopen_task"     # pull a closed task back open
    FALSE_MARK = "false_mark"       # flag a bogus completion (-10 to the doer)
    MANAGE_FLOW = "manage_flow"     # create / edit FMS templates
    MANAGE_USER = "manage_user"     # create users, set rights
    VIEW_ALL_BRANCHES = "view_all_branches"
    # Who chases the work that has not been done. PC covers Checklist + FMS,
    # EA covers Delegation. Kept as rights rather than a fixed job title so a
    # stand-in can be given it for a week without inventing a new role.
    FOLLOWUP_CHECKLIST_FMS = "followup_checklist_fms"   # the PC
    FOLLOWUP_DELEGATION = "followup_delegation"         # the EA
    # Reports are open to everybody, but without this right they only ever
    # show a person their OWN work. Tick it and the same pages cover the
    # whole company — including other people's EM scores. It is the one
    # right here that widens what someone can SEE rather than what they can
    # do, which is why it is not handed out with any role by default.
    VIEW_ALL_REPORTS = "view_all_reports"
    # Moving a deadline changes whether the work was late, which is half of
    # every EM score. Kept as its own right so it can be held by the few
    # people answerable for the numbers rather than by anyone who may edit
    # a task's wording.
    CHANGE_DUE_DATE = "change_due_date"


RIGHT_LABELS = {
    Right.CREATE_TASK: "Delegate tasks",
    Right.EDIT_TASK: "Edit tasks",
    Right.DELETE_TASK: "Delete tasks",
    Right.AUDIT_TASK: "Audit submissions",
    Right.REOPEN_TASK: "Reopen closed tasks",
    Right.FALSE_MARK: "Flag false marking (−10)",
    Right.MANAGE_FLOW: "Manage FMS flows",
    Right.MANAGE_USER: "Manage users & rights",
    Right.VIEW_ALL_BRANCHES: "See all branches",
    Right.FOLLOWUP_CHECKLIST_FMS: "Follow up Checklist & FMS (PC)",
    Right.FOLLOWUP_DELEGATION: "Follow up Delegation (EA)",
    Right.VIEW_ALL_REPORTS: "See everyone's reports",
    Right.CHANGE_DUE_DATE: "Change a task's planned date",
}

# What each role gets by default when a user is created.
DEFAULT_RIGHTS = {
    Role.OWNER: [r for r in Right],
    Role.ADMIN: [r for r in Right],
    # Deliberately NOT VIEW_ALL_REPORTS: a manager's reports stay inside the
    # branch they run until somebody decides otherwise. Handing it out with
    # the role would quietly widen what every existing manager can see.
    Role.MANAGER: [Right.CREATE_TASK, Right.EDIT_TASK, Right.AUDIT_TASK,
                   Right.REOPEN_TASK, Right.FALSE_MARK],
    Role.DOER: [],
}


class TaskSource(str, enum.Enum):
    DELEGATION = "delegation"   # one-off task assigned by a person
    RECURRING = "recurring"     # spawned by a recurrence rule
    FLOW = "flow"               # a step inside a running flow instance


# The short name a person says out loud: "DEL-14 is still open", "who has
# CL-07". Each kind of work counts on its own, so the numbers stay small and
# the prefix already tells you where to look for it.
REF_PREFIX = {
    TaskSource.DELEGATION: "DEL",
    TaskSource.RECURRING: "CL",
    TaskSource.FLOW: "FMS",
}


def ref_text(prefix: str, n: int) -> str:
    """DEL-01 … DEL-99, then DEL-100. Two digits is what people expect to
    read; past ninety-nine it simply grows rather than wrapping."""
    return f"{prefix}-{n:02d}"


class Recurrence(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"     # every second week, on chosen weekdays
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"         # every third month
    WEEKDAYS = "weekdays"
    # Once a year: domain renewals, licences, subscriptions. day_of holds the
    # month and day together as MMDD — 417 is 17 April, 1231 is 31 December —
    # because a yearly rule needs both and the rule has only one column for it.
    YEARLY = "yearly"


FREQ_LABELS = {
    Recurrence.DAILY: "Daily",
    Recurrence.WEEKLY: "Weekly",
    Recurrence.FORTNIGHTLY: "Fortnightly",
    Recurrence.MONTHLY: "Monthly",
    Recurrence.QUARTERLY: "Quarterly",
    Recurrence.YEARLY: "Yearly",
    Recurrence.WEEKDAYS: "Weekly",     # only ever seen on an older rule
}

# The six offered, most often first.
#
# Weekdays is deliberately not among them. "Monday to Friday" is Weekly with
# five days ticked, and offering it twice made people choose between two
# names for one thing — then wonder why one of them would not let them add
# Saturday. Rules already saved as Weekdays keep working and are converted on
# the next upgrade.
FREQ_ORDER = [Recurrence.DAILY, Recurrence.WEEKLY, Recurrence.FORTNIGHTLY,
              Recurrence.MONTHLY, Recurrence.QUARTERLY, Recurrence.YEARLY]


class MonthMode(str, enum.Enum):
    """The two ways people say when in the month something happens.

    "the 5th of every month" and "the first Saturday of every month" are both
    monthly, and neither can be written as the other: the 5th moves around the
    week, and the first Saturday moves around the dates.
    """
    DATE = "date"           # day_of holds 1-31
    WEEKDAY = "weekday"     # week_of_month + weekdays, e.g. 1 + Sat



# --------------------------------------------------------------------------
# Tenant / org structure
# --------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    branches: Mapped[list["Branch"]] = relationship(back_populates="org")
    users: Mapped[list["User"]] = relationship(back_populates="org")


class Branch(Base):
    """A vertical / location. For GCS: Bodyzone, Spa Kora, BIPS."""
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # The day of the week this company is closed, 0=Monday … 6=Sunday, or
    # NULL for a company that never closes.
    #
    # It is per company on purpose. The offices keep Sunday off, but the gym
    # and the spa are at their busiest on a Sunday — a group-wide "Sunday is
    # off" would quietly take every Sunday job off those two rotas.
    weekly_off: Mapped[int | None] = mapped_column(Integer, nullable=True,
                                                   default=6)

    org: Mapped[Organization] = relationship(back_populates="branches")


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))

    branch: Mapped["Branch | None"] = relationship(lazy="joined")

    @property
    def label(self) -> str:
        """Name plus branch — 'Front Desk' exists at three of them, so the
        name alone is ambiguous in a dropdown."""
        return f"{self.name} — {self.branch.name}" if self.branch else \
               f"{self.name} — all branches"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)

    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)  # WhatsApp target
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.DOER)
    # comma-separated Right values, e.g. "edit_task,delete_task"
    rights: Mapped[str] = mapped_column(Text, default="")

    # Scoring benchmark: how this person's work is expected to split across the
    # three sources. Must total 100. The weight caps how much each source can
    # cost them, so a delegation-heavy doer isn't punished equally for a
    # checklist slip. Half the weight goes to "not done", half to "not on time".
    bm_delegation: Mapped[int] = mapped_column(Integer, default=60)
    bm_checklist: Mapped[int] = mapped_column(Integer, default=20)
    bm_fms: Mapped[int] = mapped_column(Integer, default=20)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    org: Mapped[Organization] = relationship(back_populates="users")
    branch: Mapped[Branch | None] = relationship()
    department: Mapped[Department | None] = relationship()

    @property
    def right_set(self) -> set[str]:
        return {r for r in (self.rights or "").split(",") if r}

    def has(self, right: "Right") -> bool:
        """Owner and admin always hold every right; others need it granted."""
        if self.role in (Role.OWNER, Role.ADMIN):
            return True
        return right.value in self.right_set

    def set_rights(self, rights) -> None:
        self.rights = ",".join(sorted({
            r.value if isinstance(r, Right) else str(r) for r in rights
        }))

    @property
    def benchmarks(self) -> dict[str, int]:
        """TaskSource value -> benchmark percentage for this doer."""
        return {
            TaskSource.DELEGATION.value: self.bm_delegation,
            TaskSource.RECURRING.value: self.bm_checklist,
            TaskSource.FLOW.value: self.bm_fms,
        }

    @property
    def benchmark_total(self) -> int:
        return self.bm_delegation + self.bm_checklist + self.bm_fms

    def set_benchmarks(self, delegation: int, checklist: int, fms: int) -> None:
        """The three shares of this person's job the software scores.

        They no longer have to total 100. Part of an EM score is judged by
        hand — attitude, quality of a conversation, things no task list
        sees — so a split of 40/10/10 is a deliberate statement that the
        software accounts for 60 points and a person decides the other 40.

        What each number still means is unchanged: a benchmark is the most
        that kind of work can cost, so the software can never take away more
        than the three added together. That figure is `scored_by_system`,
        and the pages show it so nobody mistakes "60" for a full mark.
        """
        if min(delegation, checklist, fms) < 0:
            raise ValueError("A benchmark cannot be negative.")
        total = delegation + checklist + fms
        if total > 100:
            raise ValueError(
                f"The three benchmarks add up to {total}%, which is more than "
                "100. Together they are the most the software can deduct, so "
                "they cannot exceed the whole score."
            )
        self.bm_delegation, self.bm_checklist, self.bm_fms = delegation, checklist, fms

    @property
    def scored_by_system(self) -> int:
        """How many of the 100 points the software decides. The rest is manual."""
        return self.benchmark_total

    @property
    def scored_by_hand(self) -> int:
        return max(0, 100 - self.benchmark_total)

    # --- shorthands the templates use, so views stay free of role literals ---
    @property
    def receives_work(self) -> bool:
        """Whether this account is ever on the receiving end of a task.

        There is one assigner in this company and it is the admin account.
        Owner and admin hand work out; nobody hands work to them. Their
        "Assigned to me" list and their own EM score are therefore
        permanently empty, and an empty section at the top of the page is
        the first thing they see every morning.

        Pages that can afford a query check the tasks table as well, so an
        admin who genuinely has been given something still sees it.
        """
        return self.role not in (Role.OWNER, Role.ADMIN)

    @property
    def can_manage(self) -> bool:
        """Sees the Operations / Insight sections of the menu."""
        return self.role in (Role.OWNER, Role.ADMIN, Role.MANAGER)

    @property
    def has_create_task(self) -> bool:
        return self.has(Right.CREATE_TASK)

    @property
    def has_manage_flow(self) -> bool:
        return self.has(Right.MANAGE_FLOW)

    @property
    def has_manage_user(self) -> bool:
        return self.has(Right.MANAGE_USER)

    @property
    def has_edit_task(self) -> bool:
        return self.has(Right.EDIT_TASK)

    @property
    def can_see_all_reports(self) -> bool:
        """May open the company-wide reports (EM score, follow-ups)."""
        return self.can_manage or self.has(Right.VIEW_ALL_REPORTS)

    @property
    def report_scope(self) -> str:
        """How wide the reports actually are for this person.

        'all'    — the whole company
        'branch' — a manager, limited to the branch they run
        'self'   — an ordinary doer: their own work and nothing else

        Kept as one property so the pages can say plainly whose figures are
        on screen. A report that looks company-wide but is not is how people
        end up quoting their own three tasks at a review.
        """
        if (self.role in (Role.OWNER, Role.ADMIN)
                or self.has(Right.VIEW_ALL_REPORTS)
                or self.has(Right.VIEW_ALL_BRANCHES)):
            return "all"
        return "branch" if self.role == Role.MANAGER else "self"

    @property
    def can_follow_up(self) -> bool:
        """PC or EA — shows the Follow-ups menu to a plain doer who chases."""
        return (Right.FOLLOWUP_DELEGATION.value in self.right_set
                or Right.FOLLOWUP_CHECKLIST_FMS.value in self.right_set)


# --------------------------------------------------------------------------
# Flow Management System
# --------------------------------------------------------------------------
# What a start-form question can be. Deliberately short: every one of these
# is obvious to fill in on a phone, and a form nobody can fill in quickly is
# a form people work around.
FIELD_TYPES = {
    "text": "Short text",
    "textarea": "Long text",
    "number": "Number",
    "date": "Date",
    "select": "Choose from a list",
    "yesno": "Yes / No",
}


class Flow(Base):
    """A reusable workflow template, e.g. 'New PT Member Onboarding'."""
    __tablename__ = "flows"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    # Comma separated labels asked on the start form, on top of the reference.
    # Every flow needs different things to get going — a bill number here, a
    # member name there — so the flow itself carries its own question list.
    start_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The real start form: a JSON list of fields, each with a label, a type
    # and (for a dropdown) its choices. start_fields above was the first cut
    # — a comma-separated line of labels — and is still read for flows built
    # with it, so nothing made earlier loses its questions.
    start_form: Mapped[str | None] = mapped_column(Text, nullable=True)

    steps: Mapped[list["FlowStep"]] = relationship(
        back_populates="flow", order_by="FlowStep.position", cascade="all, delete-orphan"
    )
    branch: Mapped[Branch | None] = relationship()

    @property
    def start_field_list(self) -> list[str]:
        """Just the labels — what the older code and the tests still use."""
        return [f["label"] for f in self.start_form_fields]

    @property
    def start_form_fields(self) -> list[dict]:
        """The start form, as a list of {label, type, options, required}.

        A flow built before the form builder existed stored a comma-separated
        line instead; that is read as a list of plain text boxes, so those
        flows keep asking exactly what they always asked.
        """
        import json as _json
        if self.start_form:
            try:
                rows = _json.loads(self.start_form)
            except (ValueError, TypeError):
                rows = []
            out = []
            for i, r in enumerate(rows if isinstance(rows, list) else []):
                if not isinstance(r, dict) or not (r.get("label") or "").strip():
                    continue
                out.append({
                    "key": f"sf{i}",
                    "label": r["label"].strip(),
                    "type": r.get("type") if r.get("type") in FIELD_TYPES else "text",
                    "options": [o for o in (r.get("options") or []) if str(o).strip()],
                    "required": bool(r.get("required", True)),
                })
            return out
        return [{"key": f"sf{i}", "label": f.strip(), "type": "text",
                 "options": [], "required": True}
                for i, f in enumerate((self.start_fields or "").split(","))
                if f.strip()]


class FlowStep(Base):
    __tablename__ = "flow_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    position: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # who does it: a fixed user, or left blank to be picked at run time
    default_doer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Turnaround time, as a unit and a number: 30 minutes, 2 days, 1 month.
    # tat_hours is the original column and is kept so older rows and the
    # bulk importer keep working; tat_unit/tat_value are what the engine
    # actually reads now.
    tat_hours: Mapped[int] = mapped_column(Integer, default=24)
    tat_unit: Mapped[str] = mapped_column(String(10), default="hours")
    tat_value: Mapped[int] = mapped_column(Integer, default=24)

    # Whose planned date this step's turnaround is measured from.
    #
    #   NULL     -> from the moment this step opens (the usual case)
    #   a number -> from THAT step's planned date
    #
    # The second one matters when a whole chain hangs off one date: "CMD
    # approval is due 2 days after the bill was due to be verified" should
    # not drift just because the earlier step was closed late.
    due_from_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[Priority] = mapped_column(PriorityCol, default=Priority.MEDIUM)
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    # comma separated field labels the doer must fill in on completion
    capture_fields: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Where the flow goes once this step is closed. Stored as a POSITION
    # within the flow rather than a step id, because a whole flow is built in
    # one form submission and the ids do not exist yet while it is being
    # filled in.
    #
    #   a number  -> jump to that step (which may be earlier: a rework loop)
    #   0         -> the flow is finished here
    #   NULL      -> the old behaviour, "whatever step comes next in order",
    #                so flows built before routing existed keep working
    next_step_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # A decision step ends in one of two outcomes, chosen by the person doing
    # it. Each outcome routes somewhere of its own — that is what lets
    # "rejected" go back to step 1 while "verified" carries on.
    is_decision: Mapped[bool] = mapped_column(Boolean, default=False)
    pass_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fail_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fail_step_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)

    flow: Mapped[Flow] = relationship(back_populates="steps")
    default_doer: Mapped[User | None] = relationship()

    @property
    def tat_label(self) -> str:
        return clock.span_label(self.tat_unit or "hours",
                                self.tat_value or self.tat_hours or 0)

    @property
    def yes_label(self) -> str:
        return (self.pass_label or "").strip() or "Approved"

    @property
    def no_label(self) -> str:
        return (self.fail_label or "").strip() or "Rejected"

    def route(self, outcome: str = "pass") -> int | None:
        """Which position to go to next. 0 means the flow ends here."""
        return self.fail_step_pos if (self.is_decision and outcome == "fail") \
            else self.next_step_pos


@event.listens_for(FlowStep, "before_insert")
def _step_tat_defaults(mapper, connection, target: "FlowStep") -> None:
    """Keep the unit and the legacy hour count telling the same story.

    A step can be built by the flow form (which sets unit + value), by the
    bulk importer or the seed (which set only tat_hours), or by a future
    caller that does neither. Rather than trusting every call site to
    remember, the two are reconciled once, here.
    """
    if not target.tat_unit:
        target.tat_unit = "hours"
    if target.tat_unit == "hours":
        # Whichever of the two was actually given wins; if both were, the
        # explicit value does.
        if target.tat_value in (None, 0):
            target.tat_value = target.tat_hours or 24
        elif target.tat_value != target.tat_hours:
            # Only one of them was meant: the form always sends both equal,
            # so a difference means tat_hours was set on its own.
            target.tat_value = target.tat_hours \
                if target.tat_value == 24 else target.tat_value
        target.tat_hours = target.tat_value
    else:
        target.tat_value = target.tat_value or 1


class FlowInstance(Base):
    """One live run of a flow, e.g. onboarding for member #1042."""
    __tablename__ = "flow_instances"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    reference: Mapped[str] = mapped_column(String(200))   # member name / lead id / invoice no
    started_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_position: Mapped[int] = mapped_column(Integer, default=1)
    # JSON blob of field values carried across steps (MIDAP "Split FMS" carryover)
    context: Mapped[str] = mapped_column(Text, default="{}")

    # --- paused, and stopped -----------------------------------------------
    # A run gets held when the thing it is about goes quiet: the vendor has
    # not sent the bill, the member is travelling, the school is on holiday.
    # Nobody should be marked late for a wait somebody else decided on, so a
    # held run's open steps step out of the doer's list and out of the score
    # until it is resumed.
    #
    # Stopping is different and final: the run is abandoned, its open steps
    # are cancelled, and it never moves again.
    held_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    held_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_days: Mapped[int] = mapped_column(Integer, default=0)   # total, across holds
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    flow: Mapped[Flow] = relationship()
    started_by: Mapped[User] = relationship(foreign_keys=[started_by_id])
    held_by: Mapped[User | None] = relationship(foreign_keys=[held_by_id])
    cancelled_by: Mapped[User | None] = relationship(foreign_keys=[cancelled_by_id])

    @property
    def on_hold(self) -> bool:
        return self.held_at is not None and self.completed_at is None \
            and self.cancelled_at is None

    @property
    def state(self) -> str:
        """One word for what this run is doing, used everywhere it is shown."""
        if self.cancelled_at:
            return "stopped"
        if self.completed_at:
            return "completed"
        if self.held_at:
            return "on hold"
        return "running"

    @property
    def is_live(self) -> bool:
        """Still moving — not finished, not stopped, not paused."""
        return (self.completed_at is None and self.cancelled_at is None
                and self.held_at is None)
    # Every task this run has spawned. Read-only: tasks are created by the
    # engine, never by appending here. Progress counts these rather than
    # positions, because a flow that loops back has no "position reached".
    tasks: Mapped[list["Task"]] = relationship(
        primaryjoin="FlowInstance.id == foreign(Task.flow_instance_id)",
        viewonly=True, order_by="Task.created_at")


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)

    # The human reference — DEL-01, CL-01, FMS-01. Numbered per company and
    # per kind of work, so the three series never interleave. Nullable only
    # so an older database can be upgraded in place; every task gets one.
    ref: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # The same number as an integer. Kept because "DEL-9" and "DEL-10" sort
    # the wrong way round as text, and because finding the next number is a
    # MAX() on this column rather than a string parse.
    ref_n: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    priority: Mapped[Priority] = mapped_column(PriorityCol, default=Priority.MEDIUM)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    source: Mapped[TaskSource] = mapped_column(Enum(TaskSource), default=TaskSource.DELEGATION)

    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # links back to origin
    flow_instance_id: Mapped[int | None] = mapped_column(ForeignKey("flow_instances.id"), nullable=True)
    flow_step_id: Mapped[int | None] = mapped_column(ForeignKey("flow_steps.id"), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_rules.id"), nullable=True)

    # audit
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    # Proof of the work, not just a claim that it was done. On by default
    # everywhere: a task closed with no evidence is the thing false-marking
    # exists to catch, and catching it after the fact is worse than
    # preventing it.
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    audit_state: Mapped[AuditState] = mapped_column(
        Enum(AuditState), default=AuditState.NOT_REQUIRED, index=True)
    audit_score: Mapped[float | None] = mapped_column(Float, nullable=True)   # 0-10 quality
    audit_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    auditor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    completion_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # On a decision step: which of the two outcomes the doer chose.
    # "pass" / "fail", or NULL on an ordinary step.
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # What this task was before its FMS run was put on hold, so resuming
    # puts it back exactly where it was rather than guessing "in progress"
    # and quietly erasing that an auditor had sent it back.
    held_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # For a checklist task: the calendar day this one is FOR. Normally the
    # same day it was created, but a job due on a Sunday is created on the
    # Saturday before, and then the two differ. It is also what stops the
    # same day's job being created twice.
    covers_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    captured_data: Mapped[str] = mapped_column(Text, default="{}")

    # false marking: auditor says the doer closed this without really doing it
    false_marked: Mapped[bool] = mapped_column(Boolean, default=False)
    false_marked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    false_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    false_mark_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # reopen tracking
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    reopened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    assigner: Mapped[User] = relationship(foreign_keys=[assigner_id])
    doer: Mapped[User] = relationship(foreign_keys=[doer_id])
    auditor: Mapped[User | None] = relationship(foreign_keys=[auditor_id])
    false_marked_by: Mapped[User | None] = relationship(foreign_keys=[false_marked_by_id])
    reopened_by: Mapped[User | None] = relationship(foreign_keys=[reopened_by_id])
    branch: Mapped[Branch | None] = relationship()
    flow_instance: Mapped[FlowInstance | None] = relationship()
    flow_step: Mapped[FlowStep | None] = relationship()
    comments: Mapped[list["TaskComment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskComment.created_at"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    @property
    def is_overdue(self) -> bool:
        if self.status in (TaskStatus.COMPLETED,) + PARKED_STATES:
            return False
        return clock.now() > self.due_at

    @property
    def was_on_time(self) -> bool | None:
        if not self.submitted_at:
            return None
        return self.submitted_at <= self.due_at

    @property
    def weight(self) -> int:
        """What this task is worth in the score — 5, 2 or 1."""
        return weight_of(self.priority)

    @property
    def weight_label(self) -> str:
        return f"{self.weight}\u00d7"

    @property
    def audit_label(self) -> str:
        return AUDIT_LABELS[self.audit_state]

    @property
    def audit_open(self) -> bool:
        """Genuinely on an auditor's desk right now."""
        return self.audit_state == AuditState.PENDING

    @property
    def audit_expected(self) -> bool:
        """Will need an audit, now or once it is finished."""
        return self.audit_state in (AuditState.WAITING, AuditState.PENDING)

    @property
    def doer_finished(self) -> bool:
        """The doer has said they are done — submitted, or closed outright."""
        return self.status in (TaskStatus.SUBMITTED, TaskStatus.COMPLETED)


@event.listens_for(Task, "before_insert")
def _on_task_created(mapper, connection, target: "Task") -> None:
    """Rules that must hold for every new task, whatever created it.

    Done here rather than at each creation site so delegation, checklist, FMS
    and the bulk importer all behave the same without four copies of the rule.
    """
    # Flagged for audit -> it WAITS. Nothing is pending on an auditor until
    # the person doing it says they have finished: an auditor cannot check
    # work that has not happened, and a list full of unstarted jobs is a list
    # nobody reads.
    if target.requires_audit and target.audit_state in (None, AuditState.NOT_REQUIRED):
        target.audit_state = AuditState.WAITING

    # A task is live the moment it is assigned. There is no "accept" step:
    # the deadline was set when the work was handed over, so the clock is
    # already running and asking the doer to press a button first only adds a
    # way to look busy without doing anything.
    if target.status in (None, TaskStatus.PENDING):
        target.status = TaskStatus.IN_PROGRESS
        target.started_at = target.started_at or clock.now()


@event.listens_for(SASession, "before_flush")
def _number_new_tasks(session: SASession, flush_context, instances) -> None:
    """Give every new task its DEL-/CL-/FMS- number.

    Done on the session rather than at each creation site because there are
    five of them — the delegate form, the bulk importer, the checklist
    spawner, the FMS spawner and the seed — and a task without a reference
    is a task nobody can quote in a WhatsApp message.

    The next number is MAX(ref_n) + 1 for that company and that kind of work.
    A batch created together (a 300-row bulk import) is numbered in one pass
    here, in the order the rows were read, so the spreadsheet's order is the
    order of the references.
    """
    fresh = [o for o in session.new
             if isinstance(o, Task) and not o.ref and o.org_id]
    if not fresh:
        return

    groups: dict[tuple[int, TaskSource], list[Task]] = {}
    for t in fresh:
        groups.setdefault((t.org_id, t.source or TaskSource.DELEGATION), []).append(t)

    for (org_id, src), items in groups.items():
        nxt = (session.execute(
            select(func.max(Task.ref_n))
            .where(Task.org_id == org_id, Task.source == src)).scalar() or 0) + 1
        prefix = REF_PREFIX.get(src, "TSK")
        for t in items:
            t.ref_n = nxt
            t.ref = ref_text(prefix, nxt)
            nxt += 1


class TaskComment(Base):
    """MIDAP 'Notes' - a chat thread hanging off each task."""
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    task: Mapped[Task] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255))
    # object key in the bucket, or the file name on disk
    stored_name: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(120), default="")
    storage: Mapped[str] = mapped_column(String(10), default="local")  # s3 | local | db
    # Used only when storage == "db": the file lives in the database itself.
    # That is how a free host with no permanent disk keeps attachments across
    # restarts without a separate storage account.
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    task: Mapped[Task] = relationship(back_populates="attachments")
    uploaded_by: Mapped[User] = relationship()

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def size_label(self) -> str:
        kb = self.size / 1024
        return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


class RecurringRule(Base):
    __tablename__ = "recurring_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    doer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[Priority] = mapped_column(PriorityCol, default=Priority.MEDIUM)
    frequency: Mapped[Recurrence] = mapped_column(Enum(Recurrence), default=Recurrence.DAILY)
    # The original single-value column, kept because every rule ever made
    # uses it and nothing should have to be re-entered:
    #   monthly / quarterly by date -> the day of the month, 1-31
    #   yearly                      -> month and day as MMDD
    #   weekly (rules made before several days were allowed) -> 0=Mon..6=Sun
    day_of: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- everything a schedule needs that one number cannot hold -----------
    # Which weekdays, as "0,3" for Monday and Thursday. Used by weekly and
    # fortnightly, and by monthly/quarterly in "first Saturday" mode. A rule
    # can name several: the report that goes out on Monday AND Thursday is one
    # job, not two, and splitting it into two rules splits its score too.
    weekdays: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Several dates in the same month — "the 15th and the 30th", which is how
    # salaries, EMIs and utility bills are actually scheduled. It is NOT
    # every-two-weeks: a fortnightly rule drifts through the month and fires
    # twenty-six times a year, this fires exactly twenty-four.
    month_days: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # Which weeks of the month, for "the first Saturday" — "1", or "1,3" for
    # the first AND third, or "-1" for the last, which is not the same as the
    # fourth in a month with five of them. A list for the same reason as the
    # weekdays: "first and third Saturday" is one job on a rota, and two rules
    # would split its score in two.
    weeks_of_month: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Quarterly: the first month of the cycle, 1-12. January means Jan, Apr,
    # Jul, Oct; February means Feb, May, Aug, Nov. Without it "quarterly"
    # would silently assume the calendar quarter, which is wrong for anyone
    # whose year starts in April.
    start_month: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Fortnightly: a date in a week the job IS due, which is what decides
    # which of the two weeks is the on week. Every-two-weeks means nothing
    # without it.
    anchor_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_time: Mapped[str] = mapped_column(String(5), default="23:59")   # HH:MM local
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_spawned_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    doer: Mapped[User] = relationship(foreign_keys=[doer_id])
    assigner: Mapped[User] = relationship(foreign_keys=[assigner_id])

    # ---------------------------------------------------------- schedule ---
    WEEK_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"]
    WEEK_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November",
                   "December"]
    NTH_NAMES = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}

    @property
    def weekday_list(self) -> list[int]:
        """The weekdays this rule runs on, 0=Monday.

        Reads the newer several-days column, and falls back to the single
        day_of that every rule made before it used — so an old weekly rule
        keeps running on exactly the day it always did.
        """
        if self.weekdays:
            out = []
            for part in str(self.weekdays).split(","):
                part = part.strip()
                if part.isdigit() and 0 <= int(part) <= 6:
                    out.append(int(part))
            if out:
                return sorted(set(out))
        if self.frequency in (Recurrence.WEEKLY, Recurrence.FORTNIGHTLY):
            return [self.day_of if self.day_of is not None else 0]
        if self.month_mode == MonthMode.WEEKDAY:
            return [0]
        return []

    @property
    def month_day_list(self) -> list[int]:
        """The dates in the month this rule runs on — [15, 30], or [10]."""
        out = []
        for part in str(self.month_days or "").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 31:
                out.append(int(part))
        if out:
            return sorted(set(out))
        return [self.day_of] if self.day_of else []

    @property
    def week_list(self) -> list[int]:
        """Which weeks of the month — [1, 3], or [-1] for the last one."""
        out = []
        for part in str(self.weeks_of_month or "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit() and int(part) in (1, 2, 3, 4, 5, -1):
                out.append(int(part))
        return sorted(set(out), key=lambda n: (n < 0, n))

    @property
    def month_mode(self) -> "MonthMode":
        """Whether a monthly or quarterly rule counts dates or weekdays."""
        return MonthMode.WEEKDAY if self.week_list else MonthMode.DATE

    @property
    def weekday_words(self) -> str:
        """"Monday", "Monday and Thursday", "Monday to Friday"."""
        days = self.weekday_list
        if not days:
            return ""
        # A run of consecutive days reads as a range. "Mon, Tue, Wed, Thu and
        # Fri" is five words for the thing everybody calls the working week.
        if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
            return f"{self.WEEK_NAMES[days[0]]} to {self.WEEK_NAMES[days[-1]]}"
        names = [self.WEEK_NAMES[d] if len(days) <= 2 else self.WEEK_SHORT[d]
                 for d in days]
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]

    @property
    def schedule_label(self) -> str:
        """How this rule reads in a sentence — one wording, used everywhere."""
        f = self.frequency
        if f == Recurrence.DAILY:
            return "Every day"
        if f == Recurrence.WEEKDAYS:
            return "Every Monday to Friday"
        if f == Recurrence.WEEKLY:
            return f"Every {self.weekday_words}"
        if f == Recurrence.FORTNIGHTLY:
            every = f"Every second {self.weekday_words}"
            return (f"{every}, counting from {self.anchor_on:%d %b %Y}"
                    if self.anchor_on else every)
        if f in (Recurrence.MONTHLY, Recurrence.QUARTERLY):
            if self.month_mode == MonthMode.WEEKDAY:
                names = [self.NTH_NAMES.get(n, str(n)) for n in self.week_list]
                nth = (names[0] if len(names) == 1
                       else ", ".join(names[:-1]) + " and " + names[-1])
                when = f"The {nth} {self.weekday_words}"
            else:
                dates = self.month_day_list or [1]
                if len(dates) == 1:
                    when = f"Day {dates[0]}"
                else:
                    shown = [str(d) for d in dates]
                    when = ("Days " + ", ".join(shown[:-1]) + " and " + shown[-1])
            if f == Recurrence.MONTHLY:
                return f"{when} of every month"
            # Quarterly reads as the four dates themselves. "Every third
            # month" makes people count on their fingers; "5 Feb, May, Aug
            # and Nov" is the answer they were counting towards.
            names = [self.MONTH_NAMES[m][:3] for m in self.quarter_months]
            if self.month_mode == MonthMode.DATE:
                dates = self.month_day_list or [1]
                return (f"{dates[0]} {', '.join(names[:-1])} and {names[-1]}, "
                        "every year")
            return (f"{when} of {', '.join(names[:-1])} and {names[-1]}")
        if f == Recurrence.YEARLY:
            month, dom = divmod(self.day_of or 101, 100)
            if 1 <= month <= 12:
                return f"Every year on {dom} {self.MONTH_NAMES[month][:3]}"
            return "Once a year"
        return f.value.title()

    @property
    def quarter_months(self) -> list[int]:
        """The four months a quarterly rule runs in, from the one chosen.

        In cycle order rather than sorted, so a rule starting in November
        reads "Nov, Feb, May and Aug" — the order it will actually happen in,
        not January first because January sorts first.
        """
        start = self.start_month if self.start_month in range(1, 13) else 1
        return [((start - 1 + n * 3) % 12) + 1 for n in range(4)]


class HelpTicket(Base):
    """One employee asking another for help.

    Raising a ticket creates the Delegation task immediately — the whole point
    is that asking for help *is* delegating, without a manager in the middle.
    The helper can decline, which cancels that task and tells the raiser why.
    """
    __tablename__ = "help_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    raiser_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    helper_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)

    subject: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[Priority] = mapped_column(PriorityCol, default=Priority.MEDIUM)
    needed_by: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[HelpStatus] = mapped_column(Enum(HelpStatus), default=HelpStatus.OPEN,
                                               index=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    raiser: Mapped[User] = relationship(foreign_keys=[raiser_id])
    helper: Mapped[User] = relationship(foreign_keys=[helper_id])
    task: Mapped[Task | None] = relationship()


class AiVerdict(str, enum.Enum):
    """What the AI thought of the proof attached to a finished task."""
    OK = "ok"                 # the proof shows the job described
    WEAK = "weak"             # something was attached, but it proves little
    UNRELATED = "unrelated"   # the proof is of something else entirely
    NO_PROOF = "no_proof"     # nothing attached, or only the word "done"
    ERROR = "error"           # could not be checked — not a judgement


AI_VERDICT_LABELS = {
    AiVerdict.OK: "Proof looks right",
    AiVerdict.WEAK: "Proof is thin",
    AiVerdict.UNRELATED: "Proof does not match",
    AiVerdict.NO_PROOF: "No real proof",
    AiVerdict.ERROR: "Could not check",
}

# The two that deserve a human's attention before the task is signed off.
AI_SUSPECT = (AiVerdict.UNRELATED, AiVerdict.NO_PROOF)


class AiAudit(Base):
    """One AI reading of one finished task.

    Advisory, and only advisory. Nothing here changes a task's state, closes
    it, reopens it or moves anybody's score — it is a second pair of eyes
    that writes down what it saw, and a person still decides. An AI quietly
    costing somebody marks is how a scoring system loses the trust that
    makes it worth having.

    Kept as its own table rather than columns on the task so a task can be
    re-checked later without losing what was said the first time, and so
    turning the whole feature off leaves the tasks table untouched.
    """
    __tablename__ = "ai_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)

    verdict: Mapped[AiVerdict] = mapped_column(
        Enum(AiVerdict), default=AiVerdict.ERROR, index=True)
    # 0-100. The model's own reading of how sure it is, which is worth
    # showing: "does not match, 55% sure" is a different instruction to a
    # human from "does not match, 95% sure".
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str] = mapped_column(Text, default="")
    # What it actually had in front of it, in plain words, so nobody has to
    # wonder whether it saw the screenshot or only its file name.
    looked_at: Mapped[str] = mapped_column(Text, default="")

    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    images_seen: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(60), default="")
    took_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now,
                                                 index=True)

    task: Mapped[Task] = relationship()

    @property
    def label(self) -> str:
        return AI_VERDICT_LABELS.get(self.verdict, "Checked")

    @property
    def is_suspect(self) -> bool:
        return self.verdict in AI_SUSPECT

    @property
    def is_judgement(self) -> bool:
        """False when the check failed — an error is not an accusation."""
        return self.verdict != AiVerdict.ERROR


class Holiday(Base):
    """A day on which no work is expected.

    A holiday with no branch applies to the whole group; one with a branch
    applies only there, so Jharkhand can keep a local festival that Chandigarh
    does not. Stored as a plain date — a holiday is a whole day, never a time
    range.
    """
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("org_id", "branch_id", "day",
                                       name="uq_holiday_org_branch_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    branch: Mapped[Branch | None] = relationship()

    @property
    def scope(self) -> str:
        return self.branch.name if self.branch else "All branches"


class Followup(Base):
    """Evidence that someone chased an open task on a given day.

    One row per task per day. The day is part of the identity on purpose: a
    task pending for a fortnight needs chasing every day, and a tick from the
    first morning must not make the next thirteen look covered.
    """
    __tablename__ = "followups"
    __table_args__ = (UniqueConstraint("task_id", "day", name="uq_followup_task_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    task: Mapped["Task"] = relationship()
    by: Mapped[User] = relationship()


class OutboundMessage(Base):
    """Queue for WhatsApp/SMS notifications.

    Nothing is sent yet - rows are written by the app and a provider adapter
    (Gupshup / WATI / Meta Cloud API) drains the queue. Keeps the notification
    channel swappable.
    """
    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_phone: Mapped[str] = mapped_column(String(20))
    template: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
